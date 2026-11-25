import json
import os
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import deployments as tasks
from deployment_worker import Worker


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.project = root / "project with spaces"
        for name in ("Dockerfile", "scripts/deploy-performance.ps1",
                     "deployments/docker-desktop-performance/values.yaml", "charts/canway-quality/Chart.yaml"):
            file = self.project / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("fixture")
        for patcher in (
            patch.object(tasks, "ROOT", root),
            patch.object(tasks, "DATABASE", root / "tasks.db"),
            patch.object(tasks, "source_state", return_value={"commit": "abc", "fingerprint": "def"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.task_id = "DEP-0123456789ABCDEF"

    def submit(self, **overrides):
        args = dict(task_id=self.task_id, project_path=str(self.project), summary="tested local change")
        args.update(overrides)
        return tasks.submit(**args)

    def test_concurrent_submission_sends_once(self):
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda _: self.submit(), range(4)))
        self.assertEqual(sum(created for _, created in results), 1)
        self.assertEqual(tasks.get_status(self.task_id)["status"], "queued")
        with self.assertRaises(ValueError):
            self.submit(summary="different request")
        with self.assertRaises(ValueError):
            self.submit(task_id="DEP-0000000000000002")

    def test_claim_and_start_are_idempotent_and_path_bound(self):
        self.submit()
        with self.assertRaises(ValueError):
            tasks.claim(self.task_id, str(self.project.parent))
        claimed = tasks.claim(self.task_id, str(self.project))
        token = claimed["claim_token"]
        self.assertNotIn("claim_token", tasks.get_status(self.task_id))
        self.assertEqual(tasks.claim(self.task_id, str(self.project))["claim_token"], token)
        with self.assertRaises(ValueError):
            tasks.start(self.task_id, "wrong")
        with patch.object(tasks.subprocess, "Popen") as launch:
            launch.return_value.pid = 42
            with ThreadPoolExecutor(3) as pool:
                list(pool.map(lambda _: tasks.start(self.task_id, token), range(3)))
            self.assertEqual(launch.call_count, 1)
        with self.assertRaises(ValueError):
            tasks.stop_pending(self.task_id, "cannot cancel running worker")

    def test_spawn_failure_and_source_change(self):
        self.submit()
        token = tasks.claim(self.task_id, str(self.project))["claim_token"]
        with patch.object(tasks.subprocess, "Popen", side_effect=OSError("launch failed")):
            result = tasks.start(self.task_id, token)
        self.assertEqual(result["status"], "failed")
        self.assertIn("launch failed", result["error"])
        worker = Worker(self.task_id)
        with patch.object(tasks, "source_state", return_value={"commit": "changed"}):
            with self.assertRaisesRegex(RuntimeError, "Source changed"):
                worker.check_source()

    def test_cancel_preserves_history_and_blocks_delayed_execution(self):
        self.submit()
        tasks.record_dispatch(self.task_id, "waiting_for_editor", "example error")
        tasks.stop_pending(self.task_id, "cancel queued test")
        status = tasks.get_status(self.task_id)
        self.assertEqual(status["dispatch_stage"], "waiting_for_editor")
        self.assertEqual(status["status"], "cancelled")
        claimed = tasks.claim(self.task_id, str(self.project))
        self.assertIsNone(claimed["claim_token"])
        with patch.object(tasks.subprocess, "Popen") as launch:
            with self.assertRaises(ValueError):
                tasks.start(self.task_id, "")
            launch.assert_not_called()

    def test_receipt_does_not_imply_deployment_success(self):
        self.submit(mode="preflight")
        claimed = tasks.claim(self.task_id, str(self.project))
        self.assertEqual(claimed["status"], "received")
        self.assertIsNone(claimed["result"])
        self.assertEqual(claimed["request"]["mode"], "preflight")

    def test_sender_timeout_preserves_last_observed_stage(self):
        self.submit()
        tasks.record_dispatch(self.task_id, "waiting_for_editor")
        tasks.record_dispatch(self.task_id, "send_unconfirmed", "launcher timed out")
        status = tasks.get_status(self.task_id)
        self.assertEqual(status["dispatch_stage"], "waiting_for_editor")
        self.assertEqual(status["dispatch_error"], "launcher timed out")
        self.assertEqual(status["events"][-1]["message"], "launcher timed out")
        tasks.record_dispatch(self.task_id, "send_invoked")
        recovered = tasks.get_status(self.task_id)
        self.assertEqual(recovered["dispatch_stage"], "send_invoked")
        self.assertIsNone(recovered["dispatch_error"])

    def test_command_keeps_context_and_native_exit_codes(self):
        self.submit()
        worker = Worker(self.task_id)
        worker.kubectl = "kubectl.exe"
        self.assertIn("--context", worker.kube("get", "pods"))
        self.assertEqual(worker.env["HELM_KUBECONTEXT"], "docker-desktop")
        self.assertEqual(worker.env["DOCKER_CONTEXT"], "desktop-linux")
        with patch("deployment_worker.subprocess.run", return_value=subprocess.CompletedProcess([], 1)):
            with self.assertRaisesRegex(RuntimeError, "exit code 1"):
                worker.command("failure", ["fixture"])

    def test_duplicate_worker_does_not_run(self):
        import deployment_worker
        self.submit()
        with patch.object(deployment_worker.Worker, "execute") as execute:
            deployment_worker.main(self.task_id)
            execute.assert_not_called()

    def test_verification_accepts_canonical_images_and_ignores_completed_jobs(self):
        self.submit()
        worker = Worker(self.task_id)
        helm = self.project / ".tools/helm/windows-amd64/helm.exe"
        helm.parent.mkdir(parents=True)
        helm.touch()
        image = worker.request["image"]
        names = ["performance-canway-quality", "performance-canway-quality-worker",
                 "performance-canway-quality-scheduler"]
        deployments = [{"metadata": {"name": n}, "spec": {"template": {
            "spec": {"containers": [{"image": image}]}}}} for n in names]
        replicasets = [{"metadata": {"uid": n + "-rs", "ownerReferences": [
            {"kind": "Deployment", "name": n}]}} for n in names]
        pods = [{"metadata": {"name": n + "-pod", "ownerReferences": [{"uid": n + "-rs"}]},
                 "status": {"containerStatuses": [{"image": "docker.io/library/" + image,
                            "ready": True, "imageID": "sha256:example"}]}} for n in names]
        pods.append({"metadata": {"name": "completed-migration", "ownerReferences": [{"uid": "job"}]},
                     "status": {"containerStatuses": [{"image": image, "ready": False}]}})
        outputs = {
            "Docker Desktop engine": "linux",
            "Verify Helm target context": 'HELM_KUBECONTEXT="docker-desktop"',
            "Existing Helm release": json.dumps({"info": {"status": "deployed"}, "version": 1}),
            "Inspect built image": json.dumps([{"Id": "sha256:example", "Config": {
                "Labels": {"cw.handoff.task": self.task_id}}}]),
            "Application deployments": json.dumps({"items": deployments}),
            "Running application images": json.dumps({"items": pods}),
            "Application ReplicaSet owners": json.dumps({"items": replicasets}),
            "HTTP health": json.dumps({"status": 200}),
            "Final Helm release": json.dumps({"info": {"status": "deployed"}, "version": 2}),
        }
        with patch.object(worker, "command", side_effect=lambda label, *args: outputs.get(label, "")) as command:
            result = worker.execute()
        self.assertEqual({p["deployment"] for p in result["pods"]}, set(names))
        self.assertEqual(result["release_revision"], 2)
        self.assertEqual(result["health"]["status"], 200)
        health_args = next(call.args[1] for call in command.call_args_list if call.args[0] == "HTTP health")
        self.assertIn("pod/performance-canway-quality-pod", health_args)
        self.assertNotIn("deploy/performance-canway-quality", health_args)

    def test_windows_environment_restores_docker_plugin_folders(self):
        from handoff import windows_environment
        from mcp.client.stdio import get_default_environment
        with patch.dict(os.environ, get_default_environment(), clear=True):
            env = windows_environment()
        for name in ("SYSTEMROOT", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
                     "PROGRAMFILES", "TEMP"):
            self.assertTrue(Path(env[name]).is_dir(), name)


if __name__ == "__main__":
    unittest.main()

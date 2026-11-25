"""Fixed Docker Desktop performance deployment, started by the Claude MCP endpoint."""

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from contextlib import closing

import deployments as tasks
from handoff import windows_environment


class Worker:
    def __init__(self, task_id: str):
        self.task_id = task_id
        self.task = tasks.get_status(task_id)
        self.request = self.task["request"]
        self.project = Path(self.request["project_path"])
        self.folder = tasks.ROOT / "runs" / task_id
        self.folder.mkdir(parents=True, exist_ok=True)
        self.env = windows_environment()
        self.env["HELM_KUBECONTEXT"] = "docker-desktop"
        self.env["DOCKER_CONTEXT"] = "desktop-linux"
        self.kubectl = shutil.which("kubectl")
        self.docker = shutil.which("docker")
        self.powershell = str(Path(self.env["SYSTEMROOT"]) / "System32/WindowsPowerShell/v1.0/powershell.exe")
        self.sequence = 0

    def command(self, label: str, args: list[str], timeout: int = 60) -> str:
        self.sequence += 1
        output = self.folder / f"{self.sequence:02d}.log"
        with (self.folder / "deployment.log").open("a", encoding="utf-8") as log:
            log.write(f"\n[{tasks.now()}] {label}\n")
        # A regular file handles stderr progress and children inheriting handles.
        # The task stores paths and a bounded tail, never a pipe held by a daemon.
        with output.open("wb") as stream:
            try:
                completed = subprocess.run(
                    args, cwd=self.project, env=self.env, stdin=subprocess.DEVNULL,
                    stdout=stream, stderr=subprocess.STDOUT, timeout=timeout,
                    creationflags=subprocess.CREATE_NO_WINDOW, check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"{label} exceeded {timeout}s; inspect {output} and the actual environment") from exc
        text = output.read_text(encoding="utf-8-sig", errors="replace")
        with (self.folder / "deployment.log").open("a", encoding="utf-8") as log:
            log.write(text[-12000:])
        if completed.returncode:
            raise RuntimeError(f"{label} failed with exit code {completed.returncode}. Log: {output}\n{text[-2000:]}")
        return text

    def kube(self, *args: str) -> list[str]:
        return [self.kubectl, "--context", "docker-desktop", "--request-timeout=45s", "-n", "performance", *args]

    def script(self, *args: str) -> list[str]:
        return [self.powershell, "-NoProfile", "-NonInteractive", "-File",
                str(self.project / "scripts/deploy-performance.ps1"),
                "-Context", "docker-desktop", "-Namespace", "performance", "-Release", "performance",
                "-Tag", self.request["image"].split(":")[1], *args]

    def check_source(self) -> None:
        if tasks.source_state(self.project) != self.task["source"]:
            raise RuntimeError("Source changed after task submission. Submit the updated source as a new task.")

    def execute(self) -> dict:
        tasks.progress(self.task_id, "preflight", "Checking source, Docker, Kubernetes and existing release")
        self.check_source()
        if not self.docker or not self.kubectl:
            raise RuntimeError("Docker and kubectl must be installed on PATH")
        os_type = self.command("Docker Desktop engine", [
            self.docker, "--context", "desktop-linux", "version", "--format", "{{.Server.Os}}",
        ]).strip()
        if os_type != "linux":
            raise RuntimeError("Docker Desktop must use Linux containers")
        self.command("Docker Buildx plugin", [self.docker, "buildx", "version"])
        self.command("Kubernetes connectivity", self.kube("get", "namespace", "performance"))
        self.command("Required data volumes", self.kube("get", "pvc", "performance-results", "performance-postgres"))
        # Names only: secret values never enter task payloads or logs.
        self.command("Required secret references", self.kube(
            "get", "secret", "performance-canway-quality-secret", "performance-postgres-auth",
            "performance-redis-auth", "-o", "name",
        ))
        self.command("Render existing deployment chart", self.script("-DryRun"), 180)
        bundled = self.project / ".tools/helm/windows-amd64/helm.exe"
        helm = str(bundled) if bundled.is_file() else shutil.which("helm")
        if not helm:
            raise RuntimeError("Helm was not found after rendering")
        helm_env = self.command("Verify Helm target context", [helm, "env"])
        if 'HELM_KUBECONTEXT="docker-desktop"' not in helm_env:
            raise RuntimeError("Helm did not inherit the required docker-desktop context")
        helm_base = [helm, "--kube-context", "docker-desktop", "-n", "performance"]
        before = json.loads(self.command("Existing Helm release", [
            *helm_base, "status", "performance", "-o", "json",
        ]))
        if before["info"]["status"] != "deployed":
            raise RuntimeError(f"Existing Helm release is {before['info']['status']}")
        if self.request["mode"] == "preflight":
            return {"mode": "preflight", "context": "docker-desktop",
                    "release_revision": before["version"], "source": self.task["source"]}

        tasks.progress(self.task_id, "building", "Building a unique local image tag")
        self.command("Build application image", [
            self.docker, "--context", "desktop-linux", "buildx", "build",
            "--load", "--provenance=false", "--sbom=false", "--progress=plain",
            "--build-arg", "APT_MIRROR=https://mirrors.tencent.com/debian",
            "--build-arg", "APT_SECURITY_MIRROR=https://mirrors.tencent.com/debian-security",
            "--build-arg", "NPM_REGISTRY=https://registry.npmmirror.com",
            "--build-arg", "GIT_COMMIT=" + self.task["source"]["commit"],
            "--label", "cw.handoff.task=" + self.task_id,
            "-t", self.request["image"], ".",
        ], 3600)
        built = json.loads(self.command("Inspect built image", [
            self.docker, "--context", "desktop-linux", "image", "inspect", self.request["image"],
        ]))[0]
        if built["Config"]["Labels"].get("cw.handoff.task") != self.task_id:
            raise RuntimeError("Image does not belong to this task")
        self.check_source()

        tasks.progress(self.task_id, "deploying", "Upgrading performance in docker-desktop")
        self.command("Deploy existing Helm chart", self.script("-SkipBuild", "-SkipVerify"), 900)
        tasks.progress(self.task_id, "verifying", "Checking rollouts, application images and HTTP health")
        deployed = json.loads(self.command("Application deployments", self.kube(
            "get", "deploy", "-l", "app.kubernetes.io/instance=performance", "-o", "json",
        )))["items"]
        expected = {"performance-canway-quality", "performance-canway-quality-worker",
                    "performance-canway-quality-scheduler"}
        names = {item["metadata"]["name"] for item in deployed}
        if not expected.issubset(names):
            raise RuntimeError(f"Application deployments missing: {sorted(expected - names)}")
        for item in deployed:
            name = item["metadata"]["name"]
            if name not in expected:
                continue
            images = [c["image"] for c in item["spec"]["template"]["spec"]["containers"]]
            if self.request["image"] not in images:
                raise RuntimeError(f"{name} did not receive the submitted image")
            self.command(f"Rollout {name}", self.kube("rollout", "status", "deployment/" + name, "--timeout=300s"), 330)
        pods = json.loads(self.command("Running application images", self.kube(
            "get", "pods", "-l", "app.kubernetes.io/instance=performance", "-o", "json",
        )))["items"]
        replicasets = json.loads(self.command("Application ReplicaSet owners", self.kube(
            "get", "replicasets", "-l", "app.kubernetes.io/instance=performance", "-o", "json",
        )))["items"]
        owner_map = {}
        for replicaset in replicasets:
            for owner in replicaset["metadata"].get("ownerReferences", []):
                if owner["kind"] == "Deployment" and owner["name"] in expected:
                    owner_map[replicaset["metadata"]["uid"]] = owner["name"]
        running = []
        for pod in pods:
            if pod["metadata"].get("deletionTimestamp"):
                continue
            component = next((owner_map[o["uid"]] for o in pod["metadata"].get("ownerReferences", [])
                              if o["uid"] in owner_map), None)
            if component is None:
                continue
            for container in pod.get("status", {}).get("containerStatuses", []):
                if container["image"].removeprefix("docker.io/library/") == self.request["image"]:
                    if not container["ready"] or not container.get("imageID"):
                        raise RuntimeError("New application container is not ready")
                    running.append({"deployment": component, "pod": pod["metadata"]["name"],
                                    "image_id": container["imageID"]})
        if {p["deployment"] for p in running} != expected:
            raise RuntimeError("Could not verify all three application components")
        # Deployment label selectors can overlap worker/scheduler labels. Use
        # the verified ReplicaSet ownership rather than kubectl's label choice.
        web_pod = next(p["pod"] for p in running
                       if p["deployment"] == "performance-canway-quality")
        health_code = (
            "import json,urllib.request; "
            "r=urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/',timeout=20); "
            "print(json.dumps({'status':r.status,'body':r.read().decode()})); "
            "assert r.status==200"
        )
        health = json.loads(self.command("HTTP health", self.kube(
            "exec", "pod/" + web_pod, "-c", "canway-quality",
            "--", "/app/.venv/bin/python", "-c", health_code,
        )))
        after = json.loads(self.command("Final Helm release", [
            *helm_base, "status", "performance", "-o", "json",
        ]))
        if after["info"]["status"] != "deployed" or after["version"] <= before["version"]:
            raise RuntimeError("Helm release revision was not successfully advanced")
        return {
            "mode": "deploy", "context": "docker-desktop", "namespace": "performance",
            "image": self.request["image"], "built_image_id": built["Id"],
            "source": self.task["source"], "release_revision": after["version"],
            "pods": running, "health": health,
        }


def main(task_id: str) -> None:
    tasks.validate_id(task_id)
    # Even a duplicate process cannot execute the same task twice.
    with closing(tasks.connect()) as db, db:
        changed = db.execute(
            "UPDATE deployments SET status='preflight', heartbeat_at=? WHERE task_id=? AND status='starting'",
            (tasks.now(), task_id),
        ).rowcount
    if not changed:
        return
    stop = threading.Event()
    def heartbeat():
        while not stop.wait(10):
            with closing(tasks.connect()) as db, db:
                db.execute("UPDATE deployments SET heartbeat_at=? WHERE task_id=?", (tasks.now(), task_id))
    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        result = Worker(task_id).execute()
        tasks.finish(task_id, "succeeded", result=result)
    except Exception as exc:
        tasks.finish(task_id, "failed", error=f"{type(exc).__name__}: {exc}")
    finally:
        stop.set()
        thread.join(timeout=2)


if __name__ == "__main__":
    main(sys.argv[1])

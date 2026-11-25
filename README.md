# Claude Desktop Handoff

Windows 上的一对本地 MCP 服务。Codex 保存任务并通过 Claude Desktop 的 Code 界面发送交接指令。Claude 接收后调用 `cw-deploy-probe` 启动本地执行器，Codex 用同一个任务号读取进度和结果。

连通性探针只验证发送与回执。部署任务支持 `docker-desktop-performance`，需要符合该部署结构的 `canway-quality` 工作区。执行器在 `docker-desktop` 集群的 `performance` 命名空间构建并部署本地镜像，检查 Helm 迁移任务、应用滚动更新和 HTTP 健康状态。

## 安装

需要 Windows、Windows PowerShell 5.1、Python 3.10 或更高版本、Codex CLI、Claude Desktop（Code 模式）及 Claude Code CLI。启动和发送依赖 Claude 的 `claude://` 协议与桌面界面，因此需要可交互的 Windows 会话。部署模式还需要 Git、Docker Desktop Linux 引擎、Buildx 和可访问的本地 Kubernetes。项目的部署脚本负责定位 Helm。

在克隆后的目录中运行：

```powershell
py -3 -m venv .venv
$mcpRoot = (Get-Location).Path
$python = Join-Path $mcpRoot '.venv\Scripts\python.exe'
& $python -m pip install -r (Join-Path $mcpRoot 'requirements.txt')

codex mcp add claude-desktop-handoff -- $python -u -B (Join-Path $mcpRoot 'handoff.py')
claude mcp add --scope user cw-deploy-probe -- $python -B (Join-Path $mcpRoot 'probe.py') serve
```

两个服务要从同一份克隆启动，以便共享本地任务数据。给 Codex 用户配置中的 `[mcp_servers.claude-desktop-handoff]` 添加 `tool_timeout_sec = 180.0`，为 Claude Desktop 冷启动留出时间。重新加载客户端后，在 Claude Desktop 的 Code 模式确认接收服务已加载。

## 验证与使用

```powershell
& $python -B -m unittest test_deployments -v
powershell.exe -NoProfile -File (Join-Path $mcpRoot 'Test-DesktopHandoff.ps1') -CheckOnly
```

也可以去掉 `-CheckOnly`，让脚本发送一条新探针并等待 `ACK-` 回执。它不运行构建或部署。通过 Codex 发送探针时使用形如 `DEP-PROBE-0123456789ABCDEF` 的新任务号，并用 `get_handoff_status` 查询回执。

需要检查部署环境时，用 `submit_deployment` 的 `mode=preflight` 校验源码、Docker、Kubernetes 和 Helm 渲染。实际部署时，用 `mode=deploy`，提供 `DEP-` 加 16 位大写十六进制字符的新任务号、项目绝对路径和摘要。此模式会执行现有 Helm 迁移任务。用 `get_deployment_status` 跟踪原任务号至终态。

`probes.sqlite3`、`deployments.sqlite3` 和 `runs/` 在运行时生成。它们包含本地任务、项目路径与构建日志，安装和维护时应留在本机。当前执行配置依赖项目的 `Dockerfile`、`scripts/deploy-performance.ps1`、`charts/canway-quality/` 和 `deployments/docker-desktop-performance/values.yaml`。

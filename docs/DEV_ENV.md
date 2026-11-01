# Dev Environment

本文件记录 DailyCheck dev 环境的当前部署方式。每次 session 启动时应读取,以确保基于正确的部署模型工作。

## 当前模型（2026-07 起）

DailyCheck dev **不跑独立 Docker 容器**,而是部署到 `wdg-data-foundation` 项目的 `wdg-systemd` 容器内。
跟 WDG UI / Portal / Agent / WS Proxy / Scheduler 同 docker network,便于联调。

| 项 | host 路径 | 容器内路径 |
|---|---|---|
| 源码 | `~/Documents/GitHub/DailyCheck/` | `/opt/dailycheck/` (bind mount, live reload) |
| DB | `./db/master.db` + `./db/warehouses/*` | 同上 (bind mount, 同一份 SQLite 文件) |
| Flask systemd unit | `deploy/systemd/dailycheck-app.service` | `/etc/systemd/system/dailycheck-app.service` |
| MCP systemd unit | `deploy/systemd/dailycheck-mcp.service` | `/etc/systemd/system/dailycheck-mcp.service` |

## 入口

| 服务 | URL | 鉴权 |
|---|---|---|
| Flask | http://localhost:8080 | Flask session |
| MCP HTTP | http://localhost:5100 | Bearer `dev-mcp-token-for-testing` |

## 启动 / 恢复

```bash
# 1) 拉起 wdg-systemd 容器（首次 / 改了 wdg-data-foundation/docker-compose.yml 时）
cd /Users/ericmr/Documents/GitHub/wdg-data-foundation
docker compose up -d systemd-stack

# 2) install DailyCheck systemd units（容器重建后必跑,unit 在容器内不持久）
docker exec wdg-systemd bash /opt/dailycheck/scripts/install-in-wdg-systemd.sh
```

install 脚本是幂等的 — 依赖已装会跳过,unit 覆盖 + restart。

## 联调命令

```bash
# 服务状态
docker exec wdg-systemd systemctl status dailycheck-app dailycheck-mcp

# 重启（改 unit 后）
docker exec wdg-systemd systemctl restart dailycheck-app

# 实时日志
docker exec wdg-systemd journalctl -u dailycheck-app -f
docker exec wdg-systemd journalctl -u dailycheck-mcp -f

# 进容器
docker exec -it wdg-systemd bash
```

## 改动 → 生效

| 改动类型 | 生效方式 |
|---|---|
| Flask 业务代码 (`app.py` / `blueprints/*`) | bind mount + Flask `--debug` 自动 reload |
| MCP 代码 (`mcp_server/*`，含工具登记) | bind mount 后必须重启 `dailycheck-mcp`；无 reloader |
| systemd unit (`deploy/systemd/*.service`) | `systemctl restart dailycheck-{app,mcp}` |
| install 脚本 (`scripts/install-in-wdg-systemd.sh`) | bind mount + 重新跑 |
| WDG 容器配置 | 改 `wdg-data-foundation/docker-compose.yml` + `docker compose up -d` |
| 端口 / bind mount | 改 `wdg-data-foundation/docker-compose.yml` + 重建容器 |

## Standalone 启动（不走 docker）

只在 lint / 单测 / 不想依赖 wdg-systemd 时用:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
DAILYCHECK_SECRET_KEY=dev-key-change-me flask --app app run --host 0.0.0.0 --port 5001 --debug
```

## 已知坑

### 1. systemd unit 容器重建后丢失

`/etc/systemd/system/dailycheck-*.service` 是容器运行时安装,**不在镜像里**。
wdg-systemd 容器一旦重建,unit 就没了,需要重跑 `install-in-wdg-systemd.sh`。

### 2. `python3 -m mcp_server` 入口（默认 stdio）

确保 unit 里 `Environment=PATH=/var/www/.local/bin:...` 包含 pip 装包路径,
否则找不到 `mcp` 命令。HTTP service 的 `ExecStart` 必须显式使用
`python3 -m mcp_server --transport http --host 0.0.0.0 --port 5100`；裸入口默认 stdio。
HTTP 的 `/api/mcp/`、`/sse`、`/messages/` 与 `/health` 都由 Bearer middleware 保护。
健康检查需同时看 HTTP 200/503 与 `checks`，不能仅看顶层 `status`。

### 3. Flask debug + bind mount

macOS Docker Desktop 下 bind mount 文件变更触发 Flask reloader。
大文件改动或批量重命名可能引起连续 reload,正常现象。

### 4. 不再有独立 Dockerfile / docker-compose.dev.yml

旧的 standalone 部署方案已删除(2026-07)。如果新机器需要从零部署,直接装 Python + systemd 即可,或
把 wdg-systemd 起来后跑 install 脚本。

## 完整本地 pytest 门禁（Issue #114）

门禁需要真实退出码和独立会话结果；只有 FAILED 行或看似完整的摘要不能证明执行完整。
`check_known_failures.py` 的默认 CLI 缺少证据会失败，不再接受历史片段作为成功结果。
使用 requirements.txt 范围内的依赖（MCP1.x）和隔离的临时 DB，示例：

```bash
test_run_dir="$(mktemp -d)"
if DAILYCHECK_DB_DIR="${test_run_dir}/db" python -m pytest tests/ \
    --basetemp="${test_run_dir}/pytest" -q --tb=no -rfE --color=no \
    -p scripts.ci.pytest_evidence --dc-evidence="${test_run_dir}/session.json" \
    > "${test_run_dir}/pytest.txt" 2>&1; then
  pytest_exit=0
else
  pytest_exit=$?
fi
printf '%s\n' "${pytest_exit}" > "${test_run_dir}/native.exit"
python scripts/check_known_failures.py "${test_run_dir}/pytest.txt" \
  --pytest-exit-file "${test_run_dir}/native.exit" --session-file "${test_run_dir}/session.json" \
  --write-evidence "${test_run_dir}/evidence.json"
```

完整 native1 且只有已知失败仍允许通过；中断/internal/collection/setup错误、提前停止、
缺失或不一致终态/计数/证据不能通过。没有固定全仓用例数；参数化失败保留旧授权条件，实际参数 nodeid 和基础别名都须在清单，
基础ID不能通配新增参数变体。失败数只按实际用例计算，不按别名集合膨胀。
保留原生日志、native.exit、session.json 和 evidence.json 作为同一次运行的证据。

CI compact handoff 使用带真实 native/session/hash 的 JSON 信封；报告复用必须同时提供
`PYTEST_REUSE_FILE` 和 `PYTEST_REUSE_EVIDENCE`（strict checker生成的portable evidence），
建议显式提供两条路径；仅给出文件时按其 `.evidence.json` sidecar 查找证据。
未设置复用变量时一律 fresh pytest，即使默认路径有旧的有效文件/证据；显式空/缺失/损坏请求
失败且不重跑掩盖。调用者负责证据来自本次候选/同一流程；哈希/会话只证明数据一致，
不证明当前GitHEAD或操作/进程身份。
失败仍产02/03/ci-summary；原生退出码未知记null。报告门禁会重新核对证据，不只看gate_exit。

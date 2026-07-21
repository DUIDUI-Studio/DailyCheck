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
| 业务代码 (`app.py` / `blueprints/*` / `mcp_server/*`) | bind mount + Flask `--debug` 自动 reload |
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

### 2. `python3 -m mcp_server` 入口

确保 unit 里 `Environment=PATH=/var/www/.local/bin:...` 包含 pip 装包路径,
否则找不到 `mcp` 命令(MCP service `ExecStart` 用 `python3 -m mcp_server` 也行)。

### 3. Flask debug + bind mount

macOS Docker Desktop 下 bind mount 文件变更触发 Flask reloader。
大文件改动或批量重命名可能引起连续 reload,正常现象。

### 4. 不再有独立 Dockerfile / docker-compose.dev.yml

旧的 standalone 部署方案已删除(2026-07)。如果新机器需要从零部署,直接装 Python + systemd 即可,或
把 wdg-systemd 起来后跑 install 脚本。
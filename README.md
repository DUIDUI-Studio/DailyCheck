# DailyCheck — 轻量库存管理系统

## Dev 环境启动（在 wdg-systemd 容器里）

dev 跑在 `wdg-data-foundation` 项目的 `wdg-systemd` 容器内，跟 WDG UI / Portal / Agent 同 docker network，便于联调。

```bash
# 1) 启动 wdg-systemd 容器（首次或改了 docker-compose.yml）
cd /Users/ericmr/Documents/GitHub/wdg-data-foundation
docker compose up -d systemd-stack

# 2) install DailyCheck systemd units（容器重建后跑一次）
docker exec wdg-systemd bash /opt/dailycheck/scripts/install-in-wdg-systemd.sh
```

| 入口 | URL |
|---|---|
| Flask | http://localhost:8080 |
| MCP HTTP | http://localhost:5100 (Bearer `dev-mcp-token-for-testing`) |

绑定路径：
- 源码：`../DailyCheck` → 容器内 `/opt/dailycheck`（实时同步，改代码 Flask 自动 reload）
- DB：`./db/master.db`、`./db/warehouses/*.db`（跟 host 共用同一份 SQLite 文件）

## 调试命令

```bash
docker exec wdg-systemd systemctl status dailycheck-app dailycheck-mcp
docker exec wdg-systemd systemctl restart dailycheck-app              # 改 unit 后用
docker exec wdg-systemd journalctl -u dailycheck-app -f              # 实时日志
docker exec -it wdg-systemd bash                                     # 容器内 shell
```

## Standalone 启动（不走 docker）

只在 lint / 单测 / 不想依赖 wdg-systemd 时用：

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
DAILYCHECK_SECRET_KEY=dev-key-change-me flask --app app run --host 0.0.0.0 --port 5001 --debug
```

访问 `http://127.0.0.1:5001`。数据库文件 `db/master.db` + `db/warehouses/*.db`。

## 功能

- 后台品类管理
- 库存品管理
- 出库（含回滚）
- 库存盘点（自动记录差异）
- 库存查阅（支持搜索 + 低库存提示）
- 补货申请（含状态流转）
- 生产录入（产品配方驱动，按产出量自动扣减原料）
- MCP 服务器（13 个工具，AI Agent 数据访问）

## 生产部署

通过 GitHub Actions（`.github/workflows/deploy.yml`）push 到 VPS，详情见 `CLAUDE.md`。
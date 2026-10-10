# AGENT.md

## 项目概述
- 项目名：`DailyCheck`（轻量库存管理系统）
- 技术栈：`Flask 3.1.1 + SQLite + Jinja2 + 原生 CSS + PWA`，并内置 `MCP Server`（Starlette + Uvicorn）
- 目标场景：手机端优先的库存日常操作，覆盖品类管理、库存品管理、入库、出库、盘点、补货申请、生产录入；并通过 MCP 协议向 AI Agent 开放数据访问。
- 权威文档：
  - [`CLAUDE.md`](CLAUDE.md)（详尽）
  - [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)（**项目技术架构图**，必读）
  - [`docs/DEV_ENV.md`](docs/DEV_ENV.md)（dev 环境）
  - [`docs/mcp-configuration.md`](docs/mcp-configuration.md)（MCP 配置）
  - 本文件（AGENT.md）为精简开发指南

## 运行方式
1. 创建并激活虚拟环境
```bash
python3 -m venv .venv
source .venv/bin/activate
```
2. 安装依赖
```bash
pip install -r requirements.txt
```
3. 启动服务（standalone，仅 lint / 单测 / 不想依赖 wdg-systemd 时用）
```bash
flask --app app run --host 0.0.0.0 --port 5001 --debug
# 等价： RUNAPP=1 python3 app.py
```
4. Dev 环境（推荐，与 WDG 同 docker network 联调）：详见 `docs/DEV_ENV.md`
```bash
cd ~/Documents/GitHub/wdg-data-foundation && docker compose up -d systemd-stack
docker exec wdg-systemd bash /opt/dailycheck/scripts/install-in-wdg-systemd.sh
```
   - Flask：http://localhost:8080（systemd `dailycheck-app.service`）
   - MCP HTTP：http://localhost:5100（Bearer `dev-mcp-token-for-testing`）
- MCP 两种模式：`flask mcp`（stdio）/ `python3 -m mcp_server`（HTTP/SSE）

## 目录结构
- `app.py`：Flask 应用工厂 `create_app`，注册蓝图、过滤器、CLI
- `config.py`：路径配置、`FIXED_CATEGORIES`、角色等级 `ROLE_RANK`
- `permissions.py`：基于角色的访问控制（`@require_login`、`@require_role`、`@require_platform_admin`）
- `cli.py`：Flask CLI 命令（init-master、create-warehouse、clone-warehouse、create-user、assign-role、bootstrap、mcp、create-agent-token）
- `db/`：数据库连接（`get_master_db` / `get_warehouse_db`）、DDL、`clone.py`、`migrate.py`、`import_items.py`
- `blueprints/`：业务模块（16 个文件，含 `__init__.py` / `_helpers.py`）
  - `auth.py` 登录/登出/仓库选择（before_request 钩子）
  - `core.py` 仪表盘/首页/品类管理
  - `items.py` 库存品 CRUD / 库存视图 / 低库存预警
  - `stocktake.py` 盘点（开始 → 填写 → 提交/回滚批次）
  - `restock.py` 入库/补货申请
  - `outbound.py` 出库申请（创建/提交/回滚）
  - `production.py` 生产录入（产品 BOM + 生产批次 + CSV）
  - `reports.py` 入库/出库报表
  - `users.py` 用户管理
  - `import_items.py` CSV 导入库存品
  - `forecast.py` + `forecast_pure.py` 需求预测（含定时调度器，带锁防并发）
  - `procurement.py` + `procurement_pure.py` 采购建议
  - `notifications.py` + `notifications_pure.py` 应用内通知
  - `consumption.py` 消耗分析
  - `agent_tokens.py` Agent 令牌管理
  - `adjustment.py` 库存调整
- `mcp_server/`：MCP 协议服务器（`__main__.py` CLI 入口、`main.py` Starlette 应用、`protocol/`、`data/`、`service/`、`infra/`）
- `templates/`：Jinja2 模板（移动端优化，27+ 个）
- `static/`：移动端优先 `style.css`、`sw.js`（离线缓存）、`manifest.webmanifest`、离线回退页、`icons/`
- `tests/`：pytest 测试（约 27 个文件，夹具见 `tests/conftest.py`）
- `docs/`：`DEV_ENV.md`（dev 环境）、`mcp-configuration.md`（MCP 配置）

## 关键业务约束
- 固定品类（9 个，定义在 `config.py` 的 `FIXED_CATEGORIES`，不可增删）：包材、辅料、调味酱、调味酱 分、风味奶浆、乳制品、生产消耗品、生产工具、冰激凌成品
- 多仓库模型：`master.db` 管用户/仓库/权限；每个仓库一个独立 SQLite 文件（`db/warehouses/<code>.db`），数据物理隔离
- 库存品若存在关联业务记录（入库/盘点/补货）不可删除
- 入库记录删除时会回滚库存
- 产品定义独立于库存品（`products` 表），产品本身不入库、不产生库存
- 生产录入时若任一原料库存不足，提交被硬性拦截
- 盘点流程：开始盘点 → 进入会话 → 填写数量（可留空表示不盘）→ 提交生成批次 → 支持回滚批次

## 角色与权限
| 角色 | 等级 | 说明 |
|------|------|------|
| `staff` | 1 | 普通员工 |
| `manager` | 2 | 经理 |
| `admin` | 3 | 仓库管理员 |
| `is_admin` | — | 平台管理员（全局绕过仓库角色检查） |

## MCP 服务器（13 个工具）
通过 Model Context Protocol 提供 AI Agent 数据访问，支持 stdio（`flask mcp`）与 HTTP/SSE（`python3 -m mcp_server`，:5100）两种模式；Bearer Token（环境变量 `DAILYCHECK_MCP_TOKEN`）或 `agent_tokens` 表票据认证，支持路径级 ACL 与仓库范围权限。
工具：`items_list` / `items_detail` / `movements_list` / `restock_create` / `restock_list` / `outbound_create` / `outbound_list` / `outbound_rollback` / `warehouse_consumption` / `item_consumption` / `item_forecast` / `procurement_store` / `procurement_hub`

## 常见维护任务
- 修改固定品类：调整 `config.py` 中 `FIXED_CATEGORIES`
- 调整盘点批次回滚逻辑：`stocktake.py` 中的回滚函数
- 调整移动端 UI：`static/style.css` 与对应模板
- 调整 PWA 缓存策略：`static/sw.js`
- 新增 MCP 工具：`mcp_server/protocol/tools/` 与 `mcp_server/service/`

## 提交规范建议
- 功能提交前至少验证：
  - **必读 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)** —— 改动子系统前先看
  - 关键页面可访问（`/`、`/items`、`/stock-in`、`/stocktake`、`/restock`）
  - 盘点流程可走通（开始盘点 → 提交 → 回滚）
  - `pytest` 测试通过、`ruff check .` 无错
  - **触及架构子系统时同步更新 `docs/ARCHITECTURE.md`**（门禁：`scripts/git-hooks/pre-commit` 自动检测）
- 不要提交运行时文件：`.venv/`（含 `mcp_server/.venv/`）、`*.db`、`*.log`、截图、Playwright 调试文件（已在 `.gitignore` 覆盖）

## 后续优化建议
- 增加审计日志与操作人追踪
- 增加导出（CSV/Excel）
- 给盘点批次增加“查看明细”页面

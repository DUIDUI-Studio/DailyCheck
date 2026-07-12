# DailyCheck — 轻量库存管理系统

## 技术栈
- **后端**: Flask 3.1.1 + Python 3.10+
- **数据库**: SQLite（`master.db` 全局 + `db/warehouses/*.db` 每个仓库独立文件）
- **前端**: Jinja2 模板 + 原生 CSS（移动端优先，约 1235 行）+ PWA（Service Worker + manifest）
- **生产服务器**: Gunicorn 23.0.0（2 workers, port 8080）
- **MCP 服务器**: Python MCP SDK + Starlette + Uvicorn（支持 stdio 和 HTTP/SSE 两种模式）
- **测试**: pytest
- **Lint**: Ruff（target Python 3.10, line_length=100, 规则 E/F/W/I/B/UP）
- **部署**: GitHub Actions（push to main 触发，通过 SSH 推送至 VPS）

## 启动方式
```bash
# 开发
flask --app app run --host 0.0.0.0 --port 5001 --debug

# 或
RUNAPP=1 python3 app.py

# MCP 服务器（stdio 模式，供 Claude Code 使用）
flask mcp

# MCP 服务器（HTTP 模式，供远程 agent 使用）
python3 -m mcp_server
```

## 目录结构
```
app.py                     # Flask 应用工厂（create_app），注册蓝图、过滤器、CLI
config.py                  # 路径配置、固定品类、角色等级
permissions.py             # 基于角色的访问控制（login_required, require_role）
cli.py                     # Flask CLI 命令（init-master, create-user, assign-role, clone-warehouse, mcp 等）
db/
  __init__.py              # 数据库连接（get_master_db / get_warehouse_db）、DDL 定义
  clone.py                 # 仓库目录克隆
  migrate.py               # 旧版 inventory.db 迁移
  import_items.py          # CSV 导入库存品
blueprints/                # 14 个功能模块
  auth.py                  # 登录/登出/仓库选择，before_request 钩子
  core.py                  # 仪表盘/首页，品类管理
  items.py                 # 库存品 CRUD，库存视图，低库存预警
  stocktake.py             # 盘点（开始盘点 → 填写数量 → 提交/回滚批次）
  restock.py               # 入库/补货申请
  outbound.py              # 出库申请（创建/提交/回滚）
  production.py            # 生产录入（产品 BOM 定义 + 生产批次 + CSV 导入导出）
  reports.py               # 入库/出库报表
  users.py                 # 用户管理
  import_items.py          # CSV 导入库存品
  forecast.py              # 需求预测（含定时调度器）
  procurement.py           # 采购建议
  notifications.py         # 应用内通知
  agent_tokens.py          # Agent 令牌管理
  consumption.py           # 消耗分析
  _helpers.py              # Jinja2 过滤器与模板上下文
mcp_server/                # MCP 协议服务器
  __main__.py              # CLI 入口
  main.py                  # Starlette 应用（SSE + health + JSON-RPC）
  agent_mpc_pure.py        # 路径匹配逻辑
  protocol/                # MCP 协议层（server.py + tools/）
  data/                    # 数据访问层（unit_of_work.py, master.py, warehouse.py）
  service/                 # 业务逻辑层（auth, inventory, inbound, outbound, consumption, forecast, procurement）
  infra/                   # 基础设施（errors.py, access_log.py）
templates/                 # 27+ 个 Jinja2 模板（移动端优化）
static/
  style.css                # 移动端优先 CSS
  sw.js                    # Service Worker（离线缓存）
  manifest.webmanifest     # PWA 清单
  offline.html             # 离线回退页
tests/                     # 约 27 个测试文件
  conftest.py              # 测试夹具（logged_client, staff_client, helper 函数）
  mcp_server/              # MCP 服务器测试
  test_forecast_pure.py    # 预测逻辑测试
  test_forecast_route.py   # 预测路由测试
  test_forecast_scheduler.py
  test_procurement_pure.py
  test_procurement_route.py
  test_notifications_pure.py / test_notifications_route.py
  test_outbound_*.py       # 出库测试
  test_stocktake_*.py      # 盘点测试
  test_items_route.py      # 库存品路由测试
  test_production_grams.py # 生产克重转换测试
  test_import_items_*.py   # CSV 导入测试
  test_clone_warehouse.py  # 仓库克隆测试
  test_grams_to_stock.py   # 克重转换测试
  test_inventory_daily_avg.py
  test_summary_*.py        # 报表测试
  test_restock_delete_rollback.py
  test_nav_integration.py
  test_warehouse_categories_in_clause.py
  test_category_consumed_fix.py
scripts/
  sync_revenue.py          # 每日营收同步脚本（可能为 cron 任务）
docs/
  mcp-configuration.md     # MCP 配置文档
  superpowers/             # 开发计划文档
```

## MCP 服务器（13 个工具）

通过 Model Context Protocol 提供 AI Agent 数据访问能力。支持两种传输模式：
- **stdio**: 本地 Claude Code 使用（`flask mcp`）
- **HTTP/SSE**: 远程 Agent 通过 TCP 端口（`python3 -m mcp_server`）

| 工具 | 功能 |
|------|------|
| `items_list` | 列出仓库所有库存品 |
| `items_detail` | 单个库存品详情 |
| `movements_list` | 近期库存变动流水 |
| `restock_create` | 创建入库记录 |
| `restock_list` | 列出入库记录 |
| `outbound_create` | 创建出库申请（扣减库存） |
| `outbound_list` | 列出出库申请 |
| `outbound_rollback` | 回滚出库申请 |
| `warehouse_consumption` | 按品类汇总消耗 |
| `item_consumption` | 单品消耗统计（7天/30天/月度） |
| `item_forecast` | 单品需求预测 |
| `procurement_store` | 店铺采购建议 |
| `procurement_hub` | 全仓库采购建议 |

认证方式：Bearer Token（环境变量 `DAILYCHECK_MCP_TOKEN`）或 `agent_tokens` 表票据验证，支持路径级别的 ACL 和仓库范围权限。

## 核心业务规则
- **固定品类**: 包材、辅料、调味酱、调味酱 分、风味奶浆、乳制品、生产消耗品、生产工具、冰激凌成品（共 9 个，不可增删）
- **安全库存**: 每个库存品可设 safety_stock，低于时触发预警
- **库存品删除保护**: 存在关联业务记录（入库/盘点/补货）的库存品不可删除
- **入库回滚**: 删除入库记录时会自动回滚对应库存
- **产品与 BOM**: `products` 表独立于 `items`，产品不入库不产生库存；生产录入时若任一原料库存不足则硬性拦截
- **盘点流程**: 开始盘点 → 进入盘点会话 → 填写盘点数量（可留空）→ 提交生成批次 → 支持回滚批次
- **出库回滚**: 支持回滚出库申请，恢复库存
- **需求预测**: 后台定时调度器运行预测，带锁机制防止并发，支持失败计数追踪
- **采购建议**: 基于消耗速率、覆盖天数、安全库存、在途数量计算建议采购量

## 角色与权限
| 角色 | 等级 | 说明 |
|------|------|------|
| `staff` | 1 | 普通员工 |
| `manager` | 2 | 经理 |
| `admin` | 3 | 仓库管理员 |
| `is_admin` | — | 平台管理员（全局绕过仓库角色检查） |

权限装饰器：`@require_login`、`@require_role(min_role)`、`@require_platform_admin`

## 测试相关
```bash
# 运行全部测试
pytest

# 运行特定测试文件
pytest tests/test_forecast_pure.py -v

# 运行 MCP 测试
pytest tests/mcp_server/ -v
```

测试夹具在 `tests/conftest.py` 中可用：
- `logged_client`：临时数据库 + 管理员 session
- `staff_client`：临时数据库 + staff 角色
- `_seed_item`、`_seed_outbound`、`_seed_production_consumption` 等辅助函数

## CLI 命令
| 命令 | 功能 |
|------|------|
| `flask init-master` | 初始化 master.db 数据库 |
| `flask create-warehouse <code> <name>` | 创建新仓库 |
| `flask clone-warehouse <src> <new> <name>` | 克隆仓库目录（含品类、库存品、产品、BOM） |
| `flask create-user <username> <password>` | 创建用户（`--admin` 创建平台管理员） |
| `flask assign-role <username> <warehouse_code> <role>` | 分配仓库角色 |
| `flask list-users` | 列出所有用户及其角色 |
| `flask bootstrap` | 一键初始化（创建 master.db + 管理员 + 第一个仓库） |
| `flask mcp` | 启动 MCP 服务器（stdio 模式） |
| `flask create-agent-token <name>` | 创建 Agent 令牌（支持 `--read-paths`、`--write-paths`、`--warehouses`） |

## 部署
- GitHub Actions（`.github/workflows/deploy.yml`）：push 到 main 时自动部署
- 部署流程：tar 项目 → SSH 到 VPS → kill gunicorn → 解压 → 创建 venv → pip install → 重启 gunicorn（2 workers, port 8080）
- 数据库文件（`master.db`、`db/warehouses/*.db`）保留在服务端，不随部署推送

## 环境变量
| 变量 | 说明 |
|------|------|
| `DAILYCHECK_SECRET_KEY` | Flask secret key |
| `DAILYCHECK_MCP_TOKEN` | MCP HTTP 模式 Bearer Token |

## 提交前验证
1. 测试通过：`pytest`
2. Lint 检查：`ruff check .`
3. 关键页面可访问：`/`、`/items`、`/stock-in`、`/stocktake`、`/restock`
4. 盘点流程可走通
5. 不提交运行时文件：`.venv/`、`*.db`、`*.log`、截图/PW 调试文件

## 代码规范
- Python 3.10+ target
- 行长度 100
- Ruff linting（E/F/W/I/B/UP，忽略 E501、B008）
- 遵循 `AGENT.md` 中更详细的开发指南

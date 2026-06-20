# AGENT.md

## 项目概述
- 项目名：`DailyCheck`（轻量库存管理系统）
- 技术栈：`Flask + SQLite + Jinja2 + 原生 CSS + PWA`
- 目标场景：手机端优先的库存日常操作，覆盖品类管理、库存品管理、入库、盘点、补货申请。

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
3. 启动服务
```bash
flask --app app run --host 0.0.0.0 --port 5001 --debug
```

## 目录结构
- `app.py`：主应用入口、路由、数据库初始化、业务逻辑
- `templates/`：Jinja 页面模板
- `static/style.css`：移动端优先样式
- `static/manifest.webmanifest`：PWA 清单
- `static/sw.js`：Service Worker（缓存与离线策略）
- `static/offline.html`：离线回退页
- `static/icons/`：PWA 图标
- `blueprints/production.py`：生产录入（产品/BOM/生产批次/回退/CSV）

## 关键业务约束
- 固定品类：`包材`、`原料`、`工具`
- 品类页仅展示固定品类，不允许新增/删除
- 库存品若存在关联业务记录（入库/盘点/补货）不可删除
- 入库记录删除时会回滚库存
- 产品定义独立于库存品（products 表），产品本身不入库、不产生库存
- 生产录入时若任一原料库存不足，提交被硬性拦截

## 盘点流程（当前设计）
1. 在 `/stocktake` 点击“开始盘点”
2. 跳转 `/stocktake/session`，展示当前库存状态
3. 用户填写盘点数量（可留空，表示该项本次不盘）
4. 提交后生成一条“盘点批次”并写入明细 `stocktakes`
5. 在盘点列表可对批次执行“回滚批次”

## 数据表说明
- `categories`：品类主数据（含固定品类）
- `items`：库存品（SKU、库存、安全库存、单位）
- `stock_movements`：库存变动流水（入库/盘点调整/回滚）
- `stocktakes`：盘点明细记录（支持 `batch_id`）
- `stocktake_batches`：盘点批次（支持回滚状态）
- `restock_requests`：补货申请
- `products`：产品定义（独立于库存品）
- `product_bom`：产品-原料配方
- `production_runs`：生产批次
- `production_run_items`：批次原料消耗

## 常见维护任务
- 修改固定品类：调整 `app.py` 中 `FIXED_CATEGORIES`
- 调整盘点批次回滚逻辑：`rollback_stocktake_batch()`
- 调整移动端 UI：`static/style.css` 与对应模板
- 调整 PWA 缓存策略：`static/sw.js`

## 提交规范建议
- 功能提交前至少验证：
  - 关键页面可访问（`/`, `/items`, `/stock-in`, `/stocktake`, `/restock`）
  - 盘点流程可走通（开始盘点 -> 提交 -> 回滚）
- 不要提交运行时文件：
  - `.venv/`, `inventory.db`, 截图临时文件

## 后续优化建议
- 增加鉴权与角色权限（仓管/审批人）
- 增加审计日志与操作人追踪
- 增加导出（CSV/Excel）
- 给盘点批次增加“查看明细”页面

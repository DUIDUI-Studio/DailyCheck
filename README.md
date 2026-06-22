# 轻量库存管理软件

功能：
- 后台品类管理
- 库存品管理
- 出库
- 库存盘点（自动记录差异）
- 库存查阅（支持搜索 + 低库存提示）
- 补货申请（含状态流转）
- 生产录入（产品配方驱动，按产出量自动扣减原料）

## 启动

```bash
# 首次使用
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 启动（指定端口 5001）
DAILYCHECK_SECRET_KEY=dev-key-change-me RUNAPP=1 python3 app.py
```

或通过 Flask CLI 指定端口：

```bash
DAILYCHECK_SECRET_KEY=dev-key-change-me flask --app app run --host=0.0.0.0 --port=5001
```

### 首次启动后初始化

创建管理员账号和仓库：

```bash
DAILYCHECK_SECRET_KEY=dev-key-change-me flask --app app create-user admin admin123 --admin
DAILYCHECK_SECRET_KEY=dev-key-change-me flask --app app create-warehouse <code> <name>     # 空仓
DAILYCHECK_SECRET_KEY=dev-key-change-me flask --app app clone-warehouse <src> <new> <name> # 从 src 复制目录结构（库存归零）
```

### 重启

```bash
# 先停旧进程
kill $(lsof -ti:5001) 2>/dev/null

# 再启动
DAILYCHECK_SECRET_KEY=dev-key-change-me RUNAPP=1 python3 app.py
```

访问：`http://127.0.0.1:5001`

数据库：`db/master.db`（主库） + `db/warehouses/*.db`（每个仓库独立库）

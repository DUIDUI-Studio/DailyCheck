# Staff 入口调整 — land.html 库存管理卡指向 /inventory

## 背景

DailyCheck 的角色分 staff / manager / admin(见 `permissions.py`)。当前实现里:

- `templates/base.html` 已经按 `current_role != 'staff'` 隐藏侧边栏/移动端的「品类与品项」链接,staff 看不到该入口
- `templates/land.html` 上的「库存管理」卡片的 `href` 指向 `items.items_list`,而该路由有 `@require_role("manager")`
- 结果:staff 在 land 页能点「库存管理」卡,但点下去落到 403

需求:staff 点「库存管理」卡能进到能看的页面;manager/admin 行为不变;侧边栏对 staff 的隐藏逻辑保持。

## 目标

1. staff 点击 land.html 上的「库存管理」卡 → 进入 `/inventory`(库存查阅)
2. staff 的侧边栏/底部导航继续保持隐藏「品类与品项」/「品项」(已正确,不动)
3. manager/admin 行为完全不变

## 改动

文件:`templates/land.html`

第 11 行:

```diff
-    <a class="land-card" href="{{ url_for('items.items_list') }}">
+    <a class="land-card" href="{{ url_for('items.inventory_view') }}">
```

第 13 行卡片描述文案,从「品项 / 盘点 / 入库 / 出库 / 调整 / 库存查阅」改为「盘点 / 入库 / 出库 / 调整 / 库存查阅」(去掉「品项」一词,使其对 staff 也准确)。

## 不改动

- `blueprints/items.py` — `items_list` / `edit_item` / `delete_item` 的 `@require_role("manager")` 不动
- `blueprints/core.py` — `/categories` 路由不动
- `templates/base.html` — `current_role != 'staff'` 隐藏「品类与品项」/「品项」链接的逻辑不动(已正确)
- staff 直接访问 `/items` / `/categories` 仍会被 403 挡住(预期)

## 数据流 / 错误处理

staff 点卡 → `/inventory` → `inventory_view`(已有 `@require_login`,无 role 限制)→ `templates/inventory.html` 已渲染。不需要新增错误处理路径。

## 测试

手工验证(无需新增单测,因为 href 是 Jinja 渲染产物,而 `inventory_view` 不引入新逻辑):

1. 用 staff 账号登录 → 选仓库 → 在 land 页点「库存管理」卡 → URL 应落到 `/inventory`,看到库存卡片列表
2. 用 staff 账号登录 → 侧边栏/底部导航均不出现「品类与品项」/「品项」链接(已通过)
3. 用 manager 账号登录 → land 页「库存管理」卡 → 仍然指向 `/inventory`(文案去掉了「品项」一词,但卡片点击行为是统一指向 /inventory)

> 注:manager/admin 的旧体验原本是点「库存管理」卡进入 `/items`(品项与品类页),改动后也落到 `/inventory`。这是预期的:统一入口,从库存查阅出发,manager/admin 仍可通过侧边栏/底部导航的「品类与品项」链接直达 /items。

## 风险

极低:

- href 写错会让 Flask 启动时 `url_for` 抛错,可在启动时发现
- 文案变化是纯显示层,无破坏性
- `inventory_view` 不变,SQL 不变,数据流不变

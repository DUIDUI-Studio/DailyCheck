# PRD：门店订货（Store Ordering）

- 日期：2026-10-04
- 作者：software-product-manager
- 状态：待团队评审
- 分支：`feat/store-ordering`
- 定位：**简单 PRD**。明确「门店向总部配送中心订货」要什么、不给什么。

---

## 0. 项目信息

| 项 | 内容 |
| --- | --- |
| Language | 中文 |
| Programming Language | Flask + Jinja2 + SQLite（与 DailyCheck 主栈一致） |
| Project Name | store_ordering |
| 原始需求 | 开发「门店订货」功能：门店向总部配送中心订货，支持购物车、提交订单、审批、出库配送、库存校验、通知、历史查询。 |

---

## 1. 现状事实基线（已核实）

| 事实 | 来源 |
| --- | --- |
| 多仓架构：`master.db` 管理用户/仓库/权限；每仓独立 SQLite 文件 `db/warehouses/{code}.db` | `config.py` / `db/__init__.py` |
| 仓库类型 `warehouse_type` 当前仅有 `'storefront'`（门店，有库存）和 `'rd'`（研发中心，无库存） | `db/__init__.py:81` |
| 用户角色：`staff`(1)、`manager`(2)、`admin`(3)；`users.is_admin=1` 为平台管理员，可 bypass 仓级权限 | `config.py:55` / `permissions.py` |
| 已有通知基础设施：`notifications` 表 + `notifications_pure.emit_event()`，当前支持 `recipe_published` / `canonical_published` 等事件类型 | `db/__init__.py:126` / `blueprints/notifications_pure.py` |
| 已有出库扣库逻辑：`outbound_requests` + `stock_movements` + `items.quantity` 扣减，支持回退/删除 | `blueprints/outbound.py` |
| 已有品项主数据（canonical items）与跨仓绑定机制：`canonical_items` / `canonical_categories` / 仓内 `items.canonical_id` | `db/__init__.py:245` |
| 导航入口在 `templates/base.html` 中按 `is_admin / is_storefront / is_rd` 控制显隐 | `templates/base.html` |
| 当前没有「配送中心」这一仓库类型，也没有跨仓订单/购物车/配送相关表 | 全局代码检索 |

**关键结论**：本功能需要新增跨仓订单数据模型，并解决「配送中心」在现有仓库体系中的定位问题。订单天然跨两个仓库，数据应放在 `master.db` 中最易查询与追踪。

---

## 2. 产品目标

1. **让门店能一站式向配送中心要货**：通过可维护的购物车直接生成订货单，减少线下沟通与人工汇总。
2. **让配送中心对订单全流程可控**：从审批、拣货、出库到配送，状态透明，库存扣减可追溯。
3. **让相关人员在关键节点自动收到通知**：提交、审批、出库、送达四个节点自动推送，避免漏单。

---

## 3. 用户故事

**US-1｜门店店员：像购物车一样点货**
作为 wh_001 的店员，我进入「门店订货」页面，选择本次要订货的配送中心，看到该中心可订的商品列表，把需要的品项加入购物车并调整数量，最后提交为一张订货单，填写期望到货日期和备注。

**US-2｜配送中心经理：审批门店订单**
作为 dc_001 的经理，我在「订货审批」列表看到待审批订单，查看每个品项的数量和库存余量，选择「通过」或「拒绝」并填写原因；通过后订单进入待出库状态，拒绝后订单退回给门店。

**US-3｜配送中心店员：按单出库**
作为 dc_001 的店员，我看到已审批订单的「出库清单」，核对数量后点击「确认出库」，系统自动从配送中心库存扣减对应数量并生成出库记录，同时通知门店订单已发货。

**US-4｜门店店长：查历史订单和到货情况**
作为 wh_001 的店长，我打开「订货历史」，能按时间、状态筛选本店所有订单，点进详情看到每个品项的订货数、实出数、配送记录和当前状态。

**US-5｜总部运营：看全部门店订货情况**
作为平台管理员，我打开「全部门店订货看板」，按门店、配送中心、状态筛选订单，掌握整体要货量、审批积压和配送进度。

---

## 4. 需求池

### P0——最小可用闭环（必须上线）

#### P0-1 配送中心与门店的订货关系

- 新增仓库类型 `distribution_center`（简称 `dc`），用于标识配送中心仓库；或 alternatively 在 `warehouses` 表中增加 `is_distribution_center` 标记（待确认见 Q1）。
- 配送中心仓库具备实体库存，与 `storefront` 共享 `items` / `stock_movements` 等库存模型。
- 一家门店在一次订货中只能选择一个配送中心；一个配送中心可服务多家门店。

#### P0-2 购物车

- 每个门店用户在 `master.db` 中拥有独立的购物车（按 `user_id + store_warehouse_code`）。
- 购物车中的商品以 `canonical_id` 作为跨仓统一键，显示标准名、单位、当前配送中心库存。
- 用户可：添加商品、调整数量、删除单品、清空购物车。
- 切换配送中心时，若商品在该中心无库存/无绑定，系统提示并保留有效项或清空（具体策略待确认见 Q3）。

#### P0-3 提交订单

- 用户从购物车进入「提交订单」页，填写：期望到货日期（默认明天，必须 ≥ 今天）、备注。
- 提交时系统校验：
  - 配送中心当前库存是否 ≥ 订货数量（按基础单位），不足则列出短缺项并阻止提交。
  - 门店与配送中心是否都已绑定对应 `canonical_id`（若门店未绑定，待确认见 Q4）。
- 提交成功后订单状态为 `pending`（待审批），购物车清空。
- 触发通知 `store_order_submitted` 给：该配送中心的 `manager`/`admin`、平台管理员。

#### P0-4 订单审批

- 配送中心的 `manager`/`admin` 和平台管理员可在「订货审批」页看到本配送中心待审批订单。
- 审批动作：通过 / 拒绝。
  - 通过：状态变为 `approved`，触发 `store_order_approved` 通知给下单人及门店 `manager`/`admin`。
  - 拒绝：状态变为 `rejected`，必须填写拒绝原因，触发 `store_order_rejected` 通知给下单人及门店 `manager`/`admin`。
- 门店侧不能审批自己门店提交的订单。

#### P0-5 出库配送与库存扣减

- 审批通过后，配送中心 `staff`/`manager`/`admin` 在「出库清单」中按订单执行出库。
- 出库时系统：
  - 再次校验库存（防止提交后库存变化）。
  - 在配送中心仓内扣减 `items.quantity`。
  - 在配送中心仓内写入 `stock_movements`（action = `门店订货出库`）。
  - 生成配送记录（配送单号、出库人、出库时间、关联订单）。
- 出库后订单状态变为 `shipped`，触发 `store_order_shipped` 通知给下单人及门店 `manager`/`admin`。
- 门店确认收货或系统自动到货后，状态变为 `delivered`，触发 `store_order_delivered` 通知。

#### P0-6 库存校验

- 提交订单时做软校验：提示当前库存不足，阻止提交。
- 出库时做实校验：若库存不足则出库失败并提示，订单保持 `approved` 状态等待补货或人工处理。

#### P0-7 通知

- 扩展 `notifications_pure.ALLOWED_EVENT_TYPES`，新增：
  - `store_order_submitted`
  - `store_order_approved`
  - `store_order_rejected`
  - `store_order_shipped`
  - `store_order_delivered`
- 每个事件通知目标用户按角色与仓库归属自动计算，不通知提交人自己（除非他也是审批/配送相关人员）。

#### P0-8 历史查询

- 门店侧：登录用户当前所在门店可查看本店所有历史订单及详情。
- 配送中心侧：当前所在配送中心可查看发到本中心的所有订单及详情。
- 平台管理员：可查看全部订单，支持按门店、配送中心、状态、时间范围筛选。

---

### P1——体验增强

- **P1-1 订货Catalog搜索与筛选**：按品类、名称、库存状态筛选可订商品。
- **P1-2 部分出库**：一个订单可分多次出库，记录每次出库数量，`fulfilled_quantity` 累计。
- **P1-3 门店收货确认**：门店用户在订单详情点击「确认收货」，填写实收数量/差异备注；差异自动生成差异记录。
- **P1-4 订单取消**：在审批前，下单人可取消订单；审批后仅配送中心 `manager`/`admin` 可取消。
- **P1-5 订货量建议**：基于门店近 7 天消耗与安全库存，给出建议订货量（复用 `procurement` 计算结果）。

---

### P2——后续可扩展

- **P2-1 配送轨迹/物流单号**：支持填写物流公司、单号、预计到达时间。
- **P2-2 订货周期与截单时间**：配置每周订货日、截单时间，超过截单自动顺延。
- **P2-3 门店订货额度/授信**：按门店设置月度订货上限或账期。
- **P2-4 报表**：按门店/配送中心/品类的订货量、出库量、差异率统计。

---

## 5. 角色权限矩阵

| 功能 | staff（门店） | manager（门店） | admin（门店） | staff（DC） | manager（DC） | admin（DC） | 平台管理员 |
| --- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| 浏览可订商品 / 加购 | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ | ✅ |
| 维护购物车 | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ | ✅ |
| 提交订单 | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ | ✅ |
| 查看本店订单历史 | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ | ✅ |
| 审批订单（本DC） | ❌ | ❌ | ❌ | ❌ | ✅ | ✅ | ✅ |
| 查看本DC订单列表 | ❌ | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ |
| 执行出库 | ❌ | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ |
| 查看全部订单/报表 | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ |
| 取消订单（审批后） | ❌ | ❌ | ❌ | ❌ | ✅ | ✅ | ✅ |

说明：
- 门店角色只能操作当前所在门店的购物车与订单。
- 配送中心角色只能处理发到当前所在配送中心的订单。
- 平台管理员 `is_admin=1` 可查看所有仓库数据，但为了不破坏业务闭环，默认不代操作出库/审批（技术上保留能力）。

---

## 6. 核心业务流程

### 流程 A：门店提交订单

1. 门店用户选择配送中心 → 系统加载该中心可订商品（有 `canonical_id` 绑定的品项）。
2. 用户加购、调整数量 → 数据写入 `store_order_carts` / `store_order_cart_items`。
3. 用户填写期望到货日期、备注 → 点击提交。
4. 系统校验配送中心库存 ≥ 订货量；校验门店已绑定对应 `canonical_id`。
5. 校验通过：购物车转为订单，状态 `pending`，清空购物车。
6. 发送 `store_order_submitted` 通知给配送中心审批人。

### 流程 B：配送中心审批

1. 配送中心 `manager`/`admin` 进入「订货审批」页，看到 `pending` 订单。
2. 查看订单明细与当前库存，选择「通过」或「拒绝」。
3. 通过 → 状态 `approved`，通知门店；拒绝 → 状态 `rejected`，必须填原因，通知门店。

### 流程 C：出库配送

1. 配送中心 `staff`/`manager`/`admin` 在「出库清单」看到 `approved` 订单。
2. 按实物拣货后点击「确认出库」。
3. 系统再次校验库存，扣减配送中心 `items.quantity`，写入 `stock_movements`。
4. 生成配送记录，订单状态变为 `shipped`，通知门店。
5. 门店收货后（P1 为人工确认，P0 可为自动标记）状态变为 `delivered`，通知门店。

### 流程 D：历史查询

1. 门店用户打开「订货历史」，系统按 `store_warehouse_code` 返回本店订单。
2. 配送中心用户打开「订单列表」，系统按 `dc_warehouse_code` 返回本中心订单。
3. 平台管理员打开「全部订单」，可跨仓库筛选。

---

## 7. 页面/功能清单

| 页面/功能 | URL 建议 | 主要用户 | 说明 |
| --- | --- | --- | --- |
| 订货商品目录 | `/store-ordering/catalog` | 门店 | 选择 DC、浏览商品、加购 |
| 购物车 | `/store-ordering/cart` | 门店 | 调整数量、删除、去提交 |
| 提交订单 | `/store-ordering/cart/submit` | 门店 | 填写日期/备注、提交 |
| 门店订货历史 | `/store-ordering/orders` | 门店 | 本店订单列表与筛选 |
| 订单详情 | `/store-ordering/orders/<int:order_id>` | 门店/DC/管理员 | 明细、审批记录、配送记录 |
| 待审批列表 | `/store-ordering/review` | DC/管理员 | 本 DC 待审批订单 |
| 审批操作 | `/store-ordering/orders/<int:order_id>/review` | DC/管理员 | 通过/拒绝 |
| 出库清单 | `/store-ordering/shipments` | DC | 已审批待出库订单 |
| 执行出库 | `/store-ordering/orders/<int:order_id>/ship` | DC | 确认出库、扣库存 |
| 全部订单看板 | `/store-ordering/admin/orders` | 平台管理员 | 跨仓库筛选与查看 |
| 导航入口 | `templates/base.html` | 所有人 | 按仓库类型与角色显隐 |

---

## 8. 数据实体草稿

### 8.1 放在 `master.db` 的表（跨仓订单数据）

```sql
-- 购物车
CREATE TABLE store_order_carts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    store_warehouse_code TEXT NOT NULL,
    dc_warehouse_code TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, store_warehouse_code)
);

-- 购物车明细
CREATE TABLE store_order_cart_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cart_id INTEGER NOT NULL,
    canonical_id INTEGER NOT NULL,
    quantity REAL NOT NULL,
    unit TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (cart_id) REFERENCES store_order_carts(id) ON DELETE CASCADE
);

-- 订单主表
CREATE TABLE store_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no TEXT NOT NULL UNIQUE,
    store_warehouse_code TEXT NOT NULL,
    dc_warehouse_code TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', -- pending/approved/rejected/shipped/delivered/cancelled
    requested_by INTEGER NOT NULL,
    expected_delivery_date TEXT,
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    approved_by INTEGER,
    approved_at TEXT,
    approved_note TEXT,
    shipped_by INTEGER,
    shipped_at TEXT,
    delivered_at TEXT,
    cancelled_by INTEGER,
    cancelled_at TEXT,
    cancel_reason TEXT
);

CREATE INDEX idx_store_orders_store ON store_orders(store_warehouse_code, status, created_at DESC);
CREATE INDEX idx_store_orders_dc ON store_orders(dc_warehouse_code, status, created_at DESC);

-- 订单明细
CREATE TABLE store_order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    canonical_id INTEGER NOT NULL,
    dc_item_id INTEGER,           -- 配送中心仓内 items.id（出库时回填）
    store_item_id INTEGER,        -- 门店仓内 items.id（用于收货/入库）
    quantity REAL NOT NULL,       -- 订货数量（基础单位）
    unit TEXT NOT NULL,
    fulfilled_quantity REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending', -- pending/fulfilled/partial/cancelled
    created_at TEXT NOT NULL,
    FOREIGN KEY (order_id) REFERENCES store_orders(id) ON DELETE CASCADE
);

CREATE INDEX idx_store_order_items_order ON store_order_items(order_id);

-- 订单状态历史
CREATE TABLE store_order_status_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    actor_id INTEGER,
    note TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (order_id) REFERENCES store_orders(id) ON DELETE CASCADE
);

CREATE INDEX idx_store_order_status_history_order ON store_order_status_history(order_id, created_at DESC);

-- 配送记录
CREATE TABLE store_order_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    delivery_no TEXT NOT NULL UNIQUE,
    shipped_by INTEGER,
    shipped_at TEXT NOT NULL,
    delivered_at TEXT,
    tracking_note TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (order_id) REFERENCES store_orders(id) ON DELETE CASCADE
);
```

### 8.2 配送中心仓内（复用/扩展）

- 出库时直接更新该中心 `items.quantity` 并写入 `stock_movements`。
- 推荐在 `stock_movements.note` 中记录订单号（如 `门店订货出库 #SO-20261004-001`），便于追溯。
- 如需强关联，可考虑在配送中心仓新增 `store_order_outbounds` 表；P0 建议复用 `stock_movements` + 订单号备注即可。

### 8.3 门店仓内（P0 不自动入库，P1 再扩展）

- P0 阶段，门店订货只影响配送中心库存；门店收货为业务确认动作，不在仓内自动加库存。
- P1 可在门店仓写入 `restock_requests` 或新增 `store_order_receipts` 表实现自动入库。

---

## 9. 待确认问题

### Q1. 配送中心是新建仓库类型，还是在现有 `storefront` 上打标记？

- **倾向方案 A**：新增 `warehouse_type = 'distribution_center'`。
  - 理由：语义清晰，与 `storefront` / `rd` 并列；导航、权限、库存操作都可按类型区分。
  - 代价：需要在 `init_master_db` / `migrate` 中支持新类型，并在 `permissions.py` / `base.html` 中增加对应分支。
- **备选方案 B**：在 `warehouses` 表增加 `is_distribution_center INTEGER DEFAULT 0`。
  - 理由：改动最小，一个仓库可同时是门店和配送中心。
  - 代价：类型判断不再单一，所有按 `warehouse_type` 拦截的代码都要额外检查该标记。

### Q2. 订单数据放在 `master.db` 还是各自仓库？

- **倾向**：放在 `master.db`（已在 PRD 中按此设计）。
- 理由：订单天然跨两个仓库，放在任一方都会增加 join 复杂度；`master.db` 已承担跨仓元数据职责。

### Q3. 切换配送中心时购物车如何处理？

- **倾向**：清空当前购物车并给出提示，因为一个购物车只能属于一个配送中心。
- 备选：按 `dc_warehouse_code` 拆分购物车，切换时显示对应购物车（实现稍复杂）。

### Q4. 门店订货时，若门店还没有该 `canonical_id` 对应的本地品项怎么办？

- **倾向方案 A**：提交时校验，要求门店必须先通过「品项主数据」流程建立绑定或本地品项；否则提示并阻止提交。
  - 理由：不跨界操作 canonical/item 创建逻辑，保持功能边界清晰。
- **备选方案 B**：提交时自动在门店仓创建对应品项（quantity=0），基于 canonical 数据。
  - 理由：体验更顺，门店无需先建品项再订货。
  - 代价：需要调用 canonical 创建逻辑，可能触发 Q1=deny 等策略冲突。

### Q5. 门店收货是否在 P0 自动增加门店库存？

- **倾向**：P0 不自动入库，仅做状态变更为 `delivered`。
- 理由：避免与现有 `restock` / `stocktake` 库存逻辑纠缠；门店收货流程可作为 P1 独立扩展。

### Q6. 是否允许部分出库？

- **倾向**：P0 不允许，一个订单一次性全部出库；P1 支持部分出库与多次配送记录。
- 理由：先跑通最小闭环，再扩展复杂场景。

### Q7. 是否所有配送中心的品项都默认可订，还是需要单独标记「可订」？

- **倾向**：P0 默认所有有 `canonical_id` 且状态为 `active` 的品项都可订。
- 备选：新增 `dc_item_availability` 表或 `items` 字段标记是否对门店可订；适合未来精细化管理。

---

## 10. 验收口径

1. 门店用户能从商品目录加购、填写日期/备注并提交订单；提交时库存不足能被阻止并提示具体品项。
2. 配送中心 `manager`/`admin` 能看到待审批订单并完成通过/拒绝；拒绝必须填原因。
3. 审批通过后，配送中心 `staff` 及以上角色能执行出库；出库后配送中心 `items.quantity` 正确扣减，并生成 `stock_movements` 记录。
4. 订单在提交、审批、出库、送达四个节点均触发通知，目标用户正确收到。
5. 门店侧能查看本店历史订单及详情；配送中心侧能查看发到本中心的历史订单；平台管理员能查看全部订单。
6. 订单状态流转完整：`pending → approved → shipped → delivered`，拒绝/取消状态也能正确记录。
7. 角色权限矩阵生效：门店不能审批或出库，配送中心不能代表门店提交订单。

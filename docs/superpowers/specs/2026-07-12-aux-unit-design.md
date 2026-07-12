# 多辅单位出入库录入 — 设计文档

**日期**：2026-07-12
**分支**：待建（建议 `aux-unit`）
**目标**：在出库、入库、盘点录入页面，允许品项除了基础单位（箱/包等）外再配置 1 个辅单位（个/条/袋/瓶/克），用户录入时可在两种单位间一键切换、自动换算，避免心算出错。系统记账始终使用基础单位，辅单位仅用于录入与显示。

**约束**：

- 每品项最多 1 个辅单位（基础 + 辅 = 2 个 segment tab）
- 辅单位从 5 个固定预设中选：克 / 个 / 条 / 袋 / 瓶
- 生产录入模块的克换算逻辑保持不动（沿用 `grams_to_stock`）
- 出入库、回退、删除、报表、MCP 服务器后台数据全部保持基础单位记账

---

## 背景与现状

当前系统已有一个克专用换算字段 `items.gram_per_unit`（仅用于生产录入，2026-06-22 引入）。出入库 / 盘点 session 模板（`restock_session.html` / `outbound_session.html` / `stocktake_session.html`）只有一个 number 输入框，标签固定显示品项的 `items.unit` 基础单位。

**问题**：实际操作中，箱/包单位不便于精准出库（例如黑椒汁按箱出库给一个店可能太多，但工人想按「个」出 4 个时，必须心算「4 个 = 4/12 箱 = 0.33 箱」），易错且慢。克只覆盖生产配方场景，出入库/盘点仍无法享受。

---

## 核心架构判断

**辅单位是「录入和显示」单位；基础单位仍是「库存记账」单位。**

换算只发生在 submit 边界：
1. 用户在 segment 切到辅单位 → 输入数字 → hidden `_unit` 字段记 `aux`
2. submit 时后端若识别 `_unit == 'aux'` → 调 `aux_to_base()` 转成基础单位 → 写入 `requested_quantity` / `actual_quantity`
3. 库存流水、回退、删除、报表、MCP 全部零改动（看到的是基础单位）

这与 2026-06-22 的克换算架构一致，只是扩展到出入库/盘点 + 多单位类型。

---

## 设计

### 1. 数据模型与迁移

**`items` 表新增两列**（不删 `gram_per_unit`，保留作为回滚安全垫）：

```sql
ALTER TABLE items ADD COLUMN aux_unit TEXT;                  -- NULL = 无辅单位
ALTER TABLE items ADD COLUMN aux_rate REAL NOT NULL DEFAULT 0; -- 1 基础单位 = N 辅单位
```

- `aux_unit` 取值：`NULL` / `'克'` / `'个'` / `'条'` / `'袋'` / `'瓶'`
- `aux_rate = 0` → 该品项未启用辅单位
- `aux_rate > 0` → 1 基础单位 = `aux_rate` 个辅单位（如黑椒汁 `aux_unit='个'`, `aux_rate=12` 表示 1 箱 = 12 个；牛乳 `aux_unit='克'`, `aux_rate=1000` 表示 1 箱 = 1000 克）

**迁移（幂等，加入 `migrate_warehouse_db_columns()`）**：

1. `PRAGMA table_info(items)` 检查 `aux_unit` 列不存在则 `ALTER TABLE items ADD COLUMN aux_unit TEXT`
2. 检查 `aux_rate` 不存在则 `ALTER TABLE items ADD COLUMN aux_rate REAL NOT NULL DEFAULT 0`
3. 对于 `aux_rate = 0 AND gram_per_unit > 0` 的行（旧启用克的品项仅迁移一次）：
   ```sql
   UPDATE items SET aux_rate = gram_per_unit, aux_unit = '克'
   WHERE aux_rate = 0 AND gram_per_unit > 0 AND aux_unit IS NULL
   ```
4. 每个 warehouse db 在应用启动时 `init_warehouse_db()` 触发，幂等执行

**回滚安全**：保留 `gram_per_unit` 列，旧版本代码仍能读它（即便是 NULL/0 也不会崩）；`aux_unit` 列对旧代码无害（不读也不写）。

### 2. 换算纯函数（`blueprints/_helpers.py`）

新增通用换算函数，签名与原 `grams_to_stock` 对齐：

```python
def aux_to_base(aux_qty: float, aux_rate: float) -> float:
    """辅单位 → 基础单位。aux_rate<=0 表示无辅单位，原样返回（防御）。"""
    if aux_rate <= 0:
        return float(Decimal(str(aux_qty)).quantize(Decimal('0.01')))
    return float(
        (Decimal(str(aux_qty)) / Decimal(str(aux_rate)))
        .quantize(Decimal('0.01'))
    )

def base_to_aux(base_qty: float, aux_rate: float) -> float:
    """基础单位 → 辅单位（仅用于显示换算预览）。aux_rate<=0 返回原值。"""
    if aux_rate <= 0:
        return float(Decimal(str(base_qty)).quantize(Decimal('0.01')))
    return float(
        (Decimal(str(base_qty)) * Decimal(str(aux_rate)))
        .quantize(Decimal('0.01'))
    )
```

**`grams_to_stock` 保留为别名**（生产模块无改动）：

```python
grams_to_stock = aux_to_base  # 向后兼容
```

### 3. 出入库/盘点 session 模板改造

`templates/restock_session.html` / `outbound_session.html` / `stocktake_session.html` 中 `.er-input` 区块替换：

```html
<div class="er-input">
  {% if i.aux_unit and i.aux_rate > 0 %}
  <div class="seg" data-rate="{{ i.aux_rate }}">
    <button type="button" class="active" data-u="base">{{ i.unit }}</button>
    <button type="button" data-u="aux">{{ i.aux_unit }}</button>
  </div>
  {% endif %}
  <input
    type="number" step="0.01" min="0" inputmode="decimal"
    name="restock_{{ i.id }}" data-base-unit="{{ i.unit }}" data-aux-unit="{{ i.aux_unit or '' }}"
    placeholder="0" autocomplete="off" />
  <input type="hidden" name="restock_{{ i.id }}_unit" value="base" />
  <span class="er-unit">{{ i.unit }}</span>
  <span class="calc-hint" hidden></span>
</div>
```

**JS 行为**：
- 点击 segment tab → 切 `active` class；同步更新 hidden `*_unit` 值；更新 `.er-unit` 文案；切到辅时 input placeholder 变为 `0 {{ aux_unit }}`
- input 输入时，若 `_unit == 'aux'`：实时显示 `X {{ aux_unit }} ≈ Y {{ base_unit }}`（用 JS 内 `aux_rate` 直接乘除 → 不依赖 Decimal，仅展示用，精度 2 位小数四舍五入即可）
- 只有 1 个 tab（无辅单位）时不渲染 segment，行为同旧

**共需 3 套近乎重复的 JS**：三个 session 模板各持一份（与现状的品类筛选 JS 风格一致，不抽公共文件，YAGNI）。

### 4. 后端 submit 处理

`restock_submit` / `outbound_submit` / `stocktake_submit` 三处统一改造：

**session 视图查询多带两列**：

```python
db.execute("""SELECT i.id, i.name, i.quantity, i.unit, i.safety_stock,
                      i.aux_unit, i.aux_rate, c.name AS category_name
               FROM items i JOIN categories c ON c.id = i.category_id
               ORDER BY c.name, i.name""")
```

**submit 循环里读单位字段**：

```python
items_data = db.execute("SELECT id, quantity, aux_unit, aux_rate FROM items").fetchall()
for item in items_data:
    raw = request.form.get(f"restock_{item['id']}", "").strip()
    if raw == "":
        continue
    qty_raw = parse_qty(raw)
    if qty_raw <= 0:
        continue
    unit_choice = request.form.get(f"restock_{item['id']}_unit", "base")
    if unit_choice == "aux":
        aux_rate = float(item["aux_rate"] or 0)
        if aux_rate <= 0:
            flash(f"{item['name']} 未启用辅单位，请用基础单位录入")
            return redirect(url_for("restock.restock_session"))
        qty = aux_to_base(qty_raw, aux_rate)
    else:
        qty = qty_raw
    rows.append((int(item["id"]), qty))
```

写入 `restock_requests.requested_quantity` / `outbound_requests.requested_quantity` / `stocktakes.actual_quantity` 的**永远是基础单位**，回退 / 删除 / 报表 / MCP 零改动。

出库 `outbound_submit` 的库存不足校验保持基础单位比较（`qty > item["quantity"]`），不变。

盘点 `stocktake_submit` 同理：`actual_quantity` 亦是基础单位。

### 5. 品项编辑页

`templates/items.html`（新建表单）和 `templates/edit_item.html` 中替换「每单位克重」字段为：

```html
<label>
  <span>辅单位</span>
  <select name="aux_unit">
    <option value="">无</option>
    <option value="克">克</option>
    <option value="个">个</option>
    <option value="条">条</option>
    <option value="袋">袋</option>
    <option value="瓶">瓶</option>
  </select>
</label>
<label>
  <span>1 基础单位 = N 辅单位</span>
  <input name="aux_rate" type="number" min="0" step="0.01" placeholder="0" />
</label>
```

`blueprints/items.py`：

- create / edit 读 `aux_unit`（空字符串视为 NULL）和 `aux_rate`，`aux_rate < 0` flash 报错
- **兼容生产代码的同步写入规则**：
  - 若 `aux_unit == '克'` → 同步写 `gram_per_unit = aux_rate`（生产配方读克走原路径）
  - 若 `aux_unit` 不为 `'克'` 但该品项已被 `product_bom` 引用且原 `gram_per_unit > 0` → **flash 报错并拒绝**（"该品项已被生产配方引用且启用克，不能切换为其他辅单位，否则现有配方语义会被破坏"）。这避免「悄悄把 1000 克/件的配方变成 1000 个/件」的灾难
  - 若 `aux_unit` 不为 `'克'` 且无生产配方引用 → 写 `gram_per_unit = 0`（该品项对生产配方未启用克，生产代码按库存单位处理）
- 品项列表的「克重」列改为「辅单位」列：显示 `12 个/箱` / `1000 克/箱` / `—`（无辅）

### 6. 测试方案

**遵循 TDD**：先写测试再改实现。

新增 `tests/test_aux_unit.py`：

#### 纯函数单测
- `aux_to_base(1440, 1000)` → 1.44
- `aux_to_base(6, 0)` → 6（防御性原样返回）
- `aux_to_base(2880, 2000)` → 1.44
- `base_to_aux(1.44, 1000)` → 1440
- `base_to_aux(2, 0)` → 2

#### 出库 submit 用辅单位
- seed item: `aux_unit='个'`, `aux_rate=12`, `quantity=10`（箱）
- POST `/outbound/submit`：`outbound_{id}=24`, `outbound_{id}_unit=aux`
- 断言：`outbound_requests.requested_quantity == 2`（箱）；`items.quantity == 8`；`stock_movements` 记 `-2`

#### 入库 submit 用辅单位
- 同上 seed；POST `/restock/submit`：`restock_{id}=24`, `restock_{id}_unit=aux`
- 断言：`restock_requests.requested_quantity == 2`（箱）；`items.quantity == 12`；`stock_movements` 记 `+2`

#### 盘点 submit 用辅单位
- seed `quantity=10`；POST `/stocktake/submit`：`actual_{id}=18`, `actual_{id}_unit=aux`
- 断言：`stocktakes.actual_quantity == 1.5`（箱）；`diff == -8.5`

#### 迁移幂等
- 创建一个只有 `gram_per_unit`（无 `aux_*` 列）的旧 warehouse db；设某些 item `gram_per_unit=1000`
- 调 `init_warehouse_db()`
- 断言：`aux_unit='克'`, `aux_rate=1000`；原 `gram_per_unit` 列仍存在
- 再次调 `init_warehouse_db()`：`aux_rate` 不应被覆盖（幂等）

#### 品项编辑保存
- POST `/items` 带 `aux_unit='条'`, `aux_rate=10` → 断言落库；`gram_per_unit` 同步为 0
- POST `/items` 不带 `aux_unit`（空）→ `aux_unit=NULL`, `aux_rate=0`
- POST `/items` 带 `aux_unit='克'`, `aux_rate=1000` → `gram_per_unit` 也被同步为 1000
- **生产配方锁定**：seed item `gram_per_unit=1000, aux_unit='克'` + `product_bom` 引用该 item → POST edit `aux_unit='个'` → 应被 flash 报错拒绝，`aux_unit`, `gram_per_unit` 仍为原值
- 同上情景但无 `product_bom` 引用 → 允许切换，`gram_per_unit` 落为 0

### 7. 影响面汇总

| 文件 | 改动 |
|---|---|
| `db/__init__.py` | `WAREHOUSE_SCHEMA` 加 `aux_unit`, `aux_rate` 列；`migrate_warehouse_db_columns()` 加迁移 |
| `blueprints/_helpers.py` | 新增 `aux_to_base()`, `base_to_aux()`；`grams_to_stock = aux_to_base` 别名 |
| `blueprints/items.py` | create / edit 读写 `aux_unit`, `aux_rate`；列表页改「辅单位」列；`gram_per_unit` 兼容同步写入；被 product_bom 引用且原启用克时禁止切走 克 |
| `blueprints/restock.py` | session 查询带 `aux_unit, aux_rate`；submit 读 `_unit` 字段换算 |
| `blueprints/outbound.py` | 同 restock |
| `blueprints/stocktake.py` | 同 restock（含 `_unit` 字段换算，`actual_quantity` 入库仍是基础单位） |
| `templates/items.html` | 替换克重字段为「辅单位 select + 换算率 input」；列表页列头与渲染 |
| `templates/edit_item.html` | 同上 |
| `templates/restock_session.html` | `.er-input` 区块替换为 segment + `_unit` hidden + JS |
| `templates/outbound_session.html` | 同上 |
| `templates/stocktake_session.html` | 同上 |
| `static/style.css` | 新增 `.seg`, `.seg button`, `.seg button.active`, `.calc-hint` 样式 |
| `tests/test_aux_unit.py` | 新增（含纯函数、submit 集成、迁移、品项编辑） |

**零改动**（靠测试验证）：生产录入模块、MCP 服务器、报表、消耗、预测、采购、出库回退删除。

---

## 已确认的设计决策

1. 每品项最多 1 个辅单位，基础 + 辅 = 共 2 个 segment tab ✓
2. 辅单位从 5 个固定预设中选：克 / 个 / 条 / 袋 / 瓶 ✓
3. 适用范围：出库 + 入库 + 盘点；生产保持原克逻辑不动 ✓
4. 数据方案 A：新增 `aux_unit` + `aux_rate` 列，保留旧 `gram_per_unit` 作回滚安全垫 ✓
5. 存储语义：1 基础单位 = `aux_rate` 辅单位（沿用 `gram_per_unit` 语义） ✓
6. submit 写库永远是基础单位，回退/删除/报表/MCP 零改动 ✓
7. UI 方案 A：行内 segment 切换（每行 2 个 tab + 输入 + 实时换算提示） ✓

---

## 风险与缓解

- **回滚风险**：若新版本上线后需回滚到旧代码，新 schema 中的 `aux_unit`, `aux_rate` 列对旧代码无害（不读不写）；`gram_per_unit` 列仍存在，旧克逻辑继续工作。但大家在旧 UI 编辑后再上线新版本，新版本看到的旧 `gram_per_unit` 有值但 `aux_*` 已迁移过 → 迁移幂等条件 `aux_rate = 0 AND gram_per_unit > 0` 不会误覆盖。
- **JS 显示精度**：浏览器 JS 用浮点乘除可能出现 `0.30000000000000004`，需在 JS 里手动 `Math.round(x * 100) / 100`。仅影响显示，不影响后端（后端用 Decimal）。
- **盘点 xlsx 导入**：现有 `parse_stocktake_xlsx` 不传单位字段，导入时按基础单位（旧路径不变）；若用户切到 segment 后再导入 csv 触发的 commit，仍以基础单位为准。

- **品项在生产配方中使用克 + 用户尝试切到其他辅单位**：items.py edit 路径会硬性拒绝（检测到 `product_bom` 引用且原 `gram_per_unit > 0`），避免配方语义被悄悄破坏。用户需先清空相关配方才能切单位。
- **品项已被出库/入库/盘点记录引用后切换辅单位**：出入库/盘点记录存的是基础单位（不是辅），切换辅单位不影响历史记录的语义。无需校验历史引用。

- 生产 BOM 配方是否也加辅单位支持：暂不做（生产已有克逻辑，YAGNI；以后如有需要只需让 production session 复用 `seg + _unit` 模式）
- 品项列表筛选「未启用辅单位」的工具：暂不做
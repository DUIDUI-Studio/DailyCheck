# 品项主数据统一 — 评估与改造方案

> 2026-10-09 · 分支 `feat/item-master-unify` · 状态：owner 已拍板 4 项决策（见 §5），待排期实施

## 0. 结论先行

项目**已经走了一半**：master.db 的 `canonical_items`（/canonical/list「品项主数据」页）+ 各仓 `items.canonical_id` 绑定机制已建成，Q1=deny 已拦住仓内新增品项。

没做完的是**收口**：旧维护入口还开着（6 个仓内写路径）、三条下发通道并存（身份键各不相同）、存量数据已漂移。本方案核心 = **关旁路、留一条通道、清洗存量**，而不是从零建主数据体系。

**2026-10-09 owner 拍板（4 项）**：价格（selling_price/unit_cost）**推翻 Q6、收归主数据统一维护并扇出下发**；/items/publish 直接下线；safety_stock 归本仓；wh_001 本轮排除。

---

## 1. 现状诊断

### 1.1 数据架构（三份"主数据"并存）

| 层     | 位置                          | 行数         | 现状                                                            |
| ----- | --------------------------- | ---------- | ------------------------------------------------------------- |
| 平台主数据 | master.db `canonical_items` | 125 active | 唯一真源（/canonical 维护）                                           |
| 研发副本  | rd_001 `items`              | 125        | 2026-10-04 扇出的镜像，100% 绑定 canonical_id，sku 全 `AUTO-IC-*`，价格全 0 |
| 门店业务行 | wh_002/003/000 等 `items`    | —          | 主数据字段 + 库存 + 本仓价格混在一行，多处漂移                                    |

### 1.2 问题清单（按严重度）

**P0 — 写路径没关死，主数据可被旁路篡改：**

| # | 旁路                           | 位置                                                               | 问题                                                                            |
| - | ---------------------------- | ---------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| 1 | `/items/<id>/edit`           | `blueprints/items.py:170-235`                                    | 平台管理员在门店/rd 上下文可改已绑定行的 name/unit/gram 等主数据字段                                  |
| 2 | `/items/<id>/delete`         | `blueprints/items.py:238-259`                                    | 已绑定 canonical 的行可被物理删除，绕过 Q3 deactivate_only                                  |
| 3 | `/admin/import-items/commit` | `blueprints/import_items.py:158-264`                             | 门店仓 admin 可用，**DELETE+INSERT 破坏性替换**，不走任何策略检查                                 |
| 4 | 配方发布隐带 upsert                | `blueprints/recipe_cost.py:1012-1027`                            | 发布配方时按 **sku** 在门店 upsert BOM 品项，直接写 name/unit/**unit_cost**（与主数据"不下发价格"策略相反） |
| 5 | `/items/publish` 旧通道         | `blueprints/items.py:323-385` + `publish_recipe_pure.py:347-571` | 与 canonical 扇出并行的第二条下发通道，按 **sku** 匹配                                         |
| 6 | `/canonical/edit` 权限过宽       | `blueprints/canonical.py:113-160`                                | 仅 `@require_role("manager")` —— **门店 manager 也能改平台主数据**                       |

**P1 — 通道不统一，身份键分裂：**

三条下发通道各用各的匹配键：canonical 扇出按 `canonical_id`（`canonical_pure.py:1609-1618`）、items/publish 和配方发布按 `sku`（`publish_recipe_pure.py:392-401`）、bulk-publish 反向按 `(name, unit)`。现存 **5 种 SKU 体系**（WP* / AUTO-时间戳 / AUTO-IC-* / AUTO-RECEIVE-* / 历史自建）。

**P1 — 存量数据漂移（实测）：**

- canonical#5「冷冻芒果酱」：wh_003/wh_000 的绑定行 name=**奶油芒果酱**，三仓售价 393/360/0 各不同；
- 同物双主数据：草莓风味酱在 wh_002 绑 canonical#60、在 wh_003/wh_000 绑 canonical#59；
- wh_001 33 行 items 全部 canonical_id=NULL、12 个品类 canonical_code 全空（设计文档本轮明确排除 wh_001）；
- is_store_exclusive 口径混杂：wh_002 70/69、wh_000 89/89 几乎全为 1。

**P2 — 结构性：** 主数据字段（品项是什么）与本仓业务字段（库存/价格/安全库存）混在同一张 items 表，靠字段级策略（CANONICAL_FIELD_POLICY / NEVER_TOUCH_COLUMNS）维持纪律。

### 1.3 已有的正确基础（不要重建）

- `canonical_items` / `canonical_categories` / 绑定 / 冲突 / 扇出事件表全套
- Q1=deny 新增拦截已在 `/items` POST 生效（`items.py:91-124`）
- Q4=freeze 字段冻结 + 冲突表、Q3=deactivate_only（主数据禁物理删除）
- 门店订货全链路已以 canonical_id 为键运行

---

## 2. 目标架构

```
canonical_items (master.db)      ← 唯一维护入口 /canonical/*（platform admin）
    │  维护范围：名称/品类/单位/克重/辅单位 + selling_price + unit_cost（2026-10-09 拍板收归）
    │  唯一下发通道：canonical 扇出（/canonical/fanout + CLI align-apply）
    ▼
各仓 items = canonical 绑定键 + 本仓业务字段
    ├─ 门店仓：库存 / 安全库存（系统按消耗自动算，见 P0-12）/ is_orderable / is_store_exclusive / is_active
    └─ 研发仓：库存消耗（unit_cost 由主数据下发，配方成本口径统一）
```

原则：**"品项是什么 + 定价成本"只在主数据改；"本仓怎么用"（库存/安全库存/可订开关/单仓启停）由系统或本仓管理，与主数据下发通道隔离。**

---

## 3. 分期改造方案

### P0 — 写路径收口（核心改造，先做）

| #  | 改造点               | 位置                                                                                                      | 改法                                                                                                                                                                                                                                                                                                                                                                                                                         | 复杂度 |
| -- | ----------------- | ------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --- |
| 1  | 编辑页拆字段            | `items.py:170-235` + `templates/edit_item.html`                                                         | 已绑定行：name/unit/gram_per_unit/aux_unit/aux_rate/category/**selling_price/unit_cost/safety_stock** 全锁死（只读 + "去主数据修改"链接；safety_stock 显示公式值，由 P0-12 重算写入）；本仓仅留 is_store_exclusive                                                                                                                                                                                                                                              | 中   |
| 2  | 删除拦截              | `items.py:238-259`                                                                                      | 已绑定 canonical_id 的行拒绝删除，提示走主数据 deactivate                                                                                                                                                                                                                                                                                                                                                                                  | 低   |
| 3  | 批量导入收口            | `import_items.py:158-264`                                                                               | 废除 DELETE+INSERT；新 sku 必须先在主数据建档；不再允许导入写 safety_stock（该字段由 P0-12 系统计算）                                                                                                                                                                                                                                                                                                                                                     | 中   |
| 4  | 配方发布去 upsert      | `recipe_cost.py:1012-1027`                                                                              | 发布前校验 BOM 品项全部已纳管，未纳管拒绝发布并给出纳管指引；不再直写门店任何主数据字段（含 unit_cost）                                                                                                                                                                                                                                                                                                                                                                | 中   |
| 5  | 下线 /items/publish | `items.py:323-385`                                                                                      | **已拍板：直接下线**。route 重定向到 /canonical/fanout，历史页保留只读                                                                                                                                                                                                                                                                                                                                                                          | 低   |
| 6  | 主数据维护权限收紧         | `canonical.py:113-160`                                                                                  | `/canonical/edit`、claim-requests review 由 manager 收紧到 platform admin（is_admin）                                                                                                                                                                                                                                                                                                                                             | 低   |
| 7  | 扇出范围补 rd          | `canonical_pure.py:2451-2453`                                                                           | ALIGN_SCOPE 加入 rd_001（研发副本跟随主数据更新）；扇出 UI 目标仓列表（`canonical.py:479-482`）同步加 rd                                                                                                                                                                                                                                                                                                                                               | 低   |
| 8  | 收货建行修正            | `store_ordering_pure.py:1736-1743`                                                                      | 保留自动建行（数据取自 canonical，方向正确），unit 硬编码 `'件'` 改用 canonical.unit                                                                                                                                                                                                                                                                                                                                                               | 低   |
| 9  | **价格收归主数据**       | `config.py:84-109` CANONICAL_POLICY + `canonical_pure.py:164-193`                                       | ① Q6 由 `storefront_autonomous` 改为 `canonical_managed`；② `NEVER_TOUCH_COLUMNS` 移除 selling_price/unit_cost 两列，价格进入 `CANONICAL_FIELD_POLICY` 可下发集；③ `/canonical/edit`（`canonical.py:120-129`）与 bulk-import 表单启用 canonical_items.unit_cost/selling_price 编辑（列已存在，Q6 当时预留）；④ 门店/rd 侧价格字段随 P0-1 锁死；⑤ Q4=freeze 冲突机制对价格字段同样生效；⑥ 启用预留的 `price_follow_canonical` 跟随机制（`canonical_pure.py:1004-1007`，当前恒 0）                          | 中高  |
| 10 | **单仓品项启停开关**      | `db/__init__.py` migrate + `canonical.py` / `items.py` + `templates/canonical/detail.html`、`items.html` | ① 门店 items 幂等加列 `is_active INTEGER DEFAULT 1`；② 管理员入口两处：主数据详情页跨仓绑定列表按仓「停用/启用」、门店品项页已绑定行加「停用」按钮（均 platform admin）；③ 停用行在出库/入库/生产/订货等选择点排除或置灰，历史记录与报表保留，库存不丢（Q7 精神）；④ 停用时库存 >0 仅软提示不硬拦；⑤ `is_active` 属本仓字段，扇出 NEVER_TOUCH                                                                                                                                                                                                    | 中   |
| 11 | **主数据批量统一修改**     | `canonical.py` + `templates/canonical/list.html`                                                        | `/canonical/list` 加勾选多行 →「批量统一修改」：对选中品项统一设置某字段值（品类/单位/克重/辅单位/成本/售价）→ 保存 → 一键扇出到受影响仓。逐项操作与批量操作写同一 pure 层函数，避免两套写入路径                                                                                                                                                                                                                                                                                                         | 中   |
| 12 | **安全库存自动计算**      | pure 层新增 `recompute_safety_stocks()` + `items.py` 按钮 + CLI                                              | **规则（Eric 2026-10-09 拍板）：`safety_stock = Σ近7天出库量 × 1.2`；该品项出库历史覆盖不满 7 天窗口 → 写 0。** ① 消耗口径与采购建议/预测一致（`outbound_requests` 非 rolled_back 合计，参照 `procurement.py:141-152` 的 `_outbound_30d_sum` 改 7 天窗口）；② 覆盖判定：该行最早非 rollback 出库时间早于 `now-7d` 才算"有 7 天数据"，否则 0；③ 落库到各仓 items.safety_stock，按钮「重算安全库存」（platform admin，放各仓「品类与品项」页头）+ CLI 命令（后续可挂每日定时）；④ 系数 1.2 / 窗口 7 天进 config 常量便于调参；⑤ 扇出 NEVER_TOUCH safety_stock；⑥ 该字段对各角色只读 | 中   |

### P1 — 存量清洗与同步补齐

1. **名称漂移修复**：以 canonical 为准对 wh_002/003/000 重新扇出；Q4=freeze 会产生冲突记录，走冲突裁决页批量"按主数据覆盖"。
2. **草莓风味酱双主数据合并**：canonical#59/#60 合并为一条，重绑门店行，停用冗余条目（deactivate，不物理删）。
3. **is_store_exclusive 口径清洗**：把"已认领绑定行"批量归零该标志，只保留真正门店自建且未纳管的行。
4. **wh_001 决策项**（见 §5-D）：若纳入，先做 33 行的 claim 纳管 + 品类映射回填（`backfill_category_mappings`）。

### P2 — 远期可选（暂不排期）

门店 items 的主数据字段改为纯引用（读时 JOIN master，本地不落列）。彻底消除副本漂移，但动几十处 JOIN + MCP/CLI 全链路，收益/成本比低——**建议 P0+P1 落地后观察漂移是否复现，再决定是否做**。

---

## 4. 风险与影响面

| 风险                | 说明                                   | 缓解                                                                                                      |
| ----------------- | ------------------------------------ | ------------------------------------------------------------------------------------------------------- |
| **价格切换冲击**（P0-9）  | 门店现有售价与主数据（当前全 0 或空）不一致，直接扇出会把门店价格清零 | **先做价格回填**：以各仓现行价格为基准裁决出统一价写入 canonical_items（`/canonical/bulk-import` 或脚本），再开 Q6=canonical_managed 并扇出 |
| 配方发布行为变化          | P0-4 后，BOM 品项未纳管的配方将无法发布             | 先跑预检脚本列出受影响配方，主数据补齐后再切拦截                                                                                |
| 门店编辑页 UX          | 主数据字段+价格变只读，管理员可能困惑                  | 只读字段旁给"去主数据修改"跳转                                                                                        |
| freeze 冲突爆量       | P1 清洗时 wh_002/003 大量行会进冲突队列（含价格）     | 冲突裁决页支持批量按 canonical 覆盖                                                                                 |
| /items/publish 下线 | 现有操作习惯改变                             | redirect + 提示，历史可查                                                                                      |
| 研发成本口径            | 价格收归主数据后 rd 的 unit_cost 也是下发值        | 主数据 unit_cost 由研发/采购侧在 /canonical 维护（platform admin），配方成本口径随之统一                                         |

**验收标准（P0 完成定义）：**

1. 门店/rd 上下文的所有代码路径均无法修改已绑定行的主数据字段；
2. 主数据唯一写入口 = /canonical/*（platform admin）；
3. 唯一下发通道 = canonical 扇出；
4. `python3.12 -m pytest tests/` 全过 + 新增策略回归测试（旁路逐条覆盖）。

---

## 5. 决策记录（2026-10-09 owner 拍板）

- **A. 价格归属：收归主数据**（推翻 Q6 storefront_autonomous）。selling_price/unit_cost 在 /canonical 维护、扇出下发；门店/rd 不再改价格。前提：先做存量价格回填再切换（见 §4 风险第一行）。
- **B. /items/publish：直接下线**，统一走 canonical 扇出。
- **C. safety_stock：归本仓**，主数据不下发。
- **D. wh_001：本轮排除**，维持设计文档原范围（wh_000/002/003/004/006 + rd_001），后续单独处理。

### 实施顺序建议

1. P0-9 价格回填（存量价 → canonical_items）
2. P0-1~8 写路径收口（与价格开关同批切换，避免中间态双通道写价格）
3. P1 存量清洗（名称漂移、双主数据合并、is_store_exclusive 归零）
4. 全量回归：`python3.12 -m pytest tests/` + 新增策略测试 + 关键页面冒烟

---

## 6. 用户操作方式（改造后，按角色）

### 6.1 平台管理员 — 唯一主数据维护者

**新增品项（两条路）：**

| 场景     | 点击步骤                                                                                                             |
| ------ | ---------------------------------------------------------------------------------------------------------------- |
| 主动建    | ① 侧边栏「品项主数据」→「新建」：填名称/品类/单位/克重/辅单位/成本/售价 → 保存 ② 「扇出」页勾选目标门店 → 下发 ③ 切到门店仓「品类与品项」页确认新行已出现（sku 形如 `AUTO-IC-*`，自动绑定） |
| 响应门店申请 | ① 主数据页「新增申请」列表点开门店提交的申请 ② 审批通过即自动建档 ③ 走上面第 ② 步扇出                                                                 |

**修改字段或价格：**

1. 「品项主数据」列表 → 点品项 → 编辑（名称/单位/克重/成本/售价）→ 保存
2. 「扇出」下发到受影响仓
3. 若目标仓有本地改动 → 「冲突」页出现记录 → 批量"按主数据覆盖"

**停用品项（两种粒度，注意区分）：**

| 粒度                   | 操作                             | 影响范围                                                    |
| -------------------- | ------------------------------ | ------------------------------------------------------- |
| **全公司停用**            | 品项详情页 →「停用」                    | 所有仓随扇出标 `canonical_status=inactive`，历史业务数据全保留，无物理删除（Q3） |
| **单仓停用**（P0-10 新增能力） | 品项详情页 → 跨仓绑定列表 → 找到目标门店行 →「停用」 | 仅该门店：出库/入库/生产/订货选择中不再出现，历史记录与报表保留，库存不动；恢复同位置「启用」        |

单仓停用时若该门店仍有库存余量，页面软提示建议先出清或盘点归零，不硬拦。

**价格回填（一次性操作，切 Q6 开关前）：** 「批量导入主数据」页上传 CSV（列：canonical_sku, unit_cost, selling_price）灌入各仓现行价。

### 6.2 门店（manager/admin）— 主数据只读 + 本仓参数

| 操作                  | 改造前         | 改造后                    |
| ------------------- | ----------- | ---------------------- |
| 新增品项                | 平台管理员在门店仓可建 | ✗ 拦截页引导「从主数据选」或「提新增申请」 |
| 改名称/单位/克重/品类        | 可           | ✗ 只读，字段旁「去主数据修改」跳转     |
| 改售价/成本              | 可           | ✗ 只读（已收归主数据）           |
| 删除品项                | 可           | ✗ 已绑定行拒绝，提示走主数据停用      |
| 改安全库存               | 可           | ✓ 保留（本仓参数）             |
| 库存操作（盘点/出入库/生产/收发货） | —           | ✓ 完全不变                 |

**想要主数据里没有的新品项：**

1. 「品类与品项」页 → 点新增 → 拦截页选「提交新增申请」
2. 填名称/建议单位 → 提交
3. 平台管理员审批建主数据并扇出后，本仓行自动出现

**批量导入：** `/admin/import-items` 不再整表替换品项，只能更新安全库存等本仓字段；新 sku 必须先在主数据建档。

### 6.3 研发中心（rd）— 纯消费副本

- 品项由主数据扇出自动维护，研发侧不再手工增改（名称/单位/价格全部只读）
- 配方成本取主数据下发的 unit_cost，与门店口径一致
- 配方发布前系统自动校验 BOM 品项已全部纳管；未纳管则拒绝并给出待纳管清单

### 6.4 全流程一图

```
门店提申请 ──▶ 平台管理员审批 ──▶ 建主数据(/canonical) ──▶ 扇出(fanout)
                                                        ├──▶ 门店行自动出现(AUTO-IC-*)
                                                        └──▶ rd 镜像同步
日常字段/价格变更：/canonical 编辑 ──▶ 扇出 ──▶ 冲突页裁决 ──▶ 全仓一致
门店自留权限：安全库存、库存操作、is_store_exclusive、DC 可订开关
```

---

## 7. 测试流程

### 7.1 基线先行（动手改造前必做）

```bash
~/.local/bin/python3.12 -m pytest tests/ -q 2>&1 | grep '^FAILED' | sort > /tmp/before_fails   # 当前基线 577 passed / 18 failed（pre-existing）
ruff check . --output-format=concise | sed 's/:[0-9]*:[0-9]*:/:/' | sort > /tmp/ruff_before
```

### 7.2 随写随验（每个收口点至少一条策略测试）

新增 `tests/test_master_data_unify.py`，覆盖 9 个收口点：

| #  | 测试用例                                                                        |
| -- | --------------------------------------------------------------------------- |
| 1  | 门店上下文 POST `/items/<id>/edit` 改 name/selling_price → 拒绝；改 safety_stock → 放行 |
| 2  | 删除已绑定 canonical_id 的行 → 拒绝并提示 deactivate；未绑定行 → 放行                          |
| 3  | import-items commit 新 sku → 拒绝；仅更新本仓字段 → 成功                                 |
| 4  | 配方发布含未纳管 BOM 品项 → 拒绝 + 待纳管清单                                                |
| 5  | POST `/items/publish` → 302 重定向 /canonical/fanout                           |
| 6  | 门店 manager 访问 `/canonical/edit`（GET/POST）→ 403；platform admin → 放行          |
| 7  | 扇出目标含 rd_001，副本跟随主数据更新                                                      |
| 8  | Q6=canonical_managed 下扇出下发 unit_cost/selling_price；冲突进 freeze 队列            |
| 9  | 收货自动建行 unit 取 canonical.unit（不再硬编码 '件'）                                     |
| 10 | 单仓停用：停用行不出现在出库/订货选择点；扇出不覆盖本仓 `is_active`；历史/库存保留；库存 >0 停用出现软提示              |

### 7.3 全量回归对比（项目既定 SOP）

```bash
pytest 后生成 /tmp/after_fails；ruff 生成 /tmp/ruff_after
comm -13 /tmp/ruff_before /tmp/ruff_after          # 空 = 无新增告警
diff /tmp/before_fails /tmp/after_fails            # 空 = 失败集不变（18 个 pre-existing 除外）
```


```

### 7.4 GUI 冒烟（dev 容器 wdg-systemd :8080，手工点击清单）

1. **admin 登录 → 切仓 wh_002**：「品类与品项」页 → 新增被拦（引导页）、点编辑看价格/名称只读、删除被拒
2. **主数据链路**：`/canonical/edit` 建测试品项 → fanout 到 wh_002 → 切回 wh_002 确认新行出现且价格=主数据值
3. **门店 manager（xsj）登录**：`/canonical/edit` 应 403；门店改安全库存应成功
4. **rd 上下文**：配方发布触发 BOM 纳管校验提示
5. **订货全链**：DC 下单 → 发货 → 门店收货 → 库存/价格显示正确

### 7.5 数据一致性验证（P1 清洗后）

脚本逐仓比对绑定行五字段（name/unit/gram_per_unit/unit_cost/selling_price）与 canonical_items 完全一致，输出差异清单必须为空；价格回填前后值对比留档。

### 7.6 验收 DoD

- [ ] 7.2 九项策略测试全绿
- [ ] 7.3 回归对比两项为空
- [ ] 7.4 冒烟五步全过
- [ ] 7.5 差异清单为空
- [ ] mcp_server 测试容器内通过：`docker exec wdg-systemd /opt/dailycheck/.venv/bin/python -m pytest tests/mcp_server`
```

# 研发中心配方模块升级 — 设计文档

**日期**：2026-10-16
**分支**：`feat/recipe-rd-upgrade`（基于 origin/main b1c18d3）
**状态**：实施中
**需求来源**：Eric 口述（2026-10-16）

---

## 0. 需求原文归纳

1. 所有冰激凌配方在**研发中心**建立，再**推送到各门店**。
2. 配方原料**只能选品项内已有原料**；品项有**采购单价**与**销售单价**两套价格，
   分别计算「基于采购的成本」与「基于销售的成本」及各自毛利率。
   （销售口径 = 把物料卖给供应商/加盟门店的供货结算价。）
3. 配方分两层：**冰激凌配方**（制作一份基料，克数）与**出品配方**
   （售卖分量：小杯/中杯/蛋筒，如小杯 = 120g 基料 + 蛋筒等），
   出品配方要能算出「一份小杯基于采购成本多少钱、基于销售多少钱、毛利率各多少」。
4. 研发中心要有一张**全品牌产品成本总览表**：所有产品的成本、销售价格、毛利一目了然。
5. **存量对齐**：门店已有自己的配方（生产模块 products/product_bom），研发中心配方为空；
   完成后要把门店存量配方同步进研发中心，与门店对齐。

## 1. 现状盘点（2026-10-16，dev + 生产一致）

| 能力 | 现状 |
|------|------|
| ic_recipes / recipes 双层结构 | ✅ 已实现（2026-07-22 spec），RD-only 守卫 |
| 原料只选品项、双价、双口径毛利 | ✅ 已实现（unit_cost / selling_price） |
| 发布到门店（draft→published→superseded） | ✅ 已实现，但**多态 ic_recipe 行被 skip**（缺陷 D2） |
| 全品牌成本总览 | ❌ 缺（只有两个独立列表页） |
| 存量配方同步 | ❌ 缺；门店配方在 products/product_bom（wh_000:15 / wh_002:18 / wh_003:29 / wh_004:17 / wh_006:14 个产品） |
| ic_recipe 行 qty 语义 | ❌ 三处不一致（已知 bug，见 D1） |
| rd_001 / 各门店 ic_recipes、recipes 表 | 全部 0 行 → **qty 语义修复无存量迁移负担** |

product_bom.qty_per_unit 语义 = **克/件**（production.submit 用 grams_to_stock 折算），
与 ic_recipe_items.qty_per_unit（克）同构 → 存量迁移可直接搬数值。

## 2. 缺陷修复

### D1（P0）：出品配方 ic_recipe 行 qty 语义统一为「克」

三处现状互相矛盾：

| 位置 | 现行语义 |
|------|----------|
| `recipe_cost.py::_save_recipe`（output_qty 累计） | qty × ic.output_qty → **份** |
| `recipe_cost_pure.py::recipe_cost`（成本计算） | qty × (cost/output_qty) → **克** |
| `recipe_cost.js::lineIcCost` | grams=qty×output_qty（份）但 cost=qty×per_gram（克）→ **自相矛盾** |

**决策：统一为「克」**。理由：出品配方的心智模型是「小杯 120g = 基料 100g + 酱 20g」，
用户按克配料；成本计算（pure 层，测试覆盖最多）已按克；ic_recipe 引用行与 item 行
单位统一后，output_qty = Σ 所有行克数 的不变量对两种行型都成立。

改动点：
- `_save_recipe`：ic_recipe 行 `total_grams += qty`（删掉 × ic.output_qty）
- `recipe_cost.js::lineIcCost`：`grams = qty`
- `_build_recipe_card_lines`：ic_recipe 行单位「份」→「克」
- `recipe_edit.html`：用量列头/提示文案明确「克」；ic_recipe option 的
  `data-output-qty` 语义不变（每份基料克数，供参考显示）
- 存量数据：两库 recipes 均 0 行，无需迁移

### D2（P0）：发布出品配方时多态 ic_recipe 行被丢弃

`_apply_recipe_snapshot_to_warehouse` 对 `source_type='ic_recipe'` 行直接 `continue`
（代码内 TODO）。后果：门店拿到的出品配方缺基料行，成本全错。

**方案**：
1. `snapshot_recipe` 不变（保持 JSON 稳定）；`_handle_recipe_publish` 发布前对 snapshot
   做**增强**：扫描 lines 中 ic_recipe 行，从源仓（当前 RD 仓）对每个被引用 ic_recipe
   调 `snapshot_recipe('ic_recipe', id)`，挂到 `snapshot["referenced_ic_snapshots"]`，
   并回写 draft 的 snapshot_json（兼容改动前保存的旧 draft）。
2. `_apply_recipe_snapshot_to_warehouse` 处理 ic_recipe 行：
   - 目标仓按 **name** 查 ic_recipes：存在 → 复用其 id（不覆盖，门店价格自治）
   - 不存在 → 用 referenced snapshot 递归建（先 ensure items，再插 ic_recipe + BOM）
   - 缺 referenced snapshot 且目标仓也无同名 → 抛错（该仓标记 failed，不静默丢行）
3. 递归仅一层（出品不能引出品，DB CHECK 已保证无环）。

## 3. 新功能

### F1：全品牌成本总览 `/recipe-cost/overview`

**UX**：这是研发中心的「驾驶舱」。入口 landing 改跳总览；总览页顶部三个 tab
（成本总览 / 冰激凌配方 / 出品配方）。

- 统一表格：类型（冰激凌/出品）· 名称 · 出品量(g) · 采购成本 · 销售成本 ·
  售价 · 采购毛利率（(售价−采购成本)/售价）· 供货毛利率（(销售成本−采购成本)/销售成本）·
  供货利润（销售成本−采购成本）
- 筛选：类型（全部/冰激凌/出品）；排序：默认按类型+id，可按任一数值列
  （query string，服务端排序，简单可靠）
- 负毛利/负利润行红色高亮；售价为 0 的行毛利率显示「—」并给「未定价」标记
- CSV 导出 `/recipe-cost/overview/export.csv`
- 权限：`report.cost.view`（与现有 4 个 CSV 导出、api_cost 一致）；仅 RD 仓可用
  （blueprint 级 before_request 守卫已覆盖）

### F2：门店存量配方导入 `/recipe-cost/import`

**UX 流程（三步，预览确认制，不静默写库）**：

1. **选源门店**：GET `/recipe-cost/import` 列出所有 storefront 仓
   （产品数 / BOM 行数 / 与 RD 名称冲突数）。
2. **预览**：GET `/recipe-cost/import?warehouse=wh_003` 逐产品展开：
   - 每个 product → 目标 ic_recipe（名称沿用；Σ BOM 克数 → output_qty；sale_price=0 待补）
   - 每个 BOM 行按 **canonical_id 优先、sku 兜底** 匹配 RD 仓 items；
     显示 RD 侧品项名与双价；未匹配行红色标注
   - 名称与 RD 现有 ic_recipes 冲突 → 标「冲突」，导入时按用户选择 skip/overwrite
   - 存在未匹配原料的产品默认**不可勾选**（防止导入残缺配方）
3. **导入**：POST `/recipe-cost/import`，`product_ids[]` + `conflict_mode`(skip|overwrite)：
   - overwrite = 删旧 ic_recipe（含 BOM 级联）重建；skip = 跳过
   - 每个导入的 ic_recipe 调 `_upsert_recipe_draft` 建草稿版本（可立即走发布链路）
   - 写 audit `recipe_cost.import`（detail 记源仓、product_id→ic_recipe_id 映射、计数）
   - 结果页 flash 汇总：导入 N / 跳过 M / 失败 K

**边界**：
- 只读打开源门店 db（`_resolve_warehouse_db_path` 同款解析，绝对路径直连）；
  源仓不可达 → flash 报错不崩
- 不迁移 production_runs / 库存 —— 只迁配方定义
- 门店 products 无售价字段 → 导入后 sale_price=0，总览页显示「未定价」提醒补价
- 权限：`@require_platform_admin`（与配方编辑一致）

### F3：导航与入口整合

- `landing()` 改跳 `overview`（驾驶舱优先）
- `base_recipe.html` 增加统一 tab 导航（总览/冰激凌/出品/导入[admin]），
  替换现在各页散落的「← 返回 / → 出品配方」链接
- 列表页既有「📥 导出 CSV」按钮保留（emoji 属存量 UI 文案，本次不动模板图标体系，
  新增页面不引入 emoji）

## 4. 数据契约

**无 schema 变更**。全部功能基于现有表：
- warehouse db：`ic_recipes` / `ic_recipe_items` / `recipes` / `recipe_items` /
  `products` / `product_bom` / `items(canonical_id)`
- master db：`recipe_versions` / `recipe_publish_events*` / `warehouses`

## 5. 文件改动清单

| 文件 | 动作 |
|------|------|
| `blueprints/recipe_cost.py` | D1 修复；overview 路由×2；import 路由×3；landing 改跳；publish snapshot 增强（D2） |
| `blueprints/recipe_cost_pure.py` | （D1 后 pure 层语义即正确，无需改）新增 `overview_rows()` 汇总纯函数 |
| `blueprints/recipe_import_pure.py` | **新增**：源仓产品盘点 / BOM 匹配 / 导入执行（纯函数，可单测） |
| `static/recipe_cost.js` | D1：lineIcCost grams=qty；bump `?v=10` |
| `templates/recipe_cost/base_recipe.html` | F3 tab 导航 |
| `templates/recipe_cost/overview.html` | **新增** |
| `templates/recipe_cost/import.html` + `import_preview.html` | **新增** |
| `templates/recipe_cost/ic_recipes.html` / `recipes.html` / `*_edit.html` | 导航统一；D1 文案（份→克） |
| `permissions_manifest.py` | ROUTE_MATRIX 补 overview / import 5 个 endpoint |
| `docs/ARCHITECTURE.md` | 同 commit 更新（pre-commit 钩子要求） |
| `tests/test_recipe_cost_pure.py` / `test_recipe_cost_route.py` | D1 语义测试更新 |
| `tests/test_recipe_import_pure.py` / `test_recipe_import_route.py` | **新增** |
| `tests/test_publish_recipe_route.py` | D2 多态发布测试 |

## 6. 验收标准（EARS 摘要）

- AC-01：While 出品配方引用冰激凌配方行填 120（克），系统必须按 120g 折算
  采购/销售成本，且 output_qty 累计 = 120 + 其他行克数。
- AC-02：When 发布含 ic_recipe 引用行的出品配方到门店，系统必须在目标仓
  建立（或复用同名）被引用 ic_recipe 并完整写入引用行；缺快照且目标无同名时
  该仓必须标记 failed 而非静默丢行。
- AC-03：While 用户在 RD 仓打开 /recipe-cost/overview，系统必须展示
  ic_recipes + recipes 全量统一表（双成本、双毛利率、利润），售价 0 显示「未定价」。
- AC-04：When 用户从门店 wh_xxx 导入产品配方，系统必须按 canonical_id 优先匹配
  RD 品项；存在未匹配原料的产品必须拒绝导入并说明原因。
- AC-05：When 导入遇 RD 同名 ic_recipe，系统必须按用户选择 skip 或 overwrite，
  且每个导入结果写 audit log + 草稿版本。
- AC-06：无 `report.cost.view` 权限的用户访问 overview 及其 CSV 必须 403。

## 7. 测试与验证

- pytest（`--basetemp=/tmp/pytest-dc`）失败集零新增；known_failures 只收缩
- 关键断言做变异验证（改坏 D1 修复 → 测试必须红）
- UX 走查：用 Flask test client 按「导入 wh_003 存量 → 总览核数 → 建出品配方
  （小杯 120g）→ 发布到门店 → 门店库核对完整性」全链路模拟，验证操作方式合理
- ruff 仅改动文件

## 8. 生产侧事项（PR 中列出）

- 生产 rd_001 / 门店 recipes 均 0 行 → D1 语义修复**无生产数据迁移**
- 部署后建议操作顺序：先用 import 工具从 wh_003（配方最全，29 产品）导入对齐，
  人工补 sale_price，再逐仓发布
- 无 nginx / systemd / 防火墙变更

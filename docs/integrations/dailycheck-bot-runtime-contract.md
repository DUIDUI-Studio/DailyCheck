# DailyCheck Bot 工具运行契约（Issue #78 批准口径）

> **文档性质**：政策 / 契约文档，**不是实现说明**。本文只固化 Owner 已批准的工具运行边界，供 #80 / #81 与后续 review 引用；不修改 bot / MCP 代码、schema、配置或算法，不实现 #80 / #81 功能，也不改变 workflow、CI 或分支保护规则。
>
> **适用快照**：仓库 `a814504902850e2ef149aeec2fbb1fed5705383e`（PR1 已合入 main 的状态）+ #80 / #81 的目标状态。凡文中出现「当前」即指该快照的已实现事实；「目标」指尚未交付、不得当作已生效。
>
> **关联**：Part-Of [#78](http://hub01:3000/eric/dailycheck/issues/78)（父单 [#37](http://hub01:3000/eric/dailycheck/issues/37)）。本文不关闭 #37 / #78 / #80 / #81 中的任何一个。

## 0. 为什么需要这份契约

#78 的只读核对发现三套口径并存：实时 `get_tools()` 注册 29 项、bot LLM schema 9 项、原始计划 14 项（连同 5 个 bot 自有能力共 19 项）。历史叙述里「19 行」「所有 bot 一律只读」「机器人只暴露 6 个只读函数」都是不同时期的快照，若不区分就会被当成授权依据：

- 把展示行数写死成 19 或 34，会在下次注册新工具时静默失真；
- 把「bot 只读」当成不可变铁律，会与 #37 授权的三写限定例外冲突；
- 把批准接入当成生产已启用，会把政策前置误报成线上能力。

本文把 Owner 批准（D1–D7）落为可核对契约，并把 **PR1 当前事实** 与 **#80 / #81 目标状态** 分开写。

## 1. 权威与批准记录

| 记录 | 链接 | 作用 |
|---|---|---|
| Owner 决策 | [comment 714](http://hub01:3000/eric/dailycheck/issues/78#issuecomment-714) | `DECISION: A` —— 全部采纳 Planner 建议（D1–D7） |
| 决策选项原文 | [comment 635](http://hub01:3000/eric/dailycheck/issues/78#issuecomment-635) | A / B 两选项及七项建议的完整表述 |
| 契约核对审计 | [comment 634](http://hub01:3000/eric/dailycheck/issues/78#issuecomment-634) | 29 vs 9 vs 14/19 的对账、四旧名与 `get_price` 差异、P01–P10 待核事实 |

> 2026-10-29 的 `A`（comment 639）是补充历史；**本文以 comment 714 为最终决策**。Owner 批准的是「接入边界与阶段」，**不等于**生产已启用、已部署或已验收。

## 2. 术语与集合定义

| 符号 | 定义 |
|---|---|
| `M(t)` | 实时 MCP 注册名集合 = `mcp_server.protocol.tools.get_tools()` 返回的 `Tool.name` 集合（当前 29） |
| `O` | bot 五自有能力，固定为 `search_items`、`list_low_stock`、`list_store_overview`、`list_orders`、`get_project_help` |
| `D(t)` | 主展示集合 = `M(t) ∪ O`（当前 34 主行；动态计算，不写死） |
| `E` | 执行集合 = 已实现 adapter/schema ∩ 阶段批准 ∩ 当前消息配置快照 ∩ 可信身份 / 路径 / 仓库权限的交集 |

**核心不变量**：

1. **展示 ≠ 执行**。页面出现某名称、有元数据、被发现为新注册项、或配置写成 on，都不能单独授予执行权限。
2. **同一条消息一个快照**。实际发给 LLM 的 `tools` 数组与执行入口使用**同一次** `load_effective_config()` 结果；同一条回复的多轮工具调用看到同一套开关。
3. **执行时再做范围检查**。ACL（路径 + 仓库范围）在执行侧落实，不能只靠「配置里开着」放行。

## 3. 阶段划分

| 阶段 | 载体 | 内容 |
|---|---|---|
| **PR1（当前）** | 已合入 main | 配置热加载 + 展示/执行集合分离；34 主行；9 项 schema；四旧名子条目；`get_price` 诊断；9 项故障回退 |
| **PR2（目标）** | [#80](http://hub01:3000/eric/dailycheck/issues/80) | 原 14 + 八读接入、三写护栏、七写拒绝、父子开关、真实 userid 校验 |
| **PR3（目标）** | [#81](http://hub01:3000/eric/dailycheck/issues/81) | `replenishment_board` 注册 / adapter / 问答验收；新读遵循显式启用 |

## 4. D1–D7 决策契约

| 决策 | 批准 | 固定实施边界（精确名称） | 阶段 | 承接 / 验收 owner |
|---|---|---|---|---|
| **D1** | [714](http://hub01:3000/eric/dailycheck/issues/78#issuecomment-714) / [635](http://hub01:3000/eric/dailycheck/issues/78#issuecomment-635) / [634](http://hub01:3000/eric/dailycheck/issues/78#issuecomment-634) | 展示集合 `D(t)=M(t)∪O` 实时主行（当前 29+5=34）；四旧名（`list_warehouses` / `list_warehouse_items` / `get_item_detail` / `get_item_consumption`）挂对应父 MCP 子条目；五自有完整保留；新增注册项自动展示、标「待确认」且不可执行。断言 `set(rows)=set(get_tools names)∪O`，**不硬编码永久 34 / 19** | PR1 已实现；#80 保持并做集合回归 | #80（developer）；Reviewer 只读复核 |
| **D2** | 同上 | 原 14 之外八读逐项接入：`canonical_list`、`canonical_detail`、`canonical_categories`、`canonical_fanout_targets`、`canonical_conflicts`、`canonical_fanout_event`、`store_order_list`、`store_order_detail`。不允许只完成历史 14 就称「全量」 | 目标（#80），当前未接入 | #80 |
| **D3** | 同上 | 七个 canonical 写本轮**只展示、默认 off、不可执行**：`canonical_create`、`canonical_update`、`canonical_claim`、`canonical_unclaim`、`canonical_fanout`、`canonical_resolve_conflict`、`canonical_set_binding_active`。管理员 on、owner userid、进程全权限 token 均不能解锁 | PR1 展示；#80 保持不可执行 | #80 |
| **D4** | [714](http://hub01:3000/eric/dailycheck/issues/78#issuecomment-714) 采纳 D4 | bot 只读边界**仅**对 `restock_create` / `outbound_create` / `outbound_rollback` 三写作**有条件例外**；查询连接仍 `mode=ro`；不扩大到 canonical 写 | 授权已批准；执行能力由 #80 实现，**当前不可执行** | #80；生产前置由 Prod Agent 核实 |
| **D5** | 同上 | `get_item_consumption` 保留旧参数、流水口径与返回语义；MCP `item_consumption` 使用自己的固定窗口及业务单据口径；两者不能静默互相替换 | PR1 保留；#80 界定 | #80 |
| **D6** | 同上 | `get_price` 不新增 schema、开关或自有主行，入口持续拒绝；价格由 `get_item_detail` 承接 | PR1 已隐藏；#80 保持拒绝 | #80 |
| **D7** | 同上 | 八读验收后**仍默认 off**，管理员逐项显式启用；未来未知工具只展示；原 14 按父计划分阶段启用；三写护栏齐备后正常配置默认 on | #80 / #81 | #80（执行集合与开关）；#81（新读跟随） |

## 5. 工具清单（精确名称）

### 5.1 实时 MCP 注册 29 项

**只读 19 项**：

| 名称 | 服务模块 | 仓库范围 |
|---|---|---|
| `warehouse_list` | inventory | per-token |
| `items_list` | inventory | per-token |
| `items_detail` | inventory | per-token |
| `movements_list` | inventory | per-token |
| `restock_list` | inbound | per-token |
| `outbound_list` | outbound | per-token |
| `warehouse_consumption` | consumption | per-token |
| `item_consumption` | consumption | per-token |
| `item_forecast` | forecast | per-token |
| `procurement_store` | procurement | per-token |
| `procurement_hub` | procurement | per-token |
| `canonical_list` | canonical | 全局（跨仓） |
| `canonical_detail` | canonical | 全局（跨仓） |
| `canonical_categories` | canonical | 全局（跨仓） |
| `canonical_fanout_targets` | canonical | 全局（跨仓） |
| `canonical_conflicts` | canonical | 全局（跨仓） |
| `canonical_fanout_event` | canonical | 全局（跨仓） |
| `store_order_list` | store_order | per-token（门店仓或总仓任一命中） |
| `store_order_detail` | store_order | per-token（门店仓或总仓任一命中） |

**写 10 项**（其中 7 项为 D3 禁止，3 项为 D4 限定例外）：

| 名称 | 服务模块 | bot 边界 |
|---|---|---|
| `restock_create` | inbound | D4 限定例外（当前不可执行） |
| `outbound_create` | outbound | D4 限定例外（当前不可执行） |
| `outbound_rollback` | outbound | D4 限定例外（当前不可执行） |
| `canonical_create` | canonical | D3 禁止 |
| `canonical_update` | canonical | D3 禁止 |
| `canonical_claim` | canonical | D3 禁止 |
| `canonical_unclaim` | canonical | D3 禁止 |
| `canonical_fanout` | canonical | D3 禁止 |
| `canonical_resolve_conflict` | canonical | D3 禁止 |
| `canonical_set_binding_active` | canonical | D3 禁止 |

### 5.2 bot 五自有能力

固定为 `search_items`、`list_low_stock`、`list_store_overview`、`list_orders`、`get_project_help`。它们是 bot 自有实现，不属于 MCP 注册项，展示与执行都在 bot 进程内。

### 5.3 四旧名关联与父子开关

| 旧名（bot 实现） | 关联父 MCP 名称 | 语义区别（不可当成等价替换） |
|---|---|---|
| `list_warehouses` | `warehouse_list` | 旧名是 bot 自有实现，无 MCP 令牌仓范围 |
| `list_warehouse_items` | `items_list` | 旧名保留分类概览 / 截断 / 仓名解析 |
| `get_item_detail` | `items_detail` | 旧名按 SKU / 名称**跨仓**查库存、售价、成本，保留价格未维护语义 |
| `get_item_consumption` | `item_consumption` | 旧名按 `stock_movements` 动作流水口径；不等于 MCP 「未回滚业务单据」口径 |

> **箭头含义**：表中的「旧名 → 父 MCP 名称」只表示**展示关联**，不表示函数参数、数据源或输出等价。四个旧名是当前已接入的 bot 实现（`executable=True`），父 MCP 行在 PR1 仍标「未接入」。

**父子开关与执行语义（PR2 正常配置）**：

- 子名需**同时**满足自己的开关与关联父 MCP 的开关；父 off 则子名不进入 `tools`，执行入口也拒绝，旧历史 / 伪造调用不可旁路。
- 保留子名独立开关与各自调用统计；不把旧消费数字改写成 MCP 数字。
- PR1 阶段父 MCP 尚未接入，**不回溯**要求旧九项依赖不可用的父 adapter；PR2 切换时必须明确默认键与既有保存配置的解释，保持正常旧查询能力。

### 5.4 `get_price`（D6）

`get_price` 只存在于 bot 的 dispatch 表，**不是 LLM schema 工具**，也不占自有主行。D6 决定价格继续由 `get_item_detail` 承接，`get_price` 保持隐藏：入口对 `get_price` 返回 `unknown_tool`，不新增 schema / 开关 / 主行。

## 6. D1 动态注册集与断言

- `D(t)` 每页实时计算：页面不复制静态工具清单，新增注册项自动出现；无元数据时标「待确认（新增注册项，默认不执行）」。
- **集合断言**（`tests/test_bot_admin.py`）：
  - `set(主行名称) == {t.name for t in get_tools()} | O`
  - 四旧名子条目集合固定为四个名称
  - 注入未知注册项后主行自动增长、`executable=False`，执行集合 `E` 不增长
- **34 是当前观测值，不是永久常量**。测试同时保留「实时注册名集合相等」的结构断言，避免只锁行数。
- `E` 永远是 `TOOL_NAMES`（当前 9）的子集；MCP 名称、未知名称、旧名映射都不能凭配置扩大 `E`。

## 7. D2 八读接入（挂 #80）

八项均初始状态为「**已批准接入、未验收不可执行、验收后待管理员显式启用**」。所有只读调用的 `allowed_write_paths=[]`，不继承环境 token 或上一用户 ContextVar。

| 工具 | 当前只读路径 ACL | 参数 / 输出验收要点 |
|---|---|---|
| `canonical_list` | `GET /api/v1/canonical` | `status` / `category_code` / `q` / `limit` / `offset`；核对 `items`、`categories`、`total`、`truncated`；分页与汇总不得泄露未授权范围 |
| `canonical_detail` | `GET /api/v1/canonical/<id>` | `canonical_id`；核对 `canonical`（含别名）、`bindings`、`bound_warehouse_count`；跨仓绑定与计数同属数据范围 |
| `canonical_categories` | `GET /api/v1/canonical/categories` | 无参数；核对全局品类及 `item_count`，不伪称逐仓结果 |
| `canonical_fanout_targets` | `GET /api/v1/canonical/fanout-targets` | 无参数；核对 storefront / rd / distribution_center 候选仓；「列候选」不授予 fanout 写权限 |
| `canonical_conflicts` | `GET /api/v1/canonical/conflicts` | 可选 `canonical_id`；仅查询 open 冲突；不可调用 `canonical_resolve_conflict` |
| `canonical_fanout_event` | `GET /api/v1/canonical/fanout/event/<id>` | `event_id`；核对 `event`、`warehouses`、`items` 完整范围及不存在事件处理 |
| `store_order_list` | `GET /api/v1/store-orders` | 可选 `warehouse_code` / `status` / `limit` / `offset`；沿用门店仓 OR DC 仓任一命中 `allowed_warehouses` 的可见性；核对 `orders`、`total`、`limit`、`offset`、`truncated`、去重与跨页稳定性 |
| `store_order_detail` | `GET /api/v1/store-orders/<id>` | `order_id`；`order` + `items` + `deliveries` + `receipts` + `status_history` 五段；列表与详情可见性一致，范围外与不存在同义 |

**六个 canonical 全局读不得误写为自动限仓**：

- 现成 handler 只检查路径，service **没有**自动按 `allowed_warehouses` 裁切全局主数据；详情还会收集跨仓 bindings，事件返回逐仓 / 逐品项。
- D2 批准接受这种**能力范围**，**不等于给每位企微发送者全局读权限**。#80 必须在接线矩阵中明确哪个可信调用上下文拥有全局主数据 / 跨仓读取授权，并验证允许与拒绝两侧；仅有受限仓权限的上下文**不能被偷偷提升**为 `allowed_warehouses=None`。
- 若现有身份配置无法表达所需全局授权，保留该读 off 并把精确范围缺口交 Coordinator，而不是以「Owner 已选 A」掩盖范围校验。

**订单两读**复用现有 OR 可见性与分页：指定仓过滤进一步收窄，不能放宽；非法 / 无法解析的显式仓名**不得回退全仓**。

## 8. D3 七个 canonical 写禁止

`canonical_create`、`canonical_update`、`canonical_claim`、`canonical_unclaim`、`canonical_fanout`、`canonical_resolve_conflict`、`canonical_set_binding_active`。

- 本轮只展示、默认 off、不可执行；管理员手动 on、owner userid、进程全权限 token **均不能**解锁。
- #80 **不**为这七项注册 bot 可执行 adapter / schema；模型伪造调用逐项拒绝，且数据库无变化。
- MCP 原有客户端能力不在本计划中删除：禁止的是 **bot 执行**，不是 MCP 服务本身。

## 9. D4 三写限定例外

**唯一例外名称**：`restock_create`、`outbound_create`、`outbound_rollback`。

### 9.1 当前状态

- **当前不可执行**。PR1 没有任何写执行路径；本例外只是 Owner 批准的政策边界，执行能力由 #80 实现并验收。本文不得被引用为「bot 现在能写」。
- 例外**不扩大**到七个 canonical 写，也不授权其他任何 MCP 写工具。

### 9.2 护栏（缺一不可）

1. **开关**：三写各自开关 + 当前消息配置快照；关闭即不进入 `tools`，入口也拒绝。
2. **真实 userid**：来自企微可信 frame，传到 agent，**不从模型 args 获取**；默认仅 owner 的确切 userid，由 Owner / Prod 证据核实（原文中的字面人名不作身份事实）。
3. **路径 + 仓范围**：`allowed_write_paths` 与允许仓库逐道校验；只有受限权限的上下文不得因白名单而越权。
4. **回执与审计**：回执给实际单号 / 数量 / 影响行数；`admin_audit.log` 含 who / tool / args / result / 耗时。
5. **只读查询边界不变**：bot 的 SQLite 查询连接保持 `mode=ro`；写路径若实现，必须是独立通道，**不得**放宽查询连接的只读约束。

### 9.3 生产前置

- 无真实 owner userid 证据时**不得在生产启写**；不得因此把已批准的正常默认 on 改成新的产品决策。
- #80 必须验收：允许与拒绝两侧、并发两身份不串上下文、白名单下一条消息生效。
- 相关 PR body 必须留 `Owner-Authorization:` 行并链接 714 / 635 / 634。

## 10. D7 故障回退与显式启用

**九项故障回退**（缺表 / 锁库 / 读取异常 / 非法配置值）：

- 固定回退为「现有 9 项只读 + `medium`」，写白名单为空。
- **不**从扩大的 PR2 schema / dispatch 推导回退集，**不**启用八读、三写或七写。
- 回退仍遵守已建立的身份与查询范围边界，不能用 degraded 绕开 ACL。

**显式启用**：

- 八读验收通过后仍默认 off，管理员逐项显式启用。
- 原 14 按父计划分阶段启用；三写护栏齐备后正常配置默认 on。
- 未来未知工具只展示，不自动进入执行集合。
- 配置每条消息读取一次，修改在下一条消息生效；同一条回复各轮使用同一快照。

## 11. PR1 当前事实 vs #80 / #81 目标

| 项 | PR1 当前（main 快照） | #80 / #81 目标 |
|---|---|---|
| 展示主行 | 29 MCP + 5 自有 = 34；四旧名子条目；`get_price` 诊断 | 保持动态集合，新增注册项自动显示 |
| 执行集合 | `TOOL_NAMES`（9）∩ 消息配置快照 | 原 14 + 八读 + 三写（按开关 / 身份 / 范围） |
| MCP adapter | 无（PR1 不接入） | #80 接入并逐项验收 |
| 写能力 | 无执行路径 | D4 三写护栏 + D3 七写拒绝 |
| 故障回退 | 9 项 + `medium`，写白名单空 | 同一固定回退，不随 schema 扩张 |
| `get_price` | 隐藏，入口 `unknown_tool` | 保持隐藏 |
| 配置读取 | 每条消息一次 | 同一快照语义保持 |
| 调用计数 | `bot_tool_calls.log` JSONL 追加 | 保持；不另造遥测平台 |
| `replenishment_board` | 本快照未注册 | #81 注册 / adapter / 问答验收 |

## 12. 历史叙述澄清

| 旧叙述 | 澄清 |
|---|---|
| 「19 行」 | 原 14 MCP + 5 自有的展示基线。PR1 起展示集合 `D(t)` 动态计算（当前 34 主行），**不再把 19 写死**；新增注册项自动出现。 |
| 「所有 bot 一律只读」 | 查询连接 `mode=ro` 与七项 canonical 写禁止**保持不变、不被弱化**。「只读」现在是**按批准范围限定的策略**：D4 三写是有条件例外，且**当前不可执行**。不得据此声称 bot 已能写。 |
| 「机器人只暴露 6 个只读函数」 | v1 历史口径。当前为 9 项 LLM schema（含四旧名实现）+ 5 项自有展示 + `get_price` 诊断；详情见 §5。 |

## 13. 验收与后续证据（不在本文交付）

本文只固化契约；以下为 #80 / #81 的验收要求，引用 #78 计划：

- 八读逐名 schema → dispatch → 真实临时库结果及拒绝侧；关闭后 `tools` 不含该名、伪造调用被拒。
- 旧消费差异样本：同一临时库覆盖普通出库、门店订货出库、总仓调货出库、生产领料及镜像、已回滚记录、午夜边界与 90 天查询；验证差异与口径标签，不修改预测公式。
- 三写：开关 / userid 白名单 / 路径 / 仓范围逐道拒绝；回执与审计；并发两身份不串上下文。
- 测试使用原生收集路径：`tests/test_bot_admin.py`、`tests/bot/test_agent_config.py`、`tests/bot/test_agent_requests.py`、`tests/bot/test_agent.py`、`tests/bot/test_tools_ext.py`、`tests/test_store_order_mcp.py`。

## 14. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-10-30 | 初版：按 #78 comment 714 固化 D1–D7、三写限定例外与 PR1 / #80 / #81 阶段边界（Issue #88） |

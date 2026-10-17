# CHANGELOG

所有对外可感知的变更记录在此。格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [Unreleased]

### Fixed — 门店订货：未提交的数量选择在切换筛选后被清零（fix/catalog-draft-persist，2026-10-17）
- **症状**（Issue #100）：订货页选好品类、给几张卡片填了数量，**没点「加入购物车」**就点别的品类或「全部」，填过的数量全部消失，得重填一遍。
- **根因**：已填数量只存在于当页 DOM（卡片 hidden input + `data-saved-qty`），而品类 chip 和搜索框都是整页 GET 跳转，DOM 重建即归零。
- **修复**：按 `dc.catalogDraft.v1:<user_id>:<dc_code>` 把「canonical_id → 数量 / 单位名 / 单位档位」落到 localStorage（7 天过期），页面渲染后回填当前可见卡片；被筛选隐藏的项留在草稿里，不误删。页面顶部给出「已填 N 项（含其他品类筛选下的选择）」提示条。
- **跨筛选一并提交**：「加入购物车」前把草稿里未在当前页渲染的条目追加成 hidden input 一起 POST。此前跨品类分批填的数量会静默漏单（`cart_add_batch` 只收当前页卡片）。
- **收口点**：购物车页渲染时清空该 dc 草稿（批次加购已整体消费，防 bfcache 回退后重复加购）；「清空选择」同时清草稿；`pageshow(persisted)` 按草稿重新同步卡片。
- **附带修复**：重复编辑已填卡片时弹窗单位 pill 无法回填——`unit[]` 存的是中文单位名（后端契约），却拿它去匹配 `base`/`aux` 档位，导致两个 pill 都不高亮、点「确认」静默无响应。改由卡片 `data-saved-unit` 提供档位。
- **测试**：`tests/test_store_ordering_store_route.py` 新增 3 个模板契约用例（草稿 scope 必须是 `user:dc`、cart 页清空脚本存在），变异验证 3/3 变红；另用 Playwright 在真实浏览器跑通 24 项交互断言（含反向验证：回滚模板后「去掉筛选」必现 `—`）。

### Fixed — 反馈派发的 page_path 语义修正 + DC 品项开关整页重载（fix/feedback-page-path-and-toggles，2026-10-17）
- **`page_path` 是「提交时所在页面」，不是「出问题页面」**（Issue #93 实测）。中转页（`auth` 蓝图下 login / logout / select-warehouse）不再产生代码线索，`collect()` 返回 `matched=False` + `reason=transient_page`；按 endpoint 前缀判定而非硬编码路径，改路由时黑名单不会静默失效。失败原因细分为 `no_path` / `unmatched` / `transient_page`，正文文案各不相同（中转页明写「不能作为出问题位置的证据，以现象为准」）。
- **验收标准不再断言具体页面**。原第一条「站内 `<page>` 页面按现象描述逐项复现」在 page_path 错误时等于让开发侧改错文件还自认合格；改为「已核对代码线索指向的页面确实就是现象所在页面，不一致时以现象为准」。
- **正文「页面」字段改名为「提交时所在页面」**，措辞本身即警示。
- **管理员可覆盖实际页面**：`/admin/feedback/<id>` 新增「实际页面」输入框，`dispatch_feedback` 读 `page_override` 后重新定位；覆盖值同样经 `clean_path()` 受「只可能是站内相对路径」约束。覆盖生效时正文同时保留两行（提交时所在页面 / 实际页面（管理员指定）），可追溯。
- **「插入结构模板」空骨架不再进正文**：新增 `strip_empty_structure()`，剥掉值为空的 `现象：/影响范围：/期望结果：` 行（只匹配标签后无内容者，`现象：点导出没反应` 原样保留）；净化放在 `admin_notes()` 入口，正文与信息缺口判定共用。前端同时修光标定位（`setSelectionRange(3,3)` 落到「现象：」之后，原先 `value` 重设后 selection 归末尾，人顺着最后一行写、三行骨架全空着提交）。
- **DC 品项可订开关改 AJAX**：`templates/store_ordering/dc_items.html` 的 toggle form 补 `data-ajax data-ajax-mode="reload-stay"`。此前点「下架/恢复可订」走整页 POST → 302 重渲染 → 滚动归零，而本页按品类分组渲染多张表，连续调整多个品项必须反复从顶部滚回来（Issue #93 本体）。
- 测试：新增 `tests/test_feedback_page_path.py`（20 用例）；`test_feedback_introspect.py` 拆分到 279 行。四组变异验证全部 KILLED（去掉中转页判定 / 去掉骨架净化 / 不读 `page_override` / 模板去掉 `data-ajax`）。

### Added — Issue 精确化：派发正文自带代码定位与验收标准（feat/feedback-issue-precision，2026-10-17）
- **自动代码定位**：新增 `blueprints/feedback_introspect.py`，从反馈单的 `page_path` 反查 Flask endpoint → 源码文件:行号 → 模板 → 该文件最近 5 次提交，写进 Issue 正文的「代码线索（自动生成）」段。派发时不再只有用户一句口语。全程 fail-soft（探测失败只少一段线索，绝不阻断派发）、只读、`git` 调用 3 秒超时。
- **正文改为验收契约**：`build_issue_body()` 五段结构——顶部「已确认需要修复，不需要再回问」（挡在巡检「判断不清→转人工」的门外）、现象（引用块）、管理员判定、代码线索、信息缺口（缺什么 + 「开发侧自行推断，不要阻塞等待」）、验收标准（4 条可复核项）、范围边界（不做顺带重构，语义变更须先评论）。
- **管理员侧预览**：`/admin/feedback/<id>` 在待处理状态显示「派发后 Issue 会带上这段代码线索」，管理员可据此确认自动定位是否正确；定位失败时明确提示未能自动定位。处理意见输入框加「插入结构模板」按钮（现象 / 影响范围 / 期望结果），非强制。
- **巡检自动化同步升级**：`automation-1784861165563` 新增 `2c-pre`——接手前必须 `gh issue edit` 把正文固化成「现象 / 根因 / 修复方案 / 验收标准」四节，原文移入「原始记录」；「判断不清→转人工」新增例外：正文带「已确认需要修复」的默认按确定要修处理。
- **重构**：正文渲染从 `feedback_dispatch.py` 拆到 `feedback_issue_body.py`（纯函数，155 + 245 行，均在单文件上限内），`feedback_dispatch` 保留 re-export 维持调用方不变。
- **测试**：新增 `tests/test_feedback_introspect.py` 32 用例；三组变异验证（去掉 `inspect.unwrap` → 定位到装饰器 wrapper 而非业务文件、去掉既定结论声明、`clean_path` 放行外部 URL）各自让对应用例变红。

### Added — 问题反馈闭环（feat/feedback-board，2026-10-17）
- **用户提单**：任意登录用户在每页右上「反馈」或导航「提交问题」提交问题单（分类 / 一句话说明 / 详细描述），自动带出当前页面路径。数据落 master.db 新增的 `feedback_tickets` + `feedback_comments` 两表（系统级，不沾仓库库）。
- **双通道提醒**：提交后站内通知扇出全部平台超管（导航「反馈收件箱」带待处理角标）+ 企微群机器人推送（`WECOM_FEEDBACK_WEBHOOK`，空 = 不推企微，站内照发）。两条通道都跑在业务事务外，任一失败不回滚已落库单子。
- **管理员判定**：`/admin/feedback` 收件箱（待处理置顶 + 状态筛选）→ 详情页二选一：「不重要，忽略」（终态）或「写评论 + 发送并派发开发」（终态）。
- **开发闭环派发**：派发按 GitHub REST 建 Issue（标签 `user-feedback`），正文含反馈原文、管理员意见、来源页与闭环要求（开发 → PR → CI → merge → 部署）。一票一单（`issue_number IS NULL` 守卫 + 路由层前置判重）。token 缺失或 API 失败时降级为「只存评论 + 明确提示手动建单」，**绝不伪装成功**。
- **权限**：用户侧 `LOGIN`、管理侧 `PLATFORM`，`ROUTE_MATRIX` 9 条已登记；他人单子一律 404（不泄露 id 存在）。
- 新增 `tests/test_feedback.py`（51 用例）；`docs/ARCHITECTURE.md` 新增 §3.10。

### Added — 商业化 P0 收敛（feat/csrf 分支，2026-10-10）
- **CI 门禁**：`ci.yml`（ruff 增量 + pytest 全量零新增失败断言 + deploy 依赖 test 通过）。
- **CSRF**：全站 75 个 POST 表单注入 token（Flask-WTF）；TESTING 模式存量测试零改动。
- **Rate limit**：全局 IP 600 次/分钟；登录 15 分钟窗内连错 5 次锁 30 分钟；MCP per-token 配额（默认 120/分钟，`agent_tokens.rate_limit` 可调）。
- **迁移 runner**：`flask migrate status|up|down`（user_version 门控 + 迁移前自动热备）。
- **可回滚部署**：`scripts/deploy_vps.sh`（备份→切换→迁移→健康检查→失败自动回滚）；部署 tar 严格排除数据文件，修复原 deploy 脚本可能覆盖生产数据库的问题。
- **合规文档**：LICENSE / SECURITY.md / PRIVACY / TERMS / SLA / 本文件。

### 测试
- 新增 `tests/test_csrf.py`（7）、`tests/test_rate_limit.py`（15）、`tests/test_migration_runner.py`（9）。
- 基线：18 failed（pre-existing 恒定）/ 742+ passed，CI 强制零新增。

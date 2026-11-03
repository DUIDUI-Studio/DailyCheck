# Issue #140 开发阶段验证

## 结论

实现和本地验证完成，等待独立评审与 PR/CI 后续；生产交付尚未完成。业务源固定在 `1f995e2f0c6b962e27551c309e752ce8149ad9df`，之后只添加标准报告。最终 PR URL/head/CI 以 Orca settlement 及阶段回帖为准。

## 证据

外部工件目录：`/Users/ericmr/.cache/dailycheck-coordination/run_0cbbf33a0d93/writer-evidence-task_c1ad7027772a/`。原命令写入 `reports/issue-140/`，完成后原件按同名移入此目录；日志、XML、截图、调试 JSON 与脚本均未提交 Git。

| 结论 | 分类 | 工件/原生结果 |
|---|---|---|
| 基线缺少详情链接、库存固定货主/TOP 500 | measured | baseline-reproduction.txt，真实 Flask/Jinja+合成上游；exit 0 |
| 最终完整 suite 零新增 | measured | final-pytest.txt、final.xml、final-exit.txt=1、final-gate-exit.txt=0、final-completeness.json；14 failed/3642 passed，失败集等于基线 14 |
| 607 行全分页、跨货主同编码与跨仓限制 | measured | tests/test_wms_web_service.py、focused-tests.txt；199 passed/exit 0；SQL 方言适配执行合成库 |
| 入出详情唯一标识、库位独立且不重复数量 | measured | 服务/Flask 聚焦测试及四个 mutation-*.txt；每个变异对应断言真实失败 |
| 桌面/手机真实渲染与导航 | measured | desktop-stock.png、desktop-inbound-detail.png、mobile-stock.png、mobile-outbound-detail.png、mobile-outbound-lines.png、browser-*-proof.json；Orca 真实浏览器+合成上游，390×844/coarse，无根元素横向溢出，宽表单独滚动 |
| MCP 配置货主边界/契约不变 | measured | MCP 单货主 SQL 实执行回归，mcp-contracts.txt exit 0，32/19/22；原 wms.py、registry/schema、Bot 没有改动 |
| Lint/架构/权限/红线门禁 | measured | ruff-ci-version.txt、arch-map.txt、pr-hygiene.txt、redlines.txt；各 exit 0；Bandit High 0 |
| 上游真实 schema 与 SELECT 兼容 | measured, Coordinator 提供 | 外部 upstream-schema-evidence.json（SHA256 800f541cf28d52588fef9263098a16d0eed5918def761067b9c31e76cc57b110），candidate-select-smoke.json；获 Owner 单独授权的 Coordinator 实测 exit 0；服务 SHA256 090211bafbc430bd239b734e67cd6f35d5a116b9e47ec46d480b9127d1b9448a 与冻结业务源相等 |
| 生产真实 Web 权限/范围/观察窗通过 | 尚未证明 | 留给 Prod；本地夹具和 Coordinator SELECT 均不代替生产页面验证 |

上游两份证据位于上述目录的父目录。本文不包含真实业务行或凭据，Dev 没有访问上游或生产主机。

## 验收状态

- [x] 原生完整基线和最终终态、退出码、精确失败集比较，known_failures 不变。
- [x] 双方向唯一 ID 详情、头/货主/行/单位/批次/库位，返回原筛选。
- [x] 默认两朴宇仓 (1,3) 全货主，保留空仓，货主筛选与 >500 条稳定分页。
- [x] Cookie/DC/viewer、其他既有仓仍只读配置货主，任意 owner/warehouse 不能扩权。
- [x] 参数化 SELECT、no-store、无写入入口、安全空/错/越界/不完整状态。
- [x] 桌面/手机真实浏览器证据明确合成数据；schema 与 SELECT 有独立真实证据。
- [x] Ruff 0.16.10/0.17.3、契约、权限、架构、红线门禁及四项变异。
- [x] origin/main 起分支且 merge origin/main（最新 ccc0a76，Already up to date）。
- [ ] Fresh Reviewer 最终结论及 PR/CI 状态由 Coordinator 跟进。
- [ ] Prod 独立生产审查、候选部署、至少 30 分钟观察窗、merge、Issue 关闭和部署成功终态。

## 限制与责任

ROW_NUMBER 按唯一库存键、单据日期+唯一头键排序，保证固定数据集的完整浏览；实时库存变化仍会改变页成员，页面已有说明。详情/库位超过上限明确不完整，不当作完整单据；概览不合计混合单位。

当前是开发阶段候选，不是 delivered。没有 merge、部署、Issue 关闭、强推、策略变更、子代理或权限扩张。Coordinator 跟进独立 Review 与 CI；Prod 决定生产操作。被中断和导航失败的原件留存，未作为最终通过证据。

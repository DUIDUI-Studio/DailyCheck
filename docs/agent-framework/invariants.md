# 不变量登记册

> **本文件由 `scripts/gen_invariants.py --write` 生成，禁手改。**
> 事实来源是测试 docstring 里的 `Invariant:` 标记 —— 不变量只能登记在
> **可执行的守卫**上，写在别处扫不到。这样「登记了但没人守」在结构上
> 就不可能发生。
>
> 只登**真出过事**的（source 必须是 Issue 号），不登「最佳实践」——
> 后者会让登记册膨胀成没人读的文档。

| ID | 不变量 | 来源 | 守卫 |
|---|---|---|---|
| I-01 | 「今日」判据必须显式走 CST，不依赖进程本地时区 | #117 | `tests/test_dashboard_ops.py::test_kpi_today_boundary_is_tz_independent` |
| I-02 | 计数为 0 必须先自证采集链路活着，否则不得渲染成「无异常」 | #120 | `tests/test_dashboard_ops.py::test_events_zero_without_instrumentation_is_not_reported_as_clean` |
| I-03 | 会话分类用白名单，非自动前缀一律算人工（宁多报不漏报） | #118 | `tests/test_dashboard_ops.py::test_classify_session_uses_whitelist` |

## 已退役

见 `invariants-retired.txt`（手写，仅追加）。退役必须写明原因与替代项，
否则 `--check --base` 会报「ID 静默消失」。

（暂无）

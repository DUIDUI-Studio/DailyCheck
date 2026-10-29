# Bot `reasoning_effort` 首 token 采样（Issue #79 PR1）

日期：2026-10-29（Asia/Shanghai）；环境：开发机 macOS，Python 3.12，aiohttp 直连
`https://api.minimaxi.com/v1/chat/completions`；模型 `MiniMax-M3.1-Flash`。

## 方法

- 同一 prompt、同一模型、同一网络环境；5 组配对样本，交替执行 baseline / medium。
- **baseline**：请求体不传 `reasoning_effort`（MiniMax 文档默认 `max`）。
- **medium**：请求体顶层传 `reasoning_effort=medium`。
- `max_tokens=4000`，`stream=true`；分别记录首个 delta（reasoning 或 content）与首个
  **content** delta 的耗时，以及 reasoning 字符数与结束原因。
- prompt：8 个门店仓 + 1 个配送中心的补货分析题（约 100 字，需多步推理）。
- 本记录不含 API key；样本来自开发机，不等同于生产 bot 环境。

## 原始样本（秒；`first content = —` 表示 4000 token 预算内没有内容）

| # | baseline first delta | baseline first content | baseline reasoning chars | baseline finish | medium first delta | medium first content | medium reasoning chars | medium finish |
|---|---|---|---|---|---|---|---|---|
| 0 | 7.907 | — | 15,978 | length | 5.521 | 21.518 | 4,999 | stop |
| 1 | 3.365 | — | 16,416 | length | 3.130 | 25.298 | 7,373 | stop |
| 2 | 2.435 | — | 14,680 | length | 2.635 | 34.117 | 10,238 | stop |
| 3 | 6.512 | — | 15,577 | length | 9.059 | 56.746 | 14,053 | stop |
| 4 | 3.265 | — | 16,061 | length | 5.364 | 12.116 | 2,101 | stop |

## 观察

- **顶层字段生效**：5/5 medium 样本均被 API 接受（无 4xx），`reasoning_effort` 的位置
  与取值合同由 `tests/bot/test_agent_requests.py` 钉住。
- **baseline（默认 max）5/5 在 4000 token 内耗尽预算、没有产出任何 content**；
  reasoning 中位数 15,577 字符。
- **medium 5/5 产出 content**，首 content 中位数 25.3s（范围 12.1–56.7s）；
  reasoning 中位数 7,373 字符，约为 baseline 的 47%。
- 首个 delta（reasoning 首字）两边没有稳定差异（中位数 3.365s vs 5.364s），
  说明思考强度主要影响 reasoning 总量/完成时间，而不是 TLS/排队等首包延迟。

## 结论与边界

- 已按「同 prompt、同模型、重复样本」记录差异：medium 明显减少 reasoning 量，并在
  baseline 被预算截断时仍能产出可见内容；这是本 PR1 把默认思考强度从 `max` 收窄到
  `medium` 的直接依据。
- 生产 bot 的 `MAX_OUTPUT_TOKENS=800` 与真实问题分布不同；本采样不宣称生产首 token
  SLA。生产侧仍应按同一方法在 bot 环境用真实问法复测，并记录样本。该复测属于
  Prod 的观察项，不改变本 PR 的配置/请求接线。

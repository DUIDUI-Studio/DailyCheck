"""scripts.model_eval —— 派单 token / 成本账本采集器（Issue #91）。

数据流：
    dispatch.jsonl ──┐
    Claude transcripts ├─→ collect_pure（join / 求和 / 计价）─→ collect（落盘）─→ ledger.jsonl
    Codex rollouts ──┘
    pricing.json ────┘

入口：``python3 -m scripts.model_eval collect``。纯函数层见 ``collect_pure``。
"""

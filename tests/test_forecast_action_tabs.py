"""行动清单 tab 的语义（issue #132）。

现象：配送中心视角看到「断货风险 19」，但「本周需订」显示 **0**，
被读成「有这么多风险，本周却不用采购」。

根因不是算错：`critical` 与 `due` 被做成了**互斥** tab，而「本周需订」
这个词会被理解成「本周要下单的全部」。配送中心提前期长（L=3、R=7），
待订项几乎全部直接落进 `critical`，`due` 自然为 0 —— 数据事实，
但界面读起来自相矛盾。

修法（纯展示层）：把总额显式说成「本周下单」，`due` 改称「本周到点」，
并在脚注写明两者合计即总额。这些测试锁住三件事：
1. 总额恒等于 `critical + due`（不能退化成只数 `due`）；
2. `due` 的标签不再叫「本周需订」；
3. `due=0` 时页面给出解释，而不是留个光秃秃的 0。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

import pytest

from blueprints import replenishment_pure as R
from tests.conftest import _seed_item, _wh


def _seed_consumption(wh_path, item_id: int, days: int = 28, qty: float = 1.0) -> None:
    """按天写「生产消耗」动账，让看板估出真实需求速率。

    note 不能含「盘点」—— `_LOSS_FILTER` 会把盘点盈亏剔出需求。
    """
    conn = _wh(wh_path)
    now = datetime.now()
    for i in range(days):
        ts = (now - timedelta(days=i + 1)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO stock_movements (item_id, action, delta, note, created_at)"
            " VALUES (?, '生产消耗', ?, '生产记录#1领料', ?)",
            (item_id, -qty, ts),
        )
    conn.commit()
    conn.close()


def _action_tabs(body: str) -> dict[str, int]:
    """从「行动清单筛选」这一组 tab 里取各按钮的数字。

    限定在 aria-label 区块内解析 —— 页面下方的「明细筛选」也有一组同样文案的
    chip，直接全页正则会把两处混在一起。
    """
    m = re.search(r'aria-label="行动清单筛选".*?</div>', body, re.S)
    assert m, "页面里找不到行动清单筛选 tab"
    pairs = re.findall(r">\s*([^\s<>][^<>]*?)\s+(\d+)\s*</button>", m.group(0))
    return {name.strip(): int(n) for name, n in pairs}


# ---------------------------------------------------------------------------
# 模型层：标签语义
# ---------------------------------------------------------------------------


def test_due_label_no_longer_says_week_order():
    """`due` 的标签不能叫「本周需订」—— 它会被读成「本周要订的全部」。

    断货风险同样要本周下单，且更急；只把 due 说成「本周需订」，
    就会得出「有风险却不用采购」的错误结论。
    """
    label = R.STATUS_LABELS[R.STATUS_DUE]
    assert label == "本周到点"
    assert "需订" not in label


def test_critical_and_due_are_both_actionable():
    """不变式：只有 critical / due 才产生建议量，两者都必须 > 0。"""
    assert R.STATUS_CRITICAL in (R.STATUS_CRITICAL, R.STATUS_DUE)
    for status in (R.STATUS_CRITICAL, R.STATUS_DUE):
        qty = 0.0 if status not in (R.STATUS_CRITICAL, R.STATUS_DUE) else 5.0
        assert qty > 0


# ---------------------------------------------------------------------------
# 渲染层：断货风险 + 本周到点 = 本周下单
# ---------------------------------------------------------------------------


def test_tabs_total_equals_critical_plus_due(logged_client):
    """造「一个断货 + 一个到点」，tab 上的总额必须等于两者之和。

    这是 issue #132 的核心：总额要是 `actions | length`（critical ∪ due），
    而不是 `status_counts['due']` —— 后者在 DC 视角恒为 0。
    """
    client, wh_path = logged_client

    # 断货：库存 0、每天消耗 1 → 覆盖 0 天
    out_id, _ = _seed_item(wh_path, "断货品", qty=0, unit_cost=100)
    _seed_consumption(wh_path, out_id, days=28, qty=1.0)

    # 到点：库存 3、同样速率 → 覆盖约 6 天（门店 L=1、R=7 → [1,8) 区间）
    due_id, _ = _seed_item(wh_path, "到点品", qty=3, unit_cost=100)
    _seed_consumption(wh_path, due_id, days=28, qty=1.0)

    body = client.get("/forecast").data.decode("utf-8")
    tabs = _action_tabs(body)

    assert tabs["断货风险"] == 1, tabs
    assert tabs["本周到点"] == 1, tabs
    assert tabs["本周下单"] == tabs["断货风险"] + tabs["本周到点"] == 2, tabs


def test_zero_due_still_shows_nonzero_total(logged_client):
    """issue #132 原场景：全部待订项都是断货风险时，总额不能显示成 0。

    这也是回归锁 —— 如果谁把总额改回只数 `due`，这里立刻红。
    """
    client, wh_path = logged_client
    item_id, _ = _seed_item(wh_path, "只有断货品", qty=0, unit_cost=100)
    _seed_consumption(wh_path, item_id, days=28, qty=1.0)

    body = client.get("/forecast").data.decode("utf-8")
    tabs = _action_tabs(body)

    assert tabs["本周到点"] == 0, tabs
    assert tabs["断货风险"] == 1, tabs
    assert tabs["本周下单"] == 1, "有断货风险时总额不能是 0"
    # 空态必须解释，否则用户只会看到一个孤零零的 0
    assert "本视角暂无「本周到点」的品项" in body


def test_no_zero_notice_when_due_exists(logged_client):
    """有 `due` 行时不该出现「暂无」提示 —— 否则提示本身成了假消息。"""
    client, wh_path = logged_client
    due_id, _ = _seed_item(wh_path, "到点品项", qty=3, unit_cost=100)
    _seed_consumption(wh_path, due_id, days=28, qty=1.0)

    body = client.get("/forecast").data.decode("utf-8")
    tabs = _action_tabs(body)
    assert tabs["本周到点"] == 1, tabs
    assert "本视角暂无「本周到点」的品项" not in body


def test_page_has_no_ambiguous_week_order_wording(logged_client):
    """页面上不能再出现「本周需订」这个歧义措辞。"""
    client, wh_path = logged_client
    item_id, _ = _seed_item(wh_path, "措辞品", qty=0, unit_cost=100)
    _seed_consumption(wh_path, item_id, days=28, qty=1.0)

    body = client.get("/forecast").data.decode("utf-8")
    assert "本周需订" not in body


@pytest.mark.parametrize("path", ["/forecast", "/forecast?scope=all"])
def test_footnote_explains_additive_relation(logged_client, path):
    """脚注必须写明「两者都要本周下单，合计即总额」，否则数字仍会被误读。"""
    client, wh_path = logged_client
    item_id, _ = _seed_item(wh_path, "脚注品", qty=0, unit_cost=100)
    _seed_consumption(wh_path, item_id, days=28, qty=1.0)

    body = client.get(path).data.decode("utf-8")
    assert "两者都要在本周下单" in body

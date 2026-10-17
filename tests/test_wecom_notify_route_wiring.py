"""提交点接线测试：``submit_order_route`` → ``_notify_wecom_new_order`` 这条边。

为什么单独一份：``tests/test_wecom_notify_hook.py`` 只直接调
``so._notify_wecom_new_order(...)``，**不经过路由**——把
``blueprints/store_ordering.py`` 里挂钩那一行删掉，那 4 个用例仍然全绿，
功能全死而 CI 不红（QA 实测 48 用例全绿）。所以这里必须从 HTTP 入口进：

1. 正常提交 → 通知函数**恰好调用 1 次**，参数（计数 / ``background``）正确；
2. 通知发生时用**独立 sqlite 连接**能读到该 ``order_no`` 且 status=pending
   ——证明 ``submit_order`` 的 commit 在通知之前（不是「先发消息后落库」）；
3. 被业务校验拦下的提交（门店未绑定品项）→ 不触发通知，避免群消息刷屏；
4. 通知函数抛异常 → 下单仍成功返回 200 且订单落库（外部通道隔离）。

所有用例都 monkeypatch ``wecom_notify_pure.notify_store_order_submitted``
（挂钩层唯一的调用出口），因此**零网络**、不需要真实 webhook env。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from blueprints import wecom_notify_pure as wn
from tests import test_store_ordering_store_route as _store_route_module

# 复用现成夹具，不复制定义。直接 ``from ... import store_env`` 会让 import 名
# 与测试函数参数同名 → ruff 判 F811（redefinition）；这里用模块属性赋值把它
# 注册进本模块命名空间，pytest 按属性名收集夹具，行为等价于本模块自有夹具。
store_env = _store_route_module.store_env


def _add_item(client, canonical_id: int = 101, quantity: str = "5") -> None:
    client.post(
        "/store-ordering/cart/add",
        data={"dc": "dc_test", "canonical_id": canonical_id, "quantity": quantity, "unit": "件"},
    )


def _submit(client, note: str = ""):
    tomorrow = (datetime.now().date() + timedelta(days=1)).isoformat()
    return client.post(
        "/store-ordering/cart/submit",
        data={"expected_delivery_date": tomorrow, "note": note},
        follow_redirects=True,
    )


def _fetch_order_by_no(master_path, order_no: str):
    """独立连接读库（不复用 app 的连接，能读到即说明已 commit 落盘）。"""
    conn = sqlite3.connect(str(master_path))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT id, order_no, status, store_warehouse_code FROM store_orders WHERE order_no=?",
            (order_no,),
        ).fetchone()
    finally:
        conn.close()


def _count_orders(master_path) -> int:
    conn = sqlite3.connect(str(master_path))
    try:
        return int(conn.execute("SELECT COUNT(*) FROM store_orders").fetchone()[0])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. 正常提交：恰好一次 + 参数正确
# ---------------------------------------------------------------------------

def test_submit_route_calls_notify_exactly_once(store_env, monkeypatch):
    """提交成功后必须恰好调用一次通知，且入参计数由行项目推导。"""
    calls: list[dict] = []

    def _record(order, item_count=None, total_qty=None, background=True):
        calls.append({
            "order": order,
            "item_count": item_count,
            "total_qty": total_qty,
            "background": background,
        })

    monkeypatch.setattr(wn, "notify_store_order_submitted", _record)

    _add_item(store_env["client"], canonical_id=101, quantity="5")
    resp = _submit(store_env["client"], note="接线测试")

    assert resp.status_code == 200
    assert "提交成功" in resp.data.decode()
    assert len(calls) == 1, f"挂钩应恰好调用 1 次，实际 {len(calls)} 次"
    hit = calls[0]
    assert hit["order"]["order_no"].startswith("SO-")
    assert hit["item_count"] == 1
    assert hit["total_qty"] == 5.0
    assert hit["background"] is True, "必须走后台线程，不得阻塞下单请求"
    assert hit["order"].get("order_items"), "行项目未带回，模板会丢掉「品项」行"


# ---------------------------------------------------------------------------
# 2. commit 先于通知（独立连接可见）
# ---------------------------------------------------------------------------

def test_notify_runs_after_commit_visible_on_independent_connection(store_env, monkeypatch):
    """通知被调用的那一刻，订单必须已经 commit（独立连接查得到）。"""
    observed: dict = {}

    def _probe(order, item_count=None, total_qty=None, background=True):
        row = _fetch_order_by_no(store_env["master_path"], order.get("order_no"))
        observed["row"] = row
        observed["order_no"] = order.get("order_no")

    monkeypatch.setattr(wn, "notify_store_order_submitted", _probe)

    _add_item(store_env["client"], canonical_id=101, quantity="3")
    assert _submit(store_env["client"]).status_code == 200

    assert observed.get("row") is not None, (
        f"通知发生时库里查不到 {observed.get('order_no')}——说明 commit 在通知之后"
    )
    assert observed["row"]["status"] == "pending"
    assert observed["row"]["store_warehouse_code"] == "store_test"


# ---------------------------------------------------------------------------
# 3. 被拦截的提交不触发通知
# ---------------------------------------------------------------------------

def test_rejected_submit_does_not_notify(store_env, monkeypatch):
    """门店未绑定该品项 → 提交被拦 → 不得对外推送（否则群消息刷屏）。"""
    calls: list = []
    monkeypatch.setattr(
        wn, "notify_store_order_submitted", lambda *a, **k: calls.append((a, k)),
    )

    # canonical 102 在门店库里故意没有绑定（见 store_env 夹具）
    _add_item(store_env["client"], canonical_id=102, quantity="1")
    resp = _submit(store_env["client"])

    assert resp.status_code == 200
    assert "提交失败" in resp.data.decode()
    assert calls == [], "提交失败仍触发了对外推送"
    assert _count_orders(store_env["master_path"]) == 0


# ---------------------------------------------------------------------------
# 4. 通知炸掉不影响下单
# ---------------------------------------------------------------------------

def test_order_persists_when_notify_raises(store_env, monkeypatch):
    """外部通道异常不得回滚、不得改变响应——订单已落库就必须留得住。"""

    def _boom(*_args, **_kwargs):
        raise RuntimeError("wecom transport exploded")

    monkeypatch.setattr(wn, "notify_store_order_submitted", _boom)

    _add_item(store_env["client"], canonical_id=101, quantity="2")
    resp = _submit(store_env["client"])

    assert resp.status_code == 200
    assert "提交成功" in resp.data.decode()

    conn = sqlite3.connect(str(store_env["master_path"]))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT order_no, status FROM store_orders ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "通知异常导致订单没落库（事务被回滚）"
    assert row["status"] == "pending"
    assert row["order_no"].startswith("SO-")

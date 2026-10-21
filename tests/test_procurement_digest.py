"""周度采购建议邮件 —— 取数 / 投递 / 调度单测（Issue #145）。

分三层：
  1. **取数**：真实临时 sqlite（master + DC + 两家门店）验证「各门店库存」
     与「近 7 天消耗」的聚合口径 —— 盘点盈亏必须剔除，回退必须扣减。
  2. **装配**：`collect_digest` 的跨层拼接与告警（DC 缺失 / 未绑主数据 / 坏仓）。
  3. **投递与去重**：未配置不发、成功只发一次、失败可重试、上限后放弃、
     手动 force 可补发。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta

import pytest

from blueprints import procurement_digest as D
from blueprints.replenishment_pure import STATUS_CRITICAL, STATUS_DUE

# ---------------------------------------------------------------------------
# 环境：master + DC + 两家门店
# ---------------------------------------------------------------------------


def _insert_item(conn, *, sku, name, canonical_id, qty, unit_cost=10.0):
    cat = conn.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()[0]
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        "INSERT INTO items (sku, name, category_id, quantity, safety_stock, unit_cost, "
        "unit, canonical_id, updated_at) VALUES (?, ?, ?, ?, 0, ?, '箱', ?, ?)",
        (sku, name, cat, qty, unit_cost, canonical_id, ts),
    )
    return cur.lastrowid


def _insert_move(conn, item_id, action, delta, days_ago, note=""):
    ts = (date.today() - timedelta(days=days_ago)).strftime("%Y-%m-%d 10:00:00")
    conn.execute(
        "INSERT INTO stock_movements (item_id, action, delta, note, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (item_id, action, delta, note, ts),
    )


@pytest.fixture
def multi_warehouse(tmp_path, monkeypatch):
    """建 master + DC(wh_dc) + 门店 s1/s2，并共享同一个 canonical_id=101 的品项。"""
    import config as config_module
    import db as db_module
    from db import init_master_db, init_warehouse_db

    master_path = tmp_path / "master.db"
    wh_dir = tmp_path / "warehouses"
    wh_dir.mkdir()
    dc_path = wh_dir / "dc.db"
    s1_path = wh_dir / "s1.db"
    s2_path = wh_dir / "s2.db"

    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_dir)
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", wh_dir)

    init_master_db()
    for path in (dc_path, s1_path, s2_path):
        init_warehouse_db(path)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    m = sqlite3.connect(master_path)
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) "
        "VALUES (1, 'wh_dc', '中央配送中心', ?, 'distribution_center', ?)",
        (str(dc_path), ts),
    )
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) "
        "VALUES (2, 's1', '一号店', ?, 'storefront', ?)",
        (str(s1_path), ts),
    )
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) "
        "VALUES (3, 's2', '二号店', ?, 'storefront', ?)",
        (str(s2_path), ts),
    )
    m.commit()
    m.close()

    return {"master": master_path, "dc": dc_path, "s1": s1_path, "s2": s2_path}


def test_store_stock_and_consumption_aggregates_and_excludes_stocktake(
    multi_warehouse,
):
    from blueprints.replenishment_data import store_stock_and_consumption

    s1 = sqlite3.connect(multi_warehouse["s1"])
    item_s1 = _insert_item(s1, sku="A1", name="番茄酱", canonical_id=101, qty=5.0)
    # 近 7 天真实出库 20；盘点盘亏 300 不属于需求；一条回退 2 要扣回。
    _insert_move(s1, item_s1, "出库", -12.0, 2)
    _insert_move(s1, item_s1, "生产消耗", -8.0, 5)
    _insert_move(s1, item_s1, "出库", -300.0, 3, note="盘点审核#9盘亏 (req#1)")
    _insert_move(s1, item_s1, "出库回退", 2.0, 1)
    # 窗口外（8 天前）不该计入
    _insert_move(s1, item_s1, "出库", -50.0, 8)
    s1.commit()
    s1.close()

    s2 = sqlite3.connect(multi_warehouse["s2"])
    item_s2 = _insert_item(s2, sku="B1", name="番茄酱", canonical_id=101, qty=2.0)
    _insert_move(s2, item_s2, "出库", -6.0, 1)
    s2.commit()
    s2.close()

    stock, consumed, stores = store_stock_and_consumption(days=7)

    assert [s["code"] for s in stores] == ["s1", "s2"]
    assert stock[101] == {"s1": 5.0, "s2": 2.0}
    # 12 + 8 - 2 + 6 = 24
    assert consumed[101] == pytest.approx(24.0)


def test_store_stock_and_consumption_clamps_negative_to_zero(multi_warehouse):
    from blueprints.replenishment_data import store_stock_and_consumption

    s1 = sqlite3.connect(multi_warehouse["s1"])
    item = _insert_item(s1, sku="A1", name="番茄酱", canonical_id=101, qty=1.0)
    _insert_move(s1, item, "出库回退", 9.0, 1)  # 只有回退没有消耗
    s1.commit()
    s1.close()

    _stock, consumed, _stores = store_stock_and_consumption(days=7)
    assert consumed[101] == 0.0


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def _set_mail_config(monkeypatch, **overrides):
    import config as config_module

    values = {
        "DIGEST_MAIL_HOST": "smtp.exmail.qq.com",
        "DIGEST_MAIL_PORT": 465,
        "DIGEST_MAIL_USER": "ops@corp.example",
        "DIGEST_MAIL_PASSWORD": "secret",
        "DIGEST_MAIL_FROM": "ops@corp.example",
        "DIGEST_MAIL_TO": "owner@corp.example",
        "DIGEST_WEEKDAY": 1,
        "DIGEST_HOUR": 8,
    }
    values.update(overrides)
    for key, value in values.items():
        monkeypatch.setattr(config_module, key, value, raising=False)


def test_settings_disabled_without_credentials(monkeypatch):
    _set_mail_config(monkeypatch, DIGEST_MAIL_TO="", DIGEST_MAIL_PASSWORD="")
    settings = D.digest_settings()
    assert settings["enabled"] is False


def test_settings_split_multiple_recipients(monkeypatch):
    _set_mail_config(monkeypatch, DIGEST_MAIL_TO="a@x.com, b@x.com;c@x.com")
    settings = D.digest_settings()
    assert settings["enabled"] is True
    assert settings["recipients"] == ["a@x.com", "b@x.com", "c@x.com"]


def test_settings_tolerates_bad_numbers(monkeypatch):
    _set_mail_config(monkeypatch, DIGEST_MAIL_PORT="", DIGEST_WEEKDAY="x", DIGEST_HOUR=99)
    settings = D.digest_settings()
    assert settings["port"] == 465
    assert settings["weekday"] == 1 % 7
    assert settings["hour"] == 23


def test_mask_address_keeps_domain_only():
    assert D.mask_address("zhangsan@corp.com") == "z***@corp.com"
    assert D.mask_address("no-at-sign") == "***"


def test_is_due_only_on_configured_weekday_and_hour(monkeypatch):
    _set_mail_config(monkeypatch)
    settings = D.digest_settings()
    assert D.is_due(datetime(2026, 10, 27, 8, 0), settings) is True   # 周二 08:00
    assert D.is_due(datetime(2026, 10, 27, 9, 0), settings) is False  # 小时不对
    assert D.is_due(datetime(2026, 10, 26, 8, 0), settings) is False  # 周一


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------


def _fake_board_rows():
    base = {
        "unit": "箱",
        "category": "调味酱",
        "abc": "A",
        "on_order": 0.0,
        "cover_days": 1.5,
        "canonical_id": 101,
        "suggested_value": 120.0,
    }
    return [
        {**base, "name": "番茄酱", "sku": "S1", "quantity": 3.0,
         "suggested_qty": 12.0, "status": STATUS_CRITICAL, "orderable": True,
         "warehouse_type": "distribution_center", "warehouse_code": "wh_dc"},
        {**base, "name": "门店行", "sku": "S2", "quantity": 1.0,
         "suggested_qty": 4.0, "status": STATUS_DUE, "orderable": True,
         "warehouse_type": "storefront", "warehouse_code": "s1"},
    ]


def test_collect_digest_joins_store_data(monkeypatch):
    from blueprints import replenishment_data

    monkeypatch.setattr(
        replenishment_data,
        "build_board",
        lambda **kwargs: {
            "rows": _fake_board_rows(),
            "warehouses": [
                {"code": "wh_dc", "name": "中央配送中心", "is_dc": True},
                {"code": "s1", "name": "一号店", "is_dc": False},
            ],
            "data_quality": {"skipped_warehouses": [], "failed_warehouses": [],
                             "excluded_warehouses": []},
        },
    )
    monkeypatch.setattr(
        replenishment_data,
        "store_stock_and_consumption",
        lambda days=7, today=None: ({101: {"s1": 5.0}}, {101: 28.0},
                                    [{"code": "s1", "name": "一号店"}]),
    )
    monkeypatch.setattr(replenishment_data, "load_params", lambda: None)

    digest = D.collect_digest(today=date(2026, 10, 27))

    assert digest["dc_name"] == "中央配送中心"
    assert digest["item_count"] == 1          # 门店行被剔掉
    item = digest["items"][0]
    assert item["name"] == "番茄酱"
    assert item["consumption_7d"] == 28.0
    assert item["stores"] == [{"name": "一号店", "qty": 5.0}]
    assert digest["dc_configured"] is True


def test_collect_digest_warns_when_dc_missing(monkeypatch):
    from blueprints import replenishment_data

    monkeypatch.setattr(
        replenishment_data,
        "build_board",
        lambda **kwargs: {"rows": _fake_board_rows(),
                          "warehouses": [{"code": "s1", "name": "一号店", "is_dc": False}],
                          "data_quality": {}},
    )
    monkeypatch.setattr(
        replenishment_data,
        "store_stock_and_consumption",
        lambda days=7, today=None: ({}, {}, []),
    )
    monkeypatch.setattr(replenishment_data, "load_params", lambda: None)

    digest = D.collect_digest(today=date(2026, 10, 27))
    assert digest["dc_configured"] is False
    joined = " ".join(digest["warnings"])
    assert "未找到配送中心仓" in joined
    assert "没有可用的门店库" in joined


# ---------------------------------------------------------------------------
# 投递与去重
# ---------------------------------------------------------------------------


@pytest.fixture
def sendable(multi_warehouse, monkeypatch):
    """已配置好邮件 + 装配结果固定 + 不真发信的 send_digest 环境。"""
    _set_mail_config(monkeypatch)
    sent: list[dict] = []

    def _fake_send_mail(subject, text, html, settings, timeout=15.0):
        sent.append({"subject": subject, "text": text, "html": html,
                     "to": settings["recipients"]})

    monkeypatch.setattr(D, "send_mail", _fake_send_mail)
    monkeypatch.setattr(
        D,
        "collect_digest",
        lambda today=None, limit=None: {
            "items": [], "item_count": 0, "truncated": 0, "total_value": 0.0,
            "day": today or date.today(), "date_label": "2026-10-27（周二）",
            "generated_at": "2026-10-27 08:00", "dc_name": "中央配送中心",
            "window_days": 7, "stores": [], "warnings": [], "dc_configured": True,
        },
    )
    return {"sent": sent, "day": date(2026, 10, 27)}


def test_send_digest_requires_configuration(multi_warehouse, monkeypatch):
    _set_mail_config(monkeypatch, DIGEST_MAIL_USER="", DIGEST_MAIL_PASSWORD="")
    result = D.send_digest(today=date(2026, 10, 27))
    assert result == {"sent": False, "reason": "not_configured"}


def test_send_digest_sends_once_then_skips(sendable):
    day = sendable["day"]
    first = D.send_digest(today=day)
    assert first["sent"] is True
    assert len(sendable["sent"]) == 1

    second = D.send_digest(today=day)
    assert second["sent"] is False
    assert second["reason"] == "already_sent"
    assert len(sendable["sent"]) == 1


def test_send_digest_force_bypasses_sentinel(sendable):
    day = sendable["day"]
    D.send_digest(today=day)
    forced = D.send_digest(today=day, force=True)
    assert forced["sent"] is True
    assert len(sendable["sent"]) == 2


def test_send_digest_failure_is_retryable_then_gives_up(sendable, monkeypatch):
    day = sendable["day"]

    def _boom(*args, **kwargs):
        raise OSError("smtp down")

    monkeypatch.setattr(D, "send_mail", _boom)

    for _attempt in range(1, D.MAX_SEND_ATTEMPTS + 1):
        result = D.send_digest(today=day)
        assert result["sent"] is False
        assert result["reason"] == "send_failed"

    sentinel = json.loads(D._sentinel_path(day).read_text())
    assert sentinel["status"] == "failed"
    assert sentinel["attempts"] == D.MAX_SEND_ATTEMPTS

    # 用尽后不再重试（避免把失败变成垃圾邮件循环）
    exhausted = D.send_digest(today=day)
    assert exhausted["reason"] == "attempts_exhausted"


def test_send_digest_build_failure_does_not_raise(sendable, monkeypatch):
    def _boom(**kwargs):
        raise RuntimeError("db gone")

    monkeypatch.setattr(D, "collect_digest", _boom)
    result = D.send_digest(today=sendable["day"])
    assert result["sent"] is False
    assert result["reason"] == "build_failed"
    assert sendable["sent"] == []


def test_send_digest_marks_pending_while_sending(sendable, monkeypatch):
    """发送中途被杀会留下 pending；5 分钟内其它 worker 不得重复发。"""
    day = sendable["day"]
    path = D._sentinel_path(day)
    path.write_text(json.dumps({
        "status": "pending",
        "claimed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "attempts": 0,
    }))
    result = D.send_digest(today=day)
    assert result["reason"] == "in_progress"
    assert sendable["sent"] == []


def test_send_digest_masks_recipients_in_result(sendable):
    result = D.send_digest(today=sendable["day"])
    assert result["recipients"] == ["o***@corp.example"]
    assert "secret" not in json.dumps(result)

"""Rate limit（P0-4）测试：滑动窗口 / 登录锁定 / 全局钩子 / MCP 配额判定。"""
from datetime import datetime, timedelta

import pytest
from werkzeug.security import generate_password_hash

from rate_limit_pure import (
    LOGIN_MAX_FAILURES,
    MCP_DEFAULT_RATE_LIMIT,
    SlidingWindowLimiter,
    login_lock_state,
    mcp_quota_limit,
    next_failure_state,
)

BASE = datetime(2026, 10, 10, 12, 0, 0)
FMT = "%Y-%m-%d %H:%M:%S"


# --- SlidingWindowLimiter ----------------------------------------------------

def test_limiter_allows_up_to_max_then_blocks():
    lim = SlidingWindowLimiter(max_events=3, window_seconds=60)
    now = 1000.0
    assert lim.hit("k", now=now)
    assert lim.hit("k", now=now + 1)
    assert lim.hit("k", now=now + 2)
    assert not lim.hit("k", now=now + 3)


def test_limiter_window_expiry_frees_slots():
    lim = SlidingWindowLimiter(max_events=2, window_seconds=60)
    now = 1000.0
    assert lim.hit("k", now=now)
    assert lim.hit("k", now=now + 1)
    assert not lim.hit("k", now=now + 30)      # 窗口内仍满
    assert lim.hit("k", now=now + 61)          # 旧事件出窗，腾出位置
    assert lim.event_count("k", now=now + 61) == 1


def test_limiter_keys_are_independent():
    lim = SlidingWindowLimiter(max_events=1, window_seconds=60)
    assert lim.hit("a", now=1000.0)
    assert not lim.hit("a", now=1001.0)
    assert lim.hit("b", now=1002.0)


# --- 登录锁定纯函数 ----------------------------------------------------------

def test_login_lock_state_not_locked_when_no_lock():
    locked, remaining = login_lock_state(3, None, now=BASE)
    assert not locked and remaining == 0


def test_login_lock_state_locked_with_remaining():
    until = (BASE + timedelta(minutes=10)).strftime(FMT)
    locked, remaining = login_lock_state(5, until, now=BASE)
    assert locked and 590 <= remaining <= 600


def test_login_lock_state_expired_unlocks():
    until = (BASE - timedelta(minutes=1)).strftime(FMT)
    locked, _ = login_lock_state(5, until, now=BASE)
    assert not locked


def test_next_failure_counts_within_window():
    last = (BASE - timedelta(minutes=5)).strftime(FMT)
    count, locked_until = next_failure_state(2, last, now=BASE)
    assert count == 3 and locked_until is None


def test_next_failure_resets_after_window():
    last = (BASE - timedelta(minutes=20)).strftime(FMT)
    count, locked_until = next_failure_state(4, last, now=BASE)
    assert count == 1 and locked_until is None


def test_next_failure_locks_at_threshold():
    last = (BASE - timedelta(minutes=1)).strftime(FMT)
    count, locked_until = next_failure_state(LOGIN_MAX_FAILURES - 1, last, now=BASE)
    assert count == LOGIN_MAX_FAILURES
    assert locked_until is not None
    locked, remaining = login_lock_state(count, locked_until, now=BASE)
    assert locked and remaining > 0


# --- MCP 配额判定 ------------------------------------------------------------

def test_mcp_quota_none_uses_default():
    assert mcp_quota_limit(None) == MCP_DEFAULT_RATE_LIMIT


def test_mcp_quota_explicit_value():
    assert mcp_quota_limit(30) == 30


def test_mcp_quota_invalid_falls_back():
    assert mcp_quota_limit(0) == MCP_DEFAULT_RATE_LIMIT
    assert mcp_quota_limit(-5) == MCP_DEFAULT_RATE_LIMIT
    assert mcp_quota_limit("abc") == MCP_DEFAULT_RATE_LIMIT  # type: ignore[arg-type]


# --- Web 集成：全局钩子 + 登录锁定 -------------------------------------------

@pytest.fixture
def rate_client(logged_client):
    """开启全局限速的登录态客户端（限额 3/分钟，便于触发）。"""
    from rate_limit_pure import SlidingWindowLimiter

    client, wh_path = logged_client
    client.application.config["RATE_LIMIT_FORCE_ENABLED"] = True
    client.application.extensions["rate_limiter"] = SlidingWindowLimiter(3, 60)
    return client, wh_path


def test_global_rate_limit_blocks_after_threshold(rate_client):
    client, _ = rate_client
    codes = [client.get("/select-warehouse").status_code for _ in range(4)]
    assert codes[:3] == [200, 200, 200]
    assert codes[3] == 429


def test_global_rate_limit_disabled_in_testing_default(logged_client):
    client, _ = logged_client
    for _ in range(5):
        assert client.get("/select-warehouse").status_code == 200


def test_login_lockout_after_repeated_failures(logged_client):
    """连错 5 次后，正确密码也被锁定（429）。"""
    client, _ = logged_client
    # 建一个可验证密码的用户（conftest 的 hash='x' 无法通过校验）。
    import sqlite3

    # master 路径用 conftest patch 过的 config.MASTER_DB。
    import config as config_module

    master_path = str(config_module.MASTER_DB)
    m = sqlite3.connect(master_path)
    m.execute(
        "INSERT INTO users (username, password_hash, is_admin, created_at) "
        "VALUES ('lockme', ?, 0, '2026-10-10 00:00:00')",
        (generate_password_hash("right-password"),),
    )
    m.commit()
    m.close()

    for _ in range(LOGIN_MAX_FAILURES):
        r = client.post(
            "/login",
            data={"username": "lockme", "password": "wrong"},
        )
        assert r.status_code == 401
    r = client.post(
        "/login", data={"username": "lockme", "password": "right-password"}
    )
    assert r.status_code == 429

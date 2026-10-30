"""Issue 每日缓存的回归网（Issue #98）。

四条口径必须锁死（Owner 2026-10-30 降频要求）：

  1. 缓存新鲜 → 直接用缓存，**不发起 Gitea 调用**
  2. 缓存过期 → 重新拉取并回写
  3. 拉取失败但缓存还在 → 回退旧缓存（不显示假零），由渲染层标注「陈旧缓存」
  4. 拉取失败且无缓存 → 抛 FetchError，走调用方既有降级路径
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.dailycheck_dashboard import fetchers, issues_cache, render


def _iso(hours_from_now: float = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours_from_now)).isoformat(
        timespec="seconds"
    )


def _issue(number: int) -> dict:
    return {
        "number": number,
        "title": f"issue {number}",
        "state": "OPEN",
        "created_at": "2026-10-17",
        "closed_at": None,
        "labels": [],
    }


def _write_cache(path: Path, fetched_at: str, issues: list[dict]) -> None:
    path.write_text(
        json.dumps({"fetched_at": fetched_at, "issues": issues}), encoding="utf-8"
    )


def _no_network(*args, **kwargs):
    raise AssertionError("缓存命中时不得发起 Gitea 调用")


def _fetch_fails(*args, **kwargs):
    raise fetchers.FetchError("Gitea API unreachable")


# ============================================================
# 1. 命中不发起网络调用
# ============================================================
def test_fresh_cache_hit_skips_network(tmp_path, monkeypatch):
    cache_path = tmp_path / "issues_cache.json"
    _write_cache(cache_path, _iso(), [_issue(98)])
    monkeypatch.setattr(fetchers, "fetch_issues", _no_network)

    issues, meta = issues_cache.load_or_fetch_issues(cache_path)

    assert [i["number"] for i in issues] == [98]
    assert meta["source"] == issues_cache.SOURCE_CACHE


def test_ttl_parameter_controls_freshness(tmp_path, monkeypatch):
    """ttl 收参数：1 小时前的缓存对 2h TTL 新鲜，对 60s TTL 已过期。"""
    cache_path = tmp_path / "issues_cache.json"
    _write_cache(cache_path, _iso(-1), [_issue(98)])

    monkeypatch.setattr(fetchers, "fetch_issues", _no_network)
    issues, meta = issues_cache.load_or_fetch_issues(cache_path, ttl=2 * 3600)
    assert meta["source"] == issues_cache.SOURCE_CACHE
    assert issues[0]["number"] == 98

    monkeypatch.setattr(fetchers, "fetch_issues", lambda: [_issue(99)])
    issues, meta = issues_cache.load_or_fetch_issues(cache_path, ttl=60)
    assert meta["source"] == issues_cache.SOURCE_FRESH
    assert issues[0]["number"] == 99


# ============================================================
# 2. 过期重拉并回写
# ============================================================
def test_expired_cache_refetches_and_rewrites(tmp_path, monkeypatch):
    cache_path = tmp_path / "issues_cache.json"
    stale_at = _iso(-25)
    _write_cache(cache_path, stale_at, [_issue(97)])
    monkeypatch.setattr(fetchers, "fetch_issues", lambda: [_issue(98), _issue(99)])

    issues, meta = issues_cache.load_or_fetch_issues(cache_path)

    assert [i["number"] for i in issues] == [98, 99]
    assert meta["source"] == issues_cache.SOURCE_FRESH
    written = json.loads(cache_path.read_text(encoding="utf-8"))
    assert [i["number"] for i in written["issues"]] == [98, 99]
    assert written["fetched_at"] == meta["fetched_at"]
    assert written["fetched_at"] != stale_at


def test_cache_write_failure_keeps_rendering_and_reports_error(tmp_path, monkeypatch):
    cache_path = tmp_path / "issues_cache.json"
    monkeypatch.setattr(fetchers, "fetch_issues", lambda: [_issue(98)])

    def boom(*args, **kwargs):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(issues_cache, "_write_cache", boom)

    issues, meta = issues_cache.load_or_fetch_issues(cache_path)

    assert issues[0]["number"] == 98
    assert meta["source"] == issues_cache.SOURCE_FRESH
    assert "read-only filesystem" in meta["cache_error"]


# ============================================================
# 3. 拉取失败回退旧缓存（不显示假零）
# ============================================================
def test_fetch_failure_falls_back_to_stale_cache(tmp_path, monkeypatch):
    cache_path = tmp_path / "issues_cache.json"
    stale_at = _iso(-30)
    _write_cache(cache_path, stale_at, [_issue(95), _issue(96)])
    monkeypatch.setattr(fetchers, "fetch_issues", _fetch_fails)

    issues, meta = issues_cache.load_or_fetch_issues(cache_path)

    assert [i["number"] for i in issues] == [95, 96]
    assert meta["source"] == issues_cache.SOURCE_STALE
    assert meta["fetched_at"] == stale_at


# ============================================================
# 4. 拉取失败且无缓存 → 既有降级路径
# ============================================================
def test_fetch_failure_without_cache_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(fetchers, "fetch_issues", _fetch_fails)

    with pytest.raises(fetchers.FetchError):
        issues_cache.load_or_fetch_issues(tmp_path / "issues_cache.json")


def test_corrupt_cache_is_treated_as_missing(tmp_path, monkeypatch):
    cache_path = tmp_path / "issues_cache.json"
    cache_path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(fetchers, "fetch_issues", _fetch_fails)

    with pytest.raises(fetchers.FetchError):
        issues_cache.load_or_fetch_issues(cache_path)


# ============================================================
# 渲染层：陈旧标注 + 任务运行监控面板已移除
# ============================================================
def _render(**overrides) -> str:
    base = dict(
        created_at="2026-10-30T12:00:00+08:00",
        trigger_kind="manual",
        trigger_event_id=None,
        head_sha="a" * 40,
        origin_main_sha="b" * 40,
        summary={
            "open_issues": 1,
            "merged_7d_total": 0,
            "worktrees_total": 0,
            "closed_unmerged": 0,
        },
        issues=[_issue(98)],
    )
    base.update(overrides)
    return render.render(render.RenderInput(**base))


def test_render_marks_stale_issue_cache():
    html = _render(issues_stale_at="2026-10-29T08:00:00+00:00")

    assert "陈旧缓存 2026-10-29T08:00:00+00:00" in html
    assert "#98" in html


def test_render_omits_stale_note_when_cache_fresh():
    assert "陈旧缓存" not in _render()


def test_render_no_longer_contains_task_monitor_panel():
    html = _render()

    assert "任务运行监控" not in html
    assert "任务运行" not in html
    assert "Dashboard · 本次渲染" not in html
    assert 'id="monitor"' not in html

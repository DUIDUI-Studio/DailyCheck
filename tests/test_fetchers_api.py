"""fetchers 的 GitHub REST 适配层回归。

单独一个文件，因为这一层是「gh CLI → REST API」的替换点，四个事实必须锁死：

  1. **匿名可用**：生产上 `/root/.config/gh/hosts.yml` 认证失效正是这次故障的
     起点，gh CLI 没认证就彻底不可用；REST 匿名可读是本方案成立的前提。
  2. **state 语义映射**：REST 的 /pulls 只有 open/closed，「已合并」只体现在
     merged_at；下游 render / compute_summary 全按 OPEN/CLOSED/MERGED 三分类，
     映射错了整个 PR 区块会塌。
  3. **/issues 混入 PR**：GitHub 的 issue 端点把 PR 也当 issue 返回，不剔除就会
     重复计数。
  4. **限速错误要带余量**：不能让「拉不到」退化成「今日 0 个合并」。

不测真网络（CI 无法保证外网与配额）：全部走 monkeypatch。
"""
from __future__ import annotations

import time
import urllib.error
from email.message import Message

import pytest

from scripts.dailycheck_dashboard import fetchers


class _FakeResp:
    """urlopen 的返回值最小替身（context manager + read）。"""

    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _http_error(code: int, *, remaining: str = "0", reset_in: int = 120) -> urllib.error.HTTPError:
    headers = Message()
    headers["X-RateLimit-Remaining"] = remaining
    headers["X-RateLimit-Reset"] = str(int(time.time()) + reset_in)
    return urllib.error.HTTPError(
        "https://api.github.com/x", code, "err", headers, None  # type: ignore[arg-type]
    )


# ============================================================
# state 映射（下游三分类的地基）
# ============================================================
def test_pr_state_merged_wins_over_closed():
    """REST 里已合并的 PR state 仍是 closed —— 必须靠 merged_at 判 MERGED。"""
    assert fetchers._pr_state({"state": "closed", "merged_at": "2026-10-17T12:00:00Z"}) == "MERGED"


def test_pr_state_open_without_merge():
    assert fetchers._pr_state({"state": "open", "merged_at": None}) == "OPEN"


def test_pr_state_closed_unmerged():
    assert fetchers._pr_state({"state": "closed", "merged_at": None}) == "CLOSED"


# ============================================================
# 字段映射
# ============================================================
def test_fetch_prs_maps_rest_fields(monkeypatch):
    payload = [
        {
            "number": 105,
            "title": "fix: 标签",
            "state": "closed",
            "merged_at": "2026-10-17T14:57:47Z",
            "created_at": "2026-10-17T14:30:00Z",
            "head": {"ref": "fix/ops-kpi-label"},
        },
        {
            "number": 106,
            "title": "wip",
            "state": "open",
            "merged_at": None,
            "created_at": "2026-10-18T01:02:03Z",
            "head": {"ref": "feat/x"},
        },
    ]
    monkeypatch.setattr(fetchers, "_api_get_all", lambda *a, **kw: payload)

    prs = fetchers.fetch_prs()

    assert [p["number"] for p in prs] == [105, 106]
    merged, opened = prs
    assert merged["state"] == "MERGED"
    assert merged["head_ref"] == "fix/ops-kpi-label"
    assert merged["merged_at"] == "2026-10-17"          # 落库口径只到日
    assert merged["merged_ts"] == "2026-10-17T14:57:47Z"  # 周期计算要秒级
    assert opened["state"] == "OPEN"
    assert opened["merged_at"] is None
    assert opened["merged_ts"] is None


def test_fetch_issues_excludes_pull_requests(monkeypatch):
    """GitHub 的 /issues 会把 PR 一并返回，必须按 pull_request 字段剔除。"""
    payload = [
        {
            "number": 200,
            "title": "真 issue",
            "state": "open",
            "created_at": "2026-10-17T01:00:00Z",
            "closed_at": None,
            "labels": [{"name": "bug"}],
        },
        {
            "number": 201,
            "title": "其实是 PR",
            "state": "open",
            "pull_request": {"url": "https://api.github.com/repos/x/pulls/201"},
            "created_at": "2026-10-17T02:00:00Z",
            "closed_at": None,
            "labels": [],
        },
    ]
    monkeypatch.setattr(fetchers, "_api_get_all", lambda *a, **kw: payload)

    issues = fetchers.fetch_issues()

    assert [i["number"] for i in issues] == [200]
    assert issues[0]["state"] == "OPEN"
    assert issues[0]["labels"] == ["bug"]


# ============================================================
# 分页
# ============================================================
def test_pagination_fetches_next_page_when_full(monkeypatch):
    calls: list[int] = []

    def fake_get(path, params=None):
        page = int(params["page"])
        calls.append(page)
        return [{"n": page * 100 + i} for i in range(fetchers.API_PER_PAGE)]

    monkeypatch.setattr(fetchers, "_api_get", fake_get)

    items = fetchers._api_get_all("/repos/x/pulls")

    assert calls == [1, 2, 3]  # 满页就一直翻到上限
    assert len(items) == fetchers.API_PER_PAGE * fetchers.API_MAX_PAGES


def test_pagination_stops_on_short_page(monkeypatch):
    calls: list[int] = []

    def fake_get(path, params=None):
        page = int(params["page"])
        calls.append(page)
        return [{"n": 1}]  # 不足一页 → 到底了

    monkeypatch.setattr(fetchers, "_api_get", fake_get)

    assert len(fetchers._api_get_all("/repos/x/pulls")) == 1
    assert calls == [1]


# ============================================================
# 认证与限速
# ============================================================
def test_api_get_anonymous_headers(monkeypatch):
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return _FakeResp(b"[]")

    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(fetchers.urllib.request, "urlopen", fake_urlopen)

    assert fetchers._api_get("/repos/x/pulls") == []
    req = captured["req"]
    assert req.get_header("Authorization") is None  # 匿名可用，不带凭据
    assert req.get_header("User-agent")  # GitHub 强制要求 UA


def test_api_get_uses_token_when_present(monkeypatch):
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return _FakeResp(b"[]")

    monkeypatch.setenv("GH_TOKEN", "dummy-token")
    monkeypatch.setattr(fetchers.urllib.request, "urlopen", fake_urlopen)

    fetchers._api_get("/repos/x/pulls")

    assert captured["req"].get_header("Authorization") == "Bearer dummy-token"


def test_api_get_rate_limited_raises_with_hint(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise _http_error(403)

    monkeypatch.setattr(fetchers.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(fetchers.FetchError) as exc:
        fetchers._api_get("/repos/x/pulls")

    msg = str(exc.value)
    assert "rate_limit_remaining=0" in msg
    assert "reset_in=" in msg
    assert "GH_TOKEN" in msg  # 给出可执行的下一步，而不是只说「失败了」


def test_api_get_non_array_payload_rejected(monkeypatch):
    def fake_urlopen(req, timeout=None):
        return _FakeResp(b'{"message": "Not Found"}')

    monkeypatch.setattr(fetchers.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(fetchers.FetchError):
        fetchers._api_get("/repos/x/pulls")


def test_api_get_unreachable_raises_fetch_error(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("dns fail")

    monkeypatch.setattr(fetchers.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(fetchers.FetchError):
        fetchers._api_get("/repos/x/pulls")

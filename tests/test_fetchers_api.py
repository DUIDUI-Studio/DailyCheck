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
    """/issues 会把 PR 一并返回，必须按 pull_request 字段剔除。

    Gitea 28.1.0 起真 issue 也带 `pull_request` 键（值为 null），旧版则没有该键；
    判据必须是「值为 None」而不是「键不存在」，否则真 issue 会被全部剔成假零。
    """
    payload = [
        {
            # Gitea 28：真 issue，键存在但为 null → 必须保留
            "number": 200,
            "title": "真 issue（Gitea 28）",
            "state": "open",
            "pull_request": None,
            "created_at": "2026-10-17T01:00:00Z",
            "closed_at": None,
            "labels": [{"name": "bug"}],
        },
        {
            # 旧版 Gitea：真 issue，无该键 → 必须保留
            "number": 202,
            "title": "真 issue（旧版）",
            "state": "open",
            "created_at": "2026-10-17T03:00:00Z",
            "closed_at": None,
            "labels": [],
        },
        {
            # PR：该键为 dict → 必须剔除
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

    assert [i["number"] for i in issues] == [200, 202]
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

    assert calls == list(range(1, fetchers.API_MAX_PAGES + 1))  # 满页就一直翻到上限
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
def test_api_get_without_token_fails_closed(monkeypatch):
    """Gitea 仓库是 private，没有匿名兜底 —— 无 token 必须抛错，不能返回空列表。

    空列表会被上层读成「今天 0 个合并」，正是 GitHub 时代踩过的假信号。
    """
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    monkeypatch.setattr(fetchers, "TOKEN_FILES", ())  # 断掉文件兜底

    with pytest.raises(fetchers.FetchError) as exc:
        fetchers._api_get("/repos/x/pulls")
    assert "token" in str(exc.value).lower()


def test_api_get_uses_token_header(monkeypatch):
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return _FakeResp(b"[]")

    monkeypatch.setenv("GITEA_TOKEN", "dummy-token")
    monkeypatch.setattr(fetchers.urllib.request, "urlopen", fake_urlopen)

    assert fetchers._api_get("/repos/x/pulls") == []
    assert captured["req"].get_header("Authorization") == "token dummy-token"


def test_api_get_403_hint_points_at_scope(monkeypatch):
    """Gitea 权限不足回 403（不是 401）—— 提示必须指向 scope，别让人去怀疑 token 失效。"""

    def fake_urlopen(req, timeout=None):
        raise _http_error(403)

    monkeypatch.setenv("GITEA_TOKEN", "dummy-token")
    monkeypatch.setattr(fetchers.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(fetchers.FetchError) as exc:
        fetchers._api_get("/repos/x/pulls")

    msg = str(exc.value)
    assert "403" in msg
    assert "scope" in msg


def test_norm_ts_converts_cst_to_utc():
    """Gitea 时间戳带 +08:00，必须真正换算成 UTC。

    截前 19 位再补 Z 会把 CST 当 UTC，交付周期整整差 8 小时。
    """
    assert fetchers._norm_ts("2026-10-24T10:22:41+08:00") == "2026-10-24T02:22:41Z"
    assert fetchers._norm_ts("2026-10-17T12:42:47Z") == "2026-10-17T12:42:47Z"
    assert fetchers._norm_ts(None) is None


def test_api_get_non_array_payload_rejected(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "dummy-token")
    def fake_urlopen(req, timeout=None):
        return _FakeResp(b'{"message": "Not Found"}')

    monkeypatch.setattr(fetchers.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(fetchers.FetchError):
        fetchers._api_get("/repos/x/pulls")


def test_api_get_unreachable_raises_fetch_error(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "dummy-token")
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("dns fail")

    monkeypatch.setattr(fetchers.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(fetchers.FetchError):
        fetchers._api_get("/repos/x/pulls")

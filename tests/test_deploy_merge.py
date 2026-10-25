"""deploy_merge.py（FF-only 合并助手）测试。

用 fake curl 的响应队列控制每次 HTTP 调用；不触网、不碰真实 Gitea。
覆盖：前置校验、FF 成功、非快进、API 失败、token 缺失、脱敏。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

BG_DIR = Path(__file__).resolve().parent.parent / "scripts" / "blue_green"
SHA = "abcdef1234567890abcdef1234567890abcdef12"

_FAKE_CURL = '''#!{py}
import json, os, sys
from pathlib import Path

args = sys.argv[1:]
url = args[-1] if args else ""
method = "GET"
if "-X" in args:
    method = args[args.index("-X") + 1]
payload = None
if "--data-binary" in args:
    spec = args[args.index("--data-binary") + 1]
    if spec.startswith("@"):
        payload = Path(spec[1:]).read_text(encoding="utf-8")

with open(os.environ["FAKE_CURL_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({{"method": method, "url": url, "payload": payload}}) + "\\n")

qpath = Path(os.environ["FAKE_CURL_QUEUE"])
queue = json.loads(qpath.read_text(encoding="utf-8"))
item = queue.pop(0) if queue else {{"code": 500, "body": "{{}}"}}
qpath.write_text(json.dumps(queue), encoding="utf-8")
body = item["body"]
if not isinstance(body, str):
    body = json.dumps(body, ensure_ascii=False)
sys.stdout.write(body + "\\n" + str(item["code"]))
'''


@pytest.fixture()
def fake(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    curl = bindir / "fakecurl"
    curl.write_text(_FAKE_CURL.format(py=sys.executable), encoding="utf-8")
    curl.chmod(0o755)
    log = tmp_path / "curl.jsonl"
    queue = tmp_path / "queue.json"
    queue.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("DAILYCHECK_CURL", str(curl))
    monkeypatch.setenv("FAKE_CURL_LOG", str(log))
    monkeypatch.setenv("FAKE_CURL_QUEUE", str(queue))
    monkeypatch.setenv("DAILYCHECK_GITEA_TOKEN", "test-token")
    monkeypatch.setenv("FEEDBACK_GITEA_API", "http://gitea.test/api/v1")
    monkeypatch.setenv("FEEDBACK_GITEA_REPO", "eric/dailycheck")
    monkeypatch.delenv("DAILYCHECK_GITEA_TOKEN_FILE", raising=False)

    def respond(*items):
        queue.write_text(json.dumps(list(items)), encoding="utf-8")

    def calls():
        if not log.exists():
            return []
        return [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines() if ln]

    return {"respond": respond, "calls": calls}


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(BG_DIR / "deploy_merge.py"), *args],
        capture_output=True, text=True, timeout=30,
    )


def _pr(**overrides) -> dict:
    data = {
        "number": 42,
        "state": "open",
        "draft": False,
        "merged": False,
        "head": {"sha": SHA},
        "base": {"ref": "main"},
    }
    data.update(overrides)
    return data


def test_check_passes_for_open_pr(fake):
    fake["respond"]({"code": 200, "body": _pr()})
    proc = _run("check", "--pr", "42", "--sha", SHA)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = json.loads(proc.stdout)
    assert out["pr"] == 42
    assert out["merged"] is False
    assert len(fake["calls"]()) == 1


def test_check_rejects_closed_pr(fake):
    fake["respond"]({"code": 200, "body": _pr(state="closed")})
    proc = _run("check", "--pr", "42", "--sha", SHA)
    assert proc.returncode == 31
    assert "open" in json.loads(proc.stdout)["reason"]


def test_check_rejects_sha_mismatch(fake):
    fake["respond"]({"code": 200, "body": _pr(head={"sha": "0" * 40})})
    assert _run("check", "--pr", "42", "--sha", SHA).returncode == 31


def test_check_rejects_draft(fake):
    fake["respond"]({"code": 200, "body": _pr(draft=True)})
    assert _run("check", "--pr", "42", "--sha", SHA).returncode == 31


def test_check_rejects_non_main_base(fake):
    fake["respond"]({"code": 200, "body": _pr(base={"ref": "dev"})})
    assert _run("check", "--pr", "42", "--sha", SHA).returncode == 31


def test_merge_uses_fast_forward_only(fake):
    fake["respond"](
        {"code": 200, "body": _pr()},
        {"code": 200, "body": {}},
        {"code": 200, "body": _pr(merged=True, merge_commit_sha=SHA)},
    )
    proc = _run("merge", "--pr", "42", "--sha", SHA)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = json.loads(proc.stdout)
    assert out["merged"] is True
    assert out["merged_sha"] == SHA
    posts = [c for c in fake["calls"]() if c["method"] == "POST"]
    assert len(posts) == 1
    assert json.loads(posts[0]["payload"]) == {"do": "fast-forward-only"}


def test_merge_non_fast_forward_returns_30(fake):
    fake["respond"]({"code": 200, "body": _pr()}, {"code": 405, "body": {}})
    assert _run("merge", "--pr", "42", "--sha", SHA).returncode == 30


def test_merge_api_error_returns_32(fake):
    fake["respond"]({"code": 200, "body": _pr()}, {"code": 500, "body": {}})
    assert _run("merge", "--pr", "42", "--sha", SHA).returncode == 32


def test_merge_not_marked_merged_returns_32(fake):
    fake["respond"](
        {"code": 200, "body": _pr()},
        {"code": 200, "body": {}},
        {"code": 200, "body": _pr(merged=False)},
    )
    assert _run("merge", "--pr", "42", "--sha", SHA).returncode == 32


def test_missing_token_returns_32(fake, monkeypatch):
    monkeypatch.delenv("DAILYCHECK_GITEA_TOKEN", raising=False)
    fake["respond"]({"code": 200, "body": _pr()})
    assert _run("check", "--pr", "42", "--sha", SHA).returncode == 32


def test_get_non_json_returns_32(fake):
    fake["respond"]({"code": 200, "body": "not json"})
    assert _run("check", "--pr", "42", "--sha", SHA).returncode == 32


def test_dry_run_does_not_merge(fake):
    fake["respond"]({"code": 200, "body": _pr()})
    proc = _run("merge", "--pr", "42", "--sha", SHA, "--dry-run")
    assert proc.returncode == 0
    assert all(c["method"] == "GET" for c in fake["calls"]())


def test_token_never_appears_in_output(fake):
    fake["respond"]({"code": 200, "body": _pr()})
    proc = _run("check", "--pr", "42", "--sha", SHA)
    assert "test-token" not in proc.stdout + proc.stderr


def test_usage_error_returns_64(fake):
    assert _run("merge", "--pr", "42").returncode == 64

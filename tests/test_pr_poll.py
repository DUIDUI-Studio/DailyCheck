"""scripts/prod_agent/pr_poll.py 的用例。

这个脚本是「webhook 快通道整体失效」时唯一的发现通道，所以它的失败模式必须
是「可见」而不是「静默」：看错数据源、静默吞异常、把失败伪装成没事，都会让
一个坏掉的兜底看起来一切正常。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "prod_agent"))

import pr_poll  # noqa: E402

FAKE_CURL = '''#!{py}
import json, os, sys
args = sys.argv[1:]
url = args[-1]
log = os.environ.get("FAKE_CURL_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({{"url": url}}) + "\\n")
table = json.loads(os.environ.get("FAKE_CURL_TABLE", "{{}}"))
for key, val in table.items():
    if key in url:
        sys.stdout.write(json.dumps(val[1]) + "\\n" + str(val[0]))
        sys.exit(0)
sys.stdout.write("not found\\n404")
'''


@pytest.fixture()
def fake_curl(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "fakecurl"
    fake.write_text(FAKE_CURL.format(py=sys.executable), encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setattr(pr_poll, "CURL", str(fake))
    monkeypatch.setattr(pr_poll, "REPO", "eric/dailycheck")
    monkeypatch.setattr(pr_poll, "GITEA_API", "http://gitea.test/api/v1")
    monkeypatch.setattr(pr_poll, "TOKEN_FILES", [])
    monkeypatch.setenv("FAKE_CURL_LOG", str(tmp_path / "curl.jsonl"))
    monkeypatch.setenv("DCPOLL_REPO", "eric/dailycheck")  # 关掉 catchup 分支
    return tmp_path


def _table(monkeypatch, table: dict) -> None:
    monkeypatch.setenv("FAKE_CURL_TABLE", json.dumps(table))


def _pr(**kw) -> dict:
    base = {
        "number": 42,
        "title": "feat: something",
        "head": {"ref": "feat/x", "sha": "a" * 40},
        "mergeable": True,
        "draft": False,
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------- 静默语义

def test_no_open_pr_is_silent(fake_curl, monkeypatch):
    """无 PR = 零输出。cron 收到空输出才不投递，多一个字就是打扰。"""
    monkeypatch.setattr(pr_poll, "_token", lambda: "tok")
    _table(monkeypatch, {"/pulls": [200, []]})
    assert pr_poll._pr_block() == ""


def test_catchup_inactive_is_silent(fake_curl, monkeypatch):
    """单飞机制未初始化 = 向后兼容窗口，不该报错。"""
    monkeypatch.setattr(pr_poll, "DEPLOY_STATE", "/nonexistent/deploy_state.py")
    monkeypatch.delenv("DCPOLL_REPO", raising=False)
    assert pr_poll._catchup_block() == ""


# ---------------------------------------------------------------- 正常输出

def test_lists_open_pr_with_check_state(fake_curl, monkeypatch):
    monkeypatch.setattr(pr_poll, "_token", lambda: "tok")
    _table(monkeypatch, {
        "/pulls": [200, [_pr()]],
        "/status": [200, {"state": "success", "statuses": [
            {"context": "CI / test (pull_request)", "status": "success"},
            {"context": "CI / agent-framework-reports (pull_request)", "status": "success"},
        ]}],
    })
    out = pr_poll._pr_block()
    assert "PR #42" in out
    assert "head=aaaaaaaa" in out
    assert "checks=success" in out
    assert "mergeable=True" in out


def test_reports_failing_contexts(fake_curl, monkeypatch):
    """「哪条 check 没过」是 agent 决定下一步的关键，不能只给一个 failure。"""
    monkeypatch.setattr(pr_poll, "_token", lambda: "tok")
    _table(monkeypatch, {
        "/pulls": [200, [_pr()]],
        "/status": [200, {"state": "failure", "statuses": [
            {"context": "CI / test (pull_request)", "status": "failure"},
            {"context": "CI / agent-framework-reports (pull_request)", "status": "success"},
        ]}],
    })
    out = pr_poll._pr_block()
    assert "checks=failure" in out
    assert "CI / test (pull_request)=failure" in out
    # 「未过」列表里不该混进已通过的 context
    failing_part = out.split("（未过：")[1].split("）")[0]
    assert "agent-framework-reports" not in failing_part


def test_draft_and_unmergeable_are_visible(fake_curl, monkeypatch):
    """draft / mergeable 决定能不能进 merge 判定，必须原样透出。"""
    monkeypatch.setattr(pr_poll, "_token", lambda: "tok")
    _table(monkeypatch, {
        "/pulls": [200, [_pr(draft=True, mergeable=False)]],
        "/status": [200, {"state": "pending", "statuses": []}],
    })
    out = pr_poll._pr_block()
    assert "draft=True" in out and "mergeable=False" in out


def test_multiple_prs_all_listed(fake_curl, monkeypatch):
    monkeypatch.setattr(pr_poll, "_token", lambda: "tok")
    _table(monkeypatch, {
        "/pulls": [200, [_pr(number=1), _pr(number=2), _pr(number=3)]],
        "/status": [200, {"state": "pending"}],
    })
    out = pr_poll._pr_block()
    for n in (1, 2, 3):
        assert f"PR #{n}" in out


# ---------------------------------------------------------------- 故障可见

def test_missing_token_is_loud(fake_curl, monkeypatch):
    monkeypatch.setattr(pr_poll, "TOKEN_FILES", ["/nonexistent/token"])
    assert "异常" in pr_poll._pr_block()


def test_http_error_is_loud(fake_curl, monkeypatch):
    monkeypatch.setattr(pr_poll, "_token", lambda: "tok")
    _table(monkeypatch, {"/pulls": [500, {"message": "boom"}]})
    out = pr_poll._pr_block()
    assert "异常" in out and "500" in out


def test_non_array_payload_is_loud(fake_curl, monkeypatch):
    """API 返回了对象而不是数组 → 不能被当成「没有 PR」静默放过。"""
    monkeypatch.setattr(pr_poll, "_token", lambda: "tok")
    _table(monkeypatch, {"/pulls": [200, {"message": "unexpected"}]})
    assert "非数组" in pr_poll._pr_block()


def test_status_query_failure_does_not_hide_pr(fake_curl, monkeypatch):
    """单个 PR 的 CI 状态查不到，PR 本身仍必须列出来。"""
    monkeypatch.setattr(pr_poll, "_token", lambda: "tok")
    _table(monkeypatch, {"/pulls": [200, [_pr()]]})  # 没有 /status 条目 → 404
    out = pr_poll._pr_block()
    assert "PR #42" in out
    assert "http404" in out


def test_main_survives_exception(fake_curl, monkeypatch, capsys):
    """任何未捕获异常都必须变成可见告警，而不是让 cron 收到空输出。"""
    def boom():
        raise RuntimeError("kaput")

    monkeypatch.setattr(pr_poll, "_catchup_block", boom)
    monkeypatch.setattr(pr_poll, "_pr_block", lambda: "")
    assert pr_poll.main() == 0
    out = capsys.readouterr().out
    assert "异常" in out and "kaput" in out


# ---------------------------------------------------------------- 代码卫生

def test_docstring_paths_point_at_real_file():
    """薄包装指向的仓库路径必须真实存在（否则 cron 每轮都会 FileNotFoundError）。"""
    shim = (Path(__file__).resolve().parents[1]
            / "deploy" / "scripts" / "dailycheck_pr_poll_shim.py")
    content = shim.read_text(encoding="utf-8")
    assert 'os.environ.get("DAILYCHECK_SOURCE_DIR", "/root/DailyCheck")' in content
    assert '"scripts", "prod_agent", "pr_poll.py"' in content
    assert (Path(__file__).resolve().parents[1]
            / "scripts" / "prod_agent" / "pr_poll.py").exists()

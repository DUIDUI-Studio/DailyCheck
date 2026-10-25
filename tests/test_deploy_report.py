"""deploy_report / deploy_triage_pure 测试。

两层分开验：

- 纯函数层（归因、需求解析、对齐判定、脱敏）—— 直接断言，不需要任何外部态。
- 写回层（CLI write）—— 用 fake curl 记录实际发出的 HTTP 调用与 payload，
  所以能验「发了什么」而不只是「没报错」。这是唯一能证明「PR 上真会出现那条
  评论」的测法，生产上不会有第二次机会去试。

fake curl 刻意用 Python 实现并读 ``FAKE_CURL_*`` 环境变量控制响应，避免在
macOS / Linux 之间纠缠 shell 的 printf/转义差异。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BG_DIR = Path(__file__).resolve().parent.parent / "scripts" / "blue_green"
sys.path.insert(0, str(BG_DIR))

import deploy_report  # noqa: E402
import deploy_triage_pure as tp  # noqa: E402

SHA = "abcdef1234567890abcdef1234567890abcdef12"

_PR_BODY = """## 报告 1/4：需求对齐

| ID | 需求 | 状态 |
|----|------|------|
| R-001 | 反馈模块加 LLM 预分析 | ✅ 已实现 |
| R-002 | 二审确认后才派发 | ✅ 已实现 |

## Checklist
"""

_FAKE_CURL = '''#!{py}
import json, os, sys
args = sys.argv[1:]
url = args[-1] if args else ""
method = "GET"
if "-X" in args:
    method = args[args.index("-X") + 1]
payload = None
if "--data-binary" in args:
    spec = args[args.index("--data-binary") + 1]
    if spec.startswith("@"):
        with open(spec[1:], encoding="utf-8") as fh:
            payload = fh.read()
with open(os.environ["FAKE_CURL_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({{"method": method, "url": url, "payload": payload}},
                        ensure_ascii=False) + "\\n")
resp, code = "{{}}", "200"
if method == "GET" and "/pulls/" in url:
    resp, code = os.environ.get("FAKE_CURL_PR", "{{}}"), "200"
elif method == "GET" and "/comments" in url:
    resp, code = os.environ.get("FAKE_CURL_COMMENTS", "[]"), "200"
elif method == "POST":
    code = os.environ.get("FAKE_CURL_POST_CODE", "201")
    resp = os.environ.get("FAKE_CURL_POST_BODY", "{{}}")
elif method == "PATCH":
    code = os.environ.get("FAKE_CURL_PATCH_CODE", "200")
if method == "GET":
    resp = resp.replace("\\n", " ")
sys.stdout.write(resp + "\\n" + code)
'''


@pytest.fixture()
def fake_curl(tmp_path, monkeypatch):
    """装一个假 curl，返回 (调用记录 getter, 环境已就绪)。"""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "fakecurl"
    fake.write_text(_FAKE_CURL.format(py=sys.executable), encoding="utf-8")
    fake.chmod(0o755)
    log = tmp_path / "curl.jsonl"
    monkeypatch.setenv("DAILYCHECK_CURL", str(fake))
    monkeypatch.setenv("FAKE_CURL_LOG", str(log))
    monkeypatch.setenv("DAILYCHECK_GITEA_TOKEN", "test-token")
    monkeypatch.setenv("FEEDBACK_GITEA_API", "http://gitea.test/api/v1")
    monkeypatch.setenv("FEEDBACK_GITEA_REPO", "eric/dailycheck")
    monkeypatch.delenv("DAILYCHECK_GITEA_TOKEN_FILE", raising=False)
    monkeypatch.delenv("DAILYCHECK_DEPLOY_REPORT", raising=False)
    # 隔离唤醒队列：不隔离的话测试会往真实的 /var/lib/dailycheck-prod-agent
    # 写事件，在 VPS 上跑就等于伪造了一次部署结果喂给生产 agent。
    monkeypatch.setenv("DAILYCHECK_PROD_AGENT_INBOX", str(tmp_path / "inbox.jsonl"))

    def calls() -> list[dict]:
        if not log.exists():
            return []
        return [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines() if ln]

    return calls


# ---------------------------------------------------------------- 归因

@pytest.mark.parametrize(
    "rc,stage,kind,scope",
    [
        (0, "", tp.KIND_SUCCESS, tp.SCOPE_OUT),
        (10, "", tp.KIND_LOCK, tp.SCOPE_OUT),
        (11, "", tp.KIND_LOCK, tp.SCOPE_OUT),
        (20, "up", tp.KIND_CODE, tp.SCOPE_IN),
        (21, "verify", tp.KIND_CODE, tp.SCOPE_UNDETERMINED),
        (21, "verify-drift", tp.KIND_DRIFT, tp.SCOPE_OUT),
        (22, "promote", tp.KIND_ENV, tp.SCOPE_OUT),
        (23, "switch", tp.KIND_ENV, tp.SCOPE_OUT),
        (24, "observe", tp.KIND_ENV, tp.SCOPE_UNDETERMINED),
        (99, "", tp.KIND_UNKNOWN, tp.SCOPE_UNDETERMINED),
    ],
)
def test_triage_by_rc_and_stage(rc, stage, kind, scope):
    got = tp.triage(rc, stage, "")
    assert (got["kind"], got["scope"]) == (kind, scope)


def test_verify_drift_wins_over_rc():
    """rc=21 有两种成因，靠 rc 分不开 —— stage 必须优先。"""
    assert tp.triage(21, "verify-drift", "")["kind"] == tp.KIND_DRIFT


def test_dry_run_blocked_is_out_of_scope():
    """DRY_RUN 未关是 10-15 / 10-17 两次事故的同型故障，必须判死。"""
    got = tp.triage(24, "observe", "[dry_run] BLOCKED write denied")
    assert got["kind"] == tp.KIND_ENV
    assert got["scope"] == tp.SCOPE_OUT
    assert "DRY_RUN" in got["reason"]


def test_only_code_kind_is_actionable():
    assert tp.triage(20, "up", "")["code_actionable"] is True
    assert tp.triage(23, "switch", "")["code_actionable"] is False


# ---------------------------------------------------------------- 需求解析

def test_parse_requirements_from_report_table():
    got = tp.parse_requirements(_PR_BODY)
    assert [r["id"] for r in got] == ["R-001", "R-002"]
    assert "LLM" in got[0]["text"]


def test_parse_requirements_keeps_underscored_identifiers():
    """``DRY_RUN`` 不能被洗成 ``DRYRUN`` —— 下划线是标识符的一部分，不是斜体。"""
    body = "## 报告 1/4：需求对齐\n\n| **R-001** | 绿实例不跑 **DRY_RUN** 和 `FEEDBACK_LLM_API_KEY` | ⬜ |\n"
    got = tp.parse_requirements(body)
    assert got and "DRY_RUN" in got[0]["text"]
    assert "FEEDBACK_LLM_API_KEY" in got[0]["text"]


def test_parse_requirements_without_report_section_is_empty():
    """只有 R 字样、没有「报告 1/4」节 → 不算可核对目标。"""
    assert tp.parse_requirements("| R-001 | 随便写的 | ✅ |") == []


def test_parse_requirements_handles_empty():
    assert tp.parse_requirements("") == []
    assert tp.parse_requirements(None) == []


# ---------------------------------------------------------------- 对齐判定

def test_no_requirements_blocks_automation():
    """PR 没写目标 → 不许自动动手，哪怕失败明显指向代码。"""
    tr = tp.triage(21, "verify", "blueprints/x.py 报错")
    got = tp.assess_alignment(tr, "", ["blueprints/x.py"], "blueprints/x.py 报错")
    assert got["verdict"] == tp.VERDICT_UNVERIFIABLE
    assert "报告 1/4" in got["reason"]


def test_out_of_scope_failure_is_drifted():
    tr = tp.triage(10, "", "")
    got = tp.assess_alignment(tr, _PR_BODY, ["blueprints/x.py"], "")
    assert got["verdict"] == tp.VERDICT_DRIFTED
    assert "不要改" in got["action"]


def test_log_hitting_changed_file_is_aligned():
    tr = tp.triage(21, "verify", "FAILED blueprints/feedback.py::test_x")
    got = tp.assess_alignment(
        tr, _PR_BODY, ["blueprints/feedback.py"], "FAILED blueprints/feedback.py::test_x"
    )
    assert got["verdict"] == tp.VERDICT_ALIGNED
    assert got["hits"] == ["blueprints/feedback.py"]


def test_code_failure_without_file_hit_is_unknown():
    tr = tp.triage(21, "verify", "ModuleNotFoundError: no module named foo")
    got = tp.assess_alignment(tr, _PR_BODY, ["blueprints/x.py"], "ModuleNotFoundError")
    assert got["verdict"] == tp.VERDICT_UNKNOWN


def test_success_is_aligned():
    got = tp.assess_alignment(tp.triage(0, "", ""), _PR_BODY, [], "")
    assert got["verdict"] == tp.VERDICT_ALIGNED
    assert got["action"] == "none"


def test_bare_filename_does_not_match():
    """裸文件名太容易误命中 —— 宁可不判，不要误判 aligned。"""
    assert tp._mentioned_files("error in items.py line 12", ["items.py"]) == []


# ---------------------------------------------------------------- 动作授权

_ALIGNED = {"verdict": tp.VERDICT_ALIGNED}
_DRIFTED = {"verdict": tp.VERDICT_DRIFTED}
_UNVERIFIABLE = {"verdict": tp.VERDICT_UNVERIFIABLE}
_UNKNOWN = {"verdict": tp.VERDICT_UNKNOWN}


@pytest.mark.parametrize(
    "kind,alignment,count,expect",
    [
        (tp.KIND_SUCCESS, _ALIGNED, 0, tp.NEXT_NONE),
        (tp.KIND_USAGE, _UNKNOWN, 0, tp.NEXT_AUDIT),
        (tp.KIND_CODE, _ALIGNED, 0, tp.NEXT_AUTOFIX),
        (tp.KIND_CODE, _ALIGNED, 2, tp.NEXT_AUTOFIX),
        # 到顶不是「放弃」，是「这方向修不动了，重审目标」
        (tp.KIND_CODE, _ALIGNED, tp.MAX_AUTOFIX, tp.NEXT_HALT),
        # 环境问题即使在 aligned 下也不许改代码
        (tp.KIND_ENV, _ALIGNED, 0, tp.NEXT_ENVFIX),
        (tp.KIND_LOCK, _DRIFTED, 0, tp.NEXT_ENVFIX),
        (tp.KIND_DRIFT, _DRIFTED, 0, tp.NEXT_ENVFIX),
        (tp.KIND_CODE, _UNKNOWN, 0, tp.NEXT_ENVFIX),
        # 没目标 → 先推导，不是叫人补
        (tp.KIND_CODE, _UNVERIFIABLE, 0, tp.NEXT_DERIVE),
    ],
)
def test_next_action_table(kind, alignment, count, expect):
    assert tp.next_action({"kind": kind}, alignment, count) == expect


def test_next_action_autofix_is_bounded():
    """autofix 是本模块唯一的写授权，必须有上界。"""
    tr = {"kind": tp.KIND_CODE}
    assert tp.next_action(tr, _ALIGNED, tp.MAX_AUTOFIX - 1) == tp.NEXT_AUTOFIX
    assert tp.next_action(tr, _ALIGNED, tp.MAX_AUTOFIX) == tp.NEXT_HALT


def test_next_action_never_targets_a_human():
    """链路上不存在「通知人」这个动作 —— 这是本次设计变更的核心约束。"""
    values = {
        tp.NEXT_NONE, tp.NEXT_AUTOFIX, tp.NEXT_ENVFIX,
        tp.NEXT_DERIVE, tp.NEXT_AUDIT, tp.NEXT_HALT,
    }
    for kind in (tp.KIND_SUCCESS, tp.KIND_CODE, tp.KIND_ENV, tp.KIND_LOCK,
                 tp.KIND_DRIFT, tp.KIND_USAGE, tp.KIND_UNKNOWN):
        for align in (_ALIGNED, _DRIFTED, _UNVERIFIABLE, _UNKNOWN):
            for count in (0, tp.MAX_AUTOFIX, 99):
                assert tp.next_action({"kind": kind}, align, count) in values


# ---------------------------------------------------------------- 脱敏

def test_redact_masks_credentials():
    raw = "export SIM_USER_PASSWORD=abc123\nAuthorization: token ghp_secretvalue"
    out = deploy_report.redact(raw)
    assert "abc123" not in out
    assert "ghp_secretvalue" not in out
    assert "***" in out


def test_redact_keeps_plain_lines():
    raw = "[pipeline] 已持锁"
    assert deploy_report.redact(raw) == raw


# ---------------------------------------------------------------- 渲染

def _render(rc: int, stage: str, tail: str = "", pr_body: str = _PR_BODY) -> str:
    tr, align, err = deploy_report._assess(29, str(BG_DIR), SHA, rc, stage, tail, pr_body, "")
    return deploy_report.render(29, SHA, tr, align, tail, err)


def test_render_has_marker_and_facts():
    out = _render(21, "verify", "[pipeline] ERROR boom")
    assert out.startswith(deploy_report.MARKER)
    assert "`abcdef123456`" in out
    assert "`verify`" in out
    assert "ERROR boom" in out


def test_render_never_leaks_password():
    out = _render(21, "verify", "SIM_USER_PASSWORD=hunter2")
    assert "hunter2" not in out
    assert "***" in out


def test_render_lists_pr_requirements():
    out = _render(23, "switch")
    assert "R-001" in out
    assert "二审确认后才派发" in out


# ---------------------------------------------------------------- CLI write

def _write(tmp_path, fake_curl, sha: str | None = None, rc: int = 21, **env):
    log = tmp_path / "pipeline.log"
    log.write_text("line1\nERROR boom\n", encoding="utf-8")
    os.environ["DAILYCHECK_DEPLOY_EVENT_LOG"] = str(tmp_path / "ev.jsonl")
    for k, v in env.items():
        os.environ[k] = str(v)
    try:
        rc_code = deploy_report.main([
            "write", "--pr", "29", "--sha", sha or SHA, "--rc", str(rc),
            "--stage", "verify", "--log", str(log), "--repo", str(tmp_path),
        ])
        calls = fake_curl()
    finally:
        for k in (*env, "DAILYCHECK_DEPLOY_EVENT_LOG"):
            os.environ.pop(k, None)
    return rc_code, calls


def test_write_posts_comment(tmp_path, fake_curl, monkeypatch):
    monkeypatch.setenv("FAKE_CURL_PR", json.dumps({"body": _PR_BODY}))
    rc, calls = _write(tmp_path, fake_curl)
    assert rc == 0
    posts = [c for c in calls if c["method"] == "POST" and "/comments" in c["url"]]
    assert len(posts) == 1
    payload = json.loads(posts[0]["payload"])
    assert deploy_report.MARKER in payload["body"]
    assert "ERROR boom" in payload["body"]


def test_write_is_idempotent_per_sha(tmp_path, fake_curl, monkeypatch):
    """同 sha 重跑必须编辑而不是再发一条 —— 否则 PR 变成日志墙。"""
    monkeypatch.setenv("FAKE_CURL_PR", json.dumps({"body": _PR_BODY}))
    monkeypatch.setenv(
        "FAKE_CURL_COMMENTS",
        json.dumps([{"id": 777, "body": f"{deploy_report.MARKER}\n| Commit | `{SHA[:12]}` |"}]),
    )
    rc, calls = _write(tmp_path, fake_curl)
    assert rc == 0
    patches = [c for c in calls if c["method"] == "PATCH"]
    assert len(patches) == 1
    assert "/issues/comments/777" in patches[0]["url"]
    assert not [c for c in calls if c["method"] == "POST" and "/comments" in c["url"]]


def test_write_survives_gitea_failure(tmp_path, fake_curl, monkeypatch):
    """Gitea 挂了绝不能让部署变红 —— 收尾路径必须吞掉失败。"""
    monkeypatch.setenv("FAKE_CURL_POST_CODE", "500")
    monkeypatch.setenv("FAKE_CURL_PR", "{}")
    rc, _ = _write(tmp_path, fake_curl)
    assert rc == 0


def test_write_disabled_by_env(tmp_path, fake_curl, monkeypatch):
    monkeypatch.setenv("DAILYCHECK_DEPLOY_REPORT", "0")
    rc, calls = _write(tmp_path, fake_curl)
    assert rc == 0
    assert calls == []


def test_write_never_calls_any_external_notifier(tmp_path, fake_curl, monkeypatch):
    """失败时除 Gitea 外不许再打任何外部端点 —— 这条链路面向 agent，不面向人。

    曾经这里推企微群。人不是这条链路的执行者，把「请去处理」推给人等于把
    agent-first 的流程又接回人工值班。旧 env 即使被设上也必须无效。
    """
    monkeypatch.setenv("FAKE_CURL_PR", json.dumps({"body": _PR_BODY}))
    monkeypatch.setenv("WECOM_DEPLOY_WEBHOOK", "http://wecom.test/hook")
    rc, calls = _write(tmp_path, fake_curl)
    assert rc == 0
    assert not [c for c in calls if "wecom.test" in c["url"]]
    assert all("gitea.test" in c["url"] for c in calls), [c["url"] for c in calls]


# ---------------------------------------------------------------- 事件流

def _events(tmp_path) -> list[dict]:
    path = tmp_path / "ev.jsonl"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_write_emits_machine_readable_event(tmp_path, fake_curl, monkeypatch):
    """事件是 agent 唯一的入口，必须自包含：拿到它就知道该干什么、去哪看。"""
    monkeypatch.setenv("FAKE_CURL_PR", json.dumps({"body": _PR_BODY}))
    monkeypatch.setenv("FAKE_CURL_POST_BODY", json.dumps({"id": 4242}))
    _write(tmp_path, fake_curl)
    rows = _events(tmp_path)
    assert len(rows) == 1
    ev = rows[0]
    assert ev["v"] == 1 and ev["pr"] == 29 and ev["sha"] == SHA
    assert (ev["kind"], ev["rc"]) == ("code", 21)
    # tmp_path 不是 git 仓库 → 推不出改动文件 → 日志没命中 → unknown → envfix。
    # 具体取值由 test_next_action_table 逐格覆盖，这里只验「判定被写进事件」。
    assert ev["next"] == "envfix"
    assert ev["report_comment"] == 4242
    assert ev["log"], "事件必须带上日志路径，否则 agent 还得自己猜去哪找现场"


def test_event_emitted_even_when_gitea_down(tmp_path, fake_curl, monkeypatch):
    """Gitea 抖动不该让一次部署失败凭空消失。"""
    monkeypatch.setenv("FAKE_CURL_POST_CODE", "500")
    monkeypatch.setenv("FAKE_CURL_PR", "{}")
    _write(tmp_path, fake_curl)
    rows = _events(tmp_path)
    assert len(rows) == 1 and rows[0]["report_comment"] == 0


def test_event_log_survives_corrupt_line(tmp_path, fake_curl, monkeypatch):
    """进程被 kill 会留下半行 JSON，不该让熔断计数整体失效。"""
    path = tmp_path / "ev.jsonl"
    good = json.dumps({"pr": 29, "sha": "a" * 40, "next": "autofix"})
    path.write_text(good + "\n" + '{"pr": 29, "ne\n', encoding="utf-8")
    assert deploy_report.autofix_count(29, path) == 1


# ---------------------------------------------------------------- 唤醒队列
#
# 事件文件只解决「事实留下」，不解决「agent 什么时候知道」。下面这几条守的是
# 唤醒侧的契约 —— 一旦字段名或取值漂了，dc-inbox-watcher 会**静默**跳过事件
# （它把非白名单条目直接记 handled 不再关注，不报错），部署失败就变成没人知道。

# dc-inbox-watcher.service 的 --actions 实参。改这个集合要同步改 unit。
_WATCHER_ACTIONS = {"opened", "synchronize", "synchronized", "reopened", "completed"}


def _inbox(tmp_path) -> list[dict]:
    path = tmp_path / "inbox.jsonl"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_write_wakes_prod_agent(tmp_path, fake_curl, monkeypatch):
    """部署收尾必须把 agent 叫醒，否则它只能等下一轮兜底轮询。"""
    monkeypatch.setenv("FAKE_CURL_PR", json.dumps({"body": _PR_BODY}))
    monkeypatch.setenv("FAKE_CURL_POST_BODY", json.dumps({"id": 4242}))
    monkeypatch.setenv("DAILYCHECK_GITEA_WEB_BASE", "http://hub01:3000")
    _write(tmp_path, fake_curl)
    rows = _inbox(tmp_path)
    assert len(rows) == 1
    ev = rows[0]
    assert ev["event"] == "deploy_result"
    assert ev["pr_number"] == 29 and ev["pr_head_sha"] == SHA
    assert ev["deploy_rc"] == 21 and ev["deploy_stage"] == "verify"
    assert ev["deploy_next"] == "envfix"
    assert ev["deploy_report_comment"] == 4242
    assert ev["pr_url"] == "http://hub01:3000/eric/dailycheck/pulls/29"
    assert ev["deploy_log"], "没日志路径的话 agent 还得自己猜去哪找现场"


def test_inbox_event_passes_watcher_filters(tmp_path, fake_curl, monkeypatch):
    """watcher 只看 repo_full_name 和 action，不匹配就静默丢弃 —— 必须守住。"""
    monkeypatch.setenv("FAKE_CURL_PR", json.dumps({"body": _PR_BODY}))
    _write(tmp_path, fake_curl)
    ev = _inbox(tmp_path)[0]
    assert ev["repo_full_name"] == "eric/dailycheck"   # == watcher --repo 默认值
    assert ev["action"] in _WATCHER_ACTIONS            # 否则记 handled 后不再关注
    assert ev["delivery_id"], "缺 delivery_id 会被 watcher 当畸形行丢掉"


def test_inbox_event_is_flat(tmp_path, fake_curl, monkeypatch):
    """订阅 prompt 只做 {key} 字面替换，嵌套对象取不到 —— 字段必须全平铺。"""
    monkeypatch.setenv("FAKE_CURL_PR", json.dumps({"body": _PR_BODY}))
    _write(tmp_path, fake_curl)
    for k, v in _inbox(tmp_path)[0].items():
        assert not isinstance(v, (dict, list)), f"{k} 是嵌套结构，prompt 里取不到"


def test_success_also_wakes_agent(tmp_path, fake_curl, monkeypatch):
    """成功也要投：agent 不知道锚点已推进，就会反复判 behind 重复部署。"""
    monkeypatch.setenv("FAKE_CURL_PR", json.dumps({"body": _PR_BODY}))
    _write(tmp_path, fake_curl, rc=0)
    ev = _inbox(tmp_path)[0]
    assert ev["deploy_rc"] == 0 and ev["deploy_next"] == "none"


def test_inbox_failure_does_not_fail_deploy(tmp_path, fake_curl, monkeypatch):
    """唤醒队列写不进去是环境问题，不该把部署收尾判红。"""
    monkeypatch.setenv("FAKE_CURL_PR", json.dumps({"body": _PR_BODY}))
    blocker = tmp_path / "blocked"
    blocker.write_text("not a dir", encoding="utf-8")
    monkeypatch.setenv("DAILYCHECK_PROD_AGENT_INBOX", str(blocker / "inbox.jsonl"))
    rc, _ = _write(tmp_path, fake_curl)
    assert rc == 0
    assert _events(tmp_path), "inbox 挂了，事件流仍然必须落盘"


def test_notify_adapter_routes_deploy_result():
    """路由脚本必须给 deploy_result 单独一条订阅。

    落到默认的 dailycheck-pr 不会报错 —— 那个订阅的 prompt 是「判定 merge」，
    会把一次部署失败当成 PR 更新处理，然后什么都不做。静默错投比投不出去更坏。
    """
    script = (Path(__file__).resolve().parents[1]
              / "deploy" / "scripts" / "dc-notify-prod-agent.sh")
    content = script.read_text(encoding="utf-8")
    case_block = content.split("case \"$event\" in", 1)[1].split("esac", 1)[0]
    assert "deploy_result)" in case_block
    assert 'sub="dailycheck-deploy"' in case_block
    # 默认分支必须仍然存在，否则解析失败的事件会直接掉地上
    assert "*) " in case_block or "*)" in case_block


def test_autofix_count_dedupes_by_sha(tmp_path):
    """同 sha 重跑不累加 —— 重跑没「修」任何东西，不该吃掉熔断额度。

    真实场景：verify 挂了 → 人工/agent 修好 nginx 环境 → 同 sha 再跑一次。
    这两条事件都是 autofix，但只修了零轮，算成两轮会让第三轮正常修复被
    误判成 halt。
    """
    path = tmp_path / "ev.jsonl"
    rows = [
        {"pr": 29, "sha": "a" * 40, "next": "autofix"},
        {"pr": 29, "sha": "a" * 40, "next": "autofix"},
        {"pr": 29, "sha": "b" * 40, "next": "autofix"},
        {"pr": 29, "sha": "c" * 40, "next": "envfix"},
        {"pr": 30, "sha": "d" * 40, "next": "autofix"},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    assert deploy_report.autofix_count(29, path) == 2


def test_write_skips_event_when_disabled(tmp_path, fake_curl, monkeypatch):
    monkeypatch.setenv("DAILYCHECK_DEPLOY_REPORT", "0")
    _write(tmp_path, fake_curl)
    assert _events(tmp_path) == []


def test_aligned_failure_grants_autofix_end_to_end(tmp_path, fake_curl, monkeypatch):
    """端到端：真 git 仓库 + 日志指到改动文件 → 事件里必须是 autofix。

    autofix 是本设计唯一的写授权，只在「失败确实由本 PR 引入」时给出。这条
    用例存在的意义是防止映射表对、但 write 路径传错了入参。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env={**os.environ, **env})
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True,
                   env={**os.environ, **env})
    (repo / "blueprints").mkdir()
    (repo / "blueprints" / "feedback.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "feat"], cwd=repo, check=True,
                   env={**os.environ, **env})
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                         text=True, check=True).stdout.strip()

    monkeypatch.setenv("FAKE_CURL_PR", json.dumps({"body": _PR_BODY}))
    log = repo / "pipeline.log"
    log.write_text("FAILED blueprints/feedback.py::test_x\n", encoding="utf-8")
    os.environ["DAILYCHECK_DEPLOY_EVENT_LOG"] = str(repo / "ev.jsonl")
    try:
        deploy_report.main([
            "write", "--pr", "29", "--sha", sha, "--rc", "21", "--stage", "verify",
            "--log", str(log), "--repo", str(repo),
        ])
    finally:
        os.environ.pop("DAILYCHECK_DEPLOY_EVENT_LOG", None)

    ev = _events(repo)[0]
    assert ev["verdict"] == "aligned", ev
    assert ev["next"] == "autofix", ev


# ---------------------------------------------------------------- CLI assess

def test_assess_reports_aligned_exit_zero(tmp_path, fake_curl, monkeypatch):
    pr_body = tmp_path / "pr.md"
    pr_body.write_text(_PR_BODY, encoding="utf-8")
    log = tmp_path / "pipeline.log"
    log.write_text("FAILED blueprints/feedback.py::test_x\n", encoding="utf-8")
    changed = tmp_path / "changed.txt"
    changed.write_text("blueprints/feedback.py\n", encoding="utf-8")
    code = deploy_report.main([
        "assess", "--pr", "29", "--sha", SHA, "--rc", "21", "--stage", "verify",
        "--log", str(log), "--pr-body", str(pr_body), "--repo", str(tmp_path),
        "--changed-files", str(changed),
    ])
    assert code == 0


def test_assess_reports_drifted_exit_one(tmp_path, fake_curl):
    pr_body = tmp_path / "pr.md"
    pr_body.write_text(_PR_BODY, encoding="utf-8")
    code = deploy_report.main([
        "assess", "--pr", "29", "--sha", SHA, "--rc", "23", "--stage", "switch",
        "--pr-body", str(pr_body), "--repo", str(tmp_path),
    ])
    assert code == 1


# ---------------------------------------------------------------- pipeline 集成

@pytest.fixture()
def pipeline_sandbox(tmp_path, monkeypatch):
    """真 pipeline + 真 deploy_report/triage + 假四阶段脚本。"""
    sb = tmp_path / "bg"
    sb.mkdir()
    for name in ("deploy_state.py", "deploy_pipeline.sh",
                 "deploy_report.py", "deploy_triage_pure.py"):
        shutil.copy2(BG_DIR / name, sb / name)
        (sb / name).chmod(0o755)
    calls = tmp_path / "calls.txt"
    for name, rc in (("up.sh", 0), ("verify.sh", 1), ("promote.sh", 0), ("switch.sh", 0)):
        path = sb / name
        path.write_text(
            "#!/usr/bin/env bash\n"
            f'echo "{name} $*" >> "{calls}"\n'
            "echo 'FAILED blueprints/feedback.py::test_x'\n"
            f"exit {rc}\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    inbox = tmp_path / "inbox.jsonl"
    # 子进程继承 os.environ；在夹具层统一隔离，任何新增 pipeline 用例都不会
    # 因漏写单个 env 而落到生产默认收件箱（Issue #38）。
    monkeypatch.setenv("DAILYCHECK_PROD_AGENT_INBOX", str(inbox))
    return {"dir": sb, "state": state, "tmp": tmp_path, "inbox": inbox}


def test_pipeline_writes_back_and_keeps_exit_code(pipeline_sandbox, tmp_path):
    """verify 失败 → 退出码仍是 21，同时 PR 上出现一条部署报告。

    退出码是这条链路的对外契约（上层按 20~24 判断死在哪），回写不能改它。
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "fakecurl"
    fake.write_text(_FAKE_CURL.format(py=sys.executable), encoding="utf-8")
    fake.chmod(0o755)
    curl_log = tmp_path / "curl.jsonl"

    env = os.environ.copy()
    env.update({
        "DAILYCHECK_DEPLOY_STATE_DIR": str(pipeline_sandbox["state"]),
        "DAILYCHECK_PIPELINE_LOG": str(tmp_path / "pipeline.log"),
        "DAILYCHECK_PIPELINE_TICK_SEC": "1",
        "DAILYCHECK_CURL": str(fake),
        "FAKE_CURL_LOG": str(curl_log),
        "FAKE_CURL_PR": json.dumps({"body": _PR_BODY}),
        "DAILYCHECK_GITEA_TOKEN": "tok",
        "FEEDBACK_GITEA_API": "http://gitea.test/api/v1",
    })
    env.pop("DAILYCHECK_DEPLOY_UNLOCKED", None)
    proc = subprocess.run(
        ["bash", str(pipeline_sandbox["dir"] / "deploy_pipeline.sh"), "42", SHA],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert proc.returncode == 21, proc.stdout + proc.stderr
    assert curl_log.exists(), "cleanup 未触发 PR 回写"
    inbox_rows = _inbox(tmp_path)
    assert len(inbox_rows) == 1, inbox_rows
    assert inbox_rows[0]["pr_number"] == 42
    assert inbox_rows[0]["delivery_id"] == "deploy-42-abcdef123456-21"
    rows = [json.loads(ln) for ln in curl_log.read_text().splitlines() if ln]
    assert [r for r in rows if "gitea.test" in r["url"]]


def _replace_reporter_with_inbox_capture(pipeline_sandbox, capture: Path) -> None:
    """让 pipeline 只回显实际传给 deploy_report 的收件箱路径。"""
    reporter = pipeline_sandbox["dir"] / "deploy_report.py"
    reporter.write_text(
        "import os\n"
        "from pathlib import Path\n"
        f"Path({str(capture)!r}).write_text("
        'os.environ.get("DAILYCHECK_PROD_AGENT_INBOX", ""), encoding="utf-8")\n',
        encoding="utf-8",
    )
    reporter.chmod(0o755)


@pytest.mark.parametrize("ambient_inbox", [None, deploy_report.DEFAULT_PROD_AGENT_INBOX])
def test_state_temp_redirects_production_default_inbox(
    pipeline_sandbox, tmp_path, ambient_inbox
):
    """临时状态目录不得再把部署事件投进生产默认收件箱（Issue #38）。

    参数分别覆盖「变量缺失 → reporter 自己取默认值」和「变量已是生产默认值」
    两条泄漏路径；显式自定义路径由下一条用例保证不被误改。
    """
    capture = tmp_path / "captured-inbox.txt"
    _replace_reporter_with_inbox_capture(pipeline_sandbox, capture)
    env = os.environ.copy()
    env.update({
        "DAILYCHECK_DEPLOY_STATE_DIR": str(pipeline_sandbox["state"]),
        "DAILYCHECK_PIPELINE_LOG": str(tmp_path / "pipeline.log"),
        "DAILYCHECK_PIPELINE_TICK_SEC": "1",
    })
    if ambient_inbox is None:
        env.pop("DAILYCHECK_PROD_AGENT_INBOX", None)
    else:
        env["DAILYCHECK_PROD_AGENT_INBOX"] = ambient_inbox

    proc = subprocess.run(
        ["bash", str(pipeline_sandbox["dir"] / "deploy_pipeline.sh"), "42", SHA],
        capture_output=True, text=True, env=env, timeout=120,
    )

    assert proc.returncode == 21, proc.stdout + proc.stderr
    got = capture.read_text(encoding="utf-8").strip()
    assert got == str(pipeline_sandbox["state"] / "inbox.jsonl")
    assert got != deploy_report.DEFAULT_PROD_AGENT_INBOX


def test_state_temp_preserves_custom_inbox(pipeline_sandbox, tmp_path):
    """隔离保护只重定向生产默认值，显式模板路径不得被改写。"""
    capture = tmp_path / "captured-inbox.txt"
    _replace_reporter_with_inbox_capture(pipeline_sandbox, capture)
    custom = tmp_path / "custom-prod-agent-inbox.jsonl"
    env = os.environ.copy()
    env.update({
        "DAILYCHECK_DEPLOY_STATE_DIR": str(pipeline_sandbox["state"]),
        "DAILYCHECK_PIPELINE_LOG": str(tmp_path / "pipeline.log"),
        "DAILYCHECK_PIPELINE_TICK_SEC": "1",
        "DAILYCHECK_PROD_AGENT_INBOX": str(custom),
    })

    proc = subprocess.run(
        ["bash", str(pipeline_sandbox["dir"] / "deploy_pipeline.sh"), "42", SHA],
        capture_output=True, text=True, env=env, timeout=120,
    )

    assert proc.returncode == 21, proc.stdout + proc.stderr
    assert capture.read_text(encoding="utf-8").strip() == str(custom)


def test_pipeline_survives_missing_reporter(pipeline_sandbox, tmp_path):
    """回写脚本缺失时只 WARN，不能把部署结果带坏。"""
    (pipeline_sandbox["dir"] / "deploy_report.py").unlink()
    env = os.environ.copy()
    env.update({
        "DAILYCHECK_DEPLOY_STATE_DIR": str(pipeline_sandbox["state"]),
        "DAILYCHECK_PIPELINE_LOG": str(tmp_path / "pipeline.log"),
        "DAILYCHECK_PIPELINE_TICK_SEC": "1",
    })
    env.pop("DAILYCHECK_DEPLOY_UNLOCKED", None)
    proc = subprocess.run(
        ["bash", str(pipeline_sandbox["dir"] / "deploy_pipeline.sh"), "42", SHA],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert proc.returncode == 21
    assert "deploy_report.py 缺失" in proc.stdout


def test_pipeline_report_can_be_disabled(pipeline_sandbox, tmp_path):
    env = os.environ.copy()
    env.update({
        "DAILYCHECK_DEPLOY_STATE_DIR": str(pipeline_sandbox["state"]),
        "DAILYCHECK_PIPELINE_LOG": str(tmp_path / "pipeline.log"),
        "DAILYCHECK_PIPELINE_TICK_SEC": "1",
        "DAILYCHECK_DEPLOY_REPORT": "0",
    })
    env.pop("DAILYCHECK_DEPLOY_UNLOCKED", None)
    proc = subprocess.run(
        ["bash", str(pipeline_sandbox["dir"] / "deploy_pipeline.sh"), "42", SHA],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert proc.returncode == 21
    assert "已写回" not in proc.stdout

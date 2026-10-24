"""红线门禁的判据测试。

背景（2026-10-24 实测）：`workflow.md` §四 的 6 条红线此前没有任何机验，
PR #13 改了 `.github/workflows/ci.yml`（红线 2）被 Prod Agent 自行合并。
新增的 `check_redlines.py` 把「改了什么」变成判据，本文件钉住它的行为。

特别钉一个已修 bug：路径归一化若用 `lstrip("./")`，会把 `.github/...` 的
前导点一起吃掉，导致红线 2 永远匹配不上 —— 门禁静默失效且无任何报错。
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CHECKER = ROOT / "scripts" / "agent_framework" / "check_redlines.py"

AUTH_LINE = "Owner-Authorization: 2026-10-24 Eric「同意本次改动」"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_redlines_under_test", CHECKER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # 必须先注册进 sys.modules：check_redlines 用了 @dataclass(frozen=True)，
    # 而 dataclasses 在解析注解时要按 __module__ 反查模块，未注册会抛
    # AttributeError: 'NoneType' object has no attribute '__dict__'
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def checker():
    return _load_checker()


# ── 纯函数：classify ─────────────────────────────────────────────────


def test_ci_workflow_change_hits_redline_2(checker):
    hits = checker.classify([".github/workflows/ci.yml"], 0, "")
    assert [h.rule for h in hits] == ["R2"]
    assert ".github/workflows/ci.yml" in hits[0].evidence


def test_dot_slash_prefix_still_matches(checker):
    """防回归：`./.github/...` 必须同样命中，不能被前导点归一化吃掉。"""
    hits = checker.classify(["./.github/workflows/deploy.yml"], 0, "")
    assert [h.rule for h in hits] == ["R2"]


def test_workflow_doc_change_hits_redline_6(checker):
    hits = checker.classify(["docs/agent-framework/workflow.md"], 0, "")
    assert [h.rule for h in hits] == ["R6"]


def test_session_bootstrap_change_hits_redline_6(checker):
    hits = checker.classify(["docs/agent-framework/session-bootstrap.md"], 0, "")
    assert [h.rule for h in hits] == ["R6"]


def test_known_failures_shrink_is_allowed(checker):
    """清单只许收缩：只删行不该被拦。"""
    hits = checker.classify(["tests/known_failures.txt"], 0, "")
    assert hits == []


def test_known_failures_expand_hits_redline_1(checker):
    hits = checker.classify(["tests/known_failures.txt"], 2, "")
    assert [h.rule for h in hits] == ["R1"]
    assert "2 行" in hits[0].evidence


def test_ordinary_files_pass(checker):
    hits = checker.classify(
        ["blueprints/feedback.py", "tests/test_feedback.py", "docs/CHANGELOG.md"],
        0,
        "",
    )
    assert hits == []


def test_pending_marker_without_auth_hits_redline_5(checker):
    hits = checker.classify([], 0, "本次改动需拍板后再动。")
    assert [h.rule for h in hits] == ["R5"]


def test_pending_marker_with_auth_passes(checker):
    body = f"本次改动需拍板。\n\n{AUTH_LINE}\n"
    hits = checker.classify([], 0, body)
    assert hits == []


# ── 授权声明的识别 ───────────────────────────────────────────────────


def test_has_authorization_plain(checker):
    assert checker.has_authorization(AUTH_LINE) == AUTH_LINE


def test_has_authorization_in_list_item(checker):
    body = f"- {AUTH_LINE}\n"
    assert checker.has_authorization(body) == AUTH_LINE


def test_has_authorization_empty_value_is_not_authorization(checker):
    """只写字段名不给内容不算授权 —— 否则一个空壳就能绕门禁。"""
    assert checker.has_authorization("Owner-Authorization:") == ""
    assert checker.has_authorization("Owner-Authorization:   ") == ""


# ── 端到端：真 git 仓库里跑 main() ──────────────────────────────────


@pytest.fixture()
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def run(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            env={
                "PATH": "/usr/bin:/bin:/usr/local/bin",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_SYSTEM": "/dev/null",
                "HOME": str(tmp_path),
            },
        )

    run("init", "-b", "main")
    run("config", "user.email", "gate@test.local")
    run("config", "user.name", "gate-test")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-m", "base")
    return repo, run


def test_main_blocks_redline_change_without_auth(git_repo, tmp_path, monkeypatch):
    repo, run = git_repo
    wf = repo / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text("on: push\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-m", "touch ci")

    body = tmp_path / "body.md"
    body.write_text("## 报告 1/4\n普通正文，无授权。\n", encoding="utf-8")

    monkeypatch.chdir(repo)
    monkeypatch.setattr(sys, "path", [str(CHECKER.parent)] + sys.path)
    module = _load_checker()
    exit_code = module.main(["--base", "HEAD~1", "--body-file", str(body)])
    assert exit_code == 1


def test_main_allows_redline_change_with_auth(git_repo, tmp_path, monkeypatch):
    repo, run = git_repo
    wf = repo / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text("on: push\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-m", "touch ci")

    body = tmp_path / "body.md"
    body.write_text(f"## 报告 1/4\n\n{AUTH_LINE}\n", encoding="utf-8")

    monkeypatch.chdir(repo)
    module = _load_checker()
    exit_code = module.main(["--base", "HEAD~1", "--body-file", str(body)])
    assert exit_code == 0

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


def test_pending_marker_needs_owner_wording(checker):
    """Prod Agent 红线清单写的是「需要 owner 拍板」，与 workflow.md 的「需拍板」不同字面。

    Issue #24 缺陷 2：`"需要 owner 拍板"` 不含 `"需 owner 拍板"`（需 后面是要，不是空格），
    旧 markers 两处措辞都漏拦。
    """
    assert checker.detect_pending_marker("本项属需要 owner 拍板的事项。") == "需要 owner 拍板"


def test_pending_marker_major_change_wording(checker):
    assert checker.detect_pending_marker("重大变更：调整 CI 流水线。") == "重大变更"


def test_negated_pending_marker_is_not_pending(checker):
    """否定语境不算需拍板。

    否则一个正常 PR 写「本次无重大变更」也会红，作者为了过 CI 只能编一行
    授权声明 —— 正是门禁想消除的习惯性绕过。
    """
    assert checker.detect_pending_marker("本次无重大变更，不涉及红线。") == ""
    assert checker.detect_pending_marker("这不是重大变更。") == ""
    assert checker.detect_pending_marker("本次改造不涉及需要 owner 拍板的事项。") == ""


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


# ── 分支落后 main：三点 diff 与两点 diff 的分歧 ──────────────────────


def _git_out(repo: Path, *args: str) -> str:
    """在 fixture 造的仓库里跑 git 并取 stdout。"""
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "HOME": str(repo.parent),
        },
    ).stdout.strip()


def test_lagging_branch_does_not_false_positive(git_repo, tmp_path, monkeypatch):
    """分支落后 main 时，别人改的红线文件不该算在本 PR 头上。

    Issue #24 缺陷 1：脚本原先用 `base..head`（两点）。CI 传的 base 是 base
    分支当时的 tip、head 是 PR head，两者不是祖先关系，两点 diff 会把 main
    上别人合入的改动一起列出来。实测 main `a67c49a` vs 落后分支 `556cade`：
    两点列出 7 个文件（ci.yml / workflow.md / session-bootstrap.md 全在里面），
    三点为空 —— 该分支实际没改任何文件，却会被判命中 3 条红线。
    """
    repo, run = git_repo

    # main 前进一步：别人改了 ci.yml（红线 2）
    wf = repo / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text("on: push\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-m", "main tip: 别人改 ci.yml")
    main_tip = _git_out(repo, "rev-parse", "HEAD")

    # 本分支从同一个 base 分出，只改 README，不碰红线
    run("checkout", "-b", "feature", "HEAD~1")
    (repo / "README.md").write_text("base\nfeature\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-m", "feature: 只改 README")

    body = tmp_path / "body.md"
    body.write_text("## 报告 1/4\n只改文档，不涉及红线。\n", encoding="utf-8")

    monkeypatch.chdir(repo)
    module = _load_checker()
    exit_code = module.main(["--base", main_tip, "--body-file", str(body)])
    assert exit_code == 0, "落后分支不该因 main 上别人的红线改动被拦"


def test_own_redline_change_still_blocked_when_base_ahead(git_repo, tmp_path, monkeypatch):
    """改三点 diff 不能把该拦的漏掉：分支自己改红线仍须 exit 1。"""
    repo, run = git_repo

    # main 前进一步：别人改了 README（非红线）
    (repo / "README.md").write_text("base\nmain tip\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-m", "main tip: 别人改 README")
    main_tip = _git_out(repo, "rev-parse", "HEAD")

    # 本分支从旧 base 分出，自己改了 ci.yml
    run("checkout", "-b", "feature", "HEAD~1")
    wf = repo / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text("on: push\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-m", "feature: 改 ci.yml")

    body = tmp_path / "body.md"
    body.write_text("## 报告 1/4\n改 CI。\n", encoding="utf-8")

    monkeypatch.chdir(repo)
    module = _load_checker()
    exit_code = module.main(["--base", main_tip, "--body-file", str(body)])
    assert exit_code == 1

"""PR 开工规范门禁的判据测试。

背景（2026-10-24 / Issue #26）：Agent 接手 issue 后「怎么开工」此前没有任何
仓库内约束，规矩只活在本地记忆里。`check_pr_hygiene.py` 把其中最硬的三条
（分支前缀、issue 号一致、无号必须声明）变成判据，本文件钉住它的行为。

特别钉一个容易被"优化"掉的点：分支带 issue 号时，body 写 `Part-Of: #N` 也算
合规 —— 否则大 issue 拆多个 PR 时会被逼着写 `Closes`，导致没做完就关单。
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CHECKER = ROOT / "scripts" / "agent_framework" / "check_pr_hygiene.py"

NO_ISSUE_LINE = "No-Issue: 纯框架脚本维护，无对应业务单"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_pr_hygiene_under_test", CHECKER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # dataclass 解析注解要按 __module__ 反查模块，未注册会抛 AttributeError
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def checker():
    return _load_checker()


# ── H1：分支前缀 ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "branch,body",
    [
        ("fix/issue-26-agent-kickoff-rules", "Closes #26\n"),
        ("feat/issue-145-procurement-digest", "Closes #145\n"),
        ("chore/gitea-full-migration", "No-Issue: 迁移收尾\n"),
        ("docs/agent-bootstrap", "No-Issue: 文档\n"),
        ("ci/parallel-reports-gate", "No-Issue: CI\n"),
        ("test/redline-gate", "No-Issue: 测试\n"),
        ("refactor/split-ops-parse", "No-Issue: 重构\n"),
        ("perf/dashboard-render", "No-Issue: 性能\n"),
    ],
)
def test_allowed_prefixes_pass(checker, branch, body):
    """白名单前缀 + 对应声明，整条规范全过。"""
    assert checker.classify(branch, body) == []


@pytest.mark.parametrize(
    "branch,body",
    [
        # 带 issue 号：用 Closes 摘掉 H2
        ("issue-26-agent-kickoff-rules", "Closes #26\n"),  # 少了类型前缀
        ("feature/issue-26-x", "Closes #26\n"),  # 不在白名单
        # 不带号（裸号不认）：用 No-Issue 摘掉 H3
        ("26-agent-kickoff", "No-Issue: 测试\n"),
        ("hotfix/x", "No-Issue: 测试\n"),
        ("main", "No-Issue: 测试\n"),
    ],
)
def test_bad_prefix_hits_h1(checker, branch, body):
    """只留 H1 一个变量，其余规则用声明摘掉。"""
    findings = checker.classify(branch, body)
    assert [f.rule for f in findings] == ["H1"]


def test_dot_slash_prefix_is_not_allowed(checker):
    """`./fix/x` 这种从粘贴带进来的前缀也该拦 —— 分支名不会长得像路径。"""
    findings = checker.classify("./fix/issue-26-x", "Closes #26\n")
    assert "H1" in [f.rule for f in findings]


# ── H2：分支号必须与 body 声明一致 ───────────────────────────────────


def test_branch_issue_with_matching_closes_passes(checker):
    findings = checker.classify("feat/issue-26-agent-kickoff-rules", "Closes #26\n")
    assert findings == []


def test_branch_issue_with_matching_part_of_passes(checker):
    """大 issue 拆多个 PR：Part-Of 不该被当成违规，否则会被逼着提前关单。"""
    findings = checker.classify("feat/issue-26-part-one", "Part-Of: #26\n")
    assert findings == []


def test_mismatched_issue_number_hits_h2(checker):
    findings = checker.classify("feat/issue-26-x", "Closes #25\n")
    assert [f.rule for f in findings] == ["H2"]


def test_branch_issue_without_any_declaration_hits_h2(checker):
    findings = checker.classify("fix/issue-9-key-exposure", "普通正文，没写关联。\n")
    assert [f.rule for f in findings] == ["H2"]


def test_closes_in_list_item_still_counts(checker):
    findings = checker.classify("fix/issue-26-x", "- Closes #26\n")
    assert findings == []


@pytest.mark.parametrize(
    "branch,expected",
    [
        ("fix/issue-26-x", 26),
        ("fix/issue_26_x", 26),
        ("fix/issue26x", 26),
        ("fix/issue-9-key-exposure", 9),
        ("fix/redline-enforcement", None),
        ("feat/store-ordering", None),
    ],
)
def test_branch_issue_number_extraction(checker, branch, expected):
    assert checker.branch_issue_number(branch) == expected


# ── H3：无号分支必须声明 No-Issue ────────────────────────────────────


def test_no_issue_branch_without_declaration_hits_h3(checker):
    findings = checker.classify("fix/redline-enforcement", "普通正文。\n")
    assert [f.rule for f in findings] == ["H3"]


def test_no_issue_branch_with_declaration_passes(checker):
    findings = checker.classify("fix/redline-enforcement", f"{NO_ISSUE_LINE}\n")
    assert findings == []


def test_no_issue_empty_value_is_not_declaration(checker):
    """只写字段名不给理由不算 —— 否则一个空壳就能绕过。"""
    assert checker.has_no_issue_declaration("No-Issue:") == ""
    assert checker.has_no_issue_declaration("No-Issue:   ") == ""


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


def test_main_blocks_bad_branch(git_repo, tmp_path, monkeypatch):
    repo, _run = git_repo
    body = tmp_path / "body.md"
    body.write_text("普通正文。\n", encoding="utf-8")

    monkeypatch.chdir(repo)
    module = _load_checker()
    exit_code = module.main(["--branch", "random-name", "--body-file", str(body)])
    assert exit_code == 1


def test_main_allows_compliant_branch(git_repo, tmp_path, monkeypatch):
    repo, _run = git_repo
    body = tmp_path / "body.md"
    body.write_text("Closes #26\n", encoding="utf-8")

    monkeypatch.chdir(repo)
    module = _load_checker()
    exit_code = module.main(
        ["--branch", "fix/issue-26-x", "--body-file", str(body), "--base", "HEAD", "--head", "HEAD"]
    )
    assert exit_code == 0


def test_main_missing_branch_exits_2(git_repo, tmp_path, monkeypatch):
    """拿不到分支名不能静默放行。"""
    repo, _run = git_repo
    body = tmp_path / "body.md"
    body.write_text("Closes #26\n", encoding="utf-8")

    monkeypatch.chdir(repo)
    module = _load_checker()
    exit_code = module.main(["--branch", "", "--body-file", str(body)])
    assert exit_code == 2


def test_commits_behind_counts(git_repo, monkeypatch):
    repo, run = git_repo
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "HOME": str(repo.parent),
    }
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()

    run("checkout", "-b", "feature")
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-m", "feature commit")

    run("checkout", "main")
    for i in range(3):
        (repo / f"m{i}.txt").write_text("x\n", encoding="utf-8")
        run("add", "-A")
        run("commit", "-m", f"main {i}")

    # commits_behind 内部用不传 cwd 的 git，必须把进程切进仓库
    monkeypatch.chdir(repo)
    module = _load_checker()
    # "feature 落后 main 多少" = 3（main 有而 feature 没有的提交）
    assert module.commits_behind("main", "feature") == 3
    # 反向：main 落后 feature 1（feature 的那个独有提交）
    assert module.commits_behind("feature", "main") == 1
    # 自己比自己不落后
    assert module.commits_behind("main", "main") == 0
    assert base  # 保留变量，说明基线取自 base commit

"""MCP impact policy, real Git merge-base, PR template and required CI wiring."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from scripts.agent_framework import check_mcp_impact as gate

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/agent_framework/check_mcp_impact.py"


def entry(category="不变", tools=None, **extra):
    return {
        "category": category, "tools": tools or [],
        "reason": "修改列表排序，沿用既有查询与权限契约",
        "evidence": ["python -m pytest tests/test_sort.py: 4 passed"], **extra,
    }


def body(*entries):
    return f'{gate.START}\n```json\n{json.dumps({"version": 1, "changes": list(entries)}, ensure_ascii=False)}\n```\n{gate.END}'


@pytest.mark.parametrize("path", [
    "blueprints/items.py", "db/schema.sql", "db/new-file", "shared_calculation.py",
    "future/calculation.py", "config.py", "permissions.py", "config/roles.yaml",
    "migrations/001.sql", "mcp_server/new-tool.txt", "bot/new-permission.json",
    "config.json", "permissions.yaml", gate.LEDGER,
])
def test_business_paths_require_declaration(path):
    assert gate.assess("", [path], {}, {}) == ["missing MCP impact declaration"]


def test_docs_and_tests_only_do_not_require_declaration():
    assert gate.assess("", ["docs/guide.md", "tests/test_view.py"], {}, {}) == []


@pytest.mark.parametrize("category", ["不变", "暂不开放"])
def test_no_new_exposure_is_valid_with_reason_and_test_evidence(category):
    assert gate.assess(body(entry(category)), ["blueprints/new_page.py"], {}, {}) == []


@pytest.mark.parametrize("invalid", [
    "", " ", "待填", "TODO", "TBD", "...", "<理由>", "（理由）",
])
def test_empty_or_placeholder_reason_rejected(invalid):
    assert gate.assess(body(entry(reason=invalid)), ["config.py"], {}, {})


@pytest.mark.parametrize("changes", [
    [entry("other")], [entry("新增")], [entry("更新")],
    [entry(evidence=[])], [entry(evidence=[""])], [entry(evidence=["待填"])],
    [entry(tools=["not_registered"])], [entry(tools=["known", "known"])],
    [entry(tools=["known"]), entry(tools=["known"])],
    [entry(typo="ignored?")], [entry(tools="known")], [entry(category=[])],
])
def test_bad_declarations_fail(changes):
    assert gate.assess(body(*changes), ["config.py"], {"known": {}}, {"known": {}})


@pytest.mark.parametrize("text", [
    f"{gate.START}{{}}{gate.END}", f"{gate.END}{gate.START}",
    f'{gate.START}\n```json\n{{"version":1,"version":1,"changes":[]}}\n```\n{gate.END}',
    f'{gate.START}\n```json\n{{"version":true,"changes":[]}}\n```\n{gate.END}',
    f'{gate.START}\n```json\n{{"version":1,"changes":[]}}\n```\n{gate.END}',
    body(entry()) + body(entry()), body(entry()).replace('"version": 1', '"version": 2'),
])
def test_malformed_or_duplicate_blocks_fail_even_on_docs_only(text):
    assert gate.assess(text, ["docs/guide.md"], {}, {})


def test_new_tool_uses_current_registry_not_old_name_allowlist():
    before = {"known": {"schema": 1}}
    after = {**before, "new_tool": {"schema": 2}}
    assert gate.assess(body(entry("新增", ["new_tool"])), [gate.LEDGER], before, after) == []
    assert gate.assess(body(entry("更新", ["new_tool"])), [gate.LEDGER], before, after)
    assert gate.assess(body(entry("新增", ["known"])), [gate.LEDGER], before, after)


@pytest.mark.parametrize("category", ["不变", "暂不开放"])
def test_contract_change_cannot_silently_claim_no_impact(category):
    before = {"known": {"inputSchema": {"required": []}}}
    after = {"known": {"inputSchema": {"required": ["warehouse"]}}}
    assert gate.assess(body(entry(category)), [], before, after)
    assert gate.assess(body(entry(category, ["known"])), [], before, after)
    assert gate.assess(body(entry("更新", ["known"])), [], before, after) == []


def test_all_contract_changes_must_be_covered_and_unknown_names_fail():
    before = {"a": 1, "b": 1}
    after = {"a": 2, "b": 2}
    errors = gate.assess(body(entry("更新", ["a", "invented"])), [], before, after)
    assert any("unknown tool: invented" in e for e in errors)
    assert any("requires 更新 declaration: b" in e for e in errors)


def test_deletion_and_rename_require_explicit_compatibility_handling():
    before, after = {"old_name": {}}, {"new_name": {}}
    changes = [entry("更新", ["old_name"]), entry("新增", ["new_name"])]
    assert any("compatibility" in e for e in gate.assess(body(*changes), [], before, after))
    changes[0]["compatibility"] = "移除旧名；调用者需迁移到 new_name，相关迁移回归已通过"
    assert gate.assess(body(*changes), [], before, after) == []
    assert gate.assess(body(changes[1]), [], before, after)
    assert gate.assess(body(entry("更新", ["old_name"], compatibility="已确认调用方迁移")), [], before, {}) == []


def git(root, *args):
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=True)
    return result.stdout.strip()


def commit(root, message):
    git(root, "add", ".")
    git(root, "commit", "-qm", message)
    return git(root, "rev-parse", "HEAD")


def write_ledger(root, records):
    target = root / gate.LEDGER
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"schema_version": 1, "tools": records}), encoding="utf-8")


@pytest.fixture()
def repo(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "user.name", "MCP gate test")
    write_ledger(tmp_path, [{"name": "known", "inputSchema": {}}])
    (tmp_path / "config.py").write_text("value = 1\n", encoding="utf-8")
    commit(tmp_path, "base")
    return tmp_path


def cli(repo, text, base="main"):
    # Keep the PR body outside the fixture Git tree so it cannot affect the diff.
    path = repo.parent / f"{repo.name}-body.md"
    path.write_text(text, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--base", base, "--body-file", str(path)],
        cwd=repo, text=True, capture_output=True, check=False,
    )


def test_real_git_uses_merge_base_when_main_advances(repo):
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-qb", "feature")
    (repo / "config.py").write_text("value = 2\n", encoding="utf-8")
    commit(repo, "feature business change")
    git(repo, "checkout", "-q", "main")
    write_ledger(repo, [{"name": "known", "inputSchema": {"changed_by_other_pr": True}}])
    commit(repo, "unrelated main change")
    git(repo, "checkout", "-q", "feature")
    result = cli(repo, body(entry()))
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"merge-base={base}" in result.stdout
    assert cli(repo, "").returncode == 1


def test_real_git_added_tool_and_schema_change_rejections(repo):
    git(repo, "checkout", "-qb", "feature")
    write_ledger(repo, [{"name": "known", "inputSchema": {"required": ["warehouse"]}}, {"name": "new_tool"}])
    commit(repo, "schema and new tool")
    assert cli(repo, body(entry())).returncode == 1
    result = cli(repo, body(entry("更新", ["known"]), entry("新增", ["new_tool"])))
    assert result.returncode == 0, result.stdout + result.stderr


def test_real_git_deletion_and_rename_rejections(repo):
    git(repo, "checkout", "-qb", "feature")
    write_ledger(repo, [{"name": "renamed"}])
    commit(repo, "rename tool")
    assert cli(repo, body(entry("新增", ["renamed"]))).returncode == 1
    result = cli(repo, body(entry("新增", ["renamed"]), entry("更新", ["known"], compatibility="旧调用者迁移到 renamed，迁移测试通过")))
    assert result.returncode == 0, result.stdout + result.stderr


def test_real_git_rename_out_of_business_path_still_triggers(repo):
    git(repo, "checkout", "-qb", "feature")
    git(repo, "mv", "config.py", "notes.txt")
    commit(repo, "move business file")
    assert "config.py" in gate.changed_paths(repo, "main", "HEAD")
    assert cli(repo, "").returncode == 1


def test_bad_base_missing_head_ledger_and_malformed_ledger_fail_closed(repo):
    assert cli(repo, body(entry()), "does-not-exist").returncode == 2
    git(repo, "checkout", "-qb", "feature")
    (repo / gate.LEDGER).unlink()
    commit(repo, "missing ledger")
    assert cli(repo, body(entry())).returncode == 2
    write_ledger(repo, [{"name": "known"}, {"name": "known"}])
    commit(repo, "duplicate tools")
    assert cli(repo, body(entry())).returncode == 2


def test_template_requires_author_to_replace_placeholder_assessment():
    template = (ROOT / ".github/PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8")
    assert template.count(gate.START) == template.count(gate.END) == 1
    assert gate.assess(template, ["blueprints/new.py"], {}, {})
    assert not gate.assess(template.replace("待填：为什么本 PR 不改变 MCP", "只改页面样式，沿用既有工具契约").replace("待填：测试命令与结果", "python -m pytest tests/test_page.py: 3 passed"), ["blueprints/new.py"], {}, {})


def test_existing_required_job_runs_consistency_and_impact_checks(tmp_path):
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert set(re.findall(r"^  ([a-z-]+):$", workflow.split("jobs:\n", 1)[1], re.M)) == {"test", "agent-framework-reports"}
    reports = workflow.split("  agent-framework-reports:", 1)[1]
    step = reports.split("      - name: MCP contracts and PR impact gate\n", 1)[1].split("      - name:", 1)[0]
    assert "BASE_SHA: ${{ github.event.pull_request.base.sha }}" in step
    assert "PR_BODY: ${{ github.event.pull_request.body }}" in step
    run = textwrap.dedent(step.split("        run: |\n", 1)[1]).replace("${CI_ROOT}/venv/bin/python", "python")
    assert "python -m scripts.mcp_contracts --check" in run
    assert 'check_mcp_impact.py --base "${BASE_SHA}" --body-file' in run
    # Execute the actual step with command stubs: neither failed check may be swallowed.
    stub = tmp_path / "python"
    stub.write_text('#!/bin/sh\ncase "$*" in\n  *scripts.mcp_contracts*) exit "${CONTRACT_EXIT}";;\n  *check_mcp_impact.py*) exit "${IMPACT_EXIT}";;\n  *) exit 90;;\nesac\n', encoding="utf-8")
    stub.chmod(0o755)
    run = run.replace("${CI_ROOT}/pr", str(tmp_path / "pr"))
    for contract_exit, impact_exit in [(0, 0), (1, 0), (0, 1)]:
        env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "BASE_SHA": "main", "PR_BODY": body(entry()), "CONTRACT_EXIT": str(contract_exit), "IMPACT_EXIT": str(impact_exit)}
        result = subprocess.run(["bash", "-c", run], env=env, cwd=tmp_path, check=False)
        assert (result.returncode == 0) == (contract_exit == impact_exit == 0)


def test_runtime_schema_drift_cannot_hide_behind_unchanged_ledger():
    # Mutate the actual registry only inside a fresh process. The unchanged
    # committed artifact must be rejected by the first half of the CI gate.
    code = """
from mcp_server.protocol.registry import REGISTRY
from scripts.mcp_contracts import main
REGISTRY[0].tool.inputSchema['properties']['hidden_new_input'] = {'type': 'string'}
raise SystemExit(main(['--check']))
"""
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, text=True, capture_output=True, check=False)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "registry.json" in result.stdout


def test_default_base_is_origin_main_and_missing_reference_fails_closed(repo, monkeypatch):
    monkeypatch.chdir(repo)
    monkeypatch.delenv("BASE_SHA", raising=False)
    monkeypatch.setenv("PR_BODY", body(entry()))
    monkeypatch.delenv("PR_BODY_FILE", raising=False)
    assert gate.main([]) == 2
    git(repo, "update-ref", "refs/remotes/origin/main", "main")
    assert gate.main([]) == 0


@pytest.mark.parametrize("path", [
    "requirements.txt", "requirements-dev.txt", "requirements.in", "requirements-prod.in",
    "requirements/production.txt", "deploy/requirements.txt", "constraints.txt",
    "constraints/python312.txt", "pyproject.toml", "setup.cfg", "uv.lock", "poetry.lock",
    "Pipfile", "Pipfile.lock", "pdm.lock", "pylock.toml", ".python-version", "environment.yml",
])
def test_dependency_manifests_require_assessment(path):
    assert gate.assess("", [path], {}, {}) == ["missing MCP impact declaration"]


@pytest.mark.parametrize("manifest", ["requirements.txt", "pyproject.toml"])
def test_real_git_dependency_only_change_requires_declaration(repo, manifest):
    target = repo / manifest
    old = 'mcp>=1.0.0,<2\n' if manifest == "requirements.txt" else '[project]\ndependencies = ["mcp>=1.0.0,<2"]\n'
    target.write_text(old, encoding="utf-8")
    commit(repo, "base SDK manifest")
    git(repo, "checkout", "-qb", "feature")
    target.write_text(old.replace("1.0.0", "1.30.0"), encoding="utf-8")
    commit(repo, "only SDK dependency change")
    assert gate.changed_paths(repo, "main", "HEAD") == [manifest]
    assert cli(repo, "").returncode == 1
    result = cli(repo, body(entry(reason="升级 MCP1.x 依赖下限，工具声明和契约保持一致", evidence=["MCP1.x 协议回归通过；台账 --check 通过"])))
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("name", ["FutureRead", "_private"])
def test_real_git_registered_sdk_valid_name_preserved(repo, name):
    from mcp.types import Tool

    assert Tool(name=name, inputSchema={"type": "object"}).name == name
    git(repo, "checkout", "-qb", "feature")
    write_ledger(repo, [{"name": "known", "inputSchema": {}}, {"name": name}])
    commit(repo, "registered SDK-valid tool name")
    result = cli(repo, body(entry("新增", [name])))
    assert result.returncode == 0, result.stdout + result.stderr
    assert cli(repo, body(entry("新增", ["UnknownTool"]))).returncode == 1


@pytest.mark.parametrize("names", [[""], ["   "], [12], ["known", "known"]])
def test_names_still_require_nonempty_strings_and_no_duplicates(names):
    assert gate.assess(body(entry(tools=names)), [], {"known": {}}, {"known": {}})

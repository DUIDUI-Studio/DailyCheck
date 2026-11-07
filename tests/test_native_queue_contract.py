"""Owned real Git, full tiny fixture CI, fenced shell, SQLite and loopback HTTP.

TEST ONLY: synthetic producer/App/policy/review facts; external blue-green
facilities are owned stand-ins. The valid observation proof uses >=600 real
monotonic seconds once per module, never a fake clock or shortened grant.
"""
from __future__ import annotations

import copy
import fcntl
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from scripts.ci.merge_group import QueueReader, commit, digest, git, validate_manifest
from scripts.hosting import AccessRole, HostingClient, HostingConfig, HostingError
from scripts.prod_agent.github_readiness import validate_native_ci
from scripts.prod_agent.native_queue import (
    LoopbackFacts,
    LoopbackFixtureScope,
    LoopbackGate,
    NativeQueue,
    ProducerPolicy,
    candidate,
    deploy_state,
    head_admission,
    publication_authority,
)

ROOT = Path(__file__).resolve().parents[1]
CODE = "DUIDUI-Studio/DailyCheck"
PRIVATE = "SYNTHETIC_PRIVATE_NATIVE_164"


def command(argv, *, cwd=None, env=None, **kwargs):
    return subprocess.run([str(a) for a in argv], cwd=cwd, env=env, capture_output=True,
                          text=True, timeout=kwargs.pop("timeout", 45), **kwargs)


def checked(argv, **kwargs):
    result = command(argv, **kwargs)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


class Host:
    def __init__(self):
        self.facts = {}
        self.posts = []
        self.routes = {}
        self.read_auth = None
        self.auth_seen = []
        self.comments = []
        self.public_head = ""
        self.drop = False
        self.delay = threading.Event()
        self.delay.set()
        self.arrived = threading.Event()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def handler(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path.startswith("/repos/") and owner.read_auth is not None:
                    assert self.headers.get("Authorization") == owner.read_auth
                    owner.auth_seen.append(self.headers["Authorization"])
                if self.path in owner.routes:
                    self.reply(200, owner.routes[self.path])
                elif path == f"/repos/{CODE}":
                    self.reply(200, {"id": 1448986421, "full_name": CODE, "private": False, "visibility": "public",
                        "has_issues": True, "archived": False, "disabled": False,
                        "url": "https://api.github.com"+path, "html_url": "https://github.com/"+CODE})
                elif path == f"/repos/{CODE}/pulls/1":
                    self.reply(200, {"number": 1, "html_url": f"https://github.com/{CODE}/pull/1",
                        "head": {"sha": owner.public_head}, "base": {"repo": {"id": 1448986421}},
                        "body": "## 报告 1/4\n| R-001 | fixture | ✅ |\n## Checklist\n## 生产侧事项"})
                elif path == f"/repos/{CODE}/issues/1/comments":
                    self.reply(200, owner.comments)
                elif self.path.startswith("/facts/"):
                    self.reply(200, owner.facts[self.path.rsplit("/", 1)[-1]])
                else:
                    self.reply(200, {})

            def do_POST(self):
                value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == f"/repos/{CODE}/issues/1/comments":
                    comment = {"id": len(owner.comments)+1001, "issue_url": f"https://api.github.com/repos/{CODE}/issues/1", **value}
                    owner.comments.append(comment)
                    self.reply(201, comment)
                    return
                assert self.headers.get("X-DailyCheck-Test-Scope") == "native-queue"
                if self.path == "/runtime":
                    owner.facts[value["sha"]]["production_sha"] = value["sha"]
                    self.reply(200, {})
                    return
                assert self.path == "/checks"
                owner.posts.append(value)
                owner.arrived.set()
                assert owner.delay.wait(25), "test did not release HTTP child"
                result = {**value, "id": 901, "app_id": 76543, "installation_id": 65432}
                if value["head_sha"] in owner.facts:
                    owner.facts[value["head_sha"]]["prod_check"] = result
                if owner.drop:
                    self.connection.close()
                    return
                self.reply(201, result)

            def reply(self, status, value):
                raw = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except BrokenPipeError:
                    pass

        return Handler

    def close(self):
        self.delay.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


def shell(path, text):
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + text + "\n")
    path.chmod(0o755)


def tiny_full_ci(repo: Path, root: Path, manifest: dict, policy: ProducerPolicy) -> list[dict]:
    """Execute both literal workflow full-pytest paths on every H and G.

    Only the tiny synthetic repository's entire tests/ is executed, never the
    DailyCheck full suite. Actual workflow shell capture, gate and handoff run.
    """
    workflow = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    runs = []
    subjects = [("G", None, manifest["group_sha"], manifest["members"])]
    subjects += [("H", m["number"], m["head_sha"], [m]) for m in manifest["members"]]
    for index, (phase, pr, head, members) in enumerate(subjects, 1):
        git(repo, "checkout", "--detach", head)
        jobs = []
        for offset, context in enumerate(("test", "agent-framework-reports"), 1):
            jobroot = root / f"{head}-{context}"
            jobroot.mkdir(parents=True, exist_ok=True)
            (jobroot / "tmp").mkdir(exist_ok=True)
            (jobroot / "venv/bin").mkdir(parents=True, exist_ok=True)
            (jobroot / "venv/bin/python").symlink_to(sys.executable)
            env = {**os.environ, "CI_ROOT": str(jobroot), "CI_REPOSITORY": CODE,
                   "CI_RUN_ID": str(index), "CI_RUN_ATTEMPT": "1", "CI_EVENT": "merge_group" if phase == "G" else "pull_request",
                   "CI_EVENT_SHA": head if phase == "G" else manifest["group_sha"],
                   "CI_HEAD_SHA": "" if phase == "G" else head, "CI_GROUP_SHA": head if phase == "G" else "",
                   "CI_BASE_SHA": manifest["creation_base_sha"], "GITHUB_SERVER_URL": "https://github.com",
                   "PYTHONPATH": str(ROOT), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "TMPDIR": str(jobroot / "tmp"),
                   "CI_PYTEST_INVOCATION_LOG": str(root / "pytest-invocations.jsonl")}
            manifest_path = jobroot / "members.json"
            manifest_path.write_text(json.dumps(manifest))
            env["CI_MEMBER_MANIFEST"] = str(manifest_path)
            binding_file = jobroot / "binding.json"
            checked([sys.executable, ROOT / "scripts/ci/candidate_binding.py", binding_file], cwd=repo, env=env)
            steps = {s["name"]: s for s in workflow["jobs"][context]["steps"]}
            checked(["bash", "-c", steps["Pytest (full suite, including MCP)"]["run"]], cwd=repo, env=env)
            checked(["bash", "-c", steps["Zero-new-failure gate"]["run"]], cwd=repo, env=env)
            checked([sys.executable, ROOT / "scripts/ci/pytest_summary.py", jobroot / "pytest-output.txt",
                     "--evidence-file", jobroot / "pytest-evidence.json", "--binding-file", binding_file,
                     "--output-file", jobroot / "output", "--envelope-output", jobroot / "envelope.json"], cwd=repo, env=env)
            envelope = json.loads((jobroot / "envelope.json").read_text())
            assert envelope["evidence"]["session"]["selected"] == 2
            obligations = {"full_suite": True, "members": [
                {key: m[key] for key in ("number", "head_sha", "body_sha256")} for m in members]}
            if context == "agent-framework-reports":
                # Separate real gate-adapter execution is tested below; these
                # synthetic producer facts are not installation authority.
                obligations.update({k: True for k in ("report01", "report02", "report03", "report04", "mcp_contracts",
                                   "mcp_impact", "redlines", "hygiene", "upstream_success")})
            jobs.append({"id": index*100+offset, "name": context, "run_id": index, "attempt": 1,
                         "head_sha": head, "status": "completed", "conclusion": "success",
                         "check": {"id": index*1000+offset, "name": context, "head_sha": head,
                                   "suite_id": index*10, "app_id": policy.actions_app_id, "app_slug": "github-actions",
                                   "status": "completed", "conclusion": "success"},
                         "envelope": envelope, "obligations": obligations,
                         "artifact_sha256": digest({"envelope": envelope, "obligations": obligations})})
        runs.append({"phase": phase, "pr": pr,
                     "workflow": {"id": policy.workflow_id, "path": ".github/workflows/ci.yml", "blob_sha": policy.workflow_blob_sha},
                     "run": {"id": index, "workflow_id": policy.workflow_id, "attempt": 1, "event": "merge_group" if phase == "G" else "pull_request",
                             "head_sha": head, "repository_id": 1448986421, "repository_node_id": "R_kgDOVl3HNQ", "status": "completed",
                             "conclusion": "success", "suite_id": index*10, "historical_base_sha": manifest["creation_base_sha"]},
                     "latest_run_id": index, "latest_attempt": 1, "jobs": jobs})
    git(repo, "checkout", "--detach", manifest["group_sha"])
    return runs


@pytest.fixture(scope="module")
def native_base(tmp_path_factory):
    root = tmp_path_factory.mktemp("native164")
    (root / ".native-queue-fixture").write_text("synthetic-owned-fixture\n")
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Native fixture")
    git(repo, "config", "user.email", "native@example.invalid")
    bg = repo / "scripts/blue_green"
    bg.mkdir(parents=True)
    for name in ("deploy_state.py", "deploy_pipeline.sh"):
        shutil.copy2(ROOT / "scripts/blue_green" / name, bg / name)
    (repo / ".github/workflows").mkdir(parents=True)
    shutil.copy2(ROOT / ".github/workflows/ci.yml", repo / ".github/workflows/ci.yml")
    (repo / "scripts/ci").mkdir()
    for name in ("check_known_failures.py",):
        shutil.copy2(ROOT / "scripts" / name, repo / "scripts" / name)
    (repo / "tests").mkdir()
    (repo / "tests/known_failures.txt").write_text("")
    (repo / "tests/conftest.py").write_text(
        "import json,os,subprocess\n"
        "def pytest_sessionstart(session):\n"
        " path=os.environ.get('CI_PYTEST_INVOCATION_LOG')\n"
        " if path:\n"
        "  sha=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()\n"
        "  with open(path,'a') as output: output.write(json.dumps({'sha':sha,'pid':os.getpid()})+'\\n')\n")
    (repo / "tests/test_fixture.py").write_text("def test_one(): assert 2+2 == 4\ndef test_two(): assert 'G' != 'H'\n")
    ledger = repo / "docs/integrations/dailycheck-mcp/registry.json"
    ledger.parent.mkdir(parents=True)
    shutil.copy2(ROOT / "docs/integrations/dailycheck-mcp/registry.json", ledger)
    (repo / ".gitignore").write_text("*.db\n__pycache__/\n.pytest_cache/\n")
    calls = root / "calls"
    for name in ("up.sh", "verify.sh", "promote.sh", "switch.sh"):
        text = f'echo "{name} $*" >> "{calls}"\n'
        if name == "up.sh":
            text += f'"{sys.executable}" -c \'import sqlite3; from pathlib import Path; p=Path("{root}"); a=sqlite3.connect(p/"blue.db"); a.execute("create table if not exists fixture(n integer)"); a.commit(); b=sqlite3.connect(p/"green.db"); a.backup(b)\'\n'
        if name == "verify.sh":
            text += f'"{sys.executable}" -m pytest tests/ -q --basetemp="{root}/verification-pytest"\n'
        if name == "switch.sh":
            text += f'''"{sys.executable}" -c 'import json,os,urllib.request; sha=os.environ["NATIVE_FIXTURE_G"]; r=urllib.request.Request(os.environ["NATIVE_FIXTURE_URL"]+"/runtime",data=json.dumps({{"sha":sha}}).encode(),headers={{"X-DailyCheck-Test-Scope":"native-queue"}}); urllib.request.urlopen(r).read()'\n'''
        shell(bg / name, text)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "fixture base")
    base = git(repo, "rev-parse", "HEAD")
    for number in (1, 2):
        git(repo, "checkout", "-q", "-b", f"feat/issue-{number}-fixture", base)
        (repo / f"member{number}").write_text(str(number))
        git(repo, "add", ".")
        git(repo, "commit", "-qm", f"member {number}")
    h2 = git(repo, "rev-parse", "HEAD")
    h1 = git(repo, "rev-parse", "feat/issue-1-fixture")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "--no-ff", "-qm", "G1", h1)
    g1 = git(repo, "rev-parse", "HEAD")
    git(repo, "merge", "--no-ff", "-qm", "G2 lookahead", h2)
    g2 = git(repo, "rev-parse", "HEAD")
    # Owned bare objects/ref evidence. Never copy the source's remote/userinfo.
    checked(["git", "clone", "-q", "--bare", repo, root / "host.git"])
    git(root / "host.git", "update-ref", "refs/heads/main", base)
    workflow_blob = git(repo, "rev-parse", "HEAD:.github/workflows/ci.yml")
    policy = ProducerPolicy(15368, 51, workflow_blob, 76543, 65432)
    members = [{"number": n, "node_id": f"PR_{n}", "head_sha": h, "base_sha": base,
                "head_ref": f"feat/issue-{n}-fixture", "body_sha256": digest(f"body{n}"),
                "queue_entry_id": f"entry{n}", "position_at_creation": n, "integration_sha": g}
               for n, h, g in ((1, h1, g1), (2, h2, g2))]
    manifests = []
    for i, group in enumerate((g1, g2), 1):
        tree, parents = commit(repo, group)
        manifests.append({"version": 1, "repository": CODE, "repository_id": 1448986421,
                          "repository_node_id": "R_kgDOVl3HNQ", "queue_id": "queue",
                          "subject_entry_id": f"entry{i}", "subject_pr_number": i,
                          "creation_base_sha": base, "group_sha": group, "tree_sha": tree,
                          "parents": parents, "expected_predecessor_sha": parents[0],
                          "group_ref": f"refs/heads/opaque-queue-{i}", "event_delivery_id": f"event{i}",
                          "members": members[:i]})
    bindings = {"version": 2, "bindings": [{"pr": n, "public": deploy_state.PUBLIC_CODE_HOSTING,
                "source": {"provider": "gitea", "host": "fixture.invalid", "repository_id": 77,
                           "repository": PRIVATE + "/source", "kind": "issue", "number": n}}
               for n in (1, 2)]}
    for i, entry in enumerate(bindings["bindings"]):
        entry["source_revision"] = {"provider": "gitea", "host": "source.fixture.invalid", "repository_id": 88,
                                    "repository": PRIVATE + "/code", "sha": h1 if i == 0 else h2,
                                    "source_public_map_sha256": digest("fixture-map")}
    seed = root / "seed"
    seed.mkdir()
    (seed / "bindings.json").write_text(json.dumps(bindings))
    privates = [{"pr": n, "source": bindings["bindings"][n-1]["source"], "source_revision": bindings["bindings"][n-1]["source_revision"],
                 "binding_sha256": publication_authority.resolve_source_binding(seed, n).binding_sha256} for n in (1, 2)]
    host = Host()
    scope = LoopbackFixtureScope(root, f"http://127.0.0.1:{host.server.server_port}")
    values = [candidate(repo, m, privates[:i], policy) for i, m in enumerate(manifests, 1)]
    cis = [tiny_full_ci(repo, root / f"ci{i}", m, policy) for i, m in enumerate(manifests, 1)]
    for value, ci in zip(values, cis, strict=True):
        manifest = value["manifest"]
        host.facts[manifest["group_sha"]] = {"repository_id": 1448986421, "repository_node_id": "R_kgDOVl3HNQ", "repository": CODE,
            "candidate_sha256": digest(value), "policy_sha256": policy.sha256, "manifest": manifest,
            "dequeued": False, "front_entry_id": "entry1", "main_sha": base, "production_sha": base,
            "ci": ci, "review": {"role": "independent_prod", "verdict": "pass", "candidate_sha256": digest(value),
                                  "reviewer": "synthetic-independent", "writer": "synthetic-writer", "record_sha256": digest("review")},
            "merged_predecessors": [], "incident": False, "observation_complete": True,
            "merged": False, "merge_sha": None, "merged_pr": None, "prod_check": {}}
    journal = root / "journalctl"
    shell(journal, "exit 0")
    yield {"root": root, "repo": repo, "base": base, "h1": h1, "h2": h2, "g1": g1, "g2": g2,
           "host": host, "scope": scope, "values": values, "policy": policy, "bindings": bindings,
           "journal": journal, "calls": calls}
    host.close()


def fresh(base, name, predecessor=None):
    directory = base["root"] / name
    directory.mkdir()
    (directory / "bindings.json").write_text(json.dumps(base["bindings"]))
    runner = NativeQueue(base["repo"], directory, base["policy"], LoopbackFacts(base["scope"]), LoopbackGate(base["scope"]))
    runner.initialize_fixture(base["scope"])
    state = deploy_state.load_state(directory)
    state["deployed"] = {"sha": predecessor or base["base"]}
    deploy_state.save_state(directory, state)
    return runner


@contextmanager
def pipeline_environment(base, monkeypatch, *, tick):
    monkeypatch.setenv("PYTHON", sys.executable)
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("DAILYCHECK_PIPELINE_LOG", str(base["root"] / "pipeline.log"))
    monkeypatch.setenv("DAILYCHECK_DEPLOY_REPORT", "0")
    monkeypatch.setenv("DAILYCHECK_DASHBOARD_HOOK", "0")
    monkeypatch.setenv("DAILYCHECK_PIPELINE_TICK_SEC", str(tick))
    monkeypatch.setenv("DAILYCHECK_PIPELINE_PROBE_URL", base["scope"].endpoint + "/probe")
    monkeypatch.setenv("DAILYCHECK_PIPELINE_JOURNALCTL", str(base["journal"]))
    monkeypatch.setenv("NATIVE_FIXTURE_URL", base["scope"].endpoint)
    monkeypatch.setenv("NATIVE_FIXTURE_G", base["g1"])
    yield


def test_real_git_domains_and_lookahead_ci(native_base):
    base = native_base
    assert len(commit(base["repo"], base["h1"])[1]) == 1
    assert len(commit(base["repo"], base["g1"])[1]) == 2
    assert commit(base["repo"], base["g2"])[1][0] == base["g1"]
    for value in base["values"]:
        manifest = value["manifest"]
        checks = validate_native_ci(base["repo"], manifest, base["policy"], base["host"].facts[manifest["group_sha"]]["ci"])
        for member in manifest["members"]:
            hchecks = [c for c in checks if c["phase"] == "H" and c["pr"] == member["number"]]
            assert head_admission(base["repo"], member["head_sha"], hchecks, base["policy"])["deployment"] is False
    with pytest.raises(ValueError, match="multi-parent"):
        head_admission(base["repo"], base["g1"], [], base["policy"])
    collision = copy.deepcopy(base["values"][1]["manifest"])
    collision["members"][-1]["head_sha"] = base["g1"]
    with pytest.raises(ValueError):
        validate_manifest(base["repo"], collision)
    assert base["host"].posts == []


def test_lookahead_waits_for_exact_live_predecessor(native_base):
    base = native_base
    runner = fresh(base, "lookahead", base["g1"])
    value = base["values"][1]
    facts = base["host"].facts[base["g2"]]
    with pytest.raises(ValueError, match="predecessor"):
        runner.inspect(value, front=True)
    old = copy.deepcopy(facts)
    facts.update(front_entry_id="entry2", main_sha=base["g1"], production_sha=base["g1"],
                 merged_predecessors=[{"entry_id": "entry1", "pr": 1, "merged": True,
                                       "merge_sha": base["g1"], "anchored_sha": base["g1"]}])
    try:
        before = digest(facts["ci"])
        runner.inspect(value, front=True)
        assert facts["manifest"]["creation_base_sha"] != facts["main_sha"]
        assert digest(facts["ci"]) == before
        rebuilt = copy.deepcopy(value)
        rebuilt["manifest"]["group_sha"] = base["g1"]
        with pytest.raises(ValueError):
            runner.inspect(rebuilt, front=True)
    finally:
        facts.clear()
        facts.update(old)


@pytest.mark.parametrize("damage", ["attempt", "app", "workflow", "member", "skipped", "binding", "evidence", "missing", "upstream"])
def test_authentic_producers_fail_closed(native_base, damage):
    base = native_base
    value = base["values"][0]
    ci = copy.deepcopy(base["host"].facts[base["g1"]]["ci"])
    run, job = ci[0]["run"], ci[0]["jobs"][1]
    if damage == "attempt":
        ci[0]["latest_attempt"] += 1
    elif damage == "app":
        job["check"]["app_id"] += 1
    elif damage == "workflow":
        ci[0]["workflow"]["blob_sha"] = "0"*40
    elif damage == "member":
        job["obligations"]["members"][0]["body_sha256"] = "0"*64
    elif damage == "skipped":
        job["conclusion"] = job["check"]["conclusion"] = "skipped"
    elif damage == "binding":
        job["envelope"]["binding"]["CI_BASE_SHA"] = "0"*40
    elif damage == "evidence":
        job["envelope"]["evidence"]["native_exit"] = 2
    elif damage == "missing":
        ci[0]["jobs"].pop()
    else:
        job["obligations"]["upstream_success"] = False
    with pytest.raises((ValueError, KeyError)):
        validate_native_ci(base["repo"], value["manifest"], base["policy"], ci)
    assert run["head_sha"] == base["g1"]


def test_short_real_observation_and_skipped_verification_cannot_grant(native_base, monkeypatch):
    base = native_base
    runner = fresh(base, "short")
    facts = base["host"].facts[base["g1"]]
    old = copy.deepcopy(facts)
    try:
        with pipeline_environment(base, monkeypatch, tick=1), runner.fence():
            runner.start(base["values"][0])
            assert [r["stage"] for r in deploy_state.load_state(runner.directory)["native_queue"]["active"]["journal"]] == list(deploy_state.NATIVE_STAGES)
            with pytest.raises(ValueError, match="short observation"):
                runner.seal(base["values"][0])
            with pytest.raises(ValueError):
                runner.grant(base["values"][0])
            rejected = command(["bash", base["repo"] / "scripts/blue_green/deploy_pipeline.sh", "1", base["g1"], "--native-candidate", "--skip-verify"],
                               env=os.environ.copy(), pass_fds=(deploy_state.process_lock_fd(),))
            assert rejected.returncode == 64
        assert base["host"].posts == []
        state = deploy_state.load_state(runner.directory)
        assert state["deployed"]["sha"] == base["base"] and state["outcomes"] == {}
        assert sqlite3.connect(base["root"] / "blue.db").execute("select count(*) from fixture").fetchone() == (0,)
        assert (base["root"] / "blue.db").stat().st_ino != (base["root"] / "green.db").stat().st_ino
    finally:
        facts.clear()
        facts.update(old)


@pytest.fixture(scope="module")
def verified(native_base):
    """One genuine 10-minute shell observation; evidence reused only for identical G."""
    base = native_base
    runner = fresh(base, "verified")
    patch = pytest.MonkeyPatch()
    with pipeline_environment(base, patch, tick=300):
        with runner.fence():
            runner.start(base["values"][0])
            runner.seal(base["values"][0])
    patch.undo()
    state = deploy_state.load_state(runner.directory)
    proof = state["native_queue"]["active"]["verified_evidence"]
    assert proof["observation_seconds"] >= 600
    assert state["deployed"]["sha"] == base["base"] and state["outcomes"] == {}
    assert base["host"].posts == []
    return {"runner": runner, "facts": copy.deepcopy(base["host"].facts[base["g1"]]), "proof": proof}


@contextmanager
def resumed(base, verified, name):
    directory = base["root"] / name
    shutil.copytree(verified["runner"].directory, directory)
    runner = NativeQueue(base["repo"], directory, base["policy"], LoopbackFacts(base["scope"]), LoopbackGate(base["scope"]))
    facts = base["host"].facts[base["g1"]]
    old = copy.deepcopy(facts)
    facts.clear()
    facts.update(copy.deepcopy(verified["facts"]))
    try:
        yield runner, facts
    finally:
        base["host"].drop = False
        base["host"].delay.set()
        facts.clear()
        facts.update(old)


def ds(runner, *args):
    env = {**os.environ, "DAILYCHECK_DEPLOY_STATE_DIR": str(runner.directory)}
    env.pop(deploy_state.ATTEMPT_ENV, None)
    env.pop(deploy_state.ATTEMPT_FD_ENV, None)
    return command([sys.executable, ROOT / "scripts/blue_green/deploy_state.py", *args], env=env)


def merge_exact(base, facts):
    git(base["root"] / "host.git", "update-ref", "refs/heads/main", base["g1"])
    main = git(base["root"] / "host.git", "rev-parse", "refs/heads/main")
    facts.update(merged=True, merge_sha=main, main_sha=main,
                 merged_pr={"number": 1, "head_sha": base["h1"], "merged": True, "merge_sha": main})


@pytest.mark.parametrize("change", ["H", "B", "member", "binding", "policy", "dequeue", "attempt", "review"])
def test_preintent_changes_make_zero_posts(native_base, verified, change):
    base = native_base
    before = len(base["host"].posts)
    with resumed(base, verified, "stale-" + change) as (runner, facts), runner.fence():
        value = runner.resume_verified()
        if change == "H":
            facts["manifest"] = copy.deepcopy(facts["manifest"])
            facts["manifest"]["members"][0]["head_sha"] = base["h2"]
        elif change == "B":
            facts["manifest"] = copy.deepcopy(facts["manifest"])
            facts["manifest"]["creation_base_sha"] = base["g1"]
        elif change == "member":
            facts["manifest"] = copy.deepcopy(facts["manifest"])
            facts["manifest"]["members"][0]["body_sha256"] = digest("changed body")
        elif change == "binding":
            bindings = copy.deepcopy(base["bindings"])
            bindings["bindings"][0]["source"]["number"] += 100
            (runner.directory / "bindings.json").write_text(json.dumps(bindings))
        elif change == "policy":
            facts["policy_sha256"] = digest("new policy")
        elif change == "dequeue":
            facts["dequeued"] = True
        elif change == "attempt":
            facts["ci"][0]["latest_attempt"] += 1
        else:
            facts["review"]["record_sha256"] = digest("new review")
        with pytest.raises((ValueError, HostingError)):
            runner.grant(value)
        assert len(base["host"].posts) == before
        assert deploy_state.load_state(runner.directory)["native_queue"]["active"]["release_grant"] is None


def test_default_host_mutation_and_generic_transport_remain_off(native_base, verified):
    base = native_base
    with resumed(base, verified, "disabled") as (runner, _), runner.fence():
        value = runner.resume_verified()
        from scripts.prod_agent.native_queue import DisabledGate
        runner.gate = DisabledGate()
        with pytest.raises(RuntimeError, match="activation is off"):
            runner.grant(value)
        assert deploy_state.load_state(runner.directory)["native_queue"]["active"]["release_grant"] is None
    with pytest.raises(ValueError):
        LoopbackFixtureScope(base["root"], "https://api.github.com")
    config = HostingConfig.from_environment
    patch = pytest.MonkeyPatch()
    patch.setenv("DAILYCHECK_HOSTING_PROVIDER", "github")
    patch.setenv("DAILYCHECK_GITHUB_REPO", CODE)
    patch.setenv("DAILYCHECK_GITHUB_READ_TOKEN", "synthetic-read")
    patch.setenv("DAILYCHECK_GITHUB_REPORT_TOKEN", "synthetic-report")
    try:
        for role in (AccessRole.READ, AccessRole.REPORT):
            with pytest.raises(HostingError):
                HostingClient(config(role)).request("POST", f"/repos/{CODE}/check-runs", {"conclusion": "success"})
    finally:
        patch.undo()


def test_lost_response_reentry_ttl_clear_and_corruption_barriers(native_base, verified):
    base = native_base
    with resumed(base, verified, "lost") as (runner, facts):
        before = len(base["host"].posts)
        base["host"].drop = True
        with runner.fence():
            value = runner.resume_verified()
            with pytest.raises(subprocess.CalledProcessError):
                runner.grant(value)
            assert runner.reconcile() == "reconcile_required"
        assert len(base["host"].posts) == before + 1
        state = deploy_state.load_state(runner.directory)
        assert state["native_queue"]["active"]["phase"] == "grant_uncertain"
        state["lock"]["heartbeat_at"] = 0
        deploy_state.save_state(runner.directory, state)
        for dequeued in (True, False):
            facts["dequeued"] = dequeued
            assert ds(runner, "acquire", "--pr", "2", "--sha", base["g2"]).returncode == 10
            assert ds(runner, "clear-lock", "--yes").returncode == 10
            assert ds(runner, "init", "--sha", base["g2"]).returncode == 10
            assert ds(runner, "release", "--result", "success", "--sha", base["g1"]).returncode == 10
            with runner.fence():
                assert runner.reconcile() == "reconcile_required"
                with pytest.raises(ValueError):
                    runner.resume_verified()
        assert len(base["host"].posts) == before + 1
        original = (runner.directory / "state.json").read_bytes()
        for damage in (b"{corrupt", None, json.dumps({**state, "native_queue": None}).encode()):
            path = runner.directory / "state.json"
            if damage is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(damage)
            assert ds(runner, "acquire", "--pr", "2", "--sha", base["g2"]).returncode == 10
            assert ds(runner, "clear-lock", "--yes").returncode == 10
            assert not list(runner.directory.glob("state.json.corrupt-*"))
            with runner.fence(), pytest.raises(ValueError, match="mismatch"):
                runner.reconcile()
            path.write_bytes(original)
        assert deploy_state.load_state(runner.directory)["deployed"]["sha"] == base["base"]


def test_http_child_holds_fence_after_reporter_sigkill(native_base, verified):
    base = native_base
    with resumed(base, verified, "kill") as (runner, facts):
        host = base["host"]
        host.delay.clear()
        host.arrived.clear()
        script = base["root"] / "grant-child.py"
        script.write_text('''import json,sys
from pathlib import Path
from scripts.prod_agent.native_queue import NativeQueue,ProducerPolicy,LoopbackFixtureScope,LoopbackFacts,LoopbackGate
root,repo,directory,endpoint,blob=sys.argv[1:]
scope=LoopbackFixtureScope(Path(root),endpoint)
runner=NativeQueue(Path(repo),Path(directory),ProducerPolicy(15368,51,blob,76543,65432),LoopbackFacts(scope),LoopbackGate(scope))
with runner.fence():
 value=runner.resume_verified()
 runner.grant(value)
''')
        proc = subprocess.Popen([sys.executable, str(script), str(base["root"]), str(base["repo"]),
                                 str(runner.directory), base["scope"].endpoint, base["policy"].workflow_blob_sha],
                                env={**os.environ, "PYTHONPATH": str(ROOT)}, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True)
        try:
            assert host.arrived.wait(15), "HTTP child never arrived"
            proc.kill()  # reporter only; the actual HTTP child is deliberately alive
            proc.wait(10)
            assert proc.returncode == -signal.SIGKILL
            with (runner.directory / ".attempt.lock").open("a+") as lock, pytest.raises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert ds(runner, "acquire", "--pr", "2", "--sha", base["g2"]).returncode == 10
            assert ds(runner, "clear-lock", "--yes").returncode == 10
            host.delay.set()
            deadline = time.monotonic() + 10
            while True:
                with (runner.directory / ".attempt.lock").open("a+") as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        assert time.monotonic() < deadline
                time.sleep(0.05)
            # OS child is gone, yet the durable admission barrier still owns the lane.
            assert ds(runner, "acquire", "--pr", "2", "--sha", base["g2"]).returncode == 10
            state = deploy_state.load_state(runner.directory)
            assert state["native_queue"]["active"]["phase"] == "grant_intent"
            before = len(host.posts)
            merge_exact(base, facts)
            with runner.fence():
                assert runner.reconcile() == "anchored"
                assert runner.reconcile() == "reconciled"
            assert len(host.posts) == before
            state = deploy_state.load_state(runner.directory)
            assert state["deployed"]["sha"] == base["g1"]
            assert len(state["outcomes"]) == 1
        finally:
            host.delay.set()
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=10)


def test_exact_G_anchor_once_and_typed_public_v2(native_base, verified):
    base = native_base
    with resumed(base, verified, "exact") as (runner, facts), runner.fence():
        runner.grant(runner.resume_verified())
        state = deploy_state.load_state(runner.directory)
        assert state["deployed"]["sha"] == base["base"] and state["outcomes"] == {}
        assert runner.reconcile() == "reconcile_required"  # asynchronous host merge still pending
        merge_exact(base, facts)
        assert runner.reconcile() == "anchored"
        once = (runner.directory / "state.json").read_bytes()
        assert runner.reconcile() == "reconciled"
        assert once == (runner.directory / "state.json").read_bytes()
        _, outcome, generation = publication_authority.load_durable_outcome(runner.directory, 1, base["h1"])
        assert outcome["sha"] == base["g1"] and outcome["candidate"]["manifest"]["members"][0]["head_sha"] == base["h1"]
        from scripts.blue_green.deploy_report import PublicDeployFacts, render_public
        public = PublicDeployFacts(1, base["h1"], outcome["public_receipt"], outcome["outcome_at"], 0,
                                   "complete", "success", "in-scope", "aligned", "none", version=2,
                                   deployment_sha=base["g1"], creation_base_sha=base["base"])
        rendered = render_public(public)
        assert base["g1"] in rendered and base["h1"][:12] in rendered and PRIVATE not in rendered
        assert outcome["attempt_id"] not in rendered and outcome["attempt_token"] not in rendered
        assert generation == state["generation"]
        base["host"].public_head = base["h1"]
        shim = base["root"] / "public-curl"
        shim.write_text(f"""#!{sys.executable}
import os,sys
from urllib.parse import urlsplit
args=sys.argv[1:]
url=urlsplit(args[-1])
assert url.scheme == 'https' and url.netloc == 'api.github.com'
args[-1]={base['scope'].endpoint!r}+url.path+('?' +url.query if url.query else '')
os.execv('/usr/bin/curl',['/usr/bin/curl',*args])
""")
        shim.chmod(0o755)
        shadow = runner.directory / "shadow"
        shadow.mkdir()
        env = {**os.environ, "DAILYCHECK_HOSTING_PROVIDER": "github", "DAILYCHECK_GITHUB_REPO": CODE,
               "DAILYCHECK_GITHUB_READ_TOKEN": "synthetic-read-token", "DAILYCHECK_GITHUB_REPORT_TOKEN": "synthetic-report-token",
               "DAILYCHECK_GITHUB_SHADOW_DIR": str(shadow), "DAILYCHECK_DEPLOY_REPORT": "1",
               "DAILYCHECK_DEPLOY_EVENT_LOG": str(shadow / "events"), "DAILYCHECK_PROD_AGENT_INBOX": str(shadow / "inbox"),
               "DAILYCHECK_CURL": str(shim), "PYTHONPATH": str(ROOT)}
        args = [sys.executable, ROOT / "scripts/blue_green/deploy_report.py", "write", "--pr", "1", "--sha", base["h1"],
                "--rc", "0", "--stage", "complete", "--repo", base["repo"]]
        before = len(base["host"].comments)
        checked(args, env=env, pass_fds=(deploy_state.process_lock_fd(),))
        checked(args, env=env, pass_fds=(deploy_state.process_lock_fd(),))
        assert len(base["host"].comments) == before + 1
        body = base["host"].comments[-1]["body"]
        assert "deploy-publication:v2" in body and base["g1"] in body and PRIVATE not in body
        assert outcome["attempt_token"] not in body and outcome["attempt_id"] not in body


@pytest.mark.parametrize("wrong", ["main", "merge", "missing", "incident", "observation"])
def test_postgrant_uncertainty_never_delivers_or_unlocks(native_base, verified, wrong):
    base = native_base
    with resumed(base, verified, "wrong-" + wrong) as (runner, facts), runner.fence():
        runner.grant(runner.resume_verified())
        merge_exact(base, facts)
        if wrong == "main":
            facts["main_sha"] = base["g2"]
        elif wrong == "merge":
            facts["merge_sha"] = base["g2"]
        elif wrong == "missing":
            facts.pop("merged_pr")
        elif wrong == "incident":
            facts["incident"] = True
        else:
            facts["observation_complete"] = False
        assert runner.reconcile() == "reconcile_required"
        assert ds(runner, "clear-lock", "--yes").returncode == 10
        state = deploy_state.load_state(runner.directory)
        assert state["outcomes"] == {} and state["deployed"]["sha"] == base["base"]
        assert state["native_queue"]["active"]["phase"] == "incident"
        merge_exact(base, facts)
        facts.update(incident=False, observation_complete=True)
        assert runner.reconcile() == "reconcile_required"  # incident is absorbing


def test_authenticated_paginated_queue_manifest_ignores_ref_spelling(native_base):
    base = native_base
    pages = []
    manifest = base["values"][1]["manifest"]
    for i, member in enumerate(manifest["members"]):
        pr = {"id": member["node_id"], "number": member["number"], "headRefOid": member["head_sha"],
              "baseRefOid": member["base_sha"], "headRefName": member["head_ref"], "body": f"body{member['number']}",
              "repository": {"id": "R_kgDOVl3HNQ", "databaseId": 1448986421, "nameWithOwner": CODE}}
        row = {"id": member["queue_entry_id"], "position": i+1, "headCommit": {"oid": member["integration_sha"]},
               "baseCommit": {"oid": manifest["creation_base_sha"]}, "pullRequest": pr}
        page = {"data": {"repository": {"id": "R_kgDOVl3HNQ", "databaseId": 1448986421, "nameWithOwner": CODE,
                "mergeQueue": {"id": "queue", "entries": {"nodes": [row], "pageInfo": {
                    "hasNextPage": i == 0, "endCursor": "next" if i == 0 else None}}}}}}
        pages.append(page)
        base["host"].routes[f"/queue/{i}"] = page

    class Reader(QueueReader):
        def query(self, cursor):
            return base["scope"].get("/queue/" + ("0" if cursor is None else "1"))

    event = {"action": "checks_requested", "repository": {"id": 1448986421, "node_id": "R_kgDOVl3HNQ", "full_name": CODE},
             "merge_group": {"base_ref": "refs/heads/main", "head_ref": "refs/heads/not-a-parsable-PR-name",
                             "base_sha": base["base"], "head_sha": base["g2"]}}
    value, bodies = Reader("synthetic-read").manifest(base["repo"], event, "event2")
    assert value["members"] == manifest["members"] and bodies == {1: "body1", 2: "body2"}
    pages[1]["data"]["repository"]["mergeQueue"]["entries"]["nodes"] = [None]
    with pytest.raises((ValueError, KeyError, TypeError)):
        Reader("synthetic-read").manifest(base["repo"], event, "event2")


def test_member_report_adapter_runs_actual_gates_for_every_H(native_base, monkeypatch):
    from scripts.ci.merge_group import member_gates

    base = native_base
    # Independent owned fixture; actual gate scripts, synthetic app/preview inputs.
    repo = base["root"] / "member-gates"
    checked(["git", "clone", "-q", base["repo"], repo])
    git(repo, "checkout", "--detach", base["g2"])
    gate_dir = repo / "scripts/agent_framework"
    gate_dir.mkdir()
    for name in ("check_mcp_impact.py", "check_redlines.py", "check_pr_hygiene.py", "check_reports.py", "generate_reports.sh", "preview.sh"):
        raw = (ROOT / "scripts/agent_framework" / name).read_text()
        (gate_dir / name).write_text(raw.replace("/tmp/", str(base["root"] / "gate-tmp") + "/"))
    (base["root"] / "gate-tmp").mkdir()
    (repo / "scripts/dev_smoke_t11_e2e.py").write_text("import sqlite3\nc=sqlite3.connect(':memory:'); assert c.execute('select 1').fetchone()==(1,)\n")
    (repo / "scripts/mcp_contracts").symlink_to(ROOT / "scripts/mcp_contracts", target_is_directory=True)
    # Gate tools must exist in each member's checked-out H. Real script paths run
    # from the controller tree, while Git diffs/body checks run in exact H worktrees.
    manifest = copy.deepcopy(base["values"][1]["manifest"])
    bodies = {n: f"## 报告 1/4\n| R-001 | fixture | ✅ |\n## Checklist\n## 生产侧事项\nCloses #{n}\n" for n in (1, 2)}
    for member in manifest["members"]:
        member["body_sha256"] = digest(bodies[member["number"]])
    group_job = base["host"].facts[base["g2"]]["ci"][0]["jobs"][0]
    ci_root = base["root"] / "member-ci"
    ci_root.mkdir()
    text, evidence = ci_root / "text", ci_root / "evidence"
    text.write_text(group_job["envelope"]["text"])
    evidence.write_text(json.dumps(group_job["envelope"]["evidence"]))
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    monkeypatch.setenv("CI_ROOT", str(ci_root))
    monkeypatch.setenv("DAILYCHECK_CI_TMP", str(ci_root))
    # Use the venv's real ruff/bandit when available, no global installation.
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    member_gates(repo, manifest, bodies, evidence, text)
    assert (repo / "reports/pr-1/04-local-deploy-preview.md").exists()
    assert (repo / "reports/pr-2/04-local-deploy-preview.md").exists()
    bodies[2] += "\n❌ pending\n"
    manifest["members"][1]["body_sha256"] = digest(bodies[2])
    with pytest.raises(subprocess.CalledProcessError):
        member_gates(repo, manifest, bodies, evidence, text)


def test_native_read_client_binds_real_HTTP_jobs_checks_and_log_artifacts(native_base, monkeypatch):
    import base64
    from urllib.parse import urlencode

    from scripts.prod_agent.github_readiness import NativeGitHubFacts

    base, host = native_base, native_base["host"]
    policy = base["policy"]
    raw_runs = base["host"].facts[base["g1"]]["ci"]
    root = f"/repos/{CODE}"
    host.routes[root + "/actions/workflows/ci.yml"] = {"id": policy.workflow_id, "state": "active", "path": ".github/workflows/ci.yml"}
    logs = {}
    for item in raw_runs:
        run = item["run"]
        head, pr = run["head_sha"], item["pr"]
        host.routes[root + "/contents/.github/workflows/ci.yml?ref=" + head] = {"sha": policy.workflow_blob_sha}
        associations = ([{"number": pr, "head": {"sha": head}, "base": {"sha": base["base"], "ref": "main"}}] if pr else [])
        raw = {"id": run["id"], "run_number": run["id"], "workflow_id": policy.workflow_id, "run_attempt": 1,
               "path": ".github/workflows/ci.yml", "event": run["event"], "head_sha": head,
               "repository": {"id": 1448986421, "node_id": "R_kgDOVl3HNQ"}, "head_repository": {"full_name": CODE},
               "status": "completed", "conclusion": "success", "check_suite_id": run["suite_id"], "pull_requests": associations}
        query = urlencode({"event": run["event"], "head_sha": head, "per_page": 100, "page": 1})
        host.routes[root + f"/actions/workflows/{policy.workflow_id}/runs?" + query] = {"total_count": 1, "workflow_runs": [raw]}
        jobs, checks = [], []
        for job in item["jobs"]:
            check = job["check"]
            jobs.append({"id": job["id"], "name": job["name"], "run_id": run["id"], "run_attempt": 1, "head_sha": head,
                         "status": "completed", "conclusion": "success",
                         "check_run_url": "https://api.github.com" + root + f"/check-runs/{check['id']}"})
            checks.append({"id": check["id"], "name": check["name"], "head_sha": head, "check_suite": {"id": run["suite_id"]},
                           "app": {"id": policy.actions_app_id, "slug": "github-actions"}, "status": "completed", "conclusion": "success"})
            record = {k: job[k] for k in ("envelope", "obligations", "artifact_sha256")}
            logs[job["id"]] = "2026-11-07T00:00:00Z DAILYCHECK_NATIVE_EVIDENCE=" + base64.b64encode(json.dumps(record).encode()).decode() + "\n"
            host.routes[f"/job-log/{job['id']}"] = logs[job["id"]]
        host.routes[root + f"/actions/runs/{run['id']}/attempts/1/jobs?per_page=100&page=1"] = {"total_count": 2, "jobs": jobs}
        host.routes[root + f"/commits/{head}/check-runs?filter=all&per_page=100&page=1"] = {"total_count": 2, "check_runs": checks}
    shim = base["root"] / "read-curl"
    shim.write_text(f"""#!{sys.executable}
import os,sys
from urllib.parse import urlsplit
args=sys.argv[1:]; url=urlsplit(args[-1])
assert url.scheme=='https' and url.netloc=='api.github.com'
args[-1]={base['scope'].endpoint!r}+url.path+('?' +url.query if url.query else '')
os.execv('/usr/bin/curl',['/usr/bin/curl',*args])
""")
    shim.chmod(0o755)
    for name, value in {"DAILYCHECK_HOSTING_PROVIDER": "github", "DAILYCHECK_GITHUB_REPO": CODE,
                        "DAILYCHECK_GITHUB_READ_TOKEN": "synthetic-authenticated-read", "DAILYCHECK_CURL": str(shim)}.items():
        monkeypatch.setenv(name, value)

    class Reader(NativeGitHubFacts):
        def job_log(self, job_id):
            return base["scope"].get(f"/job-log/{job_id}")

    monkeypatch.setattr(host, "read_auth", "Bearer synthetic-authenticated-read")
    reader = Reader(HostingClient(HostingConfig.from_environment(AccessRole.READ)), None, policy,
                    runtime=None, review=None, directory=base["root"])
    actual = [reader._run("G", base["g1"], None), reader._run("H", base["h1"], 1)]
    proof = validate_native_ci(base["repo"], base["values"][0]["manifest"], policy, actual)
    assert len(proof) == 4
    assert host.auth_seen and set(host.auth_seen) == {"Bearer synthetic-authenticated-read"}
    h = actual[1]
    assert h["run"]["historical_base_sha"] == base["base"]
    # Run's historical B is accepted even after main advances; there is no current-base equality here.
    host.routes[root + "/branches/main"] = {"commit": {"sha": base["g2"]}}
    assert reader._run("H", base["h1"], 1) == h
    route = root + f"/commits/{base['g1']}/check-runs?filter=all&per_page=100&page=1"
    host.routes[route]["check_runs"][1]["app"]["id"] += 1
    bad = [reader._run("G", base["g1"], None), h]
    with pytest.raises(ValueError, match="producer"):
        validate_native_ci(base["repo"], base["values"][0]["manifest"], policy, bad)


def test_head_admission_POST_is_disjoint_from_group_release(native_base):
    base = native_base
    runner = fresh(base, "head-admission")
    member = base["values"][0]["manifest"]["members"][0]
    ci = base["host"].facts[base["g1"]]["ci"][1:]
    base["host"].routes["/head-facts/1"] = {"repository_id": 1448986421, "repository_node_id": "R_kgDOVl3HNQ",
                                          "member": member, "ci": ci}
    before = len(base["host"].posts)
    with runner.fence():
        receipt = runner.admit_head(member)
        assert receipt["deployment"] is False
        assert len(base["host"].posts) == before + 1
        assert base["host"].posts[-1]["head_sha"] == base["h1"]
        collision = {**member, "number": 2, "head_sha": base["g1"]}
        with pytest.raises(ValueError, match="multi-parent"):
            runner.admit_head(collision)
        assert len(base["host"].posts) == before + 1
    state = deploy_state.load_state(runner.directory)
    assert state["outcomes"] == {} and state["deployed"]["sha"] == base["base"]


def test_fixture_initialization_cannot_target_real_source(native_base):
    base = native_base
    directory = base["root"] / "wrong-repo"
    runner = NativeQueue(ROOT, directory, base["policy"], LoopbackFacts(base["scope"]), LoopbackGate(base["scope"]))
    with pytest.raises(ValueError, match="inside owned scope"):
        runner.initialize_fixture(base["scope"])
    assert not directory.exists()


def test_unchanged_G2_reports_keep_creation_manifest_after_G1_leaves_queue(native_base):
    base = native_base
    original = base["values"][1]["manifest"]
    event = {"action": "checks_requested", "repository": {"id": 1448986421, "node_id": "R_kgDOVl3HNQ", "full_name": CODE},
             "merge_group": {"base_ref": "refs/heads/main", "head_ref": original["group_ref"],
                             "base_sha": base["base"], "head_sha": base["g2"]}}
    base["host"].routes["/remaining-queue"] = {"queue_id": "queue", "entries": [
        {"id": "entry2", "headCommit": {"oid": base["g2"]}, "pullRequest": {"number": 2}}]}
    for member in original["members"]:
        n = member["number"]
        base["host"].routes[f"/remaining-pr/{n}"] = {"number": n, "node_id": member["node_id"],
            "head": {"sha": member["head_sha"], "repo": {"id": 1448986421}}, "base": {"repo": {"id": 1448986421}},
            "body": f"body{n}", "merged": n == 1, "merge_commit_sha": base["g1"] if n == 1 else None}

    class Reader(QueueReader):
        def entries(self):
            value = base["scope"].get("/remaining-queue")
            return value["queue_id"], value["entries"]

        def public_pr(self, number):
            return base["scope"].get(f"/remaining-pr/{number}")

    before = digest(original)
    manifest, bodies = Reader("synthetic-read").revalidate(base["repo"], original, event, "event2")
    assert digest(manifest) == before and bodies == {1: "body1", 2: "body2"}
    assert manifest["creation_base_sha"] != manifest["expected_predecessor_sha"]
    base["host"].routes["/remaining-pr/1"]["merge_commit_sha"] = base["h1"]
    with pytest.raises(ValueError, match="recorded G"):
        Reader("synthetic-read").revalidate(base["repo"], original, event, "event2")


def test_guard_blocks_config_writer_even_when_native_state_is_missing(native_base):
    base = native_base
    runner = fresh(base, "config-barrier")
    original = (runner.directory / "state.json").read_bytes()
    (runner.directory / "state.json").unlink()
    with pytest.raises(RuntimeError, match="native unresolved"):
        with deploy_state.publication_config_transaction(runner.directory):
            pytest.fail("config writer crossed missing-state guard")
    assert not (runner.directory / "state.json").exists()
    (runner.directory / "state.json").write_bytes(original)
    with deploy_state.publication_config_transaction(runner.directory):
        assert deploy_state.native_consistent(runner.directory, deploy_state.load_state(runner.directory))


def test_ci_runs_full_pytest_once_per_H_and_G(native_base):
    base = native_base
    measurements = []
    for i, value in enumerate(base["values"], 1):
        rows = [json.loads(line) for line in (base["root"] / f"ci{i}" / "pytest-invocations.jsonl").read_text().splitlines()]
        for run in base["host"].facts[value["manifest"]["group_sha"]]["ci"]:
            count = sum(row["sha"] == run["run"]["head_sha"] for row in rows)
            measurements.append({"phase": run["phase"], "sha": run["run"]["head_sha"], "invocations": count})
    (base["root"] / "workflow-invocation-counts.json").write_text(json.dumps(measurements, indent=2))
    assert all(row["invocations"] == 1 for row in measurements), measurements

"""Synthetic authorities, actual Linux native pipeline/FD/HTTP/SQLite call chain.

Only external traffic stages/Git and the fixture clock are substituted. No test
result represents a real 600-second production observation or a real grant.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.blue_green.release_credentials import PINS, render
from tests.private_audit_testenv import private_audit_fixture  # noqa: F401
from tests.test_github_ordinary_intake import dispatch
from tests.test_github_ordinary_intake import lane as ordinary_lane
from tests.test_github_transport import BASE, REPO, REPO_ID, ROOT, SHA
from tests.test_github_transport import proof as save_proof

lane = ordinary_lane

SOURCE = Path(__file__).resolve().parents[1]
OPS = "DUIDUI-Studio/DailyCheck-ops"


def put(path, raw, mode=0o600):
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.write_text(raw)
    path.chmod(mode)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(argv, env, **kwargs):
    child = None
    try:
        child = subprocess.Popen(
            argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs
        )
        out, err = child.communicate(timeout=60)
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            child.wait()
    with pytest.raises(ChildProcessError):
        os.waitpid(child.pid, os.WNOHANG)
    return child.returncode, out.decode(), err.decode()


def make_bundle(tmp_path, lane, case):
    engine, repo, root = (tmp_path / name for name in ("engine", "serving", "bundle"))
    for relative in PINS:
        put(engine / relative, (SOURCE / relative).read_text())
    for relative in ("scripts/__init__.py", "scripts/prod_agent/__init__.py"):
        put(engine / relative, "")
    # External stages and clock seam, explicitly SYNTHETIC. Ordinary algorithm,
    # eight-stage invocation, guards, selector and all native helpers stay real.
    pipeline = engine / "scripts/blue_green/deploy_pipeline.sh"
    text = pipeline.read_text().replace('sleep "${tick}"', "true # synthetic clock only")
    text = text.replace("\"${PY}\" -c 'import time; print(int(time.monotonic()))'", "fixture-clock")
    text = text.replace(
        "${DAILYCHECK_PIPELINE_CURL:-curl}", "${DAILYCHECK_PIPELINE_CURL:-fixture-probe}"
    )
    text = text.replace(
        "${DAILYCHECK_PIPELINE_JOURNALCTL:-journalctl}",
        "${DAILYCHECK_PIPELINE_JOURNALCTL:-fixture-journal}",
    )
    pipeline.write_text(text)
    for stage in ("up", "verify", "promote", "switch"):
        extra = ""
        if stage == "up" and case == "up-failure":
            extra = "exit 7\n"
        if stage == "up" and case == "aborted":
            extra = "kill -TERM $PPID\n"
        if stage == "verify":
            extra = f"curl -s http://127.0.0.1:{lane.port}/fixture/advance >/dev/null\n"
        put(
            repo / f"scripts/blue_green/{stage}.sh",
            '#!/bin/sh\nprintf "%s\\n" "' + stage + ' $*" >>"$TRACE_CALLS"\n' + extra + "exit 0\n",
            0o700,
        )
    put(repo / "scripts/hosting.py", (SOURCE / "scripts/hosting.py").read_text())
    tools = tmp_path / "tools"
    tools.mkdir(mode=0o700)
    put(
        tools / "git",
        f"""#!{sys.executable}
import sys
args=sys.argv[1:]
if args[:1]==['-C']:args=args[2:]
if args==['remote','get-url','origin']: print('https://github.com/{REPO}.git')
elif args and args[0]=='rev-parse': print({BASE!r} if args[-1]=='origin/main' else {SHA!r})
elif args and args[0] in ('fetch','checkout','merge-base','diff-index'):pass
else:raise SystemExit(128)
""",
        0o700,
    )
    put(
        tools / "fixture-clock",
        f"""#!{sys.executable}
from pathlib import Path
p=Path({str(tmp_path / "clock")!r});n=int(p.read_text()) if p.exists() else 0
print(n);p.write_text(str(n+300))
""",
        0o700,
    )
    put(tools / "fixture-probe", "#!/bin/sh\nprintf 200\n", 0o700)
    put(tools / "fixture-journal", "#!/bin/sh\nexit 0\n", 0o700)
    # Shim ABI is synthetic. It is still a real exec and records exact FD/PID/PGID.
    shim = tmp_path / "owned-app-shim.py"
    put(
        shim,
        f"""import json,os,sys,urllib.request
from pathlib import Path
root=Path({str(tmp_path)!r})
def jwt():return 'synthetic-app-jwt'
def request(method,path,bearer,payload=None):
 req=urllib.request.Request('http://127.0.0.1:{lane.port}'+path,
  data=json.dumps(payload).encode() if payload is not None else None,
  headers={{'Authorization':'Bearer '+bearer,'Content-Type':'application/json'}},method=method)
 with urllib.request.urlopen(req) as response:return json.load(response)
def refresh():
 count=root/'refresh-count';n=int(count.read_text())+1 if count.exists() else 1;count.write_text(str(n))
 fd=int(os.environ['DAILYCHECK_DEPLOY_ATTEMPT_FD']);meta=os.fstat(fd)
 with (root/'fd-trace.jsonl').open('a') as f:f.write(json.dumps(dict(n=n,pid=os.getpid(),pgid=os.getpgrp(),fd=fd,dev=meta.st_dev,ino=meta.st_ino))+'\\n')
 for role in ('read','report','finalize'):
  path=root/(role+'.token');path.write_text(role+'-phase-'+str(n));path.chmod(0o600)
  key='DAILYCHECK_GITHUB_'+role.upper()+'_TOKEN_FILE';os.environ[key]=str(path)
  os.environ.pop('DAILYCHECK_GITHUB_'+role.upper()+'_TOKEN',None)
def main():
 refresh()
 os.execv(sys.argv[2],sys.argv[2:])
""",
    )
    state = Path(lane.env["DAILYCHECK_DEPLOY_STATE_DIR"])
    current = json.loads((state / "state.json").read_text())
    current.update(generation=11)
    current["deployed"]["generation"] = 10
    (state / "state.json").write_text(json.dumps(current))
    document = {
        "version": 1,
        "bindings": [
            {
                "pr": 160,
                "public": {
                    "provider": "github",
                    "host": "github.com",
                    "repository_id": REPO_ID,
                    "repository": REPO,
                },
                "source": {
                    "provider": "github",
                    "host": "github.com",
                    "repository_id": 1449125557,
                    "repository": OPS,
                    "kind": "issue",
                    "number": 7,
                },
            }
        ],
    }
    put(state / "bindings.json", json.dumps(document))
    from scripts.blue_green.publication_authority import resolve_source_binding

    binding = resolve_source_binding(state, 160)
    policy = {
        "version": 1,
        "repository_id": REPO_ID,
        "repository": REPO,
        "mode": "automatic",
        "since_ms": int(time.time() * 1000) - 10000,
        "review_targets": [{"pr": 160, "head": SHA}],
        "automatic_pr_range": None,
        "excluded_open_prs": [15, 17, 18],
        "review_actor_ids": [11],
    }
    policy_path = tmp_path / "policy.json"
    put(policy_path, json.dumps(policy))
    lane.env["DAILYCHECK_GITHUB_INTAKE_POLICY_FILE"] = str(policy_path)
    policy_sha = hashlib.sha256(
        json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    targets = []
    for role, repository, rid, number, ident in [
        ("code", REPO, REPO_ID, 19, 6391796319),
        ("ops", OPS, 1449125557, 7, 7001),
    ]:
        targets.append(
            dict(
                repository=repository,
                repository_id=rid,
                number=number,
                issue_id=ident,
                token_file=str(root / "credentials" / (role + ".token")),
                grant_file=str(root / "credentials" / (role + ".grant.json")),
            )
        )
    profile = dict(
        uid=os.getuid(),
        python=sys.executable,
        python_target=str(Path(sys.executable).resolve()),
        python_sha256=digest(Path(sys.executable).resolve()),
        root=str(root),
        repo=str(repo),
        engine=str(engine),
        state=str(state),
        tmp=str(root / "scratch/tmp"),
        log=str(root / "pipeline.log"),
        shim=str(shim),
        shim_sha=digest(shim),
        pr=160,
        head=SHA,
        base=BASE,
        generation=11,
        deployed_generation=10,
        session="synthetic-pr160-one-run",
        task="synthetic-task",
        dispatch="synthetic-dispatch",
        pins={p: digest(engine / p) for p in PINS},
        binding_sha256=binding.binding_sha256,
        intake_policy=str(policy_path),
        intake_key=lane.env["DAILYCHECK_GITHUB_INTAKE_KEY_FILE"],
        reducer_db=lane.env["DAILYCHECK_EVENT_REDUCER_DB"],
        issue_protocol={"version": 1, "targets": targets},
        capability=dict(
            version=1,
            owner="SYNTHETIC-test-owner",
            ci_run_id=40,
            ci_attempt=2,
            policy_sha256=policy_sha,
            since_ms=policy["since_ms"],
        ),
    )
    env = dict(
        lane.env,
        PATH=str(tools) + os.pathsep + os.environ["PATH"],
        TRACE_CALLS=str(tmp_path / "calls"),
        DAILYCHECK_DASHBOARD_HOOK="0",
        DAILYCHECK_DEPLOY_EVENT_LOG=str(tmp_path / "events.jsonl"),
        DAILYCHECK_PROD_AGENT_INBOX=str(tmp_path / "result-inbox.jsonl"),
    )
    render(profile, root)
    return profile, env


@pytest.mark.skipif(
    sys.platform != "linux" or os.getuid() != 0,
    reason="production root/Linux fixture, exercised in owned Docker",
)
@pytest.mark.parametrize(
    "case",
    [
        "happy",
        "blocker",
        "up-failure",
        "aborted",
        "report-failure",
        "forbidden-scope",
        "wrong-issue",
        "issue-unknown",
    ],
)
def test_actual_poll_review_fixed_entry_native_pipeline_report_observer(lane, tmp_path, case):
    # Real repository numbers here identify SYNTHETIC HTTP fixture objects only.
    lane.port = int(lane.env["DAILYCHECK_HERMES_WEBHOOK_URL"].rsplit(":", 1)[1])
    profile, env = make_bundle(tmp_path, lane, case)
    lane.table[ROOT]["data"].update(
        node_id="R_kgDOVl3HNQ",
        private=False,
        visibility="public",
        has_issues=True,
        archived=False,
        disabled=False,
        url="https://api.github.com" + ROOT,
        html_url="https://github.com/" + REPO,
    )
    lane.table[ROOT + "/pulls/160"]["data"].update(
        user={"id": 99}, node_id="PR_synthetic", merged=False
    )
    lane.table[ROOT + "/compare/" + BASE + "..." + SHA + "?page=2"] = {
        "data": dict(
            status="ahead",
            base_commit={"sha": BASE},
            merge_base_commit={"sha": BASE},
            ahead_by=1,
            behind_by=0,
            total_commits=1,
            commits=[],
        )
    }
    lane.table[ROOT + "/git/ref/heads/main"] = {
        "data": dict(ref="refs/heads/main", object={"sha": BASE})
    }
    lane.table["/repos/" + OPS] = {
        "data": dict(
            id=1449125557,
            full_name=OPS,
            private=True,
            visibility="private",
            has_issues=True,
            archived=False,
            disabled=False,
            url="https://api.github.com/repos/" + OPS,
            html_url="https://github.com/" + OPS,
        )
    }
    for target in profile["issue_protocol"]["targets"]:
        root = "/repos/" + target["repository"]
        number = target["number"]
        lane.table[root + "/issues/" + str(number)] = {
            "data": dict(
                id=target["issue_id"],
                number=number,
                state="open",
                url="https://api.github.com" + root + "/issues/" + str(number),
                repository_url="https://api.github.com" + root,
                html_url="https://github.com/" + target["repository"] + "/issues/" + str(number),
            )
        }
    lane.table[ROOT + "/issues/160/comments?page=1"] = {"data": []}
    lane.table[ROOT + "/pulls/160/files?page=1"] = {
        "data": [{"filename": "scripts/blue_green/deploy_report.py"}]
    }
    lane.table["/fixture/advance"] = {"data": {}}
    mutations = []

    def mutation(path, method, body, headers):
        mutations.append(
            dict(path=path, method=method, body=body, auth=headers.get("Authorization"))
        )
        if path.endswith("/access_tokens"):
            rid = body["repository_ids"][0]
            if case == "forbidden-scope":
                return 201, dict(
                    token="synthetic-forbidden",
                    permissions={"issues": "write", "contents": "write", "metadata": "read"},
                    repositories=[{"id": rid}],
                    expires_at="2099-01-01T00:00:00Z",
                )
            return 201, dict(
                token="synthetic-issue-" + str(rid) + "-" + str(len(mutations)),
                permissions={"issues": "write", "metadata": "read"},
                repositories=[{"id": rid}],
                expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
            )
        if path == "/graphql":
            assert headers["Authorization"] == "Bearer finalize-phase-3"
            data = body["variables"]["input"]
            assert (
                data["refUpdates"][0]["beforeOid"] == BASE
                and data["refUpdates"][0]["afterOid"] == SHA
            )
            assert all(row["force"] is False for row in data["refUpdates"])
            lane.table[ROOT + "/pulls/160"]["data"].update(
                state="closed", merged=True, merge_commit_sha=SHA
            )
            lane.table[ROOT + "/git/ref/heads/main"]["data"]["object"]["sha"] = SHA
            return 200, {"data": {"updateRefs": {"clientMutationId": data["clientMutationId"]}}}
        if path == ROOT + "/issues/160/comments":
            assert headers["Authorization"] == "Bearer report-phase-" + (
                "3" if case in ("up-failure", "aborted") else "4"
            )
            if case == "report-failure":
                return 503, {}
            row = dict(
                id=201, body=body["body"], issue_url="https://api.github.com" + ROOT + "/issues/160"
            )
            lane.table[ROOT + "/issues/160/comments?page=1"]["data"].append(row)
            return 201, row
        if path == ROOT + "/pulls/160/reviews":
            assert headers["Authorization"] == "Bearer report-phase-1"
            lane.table[path + "?page=1"]["data"] = [
                dict(
                    id=91,
                    user={"id": 11, "type": "Bot"},
                    state="APPROVED" if body["event"] == "APPROVE" else "CHANGES_REQUESTED",
                    commit_id=body["commit_id"],
                    submitted_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                )
            ]
            return 200, lane.table[path + "?page=1"]["data"][0]
        assert method == "PATCH" and body == dict(state="closed", state_reason="completed")
        lane.table[path]["data"]["state"] = "closed"
        if case == "issue-unknown":
            return 503, {}
        return 200, lane.table[path]["data"]

    epoch = {"seconds": 0}

    def authorize(path, headers):
        if path == "/fixture/advance":
            epoch["seconds"] += 3601
        elif epoch["seconds"] > 3600:
            assert headers.get("Authorization") not in {
                "Bearer read-phase-2",
                "Bearer report-phase-2",
                "Bearer finalize-phase-2",
            }

    if case == "wrong-issue":
        lane.table[ROOT + "/issues/19"]["data"]["id"] = 123
    lane.transport["authorize"] = authorize
    lane.transport["mutation"] = mutation
    # Real poll -> adapter -> SQLite claim -> signed owned HTTP delivery.
    result = dispatch(lane)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(lane.posts) == 1
    time.sleep(1.1)  # Formal review timestamp must be after the actual dispatch claim.
    decision = "CHANGES_REQUESTED" if case == "blocker" else "APPROVED"
    review_rc, review_out, review_err = run(
        [sys.executable, "-B", str(Path(profile["root"]) / "review.py"), decision], env
    )
    assert review_rc == (31 if case == "blocker" else 0), (review_rc, review_out, review_err)
    rc, out, err = run(
        [sys.executable, "-B", str(Path(profile["root"]) / "parent.py"), "--execute-admitted"], env
    )
    expected = {
        "happy": 0,
        "blocker": 78,
        "up-failure": 20,
        "aborted": 143,
        "report-failure": 29,
        "forbidden-scope": 1,
        "wrong-issue": 78,
        "issue-unknown": 29,
    }[case]
    assert rc == expected, (
        rc,
        out,
        err,
        (Path(profile["root"]) / "retained-child.private.log").read_text(),
    )
    wait = json.loads((Path(profile["root"]) / "retained-child-wait.json").read_text())
    assert wait["positive_wait"] and wait["actual_ECHILD"]
    rows = [json.loads(line) for line in (tmp_path / "fd-trace.jsonl").read_text().splitlines()]
    assert len({(row["dev"], row["ino"], row["fd"]) for row in rows}) == 1
    assert rows[1]["pid"] == rows[1]["pgid"]
    assert all(row["pgid"] == rows[1]["pgid"] for row in rows[1:])
    assert len(rows) == (
        2
        if case in ("blocker", "forbidden-scope", "wrong-issue")
        else 3
        if case in ("up-failure", "aborted")
        else 4
    )
    if case not in ("blocker", "forbidden-scope", "wrong-issue"):
        receipt = json.loads((Path(profile["root"]) / "completion-receipt.json").read_text())
        assert receipt["remote_orca_push"] is False
        assert receipt["status"] == (
            "delivered"
            if case == "happy"
            else "deploy_failed"
            if case in ("up-failure", "aborted")
            else "UNKNOWN"
        )
        state = json.loads((Path(profile["state"]) / "state.json").read_text())
        assert state["generation"] == 12 and state["lock"] is None
        if case == "happy":
            assert len([row for row in mutations if row["method"] == "PATCH"]) == 2
            assert len([row for row in mutations if row["path"] == "/graphql"]) == 1
        elif case == "issue-unknown":
            assert len([row for row in mutations if row["method"] == "PATCH"]) == 1
            assert receipt["first_cause"] == {
                "stage": "issue-reconcile",
                "reason": "issue_close_unknown",
            }
        else:
            assert not [row for row in mutations if row["method"] == "PATCH"]
    if case not in ("blocker", "forbidden-scope", "wrong-issue"):
        notifications = [
            json.loads(line) for line in (tmp_path / "result-inbox.jsonl").read_text().splitlines()
        ]
        assert len(notifications) == 1
        assert lane.adapter(notifications[0]).returncode == 75
        assert lane.adapter().returncode == 0
        assert len(lane.posts) == 2 and lane.posts[-1]["path"] == "/webhooks/dailycheck-deploy"
        assert lane.posts[-1]["body"]["public_receipt"] == receipt["public_receipt"]
        assert lane.adapter(notifications[0]).returncode == 0
        assert lane.adapter().returncode == 0 and len(lane.posts) == 2
    # The same fixed invocation cannot replay review, grants, finalization or reporting.
    before = len(mutations)
    assert (
        run(
            [sys.executable, "-B", str(Path(profile["root"]) / "parent.py"), "--execute-admitted"],
            env,
        )[0]
        != 0
    )
    assert len(mutations) == before
    save_proof(
        "release-fullchain-" + case,
        dict(
            synthetic=True,
            native_rc=rc,
            stdout=out,
            stderr=err,
            custody=rows,
            mutations=mutations,
            observer_http=lane.posts,
            wait=wait,
            fixture_clock_not_production_observation=True,
            expiry_boundary_seconds=epoch["seconds"],
        ),
    )


@pytest.mark.skipif(
    sys.platform != "linux" or os.getuid() != 0, reason="production root/Linux parent custody"
)
@pytest.mark.parametrize("fault", ["none", "metadata", "start-audit", "wait-audit"])
def test_retained_parent_settles_same_child_after_postlaunch_errors(tmp_path, fault):
    from scripts.blue_green import release_parent

    root = tmp_path / "parent-probe"
    root.mkdir(mode=0o700)
    gate = tmp_path / "gate"
    ready = tmp_path / "ready"
    trace = tmp_path / "custody.jsonl"
    continuation = tmp_path / "continuation.py"
    put(
        continuation,
        f"""import json,os,sys,time
from pathlib import Path
fd=int(sys.argv[1]);s=os.fstat(fd)
with Path({str(trace)!r}).open('a') as f:f.write(json.dumps(dict(pid=os.getpid(),pgid=os.getpgrp(),dev=s.st_dev,ino=s.st_ino))+'\\n')
Path({str(ready)!r}).touch()
while not Path({str(gate)!r}).exists():time.sleep(.01)
raise SystemExit(17)
""",
    )
    entry = root / "entry.py"
    put(
        entry,
        f"""import fcntl,json,os,sys
from pathlib import Path
fd=os.open({str(tmp_path / "attempt.lock")!r},os.O_RDWR|os.O_CREAT,0o600)
fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);os.set_inheritable(fd,True);s=os.fstat(fd)
with Path({str(trace)!r}).open('a') as f:f.write(json.dumps(dict(pid=os.getpid(),pgid=os.getpgrp(),dev=s.st_dev,ino=s.st_ino))+'\\n')
os.execv(sys.executable,[sys.executable,{str(continuation)!r},str(fd)])
""",
        0o700,
    )
    put(root / "selector.py", "# SYNTHETIC external entry\n", 0o700)
    profile = dict(
        root=str(root),
        python=sys.executable,
        python_target=str(Path(sys.executable).resolve()),
        python_sha256=digest(Path(sys.executable).resolve()),
        repo=str(tmp_path),
        uid=os.getuid(),
        capability={"synthetic": True},
        binding_sha256="1" * 64,
        issue_protocol={"synthetic": True},
        pr=160,
        head=SHA,
        task="synthetic",
        dispatch="synthetic",
    )
    code = (
        Path(release_parent.__file__)
        .read_text()
        .replace("PROFILE = None  # BUILD_PROFILE", "PROFILE = " + repr(profile))
    )
    code = code.replace("ENTRY_SHA = None", "ENTRY_SHA = " + repr(digest(entry)))
    code = code.replace(
        "SELECTOR_SHA = None", "SELECTOR_SHA = " + repr(digest(root / "selector.py"))
    )
    parent = root / "parent.py"
    put(parent, code)
    driver = tmp_path / "driver.py"
    put(
        driver,
        f"""import errno,os,runpy,sys
from pathlib import Path
m=runpy.run_path({str(parent)!r},run_name='_owned_parent')
sys.argv=[{str(parent)!r},'--execute-admitted']
if {fault!r}=='metadata':
 def bad(pid):raise OSError(errno.EIO,'SYNTHETIC getpgid failure')
 m['os'].getpgid=bad
if {fault!r} in ('start-audit','wait-audit'):
 original=m['os'].open
 def opening(path,*args,**kwargs):
  name='retained-child-start.json' if {fault!r}=='start-audit' else 'retained-child-wait.json'
  if Path(path).name==name:raise OSError(errno.ENOSPC,'SYNTHETIC audit full')
  return original(path,*args,**kwargs)
 m['os'].open=opening
m['main']()
""",
    )
    parent_process = subprocess.Popen(
        [sys.executable, "-B", str(driver)], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and parent_process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        contender = [
            sys.executable,
            "-c",
            f"""import fcntl,sys
with open({str(tmp_path / "attempt.lock")!r},'a') as f:
 try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
 except BlockingIOError:sys.exit(10)
""",
        ]
        assert run(contender, os.environ.copy())[0] == 10
        assert parent_process.poll() is None
    finally:
        gate.touch()
        out, err = parent_process.communicate(timeout=15)
    with pytest.raises(ChildProcessError):
        os.waitpid(parent_process.pid, os.WNOHANG)
    assert parent_process.returncode == (17 if fault == "none" else 1), (out, err)
    result = json.loads(next(line for line in err.decode().splitlines() if line.startswith("{")))[
        "retained_child_settlement"
    ]
    assert result["native_rc"] == 17 and result["positive_wait"] and result["actual_ECHILD"]
    if fault != "none":
        assert (
            result["postlaunch_error" if fault != "wait-audit" else "settlement_audit_error"][
                "error_class"
            ]
            == "OSError"
        )
    rows = [json.loads(line) for line in trace.read_text().splitlines()]
    assert rows[0] == rows[1] and rows[0]["pid"] == rows[0]["pgid"]
    assert run(contender, os.environ.copy())[0] == 0
    save_proof(
        "release-parent-" + fault,
        dict(
            synthetic=True,
            source_sha256=digest(Path(release_parent.__file__)),
            result=result,
            custody=rows,
            native_rc=parent_process.returncode,
        ),
    )


@pytest.mark.skipif(
    sys.platform != "linux" or os.getuid() != 0, reason="production root/Linux config custody"
)
def test_binding_installer_real_cas_partial_rollback_and_prewrite_paths(tmp_path):
    from scripts.blue_green import release_config

    source = Path(release_config.__file__)
    state = tmp_path / "config-state"
    state.mkdir(mode=0o700)
    old = {"version": 1, "bindings": []}
    new = {
        "version": 1,
        "bindings": [
            dict(
                pr=20,
                public=dict(
                    provider="github", host="github.com", repository_id=1448986421, repository=REPO
                ),
                source=dict(
                    provider="github",
                    host="github.com",
                    repository_id=1449125557,
                    repository=OPS,
                    kind="issue",
                    number=7,
                ),
            )
        ],
    }
    target = state / "bindings.json"
    put(target, json.dumps(old))
    proposal = tmp_path / "proposal.json"
    put(proposal, json.dumps(new))
    original = digest(target)
    base = [sys.executable, "-B", str(source)]
    args = ["--state", str(state), "--input", str(proposal), "--expected-sha256", original]
    hardlink = tmp_path / "hardlink"
    os.link(target, hardlink)
    assert (
        run(
            base + ["install", *args, "--receipt", str(tmp_path / "unsafe-receipt")],
            os.environ.copy(),
        )[0]
        != 0
    )
    assert not (tmp_path / "unsafe-receipt").exists() and digest(target) == original
    hardlink.unlink()
    driver = tmp_path / "config-fault.py"
    put(
        driver,
        f"""import errno,os,runpy,sys
from pathlib import Path
m=runpy.run_path({str(source)!r},run_name='_installer')
original=os.fsync
def fsync(fd):
 if os.readlink('/proc/self/fd/'+str(fd))=={str(state)!r}:raise OSError(errno.EIO,'SYNTHETIC directory fsync failure')
 return original(fd)
os.fsync=fsync
sys.argv=[{str(source)!r},'install',*{args!r},'--receipt',{str(tmp_path / "partial")!r}]
m['main']()
""",
    )
    assert run([sys.executable, "-B", str(driver)], os.environ.copy())[0] != 0
    assert (
        json.loads(target.read_text()) == new and not (tmp_path / "partial/installed.json").exists()
    )
    assert not list(state.glob(".bindings-pr20-*"))
    changed = digest(target)
    rollback = base + [
        "rollback",
        "--state",
        str(state),
        "--input",
        str(tmp_path / "partial/before.json"),
        "--expected-sha256",
        changed,
        "--receipt",
        str(tmp_path / "rollback"),
    ]
    assert run(rollback, os.environ.copy())[0] == 0
    assert digest(target) == original
    assert run(rollback, os.environ.copy())[0] != 0  # no consumed receipt replay
    # Unrelated edits after adoption cannot be silently rolled back.
    put(target, json.dumps(dict(version=2, bindings=[])))
    assert (
        run(
            base
            + [
                "rollback",
                "--state",
                str(state),
                "--input",
                str(tmp_path / "partial/before.json"),
                "--expected-sha256",
                changed,
                "--receipt",
                str(tmp_path / "drift-rollback"),
            ],
            os.environ.copy(),
        )[0]
        != 0
    )
    assert not (tmp_path / "drift-rollback").exists()


@pytest.mark.parametrize("mode", ["-O", "-OO"])
def test_privileged_parent_optimized_python_fails_before_intent(tmp_path, mode):
    from scripts.blue_green import release_parent

    rc, out, err = run(
        [sys.executable, mode, release_parent.__file__, "--execute-admitted"], os.environ.copy()
    )
    assert rc != 0 and "optimized-python" in err

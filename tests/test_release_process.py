"""Real finite processes, fake owned App shim, unchanged native helper closure.

No App/network call. External Git/HTTP ports are explicitly owned fixture programs.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts.blue_green.release_credentials import PINS, render
from tests.test_github_transport import (
    BASE,
    ROOT,
    SHA,
    merged_table,
    ordinary,
    response,
)
from tests.test_github_transport import (
    api as api_fixture,
)

owned_api = api_fixture

REPO = Path(__file__).resolve().parents[1]
PR = 160
TRACE = """
def trace(kind):
    fd=int(os.environ['DAILYCHECK_DEPLOY_ATTEMPT_FD']);s=os.fstat(fd)
    with open(os.environ['TRACE'],'a') as f:
        f.write(json.dumps(dict(kind=kind,pid=os.getpid(),pgid=os.getpgrp(),
            fd=fd,device=s.st_dev,inode=s.st_ino))+'\\n')
"""
SHIM = (
    """import json,os,sys
from pathlib import Path
"""
    + TRACE
    + """
trace('shim')
p=Path(os.environ['CREDENTIAL_COUNT'])
n=int(p.read_text())+1 if p.exists() else 1;p.write_text(str(n))
if n==2 and 'SHIM_GATE' in os.environ:
    Path(os.environ['SHIM_READY']).write_text(str(os.getpid()))
    with open(os.environ['SHIM_GATE'],'rb',buffering=0) as gate:assert gate.read(1)==b'G'
if os.environ['CASE']=='shim-failure':raise SystemExit(17)
if os.environ['CASE']=='finalizer-shim-failure' and n==2:raise SystemExit(33)
if os.environ['CASE']=='report-shim-failure' and n==3:raise SystemExit(34)
if os.environ['CASE']=='continuation-fence-loss' and n==2:
    os.close(int(os.environ['DAILYCHECK_DEPLOY_ATTEMPT_FD']))
# Model expiry deterministically: the startup value is explicitly invalidated
# by the caller before late phases; no clock delay or live credential involved.
os.environ['DAILYCHECK_GITHUB_READ_TOKEN']='read-phase-'+str(n)
os.environ['DAILYCHECK_GITHUB_REPORT_TOKEN']='report-phase-'+str(n)
Path(os.environ['DAILYCHECK_GITHUB_FINALIZE_TOKEN_FILE']).write_text('finalize-phase-'+str(n))
assert sys.argv[1]=='--exec'
os.execv(sys.argv[2],sys.argv[2:])
"""
)
DRIVER = (
    """import argparse,contextlib,io,json,os,shutil,subprocess,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/'scripts/blue_green'))
import deploy_state as ds
"""
    + TRACE
    + """
trace('pipeline')
os.umask(0o077)
p=json.loads(Path(os.environ['PROFILE']).read_text());case=os.environ['CASE']
closure=Path(p['tmp'])/'dailycheck-pipeline-helpers-owned';closure.mkdir(mode=0o700)
for relative in p['pins']:
    if relative.endswith('deploy_pipeline.sh'):continue
    target=closure/relative;target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    shutil.copyfile(Path(p['repo'])/relative,target);target.chmod(0o600)
for relative in ('scripts/__init__.py','scripts/prod_agent/__init__.py'):
    (closure/relative).write_text('')
capture=io.StringIO()
with contextlib.redirect_stdout(capture):
    rc=ds.cmd_acquire(argparse.Namespace(pr=p['pr'],sha=p['head'],session=p['session'],
       phase='merge-start',deployment_provider='github',deployment_host='github.com',
       deployment_repository='DUIDUI-Studio/DailyCheck'))
assert rc==0,capture.getvalue()
os.environ[ds.ATTEMPT_ENV]=capture.getvalue()
# A client/config constructed at startup retains phase1 even after file changes.
sys.path.insert(0,str(Path(p['repo'])))
from scripts.hosting import HostingClient,HostingConfig,AccessRole
stale=HostingClient(HostingConfig.from_environment(AccessRole.FINALIZE))
assert stale.config.token=='finalize-phase-1'
Path(os.environ['DAILYCHECK_GITHUB_FINALIZE_TOKEN_FILE']).write_text('expired-startup')
assert stale.config.token=='finalize-phase-1'
args=[str(closure/'scripts/blue_green/deploy_merge.py'),'merge','--pr',str(p['pr']),
      '--sha',p['head'],'--base',p['base']]
if case=='bad-pr':args[3]='13'
if case=='bad-head':args[5]='0'*40
if case=='bad-base':args[7]='0'*40
if case=='extra-argument':args+=['--force']
if case=='bad-helper-path':args[0]=str(Path(p['repo'])/'scripts/blue_green/deploy_merge.py')
if case in ('wrong-session','wrong-token','wrong-identity-pr','wrong-identity-head','float-pr'):
    identity=json.loads(os.environ[ds.ATTEMPT_ENV])
    key={'wrong-session':'session','wrong-token':'token','wrong-identity-pr':'pr',
         'wrong-identity-head':'sha','float-pr':'pr'}[case]
    identity[key]=float(p['pr']) if case=='float-pr' else 13 if key=='pr' else 'wrong'
    os.environ[ds.ATTEMPT_ENV]=json.dumps(identity)
if case in ('wrong-phase','wrong-generation','wrong-provenance'):
    s=ds._load_state(Path(p['state']))
    if case=='wrong-phase':s['lock']['phase']='verify-start'
    if case=='wrong-generation':s['lock']['generation']+=1
    if case=='wrong-provenance':s['lock']['deployment']['repository']='wrong/repo'
    ds._save_state(Path(p['state']),s)
if case=='helper-hash-drift':Path(args[0]).write_text(Path(args[0]).read_text()+'\\n# drift')
if case=='shim-hash-drift':Path(p['shim']).write_text(Path(p['shim']).read_text()+'\\n# drift')
if case=='unknown-finalizer':
    (Path(p['root'])/'renewal-intent-finalizer.json').write_text('{}')
if case=='foreign-state':os.environ['DAILYCHECK_DEPLOY_STATE_DIR']=str(Path(p['state'])/'foreign')
fds=(int(os.environ[ds.ATTEMPT_FD_ENV]),)
if case=='lost-fence':os.close(fds[0]);fds=()
prefix=[p['python'],'-B',str(Path(p['root'])/'selector.py')]
if case=='forged-continuation':
    r,w=os.pipe();os.write(w,b'{}');os.close(w);fds+= (r,)
    prefix+=['--renewal-return',str(r)]
if case=='missing-continuation-fd':prefix+=['--renewal-return','9999']
def run(argv):
    child=subprocess.Popen(argv,pass_fds=fds)
    rc=child.wait(timeout=20)
    try:os.waitpid(child.pid,os.WNOHANG)
    except ChildProcessError:pass
    else:raise AssertionError('missing ECHILD')
    return rc
rc=run(prefix+args)
if case=='duplicate-finalizer':
    assert rc==0
    raise SystemExit(run(prefix+args))
if case not in ('happy','duplicate-report','native-failure','native-unknown','unknown-report','report-shim-failure','settled-foreign-failure'):raise SystemExit(rc)
expected=32 if case in ('native-failure','native-unknown') else 0
assert rc==expected,(case,rc)
with contextlib.redirect_stdout(io.StringIO()):
    rc=ds.cmd_release(argparse.Namespace(token=None,result='failed' if expected else 'success',
       sha=p['head'],verification='normal',rc=expected,stage='merge' if expected else 'complete',note=''))
assert rc==0
report=[str(closure/'scripts/blue_green/deploy_report.py'),'write','--pr',str(p['pr']),
        '--sha',p['head'],'--rc',str(expected),'--stage','',
        '--log',p['log'],'--repo',p['repo']]
if case=='unknown-report':(Path(p['root'])/'renewal-intent-typed-report.json').write_text('{}')
rc=run(prefix+report)
if case=='duplicate-report' and rc==0:rc=run(prefix+report)
raise SystemExit(expected or rc)
"""
)


def put(path, text):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o700)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture(tmp_path, monkeypatch, owned_api, case):
    api = owned_api
    table = merged_table(ordinary(api, tmp_path, monkeypatch))
    # Actual report consumer confirms private identity and typed comment through
    # the same owned fake HTTP executable as the repository finalizer tests.
    ops = "DUIDUI-Studio/DailyCheck-ops"
    table["/repos/" + ops] = response(
        dict(
            id=1449125557,
            full_name=ops,
            private=True,
            visibility="private",
            has_issues=True,
            archived=False,
            disabled=False,
            url="https://api.github.com/repos/" + ops,
            html_url="https://github.com/" + ops,
        )
    )
    table[ROOT]["data"].update(
        private=False, visibility="public", has_issues=True, archived=False, disabled=False
    )
    table[ROOT + "/issues/160/comments?page=1"] = response([])
    table[ROOT + "/issues/160/comments"] = response(
        dict(id=201, issue_url="https://api.github.com" + ROOT + "/issues/160"), status=201
    )
    if case == "native-failure":
        table[ROOT + "/branches/main/protection"] = response({}, status=403)
    table[ROOT + "/issues/160/comments"]["echo_comment"] = True
    if case == "native-unknown":
        table["/graphql"] = {"exit": 56, "status": 0, "data": None}
    api.write(table)
    curl = Path(os.environ["DAILYCHECK_CURL"])
    code = curl.read_text().replace(
        "headers = ",
        """if row.get('echo_comment'):
    line=next(line for line in config.splitlines() if line.startswith('data = '))
    payload=json.loads(json.loads(line[7:]));row['data']['body']=payload['body']
headers = """,
        1,
    )
    code = code.replace("log = {", 'log = {"parent_pid":os.getppid(), "pgid":os.getpgrp(),')
    curl.write_text(code)
    root, repo, state = tmp_path / "bundle", tmp_path / "repo", tmp_path / "deploy-state"
    repo.mkdir(mode=0o700)
    state.mkdir(mode=0o700)
    for relative in PINS:
        put(repo / relative, (REPO / relative).read_text())
    put(repo / "driver.py", DRIVER)
    put(
        repo / "scripts/blue_green/deploy_pipeline.sh",
        "#!/bin/bash\nexec " + sys.executable + " -B " + str(repo / "driver.py") + ' "$@"\n',
    )
    shim = tmp_path / "fake-shim.py"
    put(shim, SHIM)
    mapping = {
        "version": 1,
        "bindings": [
            {
                "pr": PR,
                "public": {
                    "provider": "github",
                    "host": "github.com",
                    "repository_id": 1448986421,
                    "repository": "DUIDUI-Studio/DailyCheck",
                },
                "source": {
                    "provider": "github",
                    "host": "github.com",
                    "repository_id": 1449125557,
                    "repository": ops,
                    "kind": "issue",
                    "number": 7,
                },
            }
        ],
    }
    (state / "bindings.json").write_text(json.dumps(mapping))
    # Get the digest from the real resolver; test target number is deliberately
    # different from the public PR and requires no private repository.
    sys.path.insert(0, str(REPO / "scripts/blue_green"))
    import publication_authority

    binding = publication_authority.resolve_source_binding(state, PR)
    profile = dict(
        uid=os.getuid(),
        python=os.path.abspath(sys.executable),
        root=str(root),
        repo=str(repo),
        state=str(state),
        tmp=str(root / "scratch/tmp"),
        log=str(tmp_path / "pipeline.log"),
        shim=str(shim),
        shim_sha=digest(shim),
        head=SHA,
        base=BASE,
        pr=PR,
        generation=11 if case in ("settled-foreign-failure", "settled-target-failure") else 10,
        deployed_generation=10,
        session="owned-release-test",
        task="owned-task",
        dispatch="owned-dispatch",
        pins={name: digest(repo / name) for name in PINS},
        binding_sha256=binding.binding_sha256,
    )
    render(profile, root)
    (tmp_path / "profile.json").write_text(json.dumps(profile))
    (state / "state.json").write_text(
        json.dumps(
            dict(
                version=1,
                generation=10,
                lock=None,
                last_attempt=None,
                last_evicted=None,
                outcomes={},
                publications={},
                deployed=dict(sha=BASE, pr=13, generation=10),
            )
        )
    )
    if case in ("settled-foreign-failure", "settled-target-failure"):
        # Real same-process native fence/acquire/release creates a durable failed
        # outcome; separate legacy CLI commands would only create last_attempt.
        old_pr = PR if case == "settled-target-failure" else 159
        old_sha = SHA if case == "settled-target-failure" else "f" * 40
        setup = tmp_path / "historical_setup.py"
        setup.write_text(f"""import argparse,contextlib,io,os,sys
sys.path.insert(0,{str(repo / "scripts/blue_green")!r})
import deploy_state as ds
if len(sys.argv)==1:
    ds.cmd_run(argparse.Namespace(exec=[sys.executable,"-B",__file__,"held"]))
else:
    capture=io.StringIO()
    with contextlib.redirect_stdout(capture):
        rc=ds.cmd_acquire(argparse.Namespace(pr={old_pr!r},sha={old_sha!r},
          session="settled-historical-failure",phase="verify-start",deployment_provider="github",
          deployment_host="github.com",deployment_repository="DUIDUI-Studio/DailyCheck"))
    assert rc==0,capture.getvalue()
    os.environ[ds.ATTEMPT_ENV]=capture.getvalue()
    raise SystemExit(ds.cmd_release(argparse.Namespace(token=None,result="failed",sha={old_sha!r},
        verification="normal",rc=21,stage="verify",note="retained historical fixture failure")))
""")
        previous = subprocess.Popen(
            [sys.executable, "-B", str(setup)],
            env=dict(os.environ, DAILYCHECK_DEPLOY_STATE_DIR=str(state)),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = previous.communicate(timeout=10)
        assert previous.wait() == 0, (stdout, stderr)
        with pytest.raises(ChildProcessError):
            os.waitpid(previous.pid, os.WNOHANG)
        assert json.loads((state / "state.json").read_text())["outcomes"]
    tools = tmp_path / "bin"
    tools.mkdir()
    put(
        tools / "git",
        f"""#!{sys.executable}
import sys
args=sys.argv[1:]
if args[:1]==['-C']:args=args[2:]
if args[:1]==['rev-parse']:print({SHA!r})
elif args[:1]==['diff-index']:pass
else:sys.exit(128)
""",
    )
    env = dict(
        os.environ,
        CASE=case,
        PROFILE=str(tmp_path / "profile.json"),
        TRACE=str(tmp_path / "trace"),
        CREDENTIAL_COUNT=str(tmp_path / "credential-count"),
        PATH=str(tools) + os.pathsep + os.environ["PATH"],
        DAILYCHECK_PROD_REPO=str(repo),
        DAILYCHECK_PROD_AGENT_INBOX=str(tmp_path / "inbox"),
        DAILYCHECK_DASHBOARD_HOOK="0",
    )
    for key in ("PYTHONPATH", "DAILYCHECK_DEPLOY_ATTEMPT", "DAILYCHECK_DEPLOY_ATTEMPT_FD"):
        env.pop(key, None)
    if case == "missing-binding":
        (state / "bindings.json").unlink()
    if case in ("ambiguous-binding", "wrong-binding"):
        if case == "ambiguous-binding":
            mapping["bindings"] *= 2
        else:
            mapping["bindings"][0]["source"]["number"] = 99
        (state / "bindings.json").write_text(json.dumps(mapping))
    if case == "invalid-binding":
        mapping["bindings"][0]["source"]["repository_id"] = True
        (state / "bindings.json").write_text(json.dumps(mapping))
    if case == "unknown-startup":
        (root / "renewal-intent-startup.json").write_text("{}")
    if case == "startup-hash-drift":
        put(repo / "scripts/blue_green/deploy_merge.py", "# drift\n")
    if case == "startup-baseline-drift":
        data = json.loads((state / "state.json").read_text())
        data["deployed"]["sha"] = "0" * 40
        (state / "state.json").write_text(json.dumps(data))
    return profile, env


CASES = [
    "happy",
    "settled-foreign-failure",
    "settled-target-failure",
    "unknown-startup",
    "unknown-report",
    "finalizer-shim-failure",
    "report-shim-failure",
    "continuation-fence-loss",
    "shim-failure",
    "bad-pr",
    "bad-head",
    "bad-base",
    "extra-argument",
    "bad-helper-path",
    "wrong-session",
    "wrong-token",
    "wrong-identity-pr",
    "wrong-identity-head",
    "float-pr",
    "wrong-phase",
    "wrong-generation",
    "wrong-provenance",
    "helper-hash-drift",
    "shim-hash-drift",
    "lost-fence",
    "foreign-state",
    "forged-continuation",
    "missing-continuation-fd",
    "native-failure",
    "native-unknown",
    "duplicate-finalizer",
    "duplicate-report",
    "unknown-finalizer",
    "missing-binding",
    "ambiguous-binding",
    "wrong-binding",
    "invalid-binding",
    "startup-hash-drift",
    "startup-baseline-drift",
]


@pytest.mark.parametrize("case", CASES)
def test_finite_owned_release(tmp_path, monkeypatch, owned_api, case):
    api = owned_api
    profile, env = fixture(tmp_path, monkeypatch, api, case)
    historical = json.loads((Path(profile["state"]) / "state.json").read_text())["outcomes"]
    child = subprocess.Popen(
        [sys.executable, "-B", str(Path(profile["root"]) / "entry.py"), "--execute-admitted"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = child.communicate(timeout=35)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.communicate()
        child.wait()
        raise AssertionError("owned process timed out, killed and positively waited") from None
    expected = (
        0
        if case in ("happy", "settled-foreign-failure")
        else 17
        if case == "shim-failure"
        else 32
        if case in ("native-failure", "native-unknown")
        else 33
        if case == "finalizer-shim-failure"
        else 34
        if case == "report-shim-failure"
        else 78
    )
    assert child.wait() == expected, (case, stdout.decode(), stderr.decode())
    with pytest.raises(ChildProcessError):
        os.waitpid(child.pid, os.WNOHANG)
    trace = tmp_path / "trace"
    events = [json.loads(x) for x in trace.read_text().splitlines()] if trace.exists() else []
    count = sum(x["kind"] == "shim" for x in events)
    expected_count = (
        0
        if case
        in (
            "settled-target-failure",
            "unknown-startup",
            "missing-binding",
            "ambiguous-binding",
            "wrong-binding",
            "invalid-binding",
            "startup-hash-drift",
            "startup-baseline-drift",
        )
        else 3
        if case
        in (
            "happy",
            "native-failure",
            "native-unknown",
            "duplicate-report",
            "report-shim-failure",
            "settled-foreign-failure",
        )
        else 2
        if case
        in (
            "duplicate-finalizer",
            "unknown-report",
            "finalizer-shim-failure",
            "continuation-fence-loss",
        )
        else 1
    )
    assert count == expected_count, (case, stderr.decode(), events)
    assert len({(e["fd"], e["device"], e["inode"]) for e in events}) <= 1
    assert all(e["pgid"] == child.pid for e in events)
    if count and case != "shim-failure":
        assert events[0]["pid"] == events[1]["pid"] == child.pid
    if case in ("happy", "settled-foreign-failure"):
        posts = [r for r in api.log() if r["method"] == "POST"]
        assert [r["path"] for r in posts] == ["/graphql", ROOT + "/issues/160/comments"]
        assert "finalize-phase-2" in posts[0]["config"]
        assert "report-phase-3" in posts[1]["config"]
        shims = [e for e in events if e["kind"] == "shim"]
        assert posts[0]["parent_pid"] == shims[1]["pid"]
        assert posts[1]["parent_pid"] == shims[2]["pid"]
        assert all(r["pgid"] == child.pid for r in api.log())
        assert all("expired-startup" not in r["config"] for r in api.log())
        assert posts[0]["retained_fences"] == {".attempt.lock": True, ".state.lock": True}
    if historical:
        current = json.loads((Path(profile["state"]) / "state.json").read_text())
        assert all(current["outcomes"][key] == value for key, value in historical.items())
    if case == "settled-target-failure":
        assert count == 0 and not api.log()
    if case in ("missing-binding", "ambiguous-binding", "wrong-binding", "invalid-binding"):
        reason = {
            "missing-binding": "private_source_binding_missing",
            "ambiguous-binding": "private_source_binding_ambiguous",
            "invalid-binding": "private_source_binding_invalid",
            "wrong-binding": "source-binding-changed",
        }[case]
        assert reason in stderr.decode()
        assert not api.log()  # before credential renewal and any candidate/HTTP side effect
        current = json.loads((Path(profile["state"]) / "state.json").read_text())
        assert current["generation"] == 10 and current["lock"] is None
        assert current["outcomes"] == {}


def test_fence_contender_and_consumed_entry(tmp_path, monkeypatch, owned_api):
    profile, env = fixture(tmp_path, monkeypatch, owned_api, "happy")
    gate, ready = tmp_path / "gate", tmp_path / "ready"
    os.mkfifo(gate, 0o600)
    env.update(SHIM_GATE=str(gate), SHIM_READY=str(ready))
    argv = [sys.executable, "-B", str(Path(profile["root"]) / "entry.py"), "--execute-admitted"]
    child = subprocess.Popen(
        argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
    )
    try:
        end = time.monotonic() + 10
        while not ready.exists():
            assert child.poll() is None
            assert time.monotonic() < end
            time.sleep(0.01)
        # The late shim is blocked with the actual inherited attempt FD open.
        effect = tmp_path / "forbidden-effect"
        contender = subprocess.Popen(
            [
                sys.executable,
                "-B",
                str(Path(profile["repo"]) / "scripts/blue_green/deploy_state.py"),
                "run",
                "--",
                sys.executable,
                "-c",
                "from pathlib import Path;Path(" + repr(str(effect)) + ").touch()",
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        contender.communicate(timeout=10)
        assert contender.wait() == 10
        with pytest.raises(ChildProcessError):
            os.waitpid(contender.pid, os.WNOHANG)
        assert not effect.exists()
        with gate.open("wb", buffering=0) as file:
            file.write(b"G")
        stdout, stderr = child.communicate(timeout=20)
        assert child.wait() == 0, (stdout, stderr)
        with pytest.raises(ChildProcessError):
            os.waitpid(child.pid, os.WNOHANG)
        # Consumed/unknown slots never authorize another startup refresh.
        again = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        again.communicate(timeout=10)
        assert again.wait() == 78
        with pytest.raises(ChildProcessError):
            os.waitpid(again.pid, os.WNOHANG)
        assert (tmp_path / "credential-count").read_text() == "3"
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.communicate()
            child.wait()


def test_transparent_python_preserves_pid_pgid_fd(tmp_path, monkeypatch, owned_api):
    profile, env = fixture(tmp_path, monkeypatch, owned_api, "happy")
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    try:
        child = subprocess.Popen(
            [
                sys.executable,
                "-B",
                str(Path(profile["root"]) / "selector.py"),
                "-c",
                "import os;os.fstat(" + str(read_fd) + ");print(os.getpid(),os.getpgrp())",
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(read_fd,),
            start_new_session=True,
        )
        stdout, stderr = child.communicate(timeout=10)
        assert child.wait() == 0, stderr
        assert stdout.decode().strip() == f"{child.pid} {child.pid}"
        with pytest.raises(ChildProcessError):
            os.waitpid(child.pid, os.WNOHANG)
        assert not (tmp_path / "credential-count").exists()
    finally:
        os.close(read_fd)

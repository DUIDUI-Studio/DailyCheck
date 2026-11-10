"""Real check1/pytest/strict gate under an owned genuine ordinary attempt."""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(os.environ.get("H19_SOURCE_ROOT", Path(__file__).resolve().parents[1]))
BG = ROOT / "scripts/blue_green"

REPRESENTATIVES = '''import sys
sys.path.insert(0, SOURCE_ROOT)
from tests.test_deploy_state import state_dir, test_acquire_creates_lock
from tests.test_deploy_pipeline import sandbox, test_happy_path_order_and_release
from tests.test_provider_breaker import test_fresh_cursor_starts_at_eof_and_never_rescans_old_quota_lines
from tests.test_native_preparation import bridge, test_publish_runtime_baseline_writes_unchanged_consumer_record
from tests.test_ci_report_helpers import report_repo, test_report_no_changed_python_paths_is_clean
from tests.test_blue_green_implementation import test_up_dry_run_key_actions
'''

OWNER = r'''import argparse, contextlib, fcntl, io, json, os, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import deploy_state as ds
base=Path(sys.argv[2]); dirty=sys.argv[3]=='dirty'
os.environ['DAILYCHECK_DEPLOY_STATE_DIR']=str(base/'parent-state')
out=io.StringIO()
with contextlib.redirect_stdout(out):
    rc=ds.cmd_acquire(argparse.Namespace(pr=42, sha='a'*40, session='boundary', phase='verify',
        deployment_provider='github', deployment_host='github.com',
        deployment_repository='DUIDUI-Studio/DailyCheck'))
assert rc==0,out.getvalue()
os.environ[ds.ATTEMPT_ENV]=out.getvalue()
identity=json.loads(out.getvalue())
fd=ds.process_lock_fd(); assert fd is not None
state_guard=(base/'parent-state/.state.lock').open('a+')
fcntl.flock(state_guard,fcntl.LOCK_EX)
extra=(base/'parent-authority').open('w+')
fdnums=[fd,state_guard.fileno(),extra.fileno()]
inodes={str(n):[os.fstat(n).st_dev,os.fstat(n).st_ino] for n in fdnums}
(base/'authority.json').write_text(json.dumps(inodes))
for role in ['READ','REPORT','FINALIZE','OPS_READ','FEEDBACK']:
    file=base/(role.lower()+'-role'); file.write_text('synthetic-'+role)
    os.environ['DAILYCHECK_GITHUB_'+role+'_TOKEN_FILE']=str(file)
os.environ.update(DAILYCHECK_HOSTING_PROVIDER='github',DAILYCHECK_GITHUB_LANE='ordinary',
    DAILYCHECK_BG_LOCK_HELD='1',DAILYCHECK_GITHUB_SHADOW_DIR='',
    DAILYCHECK_PROD_AGENT_INBOX=str(base/'parent-inbox'),
    DAILYCHECK_DEPLOY_EVENT_LOG=str(base/'parent-events'),
    DAILYCHECK_DB_DIR=str(base/'parent-db'),GITHUB_TOKEN='synthetic-ambient',
    SIM_USER_PASSWORD='synthetic-sim',DC_NATIVE_LOG_DIR=str(base/'parent-log'))
# This synthetic ordinary case is not native activation.
before=dict(os.environ)
lock_before=(base/'parent-state/state.json').read_bytes()
clean={k:os.environ[k] for k in ('PATH','LANG') if k in os.environ}
clean.update(PYTHONPATH=str(Path(sys.argv[1]).parents[1]),PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',
    PYTHONDONTWRITEBYTECODE='1',HOME=str(base/'clean-home'),TMPDIR=str(base/'tmp'),
    DAILYCHECK_DB_DIR=str(base/'clean-db'))
def competing(where):
    command=[sys.executable,str(Path(sys.argv[1])/'deploy_state.py'),'run','--',
             sys.executable,'-c',"raise SystemExit('unexpected competitor entry')"]
    env=dict(clean,DAILYCHECK_DEPLOY_STATE_DIR=str(base/'parent-state'))
    p=subprocess.Popen(command,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    with (base/'pids.jsonl').open('a') as f:f.write(json.dumps({'pid':p.pid,'where':where,'before_IO':True})+'\n')
    text,_=p.communicate(); code=p.wait(); assert code==10,text
    with (base/'pids.jsonl').open('a') as f:f.write(json.dumps({'pid':p.pid,'positive_wait_exit':code})+'\n')
    return code
checks=[competing('before')]
env=dict(os.environ) if dirty else clean
# Simulate the real omitted-venv PATH only in the inherited case.
env['PATH']='/usr/bin:/bin:/usr/sbin:/sbin' if dirty else str(Path(sys.executable).parent)+':/usr/bin:/bin'
seam=subprocess.Popen(['bash',str(base/'check1.sh')],env=env,
    pass_fds=tuple(fdnums) if dirty else (),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
with (base/'pids.jsonl').open('a') as f:f.write(json.dumps({'pid':seam.pid,'where':'check1','before_IO':True})+'\n')
try:
    deadline=time.monotonic()+30
    while not (base/'ready').exists() and seam.poll() is None and time.monotonic()<deadline:time.sleep(.02)
    if (base/'ready').exists():
        checks.append(competing('during'))
        (base/'gate').write_text('continue')
    output,_=seam.communicate(timeout=40)
finally:
    if seam.poll() is None:seam.kill()
    seam.wait()
    with (base/'pids.jsonl').open('a') as f:f.write(json.dumps({'pid':seam.pid,'positive_wait_exit':seam.returncode})+'\n')
checks.append(competing('after'))
assert dict(os.environ)==before
assert (base/'parent-state/state.json').read_bytes()==lock_before
assert ds.process_lock_fd()==fd
assert all([os.fstat(int(n)).st_dev,os.fstat(int(n)).st_ino]==v for n,v in inodes.items())
state_guard.close();extra.close()
with contextlib.redirect_stdout(io.StringIO()):
    release=ds.cmd_release(argparse.Namespace(token=identity['token'],result='failed',sha='a'*40,
            note='owned boundary proof',rc=21,stage='verify',verification='normal'))
assert release==0
print(json.dumps({'check1_rc':seam.returncode,'output':output,'competitor_codes':checks,
    'parent_environment_unchanged':True,'parent_lock_unchanged_until_release':True,
    'parent_fds_unchanged':True,'release_rc':release}))
'''


def _record(name, value):
    if directory := os.environ.get("H19_PROOF_DIR"):
        (Path(directory) / (name + ".json")).write_text(json.dumps(value, indent=2) + "\n")


def _case(tmp_path, mode="representatives", dirty=True):
    base = tmp_path
    repo = base / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "scripts/ci").mkdir(parents=True)
    for name in ("scripts/ci/pytest_evidence.py", "scripts/check_known_failures.py", "tests/known_failures.txt"):
        shutil.copy2(ROOT / name, repo / name)
    for name in ("tmp", "clean-home", "parent-state"):
        (base / name).mkdir()
    source = (BG / "verify.sh").read_text()
    start = source.index("_verify_check1_pytest() {")
    end = source.index("\n}\n", start) + 3
    seam = source[start:end]
    (base / "check1.sh").write_text(
        '#!/bin/bash\nset -uo pipefail\n'
        + f'REPO_ROOT={str(repo)!r}\nDC_PYTHON_BIN={sys.executable!r}\nVERIFY_TMP_DIR={str(base)!r}\n'
        + seam + '\n_verify_check1_pytest\n')
    if (BG / "verify_pytest.py").exists():
        (repo / "scripts/blue_green").mkdir()
        shutil.copy2(BG / "verify_pytest.py", repo / "scripts/blue_green/verify_pytest.py")
    observations = f'''import json, os, shutil, time
from pathlib import Path
BASE=Path({str(base)!r})
def test_observed_child():
    identities=json.loads((BASE/'authority.json').read_text())
    leaked=[]
    for n, expected in identities.items():
        try:
            s=os.fstat(int(n))
            if [s.st_dev,s.st_ino]==expected:leaked.append(n)
        except OSError:pass
    (BASE/'child.json').write_text(json.dumps({{'authority_fds':leaked,'environment':dict(os.environ),
        'ruff':shutil.which('ruff')}}))
    (BASE/'ready').write_text('ready')
    deadline=time.monotonic()+20
    while not (BASE/'gate').exists() and time.monotonic()<deadline:time.sleep(.02)
    assert (BASE/'gate').exists()
'''
    if mode == "representatives":
        observations += f"\nSOURCE_ROOT={str(ROOT)!r}\n" + REPRESENTATIVES
    elif mode == "failed":
        observations += "\ndef test_failed():\n    assert False, 'owned failing case'\n"
    elif mode == "collection":
        (repo / "tests/test_bad.py").write_text("invalid syntax !\n")
    elif mode == "evidence":
        (repo / "conftest.py").write_text(
            'import pathlib, pytest\n@pytest.hookimpl(hookwrapper=True, tryfirst=True)\n'
            'def pytest_sessionfinish(session,exitstatus):\n    yield\n'
            '    pathlib.Path(session.config.getoption("--dc-evidence")).unlink()\n')
    (repo / "tests/test_case.py").write_text(observations)
    (base / "owner.py").write_text(OWNER)
    # Every executable authority/provider path is synthetic and confined here.
    deny = base / "deny-network"
    deny.write_text("#!/bin/sh\nexit 97\n")
    deny.chmod(0o700)
    env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ") if k in os.environ}
    env.update(DAILYCHECK_DEPLOY_STATE_DIR=str(base / "parent-state"),
               DAILYCHECK_CURL=str(deny),PYTHONDONTWRITEBYTECODE="1",
               PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",PYTHONPATH=str(ROOT),TMPDIR=str(base / "tmp"),
               HOME=str(base / "clean-home"))
    command = [sys.executable, "-B", str(BG / "deploy_state.py"), "run", "--",
               sys.executable, "-B", str(base / "owner.py"), str(BG), str(base),
               "dirty" if dirty else "clean"]
    proc = subprocess.Popen(command, env=env, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)
    (base / "parent-pid.json").write_text(json.dumps({"pid": proc.pid, "before_IO": True}))
    try:
        out, err = proc.communicate(timeout=80)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        (base / "parent-pid.json").write_text(json.dumps({"pid": proc.pid, "before_IO": True,
                                                        "positive_wait_exit": proc.returncode}))
    assert proc.returncode == 0, out + err
    result = json.loads(out)
    result["process_receipts"] = [json.loads(line) for line in (base / "pids.jsonl").read_text().splitlines()]
    result["child"] = json.loads((base / "child.json").read_text()) if (base / "child.json").exists() else None
    result["pytest_log"] = (base / "verify-1-pytest.log").read_text()
    result["native_exit"] = int(re.search(r"pytest native exit=(\d+)", result["output"])[1])
    result["gate_evidence"] = (json.loads((base / "verify-1-pytest-evidence.json").read_text())
                               if (base / "verify-1-pytest-evidence.json").exists() else None)
    _record(mode + ("-dirty" if dirty else "-clean"), result)
    return result


def test_check1_existing_tests_match_clean_parent(tmp_path):
    clean = _case(tmp_path / "clean", dirty=False)
    dirty = _case(tmp_path / "dirty", dirty=True)
    assert clean["check1_rc"] == dirty["check1_rc"] == 0, dirty["pytest_log"]
    assert clean["gate_evidence"]["native_exit"] == dirty["gate_evidence"]["native_exit"] == 0
    assert clean["child"]["authority_fds"] == dirty["child"]["authority_fds"] == []
    assert dirty["competitor_codes"] == [10, 10, 10]
    assert dirty["parent_environment_unchanged"] and dirty["parent_fds_unchanged"]
    assert dirty["child"]["ruff"] == str(Path(sys.executable).parent / "ruff")


@pytest.mark.parametrize("mode, native", [("pass", 0), ("failed", 1), ("collection", 2), ("evidence", 0)])
def test_check1_real_native_and_strict_gate(tmp_path, mode, native):
    result = _case(tmp_path, mode)
    assert result["native_exit"] == native
    assert result["check1_rc"] == (0 if mode == "pass" else 1)
    if result["child"]:
        assert result["child"]["authority_fds"] == []
        env = result["child"]["environment"]
        assert "DAILYCHECK_DEPLOY_ATTEMPT" not in env and "DAILYCHECK_DEPLOY_ATTEMPT_FD" not in env
        assert not any(key.startswith(("DC_NATIVE_", "DAILYCHECK_GITHUB_")) for key in env)
        for key in ("DAILYCHECK_DEPLOY_STATE_DIR", "DAILYCHECK_DEPLOY_EVENT_LOG",
                    "DAILYCHECK_PROD_AGENT_INBOX", "DAILYCHECK_BG_LOCK_FILE", "DAILYCHECK_PROD_REPO"):
            assert env[key].startswith(env["DAILYCHECK_DB_DIR"] + "/")
        assert "DAILYCHECK_HOSTING_PROVIDER" not in env and "GITHUB_TOKEN" not in env
        assert "SIM_USER_PASSWORD" not in env
        assert env["DAILYCHECK_DB_DIR"].startswith(str(tmp_path / "tmp"))

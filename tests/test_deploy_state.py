"""deploy_state.py（部署单飞状态原语）测试。

方式：黑盒 —— 以 subprocess 调 CLI，用临时状态目录 + 文件级时间手术
（直接改 state.json 的 heartbeat_at）控制 TTL，不依赖 sleep、不依赖
flock 命令与 /var 目录（macOS dev / Linux CI 行为一致）。

覆盖：check / acquire / heartbeat / release / catchup / init /
clear-lock / status 的全部状态机路径与退出码。
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "blue_green" / "deploy_state.py"


@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / "deploy-state"
    d.mkdir()
    return d


def _run(state_dir: Path, *args: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["DAILYCHECK_DEPLOY_STATE_DIR"] = str(state_dir)
    env.pop("DAILYCHECK_DEPLOY_HEARTBEAT_TTL", None)
    env.pop("DAILYCHECK_DEPLOY_UNLOCKED", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, env=env, timeout=30,
    )


def _out(proc: subprocess.CompletedProcess) -> dict:
    return json.loads(proc.stdout.strip())


def _state(state_dir: Path) -> dict:
    return json.loads((state_dir / "state.json").read_text(encoding="utf-8"))


def _age_lock(state_dir: Path, seconds: int = 2000) -> None:
    """把锁的心跳时间拨旧，模拟「持有者卡死/掉线」。"""
    data = _state(state_dir)
    assert data["lock"] is not None
    data["lock"]["heartbeat_at"] = int(time.time()) - seconds
    (state_dir / "state.json").write_text(json.dumps(data), encoding="utf-8")


def _acquire(state_dir: Path, pr: int | None = 7, sha: str = "aaa111") -> str:
    args = ["acquire"]
    if pr is not None:
        args += ["--pr", str(pr)]
    args += ["--sha", sha]
    proc = _run(state_dir, *args)
    assert proc.returncode == 0, proc.stderr
    return _out(proc)["token"]


# ----------------------------------------------------------------------
# check
# ----------------------------------------------------------------------

def test_check_fresh_dir_inactive(state_dir: Path):
    proc = _run(state_dir, "check")
    assert proc.returncode == 0
    payload = _out(proc)
    assert payload["active"] is False
    assert payload["activated"] is True


def test_check_no_state_dir(tmp_path: Path):
    missing = tmp_path / "not-created-yet"
    proc = _run(missing, "check")
    assert proc.returncode == 0
    payload = _out(proc)
    assert payload["active"] is False
    assert payload["activated"] is False
    assert not missing.exists()  # check 无副作用


def test_check_active_lock(state_dir: Path):
    _acquire(state_dir)
    proc = _run(state_dir, "check")
    assert proc.returncode == 10
    payload = _out(proc)
    assert payload["active"] is True
    assert payload["lock"]["pr"] == 7


def test_check_pr_cross_validation(state_dir: Path):
    _acquire(state_dir, pr=7)
    ok = _run(state_dir, "check", "--pr", "7")
    assert ok.returncode == 10
    mismatch = _run(state_dir, "check", "--pr", "9")
    assert mismatch.returncode == 12
    assert _out(mismatch)["pr_mismatch"] is True


def test_check_unbound_lock_passes_any_pr(state_dir: Path):
    _acquire(state_dir, pr=None)
    proc = _run(state_dir, "check", "--pr", "42")
    assert proc.returncode == 10  # 锁未绑定 PR -> 不判 mismatch


# ----------------------------------------------------------------------
# acquire
# ----------------------------------------------------------------------

def test_acquire_creates_lock(state_dir: Path):
    token = _acquire(state_dir)
    data = _state(state_dir)
    assert data["lock"]["pr"] == 7
    assert data["lock"]["sha"] == "aaa111"
    assert data["lock"]["token"] == token


def test_acquire_second_attempt_blocked(state_dir: Path):
    _acquire(state_dir)
    proc = _run(state_dir, "acquire", "--pr", "8", "--sha", "bbb")
    assert proc.returncode == 10
    assert _out(proc)["reason"] == "lock-busy"


def test_acquire_evicts_stale_lock(state_dir: Path):
    _acquire(state_dir, pr=7)
    _age_lock(state_dir)
    proc = _run(state_dir, "acquire", "--pr", "8", "--sha", "bbb")
    assert proc.returncode == 0
    data = _state(state_dir)
    assert data["lock"]["pr"] == 8
    assert data["last_evicted"]["pr"] == 7


def test_lock_survives_verify_length_gap(state_dir: Path):
    """回归：verify 实测 12-14 分钟 —— 旧 TTL 900s 会在 verify 中途过期被抢占
    （2026-10-17 PR #96 生产实测 last_evicted：心跳 20:17:10 → 20:37:19 被抢）。
    TTL 必须明显长于单步最长耗时，否则「单飞」被合法抢占打穿。"""
    _acquire(state_dir)
    _age_lock(state_dir, seconds=1000)  # 旧 900s 已判过期；1800s 仍应活跃
    assert _run(state_dir, "check").returncode == 10
    assert "1800s" in _run(state_dir, "status").stdout


def test_acquire_creates_missing_dir(tmp_path: Path):
    missing = tmp_path / "fresh"
    proc = _run(missing, "acquire", "--pr", "1", "--sha", "x")
    assert proc.returncode == 0
    assert (missing / "state.json").exists()


# ----------------------------------------------------------------------
# heartbeat
# ----------------------------------------------------------------------

def test_heartbeat_refreshes(state_dir: Path):
    token = _acquire(state_dir)
    data = _state(state_dir)
    data["lock"]["heartbeat_at"] = int(time.time()) - 100
    (state_dir / "state.json").write_text(json.dumps(data), encoding="utf-8")
    proc = _run(state_dir, "heartbeat", "--token", token, "--phase", "verify")
    assert proc.returncode == 0
    payload = _out(proc)
    assert payload["revived"] is False
    fresh = _state(state_dir)["lock"]
    assert fresh["heartbeat_at"] >= int(time.time()) - 5
    assert fresh["phase"] == "verify"


def test_heartbeat_revives_stale_lock(state_dir: Path):
    token = _acquire(state_dir)
    _age_lock(state_dir)
    proc = _run(state_dir, "heartbeat", "--token", token)
    assert proc.returncode == 0
    assert _out(proc)["revived"] is True
    assert _run(state_dir, "check").returncode == 10  # 复活后有活跃锁


def test_heartbeat_bad_token(state_dir: Path):
    _acquire(state_dir)
    proc = _run(state_dir, "heartbeat", "--token", "deadbeefdeadbeef")
    assert proc.returncode == 11


def test_heartbeat_no_lock(state_dir: Path):
    proc = _run(state_dir, "heartbeat", "--token", "0123456789abcdef")
    assert proc.returncode == 11


# ----------------------------------------------------------------------
# release
# ----------------------------------------------------------------------

def test_release_success_sets_deployed(state_dir: Path):
    token = _acquire(state_dir, pr=7, sha="aaa111")
    proc = _run(state_dir, "release", "--token", token, "--result", "success")
    assert proc.returncode == 0
    data = _state(state_dir)
    assert data["deployed"]["sha"] == "aaa111"
    assert data["deployed"]["pr"] == 7
    assert data["lock"] is None
    assert _run(state_dir, "check").returncode == 0


def test_release_success_explicit_sha_overrides(state_dir: Path):
    token = _acquire(state_dir, pr=7, sha="aaa111")
    proc = _run(state_dir, "release", "--token", token, "--result", "success", "--sha", "ccc333")
    assert proc.returncode == 0
    assert _state(state_dir)["deployed"]["sha"] == "ccc333"


def test_release_failed_records_attempt(state_dir: Path):
    token = _acquire(state_dir, pr=7, sha="aaa111")
    proc = _run(state_dir, "release", "--token", token, "--result", "failed", "--note", "check 3 red")
    assert proc.returncode == 0
    data = _state(state_dir)
    assert data["last_attempt"]["result"] == "failed"
    assert data["last_attempt"]["sha"] == "aaa111"
    assert data["lock"] is None
    assert data["deployed"] is None  # 失败不动锚点


def test_release_bad_token(state_dir: Path):
    _acquire(state_dir)
    proc = _run(state_dir, "release", "--token", "deadbeefdeadbeef", "--result", "success")
    assert proc.returncode == 11


def test_release_success_requires_sha(state_dir: Path):
    token = _acquire(state_dir, pr=7, sha="")
    proc = _run(state_dir, "release", "--token", token, "--result", "success")
    assert proc.returncode == 64


def test_release_success_clears_old_failure(state_dir: Path):
    t1 = _acquire(state_dir, pr=7, sha="aaa111")
    _run(state_dir, "release", "--token", t1, "--result", "failed")
    t2 = _acquire(state_dir, pr=8, sha="bbb222")
    _run(state_dir, "release", "--token", t2, "--result", "success")
    assert _state(state_dir)["last_attempt"] is None


# ----------------------------------------------------------------------
# catchup
# ----------------------------------------------------------------------

def test_catchup_caught_up(state_dir: Path):
    _run(state_dir, "init", "--sha", "main111")
    proc = _run(state_dir, "catchup", "--main-sha", "main111")
    assert proc.returncode == 0
    assert _out(proc)["status"] == "caught-up"


def test_catchup_behind(state_dir: Path):
    _run(state_dir, "init", "--sha", "main111")
    proc = _run(state_dir, "catchup", "--main-sha", "main222")
    assert proc.returncode == 1
    assert _out(proc)["status"] == "behind"


def test_catchup_failed_pending(state_dir: Path):
    _run(state_dir, "init", "--sha", "main111")
    token = _acquire(state_dir, pr=9, sha="main222")
    _run(state_dir, "release", "--token", token, "--result", "failed")
    proc = _run(state_dir, "catchup", "--main-sha", "main222")
    assert proc.returncode == 2
    assert _out(proc)["status"] == "failed-pending"


def test_catchup_failed_pending_clears_on_new_main(state_dir: Path):
    _run(state_dir, "init", "--sha", "main111")
    token = _acquire(state_dir, pr=9, sha="main222")
    _run(state_dir, "release", "--token", token, "--result", "failed")
    proc = _run(state_dir, "catchup", "--main-sha", "main333")
    assert proc.returncode == 1  # main 换新 -> 失败记录不再拦（可再试）


def test_catchup_uninitialized(state_dir: Path):
    proc = _run(state_dir, "catchup", "--main-sha", "whatever")
    assert proc.returncode == 3
    assert _out(proc)["status"] == "uninitialized"


def test_catchup_no_main(state_dir: Path):
    proc = _run(
        state_dir, "catchup",
        extra_env={"DAILYCHECK_PROD_REPO": str(state_dir / "no-such-repo")},
    )
    assert proc.returncode == 65


# ----------------------------------------------------------------------
# init / clear-lock / status / 损坏自愈
# ----------------------------------------------------------------------

def test_init_sets_baseline_and_idempotent(state_dir: Path):
    first = _run(state_dir, "init", "--sha", "base000")
    assert first.returncode == 0
    assert _out(first)["already"] is False
    second = _run(state_dir, "init", "--sha", "base999")
    assert second.returncode == 0
    assert _out(second)["already"] is True
    assert _state(state_dir)["deployed"]["sha"] == "base000"  # 不被覆盖


def test_init_blocked_by_active_lock(state_dir: Path):
    _acquire(state_dir)
    proc = _run(state_dir, "init", "--sha", "base000")
    assert proc.returncode == 10


def test_clear_lock_requires_yes(state_dir: Path):
    _acquire(state_dir)
    refused = _run(state_dir, "clear-lock")
    assert refused.returncode == 64
    assert _run(state_dir, "check").returncode == 10
    cleared = _run(state_dir, "clear-lock", "--yes")
    assert cleared.returncode == 0
    assert _run(state_dir, "check").returncode == 0


def test_status_json_reports_all(state_dir: Path):
    token = _acquire(state_dir, pr=7)
    _run(state_dir, "release", "--token", token, "--result", "failed")
    proc = _run(state_dir, "status", "--json")
    assert proc.returncode == 0
    payload = _out(proc)
    assert payload["lock_active"] is False
    assert payload["last_attempt"]["result"] == "failed"


def test_corrupt_state_heals_on_write(state_dir: Path):
    (state_dir / "state.json").write_text("{not json", encoding="utf-8")
    proc = _run(state_dir, "acquire", "--pr", "1", "--sha", "x")
    assert proc.returncode == 0
    assert list(state_dir.glob("state.json.corrupt-*"))
    assert _state(state_dir)["lock"]["pr"] == 1
    assert "损坏" in proc.stderr


def test_corrupt_state_read_commands_survive(state_dir: Path):
    (state_dir / "state.json").write_text("[]", encoding="utf-8")
    proc = _run(state_dir, "check")
    assert proc.returncode == 0  # 不炸；按空状态处理
    assert not list(state_dir.glob("state.json.corrupt-*"))  # 只读命令不落盘


# ----------------------------------------------------------------------
# token 文件交接（会话层脱敏的规避通道）
#
# 背景：Hermes 会把工具 stdout 里形似密钥的值脱敏成 ***，acquire 输出的
# token 在会话侧拿不到（2026-10-17 PR #96：agent 转去 cat state.json 手抠，
# 撞上「管道进解释器」审批门禁）。因此 token 必须落 0600 文件，heartbeat /
# release 缺省从文件取 —— 这些用例锁住"文件存在、权限对、生命周期对"。
# ----------------------------------------------------------------------

def _token_file(state_dir: Path) -> Path:
    return state_dir / "lock.token"


def test_acquire_writes_token_file(state_dir: Path):
    token = _acquire(state_dir)
    path = _token_file(state_dir)
    assert path.exists()
    assert path.read_text(encoding="utf-8").strip() == token
    assert (path.stat().st_mode & 0o777) == 0o600  # token 不许同机可读


def test_acquire_reports_token_file_path(state_dir: Path):
    proc = _run(state_dir, "acquire", "--pr", "7", "--sha", "aaa111")
    payload = _out(proc)
    assert payload["token_file"] == str(_token_file(state_dir))


def test_acquire_preempt_overwrites_token_file(state_dir: Path):
    first = _acquire(state_dir, pr=7)
    _age_lock(state_dir)
    proc = _run(state_dir, "acquire", "--pr", "8", "--sha", "bbb")
    assert proc.returncode == 0
    now = _token_file(state_dir).read_text(encoding="utf-8").strip()
    assert now == _out(proc)["token"]
    assert now != first  # 抢占必须换 token，旧 token 立即失效


def test_heartbeat_defaults_to_token_file(state_dir: Path):
    _acquire(state_dir)
    # 不传 --token：走 <state_dir>/lock.token
    proc = _run(state_dir, "heartbeat", "--phase", "verify")
    assert proc.returncode == 0
    assert _state(state_dir)["lock"]["phase"] == "verify"


def test_heartbeat_token_file_option(state_dir: Path):
    token = _acquire(state_dir)
    custom = state_dir.parent / "carried.token"
    custom.write_text(token + "\n", encoding="utf-8")
    proc = _run(state_dir, "heartbeat", "--token-file", str(custom))
    assert proc.returncode == 0


def test_heartbeat_missing_token_file_rejected(state_dir: Path):
    _acquire(state_dir)
    _token_file(state_dir).unlink()
    proc = _run(state_dir, "heartbeat")
    assert proc.returncode == 11  # 拿不到 token = fail-closed，不猜


def test_release_defaults_to_token_file(state_dir: Path):
    _acquire(state_dir, pr=7, sha="aaa111")
    proc = _run(state_dir, "release", "--result", "success")
    assert proc.returncode == 0
    assert _state(state_dir)["deployed"]["sha"] == "aaa111"
    assert not _token_file(state_dir).exists()  # 收官后 token 不留在盘上


def test_release_failed_also_removes_token_file(state_dir: Path):
    _acquire(state_dir)
    _run(state_dir, "release", "--result", "failed")
    assert not _token_file(state_dir).exists()


def test_clear_lock_removes_token_file(state_dir: Path):
    _acquire(state_dir)
    assert _token_file(state_dir).exists()
    assert _run(state_dir, "clear-lock", "--yes").returncode == 0
    assert not _token_file(state_dir).exists()


def test_status_reports_token_file(state_dir: Path):
    _acquire(state_dir)
    human = _run(state_dir, "status")
    assert "token_file:" in human.stdout
    assert "present" in human.stdout
    payload = _out(_run(state_dir, "status", "--json"))
    assert payload["token_file"]["present"] is True
    assert payload["token_file"]["path"] == str(_token_file(state_dir))


def test_state_file_not_world_readable(state_dir: Path):
    _acquire(state_dir)
    # token 在 state.json 里也有一份 —— 两个文件都必须 0600
    assert ((state_dir / "state.json").stat().st_mode & 0o777) == 0o600
    # 目录也收紧（否则 0600 文件在 0755 目录里仍可被列举）
    assert (state_dir.stat().st_mode & 0o777) == 0o700



# Genuine typed outcomes from the real process fence and release transaction.
FAILED_HEAD = "a" * 40
FAILED_REPOSITORY = "DUIDUI-Studio/DailyCheck"


def _released_outcome(state_dir, *, pr=160, sha=FAILED_HEAD, provider="github",
                      host="github.com", repository=FAILED_REPOSITORY, result="failed"):
    owner = state_dir.parent / "release-owned-outcome.py"
    owner.write_text(f"""import argparse,contextlib,io,json,os,sys
sys.path.insert(0,{str(SCRIPT.parent)!r})
import deploy_state as ds
out=io.StringIO()
with contextlib.redirect_stdout(out):
 rc=ds.cmd_acquire(argparse.Namespace(pr={pr!r},sha={sha!r},session='owned-failure',phase='verify',
     deployment_provider={provider!r},deployment_host={host!r},deployment_repository={repository!r}))
assert rc==0,out.getvalue()
os.environ[ds.ATTEMPT_ENV]=out.getvalue()
raise SystemExit(ds.cmd_release(argparse.Namespace(token=json.loads(out.getvalue())['token'],
 result={result!r},sha={sha!r},note='owned verification',rc={21 if result == 'failed' else 0},
 stage='verify',verification='normal')))
""")
    proc = _run(state_dir, "run", "--", sys.executable, "-B", str(owner))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    state = _state(state_dir)
    assert state["lock"] is None and len(state["outcomes"]) >= 1
    return state


@pytest.mark.parametrize("publication", ["ready", "uncertain", "linked"])
@pytest.mark.parametrize("qualified", [True, False])
def test_failed_open_head_cannot_acquire_again(state_dir, publication, qualified):
    assert _run(state_dir, "init", "--sha", "c" * 40).returncode == 0
    state = _released_outcome(state_dir)
    oid = state["last_attempt"]["outcome_id"]
    state["publications"][oid] = {"state": publication}
    (state_dir / "state.json").write_text(json.dumps(state))
    (state_dir / "lock.token").write_text("owned-preserved-token\n")
    before = {name: (state_dir / name).read_bytes() for name in ("state.json", "lock.token")}
    extra = (["--deployment-provider", "github", "--deployment-host", "github.com",
              "--deployment-repository", FAILED_REPOSITORY] if qualified else [])
    proc = _run(state_dir, "acquire", "--pr", "160", "--sha", FAILED_HEAD, *extra)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert _out(proc)["reason"] == "failed-head"
    assert {name: (state_dir / name).read_bytes() for name in before} == before
    catchup = _run(state_dir, "catchup", "--main-sha", "b" * 40)
    assert catchup.returncode == 1 and _out(catchup)["status"] == "behind"


@pytest.mark.parametrize("different", ["provider", "host", "repository", "pr", "sha", "prefix"])
def test_failed_head_isolated_from_other_deployments(state_dir, different):
    failed = _released_outcome(state_dir)
    target = {"provider": "github", "host": "github.com", "repository": FAILED_REPOSITORY,
              "pr": 160, "sha": FAILED_HEAD}
    target[different if different != "prefix" else "sha"] = {
        "provider": "gitea", "host": "other.example", "repository": "other/repository",
        "pr": 161, "sha": "d" * 40, "prefix": FAILED_HEAD[:12]}[different]
    proc = _run(state_dir, "acquire", "--pr", str(target["pr"]), "--sha", target["sha"],
                "--deployment-provider", target["provider"], "--deployment-host", target["host"],
                "--deployment-repository", target["repository"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _state(state_dir)["outcomes"] == failed["outcomes"]
    assert _state(state_dir)["generation"] == failed["generation"] + 1


def test_gitea_outcome_reporting_to_github_does_not_block_github_deployment(state_dir):
    state = _released_outcome(state_dir, provider="gitea", host="gitea.example", repository="eric/dailycheck")
    assert next(iter(state["outcomes"].values()))["hosting"]["provider"] == "github"
    proc = _run(state_dir, "acquire", "--pr", "160", "--sha", FAILED_HEAD,
                "--deployment-provider", "github", "--deployment-host", "github.com",
                "--deployment-repository", FAILED_REPOSITORY)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _state(state_dir)["outcomes"] == state["outcomes"]


@pytest.mark.parametrize("damage", ["deployment", "receipt", "result", "generation", "anchor",
                                     "anchor-generation", "missing", "ambiguous",
                                     "deployment-pr", "unproven-foreign"])
def test_relevant_unproven_outcomes_reject_without_state_mutation(state_dir, damage):
    state = _released_outcome(state_dir)
    oid, outcome = next(iter(state["outcomes"].items()))
    if damage == "deployment":
        del outcome["deployment"]
    elif damage == "receipt":
        del outcome["public_receipt"]
    elif damage == "result":
        outcome["result"] = "success"  # actual failure rc21 cannot be relabelled
    elif damage == "generation":
        outcome["generation"] = True
    elif damage == "anchor":
        state["last_attempt"]["outcome_id"] = "missing"
    elif damage == "anchor-generation":
        state["last_attempt"]["generation"] = True
    elif damage == "missing":
        state["outcomes"] = {}
    elif damage == "deployment-pr":
        outcome["deployment"]["pr"] = 161
    elif damage == "unproven-foreign":
        outcome["deployment"] = {"provider": "gitea", "host": "gitea.example",
                                 "repository": "eric/dailycheck", "pr": 160}
        outcome.pop("public_receipt")
    else:
        state["outcomes"]["other-attempt"] = {**outcome, "attempt_id": "other-attempt"}
    path = state_dir / "state.json"
    path.write_text(json.dumps(state))
    before = path.read_bytes()
    proc = _run(state_dir, "acquire", "--pr", "160", "--sha", FAILED_HEAD,
                "--deployment-provider", "github", "--deployment-host", "github.com",
                "--deployment-repository", FAILED_REPOSITORY)
    assert proc.returncode == 2 and _out(proc)["reason"] == "reconcile-needed", proc.stdout + proc.stderr
    assert path.read_bytes() == before and not (state_dir / "lock.token").exists()


@pytest.mark.parametrize("raw", ['{bad', '[]', '{"outcomes":[]}',
                                 '{"generation":1,"generation":2}', '{"generation":NaN}'])
def test_github_corrupt_state_is_not_healed_into_admission(state_dir, raw):
    path = state_dir / "state.json"
    path.write_text(raw)
    proc = _run(state_dir, "acquire", "--pr", "160", "--sha", FAILED_HEAD,
                "--deployment-provider", "github", "--deployment-host", "github.com",
                "--deployment-repository", FAILED_REPOSITORY)
    assert proc.returncode == 2 and _out(proc)["reason"] == "reconcile-needed"
    assert path.read_text() == raw
    assert not list(state_dir.glob("*.corrupt-*")) and not (state_dir / "lock.token").exists()


@pytest.mark.parametrize("barrier", ["process", "lease", "native", "expired"])
def test_failed_head_guard_preserves_existing_fences(state_dir, barrier):
    _released_outcome(state_dir)
    if barrier in {"process", "lease", "expired"}:
        assert _run(state_dir, "acquire", "--pr", "161", "--sha", "d" * 40).returncode == 0
        if barrier != "lease":
            _age_lock(state_dir)
    if barrier == "native":
        (state_dir / "native-queue.guard").write_text('{}')
    path = state_dir / "state.json"
    before = path.read_bytes()
    token = (state_dir / "lock.token").read_bytes() if (state_dir / "lock.token").exists() else None
    with (state_dir / ".attempt.lock").open("a+") as fence:
        if barrier == "process":
            fcntl.flock(fence, fcntl.LOCK_EX | fcntl.LOCK_NB)
        proc = _run(state_dir, "acquire", "--pr", "160", "--sha", FAILED_HEAD,
                    "--deployment-provider", "github", "--deployment-host", "github.com",
                    "--deployment-repository", FAILED_REPOSITORY)
    assert proc.returncode == (2 if barrier == "expired" else 10), proc.stdout + proc.stderr
    assert path.read_bytes() == before
    assert ((state_dir / "lock.token").read_bytes() if token is not None else None) == token


def test_unrelated_malformed_outcome_does_not_poison_new_head(state_dir):
    state = _released_outcome(state_dir)
    next(iter(state["outcomes"].values())).pop("deployment")
    (state_dir / "state.json").write_text(json.dumps(state))
    proc = _run(state_dir, "acquire", "--pr", "160", "--sha", "d" * 40,
                "--deployment-provider", "github", "--deployment-host", "github.com",
                "--deployment-repository", FAILED_REPOSITORY)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _state(state_dir)["outcomes"] == state["outcomes"]


def test_success_without_deployment_provenance_is_not_clearance(state_dir):
    state = _released_outcome(state_dir, result="success")
    next(iter(state["outcomes"].values())).pop("deployment")
    (state_dir / "state.json").write_text(json.dumps(state))
    before = (state_dir / "state.json").read_bytes()
    proc = _run(state_dir, "acquire", "--pr", "160", "--sha", FAILED_HEAD,
                "--deployment-provider", "github", "--deployment-host", "github.com",
                "--deployment-repository", FAILED_REPOSITORY)
    assert proc.returncode == 2 and _out(proc)["reason"] == "reconcile-needed"
    assert (state_dir / "state.json").read_bytes() == before


@pytest.mark.parametrize("host,repository", [("github.com", "other/repository"),
                                             ("other.example", FAILED_REPOSITORY)])
def test_missing_caller_identity_cannot_escape_typed_github_failure(state_dir, host, repository):
    _released_outcome(state_dir, host=host, repository=repository)
    before = (state_dir / "state.json").read_bytes()
    proc = _run(state_dir, "acquire", "--pr", "160", "--sha", FAILED_HEAD)
    assert proc.returncode == 2 and _out(proc)["reason"] == "failed-head"
    assert (state_dir / "state.json").read_bytes() == before


def test_unqualified_success_from_foreign_deployment_cannot_clear_failure(state_dir):
    _released_outcome(state_dir)
    state = _released_outcome(state_dir, repository="other/repository", result="success")
    assert len(state["outcomes"]) == 2
    before = (state_dir / "state.json").read_bytes()
    proc = _run(state_dir, "acquire", "--pr", "160", "--sha", FAILED_HEAD)
    assert proc.returncode == 2 and _out(proc)["reason"] == "reconcile-needed"
    assert (state_dir / "state.json").read_bytes() == before

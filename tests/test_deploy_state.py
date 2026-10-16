"""deploy_state.py（部署单飞状态原语）测试。

方式：黑盒 —— 以 subprocess 调 CLI，用临时状态目录 + 文件级时间手术
（直接改 state.json 的 heartbeat_at）控制 TTL，不依赖 sleep、不依赖
flock 命令与 /var 目录（macOS dev / Linux CI 行为一致）。

覆盖：check / acquire / heartbeat / release / catchup / init /
clear-lock / status 的全部状态机路径与退出码。
"""
from __future__ import annotations

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

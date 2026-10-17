"""deploy_pipeline.sh（端到端部署编排）测试。

方式：黑盒 —— 把 orchestration 脚本 + deploy_state.py 复制进 tmp 沙盒，
用「记录调用顺序」的假子脚本替换 up/verify/promote/switch，因此不碰
systemd / nginx / flock / 真实流量，macOS dev 与 Linux CI 行为一致。

覆盖点（对应 2026-10-17 的四类事故）：
  1. 顺序固定为 up → verify → promote → switch（switch 早于 promote 必被
     DRY_RUN 守卫拒，此前靠 skill 文字约束）。
  2. 失败/中断也 release（锁不悬空，deployed 锚点不假前进）。
  3. 锁被他人持有时 rc 10 且不碰子脚本。
  4. deployed 已等于目标 sha 时幂等跳过。
  5. 后台心跳常驻：阶段耗时超过 TTL 也不会被抢；token 失效即退出。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

BG_DIR = Path(__file__).resolve().parent.parent / "scripts" / "blue_green"
SHA = "abcdef1234567890abcdef1234567890abcdef12"


@pytest.fixture()
def sandbox(tmp_path: Path):
    """最小可执行沙盒：真 deploy_state.py + 真 pipeline + 假四阶段脚本。"""
    sb = tmp_path / "bg"
    sb.mkdir()
    for name in ("deploy_state.py", "deploy_pipeline.sh"):
        shutil.copy2(BG_DIR / name, sb / name)
        (sb / name).chmod(0o755)
    calls = tmp_path / "calls.txt"
    for name, rc in (("up.sh", 0), ("verify.sh", 0), ("promote.sh", 0), ("switch.sh", 0)):
        _write_fake(sb / name, name, calls, rc)
    state = tmp_path / "state"
    state.mkdir()
    return {"dir": sb, "calls": calls, "state": state, "tmp": tmp_path}


def _write_fake(path: Path, label: str, calls: Path, rc: int) -> None:
    path.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "{label} $*" >> "{calls}"\n'
        f"echo \"[fake {label}] args=$*\"\n"
        f"exit {rc}\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _env(sb: dict, **extra) -> dict:
    env = os.environ.copy()
    env["DAILYCHECK_DEPLOY_STATE_DIR"] = str(sb["state"])
    env["DAILYCHECK_PIPELINE_LOG"] = str(sb["tmp"] / "pipeline.log")
    env["DAILYCHECK_PIPELINE_TICK_SEC"] = "1"
    env.pop("DAILYCHECK_DEPLOY_UNLOCKED", None)
    env.pop("DAILYCHECK_DEPLOY_HEARTBEAT_TTL", None)
    env.update({k: str(v) for k, v in extra.items()})
    return env


def _pipe(sb: dict, *args: str, **extra) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(sb["dir"] / "deploy_pipeline.sh"), *args],
        capture_output=True, text=True, env=_env(sb, **extra), timeout=120,
    )


def _ds(sb: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(sb["dir"] / "deploy_state.py"), *args],
        capture_output=True, text=True, env=_env(sb), timeout=30,
    )


def _state(sb: dict) -> dict:
    p = sb["state"] / "state.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _calls(sb: dict) -> list[str]:
    p: Path = sb["calls"]
    return p.read_text(encoding="utf-8").splitlines() if p.exists() else []


# ---------------------------------------------------------------- 参数校验

@pytest.mark.parametrize("args", [(), ("42",), ("99", SHA, "extra")])
def test_usage_errors(sandbox, args):
    proc = _pipe(sandbox, *args)
    assert proc.returncode == 64, proc.stderr
    assert "ERROR" in proc.stdout + proc.stderr


def test_rejects_non_numeric_pr(sandbox):
    assert _pipe(sandbox, "abc", SHA).returncode == 64


def test_rejects_unknown_option(sandbox):
    assert _pipe(sandbox, "42", SHA, "--nope").returncode == 64


def test_rejects_bad_sha(sandbox):
    assert _pipe(sandbox, "42", "zzzz").returncode == 64


def test_help_exits_zero(sandbox):
    proc = _pipe(sandbox, "--help")
    assert proc.returncode == 0
    assert "退出码" in proc.stdout


# ---------------------------------------------------------------- 主链

def test_happy_path_order_and_release(sandbox):
    proc = _pipe(sandbox, "42", SHA, "--observe-min", "0")

    assert proc.returncode == 0, proc.stderr + proc.stdout
    # 顺序是本次修复的核心：switch 必须在 promote 之后
    order = [c.split()[0] for c in _calls(sandbox)]
    assert order == ["up.sh", "verify.sh", "promote.sh", "switch.sh"], order
    assert "green 100" in _calls(sandbox)[-1]

    st = _state(sandbox)
    assert st["deployed"]["sha"] == SHA
    assert st["deployed"]["pr"] == 42
    assert st["lock"] is None
    assert not (sandbox["state"] / "lock.token").exists()


def test_switch_gets_green_100(sandbox):
    _pipe(sandbox, "42", SHA, "--observe-min", "0")
    assert _calls(sandbox)[-1] == "switch.sh green 100"


def test_up_failure_releases_failed_and_skips_rest(sandbox):
    _write_fake(sandbox["dir"] / "up.sh", "up.sh", sandbox["calls"], 7)
    proc = _pipe(sandbox, "42", SHA, "--observe-min", "0")

    assert proc.returncode == 20, proc.stderr
    assert [c.split()[0] for c in _calls(sandbox)] == ["up.sh"]

    st = _state(sandbox)
    assert st["lock"] is None, "失败必须收锁，否则后续部署全被挡"
    assert st["last_attempt"]["result"] == "failed"
    assert "up" in st["last_attempt"]["note"]
    assert st["deployed"] is None, "失败不得推进 deployed 锚点"


def test_verify_failure_does_not_promote(sandbox):
    _write_fake(sandbox["dir"] / "verify.sh", "verify.sh", sandbox["calls"], 1)
    proc = _pipe(sandbox, "42", SHA, "--observe-min", "0")

    assert proc.returncode == 21, proc.stderr
    assert [c.split()[0] for c in _calls(sandbox)] == ["up.sh", "verify.sh"]
    assert _state(sandbox)["deployed"] is None


def test_promote_failure_does_not_switch(sandbox):
    _write_fake(sandbox["dir"] / "promote.sh", "promote.sh", sandbox["calls"], 2)
    proc = _pipe(sandbox, "42", SHA, "--observe-min", "0")

    assert proc.returncode == 22, proc.stderr
    assert "switch.sh" not in " ".join(_calls(sandbox))
    assert _state(sandbox)["deployed"] is None


def test_skip_verify_bypasses_stage(sandbox):
    proc = _pipe(sandbox, "42", SHA, "--skip-verify", "--observe-min", "0")
    assert proc.returncode == 0
    assert "verify.sh" not in " ".join(_calls(sandbox))
    assert "WARN" in proc.stdout


def test_idempotent_when_already_deployed(sandbox):
    first = _pipe(sandbox, "42", SHA, "--observe-min", "0")
    assert first.returncode == 0
    sandbox["calls"].unlink()

    again = _pipe(sandbox, "42", SHA, "--observe-min", "0")
    assert again.returncode == 0
    assert "已部署" in again.stdout
    assert _calls(sandbox) == [], "幂等跳过时不得再动子脚本"


def test_dry_run_does_not_touch_state(sandbox):
    proc = _pipe(sandbox, "42", SHA, "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert "--dry-run" in _calls(sandbox)[0]
    assert _state(sandbox) == {}, "dry-run 不得写部署状态"
    assert not (sandbox["state"] / "lock.token").exists()


# ---------------------------------------------------------------- 锁

def test_blocked_by_occupied_lock(sandbox):
    acq = _ds(sandbox, "acquire", "--pr", "7", "--sha", "f" * 40)
    assert acq.returncode == 0

    proc = _pipe(sandbox, "42", SHA, "--observe-min", "0")
    assert proc.returncode == 10, proc.stderr + proc.stdout
    assert _state(sandbox)["lock"]["pr"] == 7, "别人的锁不得被动"
    assert _calls(sandbox) == []


def test_session_recorded_for_audit(sandbox):
    _pipe(sandbox, "42", SHA, "--session", "hermes-42", "--observe-min", "0")
    assert _state(sandbox)["deployed"]["pr"] == 42
    assert _state(sandbox)["lock"] is None


# ---------------------------------------------------------------- 心跳循环

def _acquire(sb: dict, pr: int = 42, sha: str = SHA) -> None:
    proc = _ds(sb, "acquire", "--pr", str(pr), "--sha", sha)
    assert proc.returncode == 0, proc.stderr


def test_heartbeat_loop_max_beats(sandbox):
    _acquire(sandbox)
    proc = _ds(sandbox, "heartbeat", "--loop", "--interval", "1", "--max-beats", "2")
    assert proc.returncode == 0, proc.stderr
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.startswith("{")]
    assert len(lines) == 2
    assert json.loads(lines[-1])["ok"] is True


def test_heartbeat_loop_keeps_lock_alive(sandbox):
    """TTL 设 3s、loop 间隔 1s：连续续约下锁必须始终可被本会话确认。"""
    _acquire(sandbox)
    popen = subprocess.Popen(
        [sys.executable, str(sandbox["dir"] / "deploy_state.py"),
         "heartbeat", "--loop", "--interval", "1"],
        env=_env(sandbox, DAILYCHECK_DEPLOY_HEARTBEAT_TTL=3),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(3.5)
        chk = subprocess.run(
            [sys.executable, str(sandbox["dir"] / "deploy_state.py"), "check", "--pr", "42"],
            capture_output=True, text=True, env=_env(sandbox, DAILYCHECK_DEPLOY_HEARTBEAT_TTL=3),
        )
        assert chk.returncode == 10, "心跳循环应让锁保持活跃（无循环时 3.5s > TTL 会过期）"
    finally:
        popen.terminate()
        popen.wait(timeout=10)


def test_heartbeat_loop_exits_when_lock_lost(sandbox):
    _acquire(sandbox)
    popen = subprocess.Popen(
        [sys.executable, str(sandbox["dir"] / "deploy_state.py"),
         "heartbeat", "--loop", "--interval", "1"],
        env=_env(sandbox), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        time.sleep(1.2)
        _ds(sandbox, "release", "--result", "failed", "--sha", SHA)
        rc = popen.wait(timeout=10)
    finally:
        if popen.poll() is None:
            popen.kill()
    assert rc == 11, "锁已释放后循环必须退出，不得空转"


def test_heartbeat_loop_sigterm_graceful(sandbox):
    _acquire(sandbox)
    popen = subprocess.Popen(
        [sys.executable, str(sandbox["dir"] / "deploy_state.py"),
         "heartbeat", "--loop", "--interval", "30"],
        env=_env(sandbox), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(0.8)
    popen.terminate()
    assert popen.wait(timeout=10) == 0, "SIGTERM 应优雅退出（rc 0），便于 cleanup 收割"


def test_heartbeat_loop_phase_not_clobbered(sandbox):
    """后台循环只在第一拍写 phase，主流程后续推进不被盖回去。"""
    _acquire(sandbox)
    popen = subprocess.Popen(
        [sys.executable, str(sandbox["dir"] / "deploy_state.py"),
         "heartbeat", "--loop", "--interval", "1", "--phase", "loop-phase"],
        env=_env(sandbox), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(1.4)
        _ds(sandbox, "heartbeat", "--phase", "verify-running")
        time.sleep(1.5)
        assert _state(sandbox)["lock"]["phase"] == "verify-running"
    finally:
        popen.terminate()
        popen.wait(timeout=10)

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
    sb = tmp_path / "scripts" / "blue_green"
    sb.mkdir(parents=True)
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
    env["PYTHONPATH"] = str(sb["tmp"])
    env["DAILYCHECK_DEPLOY_STATE_DIR"] = str(sb["state"])
    env["DAILYCHECK_PIPELINE_LOG"] = str(sb["tmp"] / "pipeline.log")
    env["DAILYCHECK_PIPELINE_TICK_SEC"] = "1"
    env["CALLS"] = str(sb["calls"])
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


# ---------------------------------------------------------------- 观察窗（PR #101 事故）
def _observe_tools(sb: dict, *, blocked_lines: int = 0, code: str = "302",
                   noise_lines: int = 0):
    """给观察窗装 fake curl / journalctl，并记录 journalctl 收到的参数。

    真 journalctl 只在 Linux 有，且沙盒里不该连真流量 —— 因此两个外部命令都
    走 env 钩子注入（见 deploy_pipeline.sh observe_window）。
    """
    jlog = sb["tmp"] / "jctl-argv.txt"
    jctl = sb["tmp"] / "fake-journalctl"
    body = "".join(f"[dry_run] BLOCKED write #{i} (log-only)\n" for i in range(blocked_lines))
    body += "".join(f"WARNING something BLOCKED unrelated #{i}\n" for i in range(noise_lines))
    jctl.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "{jlog}"\n'
        f'cat <<"JEOF"\n{body}JEOF\n'
        "exit 0\n",
        encoding="utf-8",
    )
    jctl.chmod(0o755)
    curl = sb["tmp"] / "fake-curl"
    curl.write_text(f"#!/usr/bin/env bash\nprintf '%s' '{code}'\n", encoding="utf-8")
    curl.chmod(0o755)
    return (
        {
            "DAILYCHECK_PIPELINE_JOURNALCTL": str(jctl),
            "DAILYCHECK_PIPELINE_CURL": str(curl),
        },
        jlog,
    )


def test_observe_window_quiet_green_passes(sandbox):
    """绿实例无 BLOCKED、探针 302 → 观察窗放行，不回滚。

    回归：BLOCKED 统计曾按「总观察时长」回溯（--since "30 min ago"），把
    promote 之前 DRY_RUN 期间 verify 探针产生的 [dry_run] BLOCKED 也捞进来，
    首次检查必然误判「写隔离没关」→ rc 24 回滚蓝（PR #101 实测）。
    """
    extra, jlog = _observe_tools(sandbox, blocked_lines=0)
    proc = _pipe(sandbox, "42", SHA, "--observe-min", "5", **extra)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    argv = jlog.read_text(encoding="utf-8")
    assert "--since" in argv
    assert "min ago" not in argv, f"BLOCKED 统计窗口仍在按总时长回溯：{argv}"
    assert "观察 +1min" in proc.stdout
    assert _calls(sandbox)[-1] == "switch.sh green 100"


def test_observe_window_rolls_back_on_blocked(sandbox):
    """观察窗内真的出现 BLOCKED（写隔离没关）→ rc 24 + 回滚蓝。"""
    extra, _ = _observe_tools(sandbox, blocked_lines=3)
    proc = _pipe(sandbox, "42", SHA, "--observe-min", "5", **extra)

    assert proc.returncode == 24, proc.stdout + proc.stderr
    calls = _calls(sandbox)
    assert calls[-1] == "switch.sh blue 100", calls
    assert "观察窗异常" in proc.stdout
    assert _state(sandbox).get("deployed") is None, "观察窗失败不得推进 deployed 锚点"


def test_observe_ignores_non_dry_run_blocked_noise(sandbox):
    """只有 `[dry_run] BLOCKED` 才算写隔离没关，别的 BLOCKED 字样不算。

    宽匹配（grep 'BLOCKED'）会把日志里任何含该词的告警都当成写隔离残留 ——
    与本次事故同一类误报，故匹配收窄到 dry-run 前缀。
    """
    extra, _ = _observe_tools(sandbox, blocked_lines=0, noise_lines=2)
    proc = _pipe(sandbox, "42", SHA, "--observe-min", "5", **extra)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _calls(sandbox)[-1] == "switch.sh green 100"


def test_observe_window_probe_failure_rolls_back(sandbox):
    """探针非 302/200（站点 5xx）→ 回滚。"""
    extra, _ = _observe_tools(sandbox, blocked_lines=0, code="502")
    proc = _pipe(sandbox, "42", SHA, "--observe-min", "5", **extra)

    assert proc.returncode == 24
    assert _calls(sandbox)[-1] == "switch.sh blue 100"


def test_observe_rollback_log_does_not_clobber_main_log(sandbox):
    """回滚输出必须落独立文件。

    回归：回滚曾用 `switch.sh ... >>"${LOG}"`，与调用方把 stdout 重定向到同一
    个 LOG 时形成双写，父进程缓冲按旧 offset 覆盖掉子进程刚写的行，把「观察
    +Nmin / 观察窗异常」两行诊断直接吃掉（生产事故时无从判断回滚缘由）。
    """
    extra, _ = _observe_tools(sandbox, blocked_lines=1)
    proc = _pipe(sandbox, "42", SHA, "--observe-min", "5", **extra)

    assert proc.returncode == 24
    main_log = (sandbox["tmp"] / "pipeline.log").read_text(encoding="utf-8")
    rb_log = sandbox["tmp"] / "pipeline.log.rollback"
    assert rb_log.exists(), "回滚日志文件未生成"
    assert "args=blue 100" in rb_log.read_text(encoding="utf-8")
    assert "回滚" not in main_log, "主日志仍被子进程回滚输出污染"


# ================================================================ F1 漂移守卫
# 2026-10-18 复盘 #110：verify 跑的 10 分钟里另一会话合了 PR 并 git pull，
# 工作树被换 → check1 pytest 读到混合态（进程 import 旧模块 + 模板读新文件）
# → 195 failed + BuildError，且**原因无从判断**。守卫把这种「神秘红」变成
# 「工作树在 verify 中被改动」这条明确归因，并阻止拿不可信结果继续上线。
#
# 普通 sandbox 的 REPO_ROOT 不是 git 仓库 → `rev-parse` 失败 → 两侧都 unknown
# → 守卫跳过（向后兼容，dev/离线沙盒不误伤）。要测守卫必须让 REPO_ROOT 是真仓库。


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True
    )
    assert proc.returncode == 0, f"git {args} 失败: {proc.stderr}"
    return proc.stdout.strip()


@pytest.fixture()
def repo_sandbox(tmp_path: Path):
    """REPO_ROOT 本身是真 git 仓库的沙盒（F1 漂移守卫 / F4 venv 选择需要）。

    ⚠️ 目录层级不能随手改：deploy_pipeline.sh 的 `REPO_ROOT="${SCRIPT_DIR}/../.."`，
    故脚本必须放在 `<repo>/scripts/blue_green/` 下，REPO_ROOT 才等于 `<repo>`。
    用普通 sandbox 时 REPO_ROOT 会算到 tmp_path 的**父目录**（pytest basetemp 根），
    既不是仓库、又被多个测试共享 —— 在那儿 init 会互相干扰。
    """
    repo = tmp_path / "repo"
    bg = repo / "scripts" / "blue_green"
    bg.mkdir(parents=True)
    for name in ("deploy_state.py", "deploy_pipeline.sh"):
        shutil.copy2(BG_DIR / name, bg / name)
        (bg / name).chmod(0o755)
    calls = tmp_path / "calls.txt"
    for name, rc in (("up.sh", 0), ("verify.sh", 0), ("promote.sh", 0), ("switch.sh", 0)):
        _write_fake(bg / name, name, calls, rc)
    state = tmp_path / "state"
    state.mkdir()

    _git(["init", "-q"], repo)
    _git(["config", "user.email", "deploy-test@example.com"], repo)
    _git(["config", "user.name", "deploy-test"], repo)
    (repo / "SEED").write_text("seed\n", encoding="utf-8")
    _git(["add", "SEED"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    return {
        "dir": bg, "calls": calls, "state": state, "tmp": tmp_path,
        "repo": repo, "git_head": _git(["rev-parse", "HEAD"], repo),
    }


def _write_drifting_verify(sb: dict) -> None:
    """假 verify.sh：执行时在 REPO_ROOT 提交一次 —— 模拟并发 git pull 换树。"""
    p = sb["dir"] / "verify.sh"
    p.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "verify.sh $*" >> "{sb["calls"]}"\n'
        f'git -C "{sb["repo"]}" commit -q --allow-empty -m "concurrent pull"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    p.chmod(0o755)


def _audit(sb: dict) -> str:
    p = sb["state"] / "audit.log"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def test_worktree_drift_during_verify_fails_fast_with_audit(repo_sandbox):
    """核心判据：verify 期间 HEAD 变了 → exit 21 + 明确归因 + audit 事件。"""
    sb = repo_sandbox
    _write_drifting_verify(sb)

    proc = _pipe(sb, "42", SHA, "--observe-min", "0")

    assert proc.returncode == 21, proc.stdout + proc.stderr
    assert "工作树在 verify 期间被改动" in proc.stdout, proc.stdout
    assert "kind=worktree-drift" in _audit(sb), _audit(sb)


def test_worktree_drift_blocks_promote_and_switch(repo_sandbox):
    """漂移后绝不许继续 promote/switch —— 拿不可信的验证结果上线就是事故。"""
    sb = repo_sandbox
    _write_drifting_verify(sb)

    _pipe(sb, "42", SHA, "--observe-min", "0")

    calls = _calls(sb)
    assert any(c.startswith("verify.sh") for c in calls), calls
    assert not any(c.startswith("promote.sh") for c in calls), f"漂移后仍 promote: {calls}"
    assert not any(c.startswith("switch.sh") for c in calls), f"漂移后仍 switch: {calls}"


def test_worktree_drift_still_releases_lock(repo_sandbox):
    """漂移退出也必须收锁 —— 否则锁悬空，下一轮部署被自己的死锁挡住。"""
    sb = repo_sandbox
    _write_drifting_verify(sb)

    _pipe(sb, "42", SHA, "--observe-min", "0")

    assert _state(sb)["lock"] is None, "漂移退出后锁未释放"


def test_no_drift_passes_when_head_stable(repo_sandbox):
    """反向：真仓库但 HEAD 不动 → 正常全链通过，守卫不误伤。"""
    proc = _pipe(repo_sandbox, "42", SHA, "--observe-min", "0")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "工作树在 verify 期间被改动" not in proc.stdout
    assert "worktree-drift" not in _audit(repo_sandbox)


def test_verify_failure_without_drift_not_misattributed(repo_sandbox):
    """反向：verify 真失败但 HEAD 没动 → 归因必须是「verify 失败」，不是漂移。

    防「什么都赖漂移」：错误归因和没有归因一样坏（会把人引向查 git 而不是查代码）。
    """
    sb = repo_sandbox
    _write_fake(sb["dir"] / "verify.sh", "verify.sh", sb["calls"], 1)

    proc = _pipe(sb, "42", SHA, "--observe-min", "0")

    assert proc.returncode == 21
    assert "verify 失败" in proc.stdout, proc.stdout
    assert "工作树在 verify 期间被改动" not in proc.stdout
    assert "worktree-drift" not in _audit(sb), _audit(sb)


def test_drift_guard_skipped_when_not_a_git_repo(sandbox):
    """向后兼容：REPO_ROOT 不是 git 仓库（dev 沙盒 / 离线）→ 守卫跳过不阻断。"""
    proc = _pipe(sandbox, "42", SHA, "--observe-min", "0")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "worktree-drift" not in _audit(sandbox)


# ================================================================ F5 观察窗留痕
# 机制要求观察窗 ≥10min；历史上 #111 压到 10min（当时要求 30min）却**无任何留痕**。压缩本身不该
# 阻断（人工应急确实需要），但必须从「没人知道」变成 Dashboard 可见。


def test_observe_window_shortened_records_audit(sandbox):
    """压缩观察窗必须留痕。

    ⚠️ 必须注入 fake curl/journalctl（_observe_tools）：--observe-min 5 会真进
    观察窗探测 127.0.0.1:8080，本机 dev 容器返回 302 能过、CI 上无监听得 000
    → 触发回滚 exit 24（2026-10-18 CI 实 hit：本地全绿、CI 判红）。测试要脱真流量。
    """
    extra, _ = _observe_tools(sandbox, blocked_lines=0, code="302")
    proc = _pipe(sandbox, "42", SHA, "--observe-min", "5", **extra)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    audit = _audit(sandbox)
    assert "kind=observe-window-shortened" in audit, audit
    assert "observe=5min" in audit, audit


def test_observe_window_skipped_entirely_records_audit(sandbox):
    """--observe-min 0（完全跳过）比压缩更值得留痕。"""
    proc = _pipe(sandbox, "42", SHA, "--observe-min", "0")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "kind=observe-window-shortened" in _audit(sandbox)


def test_observe_window_at_required_records_no_shortened_audit(sandbox):
    """反向：达到机制要求 10min → 不记压缩事件（否则告警天天响＝没告警）。

    沙盒里 TICK_SEC=1，10min 折算 2 轮 ×1s，可接受。
    """
    extra, _ = _observe_tools(sandbox, blocked_lines=0, code="302")
    proc = _pipe(sandbox, "42", SHA, "--observe-min", "10", **extra)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "observe-window-shortened" not in _audit(sandbox)


def test_skip_verify_records_audit(sandbox):
    """--skip-verify 是最危险的应急开关，必须留痕。"""
    proc = _pipe(sandbox, "42", SHA, "--skip-verify", "--observe-min", "0")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "kind=skip-verify" in _audit(sandbox)


# ================================================================ F4 venv + 钩子留痕
# 根因：VPS 系统 python3 的 dist-packages/scripts 是常规包，import 优先级高于
# 仓库 namespace 包 scripts/ → `-m scripts.dailycheck_dashboard` 必报 No module
# → 仪表盘钩子长期静默失败，只 WARN 到日志、靠 15min timer 兜底掩盖。


def test_py_prefers_repo_venv_python(repo_sandbox):
    """REPO_ROOT/.venv/bin/python 存在时必须用它，而非裸 python3。"""
    sb = repo_sandbox
    venv_bin = sb["repo"] / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    marker = sb["tmp"] / "venv-python-used.txt"
    fake = venv_bin / "python"
    # 记录调用后转发给真 python —— 既证明被选中，又让 deploy_state.py 真能跑
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{marker}"\n'
        f'exec "{sys.executable}" "$@"\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)

    proc = _pipe(sb, "42", SHA, "--observe-min", "0")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert marker.exists(), "venv python 未被使用（PY 仍解析成裸 python3）"
    assert "deploy_state.py" in marker.read_text(encoding="utf-8")


def test_python_env_override_wins_over_venv(repo_sandbox):
    """优先级 PYTHON > 仓库 venv > python3：显式覆盖必须生效。"""
    sb = repo_sandbox
    venv_bin = sb["repo"] / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    broken = venv_bin / "python"
    broken.write_text("#!/usr/bin/env bash\nexit 9\n", encoding="utf-8")
    broken.chmod(0o755)

    # venv 里是必坏的假 python；PYTHON 指向真 python → 应走 PYTHON 而非 venv
    proc = _pipe(sb, "42", SHA, "--observe-min", "0", PYTHON=sys.executable)

    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_dashboard_hook_failure_records_audit(sandbox):
    """钩子失败必须留痕 —— 此前只 WARN，被 timer 兜底掩盖成「看起来没事」。

    沙盒 REPO_ROOT 无 scripts/dailycheck_dashboard → render 必失败，正好验证。
    """
    proc = _pipe(sandbox, "42", SHA, "--observe-min", "0")

    assert proc.returncode == 0, "钩子失败不得影响部署结果"
    assert "kind=dashboard-hook-failed" in _audit(sandbox), _audit(sandbox)


def test_dashboard_sandbox_does_not_import_ambient_module(sandbox, tmp_path, monkeypatch):
    external = tmp_path / "external"
    module = external / "scripts/dailycheck_dashboard"
    module.mkdir(parents=True)
    (external / "scripts/__init__.py").write_text("")
    (module / "__init__.py").write_text("")
    marker = external / "imported"
    (module / "__main__.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('owned sentinel')\n")
    monkeypatch.setenv("PYTHONPATH", str(external))
    proc = _pipe(sandbox, "42", SHA, "--observe-min", "0")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not marker.exists(), "sandbox imported the owned ambient dashboard sentinel"
    assert "kind=dashboard-hook-failed" in _audit(sandbox), _audit(sandbox)
    assert _env(sandbox, PYTHONPATH=str(external))["PYTHONPATH"] == str(external)



# ---------------------------------------------------------------- 候选模式

@pytest.fixture()
def candidate_sandbox(sandbox):
    """在 sandbox 的 tmp 目录里建真 git 仓库 + bare origin + 假合并助手。"""
    repo = sandbox["tmp"]

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True, capture_output=True, text=True,
        )

    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "T")
    (repo / "app.py").write_text("v1\n", encoding="utf-8")
    git("add", "app.py")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD").stdout.strip()

    git("checkout", "-qb", "feat")
    (repo / "app.py").write_text("v2\n", encoding="utf-8")
    git("commit", "-qam", "candidate")
    sha = git("rev-parse", "HEAD").stdout.strip()

    origin = repo / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    git("remote", "add", "origin", str(origin))
    git("push", "-q", "origin", "main")
    git("push", "-q", "origin", "HEAD:refs/pull/42/head")
    git("checkout", "-q", "main")

    helper = repo / "fake_merge.py"
    helper.write_text(
        f"""#!{sys.executable}
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["CALLS"], "a", encoding="utf-8") as fh:
    fh.write("merge_helper " + " ".join(args) + "\\n")
if args and args[0] == "check" and os.environ.get("FAKE_CHECK_RC"):
    sys.exit(int(os.environ["FAKE_CHECK_RC"]))
if args and args[0] == "merge":
    if os.environ.get("FAKE_MERGE_RC"):
        sys.exit(int(os.environ["FAKE_MERGE_RC"]))
    sha = args[args.index("--sha") + 1]
    subprocess.run(
        ["git", "-C", os.environ["FAKE_ORIGIN"], "update-ref", "refs/heads/main", sha],
        check=True,
    )
    if "--base" in args:
        base = args[args.index("--base") + 1]
        print(json.dumps({{"merged": True, "sha": sha, "merged_sha": sha,
                          "main_sha": sha, "base": base}})
              if not os.environ.get("FAKE_UNCONFIRMED") else "{{}}")
sys.exit(0)
""",
        encoding="utf-8",
    )
    helper.chmod(0o755)

    up = sandbox["dir"] / "up.sh"
    up.write_text(
        "#!/usr/bin/env bash\n"
        f"echo \"up.sh $(git -C '{repo}' rev-parse --short=12 HEAD)\" >> \"{sandbox['calls']}\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    up.chmod(0o755)

    return {
        **sandbox, "repo": repo, "origin": origin,
        "sha": sha, "base": base, "helper": helper,
    }


def _cand_env(sb, **extra) -> dict:
    return {
        "DAILYCHECK_DEPLOY_MERGE_BIN": str(sb["helper"]),
        "FAKE_ORIGIN": str(sb["origin"]),
        **extra,
    }


def _head(sb) -> str:
    return subprocess.run(
        ["git", "-C", str(sb["repo"]), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _branch(sb) -> str:
    return subprocess.run(
        ["git", "-C", str(sb["repo"]), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_candidate_checks_out_pr_head_before_up(candidate_sandbox):
    sb = candidate_sandbox
    proc = _pipe(sb, "42", sb["sha"], "--candidate", "--observe-min", "0", **_cand_env(sb))

    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = _calls(sb)
    assert calls[0].startswith("merge_helper check")
    assert calls[1].startswith(f"up.sh {sb['sha'][:12]}")
    assert _head(sb) == sb["sha"], "候选模式成功后工作树应停在候选 sha"


def test_candidate_rejects_stale_branch_before_up(candidate_sandbox):
    sb = candidate_sandbox
    subprocess.run(["git", "-C", str(sb["repo"]), "checkout", "-q", "main"], check=True)
    (sb["repo"] / "other.py").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(sb["repo"]), "add", "other.py"], check=True)
    subprocess.run(["git", "-C", str(sb["repo"]), "commit", "-qm", "advance"], check=True)
    subprocess.run(["git", "-C", str(sb["repo"]), "push", "-q", "origin", "main"], check=True)
    sb["calls"].unlink(missing_ok=True)

    proc = _pipe(sb, "42", sb["sha"], "--candidate", "--observe-min", "0", **_cand_env(sb))

    assert proc.returncode == 25
    assert _calls(sb) == [], "前置失败不得调 up/verify"
    assert _branch(sb) == "main"


def test_candidate_rejects_sha_mismatch(candidate_sandbox):
    sb = candidate_sandbox
    proc = _pipe(
        sb, "42", "0" * 40, "--candidate", "--observe-min", "0", **_cand_env(sb)
    )

    assert proc.returncode == 25
    assert _calls(sb) == []


def test_candidate_rejects_gitea_precondition(candidate_sandbox):
    sb = candidate_sandbox
    proc = _pipe(
        sb, "42", sb["sha"], "--candidate", "--observe-min", "0",
        **_cand_env(sb, FAKE_CHECK_RC="31"),
    )

    assert proc.returncode == 25
    assert not any(c.startswith("up.sh") for c in _calls(sb))


def test_candidate_dry_run_does_not_checkout(candidate_sandbox):
    sb = candidate_sandbox
    proc = _pipe(sb, "42", sb["sha"], "--candidate", "--dry-run", **_cand_env(sb))

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _branch(sb) == "main"


def test_candidate_up_failure_restores_main(candidate_sandbox):
    sb = candidate_sandbox
    _write_fake(sb["dir"] / "up.sh", "up.sh", sb["calls"], 7)
    proc = _pipe(sb, "42", sb["sha"], "--candidate", "--observe-min", "0", **_cand_env(sb))

    assert proc.returncode == 20
    assert _branch(sb) == "main"
    assert _head(sb) == sb["base"]


def test_merge_runs_after_switch_and_before_release(candidate_sandbox):
    sb = candidate_sandbox
    proc = _pipe(
        sb, "42", sb["sha"], "--candidate", "--merge-pr", "--observe-min", "0",
        **_cand_env(sb),
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = _calls(sb)
    names = [c.split()[0] for c in calls]
    assert names == [
        "merge_helper", "up.sh", "verify.sh", "promote.sh", "switch.sh", "merge_helper"
    ], calls
    assert calls[0].startswith("merge_helper check")
    assert calls[-1].startswith("merge_helper merge")
    assert _state(sb)["deployed"]["sha"] == sb["sha"]
    assert _branch(sb) == "main"
    assert _head(sb) == sb["sha"]


def test_merge_failure_rolls_back_blue(candidate_sandbox):
    sb = candidate_sandbox
    proc = _pipe(
        sb, "42", sb["sha"], "--candidate", "--merge-pr", "--observe-min", "0",
        **_cand_env(sb, FAKE_MERGE_RC="30"),
    )

    assert proc.returncode == 26
    assert _calls(sb)[-1] == "switch.sh blue 100"
    assert _state(sb)["deployed"] is None
    assert _branch(sb) == "main"


def test_merge_pr_without_candidate_is_usage_error(sandbox):
    proc = _pipe(sandbox, "42", SHA, "--merge-pr")
    assert proc.returncode == 64


@pytest.fixture
def ordinary_candidate(candidate_sandbox):
    sb = candidate_sandbox
    tools = sb["tmp"] / "bin"
    tools.mkdir()
    real_git = shutil.which("git")
    git = tools / "git"
    git.write_text(f'''#!{sys.executable}
import os, sys
args = sys.argv[1:]
if args[-3:] == ["remote", "get-url", "origin"]:
    print("https://github.com/DUIDUI-Studio/DailyCheck.git")
else: os.execv({real_git!r}, [{real_git!r}, *args])
''')
    git.chmod(0o755)
    sleeper = tools / "sleep"
    sleeper.write_text("#!/bin/sh\nexit 0\n")
    sleeper.chmod(0o755)
    clock = sb["tmp"] / "monotonic"
    python = tools / "python"
    python.write_text(f'''#!{sys.executable}
import os, sys
from pathlib import Path
if sys.argv[1:] == ["-c", "import time; print(int(time.monotonic()))"]:
    p = Path({str(clock)!r})
    n = int(p.read_text()) if p.exists() else 0
    print(n)
    p.write_text(str(n + 300))
else: os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])
''')
    python.chmod(0o755)
    # Existing target resolver contract; report publication is tested separately.
    (sb["dir"] / "deploy_report.py").write_text(
        'import json\nprint(json.dumps({"provider":"github", "host":"github.com", '
        '"repository":"DUIDUI-Studio/DailyCheck"}))\n')
    for name in ("deploy_triage_pure.py", "publication_authority.py"):
        shutil.copy2(BG_DIR / name, sb["dir"] / name)
    shutil.copy2(BG_DIR.parent / "hosting.py", sb["tmp"] / "scripts/hosting.py")
    extra, _ = _observe_tools(sb)
    sb["ordinary_env"] = {**_cand_env(sb), **extra,
        "PATH": str(tools) + os.pathsep + os.environ["PATH"], "PYTHON": str(python),
        "PYTHONPATH": str(BG_DIR.parents[1]), "DAILYCHECK_HOSTING_PROVIDER": "github",
        "DAILYCHECK_GITHUB_LANE": "ordinary", "DAILYCHECK_DEPLOY_REPORT": "0"}
    return sb


@pytest.mark.parametrize("unconfirmed", [False, True])
def test_ordinary_candidate_frozen_base_and_confirmed_release(ordinary_candidate, unconfirmed):
    from tests.test_github_transport import proof

    sb = ordinary_candidate
    extra = dict(sb["ordinary_env"])
    if unconfirmed:
        extra["FAKE_UNCONFIRMED"] = "1"
        extra["PYTHONOPTIMIZE"] = "1"
    proc = _pipe(sb, "42", sb["sha"], "--candidate", "--merge-pr", **extra)
    assert proc.returncode == (26 if unconfirmed else 0), proc.stdout + proc.stderr
    calls = _calls(sb)
    assert f"--base {sb['base']}" in calls[0]
    assert f"--base {sb['base']}" in next(c for c in calls if c.startswith("merge_helper merge"))
    names = [line.split()[0] for line in calls]
    assert names[:6] == ["merge_helper", "up.sh", "verify.sh", "promote.sh", "switch.sh", "merge_helper"]
    assert "观察 +5min" in proc.stdout and "观察 +10min" in proc.stdout
    state = _state(sb)
    if unconfirmed:
        assert not state.get("deployed")
        assert state["last_attempt"]["result"] == "failed"
        assert any(c == "switch.sh blue 100" for c in calls)
        assert "release(success)" not in proc.stdout
    else:
        assert state["deployed"]["sha"] == sb["sha"]
        assert _head(sb) == sb["sha"] and "release(success)" in proc.stdout
    proof("pipeline-" + ("unconfirmed" if unconfirmed else "confirmed"), {
        "native_exit": proc.returncode, "stdout": proc.stdout, "calls": calls,
        "base": sb["base"], "head": sb["sha"], "deployed": state.get("deployed"),
        "clock": "owned simulated monotonic port; no elapsed Prod-time claim"})


@pytest.mark.parametrize("lane,args", [("shadow", []), ("ordinary", []),
    ("ordinary", ["--candidate", "--merge-pr", "--skip-verify"]),
    ("ordinary", ["--candidate", "--merge-pr", "--observe-min", "0"])])
def test_github_pipeline_rejects_disabled_or_incomplete_lane(sandbox, lane, args):
    proc = _pipe(sandbox, "42", SHA, *args, DAILYCHECK_HOSTING_PROVIDER="github",
                 DAILYCHECK_GITHUB_LANE=lane, PYTHONPATH=str(BG_DIR.parents[1]))
    assert proc.returncode in {25, 64}, proc.stdout + proc.stderr
    assert not _calls(sandbox) and not _state(sandbox)


@pytest.mark.parametrize("case", ["stable", "promote", "switch", "observe", "tree",
                                   "before_write", "after_write", "lost_response"])
def test_verified_candidate_continuity_with_actual_finalizer(ordinary_candidate, case):
    from tests import test_github_transport as t

    sb = ordinary_candidate
    for name in ("deploy_merge.py", "deploy_report.py"):
        shutil.copy2(BG_DIR / name, sb["dir"] / name)
    reader = sb["tmp"] / "scripts/prod_agent"
    reader.mkdir()
    shutil.copy2(BG_DIR.parent / "prod_agent/github_readiness.py", reader / "github_readiness.py")
    fake = sb["tmp"] / "fake-api"
    fake.mkdir()
    with pytest.MonkeyPatch.context() as m:
        table = t.ordinary(None, fake, m)
    table = json.loads(json.dumps(table).replace(t.SHA, sb["sha"]).replace(t.BASE, sb["base"])
                       .replace("/160", "/42").replace('"number": 160', '"number": 42'))
    key = t.ROOT + "/pulls/42"
    before = table[key]
    after = json.loads(json.dumps(before))
    after["data"].update(state="closed", merged=True, merge_commit_sha=sb["sha"])
    table[key] = [before] * 12 + [after]
    main = table[t.ROOT + "/git/ref/heads/main"]
    table[t.ROOT + "/git/ref/heads/main"] = [main] * 3 + [t.response({
        "ref": "refs/heads/main", "object": {"sha": sb["sha"]}})]
    if case == "lost_response":
        table["/graphql"]["exit"] = 28
    (fake / "table").write_text(json.dumps(table))
    effect = '''import subprocess
def drift():
    subprocess.run(['/usr/bin/git','-C',os.environ['CANDIDATE_REPO'],'checkout','-q',
                    '--detach',os.environ['CANDIDATE_BASE']],check=True)
if url.path == '/graphql':
    subprocess.run(['/usr/bin/git','-C',os.environ['CANDIDATE_ORIGIN'],'update-ref',
                    'refs/heads/main',os.environ['CANDIDATE_H']],check=True)
    if os.environ['CANDIDATE_CASE'] == 'after_write': drift()
if (url.path.endswith('/git/ref/heads/main') and state.get(key) == 3
        and os.environ['CANDIDATE_CASE'] == 'before_write'): drift()
'''
    curl = fake / "curl"
    curl.write_text(t.FAKE_CURL.replace("PYTHON", sys.executable).replace("log = {", effect + "log = {"))
    curl.chmod(0o700)
    (sb["state"] / "state.json").write_text(json.dumps({
        "deployed": {"sha": sb["base"], "pr": 41, "at": 1}}))
    drift = f"git -C '{sb['repo']}' checkout -q --detach '{sb['base']}'\n"
    if case == "promote":
        (sb["dir"] / "promote.sh").write_text(
            "#!/bin/sh\n" + drift + f"echo promote.sh >> '{sb['calls']}'\n")
    if case == "switch":
        (sb["dir"] / "switch.sh").write_text(
            f'#!/bin/sh\necho "switch.sh $*" >> \'{sb["calls"]}\'\n'
            + 'if [ "$1" = green ]; then\n' + drift + "fi\n")
    extra = {**sb["ordinary_env"], "DAILYCHECK_DEPLOY_MERGE_BIN": str(sb["dir"] / "deploy_merge.py"),
        "DAILYCHECK_CURL": str(curl), "DAILYCHECK_GITHUB_READ_TOKEN": "synthetic-read",
        "DAILYCHECK_GITHUB_FINALIZE_TOKEN_FILE": str(fake / "finalize-token"),
        "FAKE_TABLE": str(fake / "table"), "FAKE_STATE": str(fake / "api-state"),
        "FAKE_LOG": str(fake / "log"), "CANDIDATE_REPO": str(sb["repo"]),
        "CANDIDATE_ORIGIN": str(sb["origin"]), "CANDIDATE_H": sb["sha"],
        "CANDIDATE_BASE": sb["base"], "CANDIDATE_CASE": case, "DAILYCHECK_DASHBOARD_HOOK": "0"}
    if case in {"observe", "tree"}:
        journal = Path(extra["DAILYCHECK_PIPELINE_JOURNALCTL"])
        journal.write_text("#!/bin/sh\n" + (drift if case == "observe" else
            f"echo changed >> '{sb['repo'] / 'app.py'}'\n"))
    result = _pipe(sb, "42", sb["sha"], "--candidate", "--merge-pr", **extra)
    state = _state(sb)
    actual_main = _git(["rev-parse", "main"], sb["origin"])
    catchup = _ds(sb, "catchup", "--main-sha", actual_main)
    posts = [r for r in map(json.loads, (fake / "log").read_text().splitlines())
             if r["method"] == "POST"]
    t.proof("continuity-" + case, {"native_exit": result.returncode, "stdout": result.stdout,
        "base": sb["base"], "head": sb["sha"], "actual_main": actual_main,
        "deployed": state.get("deployed"), "last_attempt": state.get("last_attempt"),
        "outcomes": [{k: v for k, v in o.items() if k in {"sha", "result", "rc", "verification"}}
                     for o in state.get("outcomes", {}).values()], "post_count": len(posts),
        "catchup_exit": catchup.returncode, "catchup_stdout": catchup.stdout,
        "fences": [r["retained_fences"] for r in posts]})
    if case == "stable":
        assert result.returncode == 0 and state["deployed"]["sha"] == sb["sha"]
        assert len(posts) == 1 and actual_main == sb["sha"] and catchup.returncode == 0
        assert next(iter(state["outcomes"].values()))["verification"] == "normal"
    else:
        written = case in {"after_write", "lost_response"}
        assert result.returncode != 0 and state["deployed"]["sha"] == sb["base"]
        assert len(posts) == int(written)
        assert actual_main == (sb["sha"] if written else sb["base"])
        assert catchup.returncode == (2 if written else 0)
        assert state["last_attempt"]["result"] == "failed"
        assert not any(o["result"] == "success" for o in state["outcomes"].values())
        assert "switch.sh blue 100" in _calls(sb)

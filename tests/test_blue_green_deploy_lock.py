"""blue_green 部署单飞守卫测试（deploy-singleflight，2026-10-16）。

覆盖 up / verify / switch 的「会话级锁校验」拒绝与放行路径，以及 dry-run
的完全跳过；rollback / down 仅验 dry-run 不受影响（其 flock 段无状态校验
是设计语义，见各脚本注释）。

跨平台一致性 / 零副作用保证：
- 状态目录一律 tmp_path 覆盖（DAILYCHECK_DEPLOY_STATE_DIR），绝不碰 /var；
- live 用例最多走到「第一真动作」（up: APP_DIR 检查；switch: 布局守卫），
  不触碰 systemctl / nginx；
- flock 一律经 DAILYCHECK_BG_LOCK_HELD=1 跳过（进程锁非本文件测试对象；
  且避免 CI 上并发用例互抢 /var/lock——macOS dev 无 flock 命令本就会跳过，
  必须让两边行为一致）；
- 断言只认退出码与文件系统事实，不断言输出措辞（除审计行为）。

不测：flock 的进程互斥行为本身（跨平台不一致；由部署实测覆盖）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts" / "blue_green"
UP = SCRIPTS_DIR / "up.sh"
VERIFY = SCRIPTS_DIR / "verify.sh"
SWITCH = SCRIPTS_DIR / "switch.sh"
ROLLBACK = SCRIPTS_DIR / "rollback.sh"
DOWN = SCRIPTS_DIR / "down.sh"


@pytest.fixture(autouse=True)
def _skip_flock_env(monkeypatch):
    monkeypatch.setenv("DAILYCHECK_BG_LOCK_HELD", "1")


def _run(script: Path, *args: str, state_dir: Path, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["DAILYCHECK_DEPLOY_STATE_DIR"] = str(state_dir)
    env["DAILYCHECK_BG_LOCK_HELD"] = "1"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(script), *args],
        capture_output=True, text=True, env=env, timeout=60,
    )


def _acquire(state_dir: Path, pr: int, sha: str = "cafe000") -> None:
    env = os.environ.copy()
    env["DAILYCHECK_DEPLOY_STATE_DIR"] = str(state_dir)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "deploy_state.py"),
         "acquire", "--pr", str(pr), "--sha", sha],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr


@pytest.fixture()
def activated_dir(tmp_path: Path) -> Path:
    d = tmp_path / "deploy-state"
    d.mkdir()
    return d


UP_ENV = {"DAILYCHECK_APP_DIR": "/nonexistent-app-dir-for-test"}


# ----------------------------------------------------------------------
# up.sh
# ----------------------------------------------------------------------

def test_up_dry_run_skips_guard_even_activated_denied_case(activated_dir: Path):
    # dry-run 完全跳过守卫：激活 + 无锁，仍 exit 0（演练/CI 行为不变）
    proc = _run(UP, "1", "abc1234", "--dry-run", state_dir=activated_dir)
    assert proc.returncode == 0


def test_up_live_no_state_dir_passes(activated_dir: Path):
    # 机制未激活（目录不存在）-> 放行；到 APP_DIR 检查失败（rc 1，而非 10）
    missing = activated_dir.parent / "not-created"
    proc = _run(UP, "1", "abc1234", state_dir=missing, extra_env=UP_ENV)
    assert proc.returncode == 1
    assert not missing.exists()  # 不创建目录（只读探测）


def test_up_live_activated_no_lock_denied(activated_dir: Path):
    proc = _run(UP, "1", "abc1234", state_dir=activated_dir, extra_env=UP_ENV)
    assert proc.returncode == 10
    assert "acquire" in proc.stderr


def test_up_live_pr_mismatch_denied(activated_dir: Path):
    _acquire(activated_dir, pr=7)
    proc = _run(UP, "9", "abc1234", state_dir=activated_dir, extra_env=UP_ENV)
    assert proc.returncode == 10  # 锁属于 PR #7，跑 #9 被拒


def test_up_live_with_matching_lock_passes_and_audits(activated_dir: Path):
    _acquire(activated_dir, pr=7)
    proc = _run(UP, "7", "cafe000", state_dir=activated_dir, extra_env=UP_ENV)
    assert proc.returncode == 1  # 过了守卫，停在 APP_DIR 检查
    audit = (activated_dir / "audit.log").read_text(encoding="utf-8")
    assert "up.sh pr=7" in audit


def test_up_live_unlocked_escape(activated_dir: Path):
    env = dict(UP_ENV)
    env["DAILYCHECK_DEPLOY_UNLOCKED"] = "1"
    proc = _run(UP, "1", "abc1234", state_dir=activated_dir, extra_env=env)
    assert proc.returncode == 1  # 逃生舱放行
    assert "WARN" in proc.stderr


def test_up_live_empty_sha_hint(activated_dir: Path):
    # 无锁拒绝时提示命令包含 acquire 指令（引导正确姿势）
    proc = _run(UP, "5", "abc1234", state_dir=activated_dir, extra_env=UP_ENV)
    assert proc.returncode == 10
    assert "deploy_state.py acquire --pr 5" in proc.stderr


# ----------------------------------------------------------------------
# verify.sh
# ----------------------------------------------------------------------

def test_verify_live_no_lock_denied_without_report(activated_dir: Path):
    reports = activated_dir.parent / "reports"
    proc = _run(
        VERIFY, "1", state_dir=activated_dir,
        extra_env={"REPORT_DIR": str(reports)},
    )
    assert proc.returncode == 10
    # 守卫在 trap _write_report 注册之前 —— 被拒时不产出验证报告
    assert list(reports.glob("*pr-1*")) == []


def test_verify_dry_run_skips_guard(activated_dir: Path):
    proc = _run(
        VERIFY, "1", "--dry-run", state_dir=activated_dir,
        extra_env={"REPORT_DIR": str(activated_dir.parent / "reports")},
    )
    assert proc.returncode == 0


# ----------------------------------------------------------------------
# switch.sh
# ----------------------------------------------------------------------

def _old_layout_conf(tmp_path: Path) -> str:
    """生成一个真实存在的「旧布局」conf（location 直指 dailycheck-green）。

    switch.sh 布局守卫只在【文件存在且是旧布局】时确定性 exit 2 ——
    这是守卫之后的第一个确定性停点，天然跨平台且不触 cp/nginx/systemctl。
    （注意：conf 不存在时布局守卫是「跳过」而非拒绝，不能用作停点。）
    """
    conf = tmp_path / "dailycheck.conf"
    conf.write_text(
        "server {\n"
        "    listen 443 ssl;\n"
        "    location / {\n"
        "        proxy_pass http://dailycheck-green;\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    return str(conf)


def test_switch_green_live_no_lock_denied(activated_dir: Path, tmp_path: Path):
    proc = _run(
        SWITCH, "green", "100", state_dir=activated_dir,
        extra_env={
            "NGINX_DAILYCHECK_CONF": str(tmp_path / "no-such-conf"),
            "BACKUP_DIR": str(tmp_path / "backups"),
        },
    )
    assert proc.returncode == 10


def test_switch_green_live_with_lock_passes_guard(activated_dir: Path, tmp_path: Path):
    _acquire(activated_dir, pr=7)
    proc = _run(
        SWITCH, "green", "100", state_dir=activated_dir,
        extra_env={
            "NGINX_DAILYCHECK_CONF": _old_layout_conf(tmp_path),
            "BACKUP_DIR": str(tmp_path / "backups"),
        },
    )
    # 过部署锁守卫后停在布局守卫（旧布局 -> rc 2），而不是 rc 10
    assert proc.returncode == 2


def test_switch_blue_live_skips_state_guard(activated_dir: Path, tmp_path: Path):
    # blue 是安全方向：无锁也放行（rollback / 演练依赖此豁免）
    proc = _run(
        SWITCH, "blue", "100", state_dir=activated_dir,
        extra_env={
            "NGINX_DAILYCHECK_CONF": _old_layout_conf(tmp_path),
            "BACKUP_DIR": str(tmp_path / "backups"),
        },
    )
    assert proc.returncode == 2  # 过守卫，停在布局守卫


def test_switch_dry_run_skips_guard(activated_dir: Path):
    proc = _run(SWITCH, "green", "5", "--dry-run", state_dir=activated_dir)
    assert proc.returncode == 0


# ----------------------------------------------------------------------
# rollback / down：dry-run 行为不变（锁段跳过）
# ----------------------------------------------------------------------

def test_rollback_dry_run_unaffected(activated_dir: Path):
    proc = _run(ROLLBACK, "--dry-run", state_dir=activated_dir)
    assert proc.returncode == 0


def test_down_dry_run_unaffected(activated_dir: Path):
    proc = _run(DOWN, "--dry-run", state_dir=activated_dir)
    assert proc.returncode == 0


# ----------------------------------------------------------------------
# 状态一致性：守卫不应因 check 产生写副作用
# ----------------------------------------------------------------------

def test_guard_check_is_readonly(activated_dir: Path):
    _acquire(activated_dir, pr=3)
    before = (activated_dir / "state.json").read_text(encoding="utf-8")
    _run(UP, "3", "cafe000", state_dir=activated_dir, extra_env=UP_ENV)
    after = (activated_dir / "state.json").read_text(encoding="utf-8")
    assert before == after
    # audit.log 允许追加，但锁本身与 heartbeat 不应被触碰
    lock_before = json.loads(before)["lock"]
    lock_after = json.loads(after)["lock"]
    assert lock_before == lock_after

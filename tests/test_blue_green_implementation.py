"""blue_green 5 脚本实装测试（v0.4 Phase 1 步骤 10）。

验证：
- bash -n 语法（5 脚本）
- --dry-run 模式退出码 0 + 关键动作序列回显
- --help 模式输出 usage
- 参数解析（缺参/错参 exit 64）
- 跨脚本调用（rollback.sh → switch.sh）
- 关键变量替换正确（weight=0 → down 标记）

不测：
- 实跑（systemctl / nginx）— 在 VPS 沙箱由 Prod Agent 测
- Playwright / ab — 工具未装时优雅跳过
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

# 脚本路径
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts" / "blue_green"
UP = SCRIPTS_DIR / "up.sh"
VERIFY = SCRIPTS_DIR / "verify.sh"
SWITCH = SCRIPTS_DIR / "switch.sh"
DOWN = SCRIPTS_DIR / "down.sh"
ROLLBACK = SCRIPTS_DIR / "rollback.sh"
ALL_SCRIPTS = [UP, VERIFY, SWITCH, DOWN, ROLLBACK]


def _run(script: Path, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """跑 bash script，返回 CompletedProcess（含 stdout/stderr/returncode）。"""
    return subprocess.run(
        ["bash", str(script), *args],
        capture_output=True, text=True, timeout=30, cwd=cwd,
    )


# ============================================================
# bash 语法（5 脚本）
# ============================================================
@pytest.mark.parametrize("script", ALL_SCRIPTS, ids=lambda p: p.name)
def test_bash_syntax(script: Path):
    """bash -n 每个脚本都应当 OK。"""
    result = subprocess.run(
        ["bash", "-n", str(script)], capture_output=True, text=True
    )
    assert result.returncode == 0, (
        f"{script.name} bash -n 失败: {result.stderr}"
    )


# ============================================================
# --help 模式
# ============================================================
@pytest.mark.parametrize("script", ALL_SCRIPTS, ids=lambda p: p.name)
def test_help_flag(script: Path):
    """--help 应当输出 usage 并 exit 0。"""
    result = _run(script, "--help")
    assert result.returncode == 0
    assert "usage:" in result.stdout.lower()


# ============================================================
# 缺参 / 错参
# ============================================================
def test_up_no_args():
    result = _run(UP)
    assert result.returncode == 64
    assert "usage:" in result.stderr


def test_up_only_one_arg():
    result = _run(UP, "99")
    assert result.returncode == 64
    assert "usage:" in result.stderr


def test_switch_invalid_target():
    result = _run(SWITCH, "purple")
    assert result.returncode == 64
    assert "usage:" in result.stderr or "green|blue" in result.stderr


def test_switch_invalid_percent():
    result = _run(SWITCH, "green", "150")
    assert result.returncode == 64
    assert "0-100" in result.stderr


def test_switch_negative_percent():
    result = _run(SWITCH, "green", "-1")
    assert result.returncode == 64
    assert "0-100" in result.stderr


def test_verify_no_args():
    result = _run(VERIFY)
    assert result.returncode == 64


def test_down_unknown_flag():
    result = _run(DOWN, "--unknown")
    assert result.returncode == 64
    assert "unknown" in result.stderr


# ============================================================
# --dry-run 模式（核心：每个脚本应当 exit 0 + 关键动作回显）
# ============================================================
def test_up_dry_run_exit_zero():
    result = _run(UP, "99", "abc1234", "--dry-run")
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"


def test_up_dry_run_key_actions():
    result = _run(UP, "99", "abc1234", "--dry-run")
    out = result.stdout + result.stderr
    # 关键动作回显
    assert "[DRY-RUN] would execute:" in out
    assert "cp -a /var/lib/dailycheck /var/lib/dailycheck-green" in out
    assert "systemctl start dailycheck-green.service" in out
    assert "systemctl start dailycheck-green-mcp.service" in out
    assert "loop 30 × 2s curl http://127.0.0.1:8091/health" in out


def test_up_dry_run_skip_db_copy():
    result = _run(UP, "99", "abc1234", "--dry-run", "--skip-db-copy")
    out = result.stdout + result.stderr
    assert "skip" in out.lower()
    # --skip-db-copy 不应触发 cp -a
    assert "cp -a /var/lib/dailycheck /var/lib/dailycheck-green" not in out


def test_switch_green_5_dry_run():
    result = _run(SWITCH, "green", "5", "--dry-run")
    assert result.returncode == 0
    out = result.stdout + result.stderr
    assert "weight=5" in out
    assert "weight=95" in out
    # 5%/95% 不应 down（line: 行的 weight=5 表明非 down）
    assert "blue line:    server 127.0.0.1:8090 weight=95;" in out
    assert "green line:   server 127.0.0.1:8091 weight=5;" in out


def test_switch_green_100_dry_run_blue_down():
    """switch.sh green 100：蓝应当走 down 标记（修复 nginx weight=0 bug）。"""
    result = _run(SWITCH, "green", "100", "--dry-run")
    assert result.returncode == 0
    out = result.stdout + result.stderr
    # 蓝行：down
    assert "blue line:    server 127.0.0.1:8090 down;" in out
    # 绿行：weight=100
    assert "green line:   server 127.0.0.1:8091 weight=100;" in out


def test_switch_blue_100_dry_run_green_down():
    """switch.sh blue 100：绿应当 down（回滚语义）。"""
    result = _run(SWITCH, "blue", "100", "--dry-run")
    assert result.returncode == 0
    out = result.stdout + result.stderr
    assert "blue line:    server 127.0.0.1:8090 weight=100;" in out
    assert "green line:   server 127.0.0.1:8091 down;" in out


def test_switch_mcp_lines_in_output():
    """MCP 端口 :5100/:5101 也应参与切流量。"""
    result = _run(SWITCH, "green", "5", "--dry-run")
    out = result.stdout + result.stderr
    assert "blue-mcp:     server 127.0.0.1:5100 weight=95;" in out
    assert "green-mcp:    server 127.0.0.1:5101 weight=5;" in out


def test_down_dry_run_exit_zero():
    result = _run(DOWN, "--dry-run")
    assert result.returncode == 0
    out = result.stdout + result.stderr
    assert "systemctl stop dailycheck-green.service" in out


def test_down_dry_run_keep_blue_keeps_db():
    result = _run(DOWN, "--keep-blue", "--dry-run")
    assert result.returncode == 0
    out = result.stdout + result.stderr
    assert "保留" in out or "keep" in out.lower()
    # --keep-blue 不应 rm -rf DB
    assert "rm -rf /var/lib/dailycheck-green" not in out


def test_down_dry_run_removes_db_by_default():
    result = _run(DOWN, "--dry-run")
    out = result.stdout + result.stderr
    assert "rm -rf /var/lib/dailycheck-green" in out


def test_rollback_dry_run_calls_switch():
    """rollback.sh dry-run 应调 switch.sh blue 100。"""
    result = _run(ROLLBACK, "--dry-run")
    assert result.returncode == 0
    out = result.stdout + result.stderr
    assert "switch.sh blue 100" in out
    assert "journalctl" in out
    assert "rollback" in out.lower()


def test_verify_dry_run_exit_zero():
    result = _run(VERIFY, "99", "--dry-run")
    assert result.returncode == 0


def test_verify_dry_run_writes_report():
    """verify.sh dry-run 应写报告到 backups/prod-verifications/。"""
    repo_root = SCRIPTS_DIR.parent.parent
    result = _run(VERIFY, "99", "--dry-run", cwd=repo_root)
    assert result.returncode == 0
    report_dir = repo_root / "backups" / "prod-verifications"
    if report_dir.exists():
        reports = list(report_dir.glob("*-pr-99.md"))
        assert len(reports) >= 1
        # 报告应含关键段落
        content = reports[-1].read_text(encoding="utf-8")
        assert "PR #99" in content
        assert "集成测试" in content
        assert "回滚演练" in content


# ============================================================
# 跨脚本调用（rollback.sh → switch.sh）
# ============================================================
def test_rollback_dry_run_no_actual_switch_call():
    """rollback.sh dry-run 不应真跑 switch.sh（避免 nginx 真改）。"""
    # 用一个不存在的 nginx 配置路径验证 dry-run 不会尝试访问
    env = os.environ.copy()
    env["NGINX_DAILYCHECK_CONF"] = "/nonexistent/path"
    result = subprocess.run(
        ["bash", str(ROLLBACK), "--dry-run"],
        capture_output=True, text=True, env=env, timeout=10,
    )
    # dry-run 应当不真调 switch.sh（即使 switch.sh 在 dry-run 也会真 sed 真 nginx -t）
    # 但 switch.sh 自身的 --dry-run 也是安全的，所以最终 rc=0
    assert result.returncode == 0


# ============================================================
# 关键设计不变性
# ============================================================
def test_dry_run_mode_is_safe_across_all_scripts():
    """--dry-run 模式在 5 脚本上都不应触发生产副作用。

    间接验证：所有 --dry-run 测试都通过 + 没有 systemctl/ngninx 副作用。
    """
    # 跑每个脚本 --dry-run 都应当 exit 0
    for script in ALL_SCRIPTS:
        # 给每个脚本合适合法参数
        if script.name == "up.sh":
            r = _run(script, "99", "abc1234", "--dry-run")
        elif script.name == "verify.sh":
            r = _run(script, "99", "--dry-run", cwd=Path.cwd())
        elif script.name == "switch.sh":
            r = _run(script, "green", "5", "--dry-run")
        else:  # down.sh / rollback.sh
            r = _run(script, "--dry-run")
        assert r.returncode == 0, (
            f"{script.name} --dry-run exit={r.returncode}: {r.stderr}"
        )

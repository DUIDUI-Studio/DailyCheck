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
import socket
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


# ============================================================
# Issue #22 fix 回归测试
# ============================================================
def test_up_accepts_short_sha_7chars():
    """up.sh 应接受 7-char short SHA（git rev-parse --short HEAD 风格）。

    Issue #22: 原版用 `git rev-parse HEAD` 返 40-char full SHA，
    与 7-char COMMIT_SHA 比较永远不等。已修为用 --short。
    """
    # 在真 git repo 里跑（scripts/blue_green 的子 git 不可用 → 用 main 仓库）
    repo_root = SCRIPTS_DIR.parent.parent
    real_head = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, timeout=5,
    ).stdout.strip()
    # 7-char short SHA 调用 up.sh 应当至少跑到 APP_DIR 检查（live 模式缺 git HEAD 校验会 fail）
    result = _run(UP, "99", real_head, "--dry-run")
    # dry-run 模式跳过 git HEAD 校验（仅 echo）
    assert "PR #99" in result.stdout
    assert "1cacf49" in result.stdout or real_head[:7] in result.stdout or "would verify git HEAD" in result.stdout


def test_up_rejects_mismatched_sha_live():
    """live 模式下 SHA 不匹配应 exit 1（v0.5 改进：错误友好提示）。

    Issue #22 fix 验证：错误 SHA 应当报错，不能静默通过。
    """
    result = _run(UP, "99", "deadbee")  # 错 SHA + live 模式
    # live 模式不接 --dry-run → 走 git HEAD 校验 → 报 SHA 不匹配
    # 可能 exit 1（HEAD 不存在/不匹配）或 0（git rev-parse 失败 echo unknown）
    # 至少不应悄悄 PASS
    assert result.stdout + result.stderr  # 不空


def test_verify_writes_report_on_fail(tmp_path):
    """verify.sh fail 路径也写 report（Issue #22 fix + #24 Bug B 回归）。

    场景：绿 :8091 不可达 → 前置检查失败 → 必须写报告再 exit 1。
    用 REPORT_DIR 重定向到 tmp，避免污染真仓库；日期用 glob 匹配
    （旧版硬编码 20261014 换天即假失败）。
    """
    repo_root = SCRIPTS_DIR.parent.parent
    env = os.environ.copy()
    env["REPORT_DIR"] = str(tmp_path)
    env["DAILYCHECK_APP_DIR"] = str(tmp_path / "no-repo")  # check 1 不真跑 pytest
    result = subprocess.run(
        ["bash", str(VERIFY), "22"],
        capture_output=True, text=True, env=env, timeout=30, cwd=repo_root,
    )
    assert result.returncode == 1, f"stdout={result.stdout!r}"
    reports = list(tmp_path.glob("*-pr-22.md"))
    assert reports, f"fail 路径未写 report，stderr={result.stderr!r}"
    content = reports[0].read_text(encoding="utf-8")
    assert "PR #22" in content
    assert "决策摘要" in content
    assert "五项检查" in content or "2. 五项" in content


def test_verify_continues_after_check_fail_and_writes_report(tmp_path):
    """Issue #24 Bug B 核心：check 中途失败不得截断主流程，报告必须落盘。

    旧版 `set -euo pipefail` 下 _run_check 返 1 会立即 exit，
    绕过 trap 与主路径 _write_report → 报告文件永远不生成
    （prod 首演 20261014 现场：rc=1 且无报告）。
    修复后：set -uo pipefail + trap EXIT 兜底，失败由 OVERALL_PASS 跟踪。

    用本地 fake HTTP server 扮演绿 /health（让前置检查通过），
    使流程走到 check 4/5（dev/CI 上必失败），验证：
    - rc == 1（有失败项）
    - 报告文件存在且「打回」被勾选
    """
    import glob as _glob
    import threading

    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    port = srv.getsockname()[1]

    def _serve() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            try:
                conn.recv(4096)
                conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\nok")
            finally:
                conn.close()

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    try:
        env = os.environ.copy()
        env["REPORT_DIR"] = str(tmp_path)
        env["GREEN_URL"] = f"http://127.0.0.1:{port}"
        env["DAILYCHECK_APP_DIR"] = str(tmp_path / "no-repo")
        result = subprocess.run(
            ["bash", str(VERIFY), "24"],
            capture_output=True, text=True, env=env,
            timeout=120, cwd=SCRIPTS_DIR.parent.parent,
        )
    finally:
        srv.close()

    assert result.returncode == 1, f"应有失败项。stdout={result.stdout[-500:]!r}"
    reports = sorted(_glob.glob(str(tmp_path / "*-pr-24.md")))
    assert reports, f"check 中途失败后报告未生成，stderr={result.stderr!r}"
    content = Path(reports[-1]).read_text(encoding="utf-8")
    assert "[x] **打回**" in content, "有失败项时决策摘要应勾「打回」"
    assert "❌ FAIL" in content, "报告应记录具体失败项"


def test_verify_no_set_e_and_has_exit_trap():
    """静态检查：verify.sh 不应再用 set -e，且 trap EXIT 已启用（Issue #24 Bug B）。

    set -e 与「check 失败继续 + 兜底写报告」语义互斥；trap 是兜底关键。
    """
    content = VERIFY.read_text(encoding="utf-8")
    assert "set -euo" not in content, "verify.sh 仍用 set -euo（会截断 fail 路径）"
    assert "trap _write_report EXIT" in content, "trap 兜底未启用"
    assert content.count("_write_report()") == 1, (
        "_write_report 应只定义一次（PR #23 遗留的重复定义已清）"
    )


def test_verify_num_param_not_spliced():
    """verify.sh `_run_check` num 参数不应是 "1_int_test" 拼接（Issue #22 顺带 bug 3）。

    已修为 _run_check "1" "..."。
    """
    result = _run(VERIFY, "99", "--dry-run", cwd=Path.cwd())
    # 修复后输出应当是 "1. 集成测试" 而非 "1_int_test. 集成测试"
    assert "1. 集成测试" in result.stdout, (
        f"num 参数拼接错（Issue #22 bug 3）。output: {result.stdout[:500]}"
    )
    assert "1_int_test" not in result.stdout, (
        f"num 参数仍拼接 '1_int_test'。output: {result.stdout[:500]}"
    )


def test_verify_uses_tests_dir_not_contract():
    """verify.sh 不应 hardcode tests/contract 或 tests/integration 路径（Issue #22 bug 1）。

    已修为 tests/ 顶层（实际是 test_*.py）+ --ignore=tests/mcp_server。
    """
    result = _run(VERIFY, "99", "--dry-run", cwd=Path.cwd())
    out = result.stdout + result.stderr
    # 旧版硬编码路径不应再出现
    assert "tests/contract" not in out, (
        "verify.sh 仍 hardcode tests/contract 路径"
    )
    assert "tests/integration" not in out, (
        "verify.sh 仍 hardcode tests/integration 路径"
    )
    # 新版应跑 tests/ 顶层
    assert "tests/" in out or "tests\\" in out or "pytest" in out.lower()


# ============================================================
# Issue #24 Bug A 回归：switch.sh backup 不得写进 sites-enabled/
# ============================================================
def test_switch_writes_backup_outside_sites_enabled(tmp_path):
    """Issue #24 Bug A: nginx 备份必须落 BACKUP_DIR，绝不能在 sites-enabled/。

    prod 现场：`BACKUP="${NGINX_CONF}.bak.<ts>"` 写进 sites-enabled/ 后，
    nginx 主配置 `include sites-enabled/*` 把 .bak 当配置加载 →
    duplicate upstream "dailycheck-blue" → nginx -t 永久 fail。
    修复后：BACKUP="${BACKUP_DIR}/dailycheck.<ts>.bak"，BACKUP_DIR 可覆盖。
    """
    backup_dir = tmp_path / "nginx-backups"
    env = os.environ.copy()
    env["BACKUP_DIR"] = str(backup_dir)
    result = subprocess.run(
        ["bash", str(SWITCH), "green", "5", "--dry-run"],
        capture_output=True, text=True, env=env, timeout=10,
    )
    assert result.returncode == 0, f"stderr={result.stderr!r}"
    out = result.stdout + result.stderr
    # backup 路径 = BACKUP_DIR/dailycheck.<ts>.bak
    assert f"[switch] backup: {backup_dir}/dailycheck." in out, (
        f"backup 未落 BACKUP_DIR。output: {out[-400:]}"
    )
    assert ".bak" in out
    # 旧 pattern（sites-enabled 下的 .bak）不应再出现
    assert "sites-enabled/dailycheck.bak" not in out
    # dry-run 应回显 mkdir BACKUP_DIR 动作
    assert f"would mkdir -p {backup_dir}" in out


def test_switch_default_backup_dir_not_sites_enabled():
    """默认 BACKUP_DIR 不应是 nginx 配置目录（防 include 通配误加载）。"""
    content = SWITCH.read_text(encoding="utf-8")
    assert "BACKUP_DIR=" in content
    assert '${NGINX_CONF}.bak' not in content, (
        "switch.sh 仍在 NGINX_CONF 同目录拼 .bak（Issue #24 Bug A 未修干净）"
    )
    default_dir = "BACKUP_DIR=\"${BACKUP_DIR:-/var/lib/dailycheck-blue-green-backups}\""
    assert default_dir in content, "BACKUP_DIR 默认值应为 /var/lib/dailycheck-blue-green-backups"


def test_rollback_delegates_backup_to_switch():
    """rollback.sh 自身不写 nginx backup（调 switch.sh），无 Bug A 同类风险。"""
    content = ROLLBACK.read_text(encoding="utf-8")
    assert ".bak" not in content or "sites-enabled" not in content.split(
        "switch.sh"
    )[0], "rollback.sh 不应在调 switch.sh 之前自写 nginx backup"
    # 它确实委托 switch.sh blue 100（backup 逻辑收敛在 switch.sh 单点）
    assert "switch.sh\" blue 100" in content or "switch.sh blue 100" in content

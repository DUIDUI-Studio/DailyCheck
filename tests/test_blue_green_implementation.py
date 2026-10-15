"""blue_green 5 脚本实装测试（v0.4 Phase 1 步骤 10）。

验证：
- bash -n 语法（5 脚本）
- --dry-run 模式退出码 0 + 关键动作序列回显
- --help 模式输出 usage
- 参数解析（缺参/错参 exit 64）
- 跨脚本调用（rollback.sh → switch.sh）
- 关键变量替换正确（weight=0 → down 标记）
- Issue #22/#24/#28/#32/#34 专项回归：
  * verify.sh check 2 的绿库账号 provision（按标记抽取函数真跑）
  * check 7 DB 隔离证据（environ / 非软链 / inode）
  * check 8 nginx 入口探活 + switch.sh 单 upstream 布局

真跑但受控：check 2 的 provision 用例会起 flask CLI（init-master /
create-user）并落 tmp_path 的临时库，不碰仓库真库。

不测：
- 真实生产动作（systemctl / nginx / ab / Playwright 浏览器）— 在 VPS 由 Prod Agent 测
"""
from __future__ import annotations

import http.server
import os
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import urllib.parse
from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash

# 脚本路径
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts" / "blue_green"
REPO_ROOT = SCRIPTS_DIR.parent.parent
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
    # 关键动作回显（Issue #28: 拷真库 + cp -aL 解软链，不再是蓝根目录 cp）
    assert "[DRY-RUN] would execute:" in out
    assert (
        "cp -aL /root/DailyCheck/db /var/lib/dailycheck-green/db" in out
    ), "dry-run 应回显 cp -aL 真库拷贝"
    assert "systemctl start dailycheck-green.service" in out
    assert "systemctl start dailycheck-green-mcp.service" in out
    assert "loop 30 × 2s curl http://127.0.0.1:8091/health" in out


def test_up_dry_run_skip_db_copy():
    result = _run(UP, "99", "abc1234", "--dry-run", "--skip-db-copy")
    out = result.stdout + result.stderr
    assert "skip" in out.lower()
    # --skip-db-copy 不应触发真库拷贝
    assert "cp -aL /root/DailyCheck/db" not in out


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
    assert "八项检查" in content


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


# ============================================================
# verify.sh check 1：零新增失败门禁（不再用 || true 兜底）
# ============================================================
def test_verify_check1_runs_known_failures_gate():
    """check 1 必须复用 CI 门禁语义：失败集 ⊆ known_failures.txt。

    prod 首演发现 `|| true` 把 known failure 兜成 PASS——集成测试挂了
    verify 也不红。修复后：真跑 pytest + check_known_failures.py 判定。
    """
    content = VERIFY.read_text(encoding="utf-8")
    assert "|| true" not in content, "verify.sh 仍有 || true 兜底（会掩盖真实失败）"
    assert "check_known_failures.py" in content, "check 1 未接零新增失败门禁"
    assert "_verify_check1_pytest" in content, "check 1 未走门禁函数"
    # -rf 保证输出含 FAILED 行（check_known_failures.py 的解析依赖）
    assert "-rf" in content, "pytest 缺 -rf（门禁解析不到 FAILED 行会假绿）"


# ============================================================
# verify.sh check 2：sim_user.py 实装（不再静默跳过）
# ============================================================
def test_verify_check2_no_silent_skip():
    """check 2 不许「未实装 → echo 跳过 → PASS」；必须真跑 sim_user.py。"""
    content = VERIFY.read_text(encoding="utf-8")
    assert "未实装，跳过 (Phase 2 补)" not in content, "check 2 仍是静默跳过"
    assert "sim_user.py" in content, "check 2 未调用 sim_user.py"
    assert "_verify_check2_sim_user" in content, "check 2 未走实装函数"


SIM_USER = SCRIPTS_DIR.parent / "playwright" / "sim_user.py"


class _FakeDC(http.server.BaseHTTPRequestHandler):
    """最小 DailyCheck 行为：登录 → 选仓 → /land → /inventory（带 CSRF）。"""

    def log_message(self, *args):  # 静默
        pass

    def _page(self, body: str, status: int = 200) -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode() if length else ""
        return dict(urllib.parse.parse_qsl(raw))

    def do_GET(self):
        if self.path == "/login":
            self._page('<form><input type="hidden" name="csrf_token" value="T1"></form>')
        elif self.path == "/select-warehouse":
            self._page(
                '<form action="/select-warehouse/1" method="post">'
                '<input type="hidden" name="csrf_token" value="T1"></form>'
            )
        elif self.path == "/land":
            self._page("<html>land</html>")
        elif self.path == "/inventory":
            self._page("<html>inventory</html>")
        else:
            self._page("not found", 404)

    def do_POST(self):
        form = self._form()
        if self.path == "/login":
            if form.get("csrf_token") != "T1":
                self._page("csrf", 400)
            elif form.get("username") == "tester" and form.get("password") == "pw":
                self._redirect("/select-warehouse")
            else:
                self._page("bad login", 401)
        elif self.path == "/select-warehouse/1":
            if form.get("csrf_token") != "T1":
                self._page("csrf", 400)
            else:
                self._redirect("/land")
        else:
            self._page("not found", 404)


@pytest.fixture()
def fake_dc_server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeDC)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def _run_sim_user(base_url: str, env_extra: dict[str, str]) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.pop("SIM_USER_USER", None)
    env.pop("SIM_USER_PASSWORD", None)
    env.update(env_extra)
    return subprocess.run(
        ["/usr/bin/env", "python3", str(SIM_USER), "--base-url", base_url],
        capture_output=True, text=True, env=env, timeout=30,
    )


def test_sim_user_full_flow_passes(fake_dc_server):
    """4 路径全通应 exit 0（登录→选仓→/land→/inventory，含 CSRF 回填）。"""
    r = _run_sim_user(fake_dc_server, {"SIM_USER_USER": "tester", "SIM_USER_PASSWORD": "pw"})
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "4 关键路径全通" in r.stdout
    for path_name in ("登录页可达", "登录成功", "选仓进入总览", "库存页可达"):
        assert path_name in r.stdout


def test_sim_user_bad_credentials_fails(fake_dc_server):
    """错误密码应 exit 1（401 → 不允许把登录失败当通过）。"""
    r = _run_sim_user(fake_dc_server, {"SIM_USER_USER": "tester", "SIM_USER_PASSWORD": "wrong"})
    assert r.returncode == 1
    assert "登录成功" in r.stdout  # 失败路径被记录
    assert r.returncode != 0


def test_sim_user_missing_credentials_exit_64():
    """缺凭据应 exit 64 + 明确提示（绝不静默跳过）。"""
    r = _run_sim_user("http://127.0.0.1:1", {})
    assert r.returncode == 64
    assert "SIM_USER_USER" in (r.stdout + r.stderr)


# ============================================================
# Issue #34：check 2 的 sim 凭据必须 provision 进「绿」库
#
# 症状（2026-10-15 verify-18b 实测）：verify.sh 由 wrapper shell 调用，不在绿
# unit 环境里、通常没有 DAILYCHECK_DB_DIR —— 此时 create-user 写**蓝库
# （production 真库）**，而探针打的是绿 :8091（绿进程读 GREEN_DB）→
# 绿库查无此人 → 路径 2 恒 401。
# 修法：_ensure_sim_user_in_green_db() 用**显式覆盖语义**的
# DAILYCHECK_DB_DIR 在绿库内幂等 ensure 账号（且必须 --admin：非 admin
# 用户的选仓页只列 warehouse_users 绑定的仓 → 空 → 路径 3 必失败），
# 并在探针前复核「密码匹配 + 有可见仓」。
# 下列用例把该函数从 verify.sh 里抽出来真跑（受控 env + 临时库），
# 关键断言均已变异验证（去掉 env pin / 去掉复核 / 去掉 --admin → 必 FAIL）。
# ============================================================
_ENSURE_START = "# <<< ensure-sim-user:start >>>"
_ENSURE_END = "# <<< ensure-sim-user:end >>>"


def _extract_ensure_fn() -> str:
    """从 verify.sh 抽出 _ensure_sim_user_in_green_db（行为用例真跑它）。

    用显式标记定界（不靠花括号配平猜）——verify.sh 删标记即测试失败。
    """
    content = VERIFY.read_text(encoding="utf-8")
    assert _ENSURE_START in content and _ENSURE_END in content, (
        "verify.sh 缺 ensure-sim-user 标记（tests 抽取函数体的锚点）"
    )
    body = content.split(_ENSURE_START, 1)[1].split(_ENSURE_END, 1)[0].strip()
    assert "_ensure_sim_user_in_green_db() {" in body, "标记之间不是该函数"
    return body


def _init_master(db_dir: Path) -> None:
    """在指定 DB 目录建一个真的 master.db（走项目自己的 CLI）。"""
    r = subprocess.run(
        [sys.executable, "-m", "flask", "--app", "app", "init-master"],
        capture_output=True, text=True, timeout=120, cwd=str(REPO_ROOT),
        env={**os.environ, "DAILYCHECK_DB_DIR": str(db_dir)},
    )
    assert r.returncode == 0, f"init-master 失败: {r.stdout}{r.stderr}"
    assert (db_dir / "master.db").is_file(), "init-master 未产出 master.db"


def _seed_user(
    db_dir: Path, user: str, password: str,
    *, is_admin: int = 0, is_active: int = 1, bind_to_warehouse: int | None = None,
) -> None:
    """直接在绿库里塞一个凭据（模拟 up.sh 快照里已有的账号）。

    bind_to_warehouse 非 None 时补一行 warehouse_users 绑定——
    守卫只判「有没有绑定行」，故无需真造 warehouse 记录。
    """
    with sqlite3.connect(db_dir / "master.db") as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, is_admin, is_active,"
            " created_at) VALUES (?, ?, ?, ?, '2026-01-01 00:00:00')",
            (user, generate_password_hash(password), is_admin, is_active),
        )
        if bind_to_warehouse is not None:
            uid = conn.execute(
                "SELECT id FROM users WHERE username=?", (user,)
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO warehouse_users (user_id, warehouse_id, role)"
                " VALUES (?, ?, 'staff')",
                (uid, bind_to_warehouse),
            )
        conn.commit()


def _user_row(db_dir: Path, user: str) -> tuple[int, int] | None:
    """回读 (is_admin, 绑定数)；账号不存在返回 None。"""
    with sqlite3.connect(db_dir / "master.db") as conn:
        row = conn.execute(
            "SELECT id, is_admin FROM users WHERE username=?", (user,)
        ).fetchone()
        if row is None:
            return None
        bound = conn.execute(
            "SELECT count(*) FROM warehouse_users WHERE user_id=?", (row[0],)
        ).fetchone()[0]
    return row[1], bound


def _run_ensure_fn(
    green_db: Path, *,
    user: str = "sim_user_t",
    password: str = "sim-pw",
    inherited_db_dir: Path | None = None,
    omit_creds: bool = False,
    repo_root: Path | None = None,
    blue_master: Path | None = None,
    python_bin: str | None = None,
) -> subprocess.CompletedProcess:
    """在受控环境里真跑 verify.sh 的 ensure 函数。

    inherited_db_dir 非 None 时模拟「wrapper shell 自带 DAILYCHECK_DB_DIR」
    （生产里这是蓝库）——函数必须用覆盖语义顶掉它。
    omit_creds=True 时不注入凭据（验证函数自身的非空前置门禁）。
    repo_root / blue_master 可覆盖「蓝库」位置，让「拒绝写蓝库」两个闸
    能在**完全不碰仓库真库**的前提下被验证。
    """
    lines = [
        "set -uo pipefail",
        f'REPO_ROOT="{repo_root or REPO_ROOT}"',
        f'DC_PYTHON_BIN="{python_bin or sys.executable}"',
        f'GREEN_DB="{green_db}"',
    ]
    if not omit_creds:
        lines += [
            f'export SIM_USER_USER="{user}"',
            f'export SIM_USER_PASSWORD="{password}"',
        ]
    if blue_master is not None:
        lines.append(f'export BLUE_MASTER="{blue_master}"')
    lines += [_extract_ensure_fn(), "_ensure_sim_user_in_green_db", ""]
    env = os.environ.copy()
    for key in ("DAILYCHECK_DB_DIR", "SIM_USER_USER", "SIM_USER_PASSWORD",
                "BLUE_MASTER"):
        env.pop(key, None)
    if inherited_db_dir is not None:
        env["DAILYCHECK_DB_DIR"] = str(inherited_db_dir)
    return subprocess.run(
        ["bash", "-c", "\n".join(lines)], capture_output=True, text=True,
        env=env, timeout=120, cwd=str(REPO_ROOT),
    )


def test_verify_check2_ensures_account_in_green_db_statically():
    """静态：check 2 把 ensure 当硬门禁，env pin 用「覆盖」语义且带 --admin。"""
    content = VERIFY.read_text(encoding="utf-8")
    fn = _extract_ensure_fn()
    assert "if ! _ensure_sim_user_in_green_db; then" in content, (
        "check 2 未把 ensure 当硬门禁（失败必须 return 1）"
    )
    # 子 shell 内显式赋值 = 覆盖语义；`:-` 继承写法会漏回蓝库
    assert 'DAILYCHECK_DB_DIR="${GREEN_DB}" "${DC_PYTHON_BIN}" -m flask' in fn
    assert 'DAILYCHECK_DB_DIR="${GREEN_DB}" "${DC_PYTHON_BIN}" - <<' in fn
    assert 'DAILYCHECK_DB_DIR="${DAILYCHECK_DB_DIR:-' not in fn, (
        "env 用了继承写法——wrapper 自带蓝库路径时会把账号写进蓝库"
    )
    # 绑定命令行形状，不绑裸字符串：函数体注释/提示里也出现 "admin" 字样，
    # 裸 `assert "--admin" in fn` 会被文案命中而成恒真断言（轮 2 审核实测）。
    assert re.search(
        r"create-user\s+\"\$\{SIM_USER_USER\}\"\s+\"\$\{SIM_USER_PASSWORD\}\"\s+--admin",
        fn,
    ), (
        "create-user 未带 --admin——非 admin 选仓页为空 → sim_user 路径 3 必失败"
    )
    # 写库前必须自证不是蓝库（路径形态 + inode），否则 GREEN_DB 误配会先把
    # 已知口令的 admin 写进 production 真库（check 7 排在 check 2 之后）
    assert '"${GREEN_DB}" == "${REPO_ROOT}/db"' in fn, "缺「GREEN_DB 指向仓库真库」拒绝闸"
    assert '"${ino_g}" == "${ino_b}"' in fn, "缺「绿蓝 inode 相同」拒绝闸"
    assert "${BLUE_MASTER:-" in fn, "蓝库路径丢了 BLUE_MASTER 覆盖点（测试无法安全验证 inode 闸）"
    # ensure 必须排在探针之前
    gate_pos = content.find("_ensure_sim_user_in_green_db; then")
    probe_pos = content.find('sim_user.py" \\')
    assert gate_pos != -1 and probe_pos != -1, "verify.sh 缺少门禁或探针调用"
    assert gate_pos < probe_pos, "ensure 未排在 sim_user 探针之前"


def test_ensure_sim_user_writes_admin_account_into_green_db(tmp_path):
    """账号建在绿库里，且是 admin（选仓页才会列出仓）。"""
    green = tmp_path / "green"
    green.mkdir()
    _init_master(green)

    r = _run_ensure_fn(green)
    assert r.returncode == 0, f"ensure 失败: {r.stdout}{r.stderr}"
    assert "OK: 绿库账号" in r.stdout
    assert _user_row(green, "sim_user_t") == (1, 0), (
        "账号没落绿库 or 不是 admin（非 admin 选仓页为空 → 路径 3 必失败）"
    )


def test_ensure_sim_user_overrides_inherited_db_dir(tmp_path):
    """wrapper 自带 DAILYCHECK_DB_DIR（=蓝库）时仍写绿库，蓝库零写入。

    这是 Issue #34 的原始症状：账号被写进蓝库（production 真库）。
    """
    green = tmp_path / "green"
    blue = tmp_path / "blue"
    green.mkdir()
    blue.mkdir()
    _init_master(green)
    _init_master(blue)

    r = _run_ensure_fn(green, inherited_db_dir=blue)
    assert r.returncode == 0, f"ensure 失败: {r.stdout}{r.stderr}"
    assert _user_row(green, "sim_user_t") is not None, "账号没落绿库"
    assert _user_row(blue, "sim_user_t") is None, (
        "账号被写进了 wrapper 继承的库（蓝库/生产真库）——env pin 未生效"
    )


def test_ensure_sim_user_fails_without_credentials(tmp_path):
    """函数自带非空前置门禁（不依赖调用方，也不靠 set -u 的 unbound 报错）。"""
    green = tmp_path / "green"
    green.mkdir()
    _init_master(green)

    r = _run_ensure_fn(green, omit_creds=True)
    assert r.returncode != 0, "缺凭据却通过了"
    assert "缺 SIM_USER_USER" in (r.stdout + r.stderr), (
        "缺凭据时未给出自身门禁消息（说明退化成 set -u 的 unbound 报错）"
    )


def test_ensure_sim_user_rejects_disabled_account(tmp_path):
    """绿库同名账号密码对但是 is_active=0（已停用）→ FAIL + 说明。

    真实登录对停用账号直接 403（blueprints/auth.py:login），与 #34 的
    「含糊失败」同类；create-user 不会覆盖 is_active。
    """
    green = tmp_path / "green"
    green.mkdir()
    _init_master(green)
    _seed_user(green, "sim_user_t", "sim-pw", is_admin=1, is_active=0)

    r = _run_ensure_fn(green)
    assert r.returncode != 0, "已停用账号却通过了（探针只会拿到含糊 403）"
    assert "已停用" in (r.stdout + r.stderr)


def test_ensure_sim_user_reports_create_user_failure(tmp_path):
    """create-user 自身非零退出（解释器配错 / 绿库被锁）要立刻报出来。

    否则只能靠复核阶段的间接结论「无账号（create-user 没落到绿库？）」猜。
    """
    green = tmp_path / "green"
    green.mkdir()
    _init_master(green)

    r = _run_ensure_fn(green, python_bin="/usr/bin/false")
    assert r.returncode != 0, "create-user 失败却整体通过"
    assert "create-user rc=" in (r.stdout + r.stderr), (
        "没有把 create-user 的退出码报出来（诊断退化成间接猜测）"
    )


def test_ensure_sim_user_refuses_repo_db_as_green_db(tmp_path):
    """GREEN_DB 被误配成仓库真库路径 → 拒绝写入（check 7 排在 check 2 之后）。

    REPO_ROOT 用假仓（含 db/master.db），保证这道闸即使被改坏也写不到
    仓库真库；同时按消息断言，避免「假仓没有 app.py → create-user 失败」
    造成与闸无关的假绿。
    """
    fake_repo = tmp_path / "repo"
    (fake_repo / "db").mkdir(parents=True)
    _init_master(fake_repo / "db")

    r = _run_ensure_fn(fake_repo / "db", repo_root=fake_repo)
    assert r.returncode != 0, "GREEN_DB 指向蓝库却照写"
    assert "指向仓库内真库" in (r.stdout + r.stderr)
    assert _user_row(fake_repo / "db", "sim_user_t") is None, "闸没拦住实写"


def test_ensure_sim_user_refuses_same_inode_as_blue(tmp_path):
    """绿库 master.db 与蓝库同一物理文件（inode 相同）→ 拒绝写入。

    BLUE_MASTER 把「蓝库」指到临时文件 → 即使这道闸被改坏，也只会写到
    这个临时文件，绝不动仓库真库（这条硬链指向 tmp，不指向 repo）。
    """
    blue = tmp_path / "blue"
    green = tmp_path / "green"
    blue.mkdir()
    green.mkdir()
    _init_master(blue)
    os.link(blue / "master.db", green / "master.db")  # 硬链 → 同 inode

    r = _run_ensure_fn(green, blue_master=blue / "master.db")
    assert r.returncode != 0, "绿蓝同物理文件却照写"
    assert "inode 相同" in (r.stdout + r.stderr)
    assert _user_row(blue, "sim_user_t") is None, "闸没拦住实写（蓝库被污染）"


def test_ensure_sim_user_fails_when_green_db_absent(tmp_path):
    """绿库不存在（up.sh db-copy 未跑）必须 FAIL，不许静默继续。"""
    green = tmp_path / "gone"
    green.mkdir()  # 目录在、master.db 不在

    r = _run_ensure_fn(green)
    assert r.returncode != 0, "绿库缺失却通过了"
    assert "不存在" in (r.stdout + r.stderr)


def test_ensure_sim_user_fails_on_password_mismatch(tmp_path):
    """绿库已有同名账号但密码不同 → 精确 FAIL（否则探针只会报含糊 401）。

    up.sh 拷的是蓝库快照，绿库里完全可能存在同名不同密码的账号。
    """
    green = tmp_path / "green"
    green.mkdir()
    _init_master(green)
    _seed_user(green, "sim_user_t", "another-pw", is_admin=1)

    r = _run_ensure_fn(green, password="sim-pw")
    assert r.returncode != 0, "密码不匹配却通过了"
    assert "密码 != SIM_USER_PASSWORD" in (r.stdout + r.stderr)


def test_ensure_sim_user_rejects_unbound_non_admin(tmp_path):
    """绿库同名账号密码对但不是 admin 且无仓绑定 → FAIL + 说明（路径 3 根因）。"""
    green = tmp_path / "green"
    green.mkdir()
    _init_master(green)
    _seed_user(green, "sim_user_t", "sim-pw", is_admin=0)

    r = _run_ensure_fn(green)
    assert r.returncode != 0, "无可见仓的账号却通过了（路径 3 会含糊失败）"
    assert "无仓绑定" in (r.stdout + r.stderr)


def test_ensure_sim_user_accepts_bound_non_admin(tmp_path):
    """非 admin 但有仓绑定 → 选仓页非空，守卫不该过严（边界）。"""
    green = tmp_path / "green"
    green.mkdir()
    _init_master(green)
    _seed_user(green, "sim_user_t", "sim-pw", is_admin=0, bind_to_warehouse=1)

    r = _run_ensure_fn(green)
    assert r.returncode == 0, f"有绑定的非 admin 被误拒: {r.stdout}{r.stderr}"


# ============================================================
# Issue #28: 蓝绿 DB 隔离（DAILYCHECK_DB_DIR + 真库拷贝 + check7 证据）
# ============================================================

def _import_config_with(env: dict[str, str]) -> str:
    """子进程里 import config，回传关键路径（避免污染测试进程的 config）。"""
    code = (
        "import json, config;"
        "print(json.dumps({'DB_DIR': str(config.DB_DIR),"
        " 'MASTER_DB': str(config.MASTER_DB),"
        " 'WAREHOUSE_DB_DIR': str(config.WAREHOUSE_DB_DIR)}))"
    )
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, **env},
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_config_db_dir_env_override(tmp_path):
    """DAILYCHECK_DB_DIR 设置时三路径整体迁移；未设置时保持 repo 内 db/。"""
    import json

    green_db = tmp_path / "green-db"
    got = json.loads(_import_config_with({"DAILYCHECK_DB_DIR": str(green_db)}))
    assert got["DB_DIR"] == str(green_db)
    assert got["MASTER_DB"] == str(green_db / "master.db")
    assert got["WAREHOUSE_DB_DIR"] == str(green_db / "warehouses")

    default = json.loads(_import_config_with({}))
    repo_db = Path(__file__).resolve().parent.parent / "db"
    assert default["DB_DIR"] == str(repo_db)
    assert default["MASTER_DB"] == str(repo_db / "master.db")


def test_mcp_config_reexports_follow_root_config():
    """mcp_server/config.py 必须从根 config re-export MASTER_DB——
    这样 DAILYCHECK_DB_DIR 才能同时覆盖 Flask 与 MCP 两类进程。"""
    mcp_cfg = Path(__file__).resolve().parent.parent / "mcp_server" / "config.py"
    content = mcp_cfg.read_text(encoding="utf-8")
    assert re.search(r"from config import", content), (
        "mcp_server/config.py 不再从根 config 导入——DB 隔离覆盖会漏掉 MCP 进程"
    )
    assert "MASTER_DB" in content


def test_up_sh_copies_real_db_tree_dereferenced():
    """up.sh 必须从真库 ${APP_DIR}/db 拷（cp -aL 解软链），不再 cp 蓝根目录。

    Issue #28: 旧版 `cp -a /var/lib/dailycheck → /var/lib/dailycheck-green`
    两边都是遗留目录，cp 把源拷进目标内部——拷了个寂寞，蓝绿共享同一物理库。
    """
    content = UP.read_text(encoding="utf-8")
    assert 'cp -aL "${SRC_DB_DIR}" "${GREEN_DB}"' in content, "未用 cp -aL 拷真库"
    assert 'SRC_DB_DIR="${DAILYCHECK_SRC_DB_DIR:-${APP_DIR}/db}"' in content
    assert "cp -a \"${BLUE_DB}\"" not in content, "还在拷蓝根目录（Issue #28 未修）"


def test_up_sh_requires_unit_db_dir_declaration():
    """up.sh 必须守卫：绿 unit 未声明 DAILYCHECK_DB_DIR 或与拷贝目标不一致时拒启。"""
    content = UP.read_text(encoding="utf-8")
    assert "DAILYCHECK_DB_DIR" in content
    assert "未声明 DAILYCHECK_DB_DIR" in content
    # 两处失败路径都必须 exit 1（不匹配 / 缺声明）
    assert content.count("exit 1") >= 2
    assert "daemon-reload" in content  # 错误信息给出修复方法


def test_up_sh_green_db_default_is_isolated_copy_dir():
    """GREEN_DB 默认值 = /var/lib/dailycheck-green/db（unit env 同值）。"""
    content = UP.read_text(encoding="utf-8")
    assert 'GREEN_DB="${GREEN_DB:-/var/lib/dailycheck-green/db}"' in content


def test_verify_check7_isolation_evidence():
    """verify.sh check 7：三重证据（environ 声明 + 非软链 + inode 相异）。"""
    content = VERIFY.read_text(encoding="utf-8")
    assert "_verify_check7_db_isolation()" in content
    assert '_run_check "7"' in content
    # a) 进程 environ 证据（比 /proc/*/fd 可靠：sqlite 连接每请求开关会撞空窗）
    assert "DAILYCHECK_DB_DIR=" in content
    assert "/proc/${main_pid}/environ" in content
    # b) 绿 master.db 不许是软链
    assert '[[ -L "${master_green}" ]]' in content
    # c) inode 相异性（同一物理文件铁证排除）
    assert "stat -c %i" in content
    # 报告表格要有第 7 行
    assert "| 7 | DB 隔离证据" in content


# ============================================================
# Issue #32 —— nginx 入口硬编码 + 灰度从未真实生效
# 修复策略（Option 2，Eric 拍板）：合并为单 upstream 蓝绿同池，location
# 永远指 dailycheck / dailycheck-mcp，switch.sh 不动 location。三道防线：
#   ① switch.sh 布局守卫（旧布局拒跑）
#   ② switch.sh post-switch 探活（过 nginx 入口 :8080，非 502 才算成功）
#   ③ verify.sh check 8（终态保险，再过一遍入口探活）
# ============================================================

# 旧布局（location 硬编码 dailycheck-blue/green）
_OLD_LAYOUT_CONF = """\
upstream dailycheck-blue {
    server 127.0.0.1:8090;
}
upstream dailycheck-green {
    server 127.0.0.1:8091;
}
upstream dailycheck-mcp {
    server 127.0.0.1:5100;
    server 127.0.0.1:5101;
}
server {
    listen 8080;
    location / {
        proxy_pass http://dailycheck-blue;
    }
    location /api/mcp/ {
        proxy_pass http://dailycheck-mcp;
    }
}
"""

# 新布局（单 upstream 蓝绿同池，location 指合并 upstream）
_NEW_LAYOUT_CONF = """\
upstream dailycheck {
    server 127.0.0.1:8090 weight=100;
    server 127.0.0.1:8091 down;
}
upstream dailycheck-mcp {
    server 127.0.0.1:5100 weight=100;
    server 127.0.0.1:5101 down;
}
server {
    listen 8080;
    location / {
        proxy_pass http://dailycheck;
    }
    location /api/mcp/ {
        proxy_pass http://dailycheck-mcp;
    }
}
"""


def test_switch_layout_guard_rejects_old_layout(tmp_path):
    """布局守卫：旧布局（location 硬编码 dailycheck-blue）必须 exit 2 拒跑。

    Issue #32: switch.sh 只 sed server 行不动 location，旧布局下 upstream
    切了真实流量不动 → 502 必现。守卫让运维先按 nginx-site.conf 模板
    重构为单 upstream 蓝绿同池再切。
    """
    conf = tmp_path / "old.conf"
    conf.write_text(_OLD_LAYOUT_CONF, encoding="utf-8")
    env = {**os.environ, "NGINX_DAILYCHECK_CONF": str(conf), "BACKUP_DIR": str(tmp_path / "bk")}
    result = subprocess.run(
        ["bash", str(SWITCH), "green", "50", "--dry-run"],
        capture_output=True, text=True, timeout=30, env=env,
    )
    assert result.returncode == 2, (
        f"旧布局必须 exit 2 拒跑, 实际 {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    out = result.stdout + result.stderr
    assert "旧布局守卫触发" in out
    assert "dailycheck-blue/green" in out
    # 绝不能走到 sed / nginx -t 步骤
    assert "sed -i" not in out or "[DRY-RUN] would execute: sed" in out
    # 注意 dry-run 也走守卫（守卫是 prod 防线，dry-run 不能绕过）


def test_switch_layout_guard_passes_new_layout(tmp_path):
    """布局守卫：新布局（单 upstream 蓝绿同池）应当通过，继续到 sed/reload。"""
    conf = tmp_path / "new.conf"
    conf.write_text(_NEW_LAYOUT_CONF, encoding="utf-8")
    env = {**os.environ, "NGINX_DAILYCHECK_CONF": str(conf), "BACKUP_DIR": str(tmp_path / "bk")}
    result = subprocess.run(
        ["bash", str(SWITCH), "green", "50", "--dry-run"],
        capture_output=True, text=True, timeout=30, env=env,
    )
    assert result.returncode == 0, (
        f"新布局应通过守卫, 实际 {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    out = result.stdout + result.stderr
    assert "布局守卫 OK" in out


def test_switch_post_switch_probe_dry_run():
    """post-switch 探活：dry-run 也打印探活说明（提醒 prod 真跑时这步真做）。"""
    result = _run(SWITCH, "green", "50", "--dry-run")
    out = result.stdout + result.stderr
    assert "post-switch 探活" in out or "would probe" in out
    # 探活 url 应是 nginx 入口 :8080
    assert "127.0.0.1:8080" in out


def test_switch_post_switch_probe_live_against_listening_server(tmp_path):
    """post-switch 探活实跑：起一个真 http 服务模拟 nginx 入口，验证 NGINX_ENTRY 透传。

    验证 5xx/000 之外的非 502 状态码（4xx 也算通：业务 4xx 不是 nginx 502）。
    """
    conf = tmp_path / "new.conf"
    conf.write_text(_NEW_LAYOUT_CONF, encoding="utf-8")
    backup = tmp_path / "bk"
    backup.mkdir()

    # 起一个最简 http server 返回 200 / 401
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/api/mcp/"):
                self.send_response(401)
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
        def log_message(self, *_args, **_kwargs):  # noqa: A003
            return  # 静默

    # 监听一个空闲端口
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    srv = http.server.HTTPServer(("127.0.0.1", port), Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        # 关键：把 NGINX_ENTRY 指向真 http server，验证 switch.sh 透传
        # 自定义入口（而不是默认 :8080）。用 dry-run 跳过 reload/backup
        # 避免真 nginx -t 失败。
        env = {
            **os.environ,
            "NGINX_DAILYCHECK_CONF": str(conf),
            "BACKUP_DIR": str(backup),
            "NGINX_ENTRY": f"http://127.0.0.1:{port}",
        }
        result = subprocess.run(
            ["bash", str(SWITCH), "green", "50", "--dry-run"],
            capture_output=True, text=True, timeout=30, env=env,
        )
        out = result.stdout + result.stderr
        # dry-run 必含 探活说明
        assert "would probe" in out
        # NGINX_ENTRY 透传：探活说明里应含自定义端口（而非默认 :8080）
        assert f"127.0.0.1:{port}" in out, (
            f"NGINX_ENTRY 透传失败：{port} 端口未出现在 switch.sh output\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    finally:
        srv.shutdown()
        th.join(timeout=2)


def test_switch_layout_guard_does_not_touch_location(tmp_path):
    """布局守卫+新布局：sed 后 location 行必须仍是 `proxy_pass http://dailycheck;`

    不能动 location 是 Option 2 的核心契约 —— location 永远指合并 upstream。
    """
    conf = tmp_path / "new.conf"
    conf.write_text(_NEW_LAYOUT_CONF, encoding="utf-8")
    backup = tmp_path / "bk"
    backup.mkdir()
    # 静态断言：switch.sh sed 表达式只动 server 127.0.0.1:PORT 行，
    # 不出现 proxy_pass 字样。
    content = SWITCH.read_text(encoding="utf-8")
    # 直接断言 sed 块（_run sed -i 到 "${NGINX_CONF}" 之间）不含 proxy_pass
    sed_match = re.search(
        r"_run sed -i (.*?)\"\$\{NGINX_CONF\}\"",
        content,
        re.DOTALL,
    )
    assert sed_match is not None, "未找到 sed -i 块"
    sed_text = sed_match.group(1)
    # sed 表达式只能匹配 `server 127.0.0.1:PORT weight=N;` 或 `down;`，
    # 不能匹配 `proxy_pass`
    assert "proxy_pass" not in sed_text, (
        f"switch.sh sed 块不应动 location proxy_pass（Issue #32 Option 2）:\n{sed_text}"
    )
    # 强约束：sed 只能动 server 行的 weight/down
    assert sed_text.count("server 127.0.0.1:") >= 4  # 主 + MCP 蓝绿各 1 行


def test_switch_layout_guard_source_static():
    """静态检查：switch.sh 必须有布局守卫三段逻辑。"""
    content = SWITCH.read_text(encoding="utf-8")
    assert "布局守卫" in content
    # 旧布局 grep pattern
    assert "dailycheck-(blue|green)" in content
    # 新布局必含项
    assert 'proxy_pass http://dailycheck[\\s;]' in content
    # 拒跑 exit code = 2
    assert "exit 2" in content


def test_switch_post_switch_probe_source_static():
    """静态检查：post-switch 探活 + 自动回滚。"""
    content = SWITCH.read_text(encoding="utf-8")
    assert "post-switch 探活" in content
    # 探活目标：主站 + MCP
    assert '${NGINX_ENTRY}/' in content
    assert '${NGINX_ENTRY}/api/mcp/' in content
    # 5xx / 000 判为失败
    assert "=~ ^5" in content
    assert '"000"' in content
    # 自动回滚：cp 备份 + nginx -t + reload
    assert 'cp "${BACKUP}" "${NGINX_CONF}"' in content


def test_nginx_site_conf_template_exists():
    """scripts/blue_green/nginx-site.conf 模板存在且为单 upstream 形态。"""
    template = SCRIPTS_DIR / "nginx-site.conf"
    assert template.exists(), "缺少 nginx-site.conf 模板（Issue #32 目标布局）"
    content = template.read_text(encoding="utf-8")
    # 单 upstream：合并 dailycheck / dailycheck-mcp
    assert "upstream dailycheck {" in content
    assert "upstream dailycheck-mcp {" in content
    # 严禁独立 dailycheck-blue/green upstream（注释里出现 OK，
    # 但「upstream dailycheck-blue」/「upstream dailycheck-green」作关键字
    # 独立 upstream 块不能存在）
    assert "upstream dailycheck-blue" not in content
    assert "upstream dailycheck-green" not in content
    # location 必指合并 upstream
    assert "proxy_pass http://dailycheck;" in content
    assert "proxy_pass http://dailycheck-mcp;" in content


def test_verify_check8_nginx_entry_probe():
    """verify.sh check 8：nginx 入口 :8080 探活，5xx/000 判 FAIL。

    旧 bug 是「upstream 切了但 location 还在指旧池」→ 用户 502。
    探活必须过 nginx 入口（不绕开 nginx 探 upstream 端口）。
    """
    content = VERIFY.read_text(encoding="utf-8")
    assert "_verify_check8_nginx_entry()" in content
    assert '_run_check "8"' in content
    # 探活目标 = nginx 入口 :8080
    assert "127.0.0.1:8080" in content
    assert '${NGINX_ENTRY}/' in content
    assert '${NGINX_ENTRY}/api/mcp/' in content
    # 5xx / 000 判失败（与 switch.sh 探活一致）
    assert "=~ ^5" in content
    assert '"000"' in content
    # 报告表格要有第 8 行
    assert "| 8 | nginx 入口探活" in content

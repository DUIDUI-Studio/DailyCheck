"""switch.sh DRY_RUN 守卫 + conf 应用核验 + 落点核验（2026-10-15 P0 事故加固）。

背景：2026-10-15 绿实例以 DRY_RUN=true 承接生产流量 44 分钟——窗口期写操作
全部「假成功」（HTTP 200 / 审计照记 / DB 不落盘）。加固三件套：
  ① DRY_RUN 守卫：切换后承接流量（weight>0）的实例处于 DRY_RUN 时拒切（exit 3）；
  ② conf 应用核验：sed 未命中时 nginx -t 仍过、reload 仍「成功」→ 逐行核对 4
     条 server 行与期望一致，不一致回滚（exit 1）；
  ③ 落点核验：100%/0% 边界用带 nonce 的探针核对流量真实落进目标实例的
     access log（对侧命中 = reload 未生效 → 回滚；读不到日志 = 仅告警）。

测试方式：PATH 前置 fake systemctl / fake nginx（dev/CI 不碰真 systemd/nginx），
switch.sh 以 **live 模式** 跑全流程；探针打本地 HTTP 服务，「实例 access log」
用临时文件模拟（探针含 nonce 时由假服务写入目标侧或对侧）。
"""
from __future__ import annotations

import http.server
import os
import subprocess
import threading
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts" / "blue_green"
SWITCH = SCRIPTS_DIR / "switch.sh"

# 单 upstream 新布局基准 conf（Issue #32 模板形态；green 初始 down）
_CONF_BASE = """\
upstream dailycheck {
    server 127.0.0.1:8090 weight=100;  # blue
    server 127.0.0.1:8091 down;        # green
}
upstream dailycheck-mcp {
    server 127.0.0.1:5100 weight=100;  # blue-mcp
    server 127.0.0.1:5101 down;        # green-mcp
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


# ============================================================
# 测试基建：fake systemctl / fake nginx / 假 nginx 入口
# ============================================================
def _make_fake_bin(tmp_path: Path) -> Path:
    """fake systemctl（只服务 show / reload）+ fake nginx（-t 恒过）。

    fake systemctl 的 Environment 从 ${FAKE_SYSTEMCTL_DIR}/<unit>.env 读，
    支持按 unit 注入不同的环境串；MainPID 恒为空 → 守卫回退到 unit 配置
    层取证（/proc 层在无真实进程时天然跳过）。
    """
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    systemctl = bindir / "systemctl"
    systemctl.write_text(
        "#!/usr/bin/env bash\n"
        "# switch.sh 测试用 fake：只服务 show -p <prop> --value <unit> / reload\n"
        "prop=''; unit=''\n"
        "for a in \"$@\"; do\n"
        "  case \"$a\" in\n"
        "    MainPID|Environment) prop=\"$a\" ;;\n"
        "    *.service) unit=\"$a\" ;;\n"
        "  esac\n"
        "done\n"
        "if [[ \"${1:-}\" == \"show\" ]]; then\n"
        "  case \"${prop}\" in\n"
        "    MainPID) echo \"\" ;;\n"
        "    Environment) cat \"${FAKE_SYSTEMCTL_DIR}/${unit}.env\" 2>/dev/null || true ;;\n"
        "  esac\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    systemctl.chmod(0o755)
    nginx = bindir / "nginx"
    nginx.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    nginx.chmod(0o755)
    return bindir


def _switch_env(
    tmp_path: Path,
    unit_envs: dict[str, str] | None = None,
    **extra: str,
) -> dict[str, str]:
    """构造 switch.sh 运行环境：PATH 前置 fake bin + 按 unit 写 .env 文件。"""
    bindir = _make_fake_bin(tmp_path)
    unit_dir = tmp_path / "unit-envs"
    unit_dir.mkdir(exist_ok=True)
    for unit, val in (unit_envs or {}).items():
        (unit_dir / f"{unit}.env").write_text(val, encoding="utf-8")
    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["FAKE_SYSTEMCTL_DIR"] = str(unit_dir)
    env.update(extra)
    return env


class _ProbeSink:
    """记录探针请求；带 nonce 的探针请求「落进」指定假 access log。"""

    def __init__(self, log_to: Path | None = None):
        self.log_to = log_to
        self.paths: list[str] = []

    def record(self, path: str) -> None:
        self.paths.append(path)
        if self.log_to is not None and "dc_probe_" in path:
            with self.log_to.open("a", encoding="utf-8") as fh:
                fh.write(f'127.0.0.1 - - [probe] "GET {path} HTTP/1.1" 200\n')


def _start_server(sink: _ProbeSink) -> http.server.ThreadingHTTPServer:
    """假 nginx 入口：主站 200 / MCP 401（均非 5xx），落点写入 sink 的日志。"""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            sink.record(self.path)
            if self.path.startswith("/api/mcp/"):
                self.send_response(401)
            else:
                self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):  # 静默
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _run_switch(env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SWITCH), *args],
        capture_output=True, text=True, env=env, timeout=60,
    )


def _write_conf(tmp_path: Path, text: str | None = None) -> Path:
    conf = tmp_path / "dailycheck.conf"
    conf.write_text(text if text is not None else _CONF_BASE, encoding="utf-8")
    return conf


# ============================================================
# ① DRY_RUN 守卫
# ============================================================
def test_guard_blocks_dry_run_green(tmp_path):
    """绿侧 DRY_RUN=true 时 switch green 100 必须 exit 3，且不动 conf / 不产生备份。

    这是 2026-10-15 事故的直接防线：绿实例还在写隔离就必须拒绝切流量。
    """
    conf = _write_conf(tmp_path)
    original = conf.read_text(encoding="utf-8")
    env = _switch_env(
        tmp_path,
        {"dailycheck-green.service": "DRY_RUN=true DAILYCHECK_DB_DIR=/var/lib/dailycheck-green/db"},
        NGINX_DAILYCHECK_CONF=str(conf),
        BACKUP_DIR=str(tmp_path / "bk"),
    )
    r = _run_switch(env, "green", "100")
    assert r.returncode == 3, f"stdout={r.stdout}\nstderr={r.stderr}"
    out = r.stdout + r.stderr
    assert "DRY_RUN 守卫触发" in out
    assert "绿 dailycheck-green.service" in out
    assert "绿转正" in out  # 错误提示指向 README 标准步骤
    # 拒跑发生在任何写动作之前：conf 未被 sed 改动、无备份文件
    assert conf.read_text(encoding="utf-8") == original
    assert not list((tmp_path / "bk").glob("*.bak"))


def test_guard_blocks_when_any_receiving_side_dry_run(tmp_path):
    """灰度 50%（两侧都承接流量）：绿侧 DRY_RUN=1（非 "true" 写法）同样命中拒跑。"""
    conf = _write_conf(tmp_path)
    env = _switch_env(
        tmp_path,
        {
            "dailycheck.service": "FOO=bar",
            "dailycheck-green.service": "DRY_RUN=1",
        },
        NGINX_DAILYCHECK_CONF=str(conf),
        BACKUP_DIR=str(tmp_path / "bk"),
    )
    r = _run_switch(env, "green", "50")
    assert r.returncode == 3, f"stdout={r.stdout}\nstderr={r.stderr}"
    out = r.stdout + r.stderr
    assert "DRY_RUN 守卫触发" in out
    assert "绿 dailycheck-green.service" in out  # 只点名绿侧
    assert "蓝 dailycheck.service" not in out


def test_guard_blocks_blue_side_too(tmp_path):
    """对称覆盖：switch blue 100（回滚方向）承接侧=蓝，蓝 DRY_RUN 同样拒跑。"""
    conf = _write_conf(tmp_path)
    env = _switch_env(
        tmp_path,
        {"dailycheck.service": "DRY_RUN=true"},
        NGINX_DAILYCHECK_CONF=str(conf),
        BACKUP_DIR=str(tmp_path / "bk"),
    )
    r = _run_switch(env, "blue", "100")
    assert r.returncode == 3, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert "蓝 dailycheck.service" in (r.stdout + r.stderr)


def test_guard_unknown_warns_and_continues_dry_run(tmp_path):
    """无法判定（fake unit 无 Environment）→ 仅告警放行，不阻断；

    同时断言 --dry-run 路径也照跑守卫（守卫是 prod 防线，dry-run 不能绕过），
    且 dry-run 回显包含落点核验说明。
    """
    conf = _write_conf(tmp_path)
    env = _switch_env(
        tmp_path,
        {},
        NGINX_DAILYCHECK_CONF=str(conf),
        BACKUP_DIR=str(tmp_path / "bk"),
    )
    r = _run_switch(env, "green", "100", "--dry-run")
    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    out = r.stdout + r.stderr
    assert "无法判定" in out
    assert "DRY_RUN 守卫" in out
    assert "落点核验" in out


# ============================================================
# ② conf 应用核验（sed 静默未命中）
# ============================================================
def test_conf_applied_check_catches_sed_noop(tmp_path):
    """sed 未命中的 server 行（多余空格）→ 核验失败 exit 1 + conf 回滚。

    旧行为：sed 未替换时 nginx -t 仍通过、reload 仍「成功」——「切了但没变」
    完全静默。新行为：逐行核对 4 条 server 行，不一致立即回滚。
    """
    weird = _CONF_BASE.replace(
        "server 127.0.0.1:8091 down;", "server 127.0.0.1:8091 down ;"
    )
    conf = _write_conf(tmp_path, weird)
    env = _switch_env(
        tmp_path,
        {"dailycheck-green.service": "FOO=bar"},
        NGINX_DAILYCHECK_CONF=str(conf),
        BACKUP_DIR=str(tmp_path / "bk"),
    )
    r = _run_switch(env, "green", "100")
    assert r.returncode == 1, f"stdout={r.stdout}\nstderr={r.stderr}"
    out = r.stdout + r.stderr
    assert "conf 应用核验失败" in out
    # 回滚：conf 恢复原样
    assert conf.read_text(encoding="utf-8") == weird


# ============================================================
# ③ 落点核验（nonce 探针）
# ============================================================
def test_landing_probe_ok_full_flow(tmp_path):
    """live 全流程：守卫放行 → sed → 核验 → reload → 探活 → 落点命中绿 → exit 0。"""
    conf = _write_conf(tmp_path)
    green_log = tmp_path / "green-access.log"
    blue_log = tmp_path / "blue-access.log"
    green_log.write_text("", encoding="utf-8")
    blue_log.write_text("", encoding="utf-8")
    srv = _start_server(_ProbeSink(log_to=green_log))
    try:
        env = _switch_env(
            tmp_path,
            {"dailycheck.service": "FOO=bar", "dailycheck-green.service": "PORT=5101"},
            NGINX_DAILYCHECK_CONF=str(conf),
            BACKUP_DIR=str(tmp_path / "bk"),
            GREEN_ACCESS_LOG=str(green_log),
            BLUE_ACCESS_LOG=str(blue_log),
            NGINX_ENTRY=f"http://127.0.0.1:{srv.server_address[1]}",
        )
        r = _run_switch(env, "green", "100")
    finally:
        srv.shutdown()
        srv.server_close()

    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    out = r.stdout + r.stderr
    assert "DRY_RUN 守卫 OK" in out
    assert "conf 应用核验 OK" in out
    assert "落点核验 OK" in out
    # conf 已切到 green 100 / blue down
    content = conf.read_text(encoding="utf-8")
    assert "server 127.0.0.1:8090 down;" in content
    assert "server 127.0.0.1:8091 weight=100;" in content
    # 备份存在（回滚素材）
    assert list((tmp_path / "bk").glob("*.bak"))


def test_landing_probe_wrong_side_rolls_back(tmp_path):
    """nonce 只出现在对侧（蓝）access log = reload 未生效 → exit 1 + conf 回滚。"""
    conf = _write_conf(tmp_path)
    original = conf.read_text(encoding="utf-8")
    green_log = tmp_path / "green-access.log"
    blue_log = tmp_path / "blue-access.log"
    green_log.write_text("", encoding="utf-8")
    blue_log.write_text("", encoding="utf-8")
    # 错误落点：探针被写进「蓝」——即流量实际还留在旧侧
    srv = _start_server(_ProbeSink(log_to=blue_log))
    try:
        env = _switch_env(
            tmp_path,
            {"dailycheck-green.service": "FOO=bar"},
            NGINX_DAILYCHECK_CONF=str(conf),
            BACKUP_DIR=str(tmp_path / "bk"),
            GREEN_ACCESS_LOG=str(green_log),
            BLUE_ACCESS_LOG=str(blue_log),
            NGINX_ENTRY=f"http://127.0.0.1:{srv.server_address[1]}",
        )
        r = _run_switch(env, "green", "100")
    finally:
        srv.shutdown()
        srv.server_close()

    assert r.returncode == 1, f"stdout={r.stdout}\nstderr={r.stderr}"
    out = r.stdout + r.stderr
    assert "落点核验失败" in out
    assert "回滚 reload OK" in out  # 回滚路径真实执行
    assert conf.read_text(encoding="utf-8") == original  # conf 恢复切换前


def test_gray_switch_skips_landing_probe(tmp_path):
    """灰度 50% 路由随机 → 跳过落点断言（只走探活），exit 0。"""
    conf = _write_conf(tmp_path)
    srv = _start_server(_ProbeSink(log_to=None))
    try:
        env = _switch_env(
            tmp_path,
            {"dailycheck.service": "FOO=bar", "dailycheck-green.service": "FOO=bar"},
            NGINX_DAILYCHECK_CONF=str(conf),
            BACKUP_DIR=str(tmp_path / "bk"),
            GREEN_ACCESS_LOG=str(tmp_path / "g.log"),
            BLUE_ACCESS_LOG=str(tmp_path / "b.log"),
            NGINX_ENTRY=f"http://127.0.0.1:{srv.server_address[1]}",
        )
        r = _run_switch(env, "green", "50")
    finally:
        srv.shutdown()
        srv.server_close()

    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert "落点核验跳过" in (r.stdout + r.stderr)


def test_landing_probe_unreadable_logs_only_warns(tmp_path):
    """access log 不存在（不可读）→ 仅告警不回滚，exit 0（不因探针自身盲区阻断）。"""
    conf = _write_conf(tmp_path)
    srv = _start_server(_ProbeSink(log_to=None))
    try:
        env = _switch_env(
            tmp_path,
            {"dailycheck-green.service": "FOO=bar"},
            NGINX_DAILYCHECK_CONF=str(conf),
            BACKUP_DIR=str(tmp_path / "bk"),
            GREEN_ACCESS_LOG=str(tmp_path / "no-such-green.log"),
            BLUE_ACCESS_LOG=str(tmp_path / "no-such-blue.log"),
            NGINX_ENTRY=f"http://127.0.0.1:{srv.server_address[1]}",
        )
        r = _run_switch(env, "green", "100")
    finally:
        srv.shutdown()
        srv.server_close()

    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    out = r.stdout + r.stderr
    assert "落点核验" in out
    assert "无法判定" in out


# ============================================================
# 静态不变量
# ============================================================
def test_switch_guard_source_static():
    """静态检查：守卫/核验的实现要点必须在脚本内（防后续重构误删）。"""
    content = SWITCH.read_text(encoding="utf-8")
    assert "DRY_RUN 守卫" in content
    # 检测优先级：进程 environ → unit 配置
    assert "/proc/${pid}/environ" in content
    assert "systemctl show -p Environment --value" in content
    assert "exit 3" in content
    # 落点核验 nonce
    assert "dc_probe_" in content
    assert "落点核验" in content
    # conf 应用核验
    assert "_verify_conf_applied" in content

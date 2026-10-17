"""promote.sh（绿转正）+ up.sh 活流量守卫（2026-10-17 生产事故回归）。

事故现场（实测）：`up.sh` 在绿**正承接 100% 流量**时原地 `systemctl restart
dailycheck-green`，把它重启成 `DRY_RUN=true` + 副本库 —— 全站写被静默拦成
「假成功」（HTTP 200、库不落盘）；而 `switch.sh` 的 DRY_RUN 守卫只挡「切换」，
挡不住这条。另：蓝绿流程此前**没有自动转正步骤**（关 DRY_RUN + DB 指回真库 +
重启），全凭人工照 README 做，漏一步就是同一形态。

本文件覆盖：
A. `promote.sh` —— dry-run 无副作用 / 正常转正 / 幂等 / 锁门禁 / 健康失败回滚；
B. `up.sh` 活流量守卫 —— 绿在役先切蓝（**顺序**断言）/ 蓝不健康拒跑 / conf
   解析不了拒跑 / 绿不承流时行为不变；
C. `up.sh` **静息态复位**（步骤 3.0，Issue #28 闭环）—— 已转正态 unit 被复位为
   `DRY_RUN=true` + 副本库（**切蓝之后、restart 之前**，用 fake switch 的
   「切流量瞬间 unit 快照」硬断言顺序）/ 缺行则插入 `[Service]` 段 / 幂等（不写
   文件、不 daemon-reload）/ dry-run 无副作用 / drop-in 仍覆盖成 `DRY_RUN=false`
   时 fail-closed 且不重启绿 / 注释行不被改写 / env 文件覆盖也一起复位。

跨平台约束（macOS dev 与 Linux prod 都要能跑）：
- 不碰真实 systemd / nginx / curl：`SYSTEMCTL_BIN` / `CURL_BIN` 指向 fake 脚本，
  PATH 里另置 fake `ss`（up.sh 的 MCP 存活判定用 `ss -tln`）；
- 部署状态目录一律 tmp 覆盖（`DAILYCHECK_DEPLOY_STATE_DIR`），进程锁
  `DAILYCHECK_BG_LOCK_HELD=1` 跳过 —— 否则在已激活的 VPS 上会 exit 10；
- 不用 GNU 专有参数（脚本侧用「sed → 临时文件 → mv」而非 `sed -i`，BSD/GNU 同解）。
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts" / "blue_green"
PROMOTE = SCRIPTS_DIR / "promote.sh"
UP = SCRIPTS_DIR / "up.sh"

APP_DIR = "/root/DailyCheck"  # promote.sh 默认 DAILYCHECK_APP_DIR（生产实路径）
TARGET_DB = f"{APP_DIR}/db"
OLD_DB = "/var/lib/dailycheck-green/db"  # 验证期独立副本

# 绿 app unit 模板：DRY_RUN=true + 副本库 —— 转正前的真实形态
GREEN_UNIT = f"""\
[Unit]
Description=DailyCheck green (candidate)
After=network.target

[Service]
Type=simple
WorkingDirectory={APP_DIR}
Environment=PYTHONUNBUFFERED=1
Environment=DAILYCHECK_ENV=production
Environment=DRY_RUN=true
Environment=DAILYCHECK_DB_DIR={OLD_DB}
ExecStart=/usr/bin/gunicorn -w 2 --threads 4 -b 127.0.0.1:8091 app:app
Restart=always

[Install]
WantedBy=multi-user.target
"""

# 绿 MCP unit 模板：DB 用**带引号**形态（BSD sed 的零长度命中陷阱回归）
GREEN_MCP_UNIT = f"""\
[Service]
Type=simple
Environment=DAILYCHECK_DB_DIR="{OLD_DB}"
ExecStart=/usr/bin/python3 mcp_server/server.py
Restart=always
"""

# 已转正态（promoted）的绿 app unit —— **生产当前的真实态**，也是 up.sh 步骤 3.0
# 必须复位的那一侧：DRY_RUN=false（真写）+ DB 指回真库。若 up.sh 不复位，下一轮
# up 的 3.5 守卫会 exit 1（fail-closed 停摆），或绿以真实写能力跑副本库。
PROMOTED_GREEN_UNIT = f"""\
[Unit]
Description=DailyCheck green (candidate)
After=network.target

[Service]
Type=simple
WorkingDirectory={APP_DIR}
Environment=PYTHONUNBUFFERED=1
Environment=DRY_RUN=false
Environment=DAILYCHECK_DB_DIR={TARGET_DB}
ExecStart=/usr/bin/gunicorn -w 2 --threads 4 -b 127.0.0.1:8091 app:app
Restart=always

[Install]
WantedBy=multi-user.target
"""

PROMOTED_GREEN_MCP_UNIT = f"""\
[Service]
Type=simple
Environment=DAILYCHECK_DB_DIR="{TARGET_DB}"
ExecStart=/usr/bin/python3 mcp_server/server.py
Restart=always
"""

# 完全没有 DRY_RUN / DAILYCHECK_DB_DIR 行的 unit（up.sh 必须补出来：转正侧
# 「无该行则不加」，复位侧「无该行也必须保证在」，两边相反是刻意的）
BARE_GREEN_UNIT = """\
[Unit]
Description=DailyCheck green (candidate)

[Service]
Type=simple
ExecStart=/usr/bin/gunicorn -w 2 --threads 4 -b 127.0.0.1:8091 app:app
Restart=always

[Install]
WantedBy=multi-user.target
"""

# 单 upstream 布局 conf：green weight=100（= 绿在承流）
CONF_GREEN_SERVING = """\
upstream dailycheck {
    server 127.0.0.1:8090 down;
    server 127.0.0.1:8091 weight=100;
}
upstream dailycheck-mcp {
    server 127.0.0.1:5100 down;
    server 127.0.0.1:5101 weight=100;
}
server {
    listen 8080;
    location / { proxy_pass http://dailycheck; }
}
"""

# green down（= 绿不在承流）
CONF_GREEN_IDLE = """\
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
    location / { proxy_pass http://dailycheck; }
}
"""

# 解析不了：没有 dailycheck upstream 块
CONF_UNPARSEABLE = """\
server {
    listen 8080;
    location / { proxy_pass http://127.0.0.1:8090; }
}
"""


# ============================================================
# 基建：fake 命令 + 隔离环境
# ============================================================
_FAKE_SYSTEMCTL = """\
#!/usr/bin/env bash
printf 'systemctl %s\\n' "$*" >> "${FAKE_LOG}"
case "${1:-}" in
  cat) echo "Environment=DAILYCHECK_DB_DIR=${FAKE_UNIT_DB_DIR:-}" ;;
  show) echo "${FAKE_SYSTEMCTL_ENV:-DRY_RUN=false}" ;;
esac
exit "${FAKE_SYSTEMCTL_RC:-0}"
"""

_FAKE_CURL = """\
#!/usr/bin/env bash
printf 'curl %s\\n' "$*" >> "${FAKE_LOG}"
echo "${FAKE_CURL_CODE:-200}"
exit 0
"""

_FAKE_SS = """\
#!/usr/bin/env bash
echo "LISTEN 0 128 0.0.0.0:5101 0.0.0.0:*"
exit 0
"""

_FAKE_SWITCH = """\
#!/usr/bin/env bash
printf 'switch.sh %s\\n' "$*" >> "${FAKE_LOG}"
# 切流量那一刻的 unit 快照 —— 「复位是否真的发生在切流量之后」的唯一硬证据：
# 若复位先于切蓝，快照里就会看到 DRY_RUN=true（= 在役实例的配置被改）。
if [[ -n "${FAKE_SNAPSHOT_UNIT:-}" && -f "${FAKE_SNAPSHOT_UNIT}" ]]; then
  printf 'snapshot-at-switch:%s\\n' "$(tr '\\n' '|' < "${FAKE_SNAPSHOT_UNIT}")" >> "${FAKE_LOG}"
fi
exit "${FAKE_SWITCH_RC:-0}"
"""


def _write_exec(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture()
def fakebin(tmp_path: Path) -> Path:
    """fake systemctl / curl / ss / switch.sh（全部记录调用到 ${FAKE_LOG}）。"""
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    _write_exec(bindir / "systemctl", _FAKE_SYSTEMCTL)
    _write_exec(bindir / "curl", _FAKE_CURL)
    _write_exec(bindir / "ss", _FAKE_SS)
    _write_exec(bindir / "switch.sh", _FAKE_SWITCH)
    return bindir


def _base_env(tmp_path: Path, bindir: Path, **extra: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bindir}:{env.get('PATH', '')}",
            # 部署单飞：状态目录不存在 = 机制未激活 → 放行（与 dev/CI 一致）；
            # 已激活环境（VPS）必须显式覆盖，否则 live 脚本会 exit 10。
            "DAILYCHECK_DEPLOY_STATE_DIR": str(tmp_path / "deploy-state-not-activated"),
            "DAILYCHECK_BG_LOCK_HELD": "1",
            "SYSTEMCTL_BIN": str(bindir / "systemctl"),
            "CURL_BIN": str(bindir / "curl"),
            "SWITCH_SCRIPT": str(bindir / "switch.sh"),
            "FAKE_LOG": str(tmp_path / "calls.log"),
            "HEALTH_TRIES": "2",
            "HEALTH_INTERVAL": "0",
            "DAILYCHECK_APP_DIR": APP_DIR,
            "EVIDENCE_BASE": str(tmp_path / "evidence"),
        }
    )
    env.update(extra)
    return env


@pytest.fixture()
def promote_env(tmp_path: Path, fakebin: Path) -> dict[str, str]:
    """promote.sh 环境：unit/env 路径、备份目录全落 tmp。"""
    (tmp_path / "dailycheck-green.service").write_text(GREEN_UNIT, encoding="utf-8")
    (tmp_path / "dailycheck-green-mcp.service").write_text(GREEN_MCP_UNIT, encoding="utf-8")
    return _base_env(
        tmp_path,
        fakebin,
        GREEN_UNIT_PATH=str(tmp_path / "dailycheck-green.service"),
        GREEN_MCP_UNIT_PATH=str(tmp_path / "dailycheck-green-mcp.service"),
        # 默认指向不存在路径 → promote 跳过 env 文件（需要时用例自行创建）
        GREEN_ENV_FILE=str(tmp_path / "dailycheck-green.env"),
        GREEN_MCP_ENV_FILE=str(tmp_path / "dailycheck-green-mcp.env"),
        BACKUP_DIR=str(tmp_path / "backups"),
        NGINX_CONF=str(tmp_path / "nginx.conf"),
    )


def _run(script: Path, *args: str, env: dict[str, str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script), *args],
        capture_output=True, text=True, env=env, timeout=timeout,
    )


def _calls(env: dict[str, str]) -> list[str]:
    log = Path(env["FAKE_LOG"])
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _count(calls: list[str], needle: str) -> int:
    return sum(1 for c in calls if needle in c)


# ============================================================
# 静态不变量
# ============================================================
def test_promote_bash_syntax_and_help():
    assert subprocess.run(["bash", "-n", str(PROMOTE)]).returncode == 0
    r = subprocess.run(["bash", str(PROMOTE), "--help"], capture_output=True, text=True)
    assert r.returncode == 0
    assert "usage:" in r.stdout.lower()
    # 退出码契约必须写在脚本头（10 无锁 / 1 失败 / 64 参数错）
    head = PROMOTE.read_text(encoding="utf-8")[:4000]
    for code in ("10", "64"):
        assert f"\n#   {code} " in head or f"#   {code}" in head


def test_promote_rejects_unknown_arg(promote_env):
    r = _run(PROMOTE, "--nope", env=promote_env)
    assert r.returncode == 64


def test_up_has_live_traffic_guard_before_restart():
    """静态不变量：守卫解析必须在 restart 之前，且 conf 默认值正确。"""
    content = UP.read_text(encoding="utf-8")
    guard_at = content.index("_green_traffic_state")
    restart_at = content.index('_run systemctl restart "${GREEN_UNIT}"')
    assert guard_at < restart_at, "活流量守卫未排在 restart 之前"
    assert "/etc/nginx/sites-enabled/dailycheck" in content
    assert "switch.sh blue 100" in content or 'blue" 100' in content or '"blue" 100' in content


# ============================================================
# A. promote.sh
# ============================================================
def test_promote_dry_run_touches_nothing(promote_env, tmp_path):
    """--dry-run：不改文件、不建备份、不调 systemctl / curl。"""
    unit = tmp_path / "dailycheck-green.service"
    mcp = tmp_path / "dailycheck-green-mcp.service"
    before = (unit.read_text(), mcp.read_text())

    r = _run(PROMOTE, "--dry-run", env=promote_env)

    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "[DRY-RUN]" in r.stdout
    assert (unit.read_text(), mcp.read_text()) == before, "dry-run 改动了 unit 文件"
    assert _calls(promote_env) == [], "dry-run 调用了 systemctl / curl"
    assert not (tmp_path / "backups").exists(), "dry-run 建了备份目录"


def test_promote_rewrites_units_and_restarts(promote_env, tmp_path):
    """正常转正：DRY_RUN true→false、DB→真库（含引号形态）、daemon-reload+restart、健康通过。"""
    unit = tmp_path / "dailycheck-green.service"
    mcp = tmp_path / "dailycheck-green-mcp.service"

    r = _run(PROMOTE, env=promote_env)

    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    unit_text = unit.read_text(encoding="utf-8")
    mcp_text = mcp.read_text(encoding="utf-8")
    assert "DRY_RUN=false" in unit_text
    assert "DRY_RUN=true" not in unit_text
    assert f"DAILYCHECK_DB_DIR={TARGET_DB}" in unit_text
    assert OLD_DB not in unit_text
    # 带引号形态：既换掉旧值，也不留残片（`.../db"/var/...` 这种）
    assert mcp_text.count(TARGET_DB) == 1
    assert OLD_DB not in mcp_text
    assert '""' not in mcp_text

    calls = _calls(promote_env)
    assert _count(calls, "daemon-reload") == 1
    assert _count(calls, "restart dailycheck-green.service dailycheck-green-mcp.service") == 1
    assert _count(calls, "curl") >= 2, "未做健康探活"
    # 终局复核走 systemd 视角（掩盖 drop-in 覆盖的那一层取证）
    assert _count(calls, "show -p Environment --value dailycheck-green.service") == 1
    assert "systemd 侧复核通过" in r.stdout
    # 备份必须留痕（失败回滚靠它）
    assert list((tmp_path / "backups").glob("*.pre-promote.*.bak")), "未生成 unit 备份"


def test_promote_warns_when_systemd_view_still_dry_run(promote_env):
    """文件改完但 systemd 视角仍是 DRY_RUN=true（drop-in 覆盖）→ 明确告警、不静默通过。"""
    env = dict(promote_env, FAKE_SYSTEMCTL_ENV="DRY_RUN=true")

    r = _run(PROMOTE, env=env)

    out = r.stdout + r.stderr
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "仍报 DRY_RUN=true" in out, f"drop-in 覆盖未被发现：{out[-800:]!r}"
    assert "switch.sh" in out, "告警未指向真正的拦截面（switch.sh 守卫）"


def test_promote_is_idempotent(promote_env, tmp_path):
    """已转正：第二次 exit 0、打印 already promoted、且**不再** restart 在役实例。"""
    first = _run(PROMOTE, env=promote_env)
    assert first.returncode == 0, f"stdout={first.stdout!r} stderr={first.stderr!r}"
    restarts_after_first = _count(_calls(promote_env), "restart dailycheck-green")
    assert restarts_after_first == 1

    second = _run(PROMOTE, env=promote_env)

    assert second.returncode == 0, f"stdout={second.stdout!r}"
    assert "already promoted" in second.stdout
    assert _count(_calls(promote_env), "restart dailycheck-green") == restarts_after_first, (
        "幂等路径仍重启了绿（事故形态：重启在役实例）"
    )
    assert (tmp_path / "dailycheck-green.service").read_text(encoding="utf-8").count(
        "DRY_RUN=false"
    ) == 1


def test_promote_already_promoted_unit_is_noop(promote_env, tmp_path):
    """unit 本就没有 DRY_RUN 行、DB 已指真库 → already promoted（不新增 DRY_RUN 行）。"""
    unit = tmp_path / "dailycheck-green.service"
    unit.write_text(
        f"[Service]\nEnvironment=DAILYCHECK_DB_DIR={TARGET_DB}\n", encoding="utf-8"
    )
    mcp = tmp_path / "dailycheck-green-mcp.service"
    mcp.write_text(f"[Service]\nEnvironment=DAILYCHECK_DB_DIR={TARGET_DB}\n", encoding="utf-8")

    r = _run(PROMOTE, env=promote_env)

    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "already promoted" in r.stdout
    assert "DRY_RUN" not in unit.read_text(encoding="utf-8")
    assert _calls(promote_env) == []


def test_promote_requires_deploy_lock(promote_env, tmp_path):
    """无活跃部署锁 → exit 10 且不动文件；UNLOCKED=1 逃生舱 → 放行。"""
    state = tmp_path / "deploy-state"
    state.mkdir(exist_ok=True)  # 已激活（目录存在）但没人持锁
    env = dict(promote_env, DAILYCHECK_DEPLOY_STATE_DIR=str(state))
    unit = tmp_path / "dailycheck-green.service"
    before = unit.read_text(encoding="utf-8")

    denied = _run(PROMOTE, env=env)

    assert denied.returncode == 10, f"stdout={denied.stdout!r} stderr={denied.stderr!r}"
    assert unit.read_text(encoding="utf-8") == before, "被锁门禁拒绝却改了 unit"
    assert _calls(env) == []

    allowed = _run(PROMOTE, env=dict(env, DAILYCHECK_DEPLOY_UNLOCKED="1"))

    assert allowed.returncode == 0, f"stdout={allowed.stdout!r} stderr={allowed.stderr!r}"
    assert "DRY_RUN=false" in unit.read_text(encoding="utf-8")


def test_promote_restores_units_when_health_fails(promote_env, tmp_path):
    """健康失败 → exit 1 + unit 文件按备份恢复 + 再 daemon-reload（让 systemd 读回旧 unit）。"""
    unit = tmp_path / "dailycheck-green.service"
    mcp = tmp_path / "dailycheck-green-mcp.service"
    before = (unit.read_text(), mcp.read_text())
    env = dict(promote_env, FAKE_CURL_CODE="500", HEALTH_TRIES="2")

    r = _run(PROMOTE, env=env)

    assert r.returncode == 1, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert (unit.read_text(), mcp.read_text()) == before, "失败后 unit 未恢复"
    assert _count(_calls(env), "daemon-reload") == 2, "恢复后未 daemon-reload"
    assert "回滚" in (r.stdout + r.stderr)


def test_promote_restores_on_restart_failure(promote_env, tmp_path):
    """restart 失败（systemd 拒绝）同样回滚：宁可留在写隔离态，也不留半转正。"""
    unit = tmp_path / "dailycheck-green.service"
    before = unit.read_text(encoding="utf-8")
    env = dict(promote_env, FAKE_SYSTEMCTL_RC="1")

    r = _run(PROMOTE, env=env)

    assert r.returncode == 1, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert unit.read_text(encoding="utf-8") == before
    assert "DRY_RUN=true" in unit.read_text(encoding="utf-8")


def test_promote_rejects_env_override_of_dry_run(promote_env, tmp_path):
    """env 文件里还留着 DRY_RUN=true → 必须一起改（否则转正假成功）。"""
    env_file = tmp_path / "dailycheck-green.env"
    env_file.write_text("DRY_RUN=true\nDAILYCHECK_DB_DIR=/var/lib/dailycheck-green/db\n", encoding="utf-8")
    env = dict(promote_env, GREEN_ENV_FILE=str(env_file))

    r = _run(PROMOTE, env=env)

    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    text = env_file.read_text(encoding="utf-8")
    assert "DRY_RUN=true" not in text
    assert f"DAILYCHECK_DB_DIR={TARGET_DB}" in text


# ============================================================
# B. up.sh 活流量守卫
# ============================================================
def _git_repo(path: Path) -> str:
    """建单 commit 的 git 仓库（up.sh live 模式要求 HEAD == 传入 sha）。"""
    path.mkdir(parents=True, exist_ok=True)
    quiet = {"capture_output": True, "text": True, "timeout": 60}
    subprocess.run(["git", "init", "-q", str(path)], check=True, **quiet)
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.email=t@example.com", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "init"],
        check=True, **quiet,
    )
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
        check=True, **quiet,
    ).stdout.strip()


@pytest.fixture()
def up_env(tmp_path: Path, fakebin: Path) -> dict[str, str]:
    """up.sh live 环境：真 APP_DIR 用 tmp git 仓库替代，conf/DB/unit/备份全落 tmp。

    绿 unit 初始为**已转正态**（PROMOTED_*）—— 生产当前就是这个状态，也是
    up.sh 步骤 3.0（复位为静息态）唯一被要求处理的那一侧。
    """
    repo = tmp_path / "repo"
    sha = _git_repo(repo)
    greendb = tmp_path / "green-db"
    greendb.mkdir(exist_ok=True)
    conf = tmp_path / "nginx.conf"
    conf.write_text(CONF_GREEN_SERVING, encoding="utf-8")
    unit = tmp_path / "dailycheck-green.service"
    unit.write_text(PROMOTED_GREEN_UNIT, encoding="utf-8")
    mcp_unit = tmp_path / "dailycheck-green-mcp.service"
    mcp_unit.write_text(PROMOTED_GREEN_MCP_UNIT, encoding="utf-8")
    env = _base_env(
        tmp_path,
        fakebin,
        DAILYCHECK_APP_DIR=str(repo),
        NGINX_CONF=str(conf),
        GREEN_DB=str(greendb),
        FAKE_UNIT_DB_DIR=str(greendb),
        BLUE_HEALTH_URL="http://127.0.0.1:8090/health",
        # 静息态复位（步骤 3.0 / 3.5）：unit / env / 备份目录全部落 tmp
        GREEN_UNIT_PATH=str(unit),
        GREEN_MCP_UNIT_PATH=str(mcp_unit),
        GREEN_ENV_FILE=str(tmp_path / "dailycheck-green.env"),  # 不存在 → 跳过
        GREEN_MCP_ENV_FILE=str(tmp_path / "dailycheck-green-mcp.env"),
        BACKUP_DIR=str(tmp_path / "up-backups"),
        # fake systemctl show = 复位**成功后** systemd 该报的事实
        FAKE_SYSTEMCTL_ENV=f"DAILYCHECK_DB_DIR={greendb} DRY_RUN=true",
        # fake switch.sh 在切流量时快照 unit（顺序断言用）
        FAKE_SNAPSHOT_UNIT=str(unit),
    )
    env["_SHA"] = sha  # 仅测试侧用（不传给脚本也无害）
    return env


def _run_up(env: dict[str, str], sha: str) -> subprocess.CompletedProcess:
    clean = {k: v for k, v in env.items() if not k.startswith("_")}
    return _run(UP, "99", sha, "--skip-db-copy", env=clean, timeout=120)


def test_up_switches_to_blue_before_restarting_live_green(up_env, tmp_path):
    """绿在役（weight=100）→ 先 switch.sh blue 100，**再**restart 绿（顺序断言）。"""
    r = _run_up(up_env, up_env["_SHA"])

    assert r.returncode == 0, f"stdout={r.stdout[-1500:]!r} stderr={r.stderr[-800:]!r}"
    calls = _calls(up_env)
    switch_idx = [i for i, c in enumerate(calls) if c.startswith("switch.sh")]
    restart_idx = [i for i, c in enumerate(calls) if "restart dailycheck-green.service" in c]
    assert switch_idx, f"绿在役却未切蓝：{calls}"
    assert restart_idx, f"未重启绿：{calls}"
    assert calls[switch_idx[0]] == "switch.sh blue 100", calls[switch_idx[0]]
    assert switch_idx[0] < restart_idx[0], f"顺序反了（先重启了在役的绿）：{calls}"
    assert "green 在役" in r.stdout


def test_up_refuses_when_blue_unhealthy(up_env):
    """蓝不健康 → exit 1，且**绝不**重启在役的绿。"""
    env = dict(up_env, FAKE_CURL_CODE="502")

    r = _run_up(env, up_env["_SHA"])

    assert r.returncode == 1, f"stdout={r.stdout[-1500:]!r}"
    calls = _calls(env)
    assert not [c for c in calls if "restart" in c], f"蓝不健康却重启了绿：{calls}"
    assert not [c for c in calls if c.startswith("switch.sh")], "蓝不健康却仍切了流量"
    assert "不健康" in (r.stdout + r.stderr)


def test_up_refuses_when_conf_unparseable(up_env, tmp_path):
    """conf 解析不了 → 拒绝执行（不能靠猜测决定是否打断在役实例）。"""
    conf = tmp_path / "nginx.conf"
    conf.write_text(CONF_UNPARSEABLE, encoding="utf-8")

    r = _run_up(up_env, up_env["_SHA"])

    assert r.returncode == 1, f"stdout={r.stdout[-1500:]!r}"
    calls = _calls(up_env)
    assert not [c for c in calls if "restart" in c], f"conf 解析失败却重启了绿：{calls}"
    assert "无法判定" in (r.stdout + r.stderr)


def test_up_refuses_when_conf_missing(up_env, tmp_path):
    """读不到 conf 与解析失败同等对待（显式拒绝，不静默降级）。"""
    env = dict(up_env, NGINX_CONF=str(tmp_path / "no-such-nginx.conf"))

    r = _run_up(env, up_env["_SHA"])

    assert r.returncode == 1, f"stdout={r.stdout[-1500:]!r}"
    assert not [c for c in _calls(env) if "restart" in c]


def test_up_green_idle_keeps_old_behaviour(up_env, tmp_path):
    """绿不承流（down）→ 不切蓝、直接重启（加固前行为不变）。"""
    conf = tmp_path / "nginx.conf"
    conf.write_text(CONF_GREEN_IDLE, encoding="utf-8")
    env = dict(up_env, NGINX_CONF=str(conf))

    r = _run_up(env, up_env["_SHA"])

    assert r.returncode == 0, f"stdout={r.stdout[-1500:]!r} stderr={r.stderr[-800:]!r}"
    calls = _calls(env)
    assert not [c for c in calls if c.startswith("switch.sh")], "绿未承流却切了流量"
    assert [c for c in calls if "restart dailycheck-green.service" in c], f"未重启绿：{calls}"
    assert "未承流" in r.stdout


def test_up_switch_failure_blocks_restart(up_env, tmp_path):
    """switch.sh blue 100 失败 → exit 1（绿仍在承流，禁止重启）。"""
    env = dict(up_env, FAKE_SWITCH_RC="1")

    r = _run_up(env, up_env["_SHA"])

    assert r.returncode == 1, f"stdout={r.stdout[-1500:]!r}"
    calls = _calls(env)
    assert [c for c in calls if c.startswith("switch.sh")], "未尝试切蓝"
    assert not [c for c in calls if "restart" in c], f"切蓝失败却重启了绿：{calls}"


# ============================================================
# B2. up.sh 静息态复位（步骤 3.0，Issue #28 闭环）
# ============================================================
# 闭环本体：promote.sh 把绿 unit 改成「真写 + 真库」并重启，下一轮 up.sh 必须把
# 它**复位**成「写隔离 + 副本库」，否则 3.5 守卫 exit 1（停摆），或者更糟 ——
# 绿以真实写能力跑副本库（写进将被丢弃的库）。
def _line_index(lines: list[str], needle: str) -> int:
    return next(i for i, line in enumerate(lines) if line == needle)


def test_up_resets_promoted_unit_after_switch_before_restart(up_env, tmp_path):
    """已转正态 + 绿在役 → 切蓝 → **再**复位 unit → **再** restart（顺序硬断言）。"""
    unit = tmp_path / "dailycheck-green.service"
    mcp_unit = tmp_path / "dailycheck-green-mcp.service"
    greendb = up_env["GREEN_DB"]
    assert "DRY_RUN=false" in unit.read_text(encoding="utf-8"), "夹具不是已转正态"

    r = _run_up(up_env, up_env["_SHA"])

    assert r.returncode == 0, f"stdout={r.stdout[-2000:]!r} stderr={r.stderr[-800:]!r}"
    calls = _calls(up_env)
    switch_idx = [i for i, c in enumerate(calls) if c.startswith("switch.sh")]
    reload_idx = [i for i, c in enumerate(calls) if "daemon-reload" in c]
    restart_idx = [i for i, c in enumerate(calls) if "restart dailycheck-green.service" in c]
    assert switch_idx and reload_idx and restart_idx, f"缺少关键调用：{calls}"
    assert calls[switch_idx[0]] == "switch.sh blue 100"
    assert switch_idx[0] < reload_idx[0] < restart_idx[0], (
        f"顺序错（复位/重启早于切流量）：{calls}"
    )
    # 切流量那一刻 unit 仍是转正态 —— 复位确实发生在「流量已离开绿」之后
    snap = [c for c in calls if c.startswith("snapshot-at-switch:")]
    assert snap, "fake switch.sh 未留下切蓝时的 unit 快照"
    assert "DRY_RUN=false" in snap[0], f"切蓝时 unit 已被改写（复位早于切流量）：{snap[0][:200]}"

    text = unit.read_text(encoding="utf-8")
    assert "DRY_RUN=true" in text
    assert "DRY_RUN=false" not in text
    assert f"DAILYCHECK_DB_DIR={greendb}" in text
    assert TARGET_DB not in text, "unit 仍指向真库（隔离失效）"
    # MCP unit：DB 换成副本库，且**不带引号**残留（BSD sed 零长度命中陷阱回归）
    mcp_text = mcp_unit.read_text(encoding="utf-8")
    assert mcp_text.count(greendb) == 1
    assert TARGET_DB not in mcp_text
    assert '""' not in mcp_text
    # 备份留痕（失败可恢复）
    assert list((tmp_path / "up-backups").glob("*.pre-up.*.bak")), "未生成 unit 备份"


def test_up_inserts_missing_dry_run_and_db_into_service_section(up_env, tmp_path):
    """unit 里两行都没有 → 都插到 [Service] 段首（复位侧必须保证存在）。"""
    unit = tmp_path / "dailycheck-green.service"
    mcp_unit = tmp_path / "dailycheck-green-mcp.service"
    unit.write_text(BARE_GREEN_UNIT, encoding="utf-8")
    mcp_unit.write_text(BARE_GREEN_UNIT, encoding="utf-8")
    greendb = up_env["GREEN_DB"]

    r = _run_up(up_env, up_env["_SHA"])

    assert r.returncode == 0, f"stdout={r.stdout[-2000:]!r} stderr={r.stderr[-800:]!r}"
    lines = unit.read_text(encoding="utf-8").splitlines()
    svc = _line_index(lines, "[Service]")
    assert lines[svc + 1] == "Environment=DRY_RUN=true", lines[svc : svc + 3]
    assert lines[svc + 2] == f"Environment=DAILYCHECK_DB_DIR={greendb}", lines[svc : svc + 3]
    # MCP unit 只要 DB 行（不该凭空给 MCP 加 DRY_RUN）
    mcp_lines = mcp_unit.read_text(encoding="utf-8").splitlines()
    mcp_svc = _line_index(mcp_lines, "[Service]")
    assert mcp_lines[mcp_svc + 1] == f"Environment=DAILYCHECK_DB_DIR={greendb}", mcp_lines[:4]
    assert not [line for line in mcp_lines if "DRY_RUN" in line], "给 MCP unit 塞了 DRY_RUN"


def test_up_resting_state_is_idempotent(up_env, tmp_path):
    """已静息 → 文件内容与 mtime 不变、无 daemon-reload、无备份（幂等）。"""
    unit = tmp_path / "dailycheck-green.service"
    mcp_unit = tmp_path / "dailycheck-green-mcp.service"
    greendb = up_env["GREEN_DB"]
    resting = (
        f"[Service]\nType=simple\nEnvironment=DRY_RUN=true\n"
        f"Environment=DAILYCHECK_DB_DIR={greendb}\nRestart=always\n"
    )
    unit.write_text(resting, encoding="utf-8")
    mcp_unit.write_text(
        f'[Service]\nEnvironment=DAILYCHECK_DB_DIR="{greendb}"\n', encoding="utf-8"
    )
    before = (
        unit.read_text(encoding="utf-8"),
        mcp_unit.read_text(encoding="utf-8"),
        unit.stat().st_mtime_ns,
        mcp_unit.stat().st_mtime_ns,
    )

    r = _run_up(up_env, up_env["_SHA"])

    assert r.returncode == 0, f"stdout={r.stdout[-2000:]!r} stderr={r.stderr[-800:]!r}"
    assert "already resting" in r.stdout
    after = (
        unit.read_text(encoding="utf-8"),
        mcp_unit.read_text(encoding="utf-8"),
        unit.stat().st_mtime_ns,
        mcp_unit.stat().st_mtime_ns,
    )
    assert after == before, "幂等路径改动了 unit（内容或 mtime）"
    assert _count(_calls(up_env), "daemon-reload") == 0, "幂等路径仍 daemon-reload"
    assert not (tmp_path / "up-backups").exists(), "幂等路径建了备份"


def test_up_dry_run_does_not_touch_units(up_env, tmp_path):
    """--dry-run：不写 unit、不调 daemon-reload / restart（演练无副作用）。"""
    unit = tmp_path / "dailycheck-green.service"
    before = unit.read_text(encoding="utf-8")

    clean = {k: v for k, v in up_env.items() if not k.startswith("_")}
    r = _run(UP, "99", up_env["_SHA"], "--skip-db-copy", "--dry-run", env=clean, timeout=120)

    assert r.returncode == 0, f"stdout={r.stdout[-2000:]!r} stderr={r.stderr[-800:]!r}"
    assert "[DRY-RUN]" in r.stdout
    assert unit.read_text(encoding="utf-8") == before, "dry-run 改动了 unit"
    assert _calls(up_env) == [], f"dry-run 调用了真命令：{_calls(up_env)}"


def test_up_guard_fails_when_dropin_keeps_dry_run_false(up_env, tmp_path):
    """文件改成 DRY_RUN=true，但 systemd 视角仍 false（drop-in 覆盖）→ exit 1。

    宁可停摆也不能让绿以「真实写能力 + 副本库」跑起来：写会落进将被丢弃的库。
    """
    unit = tmp_path / "dailycheck-green.service"
    before = unit.read_text(encoding="utf-8")
    env = dict(up_env, FAKE_SYSTEMCTL_ENV="DRY_RUN=false")

    r = _run_up(env, up_env["_SHA"])

    out = r.stdout + r.stderr
    assert r.returncode == 1, f"stdout={r.stdout[-2000:]!r} stderr={r.stderr[-800:]!r}"
    assert "仍报 DRY_RUN=false" in out, f"未明确指出覆盖源：{out[-800:]!r}"
    calls = _calls(env)
    assert not [c for c in calls if "restart" in c], f"守卫不通过却重启了绿：{calls}"
    assert unit.read_text(encoding="utf-8") == before, "守卫不通过后 unit 未按备份回滚"


def test_up_does_not_rewrite_commented_out_lines(up_env, tmp_path):
    """整行注释不是生效行：`#Environment=DRY_RUN=false` 必须原样保留。"""
    unit = tmp_path / "dailycheck-green.service"
    greendb = up_env["GREEN_DB"]
    unit.write_text(
        "[Service]\n"
        "Environment=DRY_RUN=false\n"
        "#Environment=DRY_RUN=false\n"
        f"Environment=DAILYCHECK_DB_DIR={TARGET_DB}\n"
        f"#Environment=DAILYCHECK_DB_DIR={greendb}/old\n",
        encoding="utf-8",
    )

    r = _run_up(up_env, up_env["_SHA"])

    assert r.returncode == 0, f"stdout={r.stdout[-2000:]!r} stderr={r.stderr[-800:]!r}"
    text = unit.read_text(encoding="utf-8")
    assert "#Environment=DRY_RUN=false" in text, f"注释行被改写：{text!r}"
    assert f"#Environment=DAILYCHECK_DB_DIR={greendb}/old" in text, f"注释行被改写：{text!r}"
    assert "\nEnvironment=DRY_RUN=true\n" in text


def test_up_resets_env_file_that_overrides_dry_run(up_env, tmp_path):
    """EnvironmentFile 覆盖 unit Environment=：env 文件里的 DRY_RUN=false 必须一起改。"""
    env_file = tmp_path / "dailycheck-green.env"
    env_file.write_text(f"DRY_RUN=false\nDAILYCHECK_DB_DIR={TARGET_DB}\n", encoding="utf-8")
    greendb = up_env["GREEN_DB"]
    env = dict(up_env, GREEN_ENV_FILE=str(env_file))

    r = _run_up(env, up_env["_SHA"])

    assert r.returncode == 0, f"stdout={r.stdout[-2000:]!r} stderr={r.stderr[-800:]!r}"
    text = env_file.read_text(encoding="utf-8")
    assert "DRY_RUN=false" not in text, f"env 文件仍会覆盖成真写：{text!r}"
    assert "DRY_RUN=true" in text
    assert f"DAILYCHECK_DB_DIR={greendb}" in text


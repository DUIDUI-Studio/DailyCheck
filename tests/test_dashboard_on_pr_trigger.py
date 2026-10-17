"""dashboard 触发 listener 实装测试 — Issue #66

`deploy/scripts/dailycheck_dashboard_on_pr.py` 是 `scripts/prod_agent/inbox_watcher.py`
的孪生副本，但自引入（PR #54）起**没有任何测试** —— 这正是 #51 的三类同型缺陷
在该副本里原样存活至今的原因（单侧修复漂移）。本文件补齐该副本的回归网。

验证：
- 完整行触发 / 半行等待 / 非目标 repo 与非白名单 action 过滤 / 坏行 WARNING 一次
- 去重：同 delivery_id 只触发一次；重启不重复触发；停机补触发
- 失败退避：retry_base=0 立即重试 / 退避未到期跳过
- **Issue #66 本体**：失败 + inbox 无任何新行 → 退避到期后自动重试（端到端）
- **Issue #66 本体**：`_retry_delay` 对天文位数 attempts 不崩溃、不挂死
- **损坏 state 归一化（load() 边界，唯一口径）**：`next` / `n` 的 8 类损坏形态
  —— 非数值 / None / ±inf / NaN / 309 位整数（溢出）/ 负数 / 有限巨值（1e308 /
  1e18，Issue #51 第 5 轮审核 M-1 的「永久锁死」形态）/ 非 dict 值
- 损坏条目重试一轮后自愈重写成合法值（不永久损坏）
- prune 排除已 handled 的残留失败条目（否则 retry_due() 恒 True → 主循环空跑）
- 主循环 gating 覆盖：无变化且无退避到期 → 不空跑

不测（部署态人工验证，见 docs/ARCHITECTURE.md §3.6 / deploy/systemd）：
- 真 systemd unit 拉起 / 真 webhook 投递 / 真 dashboard render 落库
"""
from __future__ import annotations

import json
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

# deploy/scripts 是平铺脚本目录（无 __init__.py），加进 import path 后直接 import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "deploy" / "scripts"))

import dailycheck_dashboard_on_pr as trigger  # noqa: E402

REPO = "ericmr1981/DailyCheck"
SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "deploy"
    / "scripts"
    / "dailycheck_dashboard_on_pr.py"
)


# ============================================================
# 辅助
# ============================================================
def _event(**overrides) -> dict:
    rec = {
        "delivery_id": f"d-{uuid.uuid4().hex[:12]}",
        "event": "pull_request",
        "action": "opened",
        "received_at_ms": int(time.time() * 1000),
        "pr_number": 66,
        "pr_title": "test",
        "pr_url": "https://example.invalid/pr",
        "pr_state": "open",
        "pr_draft": False,
        "pr_head_sha": "deadbeef" * 5,
        "pr_base_ref": "main",
        "pr_author": "dev-agent",
        "repo_full_name": REPO,
        "repo_visibility": "private",
    }
    rec.update(overrides)
    return rec


def _append_lines(inbox: Path, records: list[dict]) -> None:
    with inbox.open("a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _read_marker(marker: Path) -> list[str]:
    if not marker.exists():
        return []
    return [ln for ln in marker.read_text(encoding="utf-8").split("\n") if ln.strip()]


def _touch_cmd(marker: Path) -> list[str]:
    """触发命令：把一个标记行 append 到 marker。

    与 inbox_watcher 不同，dashboard trigger **不向子进程喂 stdin**（事件以
    `--trigger-kind` / `--trigger-event-id` 参数传入），故这里只看「有没有跑过」。
    """
    return ["/bin/sh", "-c", f"printf 'ok\\n' >> {shlex.quote(str(marker))}"]


def _cfg(tmp_path: Path, trigger_cmd: list[str], **kw) -> trigger.TriggerConfig:
    defaults = {
        "inbox": tmp_path / "inbox.jsonl",
        "state_file": tmp_path / "state.json",
        "trigger_argv": trigger_cmd,
        "repo": REPO,
        "actions": frozenset({"opened", "synchronize", "reopened"}),
        "trigger_timeout": 10.0,
        "retry_base": 0.0,  # 默认测试无退避（立即重试）——退避专项用例单独设
        "retry_max": 120.0,
    }
    defaults.update(kw)
    return trigger.TriggerConfig(**defaults)


# ============================================================
# 触发 / 过滤 / 去重
# ============================================================
def test_triggers_on_new_complete_line(tmp_path: Path):
    marker = tmp_path / "marker.txt"
    cfg = _cfg(tmp_path, _touch_cmd(marker))
    rec = _event()
    _append_lines(cfg.inbox, [rec])

    state = trigger.TriggerState()
    assert trigger.process_once(cfg, state) == 0
    assert len(_read_marker(marker)) == 1
    assert state.handled_ids == {rec["delivery_id"]}


def test_half_line_waits_until_completed(tmp_path: Path):
    marker = tmp_path / "marker.txt"
    cfg = _cfg(tmp_path, _touch_cmd(marker))
    line = json.dumps(_event()) + "\n"
    cfg.inbox.write_text(line[: len(line) // 2], encoding="utf-8")
    state = trigger.TriggerState()
    trigger.process_once(cfg, state)
    assert _read_marker(marker) == []
    # 补全
    with cfg.inbox.open("a", encoding="utf-8") as f:
        f.write(line[len(line) // 2 :])
    trigger.process_once(cfg, state)
    assert len(_read_marker(marker)) == 1


def test_filter_other_repo_not_triggered(tmp_path: Path, capsys):
    marker = tmp_path / "marker.txt"
    cfg = _cfg(tmp_path, _touch_cmd(marker))
    rec = _event(repo_full_name="someone/other-repo")
    _append_lines(cfg.inbox, [rec])
    state = trigger.TriggerState()
    trigger.process_once(cfg, state)
    assert _read_marker(marker) == []
    assert rec["delivery_id"] in state.handled_ids  # 记入 handled，不再重复关注
    capsys.readouterr()
    trigger.process_once(cfg, state)
    assert "ignored" not in capsys.readouterr().out  # 不重复刷日志


def test_filter_other_action_not_triggered(tmp_path: Path):
    marker = tmp_path / "marker.txt"
    cfg = _cfg(tmp_path, _touch_cmd(marker))
    rec = _event(action="closed")  # 纵深防御：receiver 不该写，但写了也不触发
    _append_lines(cfg.inbox, [rec])
    state = trigger.TriggerState()
    trigger.process_once(cfg, state)
    assert _read_marker(marker) == []
    assert rec["delivery_id"] in state.handled_ids


def test_bad_lines_warn_once(tmp_path: Path, capsys):
    marker = tmp_path / "marker.txt"
    cfg = _cfg(tmp_path, _touch_cmd(marker))
    cfg.inbox.write_text("not-json\n[1,2,3]\n", encoding="utf-8")
    state = trigger.TriggerState()
    trigger.process_once(cfg, state)
    capsys.readouterr()
    trigger.process_once(cfg, state)
    assert capsys.readouterr().out.count("WARNING") == 0  # 指纹去重：只 WARNING 一次


def test_dedup_same_delivery_id(tmp_path: Path):
    marker = tmp_path / "marker.txt"
    cfg = _cfg(tmp_path, _touch_cmd(marker))
    rec = _event()
    _append_lines(cfg.inbox, [rec, rec])
    trigger.process_once(cfg, trigger.TriggerState())
    assert len(_read_marker(marker)) == 1


def test_restart_does_not_retrigger(tmp_path: Path):
    marker = tmp_path / "marker.txt"
    cfg = _cfg(tmp_path, _touch_cmd(marker))
    _append_lines(cfg.inbox, [_event()])
    state = trigger.TriggerState()
    trigger.process_once(cfg, state)
    assert len(_read_marker(marker)) == 1
    reloaded = trigger.TriggerState.load(cfg.state_file)  # 模拟重启
    assert trigger.process_once(cfg, reloaded) == 0
    assert len(_read_marker(marker)) == 1


def test_backfill_after_downtime(tmp_path: Path):
    """停机期间写入的条目在重启后补触发（conservative 宁多勿漏）。"""
    marker = tmp_path / "marker.txt"
    cfg = _cfg(tmp_path, _touch_cmd(marker))
    first = _event()
    _append_lines(cfg.inbox, [first])
    state = trigger.TriggerState()
    trigger.process_once(cfg, state)
    state.save(cfg.state_file)  # 停机前落盘
    _append_lines(cfg.inbox, [_event(pr_number=661)])  # 停机期间新增
    reloaded = trigger.TriggerState.load(cfg.state_file)
    trigger.process_once(cfg, reloaded)
    assert len(_read_marker(marker)) == 2


# ============================================================
# 失败退避 / retry_due（Issue #66 本体）
# ============================================================
def test_failure_retries_without_backoff(tmp_path: Path):
    """retry_base=0 → 立即重试（测试常用口径）；失败计数递增。"""
    attempts_log = tmp_path / "attempts.txt"
    cmd = ["/bin/sh", "-c", f"printf 'x\\n' >> {shlex.quote(str(attempts_log))}; exit 1"]
    cfg = _cfg(tmp_path, cmd, retry_base=0.0, retry_max=120.0)
    rec = _event()
    _append_lines(cfg.inbox, [rec])
    state = trigger.TriggerState()
    assert trigger.process_once(cfg, state) == 1
    assert trigger.process_once(cfg, state) == 1
    assert state.failures[rec["delivery_id"]]["n"] == 2
    assert rec["delivery_id"] not in state.handled_ids


def test_failure_backoff_skips_until_next(tmp_path: Path):
    marker = tmp_path / "marker.txt"
    cfg = _cfg(tmp_path, ["/bin/sh", "-c", "exit 1"], retry_base=3600.0)
    rec = _event()
    _append_lines(cfg.inbox, [rec])
    state = trigger.TriggerState()
    assert trigger.process_once(cfg, state) == 1
    # 退避 3600s → 下一轮跳过，不再 +1
    assert trigger.process_once(cfg, state) == 0
    assert state.failures[rec["delivery_id"]]["n"] == 1
    assert _read_marker(marker) == []


def test_retry_due_condition(tmp_path: Path):
    """Issue #66：退避到期的判定（主循环解除签名 gating 的依据）。"""
    now = 1000.0
    # 无失败条目 → 不驱动额外轮次（保住「无变化不空转」的原设计）
    assert trigger.retry_due(trigger.TriggerState(), now) is False
    # 退避未到期 → False
    s = trigger.TriggerState()
    s.failures["d-future"] = {"n": 1, "next": now + 60}
    assert trigger.retry_due(s, now) is False
    # 退避到期（含恰好等于 now 的边界）→ True
    s.failures["d-due"] = {"n": 2, "next": now}
    assert trigger.retry_due(s, now) is True
    s.failures["d-due"]["next"] = now - 1
    assert trigger.retry_due(s, now) is True
    # 多条里只要有一条到期 → True
    s.failures["d-later"] = {"n": 1, "next": now + 3600}
    assert trigger.retry_due(s, now) is True
    # 状态损坏（缺 key / 非数值 / 非 dict / 有限巨值）→ 保守视作到期，不崩不锁死
    for bad in (
        {"n": 1},
        {"n": 1, "next": "not-a-number"},
        "garbage",
        {"n": 1, "next": 1e308},
    ):
        s = trigger.TriggerState()
        s.failures["d-bad"] = bad  # type: ignore[assignment]
        assert trigger.retry_due(s, now) is True, f"未按已到期处理：{bad!r}"


def test_failure_retries_after_backoff_with_no_new_inbox_lines(tmp_path: Path):
    """Issue #66 回归（核心）：触发失败 + inbox **无任何新行** → 退避到期必须自动重试。

    修复前主循环只在文件签名变化时 process_once，而重试不写 inbox（签名不变）
    → 退避条目永不重试，本用例会一直等到 deadline 后失败。
    """
    inbox = tmp_path / "inbox.jsonl"
    inbox.write_text("", encoding="utf-8")
    marker = tmp_path / "marker.txt"
    counter = tmp_path / "attempts.txt"
    # 第 1 次触发 exit 1；第 2 次起成功（写 marker）
    body = (
        f"n=$(cat {shlex.quote(str(counter))} 2>/dev/null || echo 0); "
        f"n=$((n+1)); echo $n > {shlex.quote(str(counter))}; "
        f'if [ "$n" -lt 2 ]; then exit 1; fi; '
        f"printf 'ok\\n' >> {shlex.quote(str(marker))}"
    )
    rec = _event()
    proc = subprocess.Popen(
        [
            sys.executable,
            str(SCRIPT),
            "--inbox",
            str(inbox),
            "--state-file",
            str(tmp_path / "state.json"),
            "--trigger-cmd",
            f"/bin/sh -c {shlex.quote(body)}",
            "--poll-interval",
            "0.05",
            "--retry-base",
            "0.3",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        time.sleep(0.5)  # 首扫（空 inbox）
        t0 = time.monotonic()
        with inbox.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        deadline = t0 + 5.0
        while time.monotonic() < deadline and not _read_marker(marker):
            time.sleep(0.05)
        elapsed = time.monotonic() - t0
        # 关键：这 5 秒内 inbox 未再追加任何行，重试只能由 retry_due() 驱动
        assert len(inbox.read_text(encoding="utf-8").splitlines()) == 1
        assert _read_marker(marker), "退避后的重试未发生（Issue #66 回归）"
        assert elapsed >= 0.25, f"未走退避（elapsed={elapsed:.2f}s）"
        assert counter.read_text(encoding="utf-8").strip() == "2"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_retry_delay_survives_huge_attempts(tmp_path: Path):
    """Issue #66 复现项：异常大的 attempts 不得崩溃或挂死 —— 一律封顶。

    旧实现 `base * 2 ** (attempts - 1)` 对 `attempts=10**18` 实测挂死 >8s、
    `int(1e300)` 挂死 >5 分钟（Issue #66 有界子进程强杀记录）。
    """
    cfg = _cfg(tmp_path, ["/bin/sh", "-c", "true"], retry_base=2.0, retry_max=120.0)
    assert trigger._retry_delay(cfg, 1) == 2.0
    assert trigger._retry_delay(cfg, 3) == 8.0  # 与 base*2^(n-1) 等价
    assert trigger._retry_delay(cfg, 7) == 120.0  # 已封顶
    assert trigger._retry_delay(cfg, 1000) == 120.0
    started = time.monotonic()
    assert trigger._retry_delay(cfg, 10**6) == 120.0  # 旧式在此抛 OverflowError
    assert trigger._retry_delay(cfg, 10**18) == 120.0  # 旧式在此挂死 >8s
    assert trigger._retry_delay(cfg, int(1e300)) == 120.0  # 旧式在此挂死 >5min
    assert time.monotonic() - started < 1.0, "退避计算不得退化成长时间挂死"
    # retry_base=0（测试常用）仍返回 0 → 立即重试
    zero = _cfg(tmp_path, ["/bin/sh", "-c", "true"], retry_base=0.0)
    assert trigger._retry_delay(zero, 5) == 0.0


def test_cli_once_reports_failure_exit_code(tmp_path: Path):
    inbox = tmp_path / "inbox.jsonl"
    _append_lines(inbox, [_event()])
    ok = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--inbox",
            str(inbox),
            "--state-file",
            str(tmp_path / "state.json"),
            "--trigger-cmd",
            "/bin/sh -c 'exit 0'",
            "--once",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert ok.returncode == 0, ok.stdout + ok.stderr
    bad = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--inbox",
            str(inbox),
            "--state-file",
            str(tmp_path / "state2.json"),
            "--trigger-cmd",
            "/bin/sh -c 'exit 1'",
            "--once",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert bad.returncode == 1, bad.stdout + bad.stderr


# ============================================================
# 损坏状态：load() 边界归一化（唯一口径）+ 纵深防御
# ============================================================
@pytest.mark.parametrize(
    "bad",
    [
        {"n": "not-a-number", "next": "not-a-number"},
        {"n": "bad", "next": None},
        {"n": 0, "next": float("nan")},  # JSON 非标但 json.loads 默认接受
        {"n": 1, "next": float("inf")},  # 非有限值（+inf）同样须视作到期
        {"n": 1, "next": float("-inf")},
        {"n": 1, "next": 10**309},  # 溢出形态：float() 抛 OverflowError
        {"n": float("inf"), "next": 0},
        {"n": -5, "next": 0.0},  # 负次数：不得出现负次退避
        {"n": 1, "next": 1e308},  # 有限巨值（#51 第 5 轮 M-1「永久锁死」形态）
        {"n": 1, "next": 1e18},
        "garbage",  # 非 dict（load() 会丢弃，内存态仍须不崩）
    ],
)
def test_corrupted_failure_entry_self_heals_without_crash(tmp_path: Path, bad: object):
    """损坏 `next` / `n`（但 JSON 合法）→ 不崩 + 一轮自愈。

    修复前 `process_once` 里 `float(fail.get("next", 0))` 无兜底 → ValueError 直穿
    main()（只捕 KeyboardInterrupt）→ systemd `Restart=always` 崩溃循环。
    """
    marker = tmp_path / "marker.txt"
    cfg = _cfg(tmp_path, _touch_cmd(marker))
    rec = _event()
    _append_lines(cfg.inbox, [rec])
    state = trigger.TriggerState()
    state.failures[rec["delivery_id"]] = bad  # type: ignore[assignment]

    assert trigger.retry_due(state) is True
    assert trigger.process_once(cfg, state) == 0  # 修复前此处抛异常
    # 自愈：触发成功 → handled + failures 清空
    assert rec["delivery_id"] in state.handled_ids
    assert rec["delivery_id"] not in state.failures
    assert len(_read_marker(marker)) == 1


@pytest.mark.parametrize("bad_n", ["bad", -5, None, float("inf"), float("-inf"), float("nan")])
def test_corrupted_failure_entry_rewritten_on_repeat_failure(tmp_path: Path, bad_n: object):
    """损坏条目 + 触发仍失败 → next / n 被重写成合法值（不永久损坏）。"""
    cfg = _cfg(tmp_path, ["/bin/sh", "-c", "exit 1"], retry_base=3600.0)
    rec = _event()
    _append_lines(cfg.inbox, [rec])
    state = trigger.TriggerState()
    state.failures[rec["delivery_id"]] = {"n": bad_n, "next": None}

    assert trigger.process_once(cfg, state) == 1
    entry = state.failures[rec["delivery_id"]]
    assert entry["n"] == 1, f"损坏计数未归零重算：{entry!r}"
    assert isinstance(entry["next"], float)
    assert trigger.retry_due(state, now=time.time()) is False  # 回到正常退避语义


def _write_state(path: Path, failures: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "handled_ids": [],
                "bad_hashes": [],
                "failures": failures,
                "updated_at_ms": int(time.time() * 1000),
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_load_normalizes_corrupt_failure_entries(tmp_path: Path):
    """load() 边界是唯一归一化点：8 类损坏形态全部被钳制为合法值。

    这是 #51 连续 4 轮审核 FAIL 的根治姿势 —— 把归一化**上提**到状态入口一次性
    完成，而不是在每个使用点逐形态补 except（后者会被审核者逐轮找出新形态）。
    """
    now = time.time()
    state_file = tmp_path / "state.json"
    _write_state(
        state_file,
        {
            "d-str": {"n": 1, "next": "not-a-number"},
            "d-none": {"n": 1, "next": None},
            "d-nan": {"n": 1, "next": float("nan")},
            "d-inf": {"n": 1, "next": float("inf")},
            "d-neg": {"n": -5, "next": -1},
            "d-overflow": {"n": 1, "next": 10**309},
            "d-huge": {"n": 1, "next": 1e308},
            "d-n-inf": {"n": float("inf"), "next": 0},
            "d-notdict": "garbage",
            "d-legit": {"n": 3, "next": now + 30},
        },
    )
    st = trigger.TriggerState.load(state_file)

    # 逐条断言归一结果（非 dict 值丢弃；除 d-legit 外 next 一律归 0.0 = 已到期，
    # 而**仍然合法**的 n 必须原样保留 —— 归一化只钳制损坏字段，不得误伤合法字段）
    assert st.failures == {
        "d-str": {"n": 1, "next": 0.0},
        "d-none": {"n": 1, "next": 0.0},
        "d-nan": {"n": 1, "next": 0.0},
        "d-inf": {"n": 1, "next": 0.0},
        "d-neg": {"n": 0, "next": 0.0},  # 负次数归零、负时刻 → 已到期
        "d-overflow": {"n": 1, "next": 0.0},  # 309 位整数：float() 抛 OverflowError
        "d-huge": {"n": 1, "next": 0.0},  # 有限巨值：M-1「永久锁死」形态
        "d-n-inf": {"n": 0, "next": 0.0},  # int(inf) 抛 OverflowError
        "d-legit": {"n": 3, "next": now + 30},  # 合法条目原样保留（不得误伤）
    }
    # 归一化后形态自洽：除 d-legit 外全部到期
    assert trigger.retry_due(st, now) is True


def test_huge_next_no_longer_locks_entry(tmp_path: Path):
    """#51 第 5 轮 M-1 反回归：`next=1e308` 的条目不再被永久锁死。

    修复前该形态被判为「有限值 = 合法」→ `retry_due()` 恒 False、条目永不重试、
    无任何 WARNING（静默丢弃），与 `10**309` / `inf` 反而自愈的口径相矛盾。
    """
    marker = tmp_path / "marker.txt"
    cfg = _cfg(tmp_path, _touch_cmd(marker))
    rec = _event()
    _append_lines(cfg.inbox, [rec])
    _write_state(cfg.state_file, {rec["delivery_id"]: {"n": 1, "next": 1e308}})

    state = trigger.TriggerState.load(cfg.state_file)
    assert state.failures[rec["delivery_id"]]["next"] == 0.0
    assert trigger.retry_due(state) is True
    assert trigger.process_once(cfg, state) == 0
    assert len(_read_marker(marker)) == 1, "巨值 next 仍把条目锁死（M-1 回归）"
    assert rec["delivery_id"] in state.handled_ids


def test_sane_next_boundaries(tmp_path: Path):
    """`_sane_next` 的边界语义钉死（下界含 0、上界含 now + sane_ahead，越界/非有限 → None）。

    直接钉住「合法 = 有限、非负、≤ now + sane_ahead」这条唯一口径，防止日后有人把
    范围检查改写成 NaN 会漏过的形式（如先把 NaN 混进 min/max）。
    """
    now = 1000.0
    ahead = trigger.DEFAULT_SANE_AHEAD
    assert ahead == trigger.DEFAULT_RETRY_MAX + trigger.RETRY_NEXT_CLOCK_SLACK_S
    bound = now + ahead
    assert trigger._sane_next(0, now) == 0.0  # 下界含 0（已到期）
    assert trigger._sane_next(now - 1, now) == now - 1  # 过期时刻合法（= 已到期）
    assert trigger._sane_next(now + 60, now) == now + 60
    assert trigger._sane_next(bound, now) == bound  # 上界含等号
    for bad in (None, "x", float("nan"), float("inf"), float("-inf"), 10**309, bound + 1, -1):
        assert trigger._sane_next(bad, now) is None, f"未判损坏：{bad!r}"
    # 上界可由配置推出（sane_ahead_for），不再是一个「7 天」式的宽常量
    assert trigger.sane_ahead_for(120.0) == 420.0
    assert trigger.sane_ahead_for(86400.0) == 86700.0
    assert trigger.sane_ahead_for(0.0) == 420.0  # <=0 时退化为默认封顶


def test_load_rejects_next_beyond_configured_cap(tmp_path: Path, capsys):
    """Issue #51 上游审核 M-1 的**残余带**也要关掉：`next ∈ (now+retry_max, now+7d]`。

    「有限值一律采信」的口径矛盾不能只从「永久锁死」缩到「≤7 天静默锁死」：
    程序自身只可能写出 `next ≤ now + retry_max`，故比 `now + sane_ahead` 更晚的
    `next` 一律判损坏（→ 立即可重试 + WARNING）。
    """
    now = time.time()
    state_file = tmp_path / "state.json"
    _write_state(
        state_file,
        {
            "d-lock-6d": {"n": 1, "next": now + 6 * 24 * 3600},  # 旧 7 天口径下被采信
            "d-lock-3h": {"n": 1, "next": now + 3 * 3600},  # 旧 7 天口径下被采信
            "d-legit": {"n": 2, "next": now + 90},  # 合法：≤ now + (120 + 300)
        },
    )
    st = trigger.TriggerState.load(state_file, sane_ahead=trigger.sane_ahead_for(120.0))
    assert st.failures["d-lock-6d"]["next"] == 0.0
    assert st.failures["d-lock-3h"]["next"] == 0.0
    assert st.failures["d-legit"]["next"] == now + 90  # 合法条目不得误伤
    out = capsys.readouterr().out
    # 必须是 WARNING 级（不是 info 级留痕）：损坏状态属「必须留痕事件」
    assert out.count("[dashboard_trigger] WARNING: state 损坏") == 2
    assert "d-legit" not in out
    assert trigger.retry_due(st) is True


def test_load_warns_on_normalization_and_silent_on_legit(tmp_path: Path, capsys):
    """归一化必须留痕（M-1 ①：只修「永久锁死」不修「无 WARNING」= 静默丢弃缩量保留）。

    断言按**形态计数**（不是「出现即可」）：把任一条留痕降级为 info 都必须被抓 ——
    「有一条就算过」会让只改一处 `_warn` 的变异漏过。
    """
    state_file = tmp_path / "state.json"
    now = time.time()
    _write_state(
        state_file,
        {
            "d-huge": {"n": 1, "next": 1e308},  # 钳制形态
            "d-notdict": "garbage",  # 丢弃形态
            "d-ok": {"n": 3, "next": now + 30},  # 合法：不得打扰
        },
    )
    st = trigger.TriggerState.load(state_file)
    out = capsys.readouterr().out
    warn_lines = [ln for ln in out.splitlines() if ln.startswith("[dashboard_trigger]")]
    assert len(warn_lines) == 2, f"留痕条数不符（应恰为 2 条钳制/丢弃）：\n{out}"
    assert all("WARNING: state 损坏" in ln for ln in warn_lines), out
    assert "d-huge" in out and "d-notdict" in out and "d-ok" not in out
    assert st.failures["d-ok"] == {"n": 3, "next": now + 30}


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("failures", [{"n": 1, "next": 0.0}]),
        ("failures", "oops"),
        ("handled_ids", "d1"),
        ("handled_ids", {"d1": True}),
        ("bad_hashes", {"h": 1}),
    ],
)
def test_load_warns_on_container_level_damage(tmp_path: Path, field: str, bad_value: object, capsys):
    """容器级损坏（`failures`/`handled_ids`/`bad_hashes` 形状不对）也必须留痕（第 2 轮 M-1）。

    静默降级有真实副作用：`handled_ids` 形状损坏 → 去重集无声清空 → 同一 delivery
    被**重复触发**（违反「每条 delivery_id 至多触发一次」）；`failures` 形状损坏 →
    整张退避表无声丢失（全部立即重试）。运维却看不到任何痕。
    """
    state_file = tmp_path / "state.json"
    payload: dict = {"failures": {}, "handled_ids": ["h1"], "bad_hashes": ["b1"]}
    payload[field] = bad_value
    state_file.write_text(json.dumps(payload), encoding="utf-8")
    st = trigger.TriggerState.load(state_file)
    out = capsys.readouterr().out
    assert "[dashboard_trigger] WARNING: state 损坏" in out, f"容器级损坏静默：{field}={bad_value!r}"
    assert field in out, out
    # 其余字段仍按合法路径载入（损坏不扩散）
    assert st.failures == {} or field == "failures"


@pytest.mark.parametrize(
    "payload",
    [
        {"failures": {}, "handled_ids": ["d1", 7, None]},
        {"failures": {}, "bad_hashes": ["h1", {"x": 1}]},
    ],
)
def test_load_warns_on_non_string_items_in_sets(tmp_path: Path, payload: dict, capsys):
    """集合内混入非字符串元素 → 丢弃并留痕（`handled_ids` 静默丢弃 = 重复触发）。"""
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(payload), encoding="utf-8")
    st = trigger.TriggerState.load(state_file)
    out = capsys.readouterr().out
    assert "[dashboard_trigger] WARNING: state 损坏" in out
    assert "非字符串元素" in out
    assert st.handled_ids == {"d1"} or st.bad_hashes == {"h1"}


@pytest.mark.parametrize("entry", ['{"n":1}', '"garbage"', "5", "[1]"])
def test_load_survives_surrogate_key(tmp_path: Path, capsys, entry: str):
    """第 2 轮 H-1：状态里**键**是孤立代理字符时，归一化留痕本身不得崩溃。

    `{"failures": {"\\ud800": <任意>}}` 是合法 JSON（对象键必为字符串，转义允许
    孤立代理）。修复前留痕把该键原样插进 f-string → `print` 抛 UnicodeEncodeError
    直穿 `main()` → `Restart=always` 永久崩溃循环（每次启动都崩在同一状态文件）。
    条目值必须参数化覆盖**两条留痕分支**（归一化分支 / 非 dict 丢弃分支）—— 只测一条
    会让另一条的未转义键逃过回归网。
    ⚠️ 本用例**必须**用 `capsys`：pytest 默认 fd 捕获用 `errors="replace"`，会把
    `UnicodeEncodeError` 吞掉从而假绿。
    """
    state_file = tmp_path / "state.json"
    state_file.write_text(
        '{"failures":{"\\ud800":' + entry + "}}", encoding="utf-8"
    )
    st = trigger.TriggerState.load(state_file)  # 修复前此处抛 UnicodeEncodeError
    out = capsys.readouterr().out  # capsys 走真实 strict 编码
    assert "[dashboard_trigger] WARNING: state 损坏" in out
    # dict 条目 → 归一化后保留；非 dict 条目 → 丢弃（两种分支的留痕都不得崩）
    assert list(st.failures) == (["\ud800"] if entry == '{"n":1}' else [])


@pytest.mark.parametrize(
    ("label", "line"),
    [
        ("4301 位整数", '{"delivery_id":"d1","n":' + "9" * 4301 + "}"),
        ("超深嵌套", "[" * 200000 + "]" * 200000),
        ("非法 JSON", "{not json"),
    ],
)
def test_parse_record_never_raises(tmp_path: Path, label: str, line: str):
    """第 2 轮 M-3：inbox 行解析的异常网必须覆盖 ValueError / RecursionError。

    只捕 `JSONDecodeError` 时，这样一行会让 `process_once` 抛出 → `main()` 退出 →
    `Restart=always` 反复崩在同一行（该行永远进不了 `bad_hashes`，故永不跳过）。
    """
    assert trigger._parse_record(line) is None
    marker = tmp_path / "marker.txt"
    cfg = _cfg(tmp_path, _touch_cmd(marker))
    cfg.inbox.write_text(line + "\n", encoding="utf-8")
    state = trigger.TriggerState()
    assert trigger.process_once(cfg, state) == 0  # 修复前此处抛异常
    assert _read_marker(marker) == []
    assert len(state.bad_hashes) == 1  # 计入坏行指纹 → 不会重复告警


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        # json.loads 对 >4300 位整数字面量抛 ValueError（不是 JSONDecodeError）
        ("4301 位整数", b'{"failures":{"d":{"n":' + b"9" * 4301 + b',"next":0}}}'),
        # 非 UTF-8 字节 → UnicodeDecodeError（ValueError 子类，不是 OSError）
        ("非 UTF-8 字节", b'{"failures":{},"x":"\xff\xfe\x80"}'),
        # 超深嵌套 → RecursionError（既非 OSError 也非 ValueError）
        ("超深嵌套", b"[" * 200000 + b"]" * 200000),
    ],
)
def test_load_survives_json_layer_damage(tmp_path: Path, label: str, payload: bytes, capsys):
    """Issue #51/#66 上游审核 H-1：损坏发生在 `json.loads` / `read_text` 层时也不得崩溃。

    修复前异常网只捕 `(OSError, json.JSONDecodeError)` → 上述三种形态直穿 `main()`
    → systemd `Restart=always` 静默重启循环。本用例只断言「不抛异常 + 状态自洽」
    （不锁死具体解析结果，故对不同 CPython 的 `int_max_str_digits` 设置也稳定）。
    """
    state_file = tmp_path / "state.json"
    state_file.write_bytes(payload)
    st = trigger.TriggerState.load(state_file)  # 修复前此处抛异常
    assert isinstance(st.handled_ids, set)
    for entry in st.failures.values():
        assert 0 <= entry["n"] <= trigger.MAX_SANE_ATTEMPTS
        assert isinstance(entry["next"], float)
    assert trigger.retry_due(st) in (True, False)  # 可调用、不崩
    assert "WARNING" in capsys.readouterr().out or not st.failures, label


@pytest.mark.parametrize(
    ("raw_n", "expect"),
    [(0, 0), (1, 1), (1023, 1023), (1024, 1024), (1025, 0), (10**6, 0), (-1, 0)],
)
def test_normalize_failure_attempts_boundary(tmp_path: Path, raw_n: int, expect: int):
    """`n` 的上下界钉死（L-4）：`[0, MAX_SANE_ATTEMPTS]` 保留，越界归 0。"""
    now = time.time()
    entry = {"n": raw_n, "next": now + 10}
    fixed = trigger._normalize_failure(entry, now, trigger.DEFAULT_SANE_AHEAD)
    assert fixed["n"] == expect, f"n={raw_n} → {fixed!r}"
    assert trigger._attempts({"n": raw_n, "next": 0}) == expect
    assert fixed["next"] == now + 10  # 合法 next 不得被连带改动


def test_build_config_rejects_bad_retry_params(tmp_path: Path):
    """L-4：`--retry-max <= 0` / 非有限值、`--retry-base < 0` / 非有限值启动期即拒。

    `nan <= 0` 为 False、`inf > 0` 为 True —— 只看 `> 0` 会放过二者，封顶失效后可
    写出 `{"next": Infinity}`（非标 JSON）并让 `sane_ahead` 失去依据。
    """
    base_argv = [
        "--inbox",
        str(tmp_path / "inbox.jsonl"),
        "--trigger-cmd",
        "/bin/sh -c 'exit 0'",
    ]
    for extra in (
        ["--retry-max", "0"],
        ["--retry-max", "-5"],
        ["--retry-max", "nan"],
        ["--retry-max", "inf"],
        ["--retry-base", "-1"],
        ["--retry-base", "nan"],
        ["--retry-base", "inf"],
    ):
        with pytest.raises(SystemExit):
            trigger.build_config(trigger.parse_args(base_argv + extra))
    cfg = trigger.build_config(trigger.parse_args(base_argv + ["--retry-base", "0"]))
    assert cfg.retry_base == 0.0  # base=0（立即重试）仍是合法调试口径


def test_delivery_id_lone_surrogate_is_bad_line(tmp_path: Path, capsys):
    """L-6：`"\\ud800"` 这类孤立代理字符的 delivery_id 不得让 listener 崩溃。

    它既不能 `print`（UnicodeEncodeError）也不能作为 argv 传给子进程 —— 修复前
    `_log` 处抛 UnicodeEncodeError 直穿 main()。现按坏行处理（指纹去重 + `!r` 转义打印）。
    """
    marker = tmp_path / "marker.txt"
    cfg = _cfg(tmp_path, _touch_cmd(marker))
    cfg.inbox.write_text(
        '{"delivery_id":"\\ud800","action":"opened","repo_full_name":"' + REPO + '"}\n',
        encoding="utf-8",
    )
    state = trigger.TriggerState()
    assert trigger.process_once(cfg, state) == 0  # 修复前此处抛 UnicodeEncodeError
    assert _read_marker(marker) == []
    assert state.handled_ids == set()
    assert len(state.bad_hashes) == 1
    assert "malformed inbox line" in capsys.readouterr().out


def test_main_warns_when_inbox_missing(tmp_path: Path):
    """L-3：inbox 不存在 + 有到期条目 = 零进度零日志的静默态 → 启动时至少留一条痕。"""
    state_file = tmp_path / "state.json"
    _write_state(state_file, {"d-x": {"n": 1, "next": 0.0}})
    proc = subprocess.Popen(
        [
            sys.executable,
            str(SCRIPT),
            "--inbox",
            str(tmp_path / "nope.jsonl"),  # 不存在
            "--state-file",
            str(state_file),
            "--trigger-cmd",
            "/bin/sh -c 'exit 0'",
            "--poll-interval",
            "0.05",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        time.sleep(0.4)
    finally:
        proc.terminate()
        try:
            out = proc.stdout.read() if proc.stdout else ""
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            out = proc.stdout.read() if proc.stdout else ""
    assert out.count("不存在") == 1, f"提示缺失或刷屏（应为 1 条）：\n{out}"


def test_prune_failures_for_handled_ids(tmp_path: Path):
    """handled ∩ failures 的残留条目被 prune 掉（否则 retry_due() 恒 True → 空跑）。"""
    marker = tmp_path / "marker.txt"
    cfg = _cfg(tmp_path, _touch_cmd(marker))
    rec = _event()
    _append_lines(cfg.inbox, [rec])
    state = trigger.TriggerState()
    state.handled_ids.add(rec["delivery_id"])  # 模拟矛盾状态（手改 / 版本错位）
    state.failures[rec["delivery_id"]] = {"n": 1, "next": 0.0}

    assert trigger.retry_due(state) is True
    trigger.process_once(cfg, state)  # 被 handled 短路，不触发
    assert _read_marker(marker) == []
    assert state.failures == {}
    assert trigger.retry_due(state) is False


def test_state_roundtrip_is_stable(tmp_path: Path):
    """save → load 幂等：合法状态不被归一化改写。"""
    cfg = _cfg(tmp_path, ["/bin/sh", "-c", "true"])
    now = time.time()
    state = trigger.TriggerState(
        handled_ids={"d1"}, failures={"d2": {"n": 2, "next": now + 60}}, bad_hashes={"h"}
    )
    state.save(cfg.state_file)
    again = trigger.TriggerState.load(cfg.state_file)
    assert again.handled_ids == {"d1"}
    assert again.bad_hashes == {"h"}
    assert again.failures == {"d2": {"n": 2, "next": now + 60}}
    assert trigger.retry_due(again, now) is False


def test_bad_state_file_recovers(tmp_path: Path):
    cfg = _cfg(tmp_path, ["/bin/sh", "-c", "true"])
    cfg.state_file.write_text("{not json", encoding="utf-8")
    assert trigger.TriggerState.load(cfg.state_file).handled_ids == set()
    cfg.state_file.write_text('["not", "an", "object"]', encoding="utf-8")
    assert trigger.TriggerState.load(cfg.state_file).failures == {}


def test_main_loop_does_not_reprocess_when_idle(tmp_path: Path):
    """主循环 gating 覆盖：无变化、无退避到期 → 不空跑。

    可观测面：把 inbox 造成「stat 成功但读失败」的目录 → 每次 process_once 都会
    打一条 `cannot read inbox` WARNING。正确 gating 下首轮之后不再有 WARNING；
    若 gating 被改成无条件处理，0.9s 内会刷出十余条。
    """
    inbox = tmp_path / "inbox.jsonl"
    inbox.mkdir()  # 目录：stat OK（签名稳定），read_bytes 抛 IsADirectoryError
    proc = subprocess.Popen(
        [
            sys.executable,
            str(SCRIPT),
            "--inbox",
            str(inbox),
            "--state-file",
            str(tmp_path / "state.json"),
            "--trigger-cmd",
            "/bin/sh -c 'exit 0'",
            "--poll-interval",
            "0.05",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        time.sleep(0.9)
    finally:
        proc.terminate()
        try:
            out = proc.stdout.read() if proc.stdout else ""
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            out = proc.stdout.read() if proc.stdout else ""
    warns = out.count("cannot read inbox")
    assert warns <= 2, f"主循环空跑：0.9s 内 {warns} 条 WARNING（gating 失效）\n{out}"
    assert warns >= 1, f"首轮未处理（用例失效）\n{out}"


def test_triggered_log_survives_surrogate_values(tmp_path: Path, capsys):
    """H-1-r3 反回归：`triggered:` 日志必须转义 `delivery_id` 之外的 inbox 字段。

    `json.loads` 能把 inbox 行里的 `"\\ud800"` 解成孤立代理字符；修复前
    `pr_number` / `pr_head_sha` 被**原样**插进 f-string → `print` 抛
    UnicodeEncodeError → `process_once` 未返回、`state.save()` 永不执行 →
    systemd `Restart=always` 每次崩在同一行。规则 7 的绝对口径必须真正覆盖
    全部 inbox 字段，而不是只在 `delivery_id` 上做守卫。
    ⚠️ 必须用 `capsys`：pytest 默认 fd 捕获 `errors="replace"` 会吞掉该异常。
    """
    marker = tmp_path / "marker.txt"
    cfg = _cfg(tmp_path, _touch_cmd(marker))
    cfg.inbox.write_text(
        '{"delivery_id":"d-surr","action":"opened","repo_full_name":"'
        + REPO
        + '","pr_number":"\\ud800","pr_head_sha":"\\ud800"}\n',
        encoding="utf-8",
    )
    state = trigger.TriggerState()
    assert trigger.process_once(cfg, state) == 0  # 修复前此处抛 UnicodeEncodeError
    out = capsys.readouterr().out
    assert "triggered:" in out
    assert "d-surr" in state.handled_ids
    assert len(_read_marker(marker)) == 1

"""inbox_watcher / inbox_rotate 实装测试 — Issue #47（Prod Agent 订阅 inbox.jsonl）

验证（watcher）：
- 完整行触发 / 半行等待 / U+2028 不误切行
- 事件 JSON 原样透传（stdin）
- 机械过滤：非目标 repo / 非白名单 action → 不触发（且不重复刷日志）
- 坏行（非法 JSON / 无 delivery_id）→ WARNING 一次（指纹去重）
- 去重：同 delivery_id 多行只触发一次
- 重启不重复：state 持久化后重新加载不重触发
- 停机补触发：state 已有 id 时，文件新 id 仍触发
- 失败重试 + 退避（retry-base=0 立即重试 / retry-base 大值跳过）
- rotate 换 inode 后旧行不重触发、新行触发
- handled_ids 随文件内容 prune
- draft 事件照触发（业务语义交 Prod Agent，本脚本只做机械过滤）
- 真实子进程端到端：append → ≤5s（断言 <5s 上限，实测 ~0.2s）触发

验证（rotate）：
- 超龄行归档 / 新鲜行保留 / 坏行保留 / 边界（恰好 N 天保留）
- archive append 语义（不覆盖既有内容）
- 幂等（第二次无操作）
- inbox 不存在 → 无操作
- 未终结尾片段原样回填（tail_restored）
- 残留 rotating 自愈（上次崩溃恢复）
- 文件不存在 + stale rotating 时依然自愈

不测（部署态人工验证，见 docs/2026-10-15-webhook-setup.md §3.8）：
- 真 systemd unit 拉起 / 真 Hermes webhook test 投递
- rotate 与 receiver「旧 fd 写入」的纳秒级竞态窗口（见 inbox_rotate.py docstring
  「不丢行语义」：窗口内丢失至多一条通知，30 分钟兜底轮询覆盖）
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

# 把 scripts/prod_agent 加进 import path（与 test_webhook_receiver.py 同规）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "prod_agent"))

import inbox_rotate  # noqa: E402
import inbox_watcher  # noqa: E402

REPO = "ericmr1981/DailyCheck"


# ============================================================
# 辅助
# ============================================================
def _event(**overrides) -> dict:
    rec = {
        "delivery_id": f"d-{uuid.uuid4().hex[:12]}",
        "event": "pull_request",
        "action": "opened",
        "received_at_ms": int(time.time() * 1000),
        "pr_number": 47,
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


def _cfg(tmp_path: Path, trigger_cmd: list[str], **kw) -> inbox_watcher.WatcherConfig:
    defaults = {
        "inbox": tmp_path / "inbox.jsonl",
        "state_file": tmp_path / "state.json",
        "trigger_argv": trigger_cmd,
        "repo": REPO,
        "actions": frozenset({"opened", "synchronize", "reopened"}),
        "trigger_timeout": 10.0,
        "retry_base": 0.0,  # 默认测试无退避（立即重试）——退避专项用例单独设
        "retry_max": 60.0,
    }
    defaults.update(kw)
    return inbox_watcher.WatcherConfig(**defaults)


def _cat_cmd(marker: Path) -> list[str]:
    """触发命令：把 stdin 原样 + 换行 append 到 marker（一行一条）。"""
    return ["/bin/sh", "-c", f"cat >> {marker!s}; printf '\\n' >> {marker!s}"]


# ============================================================
# watcher — 行读取
# ============================================================
def test_read_complete_lines_skips_half_line(tmp_path: Path):
    inbox = tmp_path / "inbox.jsonl"
    inbox.write_bytes(b'{"delivery_id":"a"}\n{"delivery_id":"b"')
    lines = inbox_watcher.read_complete_lines(inbox)
    assert lines == ['{"delivery_id":"a"}']


def test_read_complete_lines_empty_and_missing(tmp_path: Path):
    assert inbox_watcher.read_complete_lines(tmp_path / "nope.jsonl") is None
    empty = tmp_path / "inbox.jsonl"
    empty.write_bytes(b"")
    assert inbox_watcher.read_complete_lines(empty) == []
    # 只有半行（尚无完整行）
    half = tmp_path / "half.jsonl"
    half.write_bytes(b'{"delivery_id":"x"')
    assert inbox_watcher.read_complete_lines(half) == []


def test_read_complete_lines_u2028_not_split(tmp_path: Path):
    """U+2028 是合法 JSON 字符串内容，绝不能当行分隔（splitlines 会误切）。"""
    inbox = tmp_path / "inbox.jsonl"
    rec = _event(pr_title="line\u2028separator")
    inbox.write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
    lines = inbox_watcher.read_complete_lines(inbox)
    assert len(lines) == 1
    assert json.loads(lines[0])["pr_title"] == "line\u2028separator"


# ============================================================
# watcher — 触发 / 过滤 / 去重
# ============================================================
def test_triggers_on_new_complete_line_and_passthrough(tmp_path: Path):
    marker = tmp_path / "marker.txt"
    cfg = _cfg(tmp_path, _cat_cmd(marker))
    rec = _event()
    _append_lines(cfg.inbox, [rec])

    state = inbox_watcher.WatcherState()
    failed = inbox_watcher.process_once(cfg, state)
    assert failed == 0
    got = _read_marker(marker)
    assert len(got) == 1
    # 原样透传：stdin 收到的 JSON 与 inbox 行等价
    assert json.loads(got[0])["delivery_id"] == rec["delivery_id"]
    assert state.handled_ids == {rec["delivery_id"]}


def test_half_line_waits_until_completed(tmp_path: Path):
    marker = tmp_path / "marker.txt"
    cfg = _cfg(tmp_path, _cat_cmd(marker))
    rec = _event()
    line = json.dumps(rec) + "\n"
    # 先写半行
    cfg.inbox.write_text(line[: len(line) // 2], encoding="utf-8")
    state = inbox_watcher.WatcherState()
    inbox_watcher.process_once(cfg, state)
    assert _read_marker(marker) == []
    # 补全
    with cfg.inbox.open("a", encoding="utf-8") as f:
        f.write(line[len(line) // 2 :])
    inbox_watcher.process_once(cfg, state)
    assert len(_read_marker(marker)) == 1


def test_filter_other_repo_not_triggered(tmp_path: Path, capsys):
    marker = tmp_path / "marker.txt"
    cfg = _cfg(tmp_path, _cat_cmd(marker))
    rec = _event(repo_full_name="someone/other-repo")
    _append_lines(cfg.inbox, [rec])
    state = inbox_watcher.WatcherState()
    inbox_watcher.process_once(cfg, state)
    assert _read_marker(marker) == []
    assert rec["delivery_id"] in state.handled_ids  # 记入 handled，不再重复关注
    # 第二轮不再重复打 ignore 日志
    capsys.readouterr()
    inbox_watcher.process_once(cfg, state)
    assert "ignored" not in capsys.readouterr().out


def test_filter_other_action_not_triggered(tmp_path: Path):
    marker = tmp_path / "marker.txt"
    cfg = _cfg(tmp_path, _cat_cmd(marker))
    rec = _event(action="closed")  # receiver 不会写 closed，纵深防御
    _append_lines(cfg.inbox, [rec])
    state = inbox_watcher.WatcherState()
    inbox_watcher.process_once(cfg, state)
    assert _read_marker(marker) == []
    assert rec["delivery_id"] in state.handled_ids


def test_draft_still_triggers(tmp_path: Path):
    """draft 属业务语义（交 Prod Agent 判定），watcher 只做机械过滤。"""
    marker = tmp_path / "marker.txt"
    cfg = _cfg(tmp_path, _cat_cmd(marker))
    _append_lines(cfg.inbox, [_event(pr_draft=True)])
    inbox_watcher.process_once(cfg, inbox_watcher.WatcherState())
    assert len(_read_marker(marker)) == 1


def test_bad_lines_warn_once(tmp_path: Path, capsys):
    marker = tmp_path / "marker.txt"
    cfg = _cfg(tmp_path, _cat_cmd(marker))
    good = _event()
    cfg.inbox.write_text(
        "not-json\n[1,2,3]\n" + json.dumps({"no_delivery_id": True}) + "\n",
        encoding="utf-8",
    )
    _append_lines(cfg.inbox, [good])
    state = inbox_watcher.WatcherState()
    inbox_watcher.process_once(cfg, state)
    out1 = capsys.readouterr().out
    assert out1.count("WARNING") == 3  # 三条坏行各一次
    assert len(_read_marker(marker)) == 1  # 好行照触发

    # 第二轮：坏行不再重复 WARNING，好行不再重复触发
    inbox_watcher.process_once(cfg, state)
    out2 = capsys.readouterr().out
    assert "WARNING" not in out2
    assert len(_read_marker(marker)) == 1


def test_dedup_same_delivery_id(tmp_path: Path):
    marker = tmp_path / "marker.txt"
    cfg = _cfg(tmp_path, _cat_cmd(marker))
    rec = _event()
    _append_lines(cfg.inbox, [rec, dict(rec, action="synchronize")])  # 同 id 两行
    state = inbox_watcher.WatcherState()
    inbox_watcher.process_once(cfg, state)
    assert len(_read_marker(marker)) == 1


def test_restart_does_not_retrigger(tmp_path: Path):
    marker = tmp_path / "marker.txt"
    cfg = _cfg(tmp_path, _cat_cmd(marker))
    _append_lines(cfg.inbox, [_event()])
    inbox_watcher.process_once(cfg, inbox_watcher.WatcherState())
    assert len(_read_marker(marker)) == 1
    # 模拟重启：从磁盘重新加载 state
    state2 = inbox_watcher.WatcherState.load(cfg.state_file)
    inbox_watcher.process_once(cfg, state2)
    assert len(_read_marker(marker)) == 1


def test_backfill_after_downtime(tmp_path: Path):
    """停机期间 receiver 写入的新条目，重启后补触发；已触发的不重复。"""
    marker = tmp_path / "marker.txt"
    cfg = _cfg(tmp_path, _cat_cmd(marker))
    rec1 = _event()
    _append_lines(cfg.inbox, [rec1])
    inbox_watcher.process_once(cfg, inbox_watcher.WatcherState())
    # 「停机」：新条目写入，state 未更新
    rec2 = _event()
    _append_lines(cfg.inbox, [rec2])
    # 重启
    state2 = inbox_watcher.WatcherState.load(cfg.state_file)
    inbox_watcher.process_once(cfg, state2)
    got = [json.loads(x)["delivery_id"] for x in _read_marker(marker)]
    assert got == [rec1["delivery_id"], rec2["delivery_id"]]


def test_failure_retries_without_backoff(tmp_path: Path):
    """触发失败 → 不记 handled → 下一轮重试（retry-base=0 立即）。"""
    marker = tmp_path / "marker.txt"
    flag = tmp_path / "second-time-ok"
    # 第一次 exit 1（且不写 marker）；之后成功（写 marker）
    cmd = [
        "/bin/sh",
        "-c",
        f"if [ -f {flag!s} ]; then cat >> {marker!s}; printf '\\n' >> {marker!s}; "
        f"else touch {flag!s}; exit 1; fi",
    ]
    cfg = _cfg(tmp_path, cmd, retry_base=0.0)
    rec = _event()
    _append_lines(cfg.inbox, [rec])
    state = inbox_watcher.WatcherState()

    failed = inbox_watcher.process_once(cfg, state)
    assert failed == 1
    assert rec["delivery_id"] not in state.handled_ids
    assert state.failures[rec["delivery_id"]]["n"] == 1

    # 第二轮（同一 state）：重试成功
    failed = inbox_watcher.process_once(cfg, state)
    assert failed == 0
    assert rec["delivery_id"] in state.handled_ids
    assert rec["delivery_id"] not in state.failures
    assert len(_read_marker(marker)) == 1


def test_failure_backoff_skips_until_next(tmp_path: Path):
    """退避期内不重试（retry-base 大值 → 第二轮直接跳过）。"""
    cfg = _cfg(
        tmp_path,
        ["/bin/sh", "-c", "exit 1"],
        retry_base=3600.0,
    )
    _append_lines(cfg.inbox, [_event()])
    state = inbox_watcher.WatcherState()
    inbox_watcher.process_once(cfg, state)
    assert len(state.failures) == 1
    # 第二轮：仍在退避期 → 不再尝试（marker 无变化可证：命令恒败，若重试会累加 n）
    inbox_watcher.process_once(cfg, state)
    assert next(iter(state.failures.values()))["n"] == 1


def test_prune_handled_after_file_shrink(tmp_path: Path):
    marker = tmp_path / "marker.txt"
    cfg = _cfg(tmp_path, _cat_cmd(marker))
    rec1 = _event()
    _append_lines(cfg.inbox, [rec1])
    state = inbox_watcher.WatcherState()
    inbox_watcher.process_once(cfg, state)
    assert rec1["delivery_id"] in state.handled_ids
    # 文件被重写为只剩新行（模拟旧行被归档清除）
    rec2 = _event()
    cfg.inbox.write_text(json.dumps(rec2) + "\n", encoding="utf-8")
    inbox_watcher.process_once(cfg, state)
    assert state.handled_ids == {rec2["delivery_id"]}


def test_inode_change_does_not_retrigger(tmp_path: Path):
    """rotate 换 inode（rename+重建）：旧行不重复触发，新行触发。"""
    marker = tmp_path / "marker.txt"
    cfg = _cfg(tmp_path, _cat_cmd(marker))
    rec1 = _event()
    _append_lines(cfg.inbox, [rec1])
    state = inbox_watcher.WatcherState()
    inbox_watcher.process_once(cfg, state)

    # 真实 rotate（新鲜行保留 → 回填到新 inode 的文件）
    inbox_rotate.rotate_inbox(cfg.inbox, tmp_path / "archive.jsonl", settle_interval=0)
    # 新 inode + 旧行仍在
    assert cfg.inbox.exists()
    rec2 = _event()
    _append_lines(cfg.inbox, [rec2])
    inbox_watcher.process_once(cfg, state)
    got = [json.loads(x)["delivery_id"] for x in _read_marker(marker)]
    assert got == [rec1["delivery_id"], rec2["delivery_id"]]  # 各一次，无重复


def test_bad_state_file_recovers(tmp_path: Path, capsys):
    state_path = tmp_path / "state.json"
    state_path.write_text("{ not json", encoding="utf-8")
    state = inbox_watcher.WatcherState.load(state_path)
    assert state.handled_ids == set()
    assert "WARNING" in capsys.readouterr().out


def test_state_atomic_save_roundtrip(tmp_path: Path):
    state_path = tmp_path / "state.json"
    state = inbox_watcher.WatcherState(
        handled_ids={"a", "b"}, failures={"c": {"n": 2, "next": 123.0}}, bad_hashes={"h1"}
    )
    state.save(state_path)
    loaded = inbox_watcher.WatcherState.load(state_path)
    assert loaded.handled_ids == {"a", "b"}
    assert loaded.failures == {"c": {"n": 2, "next": 123.0}}
    assert loaded.bad_hashes == {"h1"}
    # 原子性辅助验证：无 .tmp 残留
    assert not (tmp_path / "state.json.tmp").exists()


# ============================================================
# watcher — 端到端（真实子进程，验收「≤5s 触发」）
# ============================================================
def test_e2e_subprocess_trigger_within_5s(tmp_path: Path):
    inbox = tmp_path / "inbox.jsonl"
    inbox.write_text("", encoding="utf-8")
    marker = tmp_path / "marker.txt"
    script = Path(__file__).resolve().parent.parent / "scripts" / "prod_agent" / "inbox_watcher.py"
    proc = subprocess.Popen(
        [
            sys.executable,
            str(script),
            "--inbox",
            str(inbox),
            "--state-file",
            str(tmp_path / "state.json"),
            "--trigger-cmd",
            f"/bin/sh -c 'cat >> {marker!s}'",
            "--poll-interval",
            "0.1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        time.sleep(0.5)  # 让它完成首扫
        rec = _event()
        t0 = time.monotonic()
        with inbox.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        deadline = t0 + 5.0
        while time.monotonic() < deadline and not _read_marker(marker):
            time.sleep(0.05)
        elapsed = time.monotonic() - t0
        assert _read_marker(marker), "触发未在 5s 内到达"
        assert elapsed < 5.0
        assert json.loads(_read_marker(marker)[0])["delivery_id"] == rec["delivery_id"]
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


# ============================================================
# rotate
# ============================================================
def _rotate_cfg(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "inbox.jsonl", tmp_path / "archive.jsonl"


def test_rotate_archives_old_keeps_fresh(tmp_path: Path):
    inbox, archive = _rotate_cfg(tmp_path)
    now_ms = time.time() * 1000
    old = _event(received_at_ms=int(now_ms - 8 * 86_400_000))
    fresh = _event(received_at_ms=int(now_ms - 1 * 86_400_000))
    _append_lines(inbox, [old, fresh])

    stats = inbox_rotate.rotate_inbox(inbox, archive, settle_interval=0)
    assert stats["archived"] == 1
    assert stats["kept"] == 1
    kept_lines = inbox.read_text(encoding="utf-8").splitlines()
    archived_lines = archive.read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["delivery_id"] for x in kept_lines] == [fresh["delivery_id"]]
    assert [json.loads(x)["delivery_id"] for x in archived_lines] == [old["delivery_id"]]


def test_rotate_cutoff_boundary(tmp_path: Path):
    """恰好 N 天 → 保留；N 天 + 1s → 归档。"""
    inbox, archive = _rotate_cfg(tmp_path)
    now_ms = 1_700_000_000_000
    exactly = _event(received_at_ms=now_ms - 7 * 86_400_000)
    just_over = _event(received_at_ms=now_ms - 7 * 86_400_000 - 1000)
    _append_lines(inbox, [exactly, just_over])

    inbox_rotate.rotate_inbox(inbox, archive, max_age_days=7, now_ms=now_ms, settle_interval=0)
    kept = [json.loads(x)["delivery_id"] for x in inbox.read_text(encoding="utf-8").splitlines()]
    archived = [
        json.loads(x)["delivery_id"] for x in archive.read_text(encoding="utf-8").splitlines()
    ]
    assert kept == [exactly["delivery_id"]]
    assert archived == [just_over["delivery_id"]]


def test_rotate_bad_lines_kept(tmp_path: Path):
    """非法 JSON / 无 received_at_ms / 非数值 ts → 一律保留（保守）。"""
    inbox, archive = _rotate_cfg(tmp_path)
    inbox.write_text(
        "not-json\n"
        + json.dumps({"delivery_id": "no-ts"})
        + "\n"
        + json.dumps({"delivery_id": "bool-ts", "received_at_ms": True})
        + "\n",
        encoding="utf-8",
    )
    stats = inbox_rotate.rotate_inbox(inbox, archive, settle_interval=0)
    assert stats["archived"] == 0
    assert stats["kept"] == 3
    assert len(inbox.read_text(encoding="utf-8").splitlines()) == 3


def test_rotate_tail_fragment_preserved(tmp_path: Path):
    """未终结尾片段原样回填，字节保真。"""
    inbox, archive = _rotate_cfg(tmp_path)
    fresh = _event()
    _append_lines(inbox, [fresh])
    with inbox.open("a", encoding="utf-8") as f:
        f.write('{"delivery_id":"half')  # 无换行的半行

    stats = inbox_rotate.rotate_inbox(inbox, archive, settle_interval=0)
    assert stats["tail_restored"] == 1
    content = inbox.read_text(encoding="utf-8")
    assert fresh["delivery_id"] in content
    assert content.endswith('{"delivery_id":"half')


def test_rotate_archive_appends_not_overwrites(tmp_path: Path):
    inbox, archive = _rotate_cfg(tmp_path)
    archive.write_text('{"delivery_id":"pre-existing"}\n', encoding="utf-8")
    old = _event(received_at_ms=0)  # epoch = 必然超龄
    _append_lines(inbox, [old])

    inbox_rotate.rotate_inbox(inbox, archive, settle_interval=0)
    lines = archive.read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["delivery_id"] for x in lines] == ["pre-existing", old["delivery_id"]]


def test_rotate_idempotent(tmp_path: Path):
    inbox, archive = _rotate_cfg(tmp_path)
    now_ms = time.time() * 1000
    _append_lines(
        inbox,
        [
            _event(received_at_ms=int(now_ms - 8 * 86_400_000)),
            _event(received_at_ms=int(now_ms)),
        ],
    )
    inbox_rotate.rotate_inbox(inbox, archive, settle_interval=0)
    before = inbox.read_text(encoding="utf-8")
    stats2 = inbox_rotate.rotate_inbox(inbox, archive, settle_interval=0)
    assert stats2["archived"] == 0
    assert stats2["kept"] == 1  # 内容重写一遍（幂等语义 = 结果不变）
    assert inbox.read_text(encoding="utf-8") == before


def test_rotate_missing_inbox_noop(tmp_path: Path):
    inbox, archive = _rotate_cfg(tmp_path)
    stats = inbox_rotate.rotate_inbox(inbox, archive, settle_interval=0)
    assert stats.get("nothing_to_do") is True
    assert not inbox.exists()
    assert not archive.exists()


def test_rotate_recovers_stale_rotating(tmp_path: Path):
    """上次崩溃残留的 rotating：本次运行先自愈处理（含归档判定）。"""
    inbox, archive = _rotate_cfg(tmp_path)
    now_ms = time.time() * 1000
    stale = tmp_path / "inbox.jsonl.rotating"
    old = _event(received_at_ms=int(now_ms - 9 * 86_400_000))
    fresh = _event(received_at_ms=int(now_ms))
    _append_lines(stale, [old, fresh])

    stats = inbox_rotate.rotate_inbox(inbox, archive, settle_interval=0)
    assert stats["recovered"] is True
    assert stats["archived"] == 1
    assert [json.loads(x)["delivery_id"] for x in archive.read_text().splitlines()] == [
        old["delivery_id"]
    ]
    # 自愈回填的 fresh 行 + 空 inbox：fresh 在（顺序不保证）
    assert fresh["delivery_id"] in inbox.read_text(encoding="utf-8")
    assert not stale.exists()


def test_rotate_stale_rotating_without_inbox(tmp_path: Path):
    """inbox 不存在但有 stale rotating：照常自愈（不 early-return 漏掉它）。"""
    inbox, archive = _rotate_cfg(tmp_path)
    stale = tmp_path / "inbox.jsonl.rotating"
    rec = _event(received_at_ms=int(time.time() * 1000))
    _append_lines(stale, [rec])
    stats = inbox_rotate.rotate_inbox(inbox, archive, settle_interval=0)
    assert stats["recovered"] is True
    assert not stale.exists()
    # 自愈回填确保最终恰好一份
    lines = [ln for ln in inbox.read_text(encoding="utf-8").split("\n") if ln.strip()]
    assert [json.loads(x)["delivery_id"] for x in lines] == [rec["delivery_id"]]


def test_rotate_captures_old_fd_writes_during_drain(tmp_path: Path, monkeypatch):
    """确定性验证「旧 fd 窗口写入」的收敛回填（不丢行语义核心）。

    用 monkeypatch hook 掉 drain 的 sleep，在「某一轮读之后、下一轮读之前」
    精确注入写入（等价于 receiver 在 rename 后仍持旧 fd 写入 rotating 的窗口）
    —— 该机制 100% 确定性：注入发生在 drain 活动期内，必被下一轮读取捕捉。
    """
    inbox, archive = _rotate_cfg(tmp_path)
    now_ms = time.time() * 1000
    preset = [_event(received_at_ms=int(now_ms)) for _ in range(5)]
    _append_lines(inbox, preset)

    holder = inbox.open("a", encoding="utf-8")  # rename 后该 fd 指向 rotating
    late = [_event(received_at_ms=int(now_ms), pr_number=2000 + i) for i in range(3)]

    orig_sleep = time.sleep
    injected = {"done": False}

    def hooked_sleep(seconds):
        if not injected["done"]:
            injected["done"] = True
            for rec in late:  # 精确落在 drain 收敛循环的内部
                holder.write(json.dumps(rec) + "\n")
            holder.flush()
        orig_sleep(seconds)

    monkeypatch.setattr(time, "sleep", hooked_sleep)
    try:
        inbox_rotate.rotate_inbox(inbox, archive, settle_rounds=50, settle_interval=0.01)
    finally:
        monkeypatch.undo()
        holder.close()

    all_ids: set[str] = set()
    for path in (inbox, archive):
        if path.exists():
            all_ids |= {
                json.loads(x)["delivery_id"]
                for x in path.read_text(encoding="utf-8").split("\n")
                if x.strip()
            }
    expected = {r["delivery_id"] for r in preset} | {r["delivery_id"] for r in late}
    assert injected["done"] is True  # 注入确实发生过（seam 生效）
    assert expected <= all_ids  # 无丢失（inbox ∪ archive 全含）


# ============================================================
# rotate — CLI（--once 退出码不属于 rotate；这里只测参数最小面）
# ============================================================
def test_rotate_cli_runs(tmp_path: Path):
    inbox, archive = _rotate_cfg(tmp_path)
    now_ms = time.time() * 1000
    _append_lines(inbox, [_event(received_at_ms=int(now_ms - 8 * 86_400_000))])
    script = Path(__file__).resolve().parent.parent / "scripts" / "prod_agent" / "inbox_rotate.py"
    proc = subprocess.run(
        [
            sys.executable,
            str(script),
            "--inbox",
            str(inbox),
            "--archive-file",
            str(archive),
            "--settle-interval",
            "0",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert "archived=1" in proc.stdout
    assert not (tmp_path / "inbox.jsonl.rotating").exists()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

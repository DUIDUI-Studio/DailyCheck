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
import shlex
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


# ============================================================
# 输入层损坏：listener 不得崩溃（Issue #66 审核 H-1 / L-6）
# ============================================================
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
def test_state_load_survives_json_layer_damage(
    tmp_path: Path, label: str, payload: bytes, capsys
):
    """损坏发生在 `json.loads` / `read_text` 层时也必须只告警、不崩溃。

    修复前异常网只捕 `(OSError, json.JSONDecodeError)` → 上述三种形态（均为损坏
    state，非攻击）直穿 `main()`（只捕 KeyboardInterrupt）→ systemd
    `Restart=always` 把崩溃掩盖成静默重启循环。本用例只断言「不抛异常 + 状态自洽」，
    不锁死具体解析结果，故对不同 CPython 的 `int_max_str_digits` 设置也稳定。
    """
    state_file = tmp_path / "state.json"
    state_file.write_bytes(payload)
    st = inbox_watcher.WatcherState.load(state_file)  # 修复前此处抛异常
    assert isinstance(st.handled_ids, set)
    assert isinstance(st.failures, dict)
    assert "WARNING" in capsys.readouterr().out or not st.failures, label


def test_delivery_id_lone_surrogate_is_bad_line(tmp_path: Path, capsys):
    """`"\\ud800"` 这类孤立代理字符的 delivery_id 不得让 listener 崩溃（L-6）。

    它既不能 `print`（`UnicodeEncodeError`），也不能作为 state 文件的键写盘 ——
    修复前 `_log` / `save()` 处抛 UnicodeEncodeError 直穿 main()。现按坏行处理
    （指纹去重 + `!r` 转义打印），与孪生副本 `dailycheck_dashboard_on_pr.py` 同口径。
    """
    marker = tmp_path / "marker.txt"
    cfg = _cfg(tmp_path, _cat_cmd(marker))
    cfg.inbox.write_text(
        '{"delivery_id":"\\ud800","action":"opened","repo_full_name":"' + REPO + '"}\n',
        encoding="utf-8",
    )
    state = inbox_watcher.WatcherState()
    assert inbox_watcher.process_once(cfg, state) == 0  # 修复前此处抛 UnicodeEncodeError
    assert _read_marker(marker) == []
    assert state.handled_ids == set()
    assert len(state.bad_hashes) == 1
    assert "malformed inbox line" in capsys.readouterr().out


def test_state_load_survives_surrogate_key(tmp_path: Path, capsys):
    """状态里的**键**是孤立代理字符时，归一化留痕本身不得崩溃。

    规则 7（Issue #66 收口 #51）已在本侧落地：`{"failures": {"\\ud800": ...}}`
    是合法 JSON，归一化会为该条目打 WARNING —— 留痕必须经 `_short()`（repr）
    转义，否则 print 抛 UnicodeEncodeError 直穿 main() → Restart=always 崩溃循环。
    ⚠️ 必须用 `capsys`：pytest 默认 fd 捕获 `errors="replace"` 会吞掉该异常。
    """
    state_file = tmp_path / "state.json"
    state_file.write_text('{"failures":{"\\ud800":{"n":1}}}', encoding="utf-8")
    st = inbox_watcher.WatcherState.load(state_file)  # 修复前此处抛 UnicodeEncodeError
    out = capsys.readouterr().out
    assert "[inbox_watcher] WARNING: state 损坏" in out
    assert "\ud800" in st.failures


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
    `Restart=always` 反复崩在**同一行**（该行永远进不了 `bad_hashes`，故永不跳过）。
    """
    assert inbox_watcher._parse_record(line) is None
    marker = tmp_path / "marker.txt"
    cfg = _cfg(tmp_path, _cat_cmd(marker))
    cfg.inbox.write_text(line + "\n", encoding="utf-8")
    state = inbox_watcher.WatcherState()
    assert inbox_watcher.process_once(cfg, state) == 0  # 修复前此处抛异常
    assert _read_marker(marker) == []
    assert len(state.bad_hashes) == 1


# ============================================================
# 损坏状态：load() 边界归一化（唯一口径）+ 纵深防御（Issue #66 收口 #51）
# ============================================================
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


def test_retry_due_condition(tmp_path: Path):
    """退避到期的判定（主循环解除签名 gating 的依据，Issue #51 / #66）。"""
    now = 1000.0
    # 无失败条目 → 不驱动额外轮次（保住「无变化不空转」的原设计）
    assert inbox_watcher.retry_due(inbox_watcher.WatcherState(), now) is False
    # 退避未到期 → False
    s = inbox_watcher.WatcherState()
    s.failures["d-future"] = {"n": 1, "next": now + 60}
    assert inbox_watcher.retry_due(s, now) is False
    # 退避到期（含恰好等于 now 的边界）→ True
    s.failures["d-due"] = {"n": 2, "next": now}
    assert inbox_watcher.retry_due(s, now) is True
    s.failures["d-due"]["next"] = now - 1
    assert inbox_watcher.retry_due(s, now) is True
    # 状态损坏（缺 key / 非数值 / 非 dict / 有限巨值）→ 保守视作到期，不崩不锁死
    for bad in (
        {"n": 1},
        {"n": 1, "next": "not-a-number"},
        "garbage",
        {"n": 1, "next": 1e308},
    ):
        s = inbox_watcher.WatcherState()
        s.failures["d-bad"] = bad  # type: ignore[assignment]
        assert inbox_watcher.retry_due(s, now) is True, f"未按已到期处理：{bad!r}"


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
    st = inbox_watcher.WatcherState.load(state_file)

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
    assert inbox_watcher.retry_due(st, now) is True


def test_huge_next_no_longer_locks_entry(tmp_path: Path):
    """#51 第 5 轮 M-1 反回归：`next=1e308` 的条目不再被永久锁死。

    修复前该形态被判为「有限值 = 合法」→ `retry_due()` 恒 False、条目永不重试、
    无任何 WARNING（静默丢弃），与 `10**309` / `inf` 反而自愈的口径相矛盾。
    """
    marker = tmp_path / "marker.txt"
    cfg = _cfg(tmp_path, _cat_cmd(marker))
    rec = _event()
    _append_lines(cfg.inbox, [rec])
    _write_state(cfg.state_file, {rec["delivery_id"]: {"n": 1, "next": 1e308}})

    state = inbox_watcher.WatcherState.load(cfg.state_file)
    assert state.failures[rec["delivery_id"]]["next"] == 0.0
    assert inbox_watcher.retry_due(state) is True
    assert inbox_watcher.process_once(cfg, state) == 0
    assert len(_read_marker(marker)) == 1, "巨值 next 仍把条目锁死（M-1 回归）"
    assert rec["delivery_id"] in state.handled_ids


def test_sane_next_boundaries(tmp_path: Path):
    """`_sane_next` 的边界语义钉死（下界含 0、上界含 now + sane_ahead，越界/非有限 → None）。"""
    now = 1000.0
    ahead = inbox_watcher.DEFAULT_SANE_AHEAD
    assert ahead == inbox_watcher.DEFAULT_RETRY_MAX + inbox_watcher.RETRY_NEXT_CLOCK_SLACK_S
    bound = now + ahead
    assert inbox_watcher._sane_next(0, now) == 0.0  # 下界含 0（已到期）
    assert inbox_watcher._sane_next(now - 1, now) == now - 1  # 过期时刻合法
    assert inbox_watcher._sane_next(now + 60, now) == now + 60
    assert inbox_watcher._sane_next(bound, now) == bound  # 上界含等号
    for bad in (
        None,
        "x",
        float("nan"),
        float("inf"),
        float("-inf"),
        10**309,
        bound + 1,
        -1,
    ):
        assert inbox_watcher._sane_next(bad, now) is None, f"未判损坏：{bad!r}"
    # 上界可由配置推出（sane_ahead_for），不再是一个「7 天」式的宽常量
    assert inbox_watcher.sane_ahead_for(60.0) == 360.0
    assert inbox_watcher.sane_ahead_for(86400.0) == 86700.0
    assert inbox_watcher.sane_ahead_for(0.0) == 360.0  # <=0 时退化为默认封顶


def test_load_rejects_next_beyond_configured_cap(tmp_path: Path, capsys):
    """Issue #51 上游审核 M-1 的**残余带**也要关掉：`next ∈ (now+retry_max, now+7d]`。"""
    now = time.time()
    state_file = tmp_path / "state.json"
    _write_state(
        state_file,
        {
            "d-lock-6d": {"n": 1, "next": now + 6 * 24 * 3600},  # 旧 7 天口径下被采信
            "d-lock-3h": {"n": 1, "next": now + 3 * 3600},  # 旧 7 天口径下被采信
            "d-legit": {"n": 2, "next": now + 90},  # 合法：≤ now + (60 + 300)
        },
    )
    st = inbox_watcher.WatcherState.load(
        state_file, sane_ahead=inbox_watcher.sane_ahead_for(60.0)
    )
    assert st.failures["d-lock-6d"]["next"] == 0.0
    assert st.failures["d-lock-3h"]["next"] == 0.0
    assert st.failures["d-legit"]["next"] == now + 90  # 合法条目不得误伤
    out = capsys.readouterr().out
    assert out.count("[inbox_watcher] WARNING: state 损坏") == 2
    assert "d-legit" not in out
    assert inbox_watcher.retry_due(st) is True


def test_load_warns_on_normalization_and_silent_on_legit(tmp_path: Path, capsys):
    """归一化必须留痕（M-1：只修「永久锁死」不修「无 WARNING」= 静默丢弃缩量保留）。"""
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
    st = inbox_watcher.WatcherState.load(state_file)
    out = capsys.readouterr().out
    warn_lines = [ln for ln in out.splitlines() if ln.startswith("[inbox_watcher]")]
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
def test_load_warns_on_container_level_damage(
    tmp_path: Path, field: str, bad_value: object, capsys
):
    """容器级损坏（`failures`/`handled_ids`/`bad_hashes` 形状不对）也必须留痕。

    静默降级有真实副作用：`handled_ids` 形状损坏 → 去重集无声清空 → 同一 delivery
    被**重复触发**；`failures` 形状损坏 → 整张退避表无声丢失（全部立即重试）。
    """
    state_file = tmp_path / "state.json"
    payload: dict = {"failures": {}, "handled_ids": ["h1"], "bad_hashes": ["b1"]}
    payload[field] = bad_value
    state_file.write_text(json.dumps(payload), encoding="utf-8")
    st = inbox_watcher.WatcherState.load(state_file)
    out = capsys.readouterr().out
    assert "[inbox_watcher] WARNING: state 损坏" in out, (
        f"容器级损坏静默：{field}={bad_value!r}"
    )
    assert field in out, out
    assert st.failures == {} or field == "failures"


@pytest.mark.parametrize(
    "payload",
    [
        {"failures": {}, "handled_ids": ["d1", 7, None]},
        {"failures": {}, "bad_hashes": ["h1", {"x": 1}]},
    ],
)
def test_load_warns_on_non_string_items_in_sets(
    tmp_path: Path, payload: dict, capsys
):
    """集合内混入非字符串元素 → 丢弃并留痕（`handled_ids` 静默丢弃 = 重复触发）。"""
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(payload), encoding="utf-8")
    st = inbox_watcher.WatcherState.load(state_file)
    out = capsys.readouterr().out
    assert "[inbox_watcher] WARNING: state 损坏" in out
    assert "非字符串元素" in out
    assert st.handled_ids == {"d1"} or st.bad_hashes == {"h1"}


@pytest.mark.parametrize(
    ("raw_n", "expect"),
    [(0, 0), (1, 1), (1023, 1023), (1024, 1024), (1025, 0), (10**6, 0), (-1, 0)],
)
def test_normalize_failure_attempts_boundary(tmp_path: Path, raw_n: int, expect: int):
    """`n` 的上下界钉死：`[0, MAX_SANE_ATTEMPTS]` 保留，越界归 0。"""
    now = time.time()
    entry = {"n": raw_n, "next": now + 10}
    fixed = inbox_watcher._normalize_failure(
        entry, now, inbox_watcher.DEFAULT_SANE_AHEAD
    )
    assert fixed["n"] == expect, f"n={raw_n} → {fixed!r}"
    assert inbox_watcher._attempts({"n": raw_n, "next": 0}) == expect
    assert fixed["next"] == now + 10  # 合法 next 不得被连带改动


@pytest.mark.parametrize(
    "bad",
    [
        {"n": "not-a-number", "next": "not-a-number"},
        {"n": "bad", "next": None},
        {"n": 0, "next": float("nan")},
        {"n": 1, "next": float("inf")},
        {"n": 1, "next": float("-inf")},
        {"n": 1, "next": 10**309},
        {"n": float("inf"), "next": 0},
        {"n": -5, "next": 0.0},
        {"n": 1, "next": 1e308},
        {"n": 1, "next": 1e18},
        "garbage",
    ],
)
def test_corrupted_failure_entry_self_heals_without_crash(
    tmp_path: Path, bad: object
):
    """损坏 `next` / `n`（但 JSON 合法）→ 不崩 + 一轮自愈。

    修复前 `process_once` 里 `float(fail.get("next", 0))` 无兜底 → ValueError 直穿
    main()（只捕 KeyboardInterrupt）→ systemd `Restart=always` 崩溃循环。
    """
    marker = tmp_path / "marker.txt"
    cfg = _cfg(tmp_path, _cat_cmd(marker))
    rec = _event()
    _append_lines(cfg.inbox, [rec])
    state = inbox_watcher.WatcherState()
    state.failures[rec["delivery_id"]] = bad  # type: ignore[assignment]

    assert inbox_watcher.retry_due(state) is True
    assert inbox_watcher.process_once(cfg, state) == 0  # 修复前此处抛异常
    assert rec["delivery_id"] in state.handled_ids
    assert rec["delivery_id"] not in state.failures
    assert len(_read_marker(marker)) == 1


@pytest.mark.parametrize(
    "bad_n", ["bad", -5, None, float("inf"), float("-inf"), float("nan")]
)
def test_corrupted_failure_entry_rewritten_on_repeat_failure(
    tmp_path: Path, bad_n: object
):
    """损坏条目 + 触发仍失败 → next / n 被重写成合法值（不永久损坏）。"""
    cfg = _cfg(tmp_path, ["/bin/sh", "-c", "exit 1"], retry_base=3600.0)
    rec = _event()
    _append_lines(cfg.inbox, [rec])
    state = inbox_watcher.WatcherState()
    state.failures[rec["delivery_id"]] = {"n": bad_n, "next": None}

    assert inbox_watcher.process_once(cfg, state) == 1
    entry = state.failures[rec["delivery_id"]]
    assert entry["n"] == 1, f"损坏计数未归零重算：{entry!r}"
    assert isinstance(entry["next"], float)
    assert inbox_watcher.retry_due(state, now=time.time()) is False


def test_prune_excludes_handled_failure_entries(tmp_path: Path):
    """prune 必须清掉「已 handled 但仍在 failures 表」的残留条目。

    这类条目永远走 handled 短路（不重试、无任何日志），留在表里只会让
    `retry_due()` 恒 True → 主循环每轮空跑。只在状态文件被手改 / 旧版本遗留时出现。
    """
    marker = tmp_path / "marker.txt"
    cfg = _cfg(tmp_path, _cat_cmd(marker))
    rec = _event()
    _append_lines(cfg.inbox, [rec])
    state = inbox_watcher.WatcherState()
    state.handled_ids.add(rec["delivery_id"])
    state.failures[rec["delivery_id"]] = {"n": 1, "next": 0.0}  # 已到期
    assert inbox_watcher.retry_due(state) is True  # 残留会让主循环空转
    assert inbox_watcher.process_once(cfg, state) == 0
    assert rec["delivery_id"] not in state.failures  # prune 清掉
    assert inbox_watcher.retry_due(state) is False  # 主循环不再空转


def test_retry_delay_survives_huge_attempts(tmp_path: Path):
    """异常大的 attempts 不得崩溃或挂死 —— 一律封顶（Issue #66 复现项）。

    旧实现 `base * 2 ** (attempts - 1)` 对 `attempts=10**18` 实测挂死 >8s、
    `int(1e300)` 挂死 >5 分钟。
    """
    cfg = _cfg(tmp_path, ["/bin/sh", "-c", "true"], retry_base=2.0, retry_max=60.0)
    assert inbox_watcher._retry_delay(cfg, 1) == 2.0
    assert inbox_watcher._retry_delay(cfg, 3) == 8.0  # 与 base*2^(n-1) 等价
    assert inbox_watcher._retry_delay(cfg, 7) == 60.0  # 已封顶
    assert inbox_watcher._retry_delay(cfg, 1000) == 60.0
    started = time.monotonic()
    assert inbox_watcher._retry_delay(cfg, 10**6) == 60.0  # 旧式在此抛 OverflowError
    assert inbox_watcher._retry_delay(cfg, 10**18) == 60.0  # 旧式在此挂死 >8s
    assert inbox_watcher._retry_delay(cfg, int(1e300)) == 60.0  # 旧式挂死 >5min
    assert time.monotonic() - started < 1.0, "退避计算不得退化成长时间挂死"
    zero = _cfg(tmp_path, ["/bin/sh", "-c", "true"], retry_base=0.0)
    assert inbox_watcher._retry_delay(zero, 5) == 0.0


def test_failure_retries_after_backoff_with_no_new_inbox_lines(tmp_path: Path):
    """Issue #51 核心回归：触发失败 + inbox **无任何新行** → 退避到期必须自动重试。

    修复前主循环只在文件签名变化时 process_once，而重试不写 inbox（签名不变）
    → 退避条目永不重试，本用例会一直等到 deadline 后失败。
    """
    script = (
        Path(__file__).resolve().parent.parent
        / "scripts"
        / "prod_agent"
        / "inbox_watcher.py"
    )
    inbox = tmp_path / "inbox.jsonl"
    inbox.write_text("", encoding="utf-8")
    marker = tmp_path / "marker.txt"
    counter = tmp_path / "attempts.txt"
    body = (
        f"n=$(cat {shlex.quote(str(counter))} 2>/dev/null || echo 0); "
        f"n=$((n+1)); echo $n > {shlex.quote(str(counter))}; "
        f'if [ "$n" -lt 2 ]; then exit 1; fi; '
        f"cat >> {shlex.quote(str(marker))}; printf '\\n' >> {shlex.quote(str(marker))}"
    )
    rec = _event()
    proc = subprocess.Popen(
        [
            sys.executable,
            str(script),
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
        assert _read_marker(marker), "退避后的重试未发生（Issue #51 回归）"
        assert elapsed >= 0.25, f"未走退避（elapsed={elapsed:.2f}s）"
        assert counter.read_text(encoding="utf-8").strip() == "2"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_triggered_log_survives_surrogate_values(tmp_path: Path, capsys):
    """规则 7 的「一切 inbox 键值进日志前必须转义」也必须覆盖 `delivery_id` 之外的字段。

    审核 H-1-r3：`json.loads` 能把 inbox 行里的 `"\\ud800"` 解成孤立代理字符；
    修复前 `triggered:` 日志把 `pr_number` / `pr_head_sha` **原样**插 f-string →
    `print` 抛 UnicodeEncodeError → `process_once` 未返回、`state.save()` 永不执行
    → systemd `Restart=always` 每次崩在同一行（该行已进 handled 前就崩，故永不跳过）。
    ⚠️ 必须用 `capsys`：pytest 默认 fd 捕获 `errors="replace"` 会吞掉该异常。
    """
    marker = tmp_path / "marker.txt"
    cfg = _cfg(tmp_path, _cat_cmd(marker))
    cfg.inbox.write_text(
        '{"delivery_id":"d-surr","action":"opened","repo_full_name":"'
        + REPO
        + '","pr_number":"\\ud800","pr_head_sha":"\\ud800"}\n',
        encoding="utf-8",
    )
    state = inbox_watcher.WatcherState()
    assert inbox_watcher.process_once(cfg, state) == 0  # 修复前此处抛 UnicodeEncodeError
    out = capsys.readouterr().out
    assert "triggered:" in out
    assert "d-surr" in state.handled_ids
    assert len(_read_marker(marker)) == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

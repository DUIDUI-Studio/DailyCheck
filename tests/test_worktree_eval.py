"""Issue #102 invariants: fixtures are synthetic and never shipped as runtime rows."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from scripts.dailycheck_dashboard import eval_facts as ef
from scripts.dailycheck_dashboard import ledger_db, ops_parse
from scripts.dailycheck_dashboard.eval_view import render_worktree_eval
from scripts.model_eval.collect_pure import (
    Usage,
    build_ledger,
    parse_claude_transcript,
    parse_codex_transcript,
    parse_ts,
)
from scripts.model_eval.lifecycle import enrich_dispatches, lifecycle_fields

SNAPSHOT = "2026-10-31T12:00:00Z"


def usage(n=100, **kw):
    return {"input": n, "output": 20, "cache_read": 10, "cache_write": 5, "reasoning": 12, **kw}


def row(did="d1", **kw):
    return {
        "schema_version": 2,
        "ts": "2026-10-31T01:00:00Z",
        "dispatch_id": did,
        "worker_id": did,
        "worker_instance_id": "worker:a",
        "worktree_instance_id": "tree:1",
        "repo_id": ef.DEFAULT_REPO,
        "host_id": "host:a",
        "worktree_path": "/work/tree",
        "branch": "feat/issue-102-test",
        "role": "developer",
        "pr": 9,
        "issue": 102,
        "model_effective": "m1",
        "tokens": usage(),
        "collected_at": SNAPSHOT,
        "started_at": "2026-10-31T01:00:00Z",
        "ended_at": "2026-10-31T02:00:00Z",
        **kw,
    }


def facts(rows, prs=None, **kw):
    return ef.prepare_facts(
        rows, prs or [{"number": 9, "state": "MERGED"}], snapshot=SNAPSHOT, **kw
    )


def evt(key="e1", at="2026-10-31T01:00:00Z", model="m1", **kw):
    return {"event_id": key, "event_ts": at, "model": model, "tokens": usage(), **kw}


def test_pr_and_worker_dedupe_across_trees_and_retries():
    data = facts(
        [
            row(),
            row("d2", retry_of="d1"),
            row("d3", worktree_instance_id="tree:2", worktree_path="/work/other"),
        ]
    )
    grouped = ef.aggregate_worktrees(data)
    assert len(grouped) == 2
    assert [len(g["prs"]) for g in grouped] == [1, 1]
    s = ef.summarize(data)
    assert len(s["prs"]) == 1
    assert s["workers"] == 1 and s["executions"] == 3 and s["retries"] == 1
    assert s["worker_seconds"] == 3600  # same worker interval is not multiplied by dispatch/tree


def test_same_issue_multiple_roles_are_collaboration_not_retry():
    s = ef.summarize(facts([row(), row("d2", role="reviewer", worker_instance_id="worker:b")]))
    assert s["retries"] == 0 and s["workers"] == 2


def test_recreated_path_is_new_instance_but_reused_tree_stays_one():
    data = facts([row(), row("d2", issue=103), row("d3", worktree_instance_id="tree:2")])
    assert len(ef.aggregate_worktrees(data)) == 2
    assert len(ef.aggregate_worktrees(data)[0]["facts"]) == 2


def test_legacy_path_grouping_is_host_repo_scoped_and_approximate():
    data = facts(
        [
            row(worktree_instance_id=None),
            row("d2", worktree_instance_id=None),
            row("d3", worktree_instance_id=None, host_id="host:b"),
            row("d4", worktree_instance_id=None, repo_id="other/repo"),
        ]
    )
    assert len(ef.aggregate_worktrees(data)) == 3
    assert all(f["quality"] == "historical_path" for f in data)


def test_parallel_worker_time_exceeds_tree_span_and_unknown_end_stays_unknown():
    data = facts(
        [row(), row("d2", worker_instance_id="worker:b"), row("d3", ended_at=None, running=False)]
    )
    s = ef.summarize(data)
    assert s["worker_seconds"] == 7200 and s["span_seconds"] == 3600
    assert s["time_coverage"] == 2 and data[2]["interval"] is None


def test_proven_running_uses_observed_snapshot_not_report_now():
    data = facts([row(ended_at=None, running=True, observed_at="2026-10-31T01:15:00Z")])
    assert ef.summarize(data)["worker_seconds"] == 900


def test_time_clips_and_worker_union_in_half_open_window():
    data = facts(
        [row(), row("d2", started_at="2026-10-31T01:30:00Z", ended_at="2026-10-31T03:00:00Z")]
    )
    selected = ef.select_facts(
        data, start=parse_ts("2026-10-31T01:45:00Z"), end=parse_ts("2026-10-31T02:15:00Z")
    )
    assert ef.summarize(selected)["worker_seconds"] == 1800
    assert ef.summarize(selected)["span_seconds"] == 1800


def test_exclusive_tokens_and_reasoning_subitem_partial_fields():
    s = ef.summarize(facts([row(), row("d2", tokens={"input": 10}), row("d3", tokens=None)]))
    assert s["total"] == 145  # 100+20+10+5 + 10; reasoning is already in output
    assert s["tokens"]["reasoning"] == 12
    assert s["token_coverage"] == 1 and s["rows"] == 3
    assert ef.summarize(facts([row(tokens=None)]))["total"] is None


def test_legacy_multiple_models_unsplittable_no_duplicate_totals():
    data = facts([row(model_effective="m1,m2", tokens=usage())])
    assert data[0]["models"] == ["unsplittable"]
    assert ef.select_facts(data, model="m1") == []
    assert ef.summarize(ef.select_facts(data, model="unsplittable"))["total"] == 135


def test_model_event_filter_keeps_only_model_tokens_and_no_fake_model_time():
    data = facts([row(usage_events=[evt(), evt("e2", model="m2")])])
    assert ef.summarize(data)["total"] == 270
    selected = ef.select_facts(data, model="m2")
    assert ef.summarize(selected)["total"] == 135
    assert ef.summarize(selected)["worker_seconds"] is None


def test_event_replay_across_executions_only_counted_once():
    data = facts([row(usage_events=[evt()]), row("d2", usage_events=[evt()])])
    assert ef.summarize(data)["total"] == 135
    assert ef.summarize(data)["token_coverage"] == 1


def test_window_event_and_legacy_dispatch_attribution_cst_boundary():
    before, boundary = "2026-10-30T15:59:59Z", "2026-10-30T16:00:00Z"
    data = facts(
        [
            row(
                ts=before,
                started_at=None,
                ended_at=None,
                usage_events=[evt(at=before), evt("e2", at=boundary)],
            ),
            row("d2", ts=before, started_at=None, ended_at=None),
            row("d3", ts=boundary, started_at=None, ended_at=None),
        ]
    )
    s = ef.summarize(
        ef.select_facts(data, start=parse_ts(boundary), end=parse_ts("2026-10-31T16:00:00Z"))
    )
    assert s["total"] == 270  # one exact event + one dispatch-time legacy total
    assert len(ef.select_facts(data, end=parse_ts(boundary))) == 2


def test_old_schema_no_worker_instance_and_no_time_fabrication():
    data = facts([{"ts": "2026-10-31T01:00:00Z", "worker_id": "ctx_legacy", "tokens": None}])
    assert data[0]["interval"] is None
    s = ef.summarize(data)
    assert s["workers"] == 0 and s["unknown_workers"] == 1 and s["total"] is None
    assert data[0]["quality"] == "unattributed" and data[0]["freshness"] == "unknown"


def test_freshness_uses_source_collection_not_db_update():
    data = facts([row(collected_at="2026-10-31T11:29:59Z"), row("d2", collected_at=None)])
    assert [f["freshness"] for f in data] == ["stale", "unknown"]
    assert facts([row()], unavailable=True)[0]["freshness"] == "unavailable"


def test_branch_pr_association_exact_repo_match():
    data = facts(
        [row(pr=None)],
        prs=[
            {"number": 9, "state": "OPEN", "head_ref": "feat/issue-102-test"},
            {"number": 10, "state": "OPEN", "head_ref": "feat/issue-102"},
        ],
    )
    assert [p["number"] for p in data[0]["prs"]] == [9]


def test_db_v1_migration_idempotency_history_and_optional_null_timestamp(tmp_path):
    db = tmp_path / "dash.db"
    ledger_db.init_ledger_schema(db)
    rows = [
        ledger_db._normalise(row(worktree_deleted_at="2026-10-31T03:00:00Z")),
        ledger_db._normalise(row("d2", ts=None)),
    ]
    for _ in range(3):
        ledger_db.upsert_dispatch_ledger(db, rows)
    loaded = ledger_db.load_execution_rows(db)
    assert len(loaded) == 2
    assert any(r.get("ts") is None for r in loaded)
    assert any(r.get("worktree_deleted_at") == "2026-10-31T03:00:00Z" for r in loaded)
    assert len(ef.aggregate_worktrees(facts(loaded))) == 1


def proof():
    return {
        "head_sha": "head",
        "linked_issues_closed": True,
        "required_checks": {k: {"sha": "head", "state": "success"} for k in ef.REQUIRED_CHECKS},
        "deployment": {
            "sha": "merge",
            "result": "success",
            "verification": "success",
            "observation": "success",
            "verified_at": "2026-10-31T01:00:00Z",
            "observed_until": "2026-10-31T01:30:00Z",
            "required_observation_seconds": 1800,
            "evidence_ref": "prod-report:9",
        },
    }


@pytest.mark.parametrize(
    "missing", ["ci", "issue", "merged", "verify", "observe", "sha", "window", "ref"]
)
def test_delivery_requires_every_sha_bound_evidence(missing):
    pr = {"state": "MERGED", "head_sha": "head", "merge_sha": "merge"}
    ev = proof()
    assert ef.delivery_state(pr, ev) == "delivered"
    if missing == "ci":
        ev["required_checks"][ef.REQUIRED_CHECKS[0]]["sha"] = "old"
    if missing == "issue":
        ev["linked_issues_closed"] = False
    if missing == "merged":
        pr["state"] = "OPEN"
    if missing == "verify":
        ev["deployment"]["verification"] = "unknown"
    if missing == "observe":
        ev["deployment"]["observation"] = "unknown"
    if missing == "sha":
        ev["deployment"]["sha"] = "old"
    if missing == "window":
        ev["deployment"]["observed_until"] = "2026-10-31T01:01:00Z"
    if missing == "ref":
        ev["deployment"]["evidence_ref"] = ""
    assert ef.delivery_state(pr, ev) != "delivered"


def test_merged_and_rc_zero_never_proves_delivery():
    data = facts(
        [row()],
        prs=[{"number": 9, "state": "MERGED", "head_sha": "head"}],
        deploy_results=[{"pr": 9, "sha": "head", "rc": 0, "ts": SNAPSHOT}],
    )
    assert data[0]["prs"][0]["delivery"] == "awaiting_deploy"
    assert ef.delivery_state({"state": "OPEN"}) == "ci_unknown"


def test_deploy_result_failure_sha_scope_and_parser_redaction(tmp_path):
    p = tmp_path / "events.jsonl"
    p.write_text(
        "{broken\n"
        + json.dumps(
            {
                "pr": 9,
                "sha": "head",
                "rc": 24,
                "ts": SNAPSHOT,
                "log": "/private/prod",
                "reason": "secret",
            }
        )
    )
    results = ops_parse.parse_deploy_results(p)
    assert "secret" not in str(results) and "log" not in results[0]
    data = facts(
        [row()], prs=[{"number": 9, "state": "MERGED", "head_sha": "head"}], deploy_results=results
    )
    assert data[0]["prs"][0]["delivery"] == "deploy_failed"
    with pytest.raises(ops_parse.OpsParseError):
        ops_parse.parse_deploy_results(tmp_path / "missing")


def codex_sample(model="m1", before=100, after=200, *, reset=False):
    records = [
        {"type": "session_meta", "payload": {"id": "provider-session", "cwd": "/w"}},
        {"type": "turn_context", "payload": {"model": model}},
    ]
    for i, n in enumerate([before, after]):
        records.append(
            {
                "type": "token_usage_record",
                "timestamp": f"2026-10-31T01:0{i}:00Z",
                "ordinal": i,
                "payload": {
                    "thread_id": "thread",
                    "thread_token_usage": {
                        "input_tokens": n,
                        "output_tokens": n // 10,
                        "cached_input_tokens": n // 5,
                        "reasoning_output_tokens": n // 20,
                    },
                },
            }
        )
    return records


def parse_codex(records):
    return parse_codex_transcript(map(json.dumps, records), "/provider.jsonl", 0)


def test_codex_replay_cumulative_delta_model_switch_and_reset():
    rec = codex_sample()
    rec.insert(3, {"type": "turn_context", "payload": {"model": "m2"}})
    rec.append(rec[-1])
    t = parse_codex(rec)
    assert t.usage_by_model["m1"] == Usage(input=80, output=10, cache_read=20, reasoning=5)
    assert t.usage_by_model["m2"] == Usage(input=80, output=10, cache_read=20, reasoning=5)
    assert len(t.events) == 2
    reset = parse_codex(codex_sample(before=100, after=20))
    assert reset.usage_by_model["m1"].input == 96
    assert reset.events[1]["counter_reset"] is True


def test_codex_legacy_model_switch_without_event_time_is_unsplittable():
    rec = codex_sample()
    rec.insert(3, {"type": "turn_context", "payload": {"model": "m2"}})
    for r in rec:
        r.pop("timestamp", None)
    t = parse_codex(rec)
    assert set(t.usage_by_model) == {"unsplittable"}
    assert t.usage_by_model["unsplittable"].input == 160
    assert t.events[0]["event_ts"] is None


def test_codex_current_and_legacy_are_alternatives_and_cache_mutually_exclusive():
    rec = codex_sample()
    rec.append(
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"input_tokens": 9999, "output_tokens": 9999}},
            },
        }
    )
    t = parse_codex(rec)
    assert t.usage_by_model["m1"].input == 160


def dispatch(did="ctx_one", **kw):
    return {
        "ts": "2026-10-31T01:00:00Z",
        "issue": 102,
        "data": {
            "dispatch_id": did,
            "task_id": "task_one",
            "worktree_path": "/w",
            "started_at": "2026-10-31T01:00:00Z",
            "ended_at": "2026-10-31T02:00:00Z",
            **kw,
        },
    }


def test_concurrent_same_path_usage_is_unassigned_never_double_counted():
    t = parse_codex(codex_sample())
    rows = build_ledger([dispatch(), dispatch("ctx_two")], [t, t])
    executions = [r for r in rows if r["record_kind"] == "execution"]
    assert all(r["tokens"] is None for r in executions)
    unassigned = [r for r in rows if r["record_kind"] != "execution"]
    assert len(unassigned) == 1 and unassigned[0]["tokens"]["input"] == 160


def test_preamble_identifiers_link_usage_exactly_despite_concurrency():
    rec = codex_sample()
    rec.insert(
        1,
        {
            "type": "response_item",
            "payload": {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "You are a dispatched worker. === CLI COMMANDS === --dispatch-id ctx_one --task-id task_one",
                    }
                ],
            },
        },
    )
    rows = build_ledger([dispatch(), dispatch("ctx_two")], [parse_codex(rec)])
    assert rows[0]["tokens"]["input"] == 160 and rows[1]["tokens"] is None
    assert rows[0]["usage_events"][0]["session_id"] == "provider-session"


def test_explicit_session_link_and_mtime_is_never_attribution():
    t = parse_codex(codex_sample())
    rows = build_ledger([dispatch(started_at=None, ended_at=None)], [t])
    assert rows[0]["tokens"] is None
    rows = build_ledger(
        [dispatch(session_id="provider-session", started_at=None, ended_at=None)], [t]
    )
    assert rows[0]["tokens"]["input"] == 160


def test_long_session_dispatch_switch_only_attributes_segment():
    rec = codex_sample()

    def pre(did):
        return {
            "type": "response_item",
            "payload": {
                "role": "user",
                "content": [
                    {
                        "text": f"You are a dispatched worker. === CLI COMMANDS === --dispatch-id {did} --task-id task_one"
                    }
                ],
            },
        }

    rec.insert(1, pre("ctx_one"))
    rec.insert(4, pre("ctx_two"))
    rows = build_ledger([dispatch(), dispatch("ctx_two")], [parse_codex(rec)])
    assert [r["tokens"]["input"] for r in rows] == [80, 80]


def test_claude_message_dedupe_and_bad_lines():
    rec = {
        "type": "assistant",
        "sessionId": "s1",
        "cwd": "/w",
        "timestamp": SNAPSHOT,
        "message": {
            "id": "message1",
            "model": "claude",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    }
    t = parse_claude_transcript([json.dumps(rec), json.dumps(rec), "{bad"], "/s1", 0)
    assert len(t.events) == 1 and t.usage_by_model["claude"].input == 10
    assert t.warnings


def lifecycle(verdict="unverifiable", completed=None):
    return {
        "dispatch": {
            "id": "ctx_one",
            "taskId": "task_one",
            "processIncarnation": "process1",
            "hostScope": {"hostId": "local"},
            "dispatchedAt": "2026-10-31 01:00:00",
            "completedAt": completed,
            "status": "dispatched",
        },
        "worker": {
            "worktreeId": "repo::/w",
            "startOptions": {
                "launch": {
                    "requested": {"model": "requested"},
                    "effective": {"model": "actual", "effort": "high"},
                }
            },
        },
        "projection": {"outcome": "in_progress", "liveness": {"verdict": verdict}},
        "observation": {"status": "live", "exactWorker": True},
    }


def test_pty_live_is_not_proof_running_and_process_identity_survives_reuse():
    a = lifecycle_fields(lifecycle(), {}, SNAPSHOT, "host")
    assert a["running"] is False and a["ended_at"] is None
    b = lifecycle_fields(lifecycle("live"), {}, SNAPSHOT, "host")
    assert b["running"] is True and b["worker_instance_id"] == "host:process1"
    assert lifecycle_fields(lifecycle("exited"), {}, SNAPSHOT, "host")["running"] is False


def test_current_tree_instance_does_not_retroactively_identify_old_lifecycle():
    created = datetime(2026, 10, 31, 2, tzinfo=timezone.utc).timestamp() * 1000
    t = {"instanceId": "new", "createdAt": created, "path": "/w", "branch": "new-branch"}
    assert lifecycle_fields(lifecycle(), t, SNAPSHOT, "h")["worktree_instance_id"] is None
    assert lifecycle_fields(lifecycle(), t, SNAPSHOT, "h")["branch"] is None
    t["createdAt"] -= 7200 * 1000
    assert lifecycle_fields(lifecycle(), t, SNAPSHOT, "h")["worktree_instance_id"] == "new"


def test_orca_pagination_scope_partial_and_per_source_failure():
    calls = []

    def read(*args):
        calls.append(args)
        if args[:2] == ("worktree", "current"):
            return {"worktree": {"repoId": "repo"}}
        if args[:2] == ("orchestration", "worker-list"):
            if "--cursor" in args:
                return {"workers": [], "scope": {"source": "bound"}, "page": {"hasMore": False}}
            return {
                "workers": [{"dispatchId": "ctx_one", "resource": {"worktreeId": "repo::/w"}}],
                "scope": {"source": "bound"},
                "page": {"hasMore": True, "nextCursor": "next"},
            }
        if args[:2] == ("orchestration", "worker-show"):
            return lifecycle()
        raise RuntimeError("tree deleted")

    rows, warnings = enrich_dispatches([], read=read, observed_at=SNAPSHOT, local_host="h")
    assert any("--cursor" in c for c in calls)
    assert rows[0]["data"]["worktree_path"] == "/w"
    assert rows[0]["data"]["running"] is False
    assert any("scoped" in w for w in warnings)
    broken, _ = enrich_dispatches(
        [dispatch()],
        read=lambda *a: (_ for _ in ()).throw(RuntimeError("offline")),
        observed_at=SNAPSHOT,
        local_host="h",
    )
    assert broken[0]["data"]["source_warnings"]


def test_html_inert_json_escaping_and_self_contained_ui():
    payload = "</script><img src=x onerror=alert(1)>"
    html = render_worktree_eval(
        rows=[row(role=payload, branch="m&m", worktree_name=payload)], prs=[], snapshot=SNAPSHOT
    )
    assert payload not in html
    assert "\\u003c/script\\u003e" in html and "m\\u0026m" in html
    assert 'id="ev-window"' in html and "aria-controls" in html
    assert "按派单时间归档" in html and "其中推理" in html
    assert "<script src=" not in html


def test_history_retains_deleted_sources_without_freshening_usage():
    from scripts.model_eval.collect_pure import preserve_history

    old = row(
        usage_events=[evt()], worktree_deleted_at=SNAPSHOT, collected_at="2026-10-30T00:00:00Z"
    )
    new = row(worktree_instance_id=None, tokens=None, usage_events=[])
    retained = preserve_history([new], [old, row("d2")])
    assert len(retained) == 2
    assert retained[0]["worktree_instance_id"] == "tree:1"
    assert retained[0]["collected_at"] == old["collected_at"]
    assert retained[0]["usage_events"] == old["usage_events"]
    assert retained[0]["worktree_deleted_at"] == SNAPSHOT


def test_unknown_timestamp_totals_only_appear_in_lifecycle_view():
    data = facts([row(ts=None, started_at=None, ended_at=None)])
    assert ef.summarize(ef.select_facts(data))["total"] == 135
    assert ef.select_facts(data, start=0, end=2e9) == []


def test_half_open_window_excludes_upper_boundary_events():
    data = facts(
        [row(usage_events=[evt(at="2026-10-30T15:59:59Z"), evt("e2", at="2026-10-30T16:00:00Z")])]
    )
    selected = ef.select_facts(data, end=parse_ts("2026-10-30T16:00:00Z"))
    assert ef.summarize(selected)["total"] == 135


def test_real_ops_route_renders_worktree_module(logged_client, tmp_path, monkeypatch):
    from scripts.dailycheck_dashboard.render import RenderInput, render

    client, _ = logged_client
    path = tmp_path / "latest.html"
    path.write_text(
        render(
            RenderInput(
                created_at=SNAPSHOT,
                trigger_kind="test",
                trigger_event_id=None,
                head_sha=None,
                origin_main_sha=None,
                summary={},
                eval_rows=[row()],
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DAILYCHECK_OPS_DASHBOARD", str(path))
    result = client.get("/ops")
    assert result.status_code == 200
    assert b'id="ev-data"' in result.data and b'id="ev-rows"' in result.data
    assert "no-store" in result.headers["Cache-Control"]
    assert b'http-equiv="refresh"' in result.data


def test_atomic_publish_orders_collect_upload_rename_and_dry_run(tmp_path):
    from scripts.model_eval.publish import refresh

    calls = []

    def run(command, **kw):
        calls.append(command)
        assert kw["check"] is True

    args = dict(
        destination="approved-host",
        remote_path="/var/lib/eval/ledger.jsonl",
        ledger=tmp_path / "eval" / "ledger.jsonl",
        run=run,
    )
    plan = refresh(**args, dry_run=True)
    assert not calls and not args["ledger"].parent.exists()
    assert plan[0][1:4] == ["-m", "scripts.model_eval", "collect"]
    refresh(**args)
    assert len(calls) == 3 and calls[1][0] == "scp" and calls[2][0] == "ssh"
    assert ".upload-" in calls[1][-1]
    assert calls[2][-1].endswith(" /var/lib/eval/ledger.jsonl")


@pytest.mark.parametrize("failure", [0, 1, 2])
def test_failed_collect_or_upload_never_silently_publishes(tmp_path, failure):
    import subprocess

    from scripts.model_eval.publish import refresh

    calls = []

    def run(command, **kw):
        calls.append(command)
        if len(calls) == failure + 1:
            raise subprocess.CalledProcessError(1, command)

    with pytest.raises(subprocess.CalledProcessError):
        refresh(
            destination="host", remote_path="/ledger.jsonl", ledger=tmp_path / "ledger", run=run
        )
    assert len(calls) == failure + 1


@pytest.mark.parametrize(
    "destination,path",
    [("-oProxyCommand=bad", "/ledger"), ("host", "/tmp/ledger;rm"), ("host", "/tmp/../ledger")],
)
def test_publisher_rejects_shell_syntax(destination, path, tmp_path):
    from scripts.model_eval.publish import refresh

    with pytest.raises(ValueError):
        refresh(destination=destination, remote_path=path, ledger=tmp_path / "ledger", dry_run=True)


def test_compact_ui_preserves_cst_date_window_model_and_partial_totals():
    data = facts(
        [
            row(
                usage_events=[
                    evt("a", at="2026-10-30T15:59:59Z"),
                    evt("b", at="2026-10-30T16:00:00Z"),
                    evt("c", at="2026-10-30T20:00:00Z"),
                    evt("d", at="2026-10-31T01:00:00Z", model="m2"),
                ]
            ),
            row("d2", tokens={"input": 10}),
        ]
    )
    compact = ef.compact_ui_facts(data)
    assert len(compact[0]["events"]) == 3
    assert sum(e["event_count"] for e in compact[0]["events"]) == 4
    lo, hi = parse_ts("2026-10-30T16:00:00Z"), parse_ts("2026-10-31T16:00:00Z")
    for filters in [
        {},
        {"start": lo, "end": hi},
        {"start": lo, "end": hi, "model": "m1"},
        {"end": lo},
        {"model": "m2"},
    ]:
        a, b = [ef.summarize(ef.select_facts(x, **filters)) for x in (data, compact)]
        assert a["tokens"] == b["tokens"] and a["total"] == b["total"]
        assert a["worker_seconds"] == b["worker_seconds"]
        assert a["token_coverage"] == b["token_coverage"]
    bucket = compact[0]["events"][1]
    assert (
        bucket["event_count"] == 2
        and bucket["first_event_id"] == "b"
        and bucket["last_event_id"] == "c"
    )


def test_missing_provider_fields_remain_unknown_not_observed_zero():
    source = {
        "type": "assistant",
        "sessionId": "s",
        "message": {
            "id": "bad",
            "model": "m1",
            "usage": {"input_tokens": 8, "output_tokens": "invalid"},
        },
    }
    transcript = parse_claude_transcript([json.dumps(source)], "/s", 0)
    event = transcript.events[0]
    assert event["tokens"]["input"] == 8 and event["tokens"]["output"] is None
    assert event["tokens"]["reasoning"] is None and event["complete"] is False
    data = facts([row(usage_events=[event])])
    result = ef.summarize(data)
    assert result["total"] == 8 and result["tokens"]["output"] is None
    assert result["token_coverage"] == 0


def test_incomplete_cumulative_snapshot_does_not_reset_or_double_usage():
    rec = codex_sample()
    partial = {
        "type": "token_usage_record",
        "timestamp": "2026-10-31T01:00:30Z",
        "payload": {"thread_id": "thread", "thread_token_usage": {"input_tokens": 150}},
    }
    rec.insert(3, partial)
    t = parse_codex(rec)
    assert t.usage_by_model["m1"].input == 160
    assert any("incomplete_cumulative" in w for w in t.warnings)
    assert len(t.events) == 2


def test_reasoning_omission_does_not_reset_cumulative_base_counters():
    rec = codex_sample()
    rec[-1]["payload"]["thread_token_usage"].pop("reasoning_output_tokens")
    t = parse_codex(rec)
    assert t.usage_by_model["m1"].input == 160
    assert t.events[1]["counter_reset"] is False
    assert t.events[1]["tokens"]["reasoning"] is None


def test_quoted_identifiers_without_worker_preamble_are_not_an_explicit_link():
    rec = codex_sample()
    rec.insert(
        1,
        {
            "type": "response_item",
            "payload": {
                "role": "user",
                "content": [
                    {"text": "Explain --dispatch-id ctx_one --task-id task_one in this command"}
                ],
            },
        },
    )
    rows = build_ledger([dispatch(), dispatch("ctx_two")], [parse_codex(rec)])
    assert all(r["tokens"] is None for r in rows if r["record_kind"] == "execution")


def test_reassigned_event_does_not_remain_in_archived_unattributed_session():
    from scripts.model_eval.collect_pure import preserve_history

    previous = {"row_id": "orphan", "record_kind": "unattributed_session", "usage_events": [evt()]}
    current = row(record_kind="execution", usage_events=[evt()])
    retained = preserve_history([current], [previous])
    assert len(retained) == 1 and retained[0]["dispatch_id"] == "d1"


def test_transcript_source_boundary_excludes_other_repo_with_same_tree_name():
    from scripts.model_eval.collect import filter_project_transcripts
    from scripts.model_eval.collect_pure import Transcript

    a = Transcript("codex", "/a", "/projects/DailyCheck/shared-name", 0, {})
    b = Transcript("codex", "/b", "/projects/OtherRepo/shared-name", 0, {})
    unknown = Transcript("codex", "/c", None, 0, {})
    root = Transcript("codex", "/d", "/projects/DailyCheck", 0, {})
    kept, excluded = filter_project_transcripts(
        [a, b, unknown, root], {"/projects/DailyCheck/shared-name", "/projects/DailyCheck"}
    )
    assert kept == [a, root] and excluded == 2
    assert all("OtherRepo" not in str(t) for t in kept)


def test_actual_v1_table_migrates_without_dropping_legacy_rows(tmp_path):
    import sqlite3

    path = tmp_path / "v1.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(ledger_db._LEDGER_DDL)
        conn.execute(
            "INSERT INTO dispatch_ledger(worker_id, ts, updated_at) VALUES ('ctx_old', ?, ?)",
            ("2026-10-31T01:00:00Z", SNAPSHOT),
        )
    for _ in range(2):
        ledger_db.init_ledger_schema(path)
    loaded = ledger_db.load_execution_rows(path)
    assert len(loaded) == 1 and loaded[0]["worker_id"] == "ctx_old"
    assert ef.summarize(facts(loaded))["workers"] == 0


def test_issue_hash_search_matches_explicit_issue_number():
    data = facts([row(issue=102), row("d2", issue=103)])
    assert len(ef.select_facts(data, query="#102")) == 1

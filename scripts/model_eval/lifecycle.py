"""Read-only public Orca CLI adapter; never inspect or mutate Orca's databases."""

from __future__ import annotations

import copy
import json
import os
import platform
import shutil
import subprocess
from datetime import datetime, timezone

from .collect_pure import dispatch_id, iso_ts, parse_ts


def read_orca(*args: str) -> dict:
    executable = os.environ.get("ORCA_CLI_COMMAND") or (
        "orca-dev"
        if os.environ.get("ORCA_DEV_REPO_ROOT")
        else "orca"
        if platform.system() == "Darwin"
        else "orca-ide"
    )
    if not shutil.which(executable):
        raise RuntimeError("orca_cli_unavailable")
    result = subprocess.run(
        [executable, *args, "--json"], capture_output=True, text=True, timeout=30, check=False
    )
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise RuntimeError("orca_cli_invalid_json_shape")
    if result.returncode or payload.get("ok") is not True:
        raise RuntimeError(
            "orca_cli:" + str(payload.get("error", {}).get("code", result.returncode))
        )
    return payload.get("result", {})


def lifecycle_fields(result: dict, tree: dict, observed_at: str, local_host: str) -> dict:
    """Only auditable lifecycle fields. A live PTY alone does not prove running work."""
    dispatch, worker = result.get("dispatch") or {}, result.get("worker") or {}
    resource = result.get("terminalResource") or {}
    start, end = iso_ts(dispatch.get("dispatchedAt")), iso_ts(dispatch.get("completedAt"))
    host = (dispatch.get("hostScope") or {}).get("hostId") or "unknown"
    if host == "local":
        host = local_host
    incarnation = dispatch.get("processIncarnation") or resource.get("endpointIncarnation")
    # Process incarnation is stable across reused terminal handles and dispatches.
    instance = f"{host}:{incarnation}" if incarnation else None
    launch = (worker.get("startOptions") or {}).get("launch") or {}
    requested, effective = launch.get("requested") or {}, launch.get("effective") or {}
    worktree_id = worker.get("worktreeId") or resource.get("worktreeId")
    path = tree.get("path") or (
        worktree_id.split("::", 1)[1] if worktree_id and "::" in worktree_id else None
    )
    created = tree.get("createdAt") or (tree.get("lineage") or {}).get("createdAt")
    if isinstance(created, (int, float)):
        created = datetime.fromtimestamp(created / 1000, timezone.utc).isoformat()
    # Current-path metadata cannot identify a dispatch that predates this lifecycle.
    stable = bool(
        created and start and parse_ts(created) is not None and parse_ts(created) <= parse_ts(start)
    )
    projection = result.get("projection") or {}
    running = (
        dispatch.get("status") == "dispatched"
        and (projection.get("liveness") or {}).get("verdict") == "live"
        and projection.get("outcome") == "in_progress"
    )
    return {
        "dispatch_id": dispatch.get("id"),
        "task_id": dispatch.get("taskId"),
        "run_id": dispatch.get("runId"),
        "retry_of": dispatch.get("retryOfDispatchId"),
        "worker_instance_id": instance,
        "worktree_id": worktree_id,
        "worktree_instance_id": tree.get("instanceId") if stable else None,
        "worktree_created_at": created if stable else None,
        "worktree_path": path,
        "worktree_name": tree.get("displayName") if stable or not created else None,
        "branch": tree.get("branch") if stable or not created else None,
        "repo_id": tree.get("projectId"),
        "host_id": host,
        "started_at": start,
        "ended_at": end,
        "observed_at": observed_at,
        "collected_at": observed_at,
        "running": running and not end,
        "outcome": dispatch.get("status"),
        "model_requested": requested.get("model"),
        "model_effective": effective.get("model"),
        "effort": effective.get("effort"),
        "agent": effective.get("agent"),
        "lifecycle_source": "orca.worker-show:v1",
    }


def enrich_dispatches(
    events: list[dict], *, read=read_orca, observed_at=None, local_host=None
) -> tuple[list[dict], list[str]]:
    observed_at = observed_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    local_host = local_host or platform.node()
    events = copy.deepcopy(events)
    warnings, trees = [], {}
    by_id = {dispatch_id(e): e for e in events if dispatch_id(e)}
    try:
        current = read("worktree", "current").get("worktree") or {}
        repo = current.get("repoId")
        cursor = None
        while repo:
            args = ["orchestration", "worker-list"]
            if cursor:
                args.extend(["--cursor", cursor])
            page = read(*args)
            if (page.get("scope") or {}).get("source") != "all":
                warnings.append("orca_discovery_scoped:other_runs_not_covered")
            for worker in page.get("workers", []):
                wid = (worker.get("resource") or {}).get("worktreeId") or ""
                did = worker.get("dispatchId")
                if wid.startswith(repo + "::") and did and did not in by_id:
                    event = {
                        "ts": None,
                        "type": "dispatch",
                        "issue": None,
                        "data": {"worker_id": did, "dispatch_id": did},
                    }
                    events.append(event)
                    by_id[did] = event
            pagination = page.get("page") or {}
            if not pagination.get("hasMore"):
                break
            next_cursor = pagination.get("nextCursor")
            if not next_cursor or next_cursor == cursor:
                warnings.append("orca_worker_page_incomplete")
                break
            cursor = next_cursor
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        warnings.append(f"orca_discovery_unavailable:{type(exc).__name__}")
    for event in events:
        data = event.setdefault("data", {})
        data["host_id"] = data.get("host_id") or local_host
        data["collected_at"] = observed_at
        did = dispatch_id(event)
        if not did:
            continue
        try:
            result = read("orchestration", "worker-show", "--dispatch", did)
            worker = result.get("worker") or {}
            wid = worker.get("worktreeId") or (result.get("terminalResource") or {}).get(
                "worktreeId"
            )
            if wid and wid not in trees:
                try:
                    trees[wid] = (
                        read("worktree", "show", "--worktree", "id:" + wid).get("worktree") or {}
                    )
                except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
                    trees[wid] = {}
            fields = lifecycle_fields(result, trees.get(wid, {}), observed_at, local_host)
            data.update({k: v for k, v in fields.items() if v is not None})
            event["ts"] = fields.get("started_at") or event.get("ts")
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            data["source_warnings"] = [f"lifecycle_unavailable:{type(exc).__name__}"]
    for event in events:
        event["data"].setdefault("source_warnings", []).extend(warnings)
    return events, warnings

#!/usr/bin/env python3
"""Finite cron front door: policy first, no refresh, shared durable reducer/transport.

Install only with the existing Hermes cron's no_agent=true. Output is observation,
never an instruction to start another executor. Agent work uses the two existing
authenticated routes, with the same admission and dedup as receiver work.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.hosting import HostingClient, HostingConfig, HostingError  # noqa: E402
from scripts.prod_agent import github_intake  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
ADAPTER = ROOT / "deploy/scripts/dc-notify-prod-agent.sh"


def policy_preflight() -> dict:
    """No network, process, claim or state write; safe before an approved token refresh."""
    if not github_intake.enabled():
        return {"admitted": False, "reason": "intake_off", "deployment_actionable": False}
    rule = github_intake.policy()
    return {"admitted": rule.has_targets(),
            "epoch": rule.epoch, "policy_sha256": rule.digest,
            "deployment_actionable": False}


def reconcile_status() -> dict:
    """Private observations only; neither delivery_unknown nor 202 releases a retry."""
    from scripts.prod_agent.pr_poll import _report_block

    db = Path(os.environ.get("DAILYCHECK_EVENT_REDUCER_DB",
                             "/var/lib/dailycheck-prod-agent/event_reducer.sqlite3"))
    unresolved = []
    if db.is_file():
        with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=10) as conn:
            unresolved = [{"trigger_id": row[0], "pr": row[1], "head": row[2], "status": row[3]}
                          for row in conn.execute("""SELECT trigger_id,pr_number,head_sha,last_error
                          FROM trigger_queue WHERE repo=? AND (state='claimed' OR last_error IN
                          ('awaiting_agent_receipt','delivery_unknown')) ORDER BY first_seen_ms LIMIT 100""",
                          (github_intake.REPO.lower(),))]
    return {"publication_alert": _report_block(), "dispatch_reconciliation": unresolved}


def main() -> int:
    status = {"deployment_actionable": False, "entrypoint": "github_dispatch",
              "intake": "off", "dispatches": []}
    try:
        if not github_intake.enabled():
            return 0
        rule = github_intake.policy()  # Must precede even API/curl/adapter invocation.
        status.update(intake="quarantined" if rule.pr_range is None else "automatic",
                      policy_sha256=rule.digest)
        if rule.has_targets():
            client = HostingClient(HostingConfig.from_environment())
            client.verify_repository()
            prs = client.pages(f"/repos/{client.config.repo}/pulls", params={"state": "open"})
            eligible = [pr for pr in prs if isinstance(pr, dict)
                        and rule.accepts(pr.get("number"), (pr.get("head") or {}).get("sha"))]
            if len(eligible) > 16:
                raise HostingError("ordinary_cron_batch_exceeds_16")
            env = dict(os.environ, DAILYCHECK_REDUCER_DEBOUNCE_MS="0")
            for pr in eligible:
                now = int(time.time() * 1000)
                head = pr["head"]["sha"]
                hint = github_intake.seal({"delivery_id": f"cron-{pr['number']}-{head}-{now}",
                    "event": "pull_request", "action": "synchronize", "source_event": "cron",
                    "received_at_ms": now, "repo_full_name": github_intake.REPO,
                    "pr_number": pr["number"], "pr_head_sha": head,
                    "check_conclusion": "success"}, "receiver")
                # Clients have finite request/page bounds. Reap the adapter itself;
                # an outer timeout killing only bash would leave late reducer claims.
                result = subprocess.run(["bash", str(ADAPTER)], input=json.dumps(hint),
                                        text=True, capture_output=True, env=env)
                decision = json.loads(result.stdout) if result.stdout else {}
                status["dispatches"].append({"pr": pr["number"], "head": head,
                    "native_rc": result.returncode,
                    "decision": {key: decision[key] for key in ("decision", "reason", "trigger_id") if key in decision}})
                if result.returncode not in {0, 75}:
                    raise HostingError("ordinary_cron_adapter_unconfirmed")
            # Drains already-admitted work too; stored signatures, current policy,
            # live H/CI, native fences and result provenance are rechecked in the queue.
            result = subprocess.run(["bash", str(ADAPTER), "--drain"], text=True,
                                    capture_output=True, env=env)
            status["drain_native_rc"] = result.returncode
            if result.returncode not in {0, 75}:
                raise HostingError("ordinary_cron_drain_unconfirmed")
        status.update(reconcile_status())
    except (HostingError, OSError, ValueError, TypeError, sqlite3.Error, subprocess.TimeoutExpired) as exc:
        status.update(intake="reconcile_required", error=str(exc) if isinstance(exc, HostingError)
                      else type(exc).__name__)
        # Never refresh/retry on an unknown invocation. Existing job logs remain observable.
        print(json.dumps(status, sort_keys=True))
        return 1
    if status["dispatches"] or status.get("publication_alert") or status.get("dispatch_reconciliation"):
        print(json.dumps(status, sort_keys=True))
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--policy-preflight"]:
        try:
            result = policy_preflight()
        except HostingError:
            result = {"admitted": False, "reason": "invalid_intake_policy", "deployment_actionable": False}
        print(json.dumps(result, sort_keys=True))
        raise SystemExit(0 if result["admitted"] else 78)
    if sys.argv[1:]:
        raise SystemExit(64)
    raise SystemExit(main())

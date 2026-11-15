"""Bound report -> Issue reconciliation -> explicit local observation receipt."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.blue_green import deploy_state, publication_authority  # noqa: E402
from scripts.blue_green.release_issues import reconcile  # noqa: E402
from scripts.hosting import HostingError  # noqa: E402


def main():
    selector_path, *args = sys.argv[1:]
    spec = importlib.util.spec_from_file_location("_finish_selector", selector_path)
    selector = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(selector)
    selector.runtime()
    selector.regular(__file__, selector.PROFILE["pins"]["scripts/blue_green/release_finish.py"])
    selector.regular(
        Path(__file__).with_name("release_issues.py"),
        selector.PROFILE["pins"]["scripts/blue_green/release_issues.py"],
    )
    profile = selector.PROFILE
    binding = selector.late_binding("typed-report", args)
    # Retain the returned Popen before any postlaunch operation that can fail.
    child = None
    try:
        child = subprocess.Popen([profile["python"], "-B", *args], pass_fds=(binding["fence_fd"],))
    finally:
        if child is not None:
            while True:
                try:
                    report_rc = child.wait()
                    break
                except (KeyboardInterrupt, InterruptedError):
                    continue
    try:
        os.waitpid(child.pid, os.WNOHANG)
    except ChildProcessError:
        pass
    else:
        raise HostingError("report_reap_unproved")
    receipt = {
        "version": 1,
        "pr": profile["pr"],
        "head": profile["head"],
        "report_native_rc": report_rc,
        "report_positive_wait": True,
        "report_actual_ECHILD": True,
        "remote_orca_push": False,
        "status": "UNKNOWN",
        "issue_results": [],
        "no_replay": True,
    }
    rc = 29
    try:
        ident, outcome, generation = publication_authority.load_durable_outcome(
            Path(profile["state"]), profile["pr"], profile["head"]
        )
        receipt.update(
            outcome_id=ident,
            generation=generation,
            deploy_rc=outcome["rc"],
            deploy_stage=outcome["stage"],
            public_receipt=outcome["public_receipt"],
        )
        if outcome["rc"] != 0:
            receipt["status"] = "deploy_failed"
            receipt["first_cause"] = {"stage": outcome["stage"], "rc": outcome["rc"]}
        state = deploy_state.load_state(Path(profile["state"]), strict=True)
        publication = state.get("publications", {}).get(ident, {})
        receipt.update(
            publication_state=publication.get("state", "unconfirmed"),
            report_comment=publication.get("comment_id", 0),
        )
        rc = outcome["rc"] or (29 if report_rc != 0 else 0)
        if report_rc != 0 or publication.get("state") != "linked":
            receipt.setdefault("first_cause", "report-unconfirmed")
            receipt["report_error"] = "report-unconfirmed"
            rc = rc or 29
        elif outcome["rc"] != 0:
            receipt["status"] = "deploy_failed"
        else:
            receipt["issue_results"] = reconcile(profile)
            receipt["status"] = "delivered"
    except (HostingError, OSError, ValueError, TypeError, KeyError) as exc:
        receipt["reconciliation_error"] = type(exc).__name__
        # These are closed local protocol codes, never an HTTP body or private diagnostic.
        reason = (
            str(exc)
            if isinstance(exc, HostingError)
            and str(exc)
            in {
                "issue_credential_custody",
                "issue_grant_invalid",
                "issue_scope_forbidden",
                "issue_grant_expired",
                "issue_repository_mismatch",
                "issue_protocol_missing",
                "issue_source_transport_unapproved",
                "issue_targets_missing",
                "issue_target_invalid",
                "issue_target_mismatch",
                "issue_identity_changed",
                "issue_close_unknown",
                "issue_process_fence_missing",
                "issue_delivery_not_proved",
                "issue_merge_not_proved",
                "issue_close_uncertain_no_replay",
            }
            else type(exc).__name__
        )
        receipt.setdefault("first_cause", {"stage": "issue-reconcile", "reason": reason})
        rc = rc or 30
    raw = (json.dumps(receipt, sort_keys=True) + "\n").encode()
    try:
        fd = os.open(
            Path(profile["root"]) / "completion-receipt.json",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(fd, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        selector.sync_directory(profile["root"])
    except OSError:
        rc = rc or 29
    os.write(1, raw)  # Explicit remote output; existing local tracker reads forge separately.
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

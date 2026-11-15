"""Declassified whole-run observation, under the existing native publication fence.

This is not deployment authority. It projects the current outcome/publication
and bound Issue resolutions so the existing forge-only tracker cannot mistake
core deployment success for successful whole-run completion.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.blue_green import deploy_state, publication_authority
from scripts.blue_green.release_issues import validate_protocol
from scripts.hosting import AccessRole, HostingClient, HostingConfig, HostingError

MARKER = "<!-- dailycheck-release-completion:v1 -->"


def emit(profile: dict, status: str) -> int:
    if status not in {"delivered", "deploy_failed", "UNKNOWN"}:
        raise HostingError("completion_status_invalid")
    directory = Path(profile["state"])
    fd = deploy_state.process_lock_fd()
    if fd is None:
        raise HostingError("completion_process_fence_missing")
    client = HostingClient(HostingConfig.from_environment(AccessRole.REPORT))
    with deploy_state.state_transaction(directory):
        ident, outcome, generation = publication_authority.load_durable_outcome(
            directory, profile["pr"], profile["head"]
        )
        binding = publication_authority.resolve_source_binding(directory, profile["pr"])
        state = deploy_state.load_state(directory, strict=True)
        publication = state.get("publications", {}).get(ident, {})
        if (
            generation != profile["generation"] + 1
            or binding.binding_sha256 != profile["binding_sha256"]
            or publication.get("state") != "linked"
            or publication.get("generation") != generation
            or publication.get("binding_sha256") != binding.binding_sha256
            or type(publication.get("comment_id")) is not int
            or publication["comment_id"] <= 0
        ):
            raise HostingError("completion_publication_not_proved")
        if status == "delivered":
            if (
                outcome["rc"] != 0
                or outcome["verification"] != "normal"
                or state.get("deployed", {}).get("sha") != profile["head"]
                or state["deployed"].get("outcome_id") != ident
            ):
                raise HostingError("completion_success_not_proved")
            resolutions = publication.get("issue_resolutions", {})
            for target in validate_protocol(profile["issue_protocol"], binding):
                row = resolutions.get(f"{target['repository_id']}:{target['number']}", {})
                if row != {"state": "closed", "issue_id": target["issue_id"]}:
                    raise HostingError("completion_issues_not_proved")
        if status == "deploy_failed" and outcome["rc"] == 0:
            raise HostingError("completion_failure_not_proved")
        facts = {
            "pr": profile["pr"],
            "head": profile["head"],
            "public_receipt": outcome["public_receipt"],
            "status": status,
        }
        body = MARKER + "\n" + json.dumps(facts, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(body.encode()).hexdigest()
        existing = publication.get("completion_feedback")
        if existing is not None:
            if (
                existing.get("state") == "linked"
                and existing.get("payload_sha256") == digest
                and type(existing.get("comment_id")) is int
                and existing["comment_id"] > 0
            ):
                return existing["comment_id"]
            raise HostingError("completion_feedback_uncertain_no_replay")
        publication["completion_feedback"] = {
            "state": "uncertain",
            "payload_sha256": digest,
            "status": status,
            "comment_id": 0,
        }
        deploy_state.save_state(directory, state)  # Irreversible intent before HTTP.
        result, _ = client.request(
            "POST",
            f"/repos/{client.config.repo}/issues/{profile['pr']}/comments",
            {"body": body},
            guard_fd=fd,
        )
        if (
            not isinstance(result, dict)
            or type(result.get("id")) is not int
            or result["id"] <= 0
            or result.get("body") != body
            or result.get("issue_url")
            != f"https://api.github.com/repos/{client.config.repo}/issues/{profile['pr']}"
        ):
            raise HostingError("completion_feedback_unknown")
        # Same held transaction and process exclusion; no successor can relabel it.
        publication["completion_feedback"].update(state="linked", comment_id=result["id"])
        deploy_state.save_state(directory, state)
        return result["id"]

"""Private authenticated ordinary hints; neither deployment nor publication authority.

Only the receiver, readiness reducer and durable reporter mint these envelopes.
The explicit epoch and finite exact PR/H admission apply again at every consumer.
Old shadow records have no valid envelope and can never be upgraded by configuration.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from scripts.hosting import (
    HostingError,
    Provider,
    github_ordinary_enabled,
    provider,
    strict_json_loads,
)
from scripts.hosting import (
    require_production_work as legacy_require_production_work,
)

REPO = "DUIDUI-Studio/DailyCheck"
REPOSITORY_ID = 1448986421
STAGES = frozenset({"receiver", "ready", "outcome"})


@dataclass(frozen=True)
class Admission:
    epoch: int
    targets: frozenset[tuple[int, str]]
    pr_range: tuple[int, int] | None
    excluded: frozenset[int]
    review_actors: frozenset[int]
    digest: str
    key: bytes = field(repr=False)

    def accepts(self, pr: int, head: str) -> bool:
        return (type(pr) is int and isinstance(head, str)
                and re.fullmatch(r"[0-9a-f]{40}", head) is not None and pr not in self.excluded
                and ((pr, head) in self.targets or self.pr_range is not None
                     and self.pr_range[0] <= pr <= self.pr_range[1]))

    def has_targets(self) -> bool:
        return (any(self.accepts(pr, head) for pr, head in self.targets)
                or self.pr_range is not None
                and any(pr not in self.excluded for pr in range(self.pr_range[0], self.pr_range[1] + 1)))


def enabled() -> bool:
    return github_ordinary_enabled() and os.environ.get("DAILYCHECK_GITHUB_INTAKE_ENABLED") == "1"


def _private_read(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                or info.st_uid != os.geteuid()):
            raise ValueError("private_intake_file_required")
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("intake_file_too_large")
    return raw


def policy() -> Admission:
    try:
        if not enabled():
            raise ValueError
        path = os.environ.get("DAILYCHECK_GITHUB_INTAKE_POLICY_FILE")
        if path:
            document = strict_json_loads(_private_read(Path(path), 16384))
        else:
            # Initial quarantine compatibility; automatic ranges require an owner-protected file.
            document = {"version": 1, "repository_id": REPOSITORY_ID, "repository": REPO,
                "mode": "quarantined", "since_ms": int(os.environ["DAILYCHECK_GITHUB_INTAKE_SINCE_MS"]),
                "review_targets": strict_json_loads(os.environ.get("DAILYCHECK_GITHUB_INTAKE_TARGETS", "[]"),
                                                   max_bytes=4096),
                "automatic_pr_range": None, "excluded_open_prs": [15, 17, 18], "review_actor_ids": []}
        if (not isinstance(document, dict) or set(document) != {"version", "repository_id", "repository",
                "mode", "since_ms", "review_targets", "automatic_pr_range", "excluded_open_prs", "review_actor_ids"}
                or type(document["version"]) is not int or document["version"] != 1
                or type(document["repository_id"]) is not int or document["repository_id"] != REPOSITORY_ID
                or document["repository"] != REPO or document["mode"] not in {"quarantined", "automatic"}
                or type(document["since_ms"]) is not int):
            raise ValueError
        epoch = document["since_ms"]
        if not 0 < epoch <= int(time.time() * 1000):
            raise ValueError
        rows = document["review_targets"]
        if not isinstance(rows, list) or len(rows) > 16:
            raise ValueError
        targets = set()
        for row in rows:
            if (not isinstance(row, dict) or set(row) != {"pr", "head"}
                    or type(row["pr"]) is not int or not 0 < row["pr"] <= 2_147_483_647
                    or not isinstance(row["head"], str)
                    or not re.fullmatch(r"[0-9a-f]{40}", row["head"])):
                raise ValueError
            targets.add((row["pr"], row["head"]))
        if len(targets) != len(rows):
            raise ValueError
        excluded = document["excluded_open_prs"]
        if (not isinstance(excluded, list) or len(excluded) > 100
                or any(type(pr) is not int or pr <= 0 for pr in excluded)):
            raise ValueError
        actors = document["review_actor_ids"]
        if (not isinstance(actors, list) or len(actors) > 16
                or any(type(actor) is not int or actor <= 0 for actor in actors)
                or document["mode"] == "automatic" and not actors):
            raise ValueError
        pr_range = document["automatic_pr_range"]
        if pr_range is not None:
            if (document["mode"] != "automatic" or not isinstance(pr_range, dict)
                    or set(pr_range) != {"min", "max"}
                    or any(type(pr_range[k]) is not int for k in ("min", "max"))
                    or not 1 <= pr_range["min"] <= pr_range["max"] <= 2_147_483_647
                    or pr_range["max"] - pr_range["min"] > 10000):
                raise ValueError
            pr_range = (pr_range["min"], pr_range["max"])
        path = Path(os.environ["DAILYCHECK_GITHUB_INTAKE_KEY_FILE"])
        key = _private_read(path, 4096)
        if not 32 <= len(key) <= 4096:
            raise ValueError
        digest = hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return Admission(epoch, frozenset(targets), pr_range, frozenset(excluded), frozenset(actors), digest, key)
    except (OSError, KeyError, ValueError, TypeError) as exc:
        raise HostingError("ordinary_intake_not_admitted") from exc


def admitted(pr: int, head: str) -> bool:
    try:
        return policy().accepts(pr, head)
    except HostingError:
        return False


def _mac(payload: dict, stage: str, epoch: int, key: bytes) -> str:
    raw = json.dumps(["dailycheck-ordinary-v1", stage, epoch, payload], sort_keys=True,
                     ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode()
    return hmac.new(key, raw, hashlib.sha256).hexdigest()


def seal(payload: dict, stage: str) -> dict:
    rule = policy()
    body = {k: v for k, v in payload.items() if k != "ordinary_provenance"}
    if (stage not in STAGES or not rule.accepts(body.get("pr_number"), body.get("pr_head_sha"))
            or body.get("repo_full_name") != REPO
            or type(body.get("received_at_ms")) is not int or body["received_at_ms"] < rule.epoch):
        raise HostingError("ordinary_intake_not_admitted")
    body.update(hosting_provider="github", intake_mode="ordinary", hosting_host="github.com",
                repository_id=REPOSITORY_ID, policy_sha256=rule.digest,
                dispatch_phase="result_reconcile" if stage == "outcome" else "prod_review")
    binding = f"{rule.digest}|{body['pr_number']}|{body['pr_head_sha']}|{body['dispatch_phase']}"
    body["admission_id"] = hashlib.sha256(binding.encode()).hexdigest()
    return {**body, "ordinary_provenance": {"version": 1, "stage": stage, "epoch": rule.epoch,
                                           "mac": _mac(body, stage, rule.epoch, rule.key)}}


def authentic(payload: object, stages: frozenset[str] = STAGES) -> bool:
    try:
        rule = policy()
        if not isinstance(payload, dict):
            return False
        body = {k: v for k, v in payload.items() if k != "ordinary_provenance"}
        proof = payload.get("ordinary_provenance")
        if (not isinstance(proof, dict) or set(proof) != {"version", "stage", "epoch", "mac"}
                or type(proof["version"]) is not int or proof["version"] != 1
                or type(proof["epoch"]) is not int or proof["epoch"] != rule.epoch
                or proof["stage"] not in stages
                or not rule.accepts(body.get("pr_number"), body.get("pr_head_sha"))
                or body.get("policy_sha256") != rule.digest
                or body.get("repo_full_name") != REPO or body.get("hosting_host") != "github.com"
                or type(body.get("repository_id")) is not int
                or body["repository_id"] != REPOSITORY_ID
                or body.get("hosting_provider") != "github" or body.get("intake_mode") != "ordinary"
                or type(body.get("received_at_ms")) is not int
                or not rule.epoch <= body["received_at_ms"] <= int(time.time() * 1000)
                or not isinstance(proof["mac"], str)):
            return False
        return hmac.compare_digest(proof["mac"], _mac(body, proof["stage"], rule.epoch, rule.key))
    except (HostingError, ValueError, TypeError, KeyError):
        return False


def work_payload(value: object) -> object:
    if isinstance(value, dict) and value.get("decision") in {"trigger", "defer"}:
        return value.get("payload")
    return value


def require_work(value: object = None) -> None:
    """Only the new ordinary consumers use this guard; the pinned hosting guard is unchanged."""
    if provider() is Provider.GITHUB and enabled():
        if value is None:
            policy()
            return
        if authentic(work_payload(value)):
            return
    legacy_require_production_work(value)


def readiness(payload: dict) -> dict:
    """Re-query truth; native custody/failed-H barriers are never cleared here."""
    from scripts.blue_green import deploy_state
    from scripts.hosting import HostingClient, HostingConfig
    from scripts.prod_agent.github_readiness import GitHubReadinessClient

    try:
        if not admitted(payload["pr_number"], payload["pr_head_sha"]):
            raise HostingError("ordinary_intake_not_admitted")
        state = deploy_state.load_state(deploy_state.state_dir(), strict=True)
        if not state.get("deployed") or deploy_state.native_blocked(deploy_state.state_dir(), state):
            raise HostingError("ordinary_reconcile_needed")
        if state.get("lock") is not None:
            raise HostingError("ordinary_owner_or_custody_unresolved")
        # Positive kernel exclusion check; TTL/empty process output never releases custody.
        with deploy_state.attempt_lock(deploy_state.state_dir()):
            pass
        blocked = deploy_state.failed_head_status(state, payload["pr_number"], payload["pr_head_sha"], {
            "provider": "github", "host": "github.com", "repository": REPO,
            "pr": payload["pr_number"]})
        if blocked:
            raise HostingError(blocked)
        return GitHubReadinessClient(HostingClient(HostingConfig.from_environment())).check(
            payload["pr_number"], payload["pr_head_sha"], repo=REPO, for_intake=True)
    except (HostingError, OSError, ValueError, KeyError, TypeError) as exc:
        return {"ready": False, "reason": str(exc) if isinstance(exc, HostingError)
                else "ordinary_state_unavailable"}


def outcome_facts(pr: int, head: str) -> dict:
    """Resolve current durable result/binding, never caller-supplied success claims."""
    from scripts.blue_green import deploy_state, publication_authority

    rule = policy()
    if not rule.accepts(pr, head):
        raise HostingError("ordinary_intake_not_admitted")
    directory = deploy_state.state_dir()
    with deploy_state.state_transaction(directory):
        ident, outcome, generation = publication_authority.load_durable_outcome(directory, pr, head)
        stamp = int(datetime.strptime(outcome["outcome_at"], "%Y-%m-%dT%H:%M:%SZ")
                    .replace(tzinfo=timezone.utc).timestamp() * 1000)
        if stamp < rule.epoch or outcome.get("candidate") is not None:
            raise HostingError("historical_or_nonordinary_outcome")
        binding = publication_authority.resolve_source_binding(directory, pr)
        state = deploy_state.load_state(directory, strict=True)
        publication = state.get("publications", {}).get(ident, {})
        linked = (publication.get("state") == "linked"
                  and publication.get("generation") == generation
                  and publication.get("binding_sha256") == binding.binding_sha256
                  and type(publication.get("comment_id")) is int
                  and publication["comment_id"] > 0)
        return {"outcome_id": ident, "generation": generation,
                "public_receipt": outcome["public_receipt"], "outcome_at": outcome["outcome_at"],
                "source_binding_sha256": binding.binding_sha256,
                "deploy_rc": outcome["rc"], "deploy_stage": outcome["stage"],
                "deploy_conclusion": "success" if outcome["rc"] == 0 else "failure",
                "publication_state": "linked" if linked else "unconfirmed",
                "deploy_report_comment": publication["comment_id"] if linked else 0}


def current_outcome(payload: dict) -> bool:
    try:
        current = outcome_facts(payload["pr_number"], payload["pr_head_sha"])
        # Publication may advance from unconfirmed to linked after enqueue; outcome identity may not.
        return all(payload.get(key) == value for key, value in current.items()
                   if key not in {"publication_state", "deploy_report_comment"})
    except (HostingError, OSError, ValueError, KeyError, TypeError):
        return False


def review_completion(payload: dict) -> dict | None:
    """A formal pinned-actor exact dispatched-H review ends review waiting only."""
    from scripts.hosting import HostingClient, HostingConfig
    from scripts.prod_agent.github_readiness import Candidate, GitHubReadinessClient

    try:
        rule = policy()
        if not rule.review_actors or not authentic(payload, frozenset({"ready"})):
            return None
        if (payload.get("dispatch_phase") != "prod_review"
                or type(payload.get("dispatch_claimed_at_ms")) is not int
                or not payload.get("trigger_id") or not payload.get("admission_id")):
            return None
        client = HostingClient(HostingConfig.from_environment())
        client.verify_repository()
        root = f"/repos/{REPO}"
        pr = client.get(f"{root}/pulls/{payload['pr_number']}")
        author = pr["user"]["id"]
        if (type(author) is not int or author <= 0 or type(pr.get("number")) is not int
                or pr.get("number") != payload["pr_number"]
                or type(pr["base"]["repo"]["id"]) is not int or pr["base"]["repo"]["id"] != REPOSITORY_ID
                or pr["base"]["repo"]["full_name"] != REPO):
            return None
        reader = GitHubReadinessClient(client)
        candidate = Candidate(payload["pr_number"], payload["pr_head_sha"], payload["pr_base_sha"], "")
        workflow = client.get(f"{root}/actions/workflows/ci.yml")
        if (workflow.get("path") != client.config.workflow_path or workflow.get("state") != "active"
                or type(workflow.get("id")) is not int):
            return None
        run = reader._latest_run(candidate, workflow["id"])
        if (run["id"] != payload["observed_ci_run_id"]
                or run["run_attempt"] != payload["observed_ci_attempt"]
                or run.get("status") != "completed" or run.get("conclusion") != "success"):
            return None
        matches = []
        for row in client.pages(f"{root}/pulls/{payload['pr_number']}/reviews"):
            if (not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] <= 0
                    or not isinstance(row.get("user"), dict)
                    or type(row["user"].get("id")) is not int):
                return None
            if (row["user"]["id"] not in rule.review_actors or row["user"]["id"] == author
                    or row["user"].get("type") not in {"User", "Bot"}
                    or row.get("state") not in {"APPROVED", "CHANGES_REQUESTED"}
                    or row.get("commit_id") != candidate.head):
                continue
            stamp = row.get("submitted_at")
            if not isinstance(stamp, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", stamp):
                return None
            submitted = int(datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
                            .replace(tzinfo=timezone.utc).timestamp() * 1000)
            if payload["dispatch_claimed_at_ms"] <= submitted <= int(time.time() * 1000):
                matches.append({"review_id": row["id"], "actor_id": row["user"]["id"],
                                "head": candidate.head, "state": row["state"], "submitted_at": stamp})
        if reader._latest_run(candidate, workflow["id"]) != run or not matches:
            return None
        # Every match is bound above; a blocker remains conservative when multiple actors reviewed.
        matches.sort(key=lambda row: (row["state"] == "CHANGES_REQUESTED", row["review_id"]))
        return {**matches[-1], "trigger_id": payload["trigger_id"], "admission_id": payload["admission_id"],
                "observed_ci_run_id": run["id"], "observed_ci_attempt": run["run_attempt"]}
    except (HostingError, OSError, ValueError, TypeError, KeyError):
        return None


def claim_send(payload: dict, route: str) -> bool:
    """One irreversible transport intent per durable claim, including lost responses."""
    path = Path(os.environ.get("DAILYCHECK_EVENT_REDUCER_DB",
                               "/var/lib/dailycheck-prod-agent/event_reducer.sqlite3")).absolute()
    try:
        with sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=10) as conn:
            conn.execute("BEGIN IMMEDIATE")
            encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
            updated = conn.execute("""UPDATE trigger_queue SET last_error='dispatch_sending'
                WHERE trigger_id=? AND state='claimed' AND route=? AND payload_json=? AND last_error=''""",
                (payload.get("trigger_id"), route, encoded)).rowcount
            return updated == 1
    except (OSError, ValueError, sqlite3.Error):
        return False

#!/usr/bin/env python3
"""Issue163 —— durable deployment outcome → trusted publication adapter.

The 162 layer already defines `PublicationContext`, `PublicationAuthority`,
`PublicationClaim` and `publish_public` in `deploy_report.py`, but no real
adapter existed: the CLI registered `cmd_write` with `authority=None` and every
GitHub publication failed closed.

This module is the concrete private adapter. It deliberately reuses the two
fences that already exist in `deploy_state.py`:

* the inherited `.attempt.lock` open descriptor (OS flock) proves that a live
  normal pipeline attempt owns the process, and a deliberate post-exit retry
  takes that same descriptor exclusion itself;
* `.state.lock` remains a short read-modify-write transaction around the
  durable outcome and the mutable per-outcome publication record.

No new database, append-only protocol, lock family, queue or service is added.
The `bindings.json` file is declarative private operator configuration: it names
the public code sink and the provider-qualified private source tuple, and it
grants neither deployment success nor reviewer approval. It is never created by
code; a missing or malformed file blocks the public POST while the private
outcome/diagnostics survive.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import deploy_state

from scripts.hosting import HostingError

BINDINGS_FILENAME = "bindings.json"
SOURCE_KEYS = ("provider", "host", "repository_id", "repository", "kind", "number")
PUBLIC_KEYS = ("provider", "host", "repository_id", "repository")
GITHUB_SOURCE_IDENTITY = ("github.com", 1449125557, "DUIDUI-Studio/DailyCheck-ops")
OUTCOME_FIELDS = (
    "attempt_id", "generation", "pr", "sha", "hosting", "deployment", "result",
    "rc", "stage", "verification", "outcome_at", "public_receipt", "created_at",
)
PUBLICATION_STATES = {"ready", "uncertain", "linked"}
DEPLOYMENT_KEYS = {"provider", "host", "repository", "pr"}


def _valid_deployment(value: object, pr: int) -> bool:
    return (isinstance(value, dict) and set(value) == DEPLOYMENT_KEYS
            and value.get("provider") in {"github", "gitea"}
            and value.get("pr") == pr
            and isinstance(value.get("host"), str)
            and re.fullmatch(r"[A-Za-z0-9.:-]+", value["host"]) is not None
            and isinstance(value.get("repository"), str)
            and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
                             value["repository"]) is not None)


def require_public_deployment(outcome: dict, pr: int) -> dict:
    """The actual deployment provenance must be this GitHub code PR.

    A Gitea (or unknown) deployment cannot be relabelled as GitHub authority by
    an integer PR/full-H coincidence plus a metadata-only binding. Missing or
    legacy unproven provenance fails closed; it never defaults to GitHub.
    """
    deployment = outcome.get("deployment")
    if not _valid_deployment(deployment, pr):
        raise HostingError("deployment_provenance_missing")
    if (deployment["provider"], deployment["host"], deployment["repository"]) != (
            "github", "github.com", deploy_state.PUBLIC_CODE_HOSTING["repository"]):
        raise HostingError("deployment_provider_not_public_github")
    return deployment


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class SourceBinding:
    pr: int
    source: tuple[str, str, int, str, str, int]
    binding_sha256: str
    source_revision: dict | None = None


# ── durable outcome ───────────────────────────────────────────────────────

def _validate_outcome(attempt_id: str, outcome: object, generation: int) -> dict:
    if not isinstance(outcome, dict):
        raise HostingError("durable_outcome_missing")
    for key in OUTCOME_FIELDS:
        if key not in outcome:
            raise HostingError("durable_outcome_malformed")
    if (outcome.get("attempt_id") != attempt_id
            or type(outcome.get("generation")) is not int
            or outcome["generation"] != generation
            or type(outcome.get("pr")) is not int or outcome["pr"] <= 0
            or not isinstance(outcome.get("sha"), str)
            or re.fullmatch(r"[0-9a-f]{40}", outcome["sha"]) is None
            or outcome.get("hosting") != deploy_state.PUBLIC_CODE_HOSTING
            or outcome.get("result") not in {
                deploy_state.OUTCOME_RESULT_SUCCESS, deploy_state.OUTCOME_RESULT_FAILED}
            or type(outcome.get("rc")) is not int or not 0 <= outcome["rc"] <= 255
            or (outcome["rc"] == 0) != (outcome["result"] == deploy_state.OUTCOME_RESULT_SUCCESS)
            or not isinstance(outcome.get("stage"), str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,31}",
                            outcome["stage"]) is None):
        raise HostingError("durable_outcome_malformed")
    if not _valid_deployment(outcome.get("deployment"), outcome["pr"]):
        raise HostingError("deployment_provenance_missing")
    native = outcome.get("candidate")
    if native is not None:
        try:
            manifest = native["manifest"]
            subject = manifest["members"][-1]
            if (native["kind"] != "github_merge_group" or native["version"] != 1
                    or manifest["subject_pr_number"] != outcome["pr"]
                    or subject["number"] != outcome["pr"]
                    or manifest["group_sha"] != outcome["sha"]
                    or re.fullmatch(r"[0-9a-f]{40}", subject["head_sha"]) is None
                    or re.fullmatch(r"[0-9a-f]{40}", manifest["creation_base_sha"]) is None
                    or re.fullmatch(r"[0-9a-f]{64}", outcome["native_evidence_sha256"]) is None):
                raise ValueError
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            raise HostingError("durable_native_outcome_malformed") from exc
    try:
        if str(uuid.UUID(outcome["public_receipt"])) != outcome["public_receipt"]:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise HostingError("durable_outcome_malformed") from exc
    stamp = outcome.get("outcome_at")
    if not isinstance(stamp, str) or re.fullmatch(
            r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", stamp) is None:
        raise HostingError("durable_outcome_malformed")
    return outcome


def load_durable_outcome(directory: Path, pr: int, sha: str,
                         *, require_public_github: bool = True) -> tuple[str, dict, int]:
    """Resolve the current immutable outcome for (pr, full SHA).

    Read-only. The publication claim revalidates generation/identity under the
    state transaction before any HTTP. Missing, foreign, superseded or
    non-GitHub deployment provenance fails closed.
    """
    state = deploy_state.load_state(directory)
    try:
        generation = int(state.get("generation") or 0)
    except (TypeError, ValueError) as exc:
        raise HostingError("durable_outcome_malformed") from exc
    outcomes = state.get("outcomes")
    if not isinstance(outcomes, dict) or generation < 1:
        raise HostingError("durable_outcome_missing")
    matches = [
        (attempt_id, outcome)
        for attempt_id, outcome in outcomes.items()
        if isinstance(outcome, dict)
        and outcome.get("pr") == pr
        and public_head(outcome) == sha
        and outcome.get("generation") == generation
    ]
    if not matches:
        raise HostingError("durable_outcome_missing")
    if len(matches) > 1:
        raise HostingError("durable_outcome_ambiguous")
    attempt_id, outcome = matches[0]
    outcome = _validate_outcome(attempt_id, outcome, generation)
    if require_public_github:
        require_public_deployment(outcome, pr)
    return attempt_id, outcome, generation


def public_head(outcome: dict) -> str:
    native = outcome.get("candidate")
    if native is None:
        return outcome.get("sha")
    try:
        return native["manifest"]["members"][-1]["head_sha"]
    except (KeyError, TypeError, IndexError) as exc:
        raise HostingError("durable_native_outcome_malformed") from exc


def freeze_public_facts(directory: Path, attempt_id: str, generation: int,
                        facts: dict, payload_sha256: str) -> tuple[dict, str]:
    """Freeze the first valid typed projection for this outcome, once."""
    with deploy_state.state_transaction(directory):
        state = deploy_state.load_state(directory)
        if state.get("generation") != generation:
            raise HostingError("durable_outcome_superseded")
        outcomes = state.get("outcomes")
        if not isinstance(outcomes, dict) or attempt_id not in outcomes:
            raise HostingError("durable_outcome_missing")
        outcome = _validate_outcome(attempt_id, outcomes[attempt_id], generation)
        frozen = outcome.get("facts")
        if frozen is not None:
            stored = outcome.get("payload_sha256")
            if (not isinstance(frozen, dict)
                    or not isinstance(stored, str)
                    or re.fullmatch(r"[0-9a-f]{64}", stored) is None):
                raise HostingError("durable_outcome_malformed")
            return dict(frozen), stored
        if not isinstance(facts, dict) or not facts:
            raise HostingError("public_facts_invalid")
        if re.fullmatch(r"[0-9a-f]{64}", payload_sha256 or "") is None:
            raise HostingError("public_facts_invalid")
        outcome["facts"] = dict(facts)
        outcome["payload_sha256"] = payload_sha256
        deploy_state.save_state(directory, state)
        return dict(facts), payload_sha256


# ── declarative source binding ────────────────────────────────────────────

def _binding_path(directory: Path) -> Path:
    return Path(directory) / BINDINGS_FILENAME


def _validate_binding_entry(entry: object) -> dict:
    if (not isinstance(entry, dict)
            or set(entry) not in ({"pr", "public", "source"}, {"pr", "public", "source", "source_revision"})):
        raise HostingError("private_source_binding_invalid")
    source = entry["source"]
    public = entry["public"]
    if (type(entry["pr"]) is not int or entry["pr"] <= 0
            or not isinstance(public, dict) or set(public) != set(PUBLIC_KEYS)
            or not isinstance(source, dict) or set(source) != set(SOURCE_KEYS)):
        raise HostingError("private_source_binding_invalid")
    if public != deploy_state.PUBLIC_CODE_HOSTING:
        raise HostingError("private_source_binding_invalid")
    provider, host, rid, repo, kind, number = (source[key] for key in SOURCE_KEYS)
    if (provider not in {"gitea", "github"} or kind != "issue"
            or type(rid) is not int or rid <= 0
            or type(number) is not int or number <= 0
            or not isinstance(host, str) or re.fullmatch(r"[A-Za-z0-9.:_-]+", host) is None
            or not isinstance(repo, str)
            or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) is None):
        raise HostingError("private_source_binding_invalid")
    if provider == "github" and (host, rid, repo) != GITHUB_SOURCE_IDENTITY:
        raise HostingError("private_source_binding_invalid")
    if "source_revision" in entry:
        revision = entry["source_revision"]
        if (not isinstance(revision, dict) or set(revision) != {
                "provider", "host", "repository_id", "repository", "sha", "source_public_map_sha256"}
                or revision["provider"] not in {"gitea", "github"}
                or type(revision["repository_id"]) is not int or revision["repository_id"] <= 0
                or not isinstance(revision["host"], str)
                or re.fullmatch(r"[A-Za-z0-9.:_-]+", revision["host"]) is None
                or not isinstance(revision["repository"], str)
                or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", revision["repository"]) is None
                or not isinstance(revision["sha"], str)
                or re.fullmatch(r"[0-9a-f]{40}", revision["sha"]) is None
                or not isinstance(revision["source_public_map_sha256"], str)
                or re.fullmatch(r"[0-9a-f]{64}", revision["source_public_map_sha256"]) is None):
            raise HostingError("private_revision_binding_invalid")
    return entry


def resolve_source_binding(directory: Path, pr: int) -> SourceBinding:
    """Read/validate the private mapping under the caller's existing state lock."""
    path = _binding_path(directory)
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise HostingError("private_source_binding_missing") from exc
    except OSError as exc:
        raise HostingError("private_source_binding_invalid") from exc
    try:
        document = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise HostingError("private_source_binding_invalid") from exc
    if (not isinstance(document, dict) or set(document) != {"version", "bindings"}
            or type(document["version"]) is not int or document["version"] < 1
            or not isinstance(document["bindings"], list)):
        raise HostingError("private_source_binding_invalid")
    entries = [_validate_binding_entry(entry) for entry in document["bindings"]]
    matches = [entry for entry in entries if entry["pr"] == pr]
    if not matches:
        raise HostingError("private_source_binding_missing")
    if len(matches) > 1:
        raise HostingError("private_source_binding_ambiguous")
    entry = matches[0]
    digest = hashlib.sha256(
        _canonical({"version": document["version"], "binding": entry}).encode()
    ).hexdigest()
    source = tuple(entry["source"][key] for key in SOURCE_KEYS)
    return SourceBinding(pr=pr, source=source, binding_sha256=digest,
                         source_revision=entry.get("source_revision"))


# ── adapter ───────────────────────────────────────────────────────────────

class DurablePublicationClaim:
    """Mutable per-outcome intent/comment mapping in the private state domain."""

    def __init__(self, directory: Path, attempt_id: str, generation: int,
                 context, payload_sha256: str, binding_sha256: str,
                 inherited: bool, fd: int | None):
        self._directory = directory
        self._attempt_id = attempt_id
        self._generation = generation
        self._context = context
        self._payload_sha256 = payload_sha256
        self._binding_sha256 = binding_sha256
        self._inherited = inherited
        self._fd = fd
        self.state = "ready"
        self.comment_id = 0

    # -- fencing ---------------------------------------------------------
    def _fence_ok(self) -> None:
        if self._inherited:
            if deploy_state.process_lock_fd() is None:
                raise HostingError("deployment_fence_lost")
            return
        if self._fd is None:
            raise HostingError("deployment_fence_lost")
        try:
            os.fstat(self._fd)
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise HostingError("deployment_fence_lost") from exc

    # -- validation ------------------------------------------------------
    def _validate(self, state: dict) -> dict | None:
        if state.get("generation") != self._generation:
            raise HostingError("durable_outcome_superseded")
        outcomes = state.get("outcomes")
        if not isinstance(outcomes, dict) or self._attempt_id not in outcomes:
            raise HostingError("durable_outcome_missing")
        outcome = _validate_outcome(
            self._attempt_id, outcomes[self._attempt_id], self._generation)
        require_public_deployment(outcome, self._context.pr)
        if (outcome.get("pr") != self._context.pr
                or public_head(outcome) != self._context.sha
                or outcome.get("public_receipt") != self._context.public_receipt
                or outcome.get("outcome_at") != self._context.outcome_at
                or outcome.get("payload_sha256") != self._payload_sha256
                or outcome.get("facts") is None):
            raise HostingError("durable_outcome_mismatch")
        if outcome.get("candidate") is not None:
            facts = outcome["facts"]
            manifest = outcome["candidate"]["manifest"]
            if (facts.get("version") != 2 or facts.get("sha") != public_head(outcome)
                    or facts.get("deployment_sha") != outcome["sha"]
                    or facts.get("creation_base_sha") != manifest["creation_base_sha"]):
                raise HostingError("durable_native_projection_mismatch")

        binding = resolve_source_binding(self._directory, self._context.pr)
        if binding.binding_sha256 != self._binding_sha256:
            raise HostingError("source_binding_changed")
        if binding.source != self._context.private_source:
            raise HostingError("source_binding_mismatch")

        publications = state.get("publications")
        if not isinstance(publications, dict):
            raise HostingError("publication_state_malformed")
        record = publications.get(self._attempt_id)
        if record is None:
            return None
        if (not isinstance(record, dict)
                or record.get("generation") != self._generation
                or record.get("payload_sha256") != self._payload_sha256
                or record.get("binding_sha256") != self._binding_sha256
                or record.get("state") not in PUBLICATION_STATES):
            raise HostingError("publication_state_malformed")
        return record

    def _apply(self, record: dict) -> None:
        self.state = record["state"]
        self.comment_id = int(record.get("comment_id") or 0)

    @property
    def transport_guard_fd(self) -> int | None:
        """Explicit descriptor the trusted publication mutator must inherit.

        Passing it to the HTTP child keeps the real process fence held until
        that mutator exits, including when this Python reporter is killed.
        """
        return self._fd

    # -- PublicationClaim protocol --------------------------------------
    def check_current(self) -> None:
        self._fence_ok()
        with deploy_state.state_transaction(self._directory):
            state = deploy_state.load_state(self._directory)
            record = self._validate(state)
            if record is not None:
                self._apply(record)

    def intent(self) -> None:
        self._fence_ok()
        with deploy_state.state_transaction(self._directory):
            state = deploy_state.load_state(self._directory)
            self._validate(state)
            publications = state.setdefault("publications", {})
            record = publications.setdefault(self._attempt_id, self._new_record())
            self._apply(record)
            if record["state"] != "ready" or record["comment_id"]:
                raise HostingError("publication_intent_invalid")
            record.update(state="uncertain", intent_at=deploy_state.now_epoch())
            deploy_state.save_state(self._directory, state)
            self._apply(record)

    def bind(self, comment_id: int) -> None:
        self._fence_ok()
        if type(comment_id) is not int or comment_id <= 0:
            raise HostingError("publication_comment_invalid")
        with deploy_state.state_transaction(self._directory):
            state = deploy_state.load_state(self._directory)
            self._validate(state)
            publications = state.setdefault("publications", {})
            record = publications.setdefault(self._attempt_id, self._new_record())
            self._apply(record)
            if record["state"] == "linked":
                if record["comment_id"] != comment_id:
                    raise HostingError("publication_mapping_mismatch")
                return
            if record["state"] not in {"ready", "uncertain"}:
                raise HostingError("publication_bind_invalid")
            record.update(state="linked", comment_id=comment_id,
                          linked_at=deploy_state.now_epoch())
            deploy_state.save_state(self._directory, state)
            self._apply(record)

    def _new_record(self) -> dict:
        return {
            "generation": self._generation,
            "payload_sha256": self._payload_sha256,
            "binding_sha256": self._binding_sha256,
            "state": "ready",
            "comment_id": 0,
            "created_at": deploy_state.now_epoch(),
        }


class DurablePublicationAuthority:
    """Concrete trusted adapter for the existing `publish_public` path.

    The reporter constructs this from a durable outcome + binding, never from a
    CLI argument or a hand-written context file.
    """

    def __init__(self, *, state_dir: Path, outcome_id: str, generation: int,
                 context, payload_sha256: str, binding_sha256: str):
        self._directory = Path(state_dir)
        self._outcome_id = outcome_id
        self._generation = generation
        self._context = context
        self._payload_sha256 = payload_sha256
        self._binding_sha256 = binding_sha256

    @contextmanager
    def claim(self, context, payload_sha256):
        if context != self._context or payload_sha256 != self._payload_sha256:
            raise HostingError("publication_claim_mismatch")
        with ExitStack() as stack:
            inherited = deploy_state.process_lock_fd()
            if inherited is not None:
                fd, is_inherited = inherited, True
            else:
                try:
                    fd = stack.enter_context(deploy_state.attempt_lock(self._directory))
                except BlockingIOError as exc:
                    raise HostingError("deployment_process_busy") from exc
                is_inherited = False
            claim = DurablePublicationClaim(
                self._directory, self._outcome_id, self._generation,
                context, payload_sha256, self._binding_sha256, is_inherited, fd)
            with deploy_state.state_transaction(self._directory):
                state = deploy_state.load_state(self._directory)
                record = claim._validate(state)
                if record is None:
                    state.setdefault("publications", {})[self._outcome_id] = claim._new_record()
                    deploy_state.save_state(self._directory, state)
                    record = claim._new_record()
                claim._apply(record)
            yield claim

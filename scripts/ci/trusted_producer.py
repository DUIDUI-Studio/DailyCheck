#!/usr/bin/env python3
"""Approved native CI controller. Candidate processes never run on its host.

The approved workflow exports this entire closure by literal Git blob IDs. Run
with Python -I, outside the candidate, using the runner's platform Python (never
a candidate venv). Only Docker data and stopped-container files cross back.
The image ID below is a local fixture runtime, not a hosted installation.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import multiprocessing
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import uuid
from http.client import BadStatusLine, IncompleteRead
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

AUTHORITY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(AUTHORITY))

LOCAL_SCOPE = "local_fixture_only"
HOSTED_SCOPE = "approved_hosted_archive"
HOSTED_PLATFORM = "linux/arm64"
RELEASE_PREFIX = "https://github.com/DUIDUI-Studio/DailyCheck/releases/download/"
# Official release-asset API identity; immutable nonzero numeric asset ID.
ASSET_API_PREFIX = ("https://api.github.com/repos/DUIDUI-Studio/DailyCheck/"
                    "releases/assets/")
# Only the public GitHub release route and its bounded asset redirects.
PUBLIC_REDIRECT_HOSTS = frozenset({"github.com", "objects.githubusercontent.com",
                                   "release-assets.githubusercontent.com"})
MAX_REDIRECTS = 5
ARCHIVE_LIMIT = 2 * 1024 ** 3
SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
ARCHIVE_ROUTES = (
    re.compile(re.escape(RELEASE_PREFIX) + r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\.tar\.gz"),
    re.compile(re.escape(ASSET_API_PREFIX) + r"[1-9][0-9]{0,18}"),
)
DEFAULT_SECONDS = 180
HOSTED_PYTEST_SECONDS = 2700
# Measured full suite retained 3.5 GiB; never run it on an in-memory tmpfs.
SCRATCH_RESERVE = 4 * 1024 ** 3
HOST_FREE_MARGIN = 2 * 1024 ** 3

# Bounded retrieval retry. Only classified transient transport/HTTP failures
# are retried; integrity, route and redirect-policy failures never are. Backoff
# is fixed, so an untrusted Retry-After header can never set the wait time.
# The deadline gates every blocking open/read, so continuous slow progress
# cannot extend the stream indefinitely. Per-I/O timeout still bounds a single
# in-flight open/read, so the conservative wall bound is
# ARCHIVE_DEADLINE_SECONDS + ARCHIVE_HTTP_TIMEOUT plus local processing, not a
# strict ARCHIVE_DEADLINE_SECONDS guarantee.
ARCHIVE_HTTP_TIMEOUT = 120
ARCHIVE_CHUNK = 1024 * 1024
ARCHIVE_ATTEMPTS = 3
ARCHIVE_DEADLINE_SECONDS = 300.0
ARCHIVE_BACKOFF_SECONDS = (1.0, 2.0)
TRANSIENT_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


class ArchivePreflightError(ValueError):
    """Disk preflight failed before any retrieval attempt; never retried."""


class ArchiveTimeoutError(ValueError):
    """The stream/retry deadline expired during open/read; never retried."""

    def __init__(self, attempts: int = 1):
        self.attempts = attempts
        super().__init__("approved runtime archive deadline exceeded")


class ArchiveTransportError(ValueError):
    """Transport failure during open/read; retried within the bounded budget."""

    retryable = True

    def __init__(self, attempts: int = 1):
        self.attempts = attempts
        super().__init__("approved runtime archive transport failure")


class ArchiveHTTPError(ValueError):
    """HTTP status failure; only transient statuses are retried."""

    def __init__(self, http_status: int | None, attempts: int = 1):
        self.http_status = http_status
        self.attempts = attempts
        self.retryable = http_status in TRANSIENT_HTTP_STATUSES
        super().__init__("approved runtime archive HTTP failure")


class ArchiveIntegrityError(ValueError):
    """Size/digest mismatch; a corrupt body is never retried as transient."""

    def __init__(self, attempts: int = 1):
        self.attempts = attempts
        super().__init__("approved runtime archive integrity failure")


# Fixed, safe reasons for make_test_envelope failures. They name the gate that
# rejected the evidence without exposing candidate text or exception detail.
EVIDENCE_RAW_SESSION = "raw-session-invalid"
EVIDENCE_NEW_FAILURES = "new-failures"
EVIDENCE_SUMMARY = "summary-extraction"
EVIDENCE_COMPACT = "compact-evidence-invalid"
EVIDENCE_REASONS = frozenset({EVIDENCE_RAW_SESSION, EVIDENCE_NEW_FAILURES,
                              EVIDENCE_SUMMARY, EVIDENCE_COMPACT})

# Closed mapping from the protected checker's fixed messages to controller
# tokens. Only the fixed prefix is matched; any unrecognised message becomes
# "unknown" and the message itself is never printed or stored.
CHECKER_SUBREASON_PREFIXES = (
    ("pytest native exit must be 0/1", "native-exit"),
    ("missing/invalid pytest session evidence", "session-shape"),
    ("native exit disagrees with pytest session exit", "session-exit"),
    ("duplicate/inconsistent collected nodeids", "selected-unique"),
    ("pytest did not complete every selected test", "completion"),
    ("missing/incomplete outcome counts", "counts-shape"),
    ("pytest collection/setup/teardown errors cannot be known failures", "errors"),
    ("outcome enumeration does not match selected/completed count", "count-sum"),
    ("native exit disagrees with test outcomes", "outcome-exit"),
    ("pytest output is empty/interrupted/internal/collection/incomplete", "raw-output"),
    ("missing/truncated/ambiguous terminal pytest summary", "raw-summary"),
    ("unsupported pytest summary outcome", "summary-outcome"),
    ("duplicate pytest summary outcome", "summary-outcome-duplicate"),
    ("terminal summary counts disagree with pytest report enumeration", "summary-counts"),
    ("invalid failed-node enumeration", "failed-ids"),
    ("raw output contains pytest ERROR", "raw-error"),
    ("unrecognized FAILED line", "raw-failed"),
    ("FAILED enumeration missing/duplicated/inconsistent", "failed-enumeration"),
    ("invalid ", "counts-invalid"),
)
EVIDENCE_SUBREASONS = frozenset(
    token for _, token in CHECKER_SUBREASON_PREFIXES) | {"decode", "unknown"}
# A diagnostic test location is only the base module::test[/::Class] form from
# a validated session. Parameters are stripped and the shape is restrictive.
TEST_LOCATION_PATTERN = re.compile(
    r"tests/[A-Za-z0-9_./-]+\.py::[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)?")
MAX_DIAGNOSTIC_LOCATIONS = 5
MAX_DIAGNOSTIC_LOCATION = 120


def _safe_test_location(nodeid) -> str | None:
    """Return the parameter-free base nodeid only if it is safe to print."""
    if not isinstance(nodeid, str):
        return None
    base = nodeid.split("[", 1)[0]
    if len(base) > MAX_DIAGNOSTIC_LOCATION:
        return None
    if TEST_LOCATION_PATTERN.fullmatch(base) is None:
        return None
    path = base.split("::", 1)[0]
    if any(part in ("", ".", "..") for part in path.split("/")):
        return None
    return base


def _diagnostic_test_locations(nodeids: list) -> tuple[str, ...]:
    safe = {_safe_test_location(nodeid) for nodeid in nodeids}
    return tuple(sorted(location for location in safe if location))[
        :MAX_DIAGNOSTIC_LOCATIONS]


def _checker_subreason(error: BaseException) -> str:
    message = str(error)
    for prefix, token in CHECKER_SUBREASON_PREFIXES:
        if message.startswith(prefix):
            return token
    return "unknown"


class EvidenceValidationError(ValueError):
    """Gate failure with fixed reason/subreason; category stays invalid_data."""

    def __init__(self, reason: str, new_failures: int | None = None,
                 subreason: str | None = None, new_tests=()):
        if reason not in EVIDENCE_REASONS:
            raise ValueError("unapproved evidence reason")
        if subreason is not None and subreason not in EVIDENCE_SUBREASONS:
            raise ValueError("unapproved evidence subreason")
        if reason in (EVIDENCE_RAW_SESSION, EVIDENCE_COMPACT) and subreason is None:
            raise ValueError("evidence reason requires subreason")
        if reason in (EVIDENCE_NEW_FAILURES, EVIDENCE_SUMMARY) and subreason is not None:
            raise ValueError("evidence reason forbids subreason")
        locations = tuple(new_tests)
        if (len(locations) > MAX_DIAGNOSTIC_LOCATIONS
                or any(_safe_test_location(item) != item for item in locations)):
            raise ValueError("unapproved diagnostic location")
        self.reason = reason
        self.new_failures = new_failures
        self.subreason = subreason
        self.new_tests = locations
        super().__init__("approved native evidence validation failed")


# The only failure detail this fixed controller prints: an approved operation
# label and fixed category. Exception text, candidate output, URLs, env values
# and tracebacks never reach this line.
OPERATION = "startup"
ERROR_CATEGORIES = (
    (ArchivePreflightError, "preflight"),
    (ArchiveTimeoutError, "timeout"),
    (ArchiveTransportError, "transport"),
    (ArchiveHTTPError, "http"),
    (ArchiveIntegrityError, "integrity"),
    (subprocess.SubprocessError, "subprocess"),
    (OSError, "io"),
    (ValueError, "invalid_data"),
    (KeyError, "missing_input"),
    (TypeError, "invalid_type"),
    (RuntimeError, "runtime"),
)
# Fixed archive categories that carry bounded integer diagnostics only.
ARCHIVE_CATEGORIES = frozenset({"transport", "http", "integrity", "timeout"})


def _stage(name: str) -> None:
    global OPERATION
    OPERATION = name


def _failure_category(error: BaseException) -> str:
    for kind, category in ERROR_CATEGORIES:
        if isinstance(error, kind):
            return category
    return "unknown"


def _failure_diagnostic(error: BaseException) -> str:
    category = _failure_category(error)
    detail = f"operation={OPERATION} category={category}"
    status = getattr(error, "http_status", None)
    if category == "http" and type(status) is int and 100 <= status <= 599:
        detail += f" http_status={status}"
    attempts = getattr(error, "attempts", None)
    if category in ARCHIVE_CATEGORIES and type(attempts) is int \
            and 1 <= attempts <= ARCHIVE_ATTEMPTS:
        detail += f" attempts={attempts}"
    if isinstance(error, EvidenceValidationError):
        detail += f" reason={error.reason}"
        detail += " session_validated=" + (
            "0" if error.reason == EVIDENCE_RAW_SESSION else "1")
        if error.subreason is not None:
            detail += f" subreason={error.subreason}"
        if error.reason == EVIDENCE_NEW_FAILURES:
            count = error.new_failures
            if type(count) is int and count >= 1:
                detail += f" new_failures={count}"
            if error.new_tests:
                detail += " new_tests=" + ",".join(error.new_tests)
    return "[ci-producer] " + detail


def _native_code(value) -> str:
    return str(value) if type(value) is int and 0 <= value <= 255 else "unknown"


def _native_failure_details(kind: str, timed_out: bool, native, state: dict,
                            image_approved: bool) -> str:
    return ("native_kind={} native_exit={} timed_out={} oom_killed={} "
            "runtime_error={} image_approved={}").format(
                kind, _native_code(native), int(bool(timed_out)),
                int(bool(state.get("OOMKilled"))), int(bool(state.get("Error"))),
                int(bool(image_approved)))


# A fixed set of approved CI entrypoints, not candidate-selected executable paths.
PYTHON_GATES = {
    "preview": ("scripts/dev_smoke_t11_e2e.py", "e2e_smoke"),
    "mcp": ("scripts/mcp_contracts.py", "main"),
    "reports": ("scripts/agent_framework/check_reports.py", "main"),
    "mcp-impact": ("scripts/agent_framework/check_mcp_impact.py", "main"),
    "redlines": ("scripts/agent_framework/check_redlines.py", "main"),
    "hygiene": ("scripts/agent_framework/check_pr_hygiene.py", "main"),
}


def python_gate_command(gate: str, repo: Path, *arguments: str) -> list[str]:
    return [sys.executable, "-I", str(AUTHORITY / "scripts/ci/trusted_producer.py"),
            "python-gate", "--gate", gate, "--repo", str(repo), "--", *arguments]


def _python_gate_child(gate: str, repo: Path, arguments: list[str], completion):
    """Only this child imports the application. The supervisor never does.

    Runtime/approved packages precede explicit application-under-test lookup.
    The pipe reports a normal return of the pinned entrypoint, never log text.
    This is execution supervision, not cryptographic integrity over arbitrary
    Python code that deliberately tampers with its own process.
    """
    try:
        if not __debug__:
            raise ValueError("CI assertions must be enabled")
        repo = repo.resolve()
        sys.path[:] = [path for path in sys.path
                       if not Path(path or os.getcwd()).resolve().is_relative_to(repo)]
        sys.path.append(str(repo))
        filename, entrypoint = PYTHON_GATES[gate]
        path = repo / filename
        sys.argv = [str(path), *arguments]
        spec = importlib.util.spec_from_file_location("_dailycheck_ci_gate", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        result = getattr(module, entrypoint)()
        expected = result is None if gate == "preview" else type(result) is int and result == 0
        if not expected:
            raise ValueError("CI entrypoint did not return success")
        completion.send(("normal_return", gate))
    except BaseException:
        traceback.print_exc()
        raise SystemExit(1) from None
    finally:
        completion.close()


def run_python_gate(gate: str, repo: Path, arguments: list[str]) -> int:
    """Native0 without a normal driver return fails, including os._exit(0)."""
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_python_gate_child, args=(gate, repo, arguments, sender))
    try:
        process.start()
        sender.close()
        process.join()  # The enclosing owned container retains its wall limit.
        try:
            completed = receiver.recv() if receiver.poll() else None
        except EOFError:
            completed = None
        if process.exitcode != 0 or completed != ("normal_return", gate):
            print(f"[ci-python-gate] {gate}: missing normal driver completion", file=sys.stderr)
            return 1
        print(f"[ci-python-gate] {gate}: normal driver completion")
        return 0
    finally:
        sender.close()
        receiver.close()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def blob_id(raw: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()


def read_data(path: Path, root: Path) -> bytes:
    """Stopped candidate files are data; never follow a symlink into the host."""
    if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
        raise ValueError("candidate output escapes its owned directory")
    with path.open("rb") as handle:
        raw = handle.read(16 * 1024 * 1024 + 1)
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError("oversized producer data")
    return raw


def validate_archive_route(url: str) -> None:
    """The one route profile shared by manifest parsing and download.

    v1 still accepts the historical public browser download route; hosted now
    pins the official release-asset API identity so retrieval no longer
    depends on the browser redirect endpoint.
    """
    if not isinstance(url, str):
        raise ValueError("unapproved runtime archive route")
    try:
        parsed = urlsplit(url)
    except ValueError:
        raise ValueError("unapproved runtime archive route") from None
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or parsed.query or parsed.fragment
            or not any(route.fullmatch(url) for route in ARCHIVE_ROUTES)):
        raise ValueError("unapproved runtime archive route")


def _validate_hosted_runtime(runtime: dict) -> None:
    """The only hosted profile. Every field is fixed by the approved manifest."""
    if (set(runtime) != {"image", "platform", "scope", "controller", "archive", "image_graph"}
            or runtime["platform"] != HOSTED_PLATFORM
            or runtime["controller"] != "approved_workflow_platform_python_I"):
        raise ValueError("invalid approved hosted runtime")
    archive, graph = runtime["archive"], runtime["image_graph"]
    if (not isinstance(archive, dict) or set(archive) != {"url", "bytes", "sha256"}
            or not isinstance(graph, dict)
            or set(graph) != {"index_digest", "platform_manifest_digest", "config_digest",
                              "rootfs_diff_ids", "rootfs_diff_ids_sha256"}):
        raise ValueError("invalid approved hosted runtime identity")
    validate_archive_route(archive["url"])
    if (type(archive["bytes"]) is not int or not 0 < archive["bytes"] <= ARCHIVE_LIMIT
            or not isinstance(archive["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", archive["sha256"]) is None):
        raise ValueError("invalid approved hosted runtime archive")
    for key in ("index_digest", "platform_manifest_digest", "config_digest"):
        if not isinstance(graph[key], str) or SHA256_DIGEST.fullmatch(graph[key]) is None:
            raise ValueError("invalid approved hosted runtime graph")
    if (graph["index_digest"] != runtime["image"]
            or len({graph["index_digest"], graph["platform_manifest_digest"],
                    graph["config_digest"]}) != 3):
        raise ValueError("contradictory approved hosted runtime graph")
    diff_ids = graph["rootfs_diff_ids"]
    if (not isinstance(diff_ids, list) or not diff_ids
            or not all(isinstance(item, str) and SHA256_DIGEST.fullmatch(item)
                       for item in diff_ids)
            or len(set(diff_ids)) != len(diff_ids)
            or graph["rootfs_diff_ids_sha256"] != digest(diff_ids)):
        raise ValueError("invalid approved hosted runtime rootfs")


def parse_manifest(raw: bytes, expected_blob: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", expected_blob or "") or blob_id(raw) != expected_blob:
        raise ValueError("unapproved producer manifest blob")
    value = json.loads(raw)
    if set(value) != {"version", "runtime", "files"} or value["version"] != 1:
        raise ValueError("invalid approved producer runtime")
    runtime = value["runtime"]
    if not isinstance(runtime, dict) or runtime.get("scope") not in {LOCAL_SCOPE, HOSTED_SCOPE}:
        raise ValueError("invalid approved producer runtime")
    if runtime["scope"] == LOCAL_SCOPE:
        if (set(runtime) != {"image", "platform", "scope", "controller"}
                or not isinstance(runtime["image"], str)
                or SHA256_DIGEST.fullmatch(runtime["image"]) is None
                or runtime["platform"] not in {"linux/arm64", "linux/amd64"}
                or runtime["controller"] != "approved_workflow_platform_python_I"):
            raise ValueError("invalid approved producer runtime")
    else:
        _validate_hosted_runtime(runtime)
    files = value["files"]
    required = {"scripts/ci/trusted_producer.py", "scripts/ci/pytest_evidence.py",
                "scripts/ci/merge_group.py", "scripts/ci/candidate_binding.py",
                "scripts/ci/pytest_summary.py", "scripts/check_known_failures.py",
                "tests/known_failures.txt", "scripts/mcp_contracts.py",
                "scripts/dev_smoke_t11_e2e.py", "scripts/hosting.py"}
    required |= {"scripts/agent_framework/" + name for name in (
        "generate_reports.sh", "preview.sh", "check_reports.py", "check_mcp_impact.py",
        "check_redlines.py", "check_pr_hygiene.py")}
    if not isinstance(files, dict) or set(files) != required:
        raise ValueError("incomplete approved producer closure")
    if any(PurePosixPath(p).as_posix() != p or ".." in PurePosixPath(p).parts
           or not re.fullmatch(r"[0-9a-f]{40}", sha) for p, sha in files.items()):
        raise ValueError("invalid producer closure blob")
    return value


def approved_images(manifest: dict) -> frozenset[str]:
    """Exact daemon representations bound by this immutable manifest graph."""
    runtime = manifest["runtime"]
    if runtime["scope"] == LOCAL_SCOPE:
        return frozenset({runtime["image"]})
    graph = runtime["image_graph"]
    return frozenset({graph["index_digest"], graph["platform_manifest_digest"],
                      graph["config_digest"]})


def capture_budget(manifest: dict, kind: str) -> tuple[int, bool]:
    """(wall seconds, disk-backed untrusted scratch) for the approved runtime."""
    if manifest["runtime"]["scope"] == HOSTED_SCOPE and kind == "pytest":
        return HOSTED_PYTEST_SECONDS, True
    return DEFAULT_SECONDS, False


def identity(manifest: dict, manifest_blob: str) -> dict:
    return {"manifest_blob_sha": manifest_blob, "closure_sha256": digest(manifest["files"]),
            "runtime": manifest["runtime"]}


def validate_producer(producer: dict, manifest: dict, manifest_blob: str,
                      envelope: dict, context: str) -> None:
    """Data validation is meaningful only with the pinned, authenticated job log."""
    from scripts.ci.pytest_summary import decode_result

    binding = envelope["binding"]
    decode_result(envelope, binding)
    if (not isinstance(producer, dict) or set(producer) != {"identity", "lint_capture", "test_capture", "gates"}
            or producer["identity"] != identity(manifest, manifest_blob)):
        raise ValueError("unapproved executed producer closure/runtime")
    capture = producer["test_capture"]
    evidence = envelope["evidence"]
    validate_capture(producer["lint_capture"], manifest, binding, "lint", "test")
    if producer["lint_capture"]["native_exit"] != 0:
        raise ValueError("actual changed-file Ruff gate failed")
    validate_capture(capture, manifest, binding, "pytest", "test")
    if (capture["native_exit"] != evidence["native_exit"]
            or capture["stdout_sha256"] != evidence["raw_output_sha256"]
            or capture["session_sha256"] != digest(evidence["session"])):
        raise ValueError("envelope differs from externally captured native proof")
    gates = producer["gates"]
    if context == "test":
        if gates != []:
            raise ValueError("test cannot claim reports")
    elif context == "agent-framework-reports":
        if not isinstance(gates, list) or len(gates) != 2:
            raise ValueError("missing actual report gate captures")
        for item, kind in zip(gates, ("report-generate", "report-verify"), strict=True):
            validate_capture(item, manifest, binding, kind, context)
            if item["native_exit"] != 0:
                raise ValueError("report gate did not succeed")
    else:
        raise ValueError("unknown required context")


def validate_capture(capture: dict, manifest: dict, binding: dict, kind: str, context: str):
    keys = {"version", "container_id", "image", "kind", "context", "binding_sha256",
            "native_exit", "stdout_sha256", "session_sha256", "exited", "removed", "oom_killed"}
    if (not isinstance(capture, dict) or set(capture) != keys or capture["version"] != 1
            or not isinstance(capture["image"], str)
            or capture["image"] not in approved_images(manifest)
            or capture["kind"] != kind or capture["context"] != context
            or capture["binding_sha256"] != digest(binding)
            or capture["exited"] is not True or capture["removed"] is not True
            or capture["oom_killed"] is not False
            or type(capture["native_exit"]) is not int or capture["native_exit"] not in (0, 1)
            or not re.fullmatch(r"[0-9a-f]{64}", capture["container_id"])
            or not re.fullmatch(r"[0-9a-f]{64}", capture["stdout_sha256"])
            or (kind == "pytest" and not re.fullmatch(r"[0-9a-f]{64}", capture["session_sha256"] or ""))
            or (kind != "pytest" and capture["session_sha256"] is not None)):
        raise ValueError("invalid external native capture/run/attempt")


def _require_free(path: Path, required: int) -> int:
    free = shutil.disk_usage(path).free
    if free < required:
        raise ValueError("insufficient disk for approved native capture")
    return free


class _PublicRedirect(HTTPRedirectHandler):
    """Bounded, credential-free redirects for a public release asset only."""
    def __init__(self):
        self.count = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.count += 1
        parsed = urlsplit(newurl)
        if (self.count > MAX_REDIRECTS or parsed.scheme != "https"
                or parsed.hostname not in PUBLIC_REDIRECT_HOSTS
                or parsed.username or parsed.password):
            raise ValueError("unapproved runtime archive redirect")
        # Build fresh: no credential or signed query state is forwarded.
        return Request(newurl, method="GET", headers={"Accept": "application/octet-stream"})


def _public_opener():
    return build_opener(_PublicRedirect())


def _check_deadline(deadline: float, attempt: int) -> None:
    if time.monotonic() >= deadline:
        raise ArchiveTimeoutError(attempt)


def _read_chunk(response, attempt: int) -> bytes:
    """Read one chunk; socket/protocol failures become transport errors only."""
    try:
        return response.read(ARCHIVE_CHUNK)
    except (IncompleteRead, BadStatusLine):
        raise ArchiveTransportError(attempt) from None
    except OSError:
        raise ArchiveTransportError(attempt) from None


def _http_status(value) -> int | None:
    return value if type(value) is int and 100 <= value <= 599 else None


def _download_once(runtime: dict, destination: Path, opener, attempt: int,
                   deadline: float) -> None:
    """One retrieval attempt. Integrity and policy failures stay non-retryable."""
    archive = runtime["archive"]
    request = Request(archive["url"], method="GET",
                      headers={"Accept": "application/octet-stream"})
    _check_deadline(deadline, attempt)
    try:
        response = opener.open(request, timeout=ARCHIVE_HTTP_TIMEOUT)
    except HTTPError as error:
        # A synthetic HTTPError may carry no body handle; never let cleanup
        # turn a classified HTTP failure into an unknown exception.
        if getattr(error, "fp", None) is not None:
            error.close()
        raise ArchiveHTTPError(_http_status(error.code), attempt) from None
    except (IncompleteRead, BadStatusLine):
        raise ArchiveTransportError(attempt) from None
    except OSError:
        raise ArchiveTransportError(attempt) from None
    _check_deadline(deadline, attempt)
    # Route/redirect policy ValueError from the opener deliberately propagates.
    with response:
        status = getattr(response, "status", None)
        if status != 200:
            raise ArchiveHTTPError(_http_status(status), attempt)
        hasher, total = hashlib.sha256(), 0
        # Local destination I/O is outside the transport classification: a disk
        # write failure is reported as io and never retried.
        with destination.open("wb") as output:
            while True:
                _check_deadline(deadline, attempt)
                chunk = _read_chunk(response, attempt)
                _check_deadline(deadline, attempt)
                if not chunk:
                    break
                total += len(chunk)
                if total > archive["bytes"]:
                    raise ArchiveIntegrityError(attempt)
                hasher.update(chunk)
                output.write(chunk)
    if total != archive["bytes"] or hasher.hexdigest() != archive["sha256"]:
        raise ArchiveIntegrityError(attempt)


def download_archive(runtime: dict, destination: Path, *, opener=None) -> None:
    """Stream only the pinned public bytes; nothing is extracted or executed.

    Transient transport/HTTP failures are retried a fixed number of times. The
    stream/retry deadline is checked before and after every blocking open/read,
    so continuous slow progress cannot extend the stream indefinitely. A single
    in-flight open/read is separately bounded by ARCHIVE_HTTP_TIMEOUT, which is
    why the conservative wall bound is ARCHIVE_DEADLINE_SECONDS plus that
    timeout plus local processing, not a strict deadline. Digest/size
    mismatches, route/redirect-policy failures and deadline expiry fail on the
    first attempt. A failed attempt never leaves a partial file. A default
    opener is rebuilt for every attempt so the redirect budget starts fresh;
    an injected opener stays caller-owned across attempts.
    """
    archive = runtime["archive"]
    deadline = time.monotonic() + ARCHIVE_DEADLINE_SECONDS
    attempt = 0
    try:
        validate_archive_route(archive["url"])
        while True:
            attempt += 1
            attempt_opener = opener if opener is not None else _public_opener()
            try:
                _download_once(runtime, destination, attempt_opener, attempt, deadline)
                return
            except (ArchiveTransportError, ArchiveHTTPError) as error:
                error.attempts = attempt
                destination.unlink(missing_ok=True)
                delay = ARCHIVE_BACKOFF_SECONDS[
                    min(attempt - 1, len(ARCHIVE_BACKOFF_SECONDS) - 1)]
                if (not error.retryable or attempt >= ARCHIVE_ATTEMPTS
                        or time.monotonic() + delay >= deadline):
                    raise
                time.sleep(delay)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def inspect_image(docker: list[str], reference: str) -> dict | None:
    result = subprocess.run([*docker, "image", "inspect", reference],
                            capture_output=True, timeout=120)
    if result.returncode:
        return None
    data = json.loads(result.stdout)
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise ValueError("ambiguous approved runtime image")
    return data[0]


def resolve_approved_image(docker: list[str], runtime: dict) -> str | None:
    """Return a verified daemon reference from the immutable approved graph."""
    graph = runtime["image_graph"]
    references = (graph["index_digest"], graph["platform_manifest_digest"],
                  graph["config_digest"])
    approved = set(references)
    for reference in references:
        inspection = inspect_image(docker, reference)
        if inspection is None:
            continue
        if (inspection.get("Os"), inspection.get("Architecture")) != ("linux", "arm64"):
            raise ValueError("approved runtime platform mismatch")
        if (inspection.get("RootFS") or {}).get("Layers") != graph["rootfs_diff_ids"]:
            raise ValueError("approved runtime rootfs mismatch")
        if inspection.get("Id") not in approved:
            raise ValueError("daemon image identity is outside the approved graph")
        return reference
    return None


def ensure_approved_runtime(docker: list[str], manifest: dict, work: Path, *,
                            opener=None) -> str:
    """Resolve the approved image, loading the pinned archive once if needed."""
    runtime = manifest["runtime"]
    if runtime["scope"] == LOCAL_SCOPE:
        return runtime["image"]
    _stage("runtime-resolve")
    reference = resolve_approved_image(docker, runtime)
    if reference is not None:
        return reference
    archive_dir = work / "runtime"
    archive_dir.mkdir(mode=0o700, exist_ok=True)
    _stage("runtime-preflight")
    try:
        _require_free(archive_dir, runtime["archive"]["bytes"] + HOST_FREE_MARGIN)
    except ValueError:
        raise ArchivePreflightError() from None
    _stage("runtime-archive")
    archive_path = archive_dir / "approved-runtime.tar.gz"
    download_archive(runtime, archive_path, opener=opener)
    _stage("runtime-load")
    try:
        load = subprocess.run([*docker, "load", "-i", str(archive_path)],
                              capture_output=True, timeout=1800)
    finally:
        archive_path.unlink(missing_ok=True)
    if load.returncode:
        raise ValueError("approved runtime image load failed")
    _stage("runtime-verify")
    reference = resolve_approved_image(docker, runtime)
    if reference is None:
        raise ValueError("approved runtime image unresolved")
    return reference


class _CaptureTimeout(Exception):
    pass


# Failure-only pytest diagnostics. These are opaque, bounded copies of
# untrusted candidate data for human diagnosis; they never feed the gate and
# never create or change an attestation. The candidate input stays hidden at
# .dc-junit.xml, but the retained copy is visible junit.xml because the pinned
# upload-artifact v4 default (include-hidden-files=false) omits dot files.
PYTEST_JUNIT_NAME = ".dc-junit.xml"
DIAGNOSTIC_JUNIT_NAME = "junit.xml"
PYTEST_DIAGNOSTIC_DIR = "native-diagnostics"
MAX_JUNIT_BYTES = 8 * 1024 * 1024
MAX_RAW_TAIL_BYTES = 1024 * 1024
MAX_METADATA_BYTES = 256 * 1024
JUNIT_PREFIXES = (b"<?xml", b"<testsuites", b"<testsuite")


def clear_pytest_junit(candidate: Path) -> None:
    """Remove any candidate-preseeded JUnit path before the child runs."""
    target = candidate / PYTEST_JUNIT_NAME
    try:
        if target.is_symlink() or target.is_file():
            target.unlink()
    except OSError:
        # A directory or race leaves the path; pytest then writes nothing.
        pass


def _unlink_diagnostic(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _read_candidate_diagnostic(path: Path, root: Path, limit: int):
    """Opaque bounded read; never follow a symlink out of the candidate root."""
    if path.is_symlink():
        return "unsafe", None, 0
    try:
        resolved, root_resolved = path.resolve(), root.resolve()
    except OSError:
        return "error", None, 0
    if not resolved.is_relative_to(root_resolved):
        return "unsafe", None, 0
    if not path.exists():
        return "missing", None, 0
    if not path.is_file():
        return "unsafe", None, 0
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            raw = handle.read(limit + 1)
    except OSError:
        return "error", None, 0
    if len(raw) > limit:
        return "oversized", None, size
    return "present", raw, len(raw)


def _write_diagnostic(path: Path, raw: bytes) -> None:
    path.write_bytes(raw)
    path.chmod(0o644)


def _safe_metadata(name: str, value, directory: Path):
    if value is None:
        return "missing", 0, None
    try:
        payload = (json.dumps(value, sort_keys=True) + "\n").encode()
    except (TypeError, ValueError):
        return "invalid", 0, None
    if len(payload) > MAX_METADATA_BYTES:
        return "oversized", len(payload), hashlib.sha256(payload).hexdigest()
    try:
        _write_diagnostic(directory / name, payload)
    except OSError:
        return "error", len(payload), None
    return "present", len(payload), hashlib.sha256(payload).hexdigest()


def retain_pytest_diagnostics(candidate: Path, work: Path, capture: dict,
                              session, raw: bytes) -> dict:
    """Best-effort bounded copies; diagnostic problems never fail the gate."""
    directory = work / PYTEST_DIAGNOSTIC_DIR
    status = {"schema_version": 1, "kind": "pytest", "junit": "missing",
              "junit_name": DIAGNOSTIC_JUNIT_NAME,
              "junit_bytes": 0, "junit_sha256": None,
              "raw_tail": "missing", "raw_tail_bytes": 0,
              "raw_tail_sha256": None, "raw_total_bytes": len(raw or b""),
              "raw_tail_trimmed": 0, "session": "missing", "session_bytes": 0,
              "session_sha256": None, "capture": "missing", "capture_bytes": 0,
              "capture_sha256": None}
    try:
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            directory.unlink()
        directory.mkdir(mode=0o755, exist_ok=True)
        directory.chmod(0o755)
    except OSError:
        return status

    state, data, size = _read_candidate_diagnostic(
        candidate / PYTEST_JUNIT_NAME, candidate, MAX_JUNIT_BYTES)
    if state == "present" and not data.lstrip().startswith(JUNIT_PREFIXES):
        state = "malformed"
    if state == "present":
        try:
            _write_diagnostic(directory / DIAGNOSTIC_JUNIT_NAME, data)
        except OSError:
            state = "error"
        else:
            status.update(junit="present", junit_bytes=len(data),
                          junit_sha256=hashlib.sha256(data).hexdigest())
    if status["junit"] != "present":
        status.update(junit=state, junit_bytes=size)
        _unlink_diagnostic(directory / DIAGNOSTIC_JUNIT_NAME)

    tail = (raw or b"")[-MAX_RAW_TAIL_BYTES:]
    if tail:
        try:
            _write_diagnostic(directory / "pytest-output-tail.txt", tail)
            status.update(raw_tail="present", raw_tail_bytes=len(tail),
                          raw_tail_sha256=hashlib.sha256(tail).hexdigest(),
                          raw_tail_trimmed=int(len(tail) < len(raw or b"")))
        except OSError:
            status["raw_tail"] = "error"
    else:
        _unlink_diagnostic(directory / "pytest-output-tail.txt")

    for key, name, value in (("session", "session.json", session),
                             ("capture", "capture.json", capture)):
        state, size, sha = _safe_metadata(name, value, directory)
        status.update({key: state, key + "_bytes": size, key + "_sha256": sha})
        if state != "present":
            _unlink_diagnostic(directory / name)

    try:
        _write_diagnostic(directory / "status.json",
                          (json.dumps(status, sort_keys=True) + "\n").encode())
    except OSError:
        return status
    return status


class DockerCapture:
    """Only this trusted host process can reach the daemon or publish evidence.

    No socket, credentials, authority outputs or host PID namespace are mounted.
    A lost controller produces no marker. Timeout kills the owned container and
    is always a failure; uncertain exit/cleanup also forbids an attestation.
    """
    def __init__(self, docker: list[str], manifest: dict, authority: Path, output: Path, *,
                 image: str | None = None):
        self.docker, self.manifest, self.authority, self.output = docker, manifest, authority, output
        self.image = image or manifest["runtime"]["image"]

    def call(self, *args, timeout=30):
        result = subprocess.run([*self.docker, *args], capture_output=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError("owned Docker operation failed: " + args[0])
        return result.stdout

    def _wait(self, cid: str, timeout: int, scratch: Path | None) -> int:
        if scratch is None:
            return int(self.call("wait", cid, timeout=timeout).decode().strip())
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _CaptureTimeout
            try:
                return int(self.call("wait", cid, timeout=min(30, remaining)).decode().strip())
            except subprocess.TimeoutExpired:
                if shutil.disk_usage(scratch).free < HOST_FREE_MARGIN:
                    self.call("kill", cid)
                    raise ValueError("insufficient disk during approved native capture") from None

    def capture(self, candidate: Path, binding: dict, kind: str, context: str,
                argv: list[str], env: dict[str, str], *, trusted_scripts=False, readonly=False,
                inputs: Path | None = None):
        name = "dc-native-ci-b2-" + uuid.uuid4().hex
        runtime = self.manifest["runtime"]
        timeout, disk_scratch = capture_budget(self.manifest, kind)
        scratch = None
        if disk_scratch:
            # Untrusted per-capture scratch lives outside authority/evidence.
            scratch = self.output / "scratch" / name
            scratch.mkdir(parents=True, mode=0o700)
            scratch.chmod(0o777)
            _require_free(scratch, SCRATCH_RESERVE + HOST_FREE_MARGIN)
            scratch_args = ["--mount", f"type=bind,src={scratch},dst=/tmp"]
        else:
            scratch_args = ["--tmpfs", "/tmp:rw,nosuid,nodev,size=512m,mode=1777"]
        args = ["create", "--name", name, "--label", "org.duidui.scope=dc-native-ci-b2",
                "--pull=never", "--platform", runtime["platform"], "--network=none",
                "--user=65532:65532", "--read-only", "--cap-drop=ALL",
                "--security-opt=no-new-privileges", "--cpus=2", "--memory=2g", "--pids-limit=256",
                *scratch_args, "--workdir=/candidate",
                "--mount", f"type=bind,src={candidate},dst=/candidate" + (",readonly" if readonly else ""),
                "--mount", f"type=bind,src={self.authority},dst=/authority,readonly"]
        if trusted_scripts:
            args += ["--mount", f"type=bind,src={self.authority / 'scripts'},dst=/candidate/scripts,readonly",
                     "--mount", f"type=bind,src={self.authority / 'tests/known_failures.txt'},dst=/candidate/tests/known_failures.txt,readonly"]
        if inputs is not None:
            args += ["--mount", f"type=bind,src={inputs},dst=/input,readonly"]
        plugin_dir = None
        if kind == "pytest":
            # The candidate checkout's scripts/ is a namespace package, so the
            # authority's regular scripts package (empty __init__) wins wherever
            # it appears on sys.path and hides every candidate subpackage
            # (scripts.prod_agent, scripts.dailycheck_dashboard, ...). Mount the
            # pinned evidence plugin on its own root instead of the helper root.
            plugin_dir = self.output / "evidence-plugin"
            plugin_dir.mkdir(exist_ok=True)
            plugin_dir.chmod(0o755)
            raw_plugin = (self.authority / "dc_approved_evidence.py").read_bytes()
            if blob_id(raw_plugin) != self.manifest["files"]["scripts/ci/pytest_evidence.py"]:
                raise ValueError("approved evidence plugin changed")
            plugin_file = plugin_dir / "dc_approved_evidence.py"
            plugin_file.write_bytes(raw_plugin)
            plugin_file.chmod(0o644)
            args += ["--mount", f"type=bind,src={plugin_dir},dst=/trusted-evidence,readonly"]
        clean_env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp",
                     "PYTHONPATH": "/trusted-evidence" if plugin_dir else "/authority",
                     "PYTHONSAFEPATH": "1", "PYTHONNOUSERSITE": "1",
                     "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONDONTWRITEBYTECODE": "1",
                     "RUFF_CACHE_DIR": "/tmp/ruff",
                     "DAILYCHECK_DB_DIR": "/tmp/db", "TMPDIR": "/tmp",
                     "GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "safe.directory",
                     "GIT_CONFIG_VALUE_0": "*", "GIT_CONFIG_KEY_1": "core.hooksPath",
                     "GIT_CONFIG_VALUE_1": "/dev/null", **env}
        for key, value in clean_env.items():
            args += ["--env", key + "=" + value]
        args += [self.image, *argv]
        # The create result is not candidate stdout.
        cid = self.call(*args).decode().strip()
        if not re.fullmatch(r"[0-9a-f]{64}", cid):
            raise ValueError("invalid owned container ID")
        run_dir = self.output / name
        run_dir.mkdir()
        (run_dir / "command.json").write_text(json.dumps([*self.docker, *args], indent=2))
        timed_out = False
        try:
            self.call("start", cid)
            try:
                native = self._wait(cid, timeout, scratch)
            except (_CaptureTimeout, subprocess.TimeoutExpired):
                timed_out = True
                self.call("kill", cid)
                native = int(self.call("wait", cid).decode().strip())
            inspection = json.loads(self.call("inspect", cid))[0]
            (run_dir / "inspect.json").write_text(json.dumps(inspection, indent=2))
            state = inspection["State"]
            if state["Running"] or state["Status"] != "exited":
                raise ValueError("candidate exit unproven; preserve owned container")
            if scratch is not None:
                _require_free(scratch, HOST_FREE_MARGIN)
            # Docker merges stderr on the client stderr stream. Obtain BOTH
            # streams verbatim; never print candidate Actions commands/markers.
            logs = subprocess.run([*self.docker, "logs", cid], capture_output=True, timeout=30)
            if logs.returncode:
                raise ValueError("candidate logs unavailable")
            raw = logs.stdout + logs.stderr
            (run_dir / "stdout.bin").write_bytes(raw)
            self.call("rm", cid)
            # Only a proven exited and removed owned capture may delete scratch.
            if scratch is not None:
                shutil.rmtree(scratch)
            image_approved = inspection["Image"] in approved_images(self.manifest)
            if (timed_out or native != state["ExitCode"] or state["OOMKilled"]
                    or state["Error"] or not image_approved):
                print(f"[ci-producer] operation={OPERATION} "
                      + _native_failure_details(kind, timed_out, native, state,
                                                image_approved), file=sys.stderr)
                raise ValueError("candidate timeout/runtime/native capture failure")
            session = None
            if kind == "pytest":
                session = json.loads(read_data(candidate / ".dc-session.json", candidate))
                (run_dir / "session.json").write_text(json.dumps(session, sort_keys=True))
            capture = {"version": 1, "container_id": cid, "image": inspection["Image"],
                       "kind": kind, "context": context, "binding_sha256": digest(binding),
                       "native_exit": native, "stdout_sha256": hashlib.sha256(raw).hexdigest(),
                       "session_sha256": digest(session) if session is not None else None,
                       "exited": True, "removed": True, "oom_killed": False}
            (run_dir / "capture.json").write_text(json.dumps(capture, sort_keys=True))
            return capture, raw, session
        except BaseException:
            # Never guess that an uncertain live child has exited or remove it.
            # Its random name/label and create record allow operator recovery.
            raise


def make_test_envelope(raw: bytes, session: dict, capture: dict, binding: dict) -> dict:
    from scripts.check_known_failures import load_known_ids, make_evidence, unexpected_failures
    from scripts.ci.pytest_summary import extract_pytest_summary

    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        raise EvidenceValidationError(EVIDENCE_RAW_SESSION, subreason="decode") from None
    try:
        original = make_evidence(text, capture["native_exit"], session)
    except ValueError as error:
        raise EvidenceValidationError(
            EVIDENCE_RAW_SESSION, subreason=_checker_subreason(error)) from None
    new = unexpected_failures(session["failed_nodeids"], load_known_ids())
    if new:
        raise EvidenceValidationError(EVIDENCE_NEW_FAILURES, len(new),
                                      new_tests=_diagnostic_test_locations(new))
    try:
        compact = extract_pytest_summary(text)
    except ValueError:
        raise EvidenceValidationError(EVIDENCE_SUMMARY) from None
    try:
        evidence = make_evidence(compact, capture["native_exit"], session)
    except ValueError as error:
        raise EvidenceValidationError(
            EVIDENCE_COMPACT, subreason=_checker_subreason(error)) from None
    evidence["raw_output_sha256"] = original["output_sha256"]
    return {"schema_version": 2, "text": compact, "evidence": evidence, "binding": binding}


def attest(envelope: dict, members: list[dict], context: str, producer: dict) -> dict:
    from scripts.ci.merge_group import make_attestation

    record = make_attestation(envelope, members, context)
    record.pop("artifact_sha256")
    # Reports references the exact TEST artifact, including its producer proof.
    if context == "agent-framework-reports":
        upstream = attest(envelope, members, "test", {**producer, "gates": []})
        record["pytest_source"]["artifact_sha256"] = upstream["artifact_sha256"]
    record["producer"] = producer
    return {**record, "artifact_sha256": digest(record)}


def report_inner(candidate: Path, context: str):
    """Executed in a disposable container; never in the authority/controller."""
    from scripts.ci.merge_group import member_gates

    os.chdir(candidate)
    data = json.loads(Path("/input/data.json").read_text())
    members, binding = data["members"], data["binding"]
    proof = Path("/input")
    env = {**os.environ, "CI_ROOT": "/tmp", "DAILYCHECK_CI_TMP": "/tmp",
           "DAILYCHECK_CI_AUTHORITY": str(AUTHORITY),
           "PYTEST_REUSE_FILE": str(proof / "output.txt"),
           "PYTEST_REUSE_EVIDENCE": str(proof / "evidence.json")}

    def run(argv, extra=None):
        subprocess.run(argv, check=True, env={**env, **(extra or {})}, cwd=candidate)

    if context == "report-verify":
        # No candidate module imports or execution in this fresh verifier.
        for member in members:
            run(python_gate_command("reports", candidate),
                {"PR_NUM": str(member["number"]), "PR_BODY_FILE": str(proof / f'body-{member["number"]}.md')})
        return
    if binding["phase"] == "G":
        os.environ.update(env)
        member_gates(candidate, data["manifest"], {m["number"]: m["body"] for m in members},
                     proof / "evidence.json", proof / "output.txt", trusted=True)
    else:
        run(python_gate_command("mcp", candidate, "--check"))
        member = members[0]
        body = str(proof / f'body-{member["number"]}.md')
        for script in ("check_mcp_impact.py", "check_redlines.py", "check_pr_hygiene.py"):
            gate = {"check_mcp_impact.py": "mcp-impact", "check_redlines.py": "redlines",
                    "check_pr_hygiene.py": "hygiene"}[script]
            arguments = ["--base", binding["CI_BASE_SHA"], "--body-file", body]
            if script == "check_pr_hygiene.py":
                arguments += ["--branch", member["head_ref"]]
            run(python_gate_command(gate, candidate, *arguments))
        extra = {"PR_NUM": str(member["number"]), "BASE_SHA": binding["CI_BASE_SHA"],
                 "HEAD_SHA": binding["tested_sha"], "PR_BODY_FILE": body}
        run(["bash", "scripts/agent_framework/generate_reports.sh"], extra)
        run(["bash", "scripts/agent_framework/preview.sh", str(member["number"]), "--trusted-ci"], extra)


def snapshot(repo: Path, destination: Path, head: str):
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
    for argv in (["git", "-c", "core.hooksPath=/dev/null", "clone", "-q", "--no-hardlinks", "--no-checkout", str(repo), str(destination)],
                 ["git", "-C", str(destination), "-c", "core.hooksPath=/dev/null", "checkout", "-q", "--detach", head]):
        subprocess.run(argv, env=env, check=True, capture_output=True)
    # No runner credentials or source checkout config is copied into this clone.
    for p in destination.rglob("*"):
        if not p.is_symlink():
            p.chmod(0o777 if p.is_dir() else 0o666 | (p.stat().st_mode & 0o111))
    destination.chmod(0o777)


def job(args):
    from scripts.ci.candidate_binding import binding as make_binding
    from scripts.ci.merge_group import QueueReader

    _stage("manifest")
    manifest_blob = os.environ["CI_PRODUCER_MANIFEST_BLOB"]
    raw = (AUTHORITY / "scripts/ci/producer_manifest.json").read_bytes()
    manifest = parse_manifest(raw, manifest_blob)
    hosted = manifest["runtime"]["scope"] == HOSTED_SCOPE
    if hosted:
        # The fixed trusted producer runs as root on a hosted standard VM. The
        # runner-owned checkout must stay readable without a Git ownership stop.
        os.environ.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory",
                           "GIT_CONFIG_VALUE_0": "*"})
    # Verify executed bytes before any candidate process exists. This is a
    # trusted export check, not a candidate helper approving its own code.
    for name, expected in manifest["files"].items():
        if blob_id((AUTHORITY / name).read_bytes()) != expected:
            raise ValueError("approved authority export changed")
    for package in ("scripts", "scripts/ci", "scripts/agent_framework"):
        if (AUTHORITY / package / "__init__.py").read_bytes() != b"":
            raise ValueError("approved namespace export changed")
    work = Path(os.environ["CI_ROOT"]).resolve()
    _stage("event")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    repo = Path.cwd()
    members_manifest = None
    if os.environ["CI_EVENT"] == "merge_group":
        reader = QueueReader(os.environ["NATIVE_QUEUE_READ_TOKEN"])
        if args.context == "test":
            members_manifest, bodies = reader.manifest(repo, event, os.environ["CI_RUN_ID"])
        else:
            original = json.loads(base64.b64decode(os.environ["UPSTREAM_MEMBER_MANIFEST_B64"], validate=True))
            members_manifest, bodies = reader.revalidate(repo, original, event, os.environ["CI_RUN_ID"])
        member_path = work / "members.json"
        member_path.write_text(json.dumps(members_manifest))
        os.environ["CI_MEMBER_MANIFEST"] = str(member_path)
        members = [{**m, "body": bodies[m["number"]]} for m in members_manifest["members"]]
    else:
        pr = event["pull_request"]
        members = [{"number": pr["number"], "head_sha": pr["head"]["sha"],
                    "head_ref": pr["head"]["ref"], "body": pr["body"], "body_sha256": digest(pr["body"])}]
    binding = make_binding()
    _stage("snapshot")
    candidate = work / "candidate"
    snapshot(repo, candidate, binding["tested_sha"])
    (candidate / ".dc-session.json").unlink(missing_ok=True)
    docker = [args.docker]
    if args.docker_config:
        docker += ["--config", args.docker_config]
    if args.docker_host:
        docker += ["-H", args.docker_host]
    image = (ensure_approved_runtime(docker, manifest, work) if hosted
             else manifest["runtime"]["image"])
    runner = DockerCapture(docker, manifest, AUTHORITY, work, image=image)
    if args.context == "test":
        base = binding["CI_BASE_SHA"]
        changed = subprocess.check_output(["git", "diff", "--name-only", "--diff-filter=d", "-z",
                                           base + "..." + binding["tested_sha"], "--", "*.py"], cwd=repo).decode().split("\0")
        files = [p for p in changed if p]
        _stage("lint-capture")
        lint, _, _ = runner.capture(candidate, binding, "lint", "test",
                                    ["ruff", "check", "--", *files] if files else ["ruff", "--version"], {}, readonly=True)
        _stage("lint-gate")
        if lint["native_exit"] != 0:
            raise ValueError("actual changed-file Ruff gate failed")
        _stage("pytest-capture")
        clear_pytest_junit(candidate)
        capture, output, session = runner.capture(candidate, binding, "pytest", "test",
            ["python3", "-P", "-m", "pytest", "--basetemp=/tmp/pytest", "-q", "--tb=no", "-rfE", "--color=no",
             "-p", "dc_approved_evidence", "--dc-evidence=/candidate/.dc-session.json",
             "--junitxml=/candidate/" + PYTEST_JUNIT_NAME], {})
        # Bounded opaque copies for failure diagnosis; never part of the proof.
        retain_pytest_diagnostics(candidate, work, capture, session, output)
        _stage("evidence")
        envelope = make_test_envelope(output, session, capture, binding)
        producer = {"identity": identity(manifest, manifest_blob), "lint_capture": lint,
                    "test_capture": capture, "gates": []}
    else:
        _stage("upstream")
        upstream = json.loads(base64.b64decode(os.environ["NATIVE_TEST_B64"], validate=True))
        envelope, producer = upstream["envelope"], upstream["producer"]
        if (envelope["binding"] != binding or upstream != attest(envelope, members, "test", producer)
                or os.environ.get("UPSTREAM_RESULT") != "success"):
            raise ValueError("reports require exact successful upstream test")
        validate_producer(producer, manifest, manifest_blob, envelope, "test")
        proof = work / "report-inputs"
        proof.mkdir(mode=0o755)
        (proof / "output.txt").write_text(envelope["text"])
        (proof / "evidence.json").write_text(json.dumps(envelope["evidence"]))
        for member in members:
            (proof / f'body-{member["number"]}.md').write_text(member["body"])
        (proof / "data.json").write_text(json.dumps({"members": members, "binding": binding, "manifest": members_manifest}))
        gates = []
        for kind in ("report-generate", "report-verify"):
            _stage(kind)
            captured, _, _ = runner.capture(candidate, binding, kind, args.context,
                ["python3", "-I", "/authority/scripts/ci/trusted_producer.py", kind], {},
                trusted_scripts=True, readonly=kind == "report-verify", inputs=proof)
            if captured["native_exit"] != 0:
                raise ValueError("actual report gate failed")
            gates.append(captured)
        # Compare generated report02 proof to the original controller-held
        # upstream. Candidate writes cannot replace the reused native proof.
        if (json.loads(read_data(candidate / "reports/pytest-evidence.json", candidate)) != envelope["evidence"]
                or read_data(candidate / "reports/pytest-output.txt", candidate).decode() != envelope["text"]):
            raise ValueError("reports replaced trusted native proof")
        producer = {**producer, "gates": gates}
    _stage("attest")
    validate_producer(producer, manifest, manifest_blob, envelope, args.context)
    record = attest(envelope, members, args.context, producer)
    (work / "attestation.json").write_text(json.dumps(record, sort_keys=True))
    encoded = base64.b64encode(json.dumps(record, sort_keys=True).encode()).decode()
    if len(encoded) > 48 * 1024:
        raise ValueError("native proof too large; refusing truncation")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as handle:
        handle.write("native_test_b64=" + encoded + "\n")
        if members_manifest is not None:
            handle.write("member_manifest_b64=" + base64.b64encode(json.dumps(members_manifest).encode()).decode() + "\n")
    # This is the ONLY native marker reachable in the approved native workflow.
    print("DAILYCHECK_NATIVE_EVIDENCE=" + encoded)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("job", "report-generate", "report-verify", "python-gate"))
    parser.add_argument("--context", choices=("test", "agent-framework-reports"))
    parser.add_argument("--docker", default="/usr/bin/docker")
    parser.add_argument("--docker-config")
    parser.add_argument("--docker-host")
    parser.add_argument("--gate", choices=PYTHON_GATES)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args, arguments = parser.parse_known_args()
    if args.command == "python-gate":
        if not args.gate:
            parser.error("--gate required")
        raise SystemExit(run_python_gate(args.gate, args.repo, arguments[1:] if arguments[:1] == ["--"] else arguments))
    if arguments:
        parser.error("unexpected arguments")
    if args.command == "job":
        job(args)
    else:
        report_inner(Path("/candidate"), args.command)


def entrypoint() -> int:
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError,
            RuntimeError) as error:
        print(_failure_diagnostic(error), file=sys.stderr)
        print("trusted native producer failed; no success attestation", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(entrypoint())

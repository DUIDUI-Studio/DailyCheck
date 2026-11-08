"""Hosted runtime profile, transport and workflow contracts.

Everything here is synthetic: fake openers and fake image inspections test
parsing and fail-closed behavior, never an actual hosted runner or asset.
"""
from __future__ import annotations

import copy
import hashlib
import inspect
import json
import os
import stat
import subprocess
import sys
from http.client import IncompleteRead
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest
import yaml

from scripts.ci import trusted_producer as producer
from scripts.ci.pytest_summary import MAX_SUMMARY_BYTES, extract_pytest_summary

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_RAW = (ROOT / "scripts/ci/producer_manifest.json").read_bytes()
MANIFEST = json.loads(MANIFEST_RAW)
MANIFEST_BLOB = producer.blob_id(MANIFEST_RAW)
RUNTIME = MANIFEST["runtime"]
GRAPH = RUNTIME["image_graph"]
LAYERS = GRAPH["rootfs_diff_ids"]
API_ARCHIVE_URL = ("https://api.github.com/repos/DUIDUI-Studio/DailyCheck/"
                   "releases/assets/723434734")
BROWSER_ARCHIVE_URL = (
    "https://github.com/DUIDUI-Studio/DailyCheck/releases/download/"
    "ci-runtime-20261108-arm64-2ab572f1/dailycheck-ci-linux-arm64-2ab572f1.tar.gz")


def _raw(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, indent=2).encode() + b"\n"


def _parse(value: dict) -> dict:
    raw = _raw(value)
    return producer.parse_manifest(raw, producer.blob_id(raw))


def _local_manifest() -> dict:
    value = copy.deepcopy(MANIFEST)
    value["runtime"] = {"controller": "approved_workflow_platform_python_I",
                        "image": "sha256:" + "a" * 64, "platform": "linux/arm64",
                        "scope": "local_fixture_only"}
    return value


def _workflow() -> dict:
    return yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)


class _Response:
    def __init__(self, chunks, status=200):
        self._chunks, self.status = iter(chunks), status

    def read(self, size=-1):
        item = next(self._chunks, b"")
        if isinstance(item, BaseException):
            raise item
        return item

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _opener(chunks=(), *, status=200, error=None):
    class Opener:
        def open(self, request, timeout=None):
            if error is not None:
                raise error
            return _Response(chunks, status)

    return Opener()


class _SequenceOpener:
    """Repeat the last controlled effect for every attempt."""

    def __init__(self, effects):
        self.effects = list(effects)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        effect = self.effects[min(len(self.requests) - 1, len(self.effects) - 1)]
        if isinstance(effect, BaseException):
            raise effect
        return effect


def _runtime_for(payload):
    runtime = copy.deepcopy(RUNTIME)
    runtime["archive"].update(bytes=len(payload),
                              sha256=hashlib.sha256(payload).hexdigest())
    return runtime


def _record_sleeps(monkeypatch):
    sleeps = []
    monkeypatch.setattr(producer.time, "sleep", sleeps.append)
    return sleeps


KNOWN_IDS = [line.strip() for line in
             (ROOT / "tests/known_failures.txt").read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
KNOWN_ID = KNOWN_IDS[0]
NEW_FAILURE_ID = "tests/definitely_new.py::test_x"


def _evidence_fixture(*, raw=None, failed_ids=None, native=1, passed=100):
    """Realistic completed-pytest raw/session/capture for make_test_envelope."""
    failed_ids = [KNOWN_ID] if failed_ids is None else list(failed_ids)
    counts = {"passed": passed, "failed": len(failed_ids), "skipped": 0,
              "xfailed": 0, "xpassed": 0, "error": 0}
    selected = passed + len(failed_ids)
    session = {"schema_version": 1, "pytest_exit": native, "selected": selected,
               "selected_unique": selected, "completed": selected, "uncompleted": [],
               "unexpected": [], "failed_nodeids": failed_ids, "error_nodeids": [],
               "counts": counts}
    if raw is None:
        raw = ("".join(f"FAILED {node} - old baseline\n" for node in failed_ids)
               + f"{len(failed_ids)} failed, {passed} passed in 1s\n").encode()
    capture = {"version": 1, "container_id": "5" * 64, "image": "sha256:" + "a" * 64,
               "kind": "pytest", "context": "test", "binding_sha256": "b" * 64,
               "native_exit": native, "stdout_sha256": hashlib.sha256(raw).hexdigest(),
               "session_sha256": "c" * 64, "exited": True, "removed": True,
               "oom_killed": False}
    return raw, session, capture, {"version": 3, "phase": "H"}


class _Clock:
    """Controlled monotonic clock; reads advance it by a fixed step."""

    def __init__(self, step):
        self.step, self.now = step, 0.0

    def __call__(self):
        return self.now


class _TrickleResponse:
    status = 200

    def __init__(self, payload, clock):
        self.chunks = [payload[index:index + 1] for index in range(len(payload))]
        self.chunks.append(b"")
        self.clock = clock

    def read(self, size=-1):
        self.clock.now += self.clock.step
        return self.chunks.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _TrickleOpener:
    """One valid byte per read, then EOF; every read advances the clock."""

    def __init__(self, payload, clock):
        self.payload, self.clock = payload, clock
        self.opens, self.timeout = 0, None

    def open(self, request, timeout=None):
        self.opens += 1
        self.timeout = timeout
        return _TrickleResponse(self.payload, self.clock)


def _inspection(ident, *, architecture="arm64", os_name="linux", layers=None):
    return {"Id": ident, "Architecture": architecture, "Os": os_name,
            "RootFS": {"Layers": LAYERS if layers is None else layers}}


def test_repository_manifest_pins_the_prepared_public_runtime():
    manifest = producer.parse_manifest(MANIFEST_RAW, MANIFEST_BLOB)
    runtime = manifest["runtime"]
    assert runtime["scope"] == "approved_hosted_archive"
    assert runtime["platform"] == "linux/arm64"
    assert runtime["image"] == GRAPH["index_digest"]
    assert len({GRAPH["index_digest"], GRAPH["platform_manifest_digest"],
                GRAPH["config_digest"]}) == 3
    assert runtime["archive"] == {
        "bytes": 120584124,
        "sha256": "09e4269f8e35cc6f7101102159cb8785a9db962e6be256a63bd15b991b645c48",
        "url": API_ARCHIVE_URL}
    assert GRAPH["rootfs_diff_ids_sha256"] == producer.digest(LAYERS)
    for name, blob in manifest["files"].items():
        assert producer.blob_id((ROOT / name).read_bytes()) == blob, name
    assert manifest["files"]["scripts/ci/trusted_producer.py"] == producer.blob_id(
        (ROOT / "scripts/ci/trusted_producer.py").read_bytes())
    assert producer.approved_images(manifest) == {
        GRAPH["index_digest"], GRAPH["platform_manifest_digest"], GRAPH["config_digest"]}


def test_local_fixture_profile_and_budget_remain_exact():
    manifest = _parse(_local_manifest())
    image = manifest["runtime"]["image"]
    assert manifest["runtime"]["scope"] == "local_fixture_only"
    assert producer.approved_images(manifest) == {image}
    assert producer.capture_budget(manifest, "pytest") == (180, False)
    assert producer.capture_budget(manifest, "lint") == (180, False)


def test_hosted_pytest_owns_disk_scratch_and_long_budget():
    assert producer.capture_budget(MANIFEST, "pytest") == (2700, True)
    assert producer.capture_budget(MANIFEST, "lint") == (180, False)
    assert producer.capture_budget(MANIFEST, "report-generate") == (180, False)


@pytest.mark.parametrize("damage", [
    "extra", "missing", "version", "image-index", "duplicate-digest", "rootfs-hash",
    "rootfs-duplicate", "http-url", "foreign-url", "query-url", "zero-bytes",
    "oversize-bytes", "platform", "unknown-scope", "api-other-owner", "api-zero-asset",
    "api-traversal",
])
def test_hosted_profile_rejects_unknown_missing_or_contradictory_fields(damage):
    value = copy.deepcopy(MANIFEST)
    runtime, graph, archive = value["runtime"], value["runtime"]["image_graph"], value["runtime"]["archive"]
    if damage == "extra":
        runtime["extra"] = "x"
    elif damage == "missing":
        archive.pop("sha256")
    elif damage == "version":
        value["version"] = 2
    elif damage == "image-index":
        runtime["image"] = "sha256:" + "b" * 64
    elif damage == "duplicate-digest":
        graph["platform_manifest_digest"] = graph["config_digest"]
    elif damage == "rootfs-hash":
        graph["rootfs_diff_ids_sha256"] = "0" * 64
    elif damage == "rootfs-duplicate":
        graph["rootfs_diff_ids"][1] = graph["rootfs_diff_ids"][0]
    elif damage == "http-url":
        archive["url"] = archive["url"].replace("https:", "http:")
    elif damage == "foreign-url":
        archive["url"] = "https://github.com/other/repo/releases/download/t/a.tar.gz"
    elif damage == "query-url":
        archive["url"] += "?signed=1"
    elif damage == "zero-bytes":
        archive["bytes"] = 0
    elif damage == "oversize-bytes":
        archive["bytes"] = producer.ARCHIVE_LIMIT + 1
    elif damage == "platform":
        runtime["platform"] = "linux/amd64"
    elif damage == "api-other-owner":
        archive["url"] = API_ARCHIVE_URL.replace("DUIDUI-Studio", "other")
    elif damage == "api-zero-asset":
        archive["url"] = API_ARCHIVE_URL.rsplit("/", 1)[0] + "/0"
    elif damage == "api-traversal":
        archive["url"] = API_ARCHIVE_URL.rsplit("/", 1)[0] + "/%2e%2e/723434734"
    else:
        runtime["scope"] = "candidate_choice"
    with pytest.raises(ValueError):
        _parse(value)


def test_archive_route_profile_accepts_api_and_v1_browser_routes():
    producer.validate_archive_route(API_ARCHIVE_URL)
    producer.validate_archive_route(BROWSER_ARCHIVE_URL)


@pytest.mark.parametrize("url", [
    "http://api.github.com/repos/DUIDUI-Studio/DailyCheck/releases/assets/723434734",
    "https://api.github.com/repos/other/DailyCheck/releases/assets/723434734",
    "https://api.github.com/repos/DUIDUI-Studio/other/releases/assets/723434734",
    "https://example.invalid/repos/DUIDUI-Studio/DailyCheck/releases/assets/723434734",
    "https://api.github.com/repos/DUIDUI-Studio/DailyCheck/releases/assets/0",
    "https://api.github.com/repos/DUIDUI-Studio/DailyCheck/releases/assets/-1",
    "https://api.github.com/repos/DUIDUI-Studio/DailyCheck/releases/assets/023",
    "https://api.github.com/repos/DUIDUI-Studio/DailyCheck/releases/assets/id",
    API_ARCHIVE_URL + "?token=1",
    API_ARCHIVE_URL + "#fragment",
    "https://user:pass@api.github.com/repos/DUIDUI-Studio/DailyCheck/releases/assets/723434734",
    "https://api.github.com/repos/DUIDUI-Studio/DailyCheck/releases/assets/..%2F723434734",
    "https://api.github.com/repos/DUIDUI-Studio/DailyCheck/releases/assets/%2e%2e/723434734",
    "https://api.github.com/repos/DUIDUI-Studio/DailyCheck/releases/assets/723434734/",
    "https://api.github.com/repos/DUIDUI-Studio/DailyCheck/releases/assets/723434734/extra",
    "https://api.github.com/repos/DUIDUI-Studio/DailyCheck/releases/download/t/a.tar.gz",
])
def test_archive_route_profile_rejects_unapproved_routes(url):
    with pytest.raises(ValueError):
        producer.validate_archive_route(url)
    with pytest.raises(ValueError):
        producer.validate_archive_route(None)


@pytest.mark.parametrize("damage", ["image", "platform", "identity"])
def test_capture_accepts_only_the_approved_image_graph(damage):
    binding = {"version": 3}
    for image in (GRAPH["index_digest"], GRAPH["platform_manifest_digest"],
                  GRAPH["config_digest"]):
        producer.validate_capture(
            {"version": 1, "container_id": "5" * 64, "image": image, "kind": "lint",
             "context": "test", "binding_sha256": producer.digest(binding), "native_exit": 0,
             "stdout_sha256": "6" * 64, "session_sha256": None, "exited": True,
             "removed": True, "oom_killed": False}, MANIFEST, binding, "lint", "test")
    image = {"image": "sha256:" + "f" * 64, "platform": "amd64",
             "identity": "sha256:" + "e" * 64}[damage]
    with pytest.raises(ValueError):
        producer.validate_capture(
            {"version": 1, "container_id": "5" * 64, "image": image, "kind": "lint",
             "context": "test", "binding_sha256": producer.digest(binding), "native_exit": 0,
             "stdout_sha256": "6" * 64, "session_sha256": None, "exited": True,
             "removed": True, "oom_killed": False}, MANIFEST, binding, "lint", "test")


def test_download_streams_exact_size_and_hash(tmp_path):
    payload = b"approved-runtime-archive" * 4096
    runtime = copy.deepcopy(RUNTIME)
    runtime["archive"].update(bytes=len(payload),
                              sha256=hashlib.sha256(payload).hexdigest())
    destination = tmp_path / "archive.tar.gz"
    producer.download_archive(runtime, destination,
                              opener=_opener([payload[:1000], payload[1000:9000], payload[9000:]]))
    assert destination.read_bytes() == payload


def test_download_accepts_api_asset_route_and_never_forwards_auth(tmp_path):
    payload = b"pinned-api-asset" * 4096
    runtime = copy.deepcopy(RUNTIME)
    runtime["archive"].update(url=API_ARCHIVE_URL, bytes=len(payload),
                              sha256=hashlib.sha256(payload).hexdigest())
    seen = []

    class Opener:
        def open(self, request, timeout=None):
            seen.append(request)
            return _Response([payload[:8000], payload[8000:]])

    destination = tmp_path / "archive.tar.gz"
    producer.download_archive(runtime, destination, opener=Opener())
    assert destination.read_bytes() == payload
    assert [request.full_url for request in seen] == [API_ARCHIVE_URL]
    assert seen[0].get_header("Authorization") is None
    assert seen[0].get_header("Accept") == "application/octet-stream"


def test_download_revalidates_the_route_at_its_own_boundary(tmp_path):
    runtime = copy.deepcopy(RUNTIME)
    runtime["archive"]["url"] = API_ARCHIVE_URL.replace("DUIDUI-Studio", "other")
    destination = tmp_path / "archive.tar.gz"
    with pytest.raises(ValueError):
        producer.download_archive(runtime, destination, opener=_opener([b"bytes"]))
    assert not destination.exists()


@pytest.mark.parametrize("damage", ["long", "partial", "hash", "status", "missing",
                                    "network", "foreign-url", "json"])
def test_download_rejects_bad_bytes_before_any_load(tmp_path, monkeypatch, damage):
    _record_sleeps(monkeypatch)
    payload = b"pinned-bytes" * 100
    runtime = copy.deepcopy(RUNTIME)
    runtime["archive"].update(bytes=len(payload),
                              sha256=hashlib.sha256(payload).hexdigest())
    destination = tmp_path / "archive.tar.gz"
    if damage == "long":
        runtime["archive"]["bytes"] = len(payload) - 1
        opener = _opener([payload])
    elif damage == "partial":
        runtime["archive"]["bytes"] = len(payload) + 1
        opener = _opener([payload])
    elif damage == "hash":
        runtime["archive"]["sha256"] = "0" * 64
        opener = _opener([payload])
    elif damage == "status":
        opener = _opener([payload], status=302)
    elif damage == "missing":
        opener = _opener(error=HTTPError("https://github.com/approved", 404, "missing", {}, None))
    elif damage == "network":
        opener = _opener(error=URLError("offline"))
    elif damage == "json":
        runtime = copy.deepcopy(RUNTIME)
        opener = _opener([b'{"message":"Not Found"}'])
    else:
        runtime["archive"]["url"] = "https://example.invalid/runtime.tar.gz"
        opener = _opener([payload])
    with pytest.raises(ValueError):
        producer.download_archive(runtime, destination, opener=opener)
    assert not destination.exists()


def test_transient_transport_retries_then_streams_exact_bytes(tmp_path, monkeypatch):
    payload = b"retry-payload" * 4096
    sleeps = _record_sleeps(monkeypatch)
    opener = _SequenceOpener([URLError("transient"),
                              _Response([payload[:7000], payload[7000:]])])
    destination = tmp_path / "archive.tar.gz"
    producer.download_archive(_runtime_for(payload), destination, opener=opener)
    assert destination.read_bytes() == payload
    assert len(opener.requests) == 2
    assert sleeps == [producer.ARCHIVE_BACKOFF_SECONDS[0]]
    assert all(request.full_url == API_ARCHIVE_URL for request in opener.requests)


def test_transport_retry_budget_is_finite_and_leaves_no_file(tmp_path, monkeypatch):
    payload = b"retry-exhausted" * 4096
    sleeps = _record_sleeps(monkeypatch)
    opener = _SequenceOpener([URLError("offline")])
    destination = tmp_path / "archive.tar.gz"
    with pytest.raises(producer.ArchiveTransportError) as caught:
        producer.download_archive(_runtime_for(payload), destination, opener=opener)
    assert producer.ARCHIVE_ATTEMPTS == 3
    assert len(opener.requests) == producer.ARCHIVE_ATTEMPTS
    assert caught.value.attempts == producer.ARCHIVE_ATTEMPTS
    assert sleeps == list(producer.ARCHIVE_BACKOFF_SECONDS)
    assert not destination.exists()
    assert ("[ci-producer] operation=startup category=transport attempts=3"
            in producer._failure_diagnostic(caught.value))


@pytest.mark.parametrize("status", sorted(producer.TRANSIENT_HTTP_STATUSES))
def test_transient_http_statuses_retry_then_succeed(status, tmp_path, monkeypatch):
    payload = b"transient-http" * 4096
    sleeps = _record_sleeps(monkeypatch)
    opener = _SequenceOpener([
        HTTPError(API_ARCHIVE_URL, status, "transient", {}, None),
        _Response([payload])])
    destination = tmp_path / "archive.tar.gz"
    producer.download_archive(_runtime_for(payload), destination, opener=opener)
    assert destination.read_bytes() == payload
    assert len(opener.requests) == 2
    assert sleeps == [producer.ARCHIVE_BACKOFF_SECONDS[0]]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 416, 501, 505])
def test_permanent_http_statuses_fail_without_retry(status, tmp_path, monkeypatch):
    payload = b"permanent-http" * 4096
    sleeps = _record_sleeps(monkeypatch)
    opener = _SequenceOpener([
        HTTPError(API_ARCHIVE_URL, status, "permanent", {}, None)])
    destination = tmp_path / "archive.tar.gz"
    with pytest.raises(producer.ArchiveHTTPError) as caught:
        producer.download_archive(_runtime_for(payload), destination, opener=opener)
    assert len(opener.requests) == 1
    assert caught.value.attempts == 1 and caught.value.http_status == status
    assert sleeps == [] and not destination.exists()
    assert (f"category=http http_status={status} attempts=1"
            in producer._failure_diagnostic(caught.value))


@pytest.mark.parametrize("damage", ["hash", "size"])
def test_integrity_mismatch_is_never_retried(tmp_path, monkeypatch, damage):
    payload = b"integrity" * 4096
    runtime = _runtime_for(payload)
    if damage == "hash":
        runtime["archive"]["sha256"] = "0" * 64
    else:
        runtime["archive"]["bytes"] = len(payload) + 1
    sleeps = _record_sleeps(monkeypatch)
    opener = _SequenceOpener([_Response([payload])])
    destination = tmp_path / "archive.tar.gz"
    with pytest.raises(producer.ArchiveIntegrityError) as caught:
        producer.download_archive(runtime, destination, opener=opener)
    assert len(opener.requests) == 1
    assert caught.value.attempts == 1
    assert sleeps == [] and not destination.exists()
    assert "category=integrity attempts=1" in producer._failure_diagnostic(caught.value)


def test_incomplete_read_is_retried_and_partial_file_removed(tmp_path, monkeypatch):
    payload = b"partial-read" * 4096
    sleeps = _record_sleeps(monkeypatch)
    destination = tmp_path / "archive.tar.gz"

    class Opener:
        def __init__(self):
            self.calls = 0

        def open(self, request, timeout=None):
            self.calls += 1
            # Every attempt starts clean: no partial file from the prior one.
            assert not destination.exists()
            if self.calls == 1:
                return _Response([payload[:512], IncompleteRead(payload[:512])])
            return _Response([payload[:3000], payload[3000:]])

    opener = Opener()
    producer.download_archive(_runtime_for(payload), destination, opener=opener)
    assert opener.calls == 2
    assert destination.read_bytes() == payload
    assert sleeps == [producer.ARCHIVE_BACKOFF_SECONDS[0]]


def test_retry_delay_ignores_untrusted_retry_after(tmp_path, monkeypatch):
    payload = b"retry-after" * 4096
    sleeps = _record_sleeps(monkeypatch)
    opener = _SequenceOpener([
        HTTPError(API_ARCHIVE_URL, 503, "busy", {"Retry-After": "999999"}, None),
        _Response([payload])])
    destination = tmp_path / "archive.tar.gz"
    producer.download_archive(_runtime_for(payload), destination, opener=opener)
    assert sleeps == [producer.ARCHIVE_BACKOFF_SECONDS[0]]
    assert 999999 not in sleeps


def test_slow_but_valid_stream_aborts_at_the_deadline(tmp_path, monkeypatch):
    # Root counterexample: every read returns valid bytes while the clock jumps
    # 200s. A pre-deadline-only check would succeed after 600s.
    payload = b"ab"
    clock = _Clock(step=200.0)
    monkeypatch.setattr(producer.time, "monotonic", clock)
    opener = _TrickleOpener(payload, clock)
    destination = tmp_path / "archive.tar.gz"
    with pytest.raises(producer.ArchiveTimeoutError) as caught:
        producer.download_archive(_runtime_for(payload), destination, opener=opener)
    assert opener.opens == 1
    assert opener.timeout == producer.ARCHIVE_HTTP_TIMEOUT
    assert caught.value.attempts == 1
    assert clock.now == 400.0  # stopped after the first post-deadline read
    assert not destination.exists()
    assert "category=timeout attempts=1" in producer._failure_diagnostic(caught.value)


def test_fast_stream_still_downloads_exactly(tmp_path, monkeypatch):
    payload = b"ab"
    clock = _Clock(step=1.0)
    monkeypatch.setattr(producer.time, "monotonic", clock)
    opener = _TrickleOpener(payload, clock)
    destination = tmp_path / "archive.tar.gz"
    producer.download_archive(_runtime_for(payload), destination, opener=opener)
    assert destination.read_bytes() == payload
    assert clock.now == 3.0


def test_slow_failed_open_cannot_retry_past_the_deadline(tmp_path, monkeypatch):
    payload = b"ab"
    clock = _Clock(step=0.0)
    monkeypatch.setattr(producer.time, "monotonic", clock)
    sleeps = _record_sleeps(monkeypatch)

    class Opener:
        def __init__(self):
            self.opens = 0

        def open(self, request, timeout=None):
            self.opens += 1
            clock.now += producer.ARCHIVE_DEADLINE_SECONDS + 1
            raise URLError("slow failure")

    opener = Opener()
    destination = tmp_path / "archive.tar.gz"
    with pytest.raises(producer.ArchiveTransportError) as caught:
        producer.download_archive(_runtime_for(payload), destination, opener=opener)
    assert opener.opens == 1 and caught.value.attempts == 1
    assert sleeps == [] and not destination.exists()


def test_default_opener_is_rebuilt_for_each_attempt(tmp_path, monkeypatch):
    payload = b"opener-fresh" * 128
    made = []

    class Opener:
        def __init__(self, index):
            self.index, self.opens = index, 0

        def open(self, request, timeout=None):
            self.opens += 1
            if self.index == 0:
                raise URLError("transient")
            return _Response([payload])

    def factory():
        made.append(Opener(len(made)))
        return made[-1]

    monkeypatch.setattr(producer, "_public_opener", factory)
    sleeps = _record_sleeps(monkeypatch)
    destination = tmp_path / "archive.tar.gz"
    producer.download_archive(_runtime_for(payload), destination)
    assert len(made) == 2
    assert [opener.opens for opener in made] == [1, 1]
    assert destination.read_bytes() == payload
    assert sleeps == [producer.ARCHIVE_BACKOFF_SECONDS[0]]


def test_redirect_budget_is_fresh_per_opener_instance():
    request = producer.Request("https://github.com/approved")

    def redirect(handler):
        return handler.redirect_request(
            request, None, 302, "Found", {},
            "https://objects.githubusercontent.com/a")

    exhausted = producer._PublicRedirect()
    for _ in range(producer.MAX_REDIRECTS):
        redirect(exhausted)
    with pytest.raises(ValueError):
        redirect(exhausted)
    assert redirect(producer._PublicRedirect()).full_url.endswith("/a")


def test_redirect_and_route_policy_failures_are_not_retried(tmp_path, monkeypatch):
    payload = b"policy" * 4096
    sleeps = _record_sleeps(monkeypatch)
    opener = _SequenceOpener([ValueError("unapproved runtime archive redirect")])
    destination = tmp_path / "archive.tar.gz"
    with pytest.raises(ValueError) as caught:
        producer.download_archive(_runtime_for(payload), destination, opener=opener)
    assert type(caught.value) is ValueError
    assert len(opener.requests) == 1
    assert sleeps == [] and not destination.exists()
    assert "category=invalid_data" in producer._failure_diagnostic(caught.value)

    route = _runtime_for(payload)
    route["archive"]["url"] = API_ARCHIVE_URL.replace("DUIDUI-Studio", "other")
    destination.write_bytes(b"stale-partial")
    unopened = _SequenceOpener([_Response([payload])])
    with pytest.raises(ValueError):
        producer.download_archive(route, destination, opener=unopened)
    assert unopened.requests == [] and not destination.exists()


def test_disk_preflight_is_a_distinct_stage_and_category(monkeypatch, tmp_path):
    monkeypatch.setattr(producer, "OPERATION", "startup")
    monkeypatch.setattr(producer.shutil, "disk_usage",
                        lambda path: SimpleNamespace(free=0))
    monkeypatch.setattr(producer, "inspect_image", lambda docker, ref: None)
    downloads = []
    monkeypatch.setattr(producer, "download_archive",
                        lambda *args, **kwargs: downloads.append(args))
    with pytest.raises(producer.ArchivePreflightError) as caught:
        producer.ensure_approved_runtime(["docker"], MANIFEST, tmp_path)
    assert producer.OPERATION == "runtime-preflight"
    assert producer._failure_diagnostic(caught.value) == (
        "[ci-producer] operation=runtime-preflight category=preflight")
    assert downloads == []
    assert not (tmp_path / "runtime/approved-runtime.tar.gz").exists()


@pytest.mark.parametrize("error,expected", [
    (producer.ArchiveTransportError(3), "category=transport attempts=3"),
    (producer.ArchiveHTTPError(503, 3), "category=http http_status=503 attempts=3"),
    (producer.ArchiveIntegrityError(1), "category=integrity attempts=1"),
    (producer.ArchiveTimeoutError(2), "category=timeout attempts=2"),
    (producer.ArchivePreflightError(), "category=preflight"),
])
def test_archive_diagnostics_use_only_fixed_tokens(monkeypatch, error, expected):
    secret = "https://release-assets.githubusercontent.com/x?X-Amz-Signature=SECRET"
    error.args = (secret,)
    diagnostic = producer._failure_diagnostic(error)
    assert expected in diagnostic
    assert "SECRET" not in diagnostic and "release-assets" not in diagnostic


def test_valid_known14_evidence_envelope_is_unchanged():
    raw, session, capture, binding = _evidence_fixture()
    envelope = producer.make_test_envelope(raw, session, capture, binding)
    assert envelope["schema_version"] == 2 and envelope["binding"] == binding
    assert envelope["text"] == extract_pytest_summary(raw.decode("utf-8"))
    assert envelope["evidence"]["native_exit"] == 1
    assert envelope["evidence"]["session"] == session
    assert envelope["evidence"]["raw_output_sha256"] == hashlib.sha256(raw).hexdigest()
    # Digest captured from the pristine producer at HEAD 1e8c0f4.
    assert (hashlib.sha256(json.dumps(envelope, sort_keys=True).encode()).hexdigest()
            == "b7d9c69b0461ad55340cc978c992489c00b8702115cd2cd68457573f8a17166d")


@pytest.mark.parametrize("case,reason,subreason,validated", [
    ("session", "raw-session-invalid", "completion", "0"),
    ("decode", "raw-session-invalid", "decode", "0"),
    ("native-exit", "raw-session-invalid", "native-exit", "0"),
    ("counts-shape", "raw-session-invalid", "counts-shape", "0"),
    ("raw-summary", "raw-session-invalid", "raw-summary", "0"),
    ("raw-error", "raw-session-invalid", "raw-error", "0"),
    ("new-failure", "new-failures", None, "1"),
    ("summary", "summary-extraction", None, "1"),
    ("compact", "compact-evidence-invalid", "raw-summary", "1"),
])
def test_evidence_failure_surfaces_have_fixed_reasons_and_subreasons(
        monkeypatch, case, reason, subreason, validated):
    monkeypatch.setattr(producer, "OPERATION", "startup")
    if case == "new-failure":
        raw, session, capture, binding = _evidence_fixture(failed_ids=[NEW_FAILURE_ID])
    elif case == "summary":
        raw = (f"FAILED {KNOWN_ID} - old baseline\n"
               + "x" * (MAX_SUMMARY_BYTES + 1) + "\n1 failed, 100 passed in 1s\n").encode()
        raw, session, capture, binding = _evidence_fixture(raw=raw)
    elif case == "compact":
        raw = (f"FAILED {KNOWN_ID} - old baseline\n1 failed, 100 passed in 1s\n"
               + "\n" * 6).encode()
        raw, session, capture, binding = _evidence_fixture(raw=raw)
    elif case == "raw-summary":
        raw = f"FAILED {KNOWN_ID} - old baseline\n".encode()
        raw, session, capture, binding = _evidence_fixture(raw=raw)
    elif case == "raw-error":
        raw = b"ERROR tests/broken.py - ImportError\n0 failed, 101 passed in 1s\n"
        raw, session, capture, binding = _evidence_fixture(
            raw=raw, failed_ids=[], native=0, passed=101)
    elif case == "native-exit":
        raw, session, capture, binding = _evidence_fixture()
        capture["native_exit"] = 2
    elif case == "counts-shape":
        raw, session, capture, binding = _evidence_fixture()
        session["counts"].pop("xfailed")
    else:
        raw, session, capture, binding = _evidence_fixture()
        if case == "session":
            session["completed"] = session["selected"] - 1
        else:
            raw = b"\xff\xfe"
    producer._stage("evidence")
    with pytest.raises(producer.EvidenceValidationError) as caught:
        producer.make_test_envelope(raw, session, capture, binding)
    error = caught.value
    assert error.reason == reason and error.subreason == subreason
    assert producer._failure_category(error) == "invalid_data"
    diagnostic = producer._failure_diagnostic(error)
    assert f"reason={reason}" in diagnostic
    assert f"session_validated={validated}" in diagnostic
    if subreason is None:
        assert "subreason=" not in diagnostic
    else:
        assert f"subreason={subreason}" in diagnostic
    if case == "new-failure":
        assert "new_failures=1" in diagnostic
        assert "new_tests=tests/definitely_new.py::test_x" in diagnostic
    else:
        assert "new_failures=" not in diagnostic


def test_evidence_diagnostic_strips_parameters_and_candidate_text(monkeypatch):
    monkeypatch.setattr(producer, "OPERATION", "startup")
    injected = "EXCEPTION-TEXT-SECRET"
    nodeid = f"tests/definitely_new.py::test_x[{injected}]"
    raw, session, capture, binding = _evidence_fixture(failed_ids=[nodeid])
    producer._stage("evidence")
    with pytest.raises(producer.EvidenceValidationError) as caught:
        producer.make_test_envelope(raw, session, capture, binding)
    diagnostic = producer._failure_diagnostic(caught.value)
    assert "reason=new-failures" in diagnostic and "new_failures=1" in diagnostic
    assert "new_tests=tests/definitely_new.py::test_x" in diagnostic
    assert injected not in diagnostic and "actual native pytest" not in diagnostic


@pytest.mark.parametrize("message,token", [
    ("pytest native exit must be 0/1, got 2", "native-exit"),
    ("missing/invalid pytest session evidence", "session-shape"),
    ("native exit disagrees with pytest session exit", "session-exit"),
    ("duplicate/inconsistent collected nodeids", "selected-unique"),
    ("pytest did not complete every selected test", "completion"),
    ("missing/incomplete outcome counts", "counts-shape"),
    ("pytest collection/setup/teardown errors cannot be known failures", "errors"),
    ("outcome enumeration does not match selected/completed count", "count-sum"),
    ("native exit disagrees with test outcomes", "outcome-exit"),
    ("pytest output is empty/interrupted/internal/collection/incomplete", "raw-output"),
    ("missing/truncated/ambiguous terminal pytest summary (原始输出 required)", "raw-summary"),
    ("unsupported pytest summary outcome: weird", "summary-outcome"),
    ("duplicate pytest summary outcome: failed", "summary-outcome-duplicate"),
    ("terminal summary counts disagree with pytest report enumeration; 原始输出/evidence required",
     "summary-counts"),
    ("invalid failed-node enumeration", "failed-ids"),
    ("raw output contains pytest ERROR", "raw-error"),
    ("unrecognized FAILED line; gate requires pytest 原始输出", "raw-failed"),
    ("FAILED enumeration missing/duplicated/inconsistent with terminal counts", "failed-enumeration"),
    ("invalid selected", "counts-invalid"),
])
def test_checker_subreasons_are_a_closed_fixed_mapping(message, token):
    assert producer._checker_subreason(ValueError(message)) == token
    assert token in producer.EVIDENCE_SUBREASONS


def test_checker_subreason_unknown_is_fixed_and_does_not_leak():
    secret = "candidate-secret-and-exception-detail"
    assert producer._checker_subreason(ValueError(secret)) == "unknown"
    assert ({token for _, token in producer.CHECKER_SUBREASON_PREFIXES}
            | {"decode", "unknown"} == producer.EVIDENCE_SUBREASONS)
    error = producer.EvidenceValidationError(
        producer.EVIDENCE_RAW_SESSION, subreason="unknown")
    assert secret not in producer._failure_diagnostic(error)


def test_diagnostic_test_locations_are_bounded_and_safe():
    assert producer._safe_test_location("tests/a.py::test_x[param-1]") == "tests/a.py::test_x"
    assert (producer._safe_test_location("tests/a.py::test_x[" + "z" * 200 + "]")
            == "tests/a.py::test_x")
    assert (producer._safe_test_location("tests/pkg/mod.py::TestC::test_y")
            == "tests/pkg/mod.py::TestC::test_y")
    for bad in ("scripts/a.py::test_x", "tests/../a.py::test_x", "tests/a.py::test x",
                "tests/a.py::test_x;rm", "tests/" + "a" * 130 + ".py::test_x", None):
        assert producer._safe_test_location(bad) is None
    many = [f"tests/test_{index}.py::test_{index}" for index in range(10)]
    assert len(producer._diagnostic_test_locations(many)) == producer.MAX_DIAGNOSTIC_LOCATIONS
    assert producer._diagnostic_test_locations(
        ["tests/a.py::test_x[1]", "tests/a.py::test_x[2]"]) == ("tests/a.py::test_x",)


def test_evidence_reason_and_metadata_reject_unapproved_tokens():
    with pytest.raises(ValueError):
        producer.EvidenceValidationError("candidate-controlled-reason")
    with pytest.raises(ValueError):
        producer.EvidenceValidationError(producer.EVIDENCE_RAW_SESSION)
    with pytest.raises(ValueError):
        producer.EvidenceValidationError(producer.EVIDENCE_RAW_SESSION,
                                         subreason="candidate-controlled")
    with pytest.raises(ValueError):
        producer.EvidenceValidationError(producer.EVIDENCE_NEW_FAILURES, 1,
                                         new_tests=("scripts/a.py::test_x",))


def test_entrypoint_reports_evidence_reason_without_success_marker(monkeypatch, capsys):
    def explode():
        producer._stage("evidence")
        raise producer.EvidenceValidationError(
            producer.EVIDENCE_NEW_FAILURES, 2,
            new_tests=("tests/a.py::test_x", "tests/b.py::test_y"))

    monkeypatch.setattr(producer, "main", explode)
    assert producer.entrypoint() == 1
    error = capsys.readouterr().err
    assert ("[ci-producer] operation=evidence category=invalid_data "
            "reason=new-failures session_validated=1 new_failures=2 "
            "new_tests=tests/a.py::test_x,tests/b.py::test_y" in error)
    assert "trusted native producer failed; no success attestation" in error
    assert "DAILYCHECK_NATIVE_EVIDENCE=" not in error


def test_redirects_are_public_https_and_credential_free():
    handler = producer._PublicRedirect()
    request = producer.Request("https://github.com/approved")
    allowed = handler.redirect_request(
        request, None, 302, "Found", {}, "https://release-assets.githubusercontent.com/a")
    assert allowed.get_header("Authorization") is None
    assert allowed.full_url.endswith("/a")
    for url in ("http://release-assets.githubusercontent.com/a",
                "https://example.invalid/a",
                "https://user:pass@release-assets.githubusercontent.com/a"):
        with pytest.raises(ValueError):
            producer._PublicRedirect().redirect_request(request, None, 302, "Found", {}, url)
    looping = producer._PublicRedirect()
    for _ in range(producer.MAX_REDIRECTS):
        looping.redirect_request(request, None, 302, "Found", {},
                                 "https://objects.githubusercontent.com/a")
    with pytest.raises(ValueError):
        looping.redirect_request(request, None, 302, "Found", {},
                                 "https://objects.githubusercontent.com/a")


def test_redirect_drops_any_forwarded_authorization_header():
    request = producer.Request("https://api.github.com/approved",
                               headers={"Authorization": "Bearer secret",
                                        "Accept": "application/octet-stream"})
    allowed = producer._PublicRedirect().redirect_request(
        request, None, 302, "Found", {},
        "https://release-assets.githubusercontent.com/a?X-Amz-Signature=1")
    assert allowed.get_header("Authorization") is None
    assert allowed.get_header("Accept") == "application/octet-stream"
    assert allowed.full_url.endswith("?X-Amz-Signature=1")


def test_image_resolution_binds_platform_and_rootfs(monkeypatch):
    def resolve(mapping):
        monkeypatch.setattr(producer, "inspect_image", lambda docker, ref: mapping.get(ref))
        return producer.resolve_approved_image(["docker"], RUNTIME)

    assert resolve({}) is None
    assert resolve({GRAPH["config_digest"]:
                    _inspection(GRAPH["config_digest"])}) == GRAPH["config_digest"]
    for mapping in (
            {GRAPH["index_digest"]: _inspection(GRAPH["index_digest"], architecture="amd64")},
            {GRAPH["index_digest"]: _inspection(GRAPH["index_digest"], os_name="windows")},
            {GRAPH["index_digest"]: _inspection(GRAPH["index_digest"], layers=LAYERS[:-1])},
            {GRAPH["index_digest"]: _inspection("sha256:" + "f" * 64)}):
        with pytest.raises(ValueError):
            resolve(mapping)


def test_unavailable_or_corrupt_archive_never_reaches_docker(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(producer, "inspect_image", lambda docker, ref: None)
    monkeypatch.setattr(producer.subprocess, "run",
                        lambda *a, **k: calls.append(a) or pytest.fail("docker ran"))

    def missing(runtime, destination, *, opener=None):
        raise ValueError("approved runtime archive unavailable")

    monkeypatch.setattr(producer, "download_archive", missing)
    with pytest.raises(ValueError):
        producer.ensure_approved_runtime(["docker"], MANIFEST, tmp_path)
    assert calls == []
    assert not (tmp_path / "attestation.json").exists()


def test_low_disk_blocks_capture_and_load(monkeypatch, tmp_path):
    monkeypatch.setattr(producer.shutil, "disk_usage", lambda path: SimpleNamespace(free=0))
    with pytest.raises(ValueError):
        producer._require_free(tmp_path, 1)

    calls = []
    monkeypatch.setattr(producer, "inspect_image", lambda docker, ref: None)
    monkeypatch.setattr(producer.subprocess, "run", lambda *a, **k: calls.append(a))
    with pytest.raises(ValueError):
        producer.ensure_approved_runtime(["docker"], MANIFEST, tmp_path)
    assert calls == []


def test_workflow_uses_arm_runner_and_skips_only_github_push():
    workflow = _workflow()
    test, reports = workflow["jobs"]["test"], workflow["jobs"]["agent-framework-reports"]
    assert "ubuntu-24.04-arm" in test["runs-on"] and "ubuntu-latest" in test["runs-on"]
    assert "github.server_url != 'https://github.com'" in test["if"]
    assert "github.event_name != 'push'" in test["if"]
    assert reports["if"].startswith("always()")
    assert "pull_request" in reports["if"] and "merge_group" in reports["if"]
    assert test["timeout-minutes"] == "60" and reports["timeout-minutes"] == "60"
    native_env = workflow["env"]["CI_NATIVE_PRODUCER"]
    assert "github.server_url == 'https://github.com'" in native_env
    assert "pull_request" in native_env and "merge_group" in native_env
    assert workflow["on"]["push"]["branches"] == ["main"]


def test_native_steps_use_literal_pins_sudo_and_keep_full_pytest():
    workflow = _workflow()
    native = [step for job in workflow["jobs"].values() for step in job["steps"]
              if step["name"] == "Execute approved isolated native producer"]
    assert len(native) == 2
    assert sorted(step["env"]["NATIVE_CONTEXT"] for step in native) == [
        "agent-framework-reports", "test"]
    for step in native:
        assert step["env"]["CI_PRODUCER_MANIFEST_BLOB"] == MANIFEST_BLOB
        assert "${{" not in step["env"]["CI_PRODUCER_MANIFEST_BLOB"]
        assert "sudo -n --preserve-env=" in step["run"]
        assert "python3 -I" in step["run"]
        assert "PYTHONPATH" not in step["run"]
        assert "CI_ROOT" in step["run"] and "NATIVE_QUEUE_READ_TOKEN" in step["run"]
    assert MANIFEST_BLOB in (ROOT / ".github/workflows/ci.yml").read_text()
    pytest_step = next(step for step in workflow["jobs"]["test"]["steps"]
                       if step["name"] == "Pytest (full suite, including MCP)")
    assert "-m pytest" in pytest_step["run"]
    assert "--basetemp=${CI_ROOT}/pytest-dc" in pytest_step["run"]
    for forbidden in ("-k ", "--deselect", "--ignore", "skip"):
        assert forbidden not in pytest_step["run"]
    assert "-m pytest" not in "".join(
        step.get("run", "") for step in workflow["jobs"]["agent-framework-reports"]["steps"])


def test_no_cli_or_environment_runtime_override():
    source = inspect.getsource(producer.main)
    for forbidden in ("--runtime", "--archive", "--image", "--url", "RUNTIME_URL"):
        assert forbidden not in source
    parameters = inspect.signature(producer.ensure_approved_runtime).parameters
    assert parameters["opener"].default is None
    assert "DAILYCHECK_CI_RUNTIME" not in inspect.getsource(producer)


def test_failure_reports_fixed_operation_without_error_detail(monkeypatch, capsys):
    secret = "https://release-assets.githubusercontent.com/secret?token=abc123"

    def explode():
        producer._stage("runtime-archive")
        raise ValueError(secret)

    monkeypatch.setattr(producer, "main", explode)
    assert producer.entrypoint() == 1
    error = capsys.readouterr().err
    assert "[ci-producer] operation=runtime-archive category=invalid_data" in error
    assert "trusted native producer failed; no success attestation" in error
    assert "secret" not in error and "abc123" not in error
    assert "DAILYCHECK_NATIVE_EVIDENCE=" not in error


def test_entrypoint_reports_bounded_archive_attempts(monkeypatch, capsys):
    def explode():
        producer._stage("runtime-archive")
        raise producer.ArchiveTransportError(producer.ARCHIVE_ATTEMPTS)

    monkeypatch.setattr(producer, "main", explode)
    assert producer.entrypoint() == 1
    error = capsys.readouterr().err
    assert ("[ci-producer] operation=runtime-archive category=transport attempts=3"
            in error)
    assert "trusted native producer failed; no success attestation" in error
    assert "DAILYCHECK_NATIVE_EVIDENCE=" not in error


def test_success_reports_no_failure_diagnostic(monkeypatch, capsys):
    monkeypatch.setattr(producer, "main", lambda: None)
    assert producer.entrypoint() == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


@pytest.mark.parametrize("error,category", [
    (ValueError("x"), "invalid_data"),
    (KeyError("CI_ROOT"), "missing_input"),
    (TypeError("x"), "invalid_type"),
    (RuntimeError("x"), "runtime"),
    (OSError("x"), "io"),
    (subprocess.CalledProcessError(1, ["docker"]), "subprocess"),
    (subprocess.TimeoutExpired(["docker"], 1), "subprocess"),
])
def test_failure_category_uses_fixed_tokens(error, category):
    assert producer._failure_category(error) == category


def test_native_failure_details_keep_only_validated_codes():
    state = {"OOMKilled": True, "Error": "untrusted daemon text"}
    details = producer._native_failure_details("pytest", False, 137, state, True)
    assert details == ("native_kind=pytest native_exit=137 timed_out=0 "
                       "oom_killed=1 runtime_error=1 image_approved=1")
    assert producer._native_failure_details(
        "lint", True, "attacker-text", state, False).startswith(
        "native_kind=lint native_exit=unknown timed_out=1 ")
    assert "attacker-text" not in producer._native_failure_details(
        "lint", True, "attacker-text", state, False)
    assert "untrusted daemon text" not in details


def _capture_commands(monkeypatch, tmp_path, *, kind, argv, plugin=None):
    """Run one synthetic capture and return the docker argv it built."""
    repo_plugin = (ROOT / "scripts/ci/pytest_evidence.py").read_bytes()
    plugin = repo_plugin if plugin is None else plugin
    authority = tmp_path / "authority"
    (authority / "scripts/ci").mkdir(parents=True)
    (authority / "scripts/ci/pytest_evidence.py").write_bytes(repo_plugin)
    (authority / "dc_approved_evidence.py").write_bytes(plugin)
    output = tmp_path / "output"
    output.mkdir()
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / ".dc-session.json").write_text(json.dumps({"schema_version": 1,
                                                            "pytest_exit": 0}))
    capture = producer.DockerCapture(["docker"], MANIFEST, authority, output)
    commands = []

    def call(*args, timeout=30):
        commands.append(args)
        if args[0] == "create":
            return b"a" * 64
        if args[0] == "wait":
            return b"0"
        if args[0] == "inspect":
            return json.dumps([{
                "Image": GRAPH["index_digest"],
                "State": {"Running": False, "Status": "exited", "ExitCode": 0,
                          "OOMKilled": False, "Error": ""}}]).encode()
        return b""

    monkeypatch.setattr(capture, "call", call)
    monkeypatch.setattr(producer.shutil, "disk_usage", lambda _: SimpleNamespace(
        free=producer.SCRATCH_RESERVE + producer.HOST_FREE_MARGIN))
    monkeypatch.setattr(
        producer.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=b"1 passed\n", stderr=b""))
    capture.capture(candidate, {"version": 3}, kind, "test", argv, {})
    return commands, output


def _env(command):
    return {command[index + 1] for index, value in enumerate(command) if value == "--env"}


def test_pytest_capture_mounts_trusted_plugin_outside_the_scripts_package(monkeypatch, tmp_path):
    # The candidate repo's scripts/ is a namespace package; the authority's
    # regular scripts package would otherwise shadow every candidate subpackage.
    commands, output = _capture_commands(
        monkeypatch, tmp_path, kind="pytest",
        argv=["python3", "-P", "-m", "pytest", "-p", "dc_approved_evidence"])
    create = next(command for command in commands if command[0] == "create")
    plugin_dir = output / "evidence-plugin"
    assert (plugin_dir / "dc_approved_evidence.py").read_bytes() == (
        ROOT / "scripts/ci/pytest_evidence.py").read_bytes()
    assert (f"type=bind,src={plugin_dir},dst=/trusted-evidence,readonly"
            in create)
    assert "PYTHONPATH=/trusted-evidence" in _env(create)
    assert "PYTHONPATH=/authority" not in _env(create)
    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1" in _env(create)
    assert "--init" in create
    assert "--pids-limit=256" in create
    assert f"type=bind,src={tmp_path / 'candidate'},dst=/candidate" in create


def test_non_pytest_capture_keeps_the_authority_helper_root(monkeypatch, tmp_path):
    commands, output = _capture_commands(monkeypatch, tmp_path, kind="lint",
                                         argv=["ruff", "--version"])
    create = next(command for command in commands if command[0] == "create")
    assert "PYTHONPATH=/authority" in _env(create)
    assert "PYTHONPATH=/trusted-evidence" not in _env(create)
    assert not (output / "evidence-plugin").exists()
    assert not any("/trusted-evidence" in value for value in create)


def test_candidate_import_environment_is_scoped_to_the_tested_child(monkeypatch, tmp_path):
    from tests.conftest import candidate_python_env

    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "config.py").write_text("VALUE = 'candidate under test'\n")
    plugin = tmp_path / "trusted-evidence"
    plugin.mkdir()
    (plugin / "approved_probe.py").write_text("VALUE = 'approved plugin'\n")
    monkeypatch.setenv("PYTHONPATH", str(plugin))
    monkeypatch.setenv("PYTHONSAFEPATH", "1")
    original = dict(os.environ)
    argv = [sys.executable, "-P", "-c",
            "import config, approved_probe; print(config.VALUE, approved_probe.VALUE)"]
    unscoped = subprocess.run(argv, cwd=candidate, capture_output=True, text=True, timeout=10)
    assert unscoped.returncode != 0 and "No module named 'config'" in unscoped.stderr
    scoped = subprocess.run(argv, cwd=candidate, env=candidate_python_env(candidate),
                            capture_output=True, text=True, timeout=10)
    assert scoped.returncode == 0, scoped.stderr
    assert scoped.stdout.strip() == "candidate under test approved plugin"
    assert dict(os.environ) == original


def test_snapshot_keeps_the_bound_base_when_the_source_has_no_main_branch(tmp_path):
    source = tmp_path / "source"
    source.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(source), *args], text=True).strip()

    git("init", "-q", "-b", "fixture-head")
    git("config", "user.name", "Snapshot fixture")
    git("config", "user.email", "snapshot@example.invalid")
    (source / "legacy.py").write_text("print('bound public baseline')\n")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (source / "legacy.py").write_text("print('candidate')\n")
    git("add", ".")
    git("commit", "-qm", "head")
    head = git("rev-parse", "HEAD")
    git("config", "fixture.sourceonly", "not copied")
    candidate = tmp_path / "candidate"
    producer.snapshot(source, candidate, head, base)
    actual = subprocess.check_output(
        ["git", "-C", str(candidate), "show", "origin/main:legacy.py"], text=True)
    assert actual == "print('bound public baseline')\n"
    assert subprocess.check_output(
        ["git", "-C", str(candidate), "rev-parse", "HEAD"], text=True).strip() == head
    assert subprocess.check_output(
        ["git", "-C", str(candidate), "rev-parse", "origin/main"], text=True).strip() == base
    config = (candidate / ".git/config").read_text()
    assert "sourceonly" not in config


@pytest.mark.parametrize("hosted", [False, True])
def test_snapshot_permissions_preserve_profiles_git_modes_and_external_paths(
    tmp_path, monkeypatch, hosted
):
    from scripts.prod_agent.native_ports import protected_path

    # Other local callers exercise real mode/link behavior with their own UID;
    # the separately granted Linux65532 run exercises the actual fixed identity.
    owner = 65532 if hosted and os.geteuid() in {0, 65532} else os.geteuid()
    if hosted and owner != 65532:
        fchown = os.fchown

        def portable_chown(fd, uid, gid):
            assert (uid, gid) == (65532, 65532)
            fchown(fd, os.geteuid(), os.getegid())

        monkeypatch.setattr(producer.os, "fchown", portable_chown)
    source = tmp_path / "source"
    source.mkdir()

    def git(repo, *args):
        return subprocess.check_output(
            ["git", "-c", "safe.directory=*", "-C", str(repo), *args], text=True
        ).strip()

    git(source, "init", "-q", "-b", "main")
    git(source, "config", "user.name", "Snapshot modes fixture")
    git(source, "config", "user.email", "snapshot-modes@example.invalid")
    (source / "code.py").write_text("print('source bytes')\n")
    (source / "tool.sh").write_text("#!/bin/sh\nexit 0\n")
    (source / "tool.sh").chmod(0o755)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("outside destination")
    sentinel.chmod(0o666)
    (source / "file-link").symlink_to(sentinel)
    (source / "directory-link").symlink_to(outside, target_is_directory=True)
    git(source, "add", ".")
    git(source, "commit", "-qm", "snapshot fixture")
    head = git(source, "rev-parse", "HEAD")
    ancestor = tmp_path / "unprotected-parent"
    ancestor.mkdir()
    ancestor.chmod(0o777)
    untouched = {p: p.stat() for p in (ancestor, outside, sentinel)}
    candidate = ancestor / "candidate"
    producer.snapshot(source, candidate, head, head, hosted=hosted)
    assert stat.S_IMODE(candidate.stat().st_mode) == (0o755 if hosted else 0o777)
    assert stat.S_IMODE((candidate / "code.py").stat().st_mode) == (0o644 if hosted else 0o666)
    assert stat.S_IMODE((candidate / "tool.sh").stat().st_mode) == (0o755 if hosted else 0o777)
    assert candidate.stat().st_uid == owner
    assert (candidate / "code.py").stat().st_uid == owner
    if hosted:
        gid = 65532 if os.geteuid() in {0, 65532} else os.getegid()
        assert candidate.stat().st_gid == (candidate / "code.py").stat().st_gid == gid
    assert git(candidate, "ls-tree", "-r", "HEAD") == git(source, "ls-tree", "-r", "HEAD")
    assert (candidate / "code.py").read_bytes() == (source / "code.py").read_bytes()
    assert (candidate / "file-link").is_symlink() and (candidate / "directory-link").is_symlink()
    for path, before in untouched.items():
        after = path.stat()
        assert (after.st_uid, after.st_gid, after.st_mode) == (
            before.st_uid, before.st_gid, before.st_mode
        )
    assert sentinel.read_text() == "outside destination"
    (candidate / ".dc-session.json").write_text("{}\n")
    (candidate / ".dc-junit.xml").write_text("<testsuites/>\n")
    if hosted:
        assert protected_path(candidate / "code.py", owner, anchor=candidate)
        with pytest.raises(ValueError, match="ownership/mode"):
            protected_path(candidate / "code.py", owner, anchor=tmp_path)
        with pytest.raises(ValueError, match="ownership/mode"):
            protected_path(candidate / "file-link", owner, anchor=candidate)


def test_hosted_snapshot_normalization_refuses_a_symlink_root(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("unchanged outside bytes")
    before = (outside.stat(), sentinel.stat())
    link = tmp_path / "candidate-link"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        producer._hosted_snapshot_permissions(link)
    for path, old in zip((outside, sentinel), before, strict=True):
        current = path.stat()
        assert (current.st_uid, current.st_gid, current.st_mode) == (
            old.st_uid, old.st_gid, old.st_mode
        )
    assert sentinel.read_text() == "unchanged outside bytes"


def test_pytest_capture_rejects_a_changed_evidence_plugin(monkeypatch, tmp_path):
    with pytest.raises(ValueError):
        _capture_commands(monkeypatch, tmp_path, kind="pytest",
                          argv=["python3", "-P", "-m", "pytest"],
                          plugin=b"# candidate-controlled plugin\n")


UPLOAD_ARTIFACT_ACTION = (
    "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02")
EVIDENCE_PLUGIN = ROOT / "scripts/ci/pytest_evidence.py"


def _run_native_pytest(tmp_path, *, include_error):
    """Real pytest child with the pinned evidence plugin; no Docker."""
    candidate = tmp_path / "candidate"
    (candidate / "tests").mkdir(parents=True)
    (candidate / "tests/test_ok.py").write_text("def test_ok():\n    assert True\n")
    if include_error:
        (candidate / "tests/test_error.py").write_text(
            "import pytest\n\n"
            "@pytest.fixture\n"
            "def broken():\n"
            "    raise RuntimeError('setup boom from fixture')\n\n"
            "def test_uses_broken(broken):\n"
            "    assert True\n")
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "dc_approved_evidence.py").write_bytes(EVIDENCE_PLUGIN.read_bytes())
    junit = candidate / producer.PYTEST_JUNIT_NAME
    session_path = candidate / ".dc-session.json"
    env = {**os.environ, "PYTHONPATH": str(plugin_dir),
           "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("PYTEST_ADDOPTS", None)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--basetemp=" + str(tmp_path / "pytest-tmp"),
         "-q", "--tb=no", "-rfE", "--color=no", "-p", "dc_approved_evidence",
         "--dc-evidence=" + str(session_path), "--junitxml=" + str(junit),
         "tests" if include_error else "tests/test_ok.py"],
        cwd=candidate, env=env, capture_output=True, text=True, timeout=120)
    raw = result.stdout.encode() + result.stderr.encode()
    session = json.loads(session_path.read_text())
    capture = {"version": 1, "kind": "pytest", "context": "test",
               "native_exit": result.returncode,
               "stdout_sha256": hashlib.sha256(raw).hexdigest(),
               "session_sha256": producer.digest(session), "exited": True,
               "removed": True, "oom_killed": False}
    return candidate, junit, raw, session, capture


def test_native_pytest_junit_keeps_traceback_and_copy_is_exact(tmp_path):
    candidate, junit, raw, session, capture = _run_native_pytest(tmp_path, include_error=True)
    xml = junit.read_bytes()
    assert b"<error" in xml
    assert b"tests.test_error" in xml and b'name="test_uses_broken"' in xml
    assert b"setup boom from fixture" in xml
    work = tmp_path / "work"
    work.mkdir()
    status = producer.retain_pytest_diagnostics(candidate, work, capture, session, raw)
    directory = work / producer.PYTEST_DIAGNOSTIC_DIR
    assert (directory / producer.DIAGNOSTIC_JUNIT_NAME).read_bytes() == xml
    assert not (directory / producer.PYTEST_JUNIT_NAME).exists()
    assert status["junit_name"] == producer.DIAGNOSTIC_JUNIT_NAME
    assert not producer.DIAGNOSTIC_JUNIT_NAME.startswith(".")
    assert (directory / "pytest-output-tail.txt").read_bytes() == raw[-producer.MAX_RAW_TAIL_BYTES:]
    session_bytes = (json.dumps(session, sort_keys=True) + "\n").encode()
    assert (directory / "session.json").read_bytes() == session_bytes
    assert (directory / "capture.json").read_bytes() == (
        json.dumps(capture, sort_keys=True) + "\n").encode()
    assert status["junit"] == "present" and status["junit_bytes"] == len(xml)
    assert status["junit_sha256"] == hashlib.sha256(xml).hexdigest()
    assert status["session_sha256"] == hashlib.sha256(session_bytes).hexdigest()
    # Raw/session gate semantics are unchanged: the H6 ERROR signature remains.
    with pytest.raises(producer.EvidenceValidationError) as caught:
        producer.make_test_envelope(raw, session, capture, {"version": 3, "phase": "H"})
    assert (caught.value.reason, caught.value.subreason) == ("raw-session-invalid", "errors")


def test_native_pytest_positive_control_envelope_is_unchanged(tmp_path):
    candidate, junit, raw, session, capture = _run_native_pytest(tmp_path, include_error=False)
    assert capture["native_exit"] == 0
    assert b"<testsuite" in junit.read_bytes() and b"test_ok" in junit.read_bytes()
    work = tmp_path / "work"
    work.mkdir()
    binding = {"version": 3, "phase": "H"}
    envelope = producer.make_test_envelope(raw, session, capture, binding)
    status = producer.retain_pytest_diagnostics(candidate, work, capture, session, raw)
    assert status["junit"] == "present"
    directory = work / producer.PYTEST_DIAGNOSTIC_DIR
    (directory / producer.DIAGNOSTIC_JUNIT_NAME).write_bytes(b"mutated diagnostic")
    (candidate / producer.PYTEST_JUNIT_NAME).write_bytes(b"mutated candidate diagnostic")
    assert producer.make_test_envelope(raw, session, capture, binding) == envelope
    assert not (work / "attestation.json").exists()


def test_diagnostic_retention_negative_controls(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    directory = work / producer.PYTEST_DIAGNOSTIC_DIR
    raw = b"candidate raw output with ::error::MARKER"
    capture = {"native_exit": 1}

    status = producer.retain_pytest_diagnostics(candidate, work, capture, None, raw)
    assert status["junit"] == "missing" and not (
        directory / producer.DIAGNOSTIC_JUNIT_NAME).exists()

    canary = tmp_path / "host-canary.txt"
    canary.write_text("HOST-CANARY-SECRET")
    (candidate / producer.PYTEST_JUNIT_NAME).symlink_to(canary)
    status = producer.retain_pytest_diagnostics(candidate, work, capture, None, raw)
    assert status["junit"] == "unsafe"
    assert not (directory / producer.DIAGNOSTIC_JUNIT_NAME).exists()
    assert all("HOST-CANARY-SECRET" not in path.read_text(errors="replace")
               for path in directory.iterdir() if path.is_file())
    (candidate / producer.PYTEST_JUNIT_NAME).unlink()

    outside = tmp_path / "outside.xml"
    outside.write_text("<?xml version='1.0'?><testsuites/>")
    assert producer._read_candidate_diagnostic(outside, candidate, 1024) == ("unsafe", None, 0)

    monkeypatch.setattr(producer, "MAX_JUNIT_BYTES", 16)
    (candidate / producer.PYTEST_JUNIT_NAME).write_bytes(b"<?xml " + b"x" * 20)
    assert producer.retain_pytest_diagnostics(
        candidate, work, capture, None, raw)["junit"] == "oversized"
    monkeypatch.setattr(producer, "MAX_JUNIT_BYTES", 8 * 1024 * 1024)
    (candidate / producer.PYTEST_JUNIT_NAME).write_bytes(b"not xml")
    assert producer.retain_pytest_diagnostics(
        candidate, work, capture, None, raw)["junit"] == "malformed"
    assert not (directory / producer.DIAGNOSTIC_JUNIT_NAME).exists()

    (candidate / "secret.txt").write_text("candidate-secret")
    (candidate / producer.PYTEST_JUNIT_NAME).write_bytes(
        b"<?xml version='1.0'?><testsuites/>")
    status = producer.retain_pytest_diagnostics(
        candidate, work, capture, {"a": 1}, raw)
    assert status["junit"] == "present"
    assert {path.name for path in directory.iterdir()} == {
        producer.DIAGNOSTIC_JUNIT_NAME, "pytest-output-tail.txt", "status.json",
        "session.json", "capture.json"}
    assert all("candidate-secret" not in path.read_text(errors="replace")
               for path in directory.iterdir() if path.is_file())


def test_retention_never_prints_candidate_text(tmp_path, capsys):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / producer.PYTEST_JUNIT_NAME).write_bytes(
        b"<?xml version='1.0'?><testsuites><error>candidate-secret</error></testsuites>")
    work = tmp_path / "work"
    work.mkdir()
    producer.retain_pytest_diagnostics(
        candidate, work, {"native_exit": 1}, None,
        b"::error::CANDIDATE-MARKER candidate-secret\n1 error in 1s\n")
    captured = capsys.readouterr()
    assert "candidate-secret" not in captured.out + captured.err
    assert "::error::" not in captured.out + captured.err


def test_retained_junit_name_is_visible_for_default_upload():
    # The candidate input stays hidden, but the retained artifact file must be
    # visible: upload-artifact v4 defaults include-hidden-files=false.
    assert producer.PYTEST_JUNIT_NAME.startswith(".")
    assert producer.DIAGNOSTIC_JUNIT_NAME == "junit.xml"
    assert not producer.DIAGNOSTIC_JUNIT_NAME.startswith(".")
    for retained in (producer.DIAGNOSTIC_JUNIT_NAME, "pytest-output-tail.txt",
                     "session.json", "capture.json", "status.json"):
        assert not retained.startswith(".")
    workflow = _workflow()
    uploads = [step for job in workflow["jobs"].values() for step in job["steps"]
               if str(step.get("uses", "")).startswith("actions/upload-artifact@")]
    assert len(uploads) == 2
    for step in uploads:
        assert "include-hidden-files" not in step["with"]
        assert step["with"]["path"] == "${{ env.CI_ROOT }}/native-diagnostics"


def test_failure_diagnostics_upload_is_narrow_pinned_and_failure_only():
    workflow = _workflow()
    uploads = {}
    for job_name, job in workflow["jobs"].items():
        steps = job["steps"]
        native = next(index for index, step in enumerate(steps)
                      if step.get("name") == "Execute approved isolated native producer")
        step = steps[native + 1]
        assert step.get("name") == "Upload native failure diagnostics"
        assert step["uses"] == UPLOAD_ARTIFACT_ACTION
        assert "failure()" in step["if"]
        assert "env.CI_NATIVE_PRODUCER == 'true'" in step["if"]
        assert step["with"]["path"] == "${{ env.CI_ROOT }}/native-diagnostics"
        assert step["with"]["retention-days"] == "2"
        assert step["with"]["if-no-files-found"] == "ignore"
        assert "github.run_id" in step["with"]["name"]
        assert "github.run_attempt" in step["with"]["name"]
        uploads[job_name] = step["with"]["name"]
    assert set(uploads) == {"test", "agent-framework-reports"}
    assert uploads["test"] != uploads["agent-framework-reports"]
    assert "test" in uploads["test"] and "reports" in uploads["agent-framework-reports"]
    assert sum(1 for job in workflow["jobs"].values() for step in job["steps"]
               if step.get("uses", "").startswith("actions/upload-artifact@")) == 2
    assert workflow["permissions"] == {"contents": "read", "pull-requests": "read"}
    assert set(workflow["jobs"]) == {"test", "agent-framework-reports"}
    assert workflow["on"]["push"]["branches"] == ["main"]


def test_job_pytest_command_keeps_full_selection_and_one_junit():
    source = inspect.getsource(producer.job)
    assert '"-q", "--tb=no", "-rfE", "--color=no"' in source
    assert '"-p", "dc_approved_evidence"' in source
    assert source.count("--junitxml=/candidate/") == 1
    for forbidden in ("-k ", "--deselect", "--ignore"):
        assert forbidden not in source
    assert source.index("clear_pytest_junit(candidate)") < source.index(
        "runner.capture(candidate, binding, \"pytest\"")
    assert source.index("retain_pytest_diagnostics(candidate") < source.index(
        '_stage("evidence")')

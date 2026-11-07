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
import subprocess
import sys
import traceback
import uuid
from pathlib import Path, PurePosixPath

AUTHORITY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(AUTHORITY))

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


def parse_manifest(raw: bytes, expected_blob: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", expected_blob or "") or blob_id(raw) != expected_blob:
        raise ValueError("unapproved producer manifest blob")
    value = json.loads(raw)
    if (set(value) != {"version", "runtime", "files"} or value["version"] != 1
            or set(value["runtime"]) != {"image", "platform", "scope", "controller"}
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", value["runtime"]["image"])
            or value["runtime"]["platform"] not in {"linux/arm64", "linux/amd64"}
            or value["runtime"]["scope"] != "local_fixture_only"
            or value["runtime"]["controller"] != "approved_workflow_platform_python_I"):
        raise ValueError("invalid approved producer runtime")
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
            or capture["image"] != manifest["runtime"]["image"]
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


class DockerCapture:
    """Only this trusted host process can reach the daemon or publish evidence.

    No socket, credentials, authority outputs or host PID namespace are mounted.
    A lost controller produces no marker. Timeout kills the owned container and
    is always a failure; uncertain exit/cleanup also forbids an attestation.
    """
    def __init__(self, docker: list[str], manifest: dict, authority: Path, output: Path):
        self.docker, self.manifest, self.authority, self.output = docker, manifest, authority, output

    def call(self, *args, timeout=30):
        result = subprocess.run([*self.docker, *args], capture_output=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError("owned Docker operation failed: " + args[0])
        return result.stdout

    def capture(self, candidate: Path, binding: dict, kind: str, context: str,
                argv: list[str], env: dict[str, str], *, trusted_scripts=False, readonly=False,
                inputs: Path | None = None):
        name = "dc-native-ci-b2-" + uuid.uuid4().hex
        runtime = self.manifest["runtime"]
        args = ["create", "--name", name, "--label", "org.duidui.scope=dc-native-ci-b2",
                "--pull=never", "--platform", runtime["platform"], "--network=none",
                "--user=65532:65532", "--read-only", "--cap-drop=ALL",
                "--security-opt=no-new-privileges", "--cpus=2", "--memory=2g", "--pids-limit=256",
                "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m,mode=1777", "--workdir=/candidate",
                "--mount", f"type=bind,src={candidate},dst=/candidate" + (",readonly" if readonly else ""),
                "--mount", f"type=bind,src={self.authority},dst=/authority,readonly"]
        if trusted_scripts:
            args += ["--mount", f"type=bind,src={self.authority / 'scripts'},dst=/candidate/scripts,readonly",
                     "--mount", f"type=bind,src={self.authority / 'tests/known_failures.txt'},dst=/candidate/tests/known_failures.txt,readonly"]
        if inputs is not None:
            args += ["--mount", f"type=bind,src={inputs},dst=/input,readonly"]
        clean_env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp",
                     "PYTHONPATH": "/authority", "PYTHONSAFEPATH": "1", "PYTHONNOUSERSITE": "1",
                     "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONDONTWRITEBYTECODE": "1",
                     "RUFF_CACHE_DIR": "/tmp/ruff",
                     "DAILYCHECK_DB_DIR": "/tmp/db", "TMPDIR": "/tmp",
                     "GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "safe.directory",
                     "GIT_CONFIG_VALUE_0": "*", "GIT_CONFIG_KEY_1": "core.hooksPath",
                     "GIT_CONFIG_VALUE_1": "/dev/null", **env}
        for key, value in clean_env.items():
            args += ["--env", key + "=" + value]
        args += [runtime["image"], *argv]
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
                native = int(self.call("wait", cid, timeout=180).decode().strip())
            except subprocess.TimeoutExpired:
                timed_out = True
                self.call("kill", cid)
                native = int(self.call("wait", cid).decode().strip())
            inspection = json.loads(self.call("inspect", cid))[0]
            (run_dir / "inspect.json").write_text(json.dumps(inspection, indent=2))
            state = inspection["State"]
            if state["Running"] or state["Status"] != "exited":
                raise ValueError("candidate exit unproven; preserve owned container")
            # Docker merges stderr on the client stderr stream. Obtain BOTH
            # streams verbatim; never print candidate Actions commands/markers.
            logs = subprocess.run([*self.docker, "logs", cid], capture_output=True, timeout=30)
            if logs.returncode:
                raise ValueError("candidate logs unavailable")
            raw = logs.stdout + logs.stderr
            (run_dir / "stdout.bin").write_bytes(raw)
            self.call("rm", cid)
            if (timed_out or native != state["ExitCode"] or state["OOMKilled"]
                    or state["Error"] or inspection["Image"] != runtime["image"]):
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

    text = raw.decode("utf-8")
    original = make_evidence(text, capture["native_exit"], session)
    if unexpected_failures(session["failed_nodeids"], load_known_ids()):
        raise ValueError("actual native pytest has new failures")
    compact = extract_pytest_summary(text)
    evidence = make_evidence(compact, capture["native_exit"], session)
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

    manifest_blob = os.environ["CI_PRODUCER_MANIFEST_BLOB"]
    raw = (AUTHORITY / "scripts/ci/producer_manifest.json").read_bytes()
    manifest = parse_manifest(raw, manifest_blob)
    # Verify executed bytes before any candidate process exists. This is a
    # trusted export check, not a candidate helper approving its own code.
    for name, expected in manifest["files"].items():
        if blob_id((AUTHORITY / name).read_bytes()) != expected:
            raise ValueError("approved authority export changed")
    for package in ("scripts", "scripts/ci", "scripts/agent_framework"):
        if (AUTHORITY / package / "__init__.py").read_bytes() != b"":
            raise ValueError("approved namespace export changed")
    work = Path(os.environ["CI_ROOT"]).resolve()
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
    candidate = work / "candidate"
    snapshot(repo, candidate, binding["tested_sha"])
    (candidate / ".dc-session.json").unlink(missing_ok=True)
    docker = [args.docker]
    if args.docker_config:
        docker += ["--config", args.docker_config]
    if args.docker_host:
        docker += ["-H", args.docker_host]
    runner = DockerCapture(docker, manifest, AUTHORITY, work)
    if args.context == "test":
        base = binding["CI_BASE_SHA"]
        changed = subprocess.check_output(["git", "diff", "--name-only", "--diff-filter=d", "-z",
                                           base + "..." + binding["tested_sha"], "--", "*.py"], cwd=repo).decode().split("\0")
        files = [p for p in changed if p]
        lint, _, _ = runner.capture(candidate, binding, "lint", "test",
                                    ["ruff", "check", "--", *files] if files else ["ruff", "--version"], {}, readonly=True)
        if lint["native_exit"] != 0:
            raise ValueError("actual changed-file Ruff gate failed")
        capture, output, session = runner.capture(candidate, binding, "pytest", "test",
            ["python3", "-P", "-m", "pytest", "--basetemp=/tmp/pytest", "-q", "--tb=no", "-rfE", "--color=no",
             "-p", "dc_approved_evidence", "--dc-evidence=/candidate/.dc-session.json"], {})
        envelope = make_test_envelope(output, session, capture, binding)
        producer = {"identity": identity(manifest, manifest_blob), "lint_capture": lint,
                    "test_capture": capture, "gates": []}
    else:
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


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, RuntimeError):
        print("trusted native producer failed; no success attestation", file=sys.stderr)
        raise SystemExit(1) from None

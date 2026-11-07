#!/usr/bin/env python3
"""Extract a compact pytest summary for a GitHub Actions job output.

The CLI requires validated, hash-bound native/session evidence. It carries that
evidence with compact text in a base64 JSON envelope; decode revalidates both.
The pure extraction/encoding helpers remain diagnostic utilities, not gates.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

# Direct script execution starts sys.path at scripts/ci, not the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.check_known_failures import make_evidence, validate_evidence  # noqa: E402
from scripts.ci.candidate_binding import parse_binding  # noqa: E402

OUTPUT_NAME = "pytest_summary_b64"
SUMMARY_LINE_COUNT = 5
# Keep the encoded handoff well below common job-output limits. Required lines
# are never truncated: an oversized summary fails the producing step instead.
MAX_SUMMARY_BYTES = 48 * 1024


def _is_collection_marker(stripped: str) -> bool:
    lowered = stripped.lower()
    return (
        "interrupted:" in lowered
        or "error during collection" in lowered
        or "errors during collection" in lowered
        or "keyboardinterrupt" in lowered
        or "internalerror>" in lowered
        or "stopping after" in lowered
    )


def extract_pytest_summary(text: str) -> str:
    """Return diagnostic compact text; this alone is not completion evidence."""
    lines = text.splitlines()
    final_start = max(0, len(lines) - SUMMARY_LINE_COUNT)
    selected: list[str] = []

    for index, line in enumerate(lines):
        stripped = line.strip()
        is_status = stripped.startswith(("FAILED ", "ERROR "))
        if is_status or _is_collection_marker(stripped) or index >= final_start:
            selected.append(line)

    summary = "\n".join(selected)
    if summary:
        summary += "\n"
    size = len(summary.encode("utf-8"))
    if size > MAX_SUMMARY_BYTES:
        raise ValueError(
            f"compact pytest summary too large: {size} bytes "
            f"(limit {MAX_SUMMARY_BYTES}); refusing to truncate required lines"
        )
    return summary


def encode_summary(summary: str) -> str:
    """Encode a summary as an ASCII-only single-line job-output value."""
    return base64.b64encode(summary.encode("utf-8")).decode("ascii")


def decode_result(envelope: dict, expected_binding: dict | None = None) -> tuple[str, dict]:
    version = 2 if expected_binding is not None else 1
    keys = {"schema_version", "text", "evidence"} | ({"binding"} if version == 2 else set())
    if not isinstance(envelope, dict) or type(envelope.get("schema_version")) is not int or envelope["schema_version"] != version or set(envelope) != keys:
        raise ValueError("missing/invalid compact pytest envelope")
    if expected_binding is not None and envelope.get("binding") != expected_binding:
        raise ValueError("CI candidate/run binding mismatch")
    text = envelope["text"]
    if not isinstance(text, str):
        raise ValueError("invalid compact pytest text")
    validate_evidence(text, envelope["evidence"])
    return text, envelope["evidence"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, nargs="?", help="raw pytest output file")
    parser.add_argument("--evidence-file", type=Path, help="validated raw evidence from strict gate")
    parser.add_argument("--output-file", type=Path, help="job output file; defaults to $GITHUB_OUTPUT")
    parser.add_argument("--decode", type=Path, help="decode/revalidate a JSON envelope after base64 decoding")
    parser.add_argument("--text-output", type=Path)
    parser.add_argument("--evidence-output", type=Path)
    parser.add_argument("--binding-file", type=Path, help="expected tested H/base B/run identity")
    args = parser.parse_args(argv)
    try:
        binding = json.loads(args.binding_file.read_text(encoding="utf-8")) if args.binding_file else None
        if args.binding_file is not None:
            binding = parse_binding(binding)
        if args.decode:
            if not args.text_output or not args.evidence_output or args.input:
                print("[ci-pytest-summary] --decode requires --text-output and --evidence-output, without input", file=sys.stderr)
                return 2
            text, evidence = decode_result(json.loads(args.decode.read_text(encoding="utf-8")), binding)
            args.text_output.write_text(text, encoding="utf-8")
            args.evidence_output.write_text(json.dumps(evidence, sort_keys=True), encoding="utf-8")
            print("[ci-pytest-summary] decoded and validated native/session evidence")
            return 0
        if args.input is None or args.evidence_file is None:
            print("[ci-pytest-summary] input and --evidence-file required; fragments cannot be exported", file=sys.stderr)
            return 2
        output_file = args.output_file
        if output_file is None:
            github_output = os.environ.get("GITHUB_OUTPUT", "")
            if not github_output:
                print("[ci-pytest-summary] GITHUB_OUTPUT is not set", file=sys.stderr)
                return 2
            output_file = Path(github_output)
        text = args.input.read_text(encoding="utf-8")
        original = json.loads(args.evidence_file.read_text(encoding="utf-8"))
        validate_evidence(text, original)
        compact = extract_pytest_summary(text)
        evidence = make_evidence(compact, original["native_exit"], original["session"])
        evidence["raw_output_sha256"] = original.get("raw_output_sha256", original["output_sha256"])
        envelope = {"schema_version": 1, "text": compact, "evidence": evidence}
        if binding is not None:
            envelope.update(schema_version=2, binding=binding)
        payload = json.dumps(envelope, sort_keys=True)
        if len(payload.encode("utf-8")) > MAX_SUMMARY_BYTES:
            raise ValueError("compact pytest envelope too large; refusing to truncate evidence")
        encoded = encode_summary(payload)
        with output_file.open("a", encoding="utf-8") as handle:
            handle.write(f"{OUTPUT_NAME}={encoded}\n")
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError) as exc:
        print(f"[ci-pytest-summary] failed: {exc}", file=sys.stderr)
        return 1
    print(f"[ci-pytest-summary] wrote {OUTPUT_NAME}: {len(payload.encode('utf-8'))} bytes including native/session evidence")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Extract a compact pytest summary for a GitHub Actions job output.

Only pytest failure/error status lines, collection interruptions, and the last
five lines are retained. The result is base64 encoded because job outputs are
single-line values.
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
from pathlib import Path

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
    )


def extract_pytest_summary(text: str) -> str:
    """Return deterministic, compact pytest output suitable for job handoff."""
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="raw pytest output file")
    parser.add_argument(
        "--output-file",
        type=Path,
        help="GitHub/Gitea output file (defaults to $GITHUB_OUTPUT)",
    )
    args = parser.parse_args(argv)

    if not args.input.is_file():
        print(f"[ci-pytest-summary] input not found: {args.input}", file=sys.stderr)
        return 2
    output_file = args.output_file
    if output_file is None:
        github_output = os.environ.get("GITHUB_OUTPUT", "")
        if not github_output:
            print("[ci-pytest-summary] GITHUB_OUTPUT is not set", file=sys.stderr)
            return 2
        output_file = Path(github_output)

    try:
        summary = extract_pytest_summary(args.input.read_text(encoding="utf-8", errors="replace"))
        encoded = encode_summary(summary)
    except (OSError, ValueError) as exc:
        print(f"[ci-pytest-summary] failed: {exc}", file=sys.stderr)
        return 1

    try:
        with output_file.open("a", encoding="utf-8") as handle:
            handle.write(f"{OUTPUT_NAME}={encoded}\n")
    except OSError as exc:
        print(f"[ci-pytest-summary] cannot write {output_file}: {exc}", file=sys.stderr)
        return 2

    print(
        f"[ci-pytest-summary] wrote {OUTPUT_NAME}: "
        f"{len(summary.encode('utf-8'))} bytes, {len(summary.splitlines())} lines"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

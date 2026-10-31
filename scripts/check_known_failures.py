#!/usr/bin/env python3
"""Strict complete-pytest / zero-new-failure gate.

Fresh run: output --pytest-exit-file native.exit --session-file session.json
Reuse: output --evidence-file evidence.json
Enable the observing plugin with -p scripts.ci.pytest_evidence --dc-evidence=PATH.
Exit 0 means complete native0/1 with no new failures; 1 rejects evidence/results;
2 means invalid CLI usage. Diagnostic extraction utilities do not prove completion.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
KNOWN_FILE = REPO_ROOT / "tests" / "known_failures.txt"
OUTCOMES = ("passed", "failed", "skipped", "xfailed", "xpassed", "error")
FAILED_RE = re.compile(r"^FAILED\s+(\S+)")
COUNT_RE = re.compile(r"(\d+) (passed|failed|skipped|xfailed|xpassed|errors?|deselected|warnings?)")
FOOTER_RE = re.compile(r"^(?P<counts>\d+ [a-z]+(?:, \d+ [a-z]+)*) in \d+(?:\.\d+)?s(?: \([\d:]+\))?$")
ABNORMAL_RE = re.compile(r"^(?:INTERNALERROR>|!+.*(?:KeyboardInterrupt|Interrupted:|errors? during collection|stopping after \d+ failures?)|KeyboardInterrupt(?::.*)?$)", re.I | re.M)


def extract_failed_ids(output_path: str) -> set:
    """Legacy diagnostic API, including parameterized base aliases (not counts)."""
    failed = set()
    for line in Path(output_path).read_text(errors="replace").splitlines():
        match = FAILED_RE.match(line.strip())
        if match:
            nodeid = match[1]
            failed.add(nodeid)
            if "[" in nodeid:
                failed.add(nodeid.split("[", 1)[0])
    return failed


def load_known_ids() -> set:
    if not KNOWN_FILE.exists():
        return set()
    return {line.strip() for line in KNOWN_FILE.read_text().splitlines() if line.strip() and not line.lstrip().startswith("#")}


def unexpected_failures(nodeids: list[str], known: set[str]) -> list[str]:
    """Preserve legacy actual+base authorization; report/count actual tests only."""
    return [node for node in nodeids if node not in known or node.split("[", 1)[0] not in known]


def _integer(value, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"invalid {name}")
    return value


def footer_counts(text: str) -> dict[str, int]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or ABNORMAL_RE.search(text):
        raise ValueError("pytest output is empty/interrupted/internal/collection/incomplete")
    summaries = []
    for index, line in enumerate(lines):
        match = FOOTER_RE.fullmatch(line.strip("= "))
        if match:
            summaries.append((index, match["counts"]))
    if len(summaries) != 1 or summaries[0][0] != len(lines) - 1:
        raise ValueError("missing/truncated/ambiguous terminal pytest summary (原始输出 required)")
    counts = {}
    fields = summaries[0][1].split(", ")
    for field in fields:
        match = COUNT_RE.fullmatch(field)
        if not match:
            raise ValueError(f"unsupported pytest summary outcome: {field}")
        name = {"errors": "error", "warnings": "warning"}.get(match[2], match[2])
        if name in counts:
            raise ValueError(f"duplicate pytest summary outcome: {name}")
        counts[name] = int(match[1])
    return counts


def validate_completion(text: str, native_exit: int, session: dict) -> dict[str, int]:
    """Bind native completion, independent pytest reports, and raw terminal output."""
    if type(native_exit) is not int or native_exit not in (0, 1):
        raise ValueError(f"pytest native exit must be 0/1, got {native_exit!r}")
    if not isinstance(session, dict) or type(session.get("schema_version")) is not int or session["schema_version"] != 1:
        raise ValueError("missing/invalid pytest session evidence")
    if type(session.get("pytest_exit")) is not int or session["pytest_exit"] != native_exit:
        raise ValueError("native exit disagrees with pytest session exit")
    selected = _integer(session.get("selected"), "selected", minimum=1)
    if _integer(session.get("selected_unique"), "selected_unique") != selected:
        raise ValueError("duplicate/inconsistent collected nodeids")
    if _integer(session.get("completed"), "completed") != selected or session.get("uncompleted") != [] or session.get("unexpected") != []:
        raise ValueError("pytest did not complete every selected test")
    counts = session.get("counts")
    if not isinstance(counts, dict) or set(counts) != set(OUTCOMES):
        raise ValueError("missing/incomplete outcome counts")
    for name in OUTCOMES:
        _integer(counts[name], name)
    if counts["error"] or session.get("error_nodeids") != []:
        raise ValueError("pytest collection/setup/teardown errors cannot be known failures")
    if sum(counts.values()) != selected:
        raise ValueError("outcome enumeration does not match selected/completed count")
    if (native_exit == 0 and counts["failed"]) or (native_exit == 1 and not counts["failed"]):
        raise ValueError("native exit disagrees with test outcomes")
    summary = footer_counts(text)
    if any(summary.get(name, 0) != counts[name] for name in OUTCOMES):
        raise ValueError("terminal summary counts disagree with pytest report enumeration; 原始输出/evidence required")
    failed = session.get("failed_nodeids")
    if not isinstance(failed, list) or any(not isinstance(n, str) or not n for n in failed) or len(set(failed)) != len(failed) or len(failed) != counts["failed"]:
        raise ValueError("invalid failed-node enumeration")
    found = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("ERROR "):
            raise ValueError("raw output contains pytest ERROR")
        if not stripped.startswith("FAILED "):
            continue
        remainder = stripped.removeprefix("FAILED ")
        matches = [node for node in failed if remainder == node or remainder.startswith(node + " - ")]
        if len(matches) != 1:
            raise ValueError("unrecognized FAILED line; gate requires pytest 原始输出")
        found.append(matches[0])
    if len(found) != len(failed) or set(found) != set(failed):
        raise ValueError("FAILED enumeration missing/duplicated/inconsistent with terminal counts")
    return counts


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_evidence(text: str, native_exit: int, session: dict) -> dict:
    validate_completion(text, native_exit, session)
    return {"schema_version": 1, "native_exit": native_exit, "session": session, "output_sha256": digest(text)}


def validate_evidence(text: str, evidence: dict) -> dict[str, int]:
    if not isinstance(evidence, dict) or type(evidence.get("schema_version")) is not int or evidence["schema_version"] != 1 or evidence.get("output_sha256") != digest(text):
        raise ValueError("missing/invalid/stale pytest output evidence hash")
    return validate_completion(text, evidence.get("native_exit"), evidence.get("session"))


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    provenance = parser.add_mutually_exclusive_group(required=True)
    provenance.add_argument("--pytest-exit-file", type=Path)
    provenance.add_argument("--pytest-exit", type=int)
    provenance.add_argument("--evidence-file", type=Path)
    parser.add_argument("--session-file", type=Path)
    parser.add_argument("--write-evidence", type=Path)
    args = parser.parse_args(argv)
    try:
        text = args.output.read_text(encoding="utf-8")
        if args.evidence_file:
            evidence = _read_json(args.evidence_file)
            counts = validate_evidence(text, evidence)
        else:
            if args.session_file is None:
                raise ValueError("fresh native exit requires --session-file")
            native_exit = args.pytest_exit
            if args.pytest_exit_file:
                raw = args.pytest_exit_file.read_text(encoding="utf-8").strip()
                if not re.fullmatch(r"-?\d+", raw):
                    raise ValueError("invalid native pytest exit file")
                native_exit = int(raw)
            evidence = make_evidence(text, native_exit, _read_json(args.session_file))
            counts = evidence["session"]["counts"]
        if args.write_evidence:
            args.write_evidence.write_text(json.dumps(evidence, sort_keys=True), encoding="utf-8")
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError) as exc:
        print(f"FAIL: incomplete/invalid pytest evidence: {exc}")
        return 1
    known = load_known_ids()
    failed = evidence["session"]["failed_nodeids"]
    new = unexpected_failures(failed, known)
    if new:
        print(f"FAIL: 发现 {len(new)} 个新增失败（不在 tests/known_failures.txt）：")
        for node in sorted(new):
            print(f"  - {node}")
        return 1
    print(f"OK: complete pytest native={evidence['native_exit']}; 当前失败 {counts['failed']} 个，全部在已知清单内（清单 {len(known)} 条，passed {counts['passed']}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

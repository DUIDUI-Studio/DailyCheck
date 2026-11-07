"""Bind compact CI evidence to the tested PR head H, event SHA and base B."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

FIELDS = (
    "CI_REPOSITORY",
    "CI_RUN_ID",
    "CI_RUN_ATTEMPT",
    "CI_EVENT",
    "CI_HEAD_SHA",
    "CI_EVENT_SHA",
    "CI_BASE_SHA",
    "tested_sha",
)


def parse_binding(value: object) -> dict[str, str]:
    if (
        not isinstance(value, dict)
        or set(value) != set(FIELDS)
        or any(not isinstance(v, str) for v in value.values())
    ):
        raise ValueError("invalid CI identity")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value["CI_REPOSITORY"]):
        raise ValueError("invalid CI repository")
    if not all(re.fullmatch(r"[1-9][0-9]*", value[key]) for key in ("CI_RUN_ID", "CI_RUN_ATTEMPT")):
        raise ValueError("invalid CI attempt")
    if value["CI_EVENT"] not in {"pull_request", "push", "workflow_dispatch"}:
        raise ValueError("unsupported CI event")
    for key in ("CI_HEAD_SHA", "CI_EVENT_SHA", "tested_sha"):
        if not re.fullmatch(r"[0-9a-f]{40}", value[key]):
            raise ValueError("invalid CI SHA")
    if value["CI_BASE_SHA"] or value["CI_EVENT"] == "pull_request":
        if not re.fullmatch(r"[0-9a-f]{40}", value["CI_BASE_SHA"]):
            raise ValueError("invalid PR base")
    if value["tested_sha"] != value["CI_HEAD_SHA"]:
        raise ValueError("checkout did not test candidate H")
    return value


def binding() -> dict[str, str]:
    result = {key: os.environ.get(key, "") for key in FIELDS if key != "tested_sha"}
    result["tested_sha"] = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    parsed = parse_binding(result)
    if parsed["CI_BASE_SHA"]:
        subprocess.run(
            ["git", "cat-file", "-e", parsed["CI_BASE_SHA"] + "^{commit}"],
            check=True,
            capture_output=True,
        )
    return parsed


if __name__ == "__main__":
    try:
        Path(sys.argv[1]).write_text(json.dumps(binding(), sort_keys=True), encoding="utf-8")
    except (OSError, ValueError, subprocess.SubprocessError):
        print("candidate binding failed", file=sys.stderr)
        raise SystemExit(1) from None

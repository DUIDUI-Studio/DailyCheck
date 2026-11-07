"""Bind compact CI evidence to the tested PR head H, event SHA and base B."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

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


def parse_binding(value: object) -> dict:
    if isinstance(value, dict) and value.get("version") == 3:
        keys = set(FIELDS) | {"version", "phase", "CI_GROUP_SHA", "member_manifest_sha256"}
        if set(value) != keys or type(value["version"]) is not int:
            raise ValueError("invalid tagged CI identity")
        phase = value["phase"]
        if (not isinstance(phase, str) or phase not in {"H", "G"}
                or value["CI_REPOSITORY"] != "DUIDUI-Studio/DailyCheck"
                or value["CI_EVENT"] != {
            "H": "pull_request", "G": "merge_group"
        }[phase]):
            raise ValueError("invalid CI producer domain")
        if phase == "H":
            if value["CI_GROUP_SHA"] is not None or value["member_manifest_sha256"] is not None:
                raise ValueError("head binding contains group identity")
            head = value["CI_HEAD_SHA"]
        else:
            if (value["CI_HEAD_SHA"] is not None
                    or not isinstance(value["member_manifest_sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", value["member_manifest_sha256"])):
                raise ValueError("missing group manifest")
            head = value["CI_GROUP_SHA"]
            if value["CI_EVENT_SHA"] != head:
                raise ValueError("group event differs from tested commit")
        legacy = {key: value[key] for key in FIELDS}
        legacy.update(CI_HEAD_SHA=head, CI_EVENT="pull_request")
        parse_binding(legacy)
        return value
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


def binding() -> dict:
    result = {key: os.environ.get(key, "") for key in FIELDS if key != "tested_sha"}
    result["tested_sha"] = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if os.environ.get("GITHUB_SERVER_URL") == "https://github.com" and result["CI_EVENT"] in {
        "pull_request", "merge_group"
    }:
        group = result["CI_EVENT"] == "merge_group"
        result.update(version=3, phase="G" if group else "H",
                      CI_GROUP_SHA=os.environ.get("CI_GROUP_SHA") if group else None,
                      member_manifest_sha256=None)
        if group:
            from scripts.ci.merge_group import digest, validate_manifest

            manifest = json.loads(Path(os.environ["CI_MEMBER_MANIFEST"]).read_text())
            validate_manifest(Path.cwd(), manifest)
            result.update(CI_HEAD_SHA=None, member_manifest_sha256=digest(manifest))
            if (manifest["group_sha"], manifest["creation_base_sha"]) != (
                    result["CI_GROUP_SHA"], result["CI_BASE_SHA"]):
                raise ValueError("event/manifest mismatch")
        else:
            parents = subprocess.check_output(
                ["git", "show", "-s", "--format=%P", result["tested_sha"]], text=True).split()
            if len(parents) != 1:
                raise ValueError("native H must have exactly one parent")
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

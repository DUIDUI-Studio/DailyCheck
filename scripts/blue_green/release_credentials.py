"""Render an inert, exact operator admission into a private finite release bundle.

This is preparation tooling, never a credential broker or privileged installer.
A privileged operator must review the supplied hashes and install the bundle
outside candidate custody before executing entry.py --execute-admitted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

PINS = frozenset(
    {
        "scripts/blue_green/deploy_pipeline.sh",
        "scripts/blue_green/deploy_state.py",
        "scripts/blue_green/deploy_merge.py",
        "scripts/blue_green/deploy_report.py",
        "scripts/blue_green/publication_authority.py",
        "scripts/blue_green/deploy_triage_pure.py",
        "scripts/hosting.py",
        "scripts/prod_agent/github_readiness.py",
        "scripts/prod_agent/github_intake.py",
        "scripts/blue_green/release_finish.py",
        "scripts/blue_green/release_issues.py",
    }
)
FIELDS = frozenset(
    {
        "uid",
        "python",
        "root",
        "repo",
        "engine",
        "state",
        "tmp",
        "log",
        "shim",
        "shim_sha",
        "pr",
        "head",
        "base",
        "generation",
        "deployed_generation",
        "session",
        "task",
        "dispatch",
        "pins",
        "binding_sha256",
        "capability",
        "issue_protocol",
        "python_target",
        "python_sha256",
        "intake_policy",
        "intake_key",
        "reducer_db",
    }
)


def validate(profile: dict, destination: Path) -> None:
    if not isinstance(profile, dict) or set(profile) != FIELDS:
        raise ValueError("exact-admission-required")
    for key in ("pr", "generation", "deployed_generation", "uid"):
        if type(profile[key]) is not int or profile[key] < (0 if key == "uid" else 1):
            raise ValueError("invalid-admission-number")
    if profile["deployed_generation"] > profile["generation"]:
        raise ValueError("invalid-admission-generation")
    for key in ("head", "base", "shim_sha", "binding_sha256", "python_sha256"):
        if key == "binding_sha256" and profile[key] is None:
            continue  # An inert package is useful for review, never executable admission.
        size = 40 if key in ("head", "base") else 64
        if (
            not isinstance(profile[key], str)
            or re.fullmatch(rf"[0-9a-f]{{{size}}}", profile[key]) is None
        ):
            raise ValueError("invalid-admission-digest")
    if profile["head"] == profile["base"]:
        raise ValueError("identical-target")
    for key in (
        "root",
        "repo",
        "engine",
        "state",
        "tmp",
        "log",
        "shim",
        "python",
        "python_target",
        "intake_policy",
        "intake_key",
        "reducer_db",
    ):
        value = profile[key]
        if (
            not isinstance(value, str)
            or not value.startswith("/")
            or str(Path(value)) != value
            or any(c.isspace() for c in value)
            or ".." in Path(value).parts
        ):
            raise ValueError("invalid-admission-path")
    if Path(profile["root"]) != destination.absolute():
        raise ValueError("destination-mismatch")
    root, repo = Path(profile["root"]), Path(profile["repo"])
    if root == repo or root in repo.parents or repo in root.parents:
        raise ValueError("candidate-cannot-own-admission")
    engine = Path(profile["engine"])
    if engine == repo or engine in repo.parents or repo in engine.parents:
        raise ValueError("candidate-cannot-own-engine")
    for key in ("capability", "issue_protocol"):
        if profile[key] is not None and not isinstance(profile[key], dict):
            raise ValueError("invalid-release-protocol")
    if profile["issue_protocol"] is not None:
        from scripts.blue_green.publication_authority import SourceBinding
        from scripts.blue_green.release_issues import validate_protocol

        protocol = profile["issue_protocol"]
        if not isinstance(protocol.get("targets"), list) or len(protocol["targets"]) != 2:
            raise ValueError("issue-targets-missing")
        for row, role in zip(protocol["targets"], ("code", "ops"), strict=True):
            if any(
                row.get(key) != str(root / "credentials" / (role + suffix))
                for key, suffix in (("token_file", ".token"), ("grant_file", ".grant.json"))
            ):
                raise ValueError("issue-credential-path")
        source = (
            "github",
            "github.com",
            1449125557,
            "DUIDUI-Studio/DailyCheck-ops",
            "issue",
            protocol["targets"][1].get("number"),
        )
        validate_protocol(protocol, SourceBinding(profile["pr"], source, ""))
    if profile["tmp"] != str(root / "scratch/tmp"):
        raise ValueError("private-scratch-required")
    for key in ("session", "task", "dispatch"):
        if (
            not isinstance(profile[key], str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,159}", profile[key]) is None
        ):
            raise ValueError("invalid-admission-identity")
    if (
        not isinstance(profile["pins"], dict)
        or set(profile["pins"]) != PINS
        or any(
            not isinstance(v, str) or re.fullmatch(r"[0-9a-f]{64}", v) is None
            for v in profile["pins"].values()
        )
    ):
        raise ValueError("exact-reviewed-closure-required")


def render(profile: dict, destination: Path, *, preview: bool = False) -> None:
    """Create only; existing/consumed bundles cannot be overwritten or reset."""
    destination = Path(destination).absolute()
    validate(profile, Path(profile["root"]) if preview else destination)
    source = Path(__file__).parent
    refresh_config = {
        key: profile[key]
        for key in ("uid", "shim", "shim_sha", "issue_protocol", "engine", "pins", "root", "python")
    }
    refresh = (
        (source / "release_refresh.py")
        .read_text()
        .replace(
            "CONFIG = None  # BUILD_CONFIG",
            "CONFIG = " + repr(refresh_config) + "  # BUILD_CONFIG",
            1,
        )
    )
    profile = {**profile, "refresh_sha": hashlib.sha256(refresh.encode()).hexdigest()}
    selector = (source / "release_selector.py").read_text()
    selector = selector.replace("#!/usr/bin/env python3", "#!" + profile["python"] + " -B", 1)
    selector = selector.replace(
        "PROFILE = None  # BUILD_PROFILE", "PROFILE = " + repr(profile) + "  # BUILD_PROFILE", 1
    )
    entry = (
        (source / "release_entry.py")
        .read_text()
        .replace(
            "SELECTOR_SHA = None  # BUILD_SELECTOR_SHA",
            "SELECTOR_SHA = "
            + repr(hashlib.sha256(selector.encode()).hexdigest())
            + "  # BUILD_SELECTOR_SHA",
            1,
        )
    )
    # mkdir is exclusive. Interrupted preparation is deliberately not auto-repaired.
    destination.mkdir(mode=0o700)
    parent = (
        (source / "release_parent.py")
        .read_text()
        .replace(
            "PROFILE = None  # BUILD_PROFILE", "PROFILE = " + repr(profile) + "  # BUILD_PROFILE", 1
        )
        .replace(
            "ENTRY_SHA = None", "ENTRY_SHA = " + repr(hashlib.sha256(entry.encode()).hexdigest())
        )
    )
    parent = parent.replace(
        "SELECTOR_SHA = None",
        "SELECTOR_SHA = " + repr(hashlib.sha256(selector.encode()).hexdigest()),
    )
    review = (
        (source / "release_review.py")
        .read_text()
        .replace(
            "ENTRY_SHA = None  # BUILD_ENTRY_SHA",
            "ENTRY_SHA = "
            + repr(hashlib.sha256(entry.encode()).hexdigest())
            + "  # BUILD_ENTRY_SHA",
            1,
        )
    )
    for name, text in (
        ("selector.py", selector),
        ("entry.py", entry),
        ("parent.py", parent),
        ("refresh.py", refresh),
        ("review.py", review),
    ):
        path = destination / name
        with path.open("x") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        path.chmod(0o700)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admission", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    render(json.loads(args.admission.read_bytes()), args.destination)


if __name__ == "__main__":
    main()

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
from pathlib import Path

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
    }
)
FIELDS = frozenset(
    {
        "uid",
        "python",
        "root",
        "repo",
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
    for key in ("head", "base", "shim_sha", "binding_sha256"):
        size = 40 if key in ("head", "base") else 64
        if (
            not isinstance(profile[key], str)
            or re.fullmatch(rf"[0-9a-f]{{{size}}}", profile[key]) is None
        ):
            raise ValueError("invalid-admission-digest")
    if profile["head"] == profile["base"]:
        raise ValueError("identical-target")
    for key in ("root", "repo", "state", "tmp", "log", "shim", "python"):
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


def render(profile: dict, destination: Path) -> None:
    """Create only; existing/consumed bundles cannot be overwritten or reset."""
    destination = Path(destination).absolute()
    validate(profile, destination)
    source = Path(__file__).parent
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
    for name, text in (("selector.py", selector), ("entry.py", entry)):
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

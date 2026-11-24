"""Run the separately approved exact relay test set in a disposable offline Linux runner."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path

from scripts.prod_agent.relay_git import check_objects, git
from scripts.prod_agent.relay_protocol import hex_id, require

FILES = ("tests/test_relay_integration.py", "tests/test_relay_live_route.py",
         "tests/test_relay_protected.py")


def offline():
    # Docker's disconnected network namespace also has down kernel tunnel devices.
    # Require loopback to be the only interface marked up.
    return {p.name for p in Path("/sys/class/net").iterdir()
            if (p / "flags").is_file() and int((p / "flags").read_text(), 16) & 1} == {"lo"}


class ExactReports:
    def __init__(self):
        self.selected = []
        self.reports = []

    def pytest_collection_finish(self, session):
        self.selected = [item.nodeid for item in session.items]

    def pytest_runtest_logreport(self, report):
        self.reports.append((report.nodeid, report.when, report.outcome, hasattr(report, "wasxfail")))

    def complete(self, expected):
        return (Counter(self.selected) == Counter(expected)
                and Counter(self.reports) == Counter(
                    (node, stage, "passed", False) for node in expected
                    for stage in ("setup", "call", "teardown")))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("head", "base", "tree", "manifest-sha256"):
        parser.add_argument("--" + key, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    require(sys.platform == "linux" and os.geteuid() == 0 and offline(),
            "disposable Linux root and loopback-only network required")
    require(all(hex_id(value, 40) for value in (args.head, args.base, args.tree)),
            "exact H/B/tree required")
    repo = Path.cwd()
    check_objects(repo, {"head": args.head, "base": args.base, "tree": args.tree})
    for ref, sha in (("HEAD", args.head), ("refs/remotes/origin/main", args.base)):
        require(git(repo, "rev-parse", ref).strip() == sha.encode(), "checkout H/B differs")
    git(repo, "diff", "--no-ext-diff", "--quiet", args.head, "--")
    require(not git(repo, "ls-files", "--others"), "checkout contains untracked inputs")
    raw = args.manifest.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == args.manifest_sha256, "expected IDs pin differs")
    nodes = raw.decode().splitlines()
    require(nodes and len(nodes) == len(set(nodes))
            and all(node.partition("::")[0] in FILES and "::" in node for node in nodes),
            "invalid expected node set")
    os.environ["DAILYCHECK_RELAY_SSH_INTEGRATION"] = "1"
    import pytest

    observed = ExactReports()
    rc = pytest.main([*FILES, "-q", "-ra", "-p", "no:cacheprovider"], plugins=[observed])
    complete = rc == 0 and observed.complete(nodes)
    print(json.dumps({"head": args.head, "base": args.base, "tree": args.tree,
                      "manifest_sha256": args.manifest_sha256, "expected": len(nodes),
                      "selected": observed.selected, "reports": observed.reports,
                      "pytest_exit": int(rc), "allowed": complete}, sort_keys=True))
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())

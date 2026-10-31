"""Local deterministic refresh + atomic ledger transfer; installation is an operator action.

Run from the merged repository, under the local Orca user's account. This module never
installs a timer, creates a remote directory, restarts a service, or deploys application code.
"""

from __future__ import annotations

import argparse
import fcntl
import re
import subprocess
import sys
import uuid
from pathlib import Path


def refresh(
    *, destination: str, remote_path: str, ledger: Path, dry_run=False, run=subprocess.run
) -> list[list[str]]:
    # SSH interprets its last argument as shell code; accept only literal operator paths.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]*", destination):
        raise ValueError("destination must be a literal SSH host or user@host")
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", remote_path) or ".." in remote_path.split("/"):
        raise ValueError("remote_path must be an absolute path without shell characters or '..'")
    ledger = ledger.expanduser().resolve()
    staging = remote_path + ".upload-" + uuid.uuid4().hex
    commands = [
        [sys.executable, "-m", "scripts.model_eval", "collect", "--out", str(ledger)],
        ["scp", "-q", "--", str(ledger), destination + ":" + staging],
        ["ssh", "--", destination, "mv -- " + staging + " " + remote_path],
    ]
    if dry_run:
        return commands
    ledger.parent.mkdir(parents=True, exist_ok=True)
    # One publisher per ledger. A failed upload never replaces the published snapshot.
    with ledger.with_suffix(".publish.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for command in commands:
            run(command, check=True, timeout=900)
    return commands


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", required=True, help="operator-configured SSH destination")
    parser.add_argument(
        "--remote-path", default="/var/lib/dailycheck-dashboard/ledger/ledger.jsonl"
    )
    parser.add_argument(
        "--ledger", type=Path, default=Path("~/.local/state/dailycheck/eval/ledger.jsonl")
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print argv only; no collection or network"
    )
    args = parser.parse_args(argv)
    try:
        commands = refresh(
            destination=args.destination,
            remote_path=args.remote_path,
            ledger=args.ledger,
            dry_run=args.dry_run,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"[model_eval.publish] refresh failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    if args.dry_run:
        import json

        print(json.dumps(commands, ensure_ascii=False, indent=2))
    else:
        print("[model_eval.publish] collected and atomically published", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

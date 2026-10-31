"""Check PR MCP impact declarations against the main merge-base and registry ledger.

Run with the separate, mandatory ``python -m scripts.mcp_contracts --check`` gate:
that gate proves the committed ledger matches runtime declarations and maintained docs.
This checker reads Git objects as JSON; it never imports code from the base revision.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

START = "<!-- mcp-impact:v1 -->"
END = "<!-- /mcp-impact -->"
LEDGER = "docs/integrations/dailycheck-mcp/registry.json"
CATEGORIES = {"新增", "更新", "不变", "暂不开放"}
PLACEHOLDERS = {"待填", "待填写", "TODO", "TBD", "...", "…", "无", "N/A"}


def meaningful(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and value.strip().upper() not in PLACEHOLDERS
        and not value.strip().startswith(("待填", "TODO", "TBD", "<", "（", "("))
    )


def requires_assessment(paths: list[str]) -> bool:
    """Conservative: all non-test Python/SQL plus business/config/permission trees.

    Shared calculations can live outside today's known module list. Treat future
    Python modules as in scope too, rather than silently missing that new location.
    Both sides of Git renames are supplied by changed_paths().
    """
    prefixes = ("blueprints/", "db/", "migrations/", "mcp_server/", "bot/", "config/", "permissions/")
    for path in paths:
        if path.startswith("tests/"):
            continue
        if path.startswith(prefixes) or path == LEDGER:
            return True
        if PurePosixPath(path).suffix.lower() in {".py", ".sql"}:
            return True
        if PurePosixPath(path).name.startswith(("config.", "permissions.")):
            return True
    return False


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def declaration(body: str) -> list[dict[str, Any]] | None:
    if START not in body and END not in body:
        return None
    if body.count(START) != 1 or body.count(END) != 1:
        raise ValueError("MCP declaration requires exactly one start/end marker")
    region = body.split(START)[1].split(END)[0]
    if body.index(END) < body.index(START):
        raise ValueError("MCP declaration markers are reversed")
    match = re.fullmatch(r"\s*```json\s*\n(.*?)\n```\s*", region, re.S)
    if not match:
        raise ValueError("MCP declaration must contain one JSON fenced block")
    data = json.loads(match[1], object_pairs_hook=_unique_object)
    if not isinstance(data, dict) or set(data) != {"version", "changes"} or type(data["version"]) is not int or data["version"] != 1:
        raise ValueError("MCP declaration requires version=1 and changes")
    changes = data["changes"]
    if not isinstance(changes, list) or not changes:
        raise ValueError("MCP changes must be a nonempty array")
    for entry in changes:
        required = {"category", "tools", "reason", "evidence"}
        if not isinstance(entry, dict) or not required <= set(entry) or set(entry) - required - {"compatibility"}:
            raise ValueError("MCP entry requires category/tools/reason/evidence; optional compatibility")
        if not isinstance(entry["category"], str) or entry["category"] not in CATEGORIES:
            raise ValueError("MCP category must be 新增/更新/不变/暂不开放")
        names = entry["tools"]
        if not isinstance(names, list) or any(not isinstance(n, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", n) for n in names) or len(set(names)) != len(names):
            raise ValueError("MCP tools must be a unique array of tool names")
        if not meaningful(entry["reason"]):
            raise ValueError("MCP reason is empty or a placeholder")
        evidence = entry["evidence"]
        if not isinstance(evidence, list) or not evidence or not all(meaningful(e) for e in evidence):
            raise ValueError("MCP evidence must list concrete test commands/results")
        if "compatibility" in entry and not meaningful(entry["compatibility"]):
            raise ValueError("MCP compatibility is empty or a placeholder")
        if entry["category"] in {"新增", "更新"} and not names:
            raise ValueError("新增/更新 requires affected tools")
    return changes


def assess(body: str, paths: list[str], before: dict, after: dict) -> list[str]:
    """Pure policy check. A renamed tool is an old deletion plus a new addition."""
    added = after.keys() - before.keys()
    removed = before.keys() - after.keys()
    updated = {n for n in before.keys() & after.keys() if before[n] != after[n]}
    try:
        entries = declaration(body)
    except (ValueError, TypeError, RecursionError) as exc:
        return [str(exc)]
    if entries is None:
        return ["missing MCP impact declaration"] if requires_assessment(paths) or added or removed or updated else []
    errors = []
    declared = {}
    for entry in entries:
        category = entry["category"]
        for name in entry["tools"]:
            if name in declared:
                errors.append(f"tool declared more than once: {name}")
            declared[name] = entry
            if name not in before and name not in after:
                errors.append(f"unknown tool: {name}")
            elif category == "新增" and name not in added:
                errors.append(f"新增 tool is not newly registered: {name}")
            elif category == "更新" and name in added:
                errors.append(f"newly registered tool requires 新增: {name}")
            elif category in {"不变", "暂不开放"} and name in added | removed | updated:
                errors.append(f"actual registry change cannot claim {category}: {name}")
    for name in sorted(added | removed | updated):
        expected = "新增" if name in added else "更新"
        entry = declared.get(name)
        if entry is None or entry["category"] != expected:
            errors.append(f"registry change requires {expected} declaration: {name}")
        if name in removed and (entry is None or not meaningful(entry.get("compatibility"))):
            errors.append(f"removed/renamed tool requires explicit compatibility handling: {name}")
    return errors


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    if result.returncode:
        raise ValueError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout


def changed_paths(root: Path, base: str, head: str) -> list[str]:
    # --name-only -z without rename detection includes both the old and new name.
    return [p for p in git(root, "diff", "--no-renames", "--name-only", "-z", base, head).split("\0") if p]


def ledger(root: Path, ref: str, *, allow_missing: bool = False) -> dict[str, dict]:
    if not git(root, "ls-tree", "--name-only", ref, "--", LEDGER).strip():
        if allow_missing:
            return {}
        raise ValueError(f"missing current MCP registry ledger: {ref}:{LEDGER}")
    data = json.loads(git(root, "show", f"{ref}:{LEDGER}"), object_pairs_hook=_unique_object)
    if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("tools"), list):
        raise ValueError(f"invalid MCP registry ledger at {ref}")
    records = {}
    for entry in data["tools"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or not re.fullmatch(r"[a-z][a-z0-9_]*", entry["name"]):
            raise ValueError(f"invalid tool record at {ref}")
        if entry["name"] in records:
            raise ValueError(f"duplicate registry tool at {ref}: {entry['name']}")
        records[entry["name"]] = entry
    return records


def check(root: Path, base: str, head: str, body: str) -> tuple[str, list[str]]:
    # Resolve immutable commits first. An invalid/missing base must fail closed.
    base_sha = git(root, "rev-parse", "--verify", f"{base}^{{commit}}").strip()
    head_sha = git(root, "rev-parse", "--verify", f"{head}^{{commit}}").strip()
    merge_base = git(root, "merge-base", base_sha, head_sha).strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", merge_base):
        raise ValueError("cannot determine a unique main merge-base")
    return merge_base, assess(
        body, changed_paths(root, merge_base, head_sha),
        ledger(root, merge_base, allow_missing=True), ledger(root, head_sha),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=os.environ.get("BASE_SHA") or "origin/main")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--body-file", default=os.environ.get("PR_BODY_FILE"))
    args = parser.parse_args(argv)
    try:
        body = Path(args.body_file).read_text(encoding="utf-8") if args.body_file else os.environ.get("PR_BODY", "")
        merge_base, errors = check(Path.cwd(), args.base, args.head, body)
    except (OSError, ValueError, RecursionError) as exc:
        print(f"[check_mcp_impact] ERROR: {exc}")
        return 2
    print(f"[check_mcp_impact] merge-base={merge_base}; findings={len(errors)}")
    for error in errors:
        print(f"  {error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

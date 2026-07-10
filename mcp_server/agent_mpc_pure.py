"""Pure helpers for the Agent MPC blueprint.

This module is intentionally free of Flask / DB imports so it can be
unit-tested without any app context. It contains:

- `path_matches(pattern, path)`: per-token path authorization (spec §2.3)

Path matching semantics (PRD §2.3.2 + spec §0 self-decision):
- exact: pattern "/api/v1/items" matches only "/api/v1/items"
- prefix: same pattern ALSO matches "/api/v1/items/123", "/api/v1/items/x/y"
- wildcard: trailing "*" (only at end) matches any continuation
- middle "*" is treated as a literal — spec only locks trailing wildcard
"""
from __future__ import annotations


def path_matches(pattern: str, path: str) -> bool:
    """Return True if `pattern` authorizes `path` per spec §2.3.

    Rules:
    1. If pattern ends with "*", strip the "*" and check that path
       starts with the remaining prefix (the wildcard can match zero chars).
    2. Otherwise, the pattern acts as a prefix: path must start with
       pattern followed by either nothing or "/".
    """
    if not pattern:
        return path == ""
    if pattern.endswith("*"):
        prefix = pattern[:-1]
        return path.startswith(prefix)
    # No wildcard → exact OR prefix-with-slash.
    if path == pattern:
        return True
    if path.startswith(pattern + "/"):
        return True
    return False

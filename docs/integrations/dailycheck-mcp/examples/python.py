#!/usr/bin/env python3
"""
DailyCheck MCP — minimal Python reference client.

Dependencies: stdlib only (urllib, json). Tested against Python 3.10+.

Run as:
    DAILYCHECK_MCP_URL=http://localhost:5100 \\
    DAILYCHECK_MCP_TOKEN=dev-mcp-token-for-testing \\
    python python.py

Behavior:
1. initialize           → confirm protocol/server
2. tools/list           → load the tool registry
3. warehouse_list       → fetch warehouse codes
4. warehouse_consumption → demo call (wh_001, 7d, top 10 by turnover)
5. trigger and handle every documented error envelope
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

ENDPOINT = os.environ.get(
    "DAILYCHECK_MCP_URL", "http://localhost:5100"
).rstrip("/") + "/api/mcp/"
TOKEN = os.environ["DAILYCHECK_MCP_TOKEN"]  # may raise KeyError → fails fast


# -------- low-level transport --------

def _post(body: dict[str, Any]) -> dict[str, Any]:
    """Send one JSON-RPC request, return parsed response dict.

    Raises urllib.error.HTTPError for non-2xx (other than 202).
    """
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        ENDPOINT,
        data=raw,
        method="POST",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 401 is the auth middleware envelope (not JSON-RPC)
        if e.code == 401:
            raise PermissionError("401 Unauthorized — check DAILYCHECK_MCP_TOKEN") from e
        # everything else: surface the JSON body
        body = e.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            raise RuntimeError(f"HTTP {e.code}: {body[:500]}") from e


# -------- error normalization --------

class JsonRpcError(RuntimeError):
    """Top-level JSON-RPC error envelope (HTTP 4xx/2xx with 'error' key)."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"JSON-RPC {code}: {message}")
        self.code = code
        self.message = message


class ToolError(RuntimeError):
    """Tool-level business error (HTTP 200 with result.isError)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"tool {code}: {message}")
        self.code = code
        self.message = message


def call(method: str, params: dict[str, Any] | None = None, *, _id: int = 1) -> Any:
    """One MCP round-trip. Returns the **parsed tool data** on success,
    raises JsonRpcError / ToolError otherwise."""
    payload: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": _id,
        "method": method,
    }
    if params is not None:
        payload["params"] = params

    body = _post(payload)

    # §1 — JSON-RPC envelope error
    if "error" in body:
        err = body["error"]
        raise JsonRpcError(err.get("code", -1), err.get("message", "?"))

    result = body.get("result", {})

    # §2 — tool error
    if result.get("isError"):
        text = result["content"][0]["text"]
        # Sometimes it's plain ("Unknown tool: ..."), sometimes JSON.
        try:
            parsed = json.loads(text)
            raise ToolError(parsed.get("error", "?"), parsed.get("message", text))
        except json.JSONDecodeError:
            raise ToolError("unknown_tool_or_unparseable", text) from None

    # Some tool implementations skip the `isError=true` flag and instead
    # deliver an error JSON inside the payload. The §5 demo relies on this
    # branch — but real callers can usually ignore it; the surfaced exception
    # is the same ToolError either way.
    content = result.get("content") or []
    if content:
        text = content[0].get("text", "")
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict) and parsed.get("error") and parsed.get("message"):
                raise ToolError(parsed["error"], parsed["message"])
        except json.JSONDecodeError:
            pass

    # success path — extract the text payload
    if method == "tools/call":
        text = result["content"][0]["text"]
        return json.loads(text)

    return result  # e.g. for tools/list or initialize


# -------- demo --------

def demo() -> int:
    print("=" * 60)
    print("1. initialize")
    print("=" * 60)
    info = call("initialize", {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "python-smoke", "version": "0.1.0"},
    }, _id=1)
    print(f"server: {info['serverInfo']['name']} v{info['serverInfo']['version']}")

    print()
    print("=" * 60)
    print("2. tools/list — registry snapshot")
    print("=" * 60)
    registry = call("tools/list", _id=2)
    print(f"  {len(registry['tools'])} tools: ", end="")
    print(", ".join(t["name"] for t in registry["tools"]))

    print()
    print("=" * 60)
    print("3. tools/call warehouse_list")
    print("=" * 60)
    warehouses = call("tools/call", {
        "name": "warehouse_list",
        "arguments": {},
    }, _id=3)
    for w in warehouses:
        print(f"  - {w['code']:10s} {w['name']}")

    print()
    print("=" * 60)
    print("4. tools/call warehouse_consumption (wh_001, days=7, top 10 by turnover)")
    print("=" * 60)
    result = call("tools/call", {
        "name": "warehouse_consumption",
        "arguments": {
            "warehouse_code": "wh_001",
            "days": 7,
            "sort_by": "turnover",
            "limit": 10,
        },
    }, _id=4)
    # Returns {items: [...], warehouse_turnover: {...}}
    items = result["items"]
    wt = result["warehouse_turnover"]
    print(f"  warehouse turnover: {wt['turnover_value']} (quality={wt['data_quality']}, "
          f"items={wt['items_with_turnover']}/{wt['items_total']})")
    print(f"  {len(items)} items (showing up to 5):")
    for r in items[:5]:
        print(
            f"  - rank={r['rank']:>2}  sku={r['sku']:<30}  "
            f"consume_qty={r['consume_qty']:<6}  "
            f"current_stock={r['current_stock']:<6}  "
            f"turnover_rate={r['turnover_rate']}"
        )

    print()
    print("=" * 60)
    print("5. error envelopes — every documented failure")
    print("=" * 60)
    cases = [
        ("missing warehouse_code", {"name": "warehouse_consumption", "arguments": {}}),
        ("bad days value", {"name": "warehouse_consumption", "arguments": {"warehouse_code": "wh_001", "days": 99}}),
        ("unknown tool", {"name": "does_not_exist", "arguments": {}}),
        ("not_found item", {"name": "item_consumption", "arguments": {"warehouse_code": "wh_001", "item_id": 999999}}),
    ]
    for desc, args in cases:
        try:
            call("tools/call", args, _id=99)
            print(f"  [UNEXPECTED] {desc}: call succeeded")
        except JsonRpcError as e:
            print(f"  JsonRpcError   {desc:<28} → code={e.code} {e.message[:60]}")
        except ToolError as e:
            print(f"  ToolError      {desc:<28} → {e.code} {e.message[:60]}")

    # §1 envelope: malformed request
    print()
    print("  -- top-level JSON-RPC errors --")
    try:
        # missing Accept  → we cannot set headers from inside call(),
        # so demonstrate via a separate raw call:
        req = urllib.request.Request(
            ENDPOINT,
            data=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
            method="POST",
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError as e:
        body = json.loads(e.read().decode("utf-8"))
        print(f"  HTTPError      no Accept header                  → {e.code} {body['error']['message'][:60]}")

    print()
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(demo())

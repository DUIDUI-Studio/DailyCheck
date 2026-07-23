#!/usr/bin/env bash
#
# DailyCheck MCP — curl reference
# ------------------------------
# Run as:   DAILYCHECK_MCP_URL=http://localhost:5100 \
#           DAILYCHECK_MCP_TOKEN=dev-mcp-token-for-testing \
#           ./curl.sh
#
# Or edit DEFAULTS below.

set -euo pipefail

# ---- config ----
: "${DAILYCHECK_MCP_URL:=http://localhost:5100}"
: "${DAILYCHECK_MCP_TOKEN:=dev-mcp-token-for-testing}"

ENDPOINT="${DAILYCHECK_MCP_URL%/}/api/mcp/"

# ---- helpers ----

# send_one METHOD BODY  →  prints body, fails fast on non-2xx
send_one() {
  local body="$1"
  local http
  local out
  out=$(mktemp)
  http=$(curl -sS --max-time 10 -o "$out" -w '%{http_code}' \
    -X POST "$ENDPOINT" \
    -H "Authorization: Bearer ${DAILYCHECK_MCP_TOKEN}" \
    -H "Accept: application/json" \
    -H "Content-Type: application/json" \
    -d "$body" || true)

  cat "$out"
  rm -f "$out"

  # 202 = notifications; treat as success
  if [[ "$http" =~ ^(2[0-9][0-9])$ ]] && [[ "$http" != "202" ]]; then :; fi
  if [[ "$http" == "401" ]]; then
    echo "" >&2
    echo "ERROR: 401 Unauthorized — check DAILYCHECK_MCP_TOKEN" >&2
    return 1
  fi
}

# ---- 1. initialize ----

echo "## 1. initialize"
send_one '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"curl-smoke","version":"0.1.0"}}}'
echo
echo

# ---- 2. tools/list ----

echo "## 2. tools/list (top-level)"
send_one '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | head -c 400
echo
echo "..."

# ---- 3. warehouse_list ----

echo
echo "## 3. tools/call warehouse_list"
RAW=$(send_one '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"warehouse_list","arguments":{}}}')
echo "$RAW" | head -c 400
echo

# ---- 4. warehouse_consumption ----

echo
echo "## 4. tools/call warehouse_consumption (wh_001, days=7, top 5 by turnover)"
RAW=$(send_one '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"warehouse_consumption","arguments":{"warehouse_code":"wh_001","days":7,"sort_by":"turnover","limit":5}}}')
TEXT=$(printf '%s' "$RAW" | python3 -c 'import sys,json; r=json.load(sys.stdin); print(r["result"]["content"][0]["text"])')
printf '%s\n' "$TEXT" | python3 -m json.tool | head -40

# ---- 5. validation error (missing warehouse_code) ----

echo
echo "## 5. validation error (missing warehouse_code)"
send_one '{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"warehouse_consumption","arguments":{}}}'

echo
echo

# ---- 6. unknown tool ----

echo
echo "## 6. unknown tool"
send_one '{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"does_not_exist","arguments":{}}}'

echo
echo "## Done."

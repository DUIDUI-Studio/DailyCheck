#!/bin/bash
# deploy/scripts/dc-notify-prod-agent.sh —— inbox → deterministic reducer → Hermes
#
# Backward-compatible watcher interface:
#   - default mode reads exactly one inbox JSON line from stdin.
#   - exit 0  = consumed or intentionally suppressed.
#   - exit 75 = durably deferred (watcher retries; drain timer is a second path).
#   - other non-zero = state/provider failure; watcher retries with backoff.
#
# The reducer emits one JSON decision on stdout:
#   {"decision":"suppress",...}  no provider wake
#   {"decision":"defer",...}     queued; due_at_ms says when it becomes eligible
#   {"decision":"trigger",...}   one active lease claimed for this PR
#
# Routes are intentionally limited to:
#   pull_request  -> wdg-pr
#   check_suite   -> wdg-ci
#   deploy_result -> dailycheck-deploy
#
# `dc-notify-prod-agent --drain` runs the same send/complete path for queued work
# and is used by dc-prod-agent-drain.timer.
#
# Installation:
#   cp deploy/scripts/dc-notify-prod-agent.sh /usr/local/bin/dc-notify-prod-agent
#   chmod 755 /usr/local/bin/dc-notify-prod-agent
#
# Test/deployment overrides:
#   DAILYCHECK_PYTHON, DAILYCHECK_EVENT_REDUCER,
#   DAILYCHECK_EVENT_REDUCER_DB, DAILYCHECK_HERMES

set -uo pipefail

PYTHON_BIN="${DAILYCHECK_PYTHON:-/root/DailyCheck/.venv/bin/python}"
REDUCER="${DAILYCHECK_EVENT_REDUCER:-/root/DailyCheck/scripts/prod_agent/event_reducer.py}"
REDUCER_DB="${DAILYCHECK_EVENT_REDUCER_DB:-/var/lib/dailycheck-prod-agent/event_reducer.sqlite3}"
HERMES="${DAILYCHECK_HERMES:-/root/hermes-agent/.venv/bin/hermes}"

common_args=(
    --db "$REDUCER_DB"
    --debounce-ms "${DAILYCHECK_REDUCER_DEBOUNCE_MS:-2000}"
    --max-debounce-ms "${DAILYCHECK_REDUCER_MAX_DEBOUNCE_MS:-10000}"
    --lease-ttl-ms "${DAILYCHECK_REDUCER_LEASE_TTL_MS:-30000}"
    --provider-cooldown-ms "${DAILYCHECK_REDUCER_COOLDOWN_MS:-60000}"
)

if [ "${1:-}" = "--drain" ]; then
    if [ "$#" -ne 1 ]; then
        echo "dc-notify-prod-agent: --drain takes no additional arguments" >&2
        exit 64
    fi
    decision=$("$PYTHON_BIN" "$REDUCER" "${common_args[@]}" drain)
    reducer_rc=$?
elif [ "$#" -eq 0 ]; then
    payload=$(cat)
    if [ -z "$payload" ]; then
        echo "dc-notify-prod-agent: empty payload on stdin" >&2
        exit 2
    fi
    decision=$(printf '%s' "$payload" | "$PYTHON_BIN" "$REDUCER" "${common_args[@]}" ingest)
    reducer_rc=$?
else
    echo "dc-notify-prod-agent: unsupported arguments" >&2
    exit 64
fi

if [ "$reducer_rc" -ne 0 ] && [ "$reducer_rc" -ne 75 ]; then
    printf '%s\n' "$decision"
    exit "$reducer_rc"
fi

printf '%s\n' "$decision"
if [ "$reducer_rc" -eq 75 ]; then
    exit 75
fi

decision_kind=$(
    printf '%s' "$decision" | "$PYTHON_BIN" -c \
        'import json,sys; print(json.load(sys.stdin).get("decision", ""))'
) || {
    echo "dc-notify-prod-agent: reducer returned invalid JSON" >&2
    exit 1
}

case "$decision_kind" in
    suppress|silent)
        exit 0
        ;;
    defer)
        exit 75
        ;;
    trigger)
        ;;
    *)
        echo "dc-notify-prod-agent: unknown decision $decision_kind" >&2
        exit 1
        ;;
esac

payload_file=$(mktemp)
meta_file=$(mktemp)
cleanup() {
    rm -f "$payload_file" "$meta_file"
}
trap cleanup EXIT

if ! printf '%s' "$decision" | "$PYTHON_BIN" -c '
import json, sys
row = json.load(sys.stdin)
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    json.dump(row["payload"], fh, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
print(row["route"])
print(row["trigger_id"])
print(row["lease"])
' "$payload_file" >"$meta_file"; then
    echo "dc-notify-prod-agent: trigger decision is missing route/trigger/lease" >&2
    exit 1
fi

route=$(sed -n '1p' "$meta_file")
trigger_id=$(sed -n '2p' "$meta_file")
lease=$(sed -n '3p' "$meta_file")
payload=$(cat "$payload_file")

case "$route" in
    wdg-pr|wdg-ci|dailycheck-deploy) ;;
    *)
        echo "dc-notify-prod-agent: reducer returned unapproved route" >&2
        exit 1
        ;;
esac

# Capture provider diagnostics for quota detection, but do not echo them:
# they may contain prompt content or credentials from a downstream adapter.
provider_output=$("$HERMES" webhook test "$route" --payload "$payload" 2>&1)
provider_rc=$?

if [ "$provider_rc" -eq 0 ]; then
    completion=$(
        "$PYTHON_BIN" "$REDUCER" "${common_args[@]}" complete \
            --trigger-id "$trigger_id" --lease "$lease"
    )
    completion_rc=$?
    completion_ok=$(
        printf '%s' "$completion" | "$PYTHON_BIN" -c \
            'import json,sys; print(str(json.load(sys.stdin).get("completed", False)).lower())'
    ) || completion_ok=false
    if [ "$completion_rc" -ne 0 ] || [ "$completion_ok" != "true" ]; then
        printf '%s\n' "$completion" >&2
        exit 1
    fi
    exit 0
fi

provider_lower=$(printf '%s' "$provider_output" | tr '[:upper:]' '[:lower:]')
quota_flag=()
case "$provider_lower" in
    *429*|*quota*|*rate_limit*|*rate\ limit*)
        quota_flag=(--quota-exhausted)
        ;;
esac
reason=${provider_output:0:400}
failure=$(
    "$PYTHON_BIN" "$REDUCER" "${common_args[@]}" fail \
        --trigger-id "$trigger_id" --lease "$lease" \
        --reason "$reason" "${quota_flag[@]}"
)
failure_rc=$?
printf '%s\n' "$failure" >&2
if [ "$failure_rc" -eq 75 ]; then
    exit 75
fi
exit "${failure_rc:-1}"

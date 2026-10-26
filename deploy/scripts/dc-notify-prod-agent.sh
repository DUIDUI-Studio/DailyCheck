#!/bin/bash
# deploy/scripts/dc-notify-prod-agent.sh —— inbox → deterministic reducer → Hermes HTTP
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
# Hermes enqueue is asynchronous: HTTP 2xx means the gateway accepted the
# event, not that the agent turn finished. The adapter completes the reducer
# lease immediately after a 2xx enqueue. A 20-minute per-PR lease prevents a
# recovery drain from duplicating a still-running gateway session.
#
# `dc-notify-prod-agent --drain` runs the same enqueue/complete path for queued
# work and is used by dc-prod-agent-drain.timer.
#
# Installation:
#   cp deploy/scripts/dc-notify-prod-agent.sh /usr/local/bin/dc-notify-prod-agent
#   chmod 755 /usr/local/bin/dc-notify-prod-agent
#
# Test/deployment overrides:
#   DAILYCHECK_PYTHON, DAILYCHECK_EVENT_REDUCER,
#   DAILYCHECK_EVENT_REDUCER_DB, DAILYCHECK_HERMES_SENDER,
#   DAILYCHECK_HERMES_WEBHOOK_URL, DAILYCHECK_HERMES_SUBSCRIPTIONS

set -uo pipefail

PYTHON_BIN="${DAILYCHECK_PYTHON:-/root/DailyCheck/.venv/bin/python}"
REDUCER="${DAILYCHECK_EVENT_REDUCER:-/root/DailyCheck/scripts/prod_agent/event_reducer.py}"
REDUCER_DB="${DAILYCHECK_EVENT_REDUCER_DB:-/var/lib/dailycheck-prod-agent/event_reducer.sqlite3}"
HERMES_SENDER="${DAILYCHECK_HERMES_SENDER:-/root/DailyCheck/scripts/prod_agent/hermes_webhook_sender.py}"
HERMES_WEBHOOK_URL="${DAILYCHECK_HERMES_WEBHOOK_URL:-http://127.0.0.1:8644}"
HERMES_SUBSCRIPTIONS="${DAILYCHECK_HERMES_SUBSCRIPTIONS:-/root/.hermes/webhook_subscriptions.json}"

common_args=(
    --db "$REDUCER_DB"
    --debounce-ms "${DAILYCHECK_REDUCER_DEBOUNCE_MS:-2000}"
    --max-debounce-ms "${DAILYCHECK_REDUCER_MAX_DEBOUNCE_MS:-10000}"
    --lease-ttl-ms "${DAILYCHECK_REDUCER_LEASE_TTL_MS:-1200000}"
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
print(row["payload"]["event"])
' "$payload_file" >"$meta_file"; then
    echo "dc-notify-prod-agent: trigger decision is missing route/trigger/lease" >&2
    exit 1
fi

route=$(sed -n '1p' "$meta_file")
trigger_id=$(sed -n '2p' "$meta_file")
lease=$(sed -n '3p' "$meta_file")
event=$(sed -n '4p' "$meta_file")

case "$route" in
    wdg-pr|wdg-ci|dailycheck-deploy) ;;
    *)
        echo "dc-notify-prod-agent: reducer returned unapproved route" >&2
        exit 1
        ;;
esac

case "$event" in
    pull_request|check_suite|deploy_result) ;;
    *)
        echo "dc-notify-prod-agent: reducer returned unsupported event" >&2
        exit 1
        ;;
esac

delivery_id="dailycheck-${trigger_id}"
sender_result=$(
    "$PYTHON_BIN" "$HERMES_SENDER" \
        --subscriptions "$HERMES_SUBSCRIPTIONS" \
        --base-url "$HERMES_WEBHOOK_URL" \
        --route "$route" \
        --event "$event" \
        --delivery-id "$delivery_id" \
        --body-file "$payload_file"
)
sender_rc=$?

sender_accepted=false
sender_status=0
sender_retry_after_ms=0
if sender_fields=$(
    printf '%s' "$sender_result" | "$PYTHON_BIN" -c '
import json, sys
row = json.load(sys.stdin)
print(str(row.get("accepted", False)).lower())
print(int(row.get("http_status", 0)))
print(int(row.get("retry_after_ms", 0)))
' 2>/dev/null
); then
    sender_accepted=$(printf '%s\n' "$sender_fields" | sed -n '1p')
    sender_status=$(printf '%s\n' "$sender_fields" | sed -n '2p')
    sender_retry_after_ms=$(printf '%s\n' "$sender_fields" | sed -n '3p')
fi

if [ "$sender_rc" -eq 0 ] && [ "$sender_accepted" = "true" ]; then
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

if [ "$sender_rc" -eq 3 ] || [ "$sender_status" -eq 429 ]; then
    reason="http_status=429; retry_after_ms=${sender_retry_after_ms}"
elif [ "$sender_status" -gt 0 ]; then
    reason="http_status=${sender_status}"
elif [ "$sender_rc" -eq 0 ]; then
    reason="http_enqueue_rejected"
else
    reason="http_enqueue_exit=${sender_rc}"
fi

quota_flag=()
retry_flag=()
if [ "$sender_rc" -eq 3 ] || [ "$sender_status" -eq 429 ]; then
    quota_flag=(--quota-exhausted)
    if [ "$sender_retry_after_ms" -gt 0 ]; then
        retry_flag=(--retry-after-ms "$sender_retry_after_ms")
    fi
fi

failure=$(
    "$PYTHON_BIN" "$REDUCER" "${common_args[@]}" fail \
        --trigger-id "$trigger_id" --lease "$lease" \
        --reason="$reason" \
        ${quota_flag[@]+"${quota_flag[@]}"} \
        ${retry_flag[@]+"${retry_flag[@]}"}
)
failure_rc=$?
printf '%s\n' "$failure" >&2
if [ "$failure_rc" -eq 75 ]; then
    exit 75
fi
exit "${failure_rc:-1}"

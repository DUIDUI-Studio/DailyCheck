#!/usr/bin/env python3
"""Send one exact JSON body to a local Hermes webhook route.

This is intentionally a small stdlib-only enqueue client: Hermes returns 202 as
soon as it accepts the event, before the agent turn completes. Only deterministic
``MERGE_READY`` (``wdg-ci``) and ``deploy_result`` work can be sent; PR lifecycle
events are rejected. The sender reads the route HMAC secret from
``webhook_subscriptions.json`` and never prints it.
"""

from __future__ import annotations

import argparse
import email.utils
import hashlib
import hmac
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.hosting import HostingError, require_production_work  # noqa: E402

ROUTES = frozenset({"wdg-ci", "dailycheck-deploy"})
EVENTS = frozenset({"check_suite", "deploy_result"})
DEFAULT_SUBSCRIPTIONS = Path(
    os.environ.get("DAILYCHECK_HERMES_SUBSCRIPTIONS", "/root/.hermes/webhook_subscriptions.json")
)
DEFAULT_BASE_URL = os.environ.get("DAILYCHECK_HERMES_WEBHOOK_URL", "http://127.0.0.1:8644")
MAX_BODY_BYTES = 1_048_576


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _retry_after_ms(value: str | None, *, now_s: float | None = None) -> int:
    if not value:
        return 0
    value = value.strip()
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        seconds = -1
    if math.isfinite(seconds) and seconds >= 0:
        return min(int(seconds * 1000), 86_400_000)
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    current = time.time() if now_s is None else now_s
    return max(0, min(int((parsed.timestamp() - current) * 1000), 86_400_000))


def _load_secret(path: Path, route: str) -> str:
    try:
        subscriptions = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        raise ValueError("subscriptions_unavailable") from exc
    route_config = subscriptions.get(route) if isinstance(subscriptions, dict) else None
    secret = route_config.get("secret") if isinstance(route_config, dict) else None
    if not isinstance(secret, str) or not secret or secret == "INSECURE_NO_AUTH":
        raise ValueError("route_secret_unavailable")
    return secret


def _base_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid_local_webhook_url")
    return value.rstrip("/")


def _emit(payload: dict[str, Any]) -> None:
    print(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
        flush=True,
    )


def send(
    *,
    subscriptions_path: Path,
    base_url: str,
    route: str,
    event: str,
    delivery_id: str,
    body: bytes,
    timeout_s: float = 10.0,
) -> tuple[int, dict[str, Any]]:
    try:
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError):
            payload = None
        require_production_work(payload)
    except HostingError:
        return 5, {"accepted": False, "reason": "github_shadow_only"}
    if route not in ROUTES or event not in EVENTS:
        return 5, {"accepted": False, "reason": "invalid_route_or_event"}
    if not delivery_id or len(delivery_id) > 256:
        return 5, {"accepted": False, "reason": "invalid_delivery_id"}
    if len(body) > MAX_BODY_BYTES:
        return 5, {"accepted": False, "reason": "payload_too_large"}
    try:
        secret = _load_secret(subscriptions_path, route)
        normalized_base_url = _base_url(base_url)
    except ValueError as exc:
        return 5, {"accepted": False, "reason": str(exc)}

    signature = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    try:
        request = urllib.request.Request(
            f"{normalized_base_url}/webhooks/{urllib.parse.quote(route, safe='')}",
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "dailycheck-prod-agent/1",
                "X-GitHub-Event": event,
                "X-GitHub-Delivery": delivery_id,
                "X-Hub-Signature-256": signature,
            },
            method="POST",
        )
        opener = urllib.request.build_opener(_NoRedirectHandler())
        with opener.open(request, timeout=timeout_s) as response:
            status = int(response.status)
            response.read(4096)
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        exc.close()
        if status == 429:
            return 3, {
                "accepted": False,
                "http_status": status,
                "retry_after_ms": _retry_after_ms(exc.headers.get("Retry-After")),
                "reason": "http_429",
            }
        return 4, {
            "accepted": False,
            "http_status": status,
            "reason": "http_error",
        }
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return 4, {
            "accepted": False,
            "reason": "transport_error",
        }

    if 200 <= status < 300:
        return 0, {"accepted": True, "http_status": status}
    return 4, {"accepted": False, "http_status": status, "reason": "http_error"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subscriptions", default=str(DEFAULT_SUBSCRIPTIONS))
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--route", required=True)
    parser.add_argument("--event", required=True)
    parser.add_argument("--delivery-id", required=True)
    parser.add_argument("--body-file", required=True)
    parser.add_argument("--timeout-s", type=float, default=10.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["--production-guard"]:
        try:
            raw = sys.stdin.buffer.read(MAX_BODY_BYTES + 1)
            if len(raw) > MAX_BODY_BYTES:
                raise HostingError("invalid_guard_payload")
            try:
                payload = json.loads(raw) if raw else None
            except (ValueError, UnicodeError):
                payload = None
            require_production_work(payload)
        except HostingError as exc:
            shadow = str(exc) == "github_shadow_only"
            _emit({"decision": "suppress" if shadow else "retry",
                   "reason": "github_shadow_only" if shadow else "invalid_hosting_configuration"})
            return 5 if shadow else 1
        return 0
    args = build_parser().parse_args(argv)
    try:
        body = Path(args.body_file).read_bytes()
    except OSError:
        _emit({"accepted": False, "reason": "body_unreadable"})
        return 5
    rc, result = send(
        subscriptions_path=Path(args.subscriptions),
        base_url=args.base_url,
        route=args.route,
        event=args.event,
        delivery_id=args.delivery_id,
        body=body,
        timeout_s=args.timeout_s,
    )
    _emit(result)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

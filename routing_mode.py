"""Signed loopback witness for Web's fulfillment mode on the same master file."""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import secrets
from pathlib import Path

from routing_writer_fence import routing_writer_fence


def _key() -> bytes:
    secret = os.environ.get("DAILYCHECK_ROUTING_PROBE_SECRET", "")
    return secret.encode() if len(secret) >= 32 else b""


def _proof(key: bytes, purpose: str, nonce: str, payload: bytes = b"") -> str:
    return hmac.digest(key, purpose.encode() + b":" + nonce.encode() + b":" + payload, "sha256").hex()


def _snapshot(db_path: Path, *, dry_run: bool) -> dict:
    from blueprints.store_ordering_pure import multi_warehouse_enabled

    stat = db_path.stat()
    database = hashlib.sha256(f"{stat.st_dev}:{stat.st_ino}".encode()).hexdigest()
    return {"multi_warehouse": multi_warehouse_enabled(), "dry_run": dry_run, "database": database}


def web_witness(db_path: Path, *, dry_run: bool, nonce: str, proof: str) -> dict | None:
    key = _key()
    if not key or len(nonce) != 64 or any(c not in "0123456789abcdef" for c in nonce):
        return None
    if not hmac.compare_digest(proof.encode(), _proof(key, "request", nonce).encode()):
        return None
    payload = json.dumps(_snapshot(db_path, dry_run=dry_run), sort_keys=True)
    return {"snapshot": payload, "proof": _proof(key, "response", nonce, payload.encode())}


def _web_mode(database: str) -> dict | None:
    key = _key()
    if not key:
        return None
    try:
        port = int(os.environ.get("DAILYCHECK_ROUTING_WEB_PORT", ""))
        if not 1 <= port <= 65535:
            return None
        nonce = secrets.token_hex(32)
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            connection.request("GET", "/health", headers={
                "X-DailyCheck-Routing-Nonce": nonce,
                "X-DailyCheck-Routing-Proof": _proof(key, "request", nonce),
            })
            response = connection.getresponse()
            if response.status != 200:
                return None
            envelope = json.loads(response.read(1024))
        finally:
            connection.close()
        raw = envelope["snapshot"].encode()
        if not hmac.compare_digest(envelope["proof"].encode(), _proof(key, "response", nonce, raw).encode()):
            return None
        web = json.loads(raw)
        if (web["database"] == database and type(web["multi_warehouse"]) is bool
                and type(web["dry_run"]) is bool):
            return web
    except (ValueError, TypeError, AttributeError, KeyError, OSError, http.client.HTTPException):
        pass
    return None


def routing_modes(db_path: Path, *, schema_version: int) -> dict:
    from config import IS_PRODUCTION

    local = _snapshot(db_path, dry_run=os.environ.get("DRY_RUN", "").lower() == "true")
    web = _web_mode(local["database"])
    enabled = bool(web and local["multi_warehouse"] and web["multi_warehouse"]
                   and not local["dry_run"] and not web["dry_run"])
    if enabled and IS_PRODUCTION:
        if schema_version != 10:
            enabled = False
        else:
            try:
                with routing_writer_fence():
                    pass
            except OSError:
                enabled = False
    return {
        "mcp_multi_warehouse": local["multi_warehouse"],
        "web_multi_warehouse": web["multi_warehouse"] if web else None,
        "writes_enabled": enabled,
    }

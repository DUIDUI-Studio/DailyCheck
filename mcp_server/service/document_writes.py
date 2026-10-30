"""Single-row document write contract. Service owns the warehouse transaction."""

from __future__ import annotations

import math
import sqlite3
import unicodedata
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext

from mcp_server.data.master import resolve_warehouse
from mcp_server.infra.errors import ForbiddenError, NotFoundError, ValidationError, WriteError
from mcp_server.service.auth import check_warehouse

OMITTED = object()
CENT = Decimal("0.01")


@dataclass(frozen=True)
class Payload:
    item_id: int
    input_quantity: str
    factor: str
    effective: Decimal
    doc_no: str | None
    line_no: str | None

    @property
    def quantity(self):
        return float(self.effective)

    @property
    def snapshot(self):
        return self.item_id, self.input_quantity, self.factor, format(self.effective, ".2f")


def canonical(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def identity(value, name, limit, byte_limit):
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > limit
        or any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in value)
    ):
        raise ValidationError(f"invalid_{name}")
    if len(value.encode("utf-8")) > byte_limit:
        raise ValidationError(f"invalid_{name}")
    return value


def positive_number(value, name):
    if type(value) not in (int, float):
        raise ValidationError(f"invalid_{name}")
    try:
        number = Decimal(str(value))
    except (ValueError, InvalidOperation):
        raise ValidationError(f"invalid_{name}") from None
    if not number.is_finite() or number <= 0:
        raise ValidationError(f"invalid_{name}")
    return number


def validate(item_id, quantity, reason, doc_no, line_no, factor):
    if type(item_id) is not int or item_id <= 0:
        raise ValidationError("invalid_item_id")
    if reason is not None and not isinstance(reason, str):
        raise ValidationError("invalid_reason")
    if (doc_no is OMITTED) != (line_no is OMITTED):
        raise ValidationError("doc_no_and_line_no_required_together")
    if doc_no is OMITTED:
        doc_no = line_no = None
    else:
        doc_no = identity(doc_no, "doc_no", 128, 512)
        line_no = identity(line_no, "line_no", 64, 256)
    qty = positive_number(quantity, "quantity")
    multiple = positive_number(factor, "factor")
    try:
        with localcontext() as context:
            context.prec = max(
                28,
                len(qty.as_tuple().digits)
                + len(multiple.as_tuple().digits)
                + max(0, qty.adjusted() + multiple.adjusted())
                + 8,
            )
            effective = (qty * multiple).quantize(CENT, rounding=ROUND_HALF_EVEN)
            real = float(effective)
            if (
                effective <= 0
                or not math.isfinite(real)
                or Decimal(str(real)).quantize(CENT, rounding=ROUND_HALF_EVEN) != effective
            ):
                raise ValidationError("quantity_out_of_range")
    except (InvalidOperation, OverflowError, ValueError):
        raise ValidationError("quantity_out_of_range") from None
    return Payload(item_id, canonical(qty), canonical(multiple), effective, doc_no, line_no)


def authorize(warehouse_code, ctx):
    if not warehouse_code:
        raise ValidationError("warehouse_code_required")
    if not check_warehouse(ctx, warehouse_code):
        raise ForbiddenError("forbidden_warehouse")


def resolve(warehouse_code, ctx, resolver=resolve_warehouse):
    authorize(warehouse_code, ctx)
    wh = resolver(warehouse_code)
    if wh is None:
        raise NotFoundError("warehouse_not_found")
    return wh


def journal(conn, direction, payload):
    if payload.doc_no is None:
        return None
    return conn.execute(
        "SELECT * FROM document_write_keys WHERE direction=? AND doc_no=? AND line_no=?",
        (direction, payload.doc_no, payload.line_no),
    ).fetchone()


def replay(conn, direction, payload, key):
    snapshot = (key["item_id"], key["input_quantity"], key["factor"], key["effective_quantity"])
    if snapshot != payload.snapshot:
        raise WriteError("document_conflict", "document_line_payload_mismatch")
    row = conn.execute(
        f"SELECT * FROM {direction}_requests WHERE id=?", (key["request_id"],)
    ).fetchone()
    if row is None:
        raise WriteError(
            "document_line_deleted",
            "document_line_deleted",
            request_id=key["request_id"],
            committed=True,
        )
    if (
        row["item_id"] != payload.item_id
        or row["doc_no"] != payload.doc_no
        or row["line_no"] != payload.line_no
        or row["input_quantity"] != payload.input_quantity
        or row["factor"] != payload.factor
        or Decimal(str(row["requested_quantity"])) != payload.effective
    ):
        raise WriteError("document_line_state_conflict", "document_line_state_conflict")
    return row


def json_number(canonical_text):
    # Canonical plain decimal strings can represent JSON integers beyond REAL range.
    return float(canonical_text) if "." in canonical_text else int(canonical_text)


def result(row, direction, warehouse_code, replayed):
    return {
        "id": row["id"],
        "item_id": row["item_id"],
        "quantity": float(row["requested_quantity"]),
        "warehouse_code": warehouse_code,
        "doc_no": row["doc_no"],
        "line_no": row["line_no"],
        "input_quantity": json_number(row["input_quantity"]),
        "factor": json_number(row["factor"]),
        "status": row["status"],
        "rolled_back": row["rolled_back"] if direction == "outbound" else None,
        "replayed": replayed,
        "applied": not replayed,
    }


def create(direction, payload, warehouse_code, wh, reason, connection, writer):
    """Acquire SQLite's cross-process lock before reading any mutable state."""
    try:
        with connection(wh["db_path"]) as conn:
            conn.execute("PRAGMA busy_timeout=5000")
            try:
                conn.execute("BEGIN IMMEDIATE")
                key = journal(conn, direction, payload)
                if key is not None:
                    row = replay(conn, direction, payload, key)
                    replayed = True
                else:
                    stock = conn.execute(
                        "SELECT quantity FROM items WHERE id=?", (payload.item_id,)
                    ).fetchone()
                    if stock is None:
                        raise NotFoundError("item_not_found")
                    if direction == "outbound" and float(stock["quantity"]) < payload.quantity:
                        raise ValidationError("insufficient_stock")
                    created = writer(
                        conn,
                        payload.item_id,
                        payload.quantity,
                        reason,
                        doc_no=payload.doc_no,
                        line_no=payload.line_no,
                        input_quantity=payload.input_quantity,
                        factor=payload.factor,
                    )
                    req_id = created["id"] if isinstance(created, dict) else created
                    row = conn.execute(
                        f"SELECT * FROM {direction}_requests WHERE id=?", (req_id,)
                    ).fetchone()
                    replayed = False
                conn.commit()
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                if "UNIQUE constraint failed" not in str(exc) or payload.doc_no is None:
                    raise
                # A uniqueness race must be reconciled in a NEW transaction.
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    key = journal(conn, direction, payload)
                    if key is None:
                        raise exc
                    row = replay(conn, direction, payload, key)
                    replayed = True
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
            except ValueError as exc:
                conn.rollback()
                if str(exc) == "insufficient_stock":
                    raise ValidationError("insufficient_stock") from None
                raise
            except BaseException:
                conn.rollback()
                raise
            return result(row, direction, warehouse_code, replayed)
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc) or "busy" in str(exc):
            raise WriteError(
                "database_busy", "database_busy", 503, retry_safe=payload.doc_no is not None
            ) from None
        raise


def invalidate(callback, item_id, request_id, retry_safe):
    try:
        callback(item_id)
    except Exception as exc:
        raise WriteError(
            "cache_invalidation_failed",
            "cache_invalidation_failed",
            503,
            request_id=request_id,
            committed=True,
            retry_safe=retry_safe,
        ) from exc

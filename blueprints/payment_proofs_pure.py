"""付款凭证（Issue #135）—— 校验、落盘、查询。

纯逻辑集中在这里，视图只做编排。这样边界（扩展名、大小、路径穿越）
可以直接被单测覆盖，不依赖 Flask 请求上下文。
"""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Any, BinaryIO

# 只收图片与 PDF：付款凭证要么是截图/拍照，要么是银行回单 PDF。
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".pdf"}
MAX_FILES_PER_ORDER = 3
MAX_FILE_BYTES = 8 * 1024 * 1024

_EXT_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
}


def validate_upload(filename: str, size: int) -> str | None:
    """校验单个文件，返回中文错误信息；None 表示通过。

    size 取上传流声明的大小（Flask 的 FileStorage 有 content_length，
    但客户端可能不传，视图侧会退化成按落盘后实测），这里只挡明显超限。
    """
    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return "仅支持 JPG / PNG / WEBP / PDF 格式"
    if size <= 0:
        return "文件为空"
    if size > MAX_FILE_BYTES:
        return f"单个文件不得超过 {MAX_FILE_BYTES // (1024 * 1024)}MB"
    return None


def store_upload(
    stream: BinaryIO,
    filename: str,
    upload_root: Path,
    order_id: int,
) -> dict[str, Any]:
    """把上传流写到 <upload_root>/<order_id>/<uuid><ext>，返回落库字段。

    文件名一律重新生成：用户给的名字只留在 original_name 用于展示，
    不参与路径拼接，从根上排除 `../../` 这类穿越。
    """
    ext = Path(filename).suffix.lower()
    stored_name = f"{uuid.uuid4().hex}{ext}"
    target_dir = Path(upload_root) / str(order_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / stored_name
    with target.open("wb") as fh:
        shutil.copyfileobj(stream, fh)
    return {
        "stored_name": stored_name,
        "original_name": Path(filename).name[:120] or stored_name,
        "mime_type": _EXT_MIME.get(ext, "application/octet-stream"),
        "size_bytes": target.stat().st_size,
    }


def proof_path(upload_root: Path, order_id: int, stored_name: str) -> Path:
    """拼磁盘路径，并确认解析后仍落在该订单目录内。

    stored_name 来自数据库，理论上已被 store_upload 生成为 uuid，
    但库被手工改过时不能让下载路由变成任意文件读取 —— 这里再兜一次。
    """
    base = (Path(upload_root) / str(order_id)).resolve()
    target = (base / stored_name).resolve()
    if target.parent != base:
        raise ValueError(f"invalid stored_name: {stored_name!r}")
    return target


def insert_proof(
    conn: Any,
    order_id: int,
    meta: dict[str, Any],
    uploaded_by: int,
    created_at: str,
) -> int:
    cur = conn.execute(
        """INSERT INTO store_order_payment_proofs
           (order_id, stored_name, original_name, mime_type, size_bytes,
            uploaded_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            order_id,
            meta["stored_name"],
            meta["original_name"],
            meta["mime_type"],
            int(meta["size_bytes"]),
            uploaded_by,
            created_at,
        ),
    )
    return int(cur.lastrowid)


def list_proofs(conn: Any, order_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT id, stored_name, original_name, mime_type, size_bytes,
                  uploaded_by, created_at
           FROM store_order_payment_proofs
           WHERE order_id=?
           ORDER BY id""",
        (order_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def count_proofs(conn: Any, order_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM store_order_payment_proofs WHERE order_id=?",
        (order_id,),
    ).fetchone()
    return int(row["n"]) if row else 0


def get_proof(conn: Any, order_id: int, proof_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT id, order_id, stored_name, original_name, mime_type, size_bytes
           FROM store_order_payment_proofs
           WHERE id=? AND order_id=?""",
        (proof_id, order_id),
    ).fetchone()
    return dict(row) if row else None


def is_image(mime_type: str) -> bool:
    return (mime_type or "").startswith("image/")

"""scripts.dailycheck_dashboard.issues_cache —— Issue 数据每日缓存（24h TTL）。

看板每 15 分钟渲染一次，但 Issue 列表没有这个更新频率需求（Owner 2026-10-30
拍板降频）。缓存口径：

  - 命中（`fetched_at` 距今 < TTL）→ 直接用缓存，**不发起 Gitea 调用**
  - 过期 / 缺失 → 拉 Gitea issues；成功则回写缓存
  - 拉取失败但缓存存在（哪怕过期）→ 用旧缓存，由调用方在页面标注「陈旧缓存」
    （遵循看板「拉不到 ≠ 没有」原则，不显示假零）
  - 拉取失败且无缓存 → 抛 FetchError，走调用方既有降级路径

缓存文件结构：`{"fetched_at": "<ISO8601>", "issues": [...]}`。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import fetchers

# Owner 2026-10-30 要求 Issue 数据降频：每天最多拉一次
ISSUES_CACHE_TTL_SECONDS = 24 * 3600

SOURCE_FRESH = "fresh"  # 本轮成功拉取并回写
SOURCE_CACHE = "cache"  # 命中新鲜缓存，未发网络请求
SOURCE_STALE = "stale"  # 拉取失败，回退旧缓存


def load_or_fetch_issues(
    cache_path: Path,
    *,
    ttl: float = ISSUES_CACHE_TTL_SECONDS,
) -> tuple[list[dict], dict]:
    """返回 `(issues, meta)`；meta 含 `source` / `fetched_at` / 可选 `cache_error`。

    无缓存且拉取失败时原样抛 `fetchers.FetchError`，不伪装成空列表。
    """
    cached = _read_cache(cache_path)
    if cached is not None and _is_fresh(cached["fetched_at"], ttl):
        return cached["issues"], {"source": SOURCE_CACHE, "fetched_at": cached["fetched_at"]}

    try:
        issues = fetchers.fetch_issues()
    except fetchers.FetchError:
        if cached is not None:
            return cached["issues"], {"source": SOURCE_STALE, "fetched_at": cached["fetched_at"]}
        raise

    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    meta: dict = {"source": SOURCE_FRESH, "fetched_at": fetched_at}
    try:
        _write_cache(cache_path, fetched_at, issues)
    except OSError as exc:
        # 缓存写不进去不影响本轮渲染；把事实交给调用方留痕，不静默吞掉。
        meta["cache_error"] = str(exc)
    return issues, meta


def _read_cache(cache_path: Path) -> dict | None:
    """读缓存；缺失/损坏/无有效时间戳 → None（当作没有缓存）。"""
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("issues"), list):
        return None
    fetched_at = payload.get("fetched_at")
    if not isinstance(fetched_at, str) or _parse_ts(fetched_at) is None:
        return None
    return {"fetched_at": fetched_at, "issues": payload["issues"]}


def _write_cache(cache_path: Path, fetched_at: str, issues: list[dict]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({"fetched_at": fetched_at, "issues": issues}, ensure_ascii=False),
        encoding="utf-8",
    )


def _is_fresh(fetched_at: str, ttl: float) -> bool:
    parsed = _parse_ts(fetched_at)
    if parsed is None:
        return False
    return (datetime.now(timezone.utc) - parsed).total_seconds() < ttl


def _parse_ts(value: str) -> datetime | None:
    """ISO 8601 → aware datetime；无时区标记按 UTC（与 fetchers._norm_ts 同口径）。"""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed

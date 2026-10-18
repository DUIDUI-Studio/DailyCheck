"""运维交付看板：托管渲染产物 latest.html（只读，不查库）。

产物由 `dc-dashboard-refresh.timer` 每 15 分钟重写一次（oneshot 渲染，
见 scripts/dailycheck_dashboard/render_ops.py），本路由只做「把文件吐出来」：
  - 不解析、不重排、不改数据 —— 渲染口径的唯一事实来源在 Python 侧
  - 不做进程内缓存：文件被 timer 覆盖后下次请求立刻读到新内容
  - 只注入一个 5 分钟自刷新的 meta，避免用户手动 F5
"""
from __future__ import annotations

import os
from pathlib import Path

from flask import Blueprint, Response

from permissions import require_platform_admin

bp = Blueprint("ops", __name__)

# 与 deploy/systemd/dc-dashboard-refresh.service 的 --html-latest 默认值一致。
# 环境变量仅供测试 / 排障改指，生产不设。
DEFAULT_PATH = "/var/lib/dailycheck-dashboard/latest.html"

# 文件每 15 分钟重写，浏览器 5 分钟自刷新一档即可，不必更密（纯静态无长连接）
AUTO_REFRESH_SEC = 300

_MISSING_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>运维看板 · 尚未生成</title>
<style>
  body{{margin:0;padding:32px 20px;background:#0f1115;color:#e6e8eb;
       font:15px/1.7 -apple-system,"PingFang SC",sans-serif}}
  .box{{max-width:640px;margin:0 auto}}
  h1{{font-size:20px;margin:0 0 12px}}
  p{{color:#9aa4b2;margin:8px 0}}
  code{{background:#1b1f27;padding:2px 6px;border-radius:4px;color:#e6e8eb}}
  .hint{{margin-top:20px;padding:14px 16px;border-left:3px solid #c9a227;
        background:#191d26;border-radius:0 6px 6px 0}}
</style></head><body><div class="box">
<h1>看板还没有生成</h1>
<p>找不到渲染产物：<code>{path}</code></p>
<div class="hint">
  <p>正常情况下它由 <code>dc-dashboard-refresh.timer</code> 每 15 分钟自动生成。</p>
  <p>排查命令：<code>systemctl status dc-dashboard-refresh.service</code></p>
  <p>手动补一次：<code>sudo systemctl start dc-dashboard-refresh.service</code></p>
</div>
</div></body></html>
"""


def dashboard_path() -> Path:
    return Path(os.environ.get("DAILYCHECK_OPS_DASHBOARD", DEFAULT_PATH))


def _inject_autorefresh(raw: str) -> str:
    """在首个 <head> 之后插入 meta refresh；无 <head> 时前置。"""
    marker = f'<meta http-equiv="refresh" content="{AUTO_REFRESH_SEC}">'
    idx = raw.lower().find("<head>")
    if idx < 0:
        return marker + raw
    at = idx + len("<head>")
    return raw[:at] + marker + raw[at:]


@bp.route("/ops")
@require_platform_admin
def ops_dashboard():
    path = dashboard_path()
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        raw = ""

    if not raw.strip():
        # 200 而不是 503：页面本身可达，缺的是内容；监控看的是路由存活，
        # 内容新鲜度由页面自身的渲染时间戳与操作符判断。
        return Response(
            _MISSING_HTML.format(path=path),
            mimetype="text/html",
            headers={"Cache-Control": "no-store"},
        )

    return Response(
        _inject_autorefresh(raw),
        mimetype="text/html",
        headers={"Cache-Control": "no-store"},
    )

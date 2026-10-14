#!/usr/bin/env python3
"""模拟用户操作 —— verify.sh check 2 的 4 关键路径探针（纯 stdlib，无浏览器依赖）.

路径（对绿环境 --base-url 依次验证）：
  1. 登录页可达          GET  /login                       → 200
  2. 登录成功            POST /login（含 CSRF 回填）        → 302 → 选仓页 200
  3. 选仓进入总览        POST /select-warehouse/<id>       → 302 → /land 200
  4. 库存页              GET  /inventory                   → 200

凭据：环境变量 SIM_USER_USER / SIM_USER_PASSWORD（缺失退出码 64 并提示，
绝不允许静默跳过——check 2 的意义就是把绿环境当真实用户走一遍）。
CSRF：自动从页面 <input name="csrf_token"> 抓取回填（页面无 token 则不带）。
会话：urllib + CookieJar，等同浏览器 cookie 行为。

退出码：0 全绿；1 任一路径失败；64 参数/凭据缺失。
"""
from __future__ import annotations

import argparse
import http.cookiejar
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

CSRF_RES = [
    re.compile(r'name="csrf_token"[^>]*value="([^"]+)"'),
    re.compile(r'value="([^"]+)"[^>]*name="csrf_token"'),
]
WAREHOUSE_ID_RE = re.compile(r"/select-warehouse/(\d+)")


class Probe:
    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/")
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar)
        )
        self.csrf = ""

    def get(self, path: str) -> tuple[int, str, str]:
        """GET，返回 (final_status, final_url, body)。跟随重定向。"""
        req = urllib.request.Request(self.base + path, method="GET")
        with self.opener.open(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, resp.geturl(), body

    def post(self, path: str, data: dict[str, str]) -> tuple[int, str, str]:
        """POST 表单（自动回填 CSRF），跟随重定向。"""
        payload = dict(data)
        if self.csrf:
            payload.setdefault("csrf_token", self.csrf)
        body_bytes = urllib.parse.urlencode(payload).encode()
        req = urllib.request.Request(
            self.base + path, data=body_bytes, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with self.opener.open(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, resp.geturl(), body


def main() -> int:
    ap = argparse.ArgumentParser(description="模拟用户 4 关键路径探针")
    ap.add_argument("--base-url", required=True, help="绿环境根 URL，如 http://127.0.0.1:8091")
    args = ap.parse_args()

    user = os.environ.get("SIM_USER_USER", "").strip()
    password = os.environ.get("SIM_USER_PASSWORD", "")
    if not user or not password:
        print(
            "❌ 缺凭据：export SIM_USER_USER=<用户名> SIM_USER_PASSWORD=<密码> 后重跑",
            file=sys.stderr,
        )
        return 64

    probe = Probe(args.base_url)
    failures: list[str] = []

    def record(num: int, name: str, ok: bool, detail: str = "") -> None:
        mark = "✅" if ok else "❌"
        extra = f" — {detail}" if detail else ""
        print(f"[sim-user] {mark} 路径{num} {name}{extra}")
        if not ok:
            failures.append(name)

    # ---- 路径 1：登录页可达 + 抓 CSRF ----
    try:
        status, _, body = probe.get("/login")
        ok = status == 200
        record(1, "登录页可达", ok, f"status={status}")
        if ok:
            for rx in CSRF_RES:
                m = rx.search(body)
                if m:
                    probe.csrf = m.group(1)
                    break
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        record(1, "登录页可达", False, str(e))

    # ---- 路径 2：登录成功（302 → 选仓页）----
    picker_html = ""
    try:
        status, final_url, body = probe.post(
            "/login", {"username": user, "password": password}
        )
        ok = status == 200 and "/select-warehouse" in final_url
        record(2, "登录成功", ok, f"final={final_url} status={status}")
        if ok:
            picker_html = body
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        record(2, "登录成功", False, str(e))

    # ---- 路径 3：选仓 → /land ----
    if picker_html:
        m = WAREHOUSE_ID_RE.search(picker_html)
        if not m:
            record(3, "选仓进入总览", False, "选仓页未解析到 /select-warehouse/<id>")
        else:
            # 选仓页也可能带 CSRF（每次渲染 token 可能刷新，重新抓）
            for rx in CSRF_RES:
                m2 = rx.search(picker_html)
                if m2:
                    probe.csrf = m2.group(1)
                    break
            wid = m.group(1)
            try:
                status, final_url, body = probe.post(f"/select-warehouse/{wid}", {})
                ok = status == 200 and final_url.endswith("/land")
                record(3, "选仓进入总览", ok, f"warehouse={wid} final={final_url}")
            except (urllib.error.HTTPError, urllib.error.URLError) as e:
                record(3, "选仓进入总览", False, str(e))
    else:
        record(3, "选仓进入总览", False, "登录未成功，跳过")

    # ---- 路径 4：库存页 ----
    try:
        status, final_url, _ = probe.get("/inventory")
        record(4, "库存页可达", status == 200, f"status={status}")
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        record(4, "库存页可达", False, str(e))

    if failures:
        print(f"[sim-user] ❌ 失败路径：{'、'.join(failures)}")
        return 1
    print("[sim-user] ✅ 4 关键路径全通")
    return 0


if __name__ == "__main__":
    sys.exit(main())

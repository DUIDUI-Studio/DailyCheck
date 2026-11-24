#!/usr/bin/env python3
"""模拟用户操作 —— verify.sh check 2 的 5 关键路径探针（使用 curl，无浏览器依赖）.

路径（对绿环境 --base-url 依次验证）：
  1. 登录页可达          GET  /login                       → 200
  2. 登录成功            POST /login（含 CSRF 回填）        → 302 → 选仓页 200
  3. 选仓进入总览        POST /select-warehouse/<id>       → 302 → /land 200
  4. 库存页              GET  /inventory                   → 200
  5. 库存健康报表        GET  /report/health               → 200

路径 5 是 Issue #38 补的回归网：该页曾因生产存量时间戳无秒（「YYYY-MM-DD HH:MM」）
在 manager+ 视角直接 500，而 check 2 只走 4 条路径、单测又全用带秒样例，
没有任何一层拦得住它。它要求**凭据账号为 manager+**（平台 admin 直接放行）：
403 会按失败处理而非跳过——静默降级等于没覆盖。

凭据：环境变量 SIM_USER_USER / SIM_USER_PASSWORD（缺失退出码 64 并提示，
绝不允许静默跳过——check 2 的意义就是把绿环境当真实用户走一遍）。
CSRF：自动从页面 <input name="csrf_token"> 抓取回填（页面无 token 则不带）。
会话：统一使用 curl 的 localhost 会话行为，仅访问独立绿组回环地址。

退出码：0 全绿；1 任一路径失败；64 参数/凭据缺失。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse

CSRF_RES = [
    re.compile(r'name="csrf_token"[^>]*value="([^"]+)"'),
    re.compile(r'value="([^"]+)"[^>]*name="csrf_token"'),
]
WAREHOUSE_ID_RE = re.compile(r"/select-warehouse/(\d+)")


class Probe:
    """One local green-service client, with a private curl session."""

    def __init__(self, base_url: str, directory: str) -> None:
        endpoint = urllib.parse.urlsplit(base_url)
        port = endpoint.port
        if (endpoint.scheme != "http" or endpoint.hostname not in {"localhost", "127.0.0.1"}
                or not port or endpoint.username is not None or endpoint.password is not None
                or endpoint.path not in {"", "/"} or endpoint.query or endpoint.fragment):
            raise ValueError("preview endpoint must be a local green service with an explicit port")
        self.base = f"http://localhost:{port}"
        self.origin = f"localhost:{port}"
        self.resolve = f"localhost:{port}:127.0.0.1"
        self.directory = directory
        self.csrf = ""

    def _request(self, path: str, data: dict[str, str] | None = None):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("preview request requires a root-relative application path")
        url = self.base + path
        body_file = os.path.join(self.directory, "body")
        jar = os.path.join(self.directory, "cookies")
        for _ in range(10):
            command = [
                "/usr/bin/curl", "--disable", "--silent", "--show-error", "--max-time", "15",
                "--ipv4", "--noproxy", "*", "--proto", "=http", "--globoff",
                "--resolve", self.resolve,
                "--cookie", jar, "--cookie-jar", jar, "--output", body_file,
                "--write-out", "%{http_code}\n%{redirect_url}", "--url", url,
            ]
            payload = None
            if data is not None:
                command += ["--header", "Content-Type: application/x-www-form-urlencoded",
                            "--data-binary", "@-"]
                payload = urllib.parse.urlencode(data)
            result = subprocess.run(command, input=payload, text=True, capture_output=True)
            if result.returncode:
                raise urllib.error.URLError("local preview request failed")
            status_text, redirect = result.stdout.split("\n", 1)
            status = int(status_text)
            if status in {301, 302, 303} and redirect:
                target = urllib.parse.urlsplit(redirect)
                if (target.scheme, target.netloc) != ("http", self.origin):
                    raise urllib.error.URLError("preview redirect left the local service")
                url, data = redirect, None
                continue
            if status >= 400:
                raise urllib.error.HTTPError(url, status, "preview HTTP failure", {}, None)
            with open(body_file, encoding="utf-8", errors="replace") as response:
                return status, url, response.read()
        raise urllib.error.URLError("too many preview redirects")

    def get(self, path: str):
        return self._request(path)

    def post(self, path: str, data: dict[str, str]):
        payload = dict(data)
        if self.csrf:
            payload.setdefault("csrf_token", self.csrf)
        return self._request(path, payload)


def main() -> int:
    ap = argparse.ArgumentParser(description="模拟用户 5 关键路径探针")
    ap.add_argument("--base-url", required=True, help="绿环境根 URL，如 http://localhost:8091")
    args = ap.parse_args()

    user = os.environ.get("SIM_USER_USER", "").strip()
    password = os.environ.get("SIM_USER_PASSWORD", "")
    if not user or not password:
        print(
            "❌ 缺凭据：export SIM_USER_USER=<用户名> SIM_USER_PASSWORD=<密码> 后重跑",
            file=sys.stderr,
        )
        return 64

    with tempfile.TemporaryDirectory(prefix="dc-sim-user-") as directory:
        try:
            probe = Probe(args.base_url, directory)
        except ValueError as error:
            ap.error(str(error))
        return run_paths(probe, user, password)


def run_paths(probe: Probe, user: str, password: str) -> int:
    failures: list[str] = []

    def record(num: int, name: str, ok: bool, detail: str = "") -> None:
        mark = "✅" if ok else "❌"
        extra = f" — {detail}" if detail else ""
        print(f"[sim-user] {mark} 路径{num} {name}{extra}")
        if not ok:
            failures.append(name)

    # ---- 路径 1：登录页可达 + 抓 CSRF ----
    try:
        status, final_url, body = probe.get("/login")
        ok = status == 200 and urllib.parse.urlsplit(final_url).path == "/login"
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
        ok = status == 200 and urllib.parse.urlsplit(final_url).path == "/select-warehouse"
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
                ok = status == 200 and urllib.parse.urlsplit(final_url).path == "/land"
                record(3, "选仓进入总览", ok, f"warehouse={wid} final={final_url}")
            except (urllib.error.HTTPError, urllib.error.URLError) as e:
                record(3, "选仓进入总览", False, str(e))
    else:
        record(3, "选仓进入总览", False, "登录未成功，跳过")

    # ---- 路径 4：库存页 ----
    try:
        status, final_url, _ = probe.get("/inventory")
        record(4, "库存页可达", status == 200 and urllib.parse.urlsplit(final_url).path == "/inventory",
               f"status={status} final={final_url}")
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        record(4, "库存页可达", False, str(e))

    # ---- 路径 5：库存健康报表（Issue #38 回归网，需 manager+）----
    try:
        status, final_url, _ = probe.get("/report/health")
        # 同时校验落点：会话一旦丢失会 302 到 /login 并被跟随后返回 200，
        # 只看 status 会把「已掉登录」误判成通过。
        ok = status == 200 and urllib.parse.urlsplit(final_url).path == "/report/health"
        record(5, "库存健康报表可达", ok, f"status={status} final={final_url}")
    except urllib.error.HTTPError as e:
        # 5xx 是本次要拦的故障；403 则是角色门（凭据账号非 manager+）——
        # 两者都算失败（不静默降级），但提示不同，便于一眼定性。
        hint = "（凭据账号需 manager+ 角色）" if e.code == 403 else ""
        record(5, "库存健康报表可达", False, f"status={e.code}{hint} {e.reason}")
    except urllib.error.URLError as e:
        record(5, "库存健康报表可达", False, str(e))

    if failures:
        print(f"[sim-user] ❌ 失败路径：{'、'.join(failures)}")
        return 1
    print("[sim-user] ✅ 5 关键路径全通")
    return 0


if __name__ == "__main__":
    sys.exit(main())

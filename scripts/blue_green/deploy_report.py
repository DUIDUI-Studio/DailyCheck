#!/usr/bin/env python3
"""deploy_report.py —— 把部署结果写回 PR（并可选推企微群）。

## 为什么需要它

`deploy_pipeline.sh` 的退出码 20~24 已经标了死在哪个阶段，但这份信息过去只
存在于**跑部署那个终端的滚动缓冲**里：窗口一关，证据就没了。于是任何后来者
（人或 agent）都无法回答「这个 PR 上次部署为什么红」，只能重新跑一遍碰运气。

本脚本在 `cleanup()`（`trap EXIT`）里被调用，把客观事实落到 PR 评论上。三个
刻意的设计约束：

1. **只记录事实，不做判断之外的推断**。归因走 `deploy_triage_pure`，它只认
   确定的东西；判不了的写 unknown，不猜。
2. **绝不建 issue**。部署事故单混进 issue 队列会被巡检 agent 当成需求认领，
   拿一个「nginx 配置漂了」去自动改代码，比没人管更坏。留档用 PR 评论（没人
   会抢），喊人用企微群（不会被认领），两个通道各干一件事。
3. **best-effort，永不抛错**。Gitea 挂了 / token 过期 / 网络不通都只写日志，
   因为这是部署收尾路径 —— 让「回写失败」把一次成功部署变成红，本末倒置。

## 机器可读契约

每条评论第一行是固定标记 `<!-- deploy-report:v1 -->`。消费端（人或 agent）
按它检索最新一条即可拿到阶段 / 归因 / 对齐结论，不需要解析自然语言。

## 用法

    deploy_report.py write  --pr 29 --sha <sha> --rc 21 --stage verify --log <file>
    deploy_report.py assess --rc 21 --stage verify --log <file> --pr-body <file>

`write` 会读 PR body 做对齐判定，再发评论；`assess` 只输出结论 JSON（供 agent
侧在动手前调用，避免各自实现一遍规则）。

## 环境变量

    DAILYCHECK_GITEA_TOKEN_FILE   默认 /etc/dailycheck/gitea-token
    DAILYCHECK_GITEA_TOKEN        显式 token（优先于文件）
    FEEDBACK_GITEA_API            默认 http://172.28.129.216:3000/api/v1
    FEEDBACK_GITEA_REPO           默认 eric/dailycheck
    DAILYCHECK_GITEA_WEB_BASE     给人点的链接基址
    WECOM_DEPLOY_WEBHOOK          部署失败推送；空 = 不推。回退 WECOM_FEEDBACK_WEBHOOK
    DAILYCHECK_DEPLOY_REPORT      0 = 整体关闭回写
    DAILYCHECK_CURL               curl 可执行文件（测试注入，默认 curl）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from deploy_triage_pure import (  # noqa: E402
    KIND_SUCCESS,
    VERDICT_ALIGNED,
    VERDICT_DRIFTED,
    VERDICT_UNVERIFIABLE,
    assess_alignment,
    triage,
)

MARKER = "<!-- deploy-report:v1 -->"
DEFAULT_TOKEN_FILE = "/etc/dailycheck/gitea-token"
DEFAULT_API = "http://172.28.129.216:3000/api/v1"
DEFAULT_REPO = "eric/dailycheck"
LOG_TAIL_LINES = 40
COMMENT_MAX = 50000

# 两种形态都要挡：``password=xxx`` / ``password: xxx`` 与 ``Bearer xxx`` /
# ``Authorization: token xxx``。只挡前一种会让 header 形态的凭据漏出去。
_REDACT = re.compile(
    r"(?i)((?:password|passwd|pass|secret|api[_-]?key)\s*[=:]\s*"
    r"|(?:bearer|token)\s+)"
    r"(\S+)"
)


# ── 配置 ──────────────────────────────────────────────────────────────────

def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or "").strip() or default


def _token() -> str:
    env_token = _env("DAILYCHECK_GITEA_TOKEN") or _env("FEEDBACK_GITEA_TOKEN")
    if env_token:
        return env_token
    path = Path(_env("DAILYCHECK_GITEA_TOKEN_FILE", DEFAULT_TOKEN_FILE))
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _api_base() -> str:
    return _env("FEEDBACK_GITEA_API", DEFAULT_API).rstrip("/")


def _repo() -> str:
    return _env("FEEDBACK_GITEA_REPO", DEFAULT_REPO)


def _web_base() -> str:
    return _env("DAILYCHECK_GITEA_WEB_BASE", "").rstrip("/")


# ── 事实采集 ──────────────────────────────────────────────────────────────

def redact(text: str) -> str:
    """抹掉日志里形如 ``password=xxx`` 的凭证。

    部署日志会带出 verify 探针凭据与各类 env，而 PR 评论对仓库内所有成员
    可见 —— 拿不准的时候多抹一次，代价只是可读性。
    """
    return _REDACT.sub(r"\1***", text or "")


def log_tail(path: str, lines: int = LOG_TAIL_LINES) -> str:
    p = Path(path) if path else None
    if p is None or not p.is_file():
        return ""
    try:
        raw = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return redact("\n".join(raw[-lines:]))


def _git(repo: str, *args: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True, text=True, timeout=20, check=False,
        )
        return out.stdout if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def changed_files(repo: str, sha: str, base: str = "") -> list[str]:
    """本 PR 改动过的文件。

    取**第一个父提交**到 sha 的 diff：部署的目标通常是 merge commit，那样得到
    的正好是整个 PR 的改动。fast-forward 部署时只能拿到最后一个 commit，改动
    偏少 —— 保守方向是安全的：命中越少，``assess_alignment`` 越倾向 unknown
    而非误判 aligned，不会让自动化改错地方。
    """
    rng = f"{base}..{sha}" if base else f"{sha}^1..{sha}"
    out = _git(repo, "diff", "--name-only", rng)
    files = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if not files and base:
        out = _git(repo, "diff", "--name-only", f"{sha}^1..{sha}")
        files = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return files


# ── HTTP（一律走 curl：urllib 发 POST/PATCH 到本 Gitea 恒返 502 空体）────────

def _curl(args: list[str], stdin_bytes: bytes | None = None) -> tuple[int, str]:
    curl = _env("DAILYCHECK_CURL", "curl")
    try:
        out = subprocess.run(
            [curl, *args],
            input=stdin_bytes, capture_output=True, timeout=25, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 0, ""
    return out.returncode, out.stdout.decode("utf-8", "replace")


def gitea(method: str, path: str, payload: dict[str, Any] | None = None) -> tuple[int, str]:
    """调 Gitea。返回 (HTTP 状态码, 响应体)。

    用临时文件 + ``--data-binary @file``（不是 ``@-``）：这是本仓在生产上验证
    过能用的形态，别改成管道传参。
    """
    token = _token()
    if not token:
        return 0, ""
    url = f"{_api_base()}{path}"
    args = [
        "-s", "-o", "-", "-w", "\n%{http_code}", "--max-time", "15",
        "-H", f"Authorization: token {token}",
        "-H", "Content-Type: application/json",
        "-X", method,
    ]
    tmp_path = None
    if payload is not None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        with tempfile.NamedTemporaryFile("wb", suffix=".json", delete=False) as fh:
            fh.write(raw)
            tmp_path = fh.name
        args += ["--data-binary", f"@{tmp_path}"]
    try:
        rc, out = _curl([*args, url])
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)
    if rc != 0:
        return 0, ""
    body, _, code = out.rpartition("\n")
    try:
        status = int(code.strip() or 0)
    except ValueError:
        status = 0
    return status, body


def fetch_pr_body(pr: int) -> tuple[str, str]:
    """返回 (PR body, 失败原因)。拿不到时 body 为空且原因非空。"""
    status, body = gitea("GET", f"/repos/{_repo()}/pulls/{pr}")
    if status != 200:
        why = "未配置 Gitea token" if status == 0 and not _token() else f"HTTP {status}"
        return "", why
    try:
        return str(json.loads(body).get("body") or ""), ""
    except (ValueError, AttributeError):
        return "", "响应不是合法 JSON"


def find_same_sha_comment(pr: int, sha: str) -> int:
    """找同一 sha 已有的部署评论 id（用于编辑而不是新增）。

    同一 sha 重跑一次就多一条评论，PR 很快就会变成刷屏的日志墙 —— 页面一刷屏
    就没人看了，等于回到「没有留档」。所以按 sha 幂等：同 sha 更新，不同 sha
    各留一条（多次部署的历史是需要的，那是「这次改动修好了没」的证据链）。
    """
    status, body = gitea("GET", f"/repos/{_repo()}/issues/{pr}/comments")
    if status != 200:
        return 0
    try:
        rows = json.loads(body)
    except ValueError:
        return 0
    if not isinstance(rows, list):
        return 0
    needle = f"`{sha[:12]}`"
    hit = 0
    for row in rows:
        text = str((row or {}).get("body") or "")
        if MARKER in text and needle in text:
            hit = int((row or {}).get("id") or 0)
    return hit


def push_wecom(text: str) -> bool:
    hook = _env("WECOM_DEPLOY_WEBHOOK") or _env("WECOM_FEEDBACK_WEBHOOK")
    if not hook:
        return False
    payload = {"msgtype": "markdown", "markdown": {"content": text[:4000]}}
    with tempfile.NamedTemporaryFile("wb", suffix=".json", delete=False) as fh:
        fh.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        tmp_path = fh.name
    try:
        rc, out = _curl(
            ["-s", "-o", "/dev/null", "--max-time", "10", "-H",
             "Content-Type: application/json", "--data-binary", f"@{tmp_path}", hook]
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    return rc == 0


# ── 渲染 ──────────────────────────────────────────────────────────────────

_VERDICT_LABEL = {
    VERDICT_ALIGNED: "契合 — 可继续",
    VERDICT_DRIFTED: "偏离 — 不要改代码",
    VERDICT_UNVERIFIABLE: "无法核对 — 先补 PR 目标",
    "unknown": "判不了 — 交语义层/人工",
}


def render(
    pr: int,
    sha: str,
    tr: dict[str, Any],
    align: dict[str, Any],
    tail: str,
    fetch_error: str = "",
) -> str:
    ok = tr.get("kind") == KIND_SUCCESS
    head = "部署结果 · 成功" if ok else f"部署结果 · 失败（rc={tr.get('rc')}）"
    verdict = str(align.get("verdict") or "unknown")
    lines = [
        MARKER,
        f"## {head}",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| PR | #{pr} |",
        f"| Commit | `{sha[:12]}` |",
        f"| 阶段 | `{tr.get('stage')}` |",
        f"| 归因 | `{tr.get('kind')}` / `{tr.get('scope')}` |",
        f"| 对齐 | `{verdict}` |",
        f"| 时间 | {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %z')} |",
        "",
        f"**原因**：{tr.get('reason', '')}",
        "",
    ]

    reqs = align.get("requirements") or []
    if fetch_error:
        lines += [f"**对齐判定**：跳过 — 读取 PR body 失败（{fetch_error}）", ""]
    else:
        lines.append(f"**对齐判定**：{_VERDICT_LABEL.get(verdict, verdict)}")
        if align.get("reason"):
            lines += ["", f"> {align['reason']}"]
        if align.get("action"):
            lines += ["", f"**下一步**：{align['action']}"]
        if reqs:
            lines += ["", "对照本次 PR 声明的目标（报告 1/4）：", ""]
            lines += [f"- `{r['id']}` {r['text'][:120]}" for r in reqs[:8]]
        lines.append("")

    if tail:
        # 再脱敏一次，不依赖调用方：render 可能被 agent 侧直接调用，而漏一次
        # 就是把凭据写进所有仓库成员可见的地方。
        lines += [
            "<details><summary>日志尾部（已脱敏）</summary>",
            "",
            "```",
            redact(tail),
            "```",
            "</details>",
            "",
        ]
    if _web_base():
        lines.append(f"[在 Gitea 查看 PR #{pr}]({_web_base()}/{_repo()}/pulls/{pr})")
    return "\n".join(lines)[:COMMENT_MAX]


def _wecom_text(pr: int, tr: dict[str, Any], align: dict[str, Any]) -> str:
    verdict = str(align.get("verdict") or "unknown")
    return "\n".join([
        f"**部署失败** · PR #{pr}",
        f"> 阶段：`{tr.get('stage')}`　归因：`{tr.get('kind')}`",
        f"> 对齐判定：`{verdict}`",
        f"> {tr.get('reason', '')}"[:600],
        "",
        f"下一步：{align.get('action') or '见 PR 评论'}"[:400],
    ])


# ── 动作 ──────────────────────────────────────────────────────────────────

def _assess(pr: int, repo: str, sha: str, rc: int, stage: str, tail: str,
            pr_body: str | None, base: str,
            files: list[str] | None = None) -> tuple[dict, dict, str]:
    tr = triage(rc, stage, tail)
    fetch_error = ""
    if pr_body is None:
        pr_body, fetch_error = fetch_pr_body(pr) if pr else ("", "未提供 PR 编号")
    if files is None:
        files = changed_files(repo, sha, base) if sha else []
    if fetch_error:
        align = {"verdict": "unknown", "reason": "", "action": "", "requirements": []}
    else:
        align = assess_alignment(tr, pr_body, files, tail)
    return tr, align, fetch_error


def cmd_write(args: argparse.Namespace) -> int:
    if _env("DAILYCHECK_DEPLOY_REPORT", "1") == "0":
        return 0
    if args.dry_run:
        return 0
    tail = log_tail(args.log)
    tr, align, fetch_error = _assess(
        args.pr, args.repo, args.sha, args.rc, args.stage, tail, None, args.base
    )
    body = render(args.pr, args.sha, tr, align, tail, fetch_error)
    existing = find_same_sha_comment(args.pr, args.sha) if args.sha else 0
    if existing:
        status, _ = gitea("PATCH", f"/repos/{_repo()}/issues/comments/{existing}",
                          {"body": body})
        verb = "更新"
    else:
        status, _ = gitea("POST", f"/repos/{_repo()}/issues/{args.pr}/comments",
                          {"body": body})
        verb = "写回"
    if status not in (200, 201):
        print(f"[deploy-report] WARN {verb} PR 失败 HTTP {status}", file=sys.stderr)
    else:
        print(f"[deploy-report] 已{verb} PR #{args.pr}", file=sys.stderr)
    if tr.get("kind") != KIND_SUCCESS:
        pushed = push_wecom(_wecom_text(args.pr, tr, align))
        if not pushed:
            print("[deploy-report] WARN 企微未推送（未配置或失败）", file=sys.stderr)
    return 0


def cmd_assess(args: argparse.Namespace) -> int:
    tail = log_tail(args.log)
    pr_body = Path(args.pr_body).read_text(encoding="utf-8") if args.pr_body else None
    files = None
    if args.changed_files:
        files = [
            ln.strip()
            for ln in Path(args.changed_files).read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
    tr, align, fetch_error = _assess(
        args.pr, args.repo, args.sha, args.rc, args.stage, tail, pr_body, args.base, files
    )
    print(json.dumps(
        {"triage": tr, "alignment": align, "fetch_error": fetch_error},
        ensure_ascii=False, indent=2,
    ))
    # 供 shell 消费：aligned 才能自动继续
    return 0 if align.get("verdict") == VERDICT_ALIGNED else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="部署结果写回 PR")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--pr", type=int, default=0)
        p.add_argument("--sha", default="")
        p.add_argument("--rc", type=int, default=1)
        p.add_argument("--stage", default="")
        p.add_argument("--log", default="")
        p.add_argument("--repo", default=os.environ.get("DAILYCHECK_PROD_REPO", "."))
        p.add_argument("--base", default="")

    w = sub.add_parser("write", help="写回 PR 评论（含失败推企微）")
    common(w)
    w.add_argument("--dry-run", action="store_true")
    w.set_defaults(func=cmd_write)

    a = sub.add_parser("assess", help="只输出对齐判定（供 agent 动手前调用）")
    common(a)
    a.add_argument("--pr-body", default="")
    a.add_argument("--changed-files", default="",
                   help="每行一个路径；缺省用 git diff 推断")
    a.set_defaults(func=cmd_assess)

    args = ap.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:  # noqa: BLE001 — 收尾路径绝不能让异常冒泡
        print(f"[deploy-report] WARN 内部异常已忽略: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

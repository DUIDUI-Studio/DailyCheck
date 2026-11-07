#!/usr/bin/env python3
"""DailyCheck open PR 兜底轮询 —— webhook 快通道的保底发现通道。

背景：快通道 = Gitea webhook → dc-webhook-receiver → inbox.jsonl →
dc-inbox-watcher → dc-notify-prod-agent → Hermes 订阅（≤5s 唤起判定）。
本脚本是「快通道整体故障时」的兜底，两个检查：

    检查 A「部署追平」：origin/main vs deployed.sha（单飞事实锚点）
    检查 B「open PR 清单」：Gitea REST

**2026-10-25 迁移**：检查 B 原来走 `gh pr list --repo ericmr1981/DailyCheck`。
GitHub 账号 10-21 起 suspended，这条检查此后只会每轮吐「gh 退出码 1」的假故障
（cron 也因此被 pause）。改走 Gitea REST，并把实现从 Hermes 的 scripts 目录
搬进本仓库 —— 逻辑可测、可回滚，Hermes 侧只留一个薄包装。

静默语义（skill `no-agent-cron-script-pattern`）：
  - 全部无事（已追平 + 无 open PR）→ 零 stdout（cron 收空 → 静默，无投递）
  - 有 open PR                     → stdout 输出 PR 清单（agent 按 skill 判定）
  - 有部署欠账（catchup rc 1）      → stdout 输出欠账块（走「部署单飞协议」补部署）
  - rc 2（同 sha 上次失败）         → stdout 输出告警块（不自动重试）
  - rc 65 / 查询失败 / 脚本异常     → stdout 输出 ⚠️ 告警（自身故障必须可见，不得静默）
  - rc 3（未初始化）                → 静默跳过（机制未激活 = 向后兼容窗口）

用法（测试）：
    DCPOLL_REPO=eric/dailycheck GITEA_API=... python3 pr_poll.py

环境变量：
    GITEA_API        默认 http://172.28.129.216:3000/api/v1
    GITEA_REPO       默认 eric/dailycheck（同时作为 watcher 的 repo 过滤器口径）
    GITEA_TOKEN_FILE 默认 /etc/dailycheck/gitea-token，回退 /root/.gitea-token
    DAILYCHECK_PROD_REPO 默认 /root/DailyCheck
"""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.hosting import HostingClient, HostingConfig, Provider, provider  # noqa: E402
from scripts.prod_agent.github_readiness import GitHubReadinessClient  # noqa: E402

CURL = os.environ.get("DAILYCHECK_CURL", "/usr/bin/curl")
TIMEOUT = 20
GITEA_API = os.environ.get("GITEA_API", "http://172.28.129.216:3000/api/v1").rstrip("/")
REPO = os.environ.get("DCPOLL_REPO") or os.environ.get("GITEA_REPO", "eric/dailycheck")
TOKEN_FILES = [
    os.environ.get("GITEA_TOKEN_FILE", ""),
    "/etc/dailycheck/gitea-token",
    "/root/.gitea-token",
]
# 部署追平检测（单飞机制）
PROD_REPO = os.environ.get("DAILYCHECK_PROD_REPO", "/root/DailyCheck")
DEPLOY_STATE = os.path.join(PROD_REPO, "scripts", "blue_green", "deploy_state.py")
PR_LIMIT = 20


def _alert(msg: str) -> str:
    return f"⚠️ DailyCheck PR 兜底轮询异常：{msg}"


def _token() -> str:
    for path in TOKEN_FILES:
        if not path:
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                tok = fh.read().strip()
            if tok:
                return tok
        except OSError:
            continue
    return ""


def _gitea_get(path: str, token: str) -> tuple[int, object]:
    """GET Gitea API。返回 (http_code, 解析后的 JSON 或原始文本)。

    用 curl 而不是 urllib：本仓既有实践（deploy_report.py）走 curl，且这套
    Gitea 对 Python 的 HTTP 客户端有过 502 空体的记录，不值得再踩一遍。
    """
    url = f"{GITEA_API}{path}"
    proc = subprocess.run(
        [CURL, "-s", "--max-time", str(TIMEOUT),
         "-H", f"Authorization: token {token}",
         "-w", "\n%{http_code}", url],
        capture_output=True, text=True, timeout=TIMEOUT + 10,
    )
    if proc.returncode != 0:
        raise OSError(f"curl rc={proc.returncode} {(proc.stderr or '').strip()[:120]}")
    raw, _, code_str = (proc.stdout or "").rpartition("\n")
    # 状态码先单独解析：响应体不是 JSON 时（curl 超时页、nginx 502 页、Gitea
    # 明文报错）不能连状态码一起丢掉 —— 告警里「HTTP 502」和「HTTP 0」的
    # 排查方向完全不同。
    try:
        code = int(code_str.strip() or 0)
    except ValueError:
        code = 0
    try:
        return code, json.loads(raw) if raw.strip() else None
    except json.JSONDecodeError:
        return code, raw


def _catchup_block() -> str:
    """检查 A：部署追平。返回输出块（空串 = 已追平/未激活，静默）。"""
    if provider() is Provider.GITHUB:
        return ""  # Shadow transport cannot inspect/mutate the production deployment lane.
    if os.environ.get("DCPOLL_REPO"):
        return ""  # 测试借用模式：仅验证 PR 输出格式
    if not os.path.exists(DEPLOY_STATE):
        return ""  # 单飞机制文件不在（旧版生产/已回滚）→ 未激活，静默
    # 先刷新 origin/main ref（失败不阻断：catchup 自带 no-main 兜底）
    try:
        subprocess.run(
            ["git", "-C", PROD_REPO, "fetch", "--quiet", "origin", "main"],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass
    try:
        proc = subprocess.run(
            ["/usr/bin/python3", DEPLOY_STATE, "catchup"],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return _alert(f"catchup 调用失败（{exc}）")
    rc = proc.returncode
    out = (proc.stdout or "").strip()
    if rc == 0:
        return ""  # 已追平 — 静默
    if rc == 3:
        return ""  # 未初始化 — 机制未激活（向后兼容窗口），静默
    if rc == 1:
        return (
            "========== 部署欠账（catchup rc=1） ==========\n"
            f"{out}\n"
            "按 dailycheck-prod-agent skill「部署单飞协议」补部署：先 acquire 抢锁，"
            "rc 10（他人在部署）→ 本轮跳过、不排队。\n"
        )
    # rc 2（同 sha 上次失败）/ rc 65（取不到 main）/ 其他
    return (
        f"⚠️ DailyCheck 部署追平检测异常（catchup rc={rc}）：\n{out}\n"
        "rc 2 = 同 sha 上次失败（不自动重试，需 owner）；rc 65 = 取不到 origin/main。\n"
    )


def _combined(sha: str, token: str) -> tuple[str, list[str]]:
    """单个 commit 的 CI 汇总状态。返回 (combined_state, 未成功的 context 列表)。"""
    try:
        code, data = _gitea_get(f"/repos/{REPO}/commits/{sha}/status", token)
    except OSError as exc:
        return f"error:{exc}", []
    if code != 200 or not isinstance(data, dict):
        return f"http{code}", []
    state = str(data.get("state") or "?")
    bad = []
    for st in data.get("statuses") or []:
        if not isinstance(st, dict):
            continue
        s = str(st.get("status") or "?")
        if s not in ("success", "pending"):
            bad.append(f"{st.get('context')}={s}")
    return state, bad


def _pr_block() -> str:
    """检查 B：open PR 清单。返回输出块（空串 = 无 PR，静默）。"""
    if provider() is Provider.GITHUB:
        return _github_pr_block()
    token = _token()
    if not token:
        return _alert(f"Gitea token 读不到（试过 {', '.join(p for p in TOKEN_FILES if p)}）")

    try:
        code, prs = _gitea_get(
            f"/repos/{REPO}/pulls?state=open&limit={PR_LIMIT}", token
        )
    except OSError as exc:
        return _alert(f"Gitea 查询失败（{exc}）")
    if code != 200:
        snippet = str(prs)[:160].replace("\n", " ")
        return _alert(f"Gitea /pulls HTTP {code}: {snippet}")
    if not isinstance(prs, list):
        return _alert(f"Gitea /pulls 返回非数组：{str(prs)[:120]}")

    if not prs:
        return ""  # 无 open PR — 静默（设计语义）

    lines = [f"📋 {REPO} open PR 共 {len(prs)} 个（兜底轮询）："]
    for pr in prs:
        if not isinstance(pr, dict):
            continue
        head = pr.get("head") or {}
        sha = str(head.get("sha") or "")
        branch = str(head.get("ref") or "")
        state, bad = _combined(sha, token) if sha else ("nosha", [])
        lines.append(
            f"- PR #{pr.get('number')}「{pr.get('title')}」 "
            f"branch={branch} head={sha[:8]} "
            f"mergeable={pr.get('mergeable')} draft={pr.get('draft')} "
            f"checks={state}" + (f"（未过：{', '.join(bad)}）" if bad else "")
        )
    lines.append(
        "请按 dailycheck-prod-agent skill 走 merge 判定：CI 两必需 check（test + "
        "agent-framework-reports）全绿才进队列；红线文件命中转 owner。无异议才 merge。"
    )
    return "\n".join(lines)


def _github_pr_block() -> str:
    client = HostingClient(HostingConfig.from_environment())
    client.verify_repository()
    prs = client.pages(f"/repos/{client.config.repo}/pulls", params={"state": "open"})
    if not prs:
        return ""
    lines = [f"{client.config.repo} open PRs (GitHub shadow-only):"]
    for pr in prs:
        number = pr.get("number")
        head = pr.get("head")
        if type(number) is not int or not isinstance(head, dict) or not isinstance(head.get("sha"), str):
            raise ValueError("invalid_pr_response")
        evidence = GitHubReadinessClient(client).check(number, head["sha"])
        lines.append(f"- PR #{number} {client.config.pr_url(number)} head={head['sha']} base={evidence.get('base_sha')} checks={evidence['reason']}")
    lines.append("Shadow evidence only; GitHub finalization is unsupported.")
    return "\n".join(lines)


def main() -> int:
    blocks = []
    for fn in (_catchup_block, _pr_block):
        try:
            block = fn()
            if block:
                blocks.append(block)
        except Exception as exc:  # 保底通道：任何异常都必须可见
            detail = str(exc) if os.environ.get("DAILYCHECK_HOSTING_PROVIDER", "gitea") == "gitea" else type(exc).__name__
            blocks.append(_alert(f"{fn.__name__} 未捕获异常（{detail}）"))
    if blocks:
        print("\n".join(blocks))
    return 0


if __name__ == "__main__":
    sys.exit(main())

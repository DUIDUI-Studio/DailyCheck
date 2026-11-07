#!/usr/bin/env python3
"""deploy_report.py —— 把部署结果写回 PR，并投进 agent 事件流。

## 为什么需要它

`deploy_pipeline.sh` 的退出码 20~24 已经标了死在哪个阶段，但这份信息过去只
存在于**跑部署那个终端的滚动缓冲**里：窗口一关，证据就没了。于是任何后来者
（人或 agent）都无法回答「这个 PR 上次部署为什么红」，只能重新跑一遍碰运气。

本脚本在 `cleanup()`（`trap EXIT`）里被调用，把客观事实落到两个通道上。四个
刻意的设计约束：

1. **只记录事实，不做判断之外的推断**。归因走 `deploy_triage_pure`，它只认
   确定的东西；判不了的写 unknown，不猜。
2. **绝不建 issue**。部署事故单混进 issue 队列会被巡检 agent 当成需求认领，
   拿一个「nginx 配置漂了」去自动改代码，比没人管更坏。留档用 PR 评论 ——
   没人会抢，且它天然挂在「这次改动」旁边。
3. **面向 agent，不面向人**。这条链路里人不是执行者，所以没有任何「通知谁
   来处理」的推送：consumer 是 agent 的**事件文件**（JSONL，append-only），
   agent 自己轮询/被唤醒，不需要有人被喊醒。人也拿不到一个「请去处理」的
   请求 —— 人该看的时候会去 PR 上看，那是留档，不是工单。
4. **best-effort，永不抛错**。Gitea 挂了 / token 过期 / 网络不通都只写日志，
   因为这是部署收尾路径 —— 让「回写失败」把一次成功部署变成红，本末倒置。

## 三条通道各管什么

- **PR 评论**：人可读的留档 + 事件文件丢了也能回读的持久化副本。首行固定
  标记 `<!-- deploy-report:v1 -->`。
- **事件文件**（`DAILYCHECK_DEPLOY_EVENT_LOG`，JSONL）：agent 的收件箱。
  每行一个自包含事件，带 `next` 字段（动作授权枚举）与 `report_comment`
  （回指 PR 评论 id），agent 不需要解析自然语言，也不需要去猜该干什么。
  同时它也是 `autofix` 熔断的计数来源 —— 状态不存在别处。
- **唤醒队列**（`DAILYCHECK_PROD_AGENT_INBOX`）：Hermes 的 `inbox.jsonl`。
  事件文件只解决「事实留下」，不解决「agent 什么时候知道」。这一条把它接到
  已有的 `inbox → watcher → webhook 订阅` 快通道上（≤5s 唤醒），否则 agent
  仍要等到下一轮兜底轮询才看得见。

## 用法

    deploy_report.py write  --pr 29 --sha <sha> --rc 21 --stage verify --log <file>
    deploy_report.py assess --rc 21 --stage verify --log <file> --pr-body <file>

`write` 会读 PR body 做对齐判定，再发评论 + 投事件；`assess` 只输出结论 JSON
（供 agent 侧在动手前调用，避免各自实现一遍规则）。

## 环境变量

    DAILYCHECK_GITEA_TOKEN_FILE   默认 /etc/dailycheck/gitea-token
    DAILYCHECK_GITEA_TOKEN        显式 token（优先于文件）
    FEEDBACK_GITEA_API            默认 http://172.28.129.216:3000/api/v1
    FEEDBACK_GITEA_REPO           默认 eric/dailycheck
    DAILYCHECK_GITEA_WEB_BASE     给人点的链接基址
    DAILYCHECK_DEPLOY_EVENT_LOG   默认 /var/lib/dailycheck-deploy/events.jsonl
    DAILYCHECK_PROD_AGENT_INBOX   默认 /var/lib/dailycheck-prod-agent/inbox.jsonl
    DAILYCHECK_DEPLOY_REPORT      0 = 整体关闭回写
    DAILYCHECK_CURL               curl 可执行文件（测试注入，默认 curl）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from deploy_triage_pure import (  # noqa: E402
    KIND_SUCCESS,
    VERDICT_ALIGNED,
    VERDICT_DRIFTED,
    VERDICT_UNVERIFIABLE,
    assess_alignment,
    next_action,
    triage,
)

from scripts.hosting import (  # noqa: E402
    AccessRole,
    HostingClient,
    HostingConfig,
    HostingError,
    Provider,
    parse_json_object,
    provider,
    require_shadow_path,
)

MARKER = "<!-- deploy-report:v1 -->"
DEFAULT_TOKEN_FILE = "/etc/dailycheck/gitea-token"
DEFAULT_API = "http://172.28.129.216:3000/api/v1"
DEFAULT_REPO = "eric/dailycheck"
DEFAULT_EVENT_LOG = "/var/lib/dailycheck-deploy/events.jsonl"
DEFAULT_PROD_AGENT_INBOX = "/var/lib/dailycheck-prod-agent/inbox.jsonl"
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
    if provider() is Provider.GITHUB:
        return HostingConfig.from_environment(AccessRole.REPORT).token
    env_token = _env("DAILYCHECK_GITEA_TOKEN") or _env("FEEDBACK_GITEA_TOKEN")
    if env_token:
        return env_token
    path = Path(_env("DAILYCHECK_GITEA_TOKEN_FILE", DEFAULT_TOKEN_FILE))
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _api_base() -> str:
    if provider() is Provider.GITHUB:
        return HostingConfig.from_environment().api_url
    return _env("FEEDBACK_GITEA_API", DEFAULT_API).rstrip("/")


def _repo() -> str:
    if provider() is Provider.GITHUB:
        return HostingConfig.from_environment().repo
    return _env("FEEDBACK_GITEA_REPO", DEFAULT_REPO)


def _web_base() -> str:
    if provider() is Provider.GITHUB:
        return HostingConfig.from_environment().web_url
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


def _is_ancestor(repo: str, ancestor: str, descendant: str) -> bool:
    try:
        out = subprocess.run(
            ["git", "-C", repo, "merge-base", "--is-ancestor", ancestor, descendant],
            capture_output=True, text=True, timeout=20, check=False,
        )
        return out.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def changed_files(repo: str, sha: str, base: str = "") -> list[str]:
    """本 PR 改动过的文件。

    deploy-first 下部署目标是 PR head（尚未合入 main），``sha^1..sha`` 只能看到
    最后一个 commit —— 用 ``merge-base(origin/main, sha)..sha`` 才覆盖整个 PR。
    已合入 main 的 merge commit 仍走 ``sha^1..sha``（第一个父提交即合并前 main）。
    两个 base 都取不到时退回旧行为，保守方向是安全的：命中越少，
    ``assess_alignment`` 越倾向 unknown 而非误判 aligned。
    """
    if base:
        rng = f"{base}..{sha}"
    elif _is_ancestor(repo, sha, "origin/main"):
        rng = f"{sha}^1..{sha}"
    else:
        merge_base = _git(repo, "merge-base", "origin/main", sha).strip()
        rng = f"{merge_base}..{sha}" if merge_base else f"{sha}^1..{sha}"
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
    if provider() is Provider.GITHUB:
        if method != "GET":
            raise HostingError("trusted_publication_claim_required")
        role = AccessRole.READ if method == "GET" else AccessRole.REPORT
        client = HostingClient(HostingConfig.from_environment(role))
        try:
            client.verify_repository()
            return 201 if method == "POST" else 200, json.dumps(client.request(method, path, payload)[0])
        except HostingError as exc:
            return exc.status, ""
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
    if provider() is Provider.GITHUB:
        raise HostingError("sha_only_publication_identity_forbidden")
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


def event_log_path() -> Path:
    return Path(_env("DAILYCHECK_DEPLOY_EVENT_LOG", DEFAULT_EVENT_LOG))


def read_events(path: Path | None = None) -> list[dict[str, Any]]:
    """读事件流。坏行直接跳过 —— 一行写了一半（进程被杀）不该让整条链失效。"""
    p = path or event_log_path()
    if not p.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                out.append(row)
    except OSError:
        return []
    return out


def autofix_count(pr: int, path: Path | None = None) -> int:
    """同一 PR 已发生过的 autofix 次数，作为熔断计数。

    **按不同 sha 去重**，不按事件条数。同一个 sha 部署失败后重跑是常态（改
    nginx、放锁、重启实例后再来一次），那些重跑并没有「修」任何东西，把它们
    算进轮次会让熔断在第二轮就误触发 —— 该继续修的时候停下来重审目标，比
    修不动了还硬修更浪费时间。

    从事件流里数而不是别处存状态：**事件本身就是状态**。多存一份计数器就多
    一个会和事实脱节的东西，而 deploy 收尾路径最不需要的就是第二个真相源。
    """
    shas: set[str] = set()
    for e in read_events(path):
        if int(e.get("pr") or 0) != int(pr) or e.get("next") != "autofix":
            continue
        shas.add(str(e.get("sha") or ""))
    return len(shas)


def append_event(event: dict[str, Any]) -> bool:
    """把事件追加到 agent 收件箱。失败只返回 False，绝不抛错。

    用单次 ``write`` + ``O_APPEND``：并发部署（不同 PR）可能同时写同一个文件，
    append 是原子的（PIPE_BUF 内），不会互相截断。不做锁 —— 锁在部署收尾
    路径上是自找的阻塞源。
    """
    event = {k: redact(v) if isinstance(v, str) else v for k, v in event.items()}
    p = event_log_path()
    if provider() is Provider.GITHUB:
        try:
            require_shadow_path(p)
        except HostingError:
            print("[deploy-report] GitHub event log requires shadow isolation", file=sys.stderr)
            return False
        event = {**event, "hosting_provider": "github", "intake_mode": "shadow", "repo_full_name": _repo()}
    line = json.dumps(event, ensure_ascii=False) + "\n"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(line)
        return True
    except OSError as exc:
        print(f"[deploy-report] WARN 事件写入失败（{p}）：{exc}", file=sys.stderr)
        return False


def inbox_path() -> Path:
    return Path(_env("DAILYCHECK_PROD_AGENT_INBOX", DEFAULT_PROD_AGENT_INBOX))


def push_inbox_event(
    pr: int,
    sha: str,
    tr: dict[str, Any],
    align: dict[str, Any],
    nxt: str,
    comment_id: int = 0,
    log_path: str = "",
) -> bool:
    """把部署结果投进 prod-agent 的唤醒队列（Hermes ``inbox.jsonl``）。

    **为什么不只写 events.jsonl**：文件写了没人读，等于没写。Hermes 侧已经有
    ``inbox.jsonl → dc-inbox-watcher → dc-notify-prod-agent → webhook 订阅``
    这条现成链路（≤5s 唤醒 agent），复用它的唤醒机制比新增常驻服务或 cron 便宜
    得多，也不会多一处需要跟着 PR 节奏走的配置。

    **为什么字段全部平铺**：订阅 prompt 只做 ``{key}`` 字面替换，嵌套对象取不到。
    把 ``next`` / ``verdict`` / ``log`` 摊到顶层，prompt 才能直接引用。

    **为什么成功也投**：agent 需要知道 ``deployed`` 锚点已推进。不投的话，兜底
    轮询的 catchup 会对着旧锚点反复判 behind，白白多跑一轮部署。
    """
    now_ms = int(datetime.now().timestamp() * 1000)
    web = _web_base()
    repo = _repo()
    payload = {
        "delivery_id": f"deploy-{pr}-{(sha or 'nosha')[:12]}-{tr.get('rc')}",
        "event": "deploy_result",
        # 复用 completed：dc-inbox-watcher 的 --actions 白名单里已有它，为一个新
        # action 去改 systemd unit 不划算；「部署结束」本身也是 completed 语义。
        # 真正的分流在 dc-notify-prod-agent，它按 event 字段路由，不看 action。
        "action": "completed",
        "received_at_ms": now_ms,
        "repo_full_name": repo,
        "pr_number": int(pr),
        "pr_head_sha": sha,
        "pr_url": f"{web}/{repo}/{'pull' if provider() is Provider.GITHUB else 'pulls'}/{pr}" if web else "",
        "pr_base_ref": "main",
        "deploy_rc": int(tr.get("rc") or 0),
        "deploy_stage": str(tr.get("stage") or ""),
        "deploy_kind": str(tr.get("kind") or ""),
        "deploy_scope": str(tr.get("scope") or ""),
        "deploy_verdict": str(align.get("verdict") or ""),
        "deploy_next": nxt,
        "deploy_reason": redact(str(tr.get("reason") or ""))[:400],
        "deploy_report_comment": int(comment_id or 0),
        "deploy_log": log_path,
    }
    p = inbox_path()
    if provider() is Provider.GITHUB:
        payload.update(hosting_provider="github", intake_mode="shadow")
        try:
            require_shadow_path(p)
        except HostingError:
            print("[deploy-report] GitHub production intake unsupported", file=sys.stderr)
            return False
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(line)
        return True
    except OSError as exc:
        print(f"[deploy-report] WARN inbox 投递失败（{p}）：{exc}", file=sys.stderr)
        return False


# ── 渲染 ──────────────────────────────────────────────────────────────────

_VERDICT_LABEL = {
    VERDICT_ALIGNED: "契合 — 可继续",
    VERDICT_DRIFTED: "偏离 — 不要改代码",
    VERDICT_UNVERIFIABLE: "无法核对 — 先补 PR 目标",
    "unknown": "判不了 — 交语义层",
}

# 给 agent 的动作枚举，落到 PR 评论上一行。人是可以顺手看一眼的，但这一行
# 的收件人不是人 —— 写出来是为了让「这次事故该谁动手」在评论里也是确定的。
_NEXT_LABEL = {
    "none": "`none` — 部署成功，无需动作",
    "autofix": "`autofix` — 在本 PR 范围内改代码修复后重新部署",
    "envfix": "`envfix` — 修环境/流程后重跑，**不要改本次改动的代码**",
    "derive": "`derive` — 先推导并补写 PR 目标，再重跑 assess 重判",
    "audit": "`audit` — 调用方参数错了，修调用姿势",
    "halt": "`halt` — 熔断，停止自动重试，回去重审目标本身",
}


def render_private(
    pr: int,
    sha: str,
    tr: dict[str, Any],
    align: dict[str, Any],
    tail: str,
    fetch_error: str = "",
    nxt: str = "",
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
        f"| 下一步 | {_NEXT_LABEL.get(nxt, nxt or '—')} |",
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
        noun = "pull" if provider() is Provider.GITHUB else "pulls"
        host = "GitHub" if provider() is Provider.GITHUB else "Gitea"
        lines.append(f"[在 {host} 查看 PR #{pr}]({_web_base()}/{_repo()}/{noun}/{pr})")
    return redact("\n".join(lines)[:COMMENT_MAX])


def render(*args, **kwargs) -> str:
    if provider() is Provider.GITHUB:
        raise HostingError("typed_public_deploy_facts_required")
    return render_private(*args, **kwargs)


@dataclass(frozen=True)
class PublicDeployFacts:
    pr: int
    sha: str
    receipt: str
    outcome_at: str
    rc: int
    stage: str
    kind: str
    scope: str
    verdict: str
    next: str

    def __post_init__(self):
        enums = {
            "stage": {"acquire", "candidate", "up", "verify", "promote", "switch",
                      "observe", "merge", "release", "complete"},
            "kind": {"success", "usage", "lock", "code", "drift", "env", "unknown"},
            "scope": {"in-scope", "out-of-scope", "undetermined"},
            "verdict": {"aligned", "drifted", "unverifiable", "unknown"},
            "next": {"none", "autofix", "envfix", "derive", "audit", "halt"},
        }
        if type(self.pr) is not int or self.pr <= 0 or type(self.rc) is not int or not 0 <= self.rc <= 255:
            raise HostingError("invalid_public_deploy_number")
        if not isinstance(self.sha, str) or not re.fullmatch(r"[0-9a-f]{40}", self.sha):
            raise HostingError("invalid_public_deploy_sha")
        try:
            if str(uuid.UUID(self.receipt)) != self.receipt:
                raise ValueError
            datetime.strptime(self.outcome_at, "%Y-%m-%dT%H:%M:%SZ")
            if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", self.outcome_at):
                raise ValueError
        except (ValueError, TypeError, AttributeError) as exc:
            raise HostingError("invalid_public_deploy_receipt_time") from exc
        if any(not isinstance(getattr(self, k), str) or getattr(self, k) not in v
               for k, v in enums.items()):
            raise HostingError("invalid_public_deploy_enum")
        if (self.rc == 0) != (self.kind == "success"):
            raise HostingError("inconsistent_public_deploy_result")


def publication_marker(facts: PublicDeployFacts) -> str:
    return (f"<!-- deploy-publication:v1 repo=1448986421 pr={facts.pr} "
            f"head={facts.sha} receipt={facts.receipt} -->")


def render_public(facts: PublicDeployFacts) -> str:
    """No requirement, explanation, log, path, planner or private identity parameter."""
    head = "部署结果 · 成功" if facts.rc == 0 else f"部署结果 · 失败（rc={facts.rc}）"
    return "\n".join([
        MARKER, publication_marker(facts), f"## {head}", "", "| 项 | 值 |", "|---|---|",
        f"| PR | #{facts.pr} |", f"| Commit | `{facts.sha[:12]}` |",
        f"| RC | {facts.rc} |", f"| 阶段 | `{facts.stage}` |",
        f"| 归因 | `{facts.kind}` / `{facts.scope}` |", f"| 对齐 | `{facts.verdict}` |",
        f"| 下一步 | {_NEXT_LABEL[facts.next]} |", f"| 时间 | {facts.outcome_at} |", "",
        "Diagnostics are retained privately.", "",
        f"[在 GitHub 查看 PR #{facts.pr}](https://github.com/DUIDUI-Studio/DailyCheck/pull/{facts.pr})",
    ])


@dataclass(frozen=True)
class PublicationContext:
    """PRIVATE input ABI. Its JSON describes facts; it cannot grant authority."""

    provider: str
    host: str
    repository_id: int
    repository: str
    pr: int
    sha: str
    attempt_id: str
    generation: int
    outcome_at: str
    public_receipt: str
    private_source: tuple[str, str, int, str, str, int]
    payload_sha256: str

    @classmethod
    def parse(cls, raw: object) -> PublicationContext:
        try:
            if not isinstance(raw, dict):
                raise ValueError
            value = dict(raw)
            source = value["private_source"]
            keys = {"provider", "host", "repository_id", "repository", "kind", "number"}
            if not isinstance(source, dict) or set(source) != keys:
                raise ValueError
            value["private_source"] = tuple(source[k] for k in (
                "provider", "host", "repository_id", "repository", "kind", "number"))
            context = cls(**value)
            if (context.provider, context.host, context.repository_id, context.repository) != (
                "github", "github.com", 1448986421, "DUIDUI-Studio/DailyCheck"
            ) or type(context.repository_id) is not int:
                raise ValueError
            if (not isinstance(context.attempt_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", context.attempt_id)
                or context.attempt_id == context.public_receipt
                or type(context.generation) is not int or context.generation < 1
                or not isinstance(context.payload_sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", context.payload_sha256)):
                raise ValueError
            sp, host, rid, repo, kind, number = context.private_source
            if (sp not in {"gitea", "github"} or kind != "issue"
                or type(rid) is not int or rid <= 0 or type(number) is not int or number <= 0
                or not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9.:-]+", host)
                or not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)):
                raise ValueError
            if sp == "github" and (host, rid, repo) != (
                "github.com", 1449125557, "DUIDUI-Studio/DailyCheck-ops"
            ):
                raise ValueError
            return context
        except (ValueError, TypeError, KeyError) as exc:
            raise HostingError("invalid_private_publication_context") from exc


class PublicationClaim(Protocol):
    """Adapter owns durable per-sink state and holds fencing through HTTP.

    check_current rejects superseded attempts (including newer success). intent
    durably records uncertainty BEFORE a POST; bind is a fenced CAS, never an
    authoritative deployment receipt. Only ready claims can make a first POST.
    """

    state: str
    comment_id: int

    def check_current(self) -> None: ...
    def intent(self) -> None: ...
    def bind(self, comment_id: int) -> None: ...


class PublicationAuthority(Protocol):
    def claim(self, context: PublicationContext, payload_sha256: str
              ) -> AbstractContextManager[PublicationClaim]: ...


def _payload_digest(body: str) -> str:
    return hashlib.sha256(json.dumps({"body": body}, ensure_ascii=False,
                                     sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def publish_public(facts: PublicDeployFacts, context: PublicationContext,
                   *, authority: PublicationAuthority | None = None) -> int:
    """Requires a trusted adapter; a context JSON alone never grants authority."""
    body = render_public(facts)
    digest = _payload_digest(body)
    if authority is None:
        raise HostingError("trusted_fencing_adapter_not_wired")
    if (context.pr, context.sha, context.public_receipt, context.outcome_at,
        context.payload_sha256) != (facts.pr, facts.sha, facts.receipt, facts.outcome_at, digest):
        raise HostingError("public_publication_payload_mismatch")
    client = HostingClient(HostingConfig.from_environment(AccessRole.REPORT))
    if client.config.provider is not Provider.GITHUB:
        raise HostingError("public_github_destination_required")
    root = f"/repos/{client.config.repo}"
    with authority.claim(context, digest) as claim:
        claim.check_current()
        guard_fd = getattr(claim, "transport_guard_fd", None)
        metadata = client.verify_repository()
        if metadata.get("private") is not False or metadata.get("visibility") != "public":
            raise HostingError("public_report_destination_mismatch")
        pr = client.get(root + f"/pulls/{facts.pr}")
        if (not isinstance(pr, dict) or pr.get("number") != facts.pr
            or pr.get("html_url") != client.config.pr_url(facts.pr)
            or pr.get("head", {}).get("sha") != facts.sha
            or pr.get("base", {}).get("repo", {}).get("id") != 1448986421):
            raise HostingError("public_report_pr_mismatch")
        rows = client.pages(root + f"/issues/{facts.pr}/comments")
        marker = publication_marker(facts)
        matches = [row for row in rows if marker in str(row.get("body") or "")]
        if len(matches) > 1:
            raise HostingError("public_report_ambiguous")

        def confirm(row):
            if (not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] <= 0
                or row.get("body") != body
                or row.get("issue_url") != client.config.api_url + root + f"/issues/{facts.pr}"):
                raise HostingError("public_report_confirmation_mismatch")
            return row["id"]

        if matches:
            ident = confirm(matches[0])
            if claim.comment_id and claim.comment_id != ident:
                raise HostingError("public_report_mapping_mismatch")
        else:
            if claim.state != "ready" or claim.comment_id:
                raise HostingError("public_report_response_uncertain")
            claim.check_current()
            claim.intent()
            claim.check_current()
            ident = confirm(client.request("POST", root + f"/issues/{facts.pr}/comments",
                                           {"body": body}, guard_fd=guard_fd)[0])
        claim.check_current()
        claim.bind(ident)
        return ident


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


def _write_private_diagnostic(private_dir: Path, body: str) -> None:
    private_file = private_dir / f"{uuid.uuid4()}.md"
    fd = os.open(private_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(body)


def _public_stage(tr: dict, rc: int) -> str:
    stage = tr["stage"]
    if stage == "verify-drift":
        return "verify"
    if stage == "unknown" and rc == 0:
        return "complete"
    return stage


def _publication_context(outcome: dict, binding, payload_sha256: str) -> PublicationContext:
    from publication_authority import SOURCE_KEYS

    hosting = outcome["hosting"]
    return PublicationContext.parse({
        "provider": hosting["provider"], "host": hosting["host"],
        "repository_id": hosting["repository_id"], "repository": hosting["repository"],
        "pr": outcome["pr"], "sha": outcome["sha"],
        "attempt_id": outcome["attempt_id"], "generation": outcome["generation"],
        "outcome_at": outcome["outcome_at"],
        "public_receipt": outcome["public_receipt"],
        "private_source": dict(zip(SOURCE_KEYS, binding.source, strict=True)),
        "payload_sha256": payload_sha256,
    })


def _cmd_write_github(args, tail: str, authority):
    """Real GitHub path: durable outcome + binding -> the existing publish_public."""
    import deploy_state
    import publication_authority

    private_dir = event_log_path().parent / "reports"
    require_shadow_path(private_dir)
    private_dir.mkdir(parents=True, exist_ok=True)

    outcome_info = None
    resolve_error = None
    if authority is None:
        try:
            outcome_info = publication_authority.load_durable_outcome(
                deploy_state.state_dir(), args.pr, args.sha)
        except (HostingError, OSError, ValueError, TypeError) as exc:
            resolve_error = exc

    outcome = outcome_info[1] if outcome_info else None
    tr, align, fetch_error = _assess(
        args.pr, args.repo, args.sha,
        outcome["rc"] if outcome else args.rc,
        outcome["stage"] if outcome else args.stage,
        tail, None, args.base)
    nxt = next_action(tr, align, autofix_count(args.pr) if args.pr else 0)
    private_body = render_private(args.pr, args.sha, tr, align, tail, fetch_error, nxt)
    if outcome_info:
        private_body += (
            f"\n\n**Durable outcome**：`{outcome_info[0]}` generation={outcome_info[2]} "
            f"verification=`{outcome.get('verification', 'normal')}` "
            f"receipt=`{outcome['public_receipt']}`\n")
    _write_private_diagnostic(private_dir, private_body)

    comment_id = 0
    write_rc = 0
    try:
        if authority is not None:
            # Trusted code injection retained for the staged ABI and its tests;
            # the CLI never supplies one.
            path = getattr(args, "publication_context", "")
            if not path:
                raise HostingError("private_publication_context_required")
            context = PublicationContext.parse(parse_json_object(Path(path).read_bytes()))
            facts = PublicDeployFacts(
                args.pr, args.sha, context.public_receipt, context.outcome_at,
                tr["rc"], _public_stage(tr, args.rc), tr["kind"], tr["scope"],
                align["verdict"], nxt)
            comment_id = publish_public(facts, context, authority=authority)
        else:
            if resolve_error is not None:
                raise resolve_error
            attempt_id, outcome, generation = outcome_info
            if args.rc != outcome["rc"]:
                raise HostingError("publication_result_mismatch")
            facts = PublicDeployFacts(
                args.pr, args.sha, outcome["public_receipt"], outcome["outcome_at"],
                outcome["rc"], _public_stage(tr, outcome["rc"]), tr["kind"],
                tr["scope"], align["verdict"], nxt)
            frozen, digest = publication_authority.freeze_public_facts(
                deploy_state.state_dir(), attempt_id, generation,
                asdict(facts), _payload_digest(render_public(facts)))
            facts = PublicDeployFacts(**frozen)
            binding = publication_authority.resolve_source_binding(
                deploy_state.state_dir(), args.pr)
            context = _publication_context(outcome, binding, digest)
            adapter = publication_authority.DurablePublicationAuthority(
                state_dir=deploy_state.state_dir(), outcome_id=attempt_id,
                generation=generation, context=context,
                payload_sha256=digest, binding_sha256=binding.binding_sha256)
            comment_id = publish_public(facts, context, authority=adapter)
    except (HostingError, OSError, ValueError, TypeError) as exc:
        print(f"[deploy-report] public publication blocked: {type(exc).__name__}",
              file=sys.stderr)
        write_rc = 1
    return comment_id, tr, align, fetch_error, nxt, write_rc


def cmd_write(args: argparse.Namespace, *, authority: PublicationAuthority | None = None) -> int:
    if _env("DAILYCHECK_DEPLOY_REPORT", "1") == "0":
        return 0
    if args.dry_run:
        return 0
    tail = log_tail(args.log)
    comment_id = 0
    write_rc = 0
    if provider() is Provider.GITHUB:
        try:
            require_shadow_path(event_log_path())
            require_shadow_path(inbox_path())
        except HostingError:
            print("[deploy-report] GitHub requires isolated shadow report paths", file=sys.stderr)
            return 1
        comment_id, tr, align, fetch_error, nxt, write_rc = _cmd_write_github(
            args, tail, authority)
    else:
        tr, align, fetch_error = _assess(
            args.pr, args.repo, args.sha, args.rc, args.stage, tail, None, args.base)
        nxt = next_action(tr, align, autofix_count(args.pr) if args.pr else 0)
        body = render(args.pr, args.sha, tr, align, tail, fetch_error, nxt)
        try:
            existing = find_same_sha_comment(args.pr, args.sha) if args.sha else 0
        except HostingError:
            print("[deploy-report] hosting comment lookup failed", file=sys.stderr)
            return 1
        if existing:
            status, resp = gitea("PATCH", f"/repos/{_repo()}/issues/comments/{existing}",
                                 {"body": body})
            verb, comment_id = "更新", existing
        else:
            status, resp = gitea("POST", f"/repos/{_repo()}/issues/{args.pr}/comments", {"body": body})
            verb = "写回"
            try:
                comment_id = int(json.loads(resp).get("id") or 0)
            except (ValueError, AttributeError):
                comment_id = 0
        if status not in (200, 201):
            print(f"[deploy-report] WARN {verb} PR 失败 HTTP {status}", file=sys.stderr)
        else:
            print(f"[deploy-report] 已{verb} PR #{args.pr}", file=sys.stderr)

    # 评论写失败也照样投事件：事件是 agent 唯一的入口，Gitea 抖动不该让一次
    # 部署失败凭空消失。agent 拿到 next 后能自己回拉 PR 状态补齐上下文。
    appended = append_event({
        "v": 1,
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "pr": int(args.pr or 0),
        "sha": args.sha,
        "rc": int(args.rc),
        "stage": tr.get("stage", ""),
        "kind": tr.get("kind", ""),
        "scope": tr.get("scope", ""),
        "verdict": align.get("verdict", ""),
        "next": nxt,
        "reason": tr.get("reason", ""),
        "action": align.get("action", ""),
        "report_comment": comment_id,
        "log": str(Path(args.log).resolve()) if args.log else "",
    })
    if appended:
        print(f"[deploy-report] 事件已投递 next={nxt}", file=sys.stderr)

    # 唤醒队列与审计流是两个通道，各自独立失败：
    # events 保事实，inbox 负责「叫醒」。inbox 写不进去时 agent 仍可在下一轮
    # 兜底轮询里从 events / PR 评论补上，反之亦然。
    pushed = push_inbox_event(
        args.pr, args.sha, tr, align, nxt, comment_id,
        str(Path(args.log).resolve()) if args.log else "",
    )
    if pushed:
        print(f"[deploy-report] 已唤醒 prod-agent next={nxt}", file=sys.stderr)
    return write_rc


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
    nxt = next_action(tr, align, autofix_count(args.pr) if args.pr else 0)
    print(json.dumps(
        {"triage": tr, "alignment": align, "next": nxt, "fetch_error": fetch_error},
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

    w = sub.add_parser("write", help="写回 PR 评论并投递 agent 事件")
    common(w)
    w.add_argument("--dry-run", action="store_true")
    w.add_argument("--publication-context", default="",
                   help="Only for trusted injected callers; the CLI builds the "
                        "durable outcome/binding context itself and ignores this file")
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
        print(f"[deploy-report] WARN 内部异常: {type(exc).__name__}",
              file=sys.stderr)
        return 1 if provider() is Provider.GITHUB else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Issue 精确化：把 ``page_path`` 反查成代码位置，供派发时的 Issue 正文使用。

为什么需要这一层：用户写「导出按钮没反应」，管理员写「确认是按钮问题，修一下」，
这两句都没法直接变成任务。但用户点「反馈」时 URL 已经自动带上了**当时所在的
页面**，而「页面 → 视图函数 → 源文件行号 → 模板 → 最近的改动」全都可以机器算
出来。一段行号比三百字描述更能让开发侧一次做对。

⚠️ ``page_path`` 的语义边界（Issue #93 实测踩到）：
它是「**点反馈那一刻所在的页面**」，不是「出问题的页面」。两者通常一致，
在以下情况必定不一致，此时给线索等于给错答案 —— 比没有线索更坏，因为开发侧
会照着改错误的文件：

- **纯中转页**（登录 / 登出 / 仓库选择）── 用户只是路过，页面上没有任何业务
  内容。这类页面按 endpoint 前缀（``auth.``）判定并直接拒绝出线索。
- 用户在 A 页遇到问题，切到 B 页（比如去查个数据）后才想起来提反馈。

所以正文侧一律以「提交时所在页面」字样呈现，并保留「以现象为准」的兜底；
管理员也可在派发前人工覆盖实际页面（见 ``feedback.dispatch_feedback``）。

设计约定：
- **绝不抛异常**。探测是锦上添花，任何一步失败只是少一段线索，不能阻断
  管理员派发 —— 一张任务单丢掉，代价远大于少一行行号。
- **只读**。只用 Flask 的 ``view_functions`` 反射和一次 ``git log``，
  不 import 蓝图模块、不碰业务库、不写文件。
- **不阻塞太久**。``git`` 调用带 3 秒超时，整个探测在毫秒级完成。
- 输出面向开发 agent 而不是用户：宁可写路径和行号，不写解释。

Spec: docs/ARCHITECTURE.md §3.10（反馈闭环 / Issue 精确化）。
"""
from __future__ import annotations

import inspect
import re
import subprocess
from pathlib import Path
from typing import Any

import config

# git 是外部进程：宁可放弃最近改动，也不能让派发按钮转圈几秒没反应。
GIT_TIMEOUT_SECONDS = 3.0
MAX_COMMITS = 5
# 模板行里取一个够用的展示宽度，避免把整条长路径塞进正文。
MAX_SUBJECT_LEN = 100

# 纯中转页：``auth`` 蓝图下只有 login / logout / select-warehouse 四个路由，
# 全部不含业务内容。按 endpoint 前缀判定而不是硬编码路径 —— 改路由时黑名单
# 不会静默失效。见 Issue #93：用户在仓库选择页顺手提了反馈，自动定位把
# ``blueprints/auth.py`` 当成"出问题的代码"，而真凶在
# ``templates/store_ordering/dc_items.html``。
TRANSIENT_ENDPOINT_PREFIX = "auth."
# collect() 的 matched=False 分两种，文案和可信度都不同：
REASON_NO_PATH = "no_path"                # 提交时就没记路径
REASON_UNMATCHED = "unmatched"            # 有路径但没匹配到路由
REASON_TRANSIENT = "transient_page"       # 匹配到了，但是纯中转页

_TEMPLATE_RE = re.compile(r"""render_template\(\s*["']([^"']+\.html)["']""")


# ---------------------------------------------------------------------------
# 探测步骤（每一步都 fail-soft）
# ---------------------------------------------------------------------------

def clean_path(page_path: Any) -> str:
    """只留下 path 部分：``/inventory?x=1#top`` → ``/inventory``。

    非站内路径（外部 URL / 协议相对 / javascript:）一律返回空 —— 这个字段
    会进 Issue 正文，保持「只可能是站内相对路径」这条既有约束。
    """
    text = str(page_path or "").strip()
    if not text.startswith("/") or text.startswith("//"):
        return ""
    for sep in ("?", "#"):
        idx = text.find(sep)
        if idx >= 0:
            text = text[:idx]
    return text if text.startswith("/") else ""


def match_endpoint(app: Any, page_path: Any) -> tuple[str, str]:
    """``/inventory`` → ``("items.inventory_view", "/inventory")``。

    只用 GET：用户是从页面上点的「反馈」，他停留在 GET 页面。
    匹配失败（404 / 只接受 POST / 参数不合法）返回两个空串。
    """
    path = clean_path(page_path)
    if not path or app is None:
        return "", ""
    try:
        # bind 到任意 host 即可：本站路由不按 host 分流。
        adapter = app.url_map.bind("localhost")
        endpoint, _ = adapter.match(path, method="GET")
    except Exception:  # noqa: BLE001 - 404/405/Redirect 都算探测失败
        return "", ""
    endpoint = str(endpoint or "")
    if not endpoint or endpoint == "static" or endpoint.endswith(".static"):
        return "", ""
    return endpoint, path


def locate_view(app: Any, endpoint: str) -> dict[str, Any]:
    """endpoint → 源文件、起始行号、内部调用的模板名。

    ``inspect.unwrap`` 是必须的：项目的权限装饰器都用了 ``functools.wraps``，
    不解包的话拿到的是 ``permissions.py`` 里的 wrapper，行号毫无意义。
    """
    if app is None or not endpoint:
        return {}
    view = app.view_functions.get(endpoint)
    if view is None:
        return {}
    try:
        target = inspect.unwrap(view)
        source_file = inspect.getsourcefile(target) or ""
        source_lines, lineno = inspect.getsourcelines(target)
    except (OSError, TypeError):
        return {}
    if not source_file:
        return {}
    return {
        "source": _relative(source_file, getattr(config, "BASE_DIR", "")),
        "line": int(lineno),
        "template": _find_template(source_lines),
        "func": str(getattr(target, "__name__", "") or ""),
    }


def _find_template(source_lines: list[str]) -> str:
    """在视图函数体里找第一个 ``render_template("x.html")`` 的字面量。

    找不到不算错：很多视图直接返回 ``jsonify`` 或重定向，那本来就没有模板。
    """
    for line in source_lines or []:
        match = _TEMPLATE_RE.search(line)
        if match:
            return match.group(1)
    return ""


def _relative(path: Any, base_dir: Any) -> str:
    """绝对路径 → 仓库内相对路径。超出仓库或解析失败时原样返回。"""
    text = str(path or "").strip()
    base = str(base_dir or "").strip()
    if not text or not base:
        return text
    try:
        return str(Path(text).resolve().relative_to(Path(base).resolve()))
    except (ValueError, OSError):
        return text


def recent_commits(
    repo_root: Any,
    rel_paths: list[str],
    limit: int = MAX_COMMITS,
) -> list[dict[str, str]]:
    """取这些文件最近的提交，让开发侧一眼看到「谁刚动过这里」。"""
    paths = [p for p in (rel_paths or []) if str(p or "").strip()]
    if not paths or not str(repo_root or "").strip():
        return []
    cmd = [
        "git", "-C", str(repo_root), "log",
        f"-{max(1, int(limit))}",
        "--format=%h\t%ad\t%s",
        "--date=short",
        "--",
    ] + paths
    try:
        done = subprocess.run(  # noqa: S603 - 固定 argv，无 shell，无用户输入拼接
            cmd,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if done.returncode != 0:
        return []
    commits: list[dict[str, str]] = []
    for raw in (done.stdout or "").splitlines():
        parts = raw.split("\t", 2)
        if len(parts) < 3:
            continue
        commits.append({
            "sha": parts[0].strip(),
            "date": parts[1].strip(),
            "subject": parts[2].strip()[:MAX_SUBJECT_LEN],
        })
    return commits


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def collect(
    app: Any,
    page_path: Any,
    base_dir: Any = None,
    with_commits: bool = True,
) -> dict[str, Any]:
    """一次探测，返回可直接交给 ``feedback_dispatch`` 渲染的字典。

    返回结构（``matched=False`` 时带 ``page_path`` + ``reason``）::

        {
          "matched": True,
          "page_path": "/inventory",
          "rule": "/inventory",
          "endpoint": "items.inventory_view",
          "func": "inventory",
          "source": "blueprints/items.py",
          "line": 120,
          "template": "inventory.html",
          "commits": [{"sha": "3d9f2a1", "date": "2026-10-15", "subject": "..."}],
        }

    ``matched=False`` 时 ``reason`` 取 ``no_path`` / ``unmatched`` /
    ``transient_page`` 之一 —— 渲染层据此给不同文案，因为三种「没线索」的
    可信度完全不同（中转页是**已知不可信**，不是**未知**）。

    ``with_commits=False`` 供单元测试用，避免测试依赖宿主仓库的 git 历史。
    """
    path = clean_path(page_path)
    if not path:
        return {"matched": False, "page_path": "", "reason": REASON_NO_PATH}
    endpoint, rule = match_endpoint(app, page_path)
    if not endpoint:
        return {"matched": False, "page_path": path, "reason": REASON_UNMATCHED}
    if endpoint.startswith(TRANSIENT_ENDPOINT_PREFIX):
        # 中转页一律拒绝出线索：这里的文件路径（auth.py）与用户描述的问题
        # 毫无关系，写进正文就是误导。见 Issue #93。
        return {
            "matched": False,
            "page_path": path,
            "reason": REASON_TRANSIENT,
            "transient_endpoint": endpoint,
        }

    info = locate_view(app, endpoint)
    source = str(info.get("source") or "")
    template = str(info.get("template") or "")
    template_path = f"templates/{template}" if template else ""

    clues: dict[str, Any] = {
        "matched": True,
        "page_path": path,
        "rule": rule,
        "endpoint": endpoint,
        "func": info.get("func") or "",
        "source": source,
        "line": info.get("line") or 0,
        "template": template,
        "commits": [],
    }
    if with_commits:
        base = base_dir if base_dir is not None else getattr(config, "BASE_DIR", "")
        clues["commits"] = recent_commits(base, [source, template_path])
    return clues

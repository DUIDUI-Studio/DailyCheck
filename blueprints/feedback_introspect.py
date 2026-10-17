"""Issue 精确化：把 ``page_path`` 反查成代码位置，供派发时的 Issue 正文使用。

为什么需要这一层：用户写「导出按钮没反应」，管理员写「确认是按钮问题，修一下」，
这两句都没法直接变成任务。但用户点「反馈」时 URL 已经自动带上了出问题的页面，
而「页面 → 视图函数 → 源文件行号 → 模板 → 最近的改动」全都可以机器算出来。
一段行号比三百字描述更能让开发侧一次做对。

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

    返回结构（``matched=False`` 时只带 ``page_path``）::

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

    ``with_commits=False`` 供单元测试用，避免测试依赖宿主仓库的 git 历史。
    """
    path = clean_path(page_path)
    if not path:
        return {"matched": False, "page_path": ""}
    endpoint, rule = match_endpoint(app, page_path)
    if not endpoint:
        return {"matched": False, "page_path": path}

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

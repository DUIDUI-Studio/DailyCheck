#!/usr/bin/env python3
"""从源码派生架构事实图 → docs/ARCHITECTURE_MAP.md

用法：
    python3 scripts/arch_map.py --write    # 重写地图
    python3 scripts/arch_map.py --check    # CI 校验：与提交版本不一致 → exit 1

设计约束
--------
- **纯 stdlib**、不连数据库、不 import Flask app（秒级完成，CI 可跑）
- **确定性**：输出不含时间戳 / 绝对路径 / 行号，同一份源码必得同一份字节
- 权威性分工：

  | 事实 | 来源 | 权威性 |
  |------|------|--------|
  | 路由集合 + 鉴权意图 | `permissions_manifest.ROUTE_MATRIX`（运行时导入） | 权威（`tests/test_route_permission_matrix.py` 双向校验 == `app.url_map`） |
  | URL 规则 | `blueprints/*.py` 静态解析 | best-effort，未解析到标 `?` |
  | 数据表 | `db/*.py` 的 `CREATE TABLE` | 权威 |
  | MCP 工具域 | `mcp_server/protocol/tools/*.py` | 权威 |
  | 蓝图装配 | `app.py` 的 `register_blueprint` | 权威 |

- **不收测试文件清单**：测试增删不改变结构，收进来会让地图每周都动
  （实测近 30 天 130 个提交里：结构真变 21 个，仅新增测试 16 个）

维护约定：本文件由脚本生成，**禁止手工编辑**。要改内容就改脚本。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "ARCHITECTURE_MAP.md"

AUTH_LABEL = {
    "public": "公开",
    "login": "登录",
    "platform": "平台超管",
}
PREFIX_LABEL = (
    ("perm:", "权限点"),
    ("any:", "任一权限"),
    ("role:", "角色"),
    ("dc>", "配送中心"),
)


def _auth_label(intent: str) -> str:
    if intent in AUTH_LABEL:
        return AUTH_LABEL[intent]
    for prefix, label in PREFIX_LABEL:
        if intent.startswith(prefix):
            return label
    return intent


def _auth_detail(intent: str) -> str:
    for prefix in ("perm:", "any:", "role:", "dc>"):
        if intent.startswith(prefix):
            return intent[len(prefix):]
    return ""


# --------------------------------------------------------------------------
# 蓝图与路由（静态解析）
# --------------------------------------------------------------------------

def parse_blueprint_file(path: Path) -> dict:
    """解析单个蓝图文件：蓝图变量 → 注册名，以及 (var, rule, methods, func)。"""
    src = path.read_text(encoding="utf-8", errors="replace")
    bpvar: dict[str, str] = {}
    for m in re.finditer(r"^(\w+)\s*=\s*Blueprint\(\s*['\"]([^'\"]+)['\"]", src, re.M):
        bpvar[m.group(1)] = m.group(2)

    routes: list[tuple[str, str, list[str], str]] = []
    for m in re.finditer(r"@(\w+)\.route\(", src):
        var = m.group(1)
        start = m.end() - 1  # 指向 '('
        depth = 0
        end = -1
        for j in range(start, len(src)):
            ch = src[j]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    end = j
                    break
        if end < 0:
            continue
        args = src[start + 1:end]
        rm = re.match(r"\s*(['\"])(.*?)\1", args, re.S)
        if not rm:
            continue
        rule = rm.group(2)
        mm = re.search(r"methods\s*=\s*\[([^\]]*)\]", args)
        if mm:
            methods = [x.strip().strip("'\"") for x in mm.group(1).split(",") if x.strip()]
        else:
            methods = ["GET"]
        fm = re.search(
            r"^\s*(?:@[^\n]*\n\s*)*(?:async\s+)?def\s+(\w+)\s*\(",
            src[end + 1:end + 900],
            re.M,
        )
        func = fm.group(1) if fm else "?"
        routes.append((var, rule, sorted(methods), func))
    return {"bpvar": bpvar, "routes": routes}


def collect_blueprints() -> list[dict]:
    out = []
    for f in sorted((ROOT / "blueprints").glob("*.py")):
        info = parse_blueprint_file(f)
        if not info["bpvar"]:
            continue  # 纯逻辑模块（*_pure.py / _helpers.py）不是蓝图
        out.append(
            {
                "file": f.name,
                "names": info["bpvar"],
                "routes": info["routes"],
            }
        )
    return out


# --------------------------------------------------------------------------
# 权限矩阵（运行时权威）
# --------------------------------------------------------------------------

def load_route_matrix() -> dict[str, str]:
    sys.path.insert(0, str(ROOT))
    try:
        import permissions_manifest as pm  # noqa: PLC0415
    finally:
        sys.path.pop(0)
    return dict(pm.ROUTE_MATRIX)


# --------------------------------------------------------------------------
# 数据层
# --------------------------------------------------------------------------

def collect_tables() -> tuple[int | None, list[str]]:
    version = None
    tables: set[str] = set()
    for f in sorted((ROOT / "db").glob("*.py")):
        src = f.read_text(encoding="utf-8", errors="replace")
        vm = re.search(r"WAREHOUSE_SCHEMA_VERSION\s*=\s*(\d+)", src)
        if vm:
            version = int(vm.group(1))
        for t in re.finditer(
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"'`]?(\w+)[\"'`]?\s*[(\n]",
            src,
            re.I,
        ):
            tables.add(t.group(1))
    return version, sorted(tables)


# --------------------------------------------------------------------------
# MCP 工具域
# --------------------------------------------------------------------------

def collect_mcp() -> list[tuple[str, int]]:
    d = ROOT / "mcp_server" / "protocol" / "tools"
    if not d.exists():
        return []
    out = []
    for f in sorted(d.glob("*.py")):
        if f.name == "__init__.py":
            continue
        src = f.read_text(encoding="utf-8", errors="replace")
        fns = re.findall(r"^(?:async\s+)?def\s+([a-z]\w*)\s*\(", src, re.M)
        out.append((f.stem, len(fns)))
    return out


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------

def render() -> str:
    bps = collect_blueprints()
    matrix = load_route_matrix()
    version, tables = collect_tables()
    mcp = collect_mcp()

    # endpoint -> 静态解析到的规则
    rule_of: dict[str, tuple[str, str]] = {}
    bp_rows: list[dict] = []
    for b in bps:
        # 一个文件可能定义多个蓝图（auth.py → auth_bp + pwa_bp）
        bp_name = " / ".join(sorted(b["names"].values()))
        # 装配判据：该蓝图的路由是否出现在权限矩阵里。
        # 矩阵由 tests/test_route_permission_matrix.py 双向校验 == app.url_map，
        # 所以「在矩阵里」等价于「真装配进了 app」。比匹配 register_blueprint
        # 变量名可靠（蓝图变量在文件里常叫 bp，在 app.py 里被重命名导入）。
        mounted = any(
            f"{b['names'].get(var, '')}.{func}" in matrix
            for var, _rule, _methods, func in b["routes"]
        )
        b["mounted"] = mounted
        b["bp_name"] = bp_name
        for var, rule, methods, func in b["routes"]:
            ep = f"{b['names'].get(var, bp_name)}.{func}"
            rule_of[ep] = (rule, ",".join(methods))
        bp_rows.append(b)

    known = set(matrix)
    unregistered = sorted(set(rule_of) - known)
    unresolved = sorted(known - set(rule_of))

    stat: dict[str, int] = {}
    for intent in matrix.values():
        k = _auth_label(intent)
        stat[k] = stat.get(k, 0) + 1

    L: list[str] = []
    A = L.append
    A("# DailyCheck 架构事实图")
    A("")
    A("> 本文件由 `scripts/arch_map.py` **自动生成，禁止手工编辑**。")
    A("> 一致性由 `tests/test_arch_map.py` 在 CI 的 `test` job 强制：与源码不符即失败。")
    A("> 要更新请运行 `python3 scripts/arch_map.py --write`。")
    A("")
    A("**怎么用这张表**")
    A("")
    A("1. 想知道「这个 URL 要什么权限」→ 查 §2，按蓝图分组，`鉴权` 列是唯一事实来源。")
    A("2. 想知道「系统有哪些模块、谁在服役」→ 查 §1 与 §5（未装配 = 死代码）。")
    A("3. 想知道「最近架构变了什么」→ `git log -p -- docs/ARCHITECTURE_MAP.md`。")
    A("")
    A("## 0. 一览")
    A("")
    mounted_n = sum(1 for b in bp_rows if b["mounted"])
    A(f"- 蓝图 {len(bp_rows)} 个（已装配 {mounted_n} / 未装配 {len(bp_rows) - mounted_n}）")
    A(f"- 路由 **{len(matrix)}** 条（按鉴权强度：" + " · ".join(
        f"{k} {v}" for k, v in sorted(stat.items(), key=lambda x: -x[1])) + "）")
    A(f"- 数据表 **{len(tables)}** 张，`WAREHOUSE_SCHEMA_VERSION = {version}`")
    A(f"- MCP 工具域 {len(mcp)} 个")
    if unregistered:
        A(f"- ⚠️ 静态解析到但矩阵未登记的路由 {len(unregistered)} 条（见 §5）")
    A("")

    A("## 1. 蓝图装配")
    A("")
    A("| 蓝图 | 文件 | 路由数 | 装配 |")
    A("|---|---|---|---|")
    for b in sorted(bp_rows, key=lambda x: (-len(x["routes"]), x["bp_name"])):
        state = "在役" if b["mounted"] else "**未装配**"
        A(f"| `{b['bp_name']}` | `{b['file']}` | {len(b['routes'])} | {state} |")
    A("")

    A("## 2. 路由 × 鉴权（唯一事实来源：`permissions_manifest.ROUTE_MATRIX`）")
    A("")
    grouped: dict[str, list[tuple[str, str]]] = {}
    for ep, intent in matrix.items():
        grouped.setdefault(ep.split(".")[0], []).append((ep, intent))
    for bp in sorted(grouped):
        A(f"### `{bp}`")
        A("")
        A("| endpoint | 方法 | 规则 | 鉴权 | 明细 |")
        A("|---|---|---|---|---|")
        for ep, intent in sorted(grouped[bp]):
            rule, methods = rule_of.get(ep, ("?", "?"))
            A(f"| `{ep}` | {methods} | `{rule}` | {_auth_label(intent)} | {_auth_detail(intent)} |")
        A("")

    A("## 3. 数据层")
    A("")
    A(f"- `WAREHOUSE_SCHEMA_VERSION = {version}`（改 ALTER 块须 bump，见 ARCHITECTURE.md §4）")
    A(f"- 表 {len(tables)} 张：")
    A("")
    for i in range(0, len(tables), 4):
        A("- " + " · ".join(f"`{t}`" for t in tables[i:i + 4]))
    A("")

    A("## 4. MCP 工具域")
    A("")
    A("| 域 | 导出函数数 |")
    A("|---|---|")
    for name, n in mcp:
        A(f"| `{name}` | {n} |")
    A("")

    A("## 5. 未装配 / 未登记（死代码与漏网）")
    A("")
    dead_bps = {b["bp_name"] for b in bp_rows if not b["mounted"]}
    dead_eps = [ep for ep in unregistered if ep.split(".")[0] in dead_bps]
    other_eps = [ep for ep in unregistered if ep not in dead_eps]
    if not unregistered and not unresolved:
        A("无。静态解析与权限矩阵完全对齐。")
    else:
        if dead_eps:
            A(f"**未装配蓝图的路由**（所属蓝图未注册到 app，访问即 404，属死代码；"
              f"当前：{', '.join('`' + n + '`' for n in sorted(dead_bps))}）")
            A("")
            for ep in dead_eps:
                rule, methods = rule_of[ep]
                A(f"- `{ep}` ← `{rule}` ({methods})")
            A("")
        if other_eps:
            A("**其他未登记路由**（蓝图在役，但新路由没写进 `ROUTE_MATRIX` —— 权限漏网，必须补）")
            A("")
            for ep in other_eps:
                rule, methods = rule_of[ep]
                A(f"- `{ep}` ← `{rule}` ({methods})")
            A("")
        if unresolved:
            A("**矩阵已登记、代码中未解析到 URL 规则**（应用级路由或动态注册，非缺陷）")
            A("")
            for ep in unresolved:
                A(f"- `{ep}`")
            A("")
    A("---")
    A("")
    A("生成方式：`scripts/arch_map.py`（纯 stdlib、不连库、不 import app）。")
    A("数据来源与权威性说明见脚本头部 docstring。")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--write", action="store_true", help="重写 docs/ARCHITECTURE_MAP.md")
    g.add_argument("--check", action="store_true", help="与提交版本比对，不一致则退出 1")
    ap.add_argument("--out", type=Path, default=OUT, help="输出路径（默认 docs/ARCHITECTURE_MAP.md）")
    args = ap.parse_args()

    text = render()
    if args.write:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"written: {args.out.relative_to(ROOT) if args.out.is_relative_to(ROOT) else args.out}")
        return 0

    if not args.out.exists():
        print(f"FAIL: {args.out} 不存在。运行 python3 scripts/arch_map.py --write")
        return 1
    current = args.out.read_text(encoding="utf-8")
    if current == text:
        print(f"OK: {args.out.name} 与源码一致")
        return 0
    print("FAIL: 架构事实图与源码不一致。")
    print("      运行 python3 scripts/arch_map.py --write 后提交该文件。")
    import difflib

    diff = list(
        difflib.unified_diff(
            current.splitlines(),
            text.splitlines(),
            fromfile="committed",
            tofile="generated",
            lineterm="",
            n=1,
        )
    )
    print("\n".join(diff[:60]))
    if len(diff) > 60:
        print(f"... 另有 {len(diff) - 60} 行差异")
    return 1


if __name__ == "__main__":
    sys.exit(main())

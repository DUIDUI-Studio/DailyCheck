#!/usr/bin/env python3
"""架构变更简报 —— 对比两个时间点的 docs/ARCHITECTURE_MAP.md，输出人读摘要。

用法：
    python3 scripts/arch_digest.py                 # 默认近 7 天
    python3 scripts/arch_digest.py --since "30 days ago"
    python3 scripts/arch_digest.py --base <sha>    # 指定基线提交

为什么比结构而不是比文本
------------------------
地图是派生文件，文本 diff 会把「表格重排」也算成变更。本脚本解析地图的
结构化事实（蓝图装配 / 路由×鉴权 / 数据表 / MCP 域）后求差集，只报**语义变化**。

只读 git 与工作区文件，不写任何东西（要落盘请重定向）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAP_REL = "docs/ARCHITECTURE_MAP.md"
MAP = ROOT / MAP_REL


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True
    ).stdout


# --------------------------------------------------------------------------
# 解析地图结构
# --------------------------------------------------------------------------

def parse_map(text: str) -> dict:
    out: dict = {"blueprints": {}, "routes": {}, "tables": set(), "mcp": {}}
    section = ""
    for line in text.splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        if section.startswith("1.") and line.startswith("| `"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) >= 4:
                name = cells[0].strip("`")
                out["blueprints"][name] = "未装配" not in cells[3]
        elif section.startswith("2.") and line.startswith("| `"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) >= 5:
                ep = cells[0].strip("`")
                out["routes"][ep] = (cells[1], cells[2].strip("`"), cells[3], cells[4])
        elif section.startswith("3.") and line.startswith("- `"):
            for t in re.findall(r"`([^`]+)`", line):
                if t and not t.startswith("WAREHOUSE"):
                    out["tables"].add(t)
        elif section.startswith("4.") and line.startswith("| `"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) >= 2:
                out["mcp"][cells[0].strip("`")] = cells[1]
    return out


# --------------------------------------------------------------------------
# 取基线
# --------------------------------------------------------------------------

def resolve_base(since: str, base: str | None) -> tuple[str | None, str]:
    if base:
        return base, git("log", "-1", "--format=%ad", "--date=short", base).strip()
    cutoff = git("log", "-1", "--format=%cI", f"--before={since}", "--", MAP_REL).strip()
    sha = git("log", "-1", "--format=%H", f"--before={since}", "--", MAP_REL).strip()
    if not sha:
        sha = git("rev-list", "--max-parents=0", "HEAD").split()[0]
    return sha or None, cutoff[:10] if cutoff else "(初始)"


def compute_delta(old: dict, new: dict) -> dict:
    """比较两份已解析的地图，返回语义差异（不含渲染）。"""
    add_routes = sorted(set(new["routes"]) - set(old["routes"]))
    del_routes = sorted(set(old["routes"]) - set(new["routes"]))
    chg_auth = []
    for ep in sorted(set(old["routes"]) & set(new["routes"])):
        o, n = old["routes"][ep], new["routes"][ep]
        if o[2:] != n[2:]:
            chg_auth.append((ep, o[2], n[2]))
        elif o[:2] != n[:2]:
            chg_auth.append((ep, f"{o[0]} `{o[1]}`", f"{n[0]} `{n[1]}`"))
    return {
        "add_routes": add_routes,
        "del_routes": del_routes,
        "chg_auth": chg_auth,
        "add_tbl": sorted(new["tables"] - old["tables"]),
        "del_tbl": sorted(old["tables"] - new["tables"]),
        "add_bp": sorted(set(new["blueprints"]) - set(old["blueprints"])),
        "del_bp": sorted(set(old["blueprints"]) - set(new["blueprints"])),
        "bp_state": [
            (name, old["blueprints"].get(name), new["blueprints"][name])
            for name in sorted(new["blueprints"])
            if old["blueprints"].get(name) != new["blueprints"][name]
        ],
        "mcp_chg": [
            (d, old["mcp"].get(d), new["mcp"][d])
            for d in sorted(new["mcp"])
            if old["mcp"].get(d) != new["mcp"][d]
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", default="7 days ago", help="基线时间点（git --before 语法）")
    ap.add_argument("--base", default=None, help="直接指定基线提交")
    args = ap.parse_args()

    if not MAP.exists():
        print(f"地图不存在：{MAP_REL}，先运行 scripts/arch_map.py --write")
        return 2

    sha, date = resolve_base(args.since, args.base)
    old_text = git("show", f"{sha}:{MAP_REL}") if sha else ""
    if not old_text.strip():
        print(f"基线 {sha[:7] if sha else '?'} 无地图，视为首次。")
        return 0

    old = parse_map(old_text)
    new = parse_map(MAP.read_text(encoding="utf-8"))

    delta = compute_delta(old, new)
    add_routes = delta["add_routes"]
    del_routes = delta["del_routes"]
    chg_auth = delta["chg_auth"]
    add_tbl = delta["add_tbl"]
    del_tbl = delta["del_tbl"]
    bp_state = delta["bp_state"]
    add_bp = delta["add_bp"]
    del_bp = delta["del_bp"]
    mcp_chg = delta["mcp_chg"]

    iso = dt.date.today().isocalendar()
    L = [f"# 架构变更简报 · {iso.year}-W{iso.week:02d}", ""]
    L.append(f"基线：`{sha[:7]}`（{date}） → 现状 `HEAD`")
    L.append("")

    total = len(add_routes) + len(del_routes) + len(chg_auth) + len(add_tbl) + len(del_tbl) + len(bp_state) + len(add_bp) + len(del_bp) + len(mcp_chg)
    if not total:
        L.append("**本周无结构性变化。** 没有新增/删除路由、表、蓝图装配或鉴权调整。")
        print("\n".join(L))
        return 0

    L.append("## 摘要")
    L.append("")
    L.append(f"- 路由：新增 {len(add_routes)} / 删除 {len(del_routes)} / 鉴权或方法变更 {len(chg_auth)}")
    L.append(f"- 数据表：新增 {len(add_tbl)} / 删除 {len(del_tbl)}")
    L.append(f"- 蓝图：新增 {len(add_bp)} / 删除 {len(del_bp)} / 装配状态变化 {len(bp_state)}")
    L.append(f"- MCP 工具域变化：{len(mcp_chg)}")
    L.append("")

    if add_routes:
        L += ["## 新增路由", "", "| endpoint | 方法 | 规则 | 鉴权 |", "|---|---|---|---|"]
        for ep in add_routes:
            m, r, a, d = new["routes"][ep]
            L.append(f"| `{ep}` | {m} | `{r}` | {a}{(' · ' + d) if d else ''} |")
        L.append("")

    if chg_auth:
        L += ["## 鉴权 / 方法变更（最需要你确认的一项）", "", "| endpoint | 原 | 现 |", "|---|---|---|"]
        for ep, o, n in chg_auth:
            L.append(f"| `{ep}` | {o} | {n} |")
        L.append("")

    if del_routes:
        L += ["## 删除路由", ""]
        L += [f"- `{ep}`（原 {old['routes'][ep][2]}）" for ep in del_routes]
        L.append("")

    if add_tbl or del_tbl:
        L += ["## 数据表", ""]
        L += [f"- 新增 `{t}`" for t in add_tbl]
        L += [f"- 删除 `{t}`" for t in del_tbl]
        L.append("")

    if add_bp or del_bp or bp_state:
        L += ["## 蓝图装配", ""]
        L += [f"- 新增蓝图 `{b}`" for b in add_bp]
        L += [f"- 移除蓝图 `{b}`" for b in del_bp]
        for name, o, n in bp_state:
            L.append(f"- `{name}`：{'在役' if o else '未装配'} → {'在役' if n else '**未装配**'}")
        L.append("")

    if mcp_chg:
        L += ["## MCP 工具域", ""]
        L += [f"- `{d}`：{o} → {n} 个函数" for d, o, n in mcp_chg]
        L.append("")

    L.append("---")
    L.append("明细与全量事实：`docs/ARCHITECTURE_MAP.md`（`git log -p` 可看完整演进）。")
    print("\n".join(L))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""scripts/agent_framework/report_html.py

把 agent-framework 报告（Markdown）渲染成**自包含、响应式**的 HTML ——
供手机 / iPad 上直接打开阅读（Markdown 原件保留不动，HTML 是只读视图产物）。

背景：Markdown 本身不带版式，窄屏可读性由渲染器决定、不可控；
本工具产出「内嵌 CSS 的单文件 HTML」，在手机（≤480px）/ 平板（≤1024px）/ 桌面
三档宽度下均可读，并与 dashboard 保持同一视觉语言。

命令：
  convert <path...>               md → 同名 .html（目录则递归 *.md）
  index   <dir...> -o <out.html>  生成索引页（自动先转换缺失 / 过期的 .html）

依赖：pandoc（渲染引擎，brew install pandoc）。缺失时打印安装指引并以 64 退出。

退出码：0 成功；1 转换失败；64 依赖缺失 / 参数错误。
"""

from __future__ import annotations

import argparse
import html
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ============================================================
# HTML 模板（报告视图）
# ============================================================
REPORT_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>${title}</title>
<style>
    :root{
        --bg:#f6f7f9;--card:#fff;--line:#e7e9ee;
        --t1:#111827;--t2:#6b7280;--t3:#9ca3af;--accent:#1d4ed8;
    }
    *{box-sizing:border-box}
    html{-webkit-text-size-adjust:100%;text-size-adjust:100%}
    body{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;margin:0;background:var(--bg);color:var(--t1)}
    .wrap{max-width:860px;margin:0 auto;padding:24px 20px 64px}
    .doc{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:26px 30px}
    h1{font-size:22px;margin:0 0 6px;line-height:1.35}
    h2{font-size:17.5px;margin:26px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--line)}
    h3{font-size:15.5px;margin:20px 0 8px}
    h4{font-size:14.5px;margin:16px 0 6px}
    p{font-size:15px;line-height:1.75;margin:10px 0}
    a{color:var(--accent);word-break:break-word}
    code{background:#f3f4f6;padding:1px 5px;border-radius:4px;font-size:.9em;word-break:break-word}
    pre{background:#f6f8fa;border:1px solid var(--line);border-radius:10px;padding:12px 14px;overflow-x:auto;-webkit-overflow-scrolling:touch}
    pre code{background:none;padding:0;font-size:12.5px;line-height:1.6;word-break:normal}
    blockquote{margin:12px 0;padding:8px 16px;border-left:3px solid #cbd5e1;background:#f8fafc;color:var(--t2);border-radius:0 8px 8px 0}
    blockquote p{margin:4px 0;font-size:14px}
    ul,ol{padding-left:22px;font-size:15px;line-height:1.75}
    li{margin:3px 0}
    hr{border:none;border-top:1px solid var(--line);margin:22px 0}
    table{width:100%;border-collapse:collapse;font-size:13.5px;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
    th{background:#f3f5f8;text-align:left;padding:8px 12px;font-weight:600;color:#4b5563}
    td{padding:7px 12px;border-top:1px solid #f1f2f4;overflow-wrap:break-word}
    tr:hover td{background:#fafbfc}
    .tbl{overflow-x:auto;-webkit-overflow-scrolling:touch;border-radius:10px;margin:12px 0}
    input[type=checkbox]{margin-right:6px}
    .docmeta{font-size:12px;color:var(--t3);margin:0 0 14px}
    .pagefoot{margin-top:14px;font-size:11.5px;color:var(--t3);text-align:center}
    /* ---------- 响应式：平板（iPad 竖屏 / 小笔记本）---------- */
    @media(max-width:1024px){
        .wrap{padding:20px 18px 56px}
    }
    /* ---------- 响应式：手机 / 小屏 ---------- */
    @media(max-width:760px){
        .wrap{padding:16px 12px calc(40px + env(safe-area-inset-bottom))}
        .doc{padding:18px 16px;border-radius:12px}
        h1{font-size:19px}
        h2{font-size:16px;margin:22px 0 8px}
        p,ul,ol{font-size:14.5px}
        .tbl table{min-width:560px}
        th,td{padding:7px 10px;font-size:12.5px}
        pre{padding:10px 12px}
    }
    /* ---------- 响应式：小屏手机 ---------- */
    @media(max-width:480px){
        .wrap{padding:14px 10px calc(36px + env(safe-area-inset-bottom))}
        .doc{padding:15px 13px}
        h1{font-size:17.5px}
        p,ul,ol{font-size:14px}
        .tbl table{min-width:520px}
    }
</style>
</head>
<body>
<div class="wrap">
<div class="doc">
<div class="docmeta">只读视图 · 源：${src_name} · 渲染：${rendered_at}</div>
${body}
</div>
<div class="pagefoot">由 <code>scripts/agent_framework/report_html.py</code> 渲染（pandoc）· Markdown 原件为准</div>
</div>
</body>
</html>
"""

# ============================================================
# HTML 模板（索引页）
# ============================================================
INDEX_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DailyCheck · 报告索引</title>
<style>
    :root{
        --bg:#f6f7f9;--card:#fff;--line:#e7e9ee;
        --t1:#111827;--t2:#6b7280;--t3:#9ca3af;--accent:#1d4ed8;
    }
    *{box-sizing:border-box}
    html{-webkit-text-size-adjust:100%;text-size-adjust:100%}
    body{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;margin:0;background:var(--bg);color:var(--t1)}
    .wrap{max-width:860px;margin:0 auto;padding:24px 20px calc(64px + env(safe-area-inset-bottom))}
    h1{font-size:21px;margin:0 0 4px}
    .sub{color:var(--t2);font-size:12.5px;margin:0 0 8px}
    h2{font-size:15.5px;margin:24px 0 10px;display:flex;align-items:center;gap:8px}
    h2 .cnt{font-size:12px;color:var(--t3);font-weight:400}
    .item{display:block;padding:12px 16px;background:var(--card);border:1px solid var(--line);border-radius:11px;
          margin-bottom:8px;text-decoration:none;color:var(--t1)}
    .item:hover{border-color:#c7d2fe;background:#fdfdff}
    .item .it{font-size:14px;font-weight:600;line-height:1.5}
    .item .is{font-size:12px;color:var(--t2);margin-top:3px;font-variant-numeric:tabular-nums}
    .item.dash{border-top:3px solid #2f6fed}
    /* ---------- 响应式：手机 / 小屏 ---------- */
    @media(max-width:760px){
        .wrap{padding:16px 12px calc(48px + env(safe-area-inset-bottom))}
        h1{font-size:18.5px}
        .item{padding:11px 14px}
        .item .it{font-size:13.5px}
    }
    @media(max-width:480px){
        .wrap{padding:14px 10px calc(40px + env(safe-area-inset-bottom))}
        h1{font-size:17.5px}
    }
</style>
</head>
<body>
<div class="wrap">
<h1>DailyCheck · 报告索引</h1>
<p class="sub">生成于 ${generated_at} · 手机 / iPad / 桌面自适应 · 点条目直接打开</p>
${sections}
</div>
</body>
</html>
"""


# ============================================================
# 工具函数
# ============================================================
def _esc(s: str) -> str:
    return html.escape(str(s), quote=True)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime())


def _find_pandoc() -> str:
    p = shutil.which("pandoc")
    if not p:
        print(
            "错误：未找到 pandoc（渲染引擎）。\n"
            "安装：brew install pandoc\n"
            "或（Linux）：apt-get install -y pandoc",
            file=sys.stderr,
        )
        sys.exit(64)
    return p


def _extract_title(md_text: str, fallback: str) -> str:
    """取第一个 ATX 一级标题（# xxx）作为标题；跳过代码块（防 shell 注释误判）。

    级联：# 标题 → ## 标题 → 文件名。
    """
    in_code = False
    h2: str | None = None
    for line in md_text.splitlines():
        s = line.strip()
        if s.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        if s.startswith("# "):
            return s[2:].strip()
        if h2 is None and s.startswith("## "):
            h2 = s[3:].strip()
    return h2 or fallback


def _iter_md(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        if p.is_dir():
            out.extend(
                sorted(
                    md
                    for md in p.rglob("*.md")
                    if not any(part.startswith(".") for part in md.relative_to(p).parts)
                )
            )
        elif p.suffix.lower() == ".md":
            out.append(p)
    return out


def _md_to_body(md_path: Path, pandoc: str) -> str:
    """调用 pandoc 渲染 markdown → HTML body 片段，并做响应式后处理。"""
    r = subprocess.run(
        [pandoc, "-f", "gfm", "-t", "html5", "--wrap=none", str(md_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if r.returncode != 0:
        raise RuntimeError(f"pandoc 失败（{md_path}）：{r.stderr.strip()[:300]}")
    body = r.stdout
    # 响应式后处理：表格包横滚容器（仅 pandoc 裸 <table> 标签，gfm 表格不会带属性）
    body = body.replace("<table>", '<div class="tbl"><table>')
    body = body.replace("</table>", "</table></div>")
    return body


def convert_one(md_path: Path, out_path: Path | None, pandoc: str) -> Path:
    md_text = md_path.read_text(encoding="utf-8")
    title = _extract_title(md_text, md_path.stem)
    body = _md_to_body(md_path, pandoc)
    page = REPORT_TEMPLATE
    for key, val in {
        "${title}": _esc(title),
        "${src_name}": _esc(md_path.name),
        "${rendered_at}": _esc(_now_iso()),
        "${body}": body,
    }.items():
        page = page.replace(key, val)
    out = out_path or md_path.with_suffix(".html")
    out.write_text(page, encoding="utf-8")
    return out


# ============================================================
# 子命令
# ============================================================
def cmd_convert(args: argparse.Namespace) -> int:
    pandoc = _find_pandoc()
    targets = _iter_md([Path(p) for p in args.paths])
    if not targets:
        print("没有找到 .md 文件", file=sys.stderr)
        return 64
    ok = 0
    for md in targets:
        try:
            out = convert_one(md, None, pandoc)
            print(f"✓ {md} → {out.name}")
            ok += 1
        except Exception as e:  # noqa: BLE001 —— 单文件失败不中断批处理
            print(f"✗ {md}: {e}", file=sys.stderr)
    print(f"转换完成：{ok}/{len(targets)}")
    return 0 if ok == len(targets) else 1


def _dir_section(dir_path: Path, group_title: str, pandoc: str, base_out: Path) -> str:
    """为一个目录生成一组索引条目（自动转换缺失 / 过期的 .html）。"""
    items: list[tuple[float, str]] = []
    for md in sorted(dir_path.rglob("*.md")):
        # 跳过隐藏目录（.dashboard / .git 等配置目录里的说明文件不是给人看的报告）
        if any(part.startswith(".") for part in md.relative_to(dir_path).parts):
            continue
        html_path = md.with_suffix(".html")
        # 缺失或过期 → 转换
        try:
            if not html_path.exists() or html_path.stat().st_mtime < md.stat().st_mtime:
                convert_one(md, html_path, pandoc)
        except Exception as e:  # noqa: BLE001
            print(f"✗ 跳过 {md}: {e}", file=sys.stderr)
            continue
        title = _extract_title(md.read_text(encoding="utf-8"), md.stem)
        # 链接用「从索引页所在目录出发」的相对路径（跨目录也能跳转）
        rel = os.path.relpath(str(html_path), str(base_out.parent))
        mtime = time.strftime("%Y-%m-%d", time.localtime(md.stat().st_mtime))
        items.append((md.stat().st_mtime, (title, f"{rel} · {mtime}", rel)))
    if not items:
        return ""
    items.sort(key=lambda x: x[0], reverse=True)
    rows = "\n".join(
        f'<a class="item" href="{_esc(rel)}"><div class="it">{_esc(title)}</div>'
        f'<div class="is">{_esc(sub)}</div></a>'
        for _, (title, sub, rel) in items
    )
    return (
        f'<h2>{_esc(group_title)} <span class="cnt">{len(items)} 份</span></h2>\n{rows}\n'
    )


def cmd_index(args: argparse.Namespace) -> int:
    pandoc = _find_pandoc()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    sections: list[str] = []

    # 仪表盘置顶（若与输出同目录存在 dashboard.html）
    dash = out.parent / "dashboard.html"
    if dash.exists():
        dt = time.strftime("%Y-%m-%d %H:%M", time.localtime(dash.stat().st_mtime))
        sections.append(
            '<h2>📊 仪表盘 <span class="cnt">1 份</span></h2>\n'
            f'<a class="item dash" href="dashboard.html"><div class="it">项目任务仪表盘</div>'
            f'<div class="is">dashboard.html · 更新 {dt}</div></a>\n'
        )

    for d in args.dirs:
        dp = Path(d)
        if not dp.is_dir():
            print(f"跳过（不是目录）：{d}", file=sys.stderr)
            continue
        if dp.name == "issue-reports":
            group = "🔍 Issue 巡检报告"
        elif dp.name == "reports":
            group = "📋 PR 门禁报告"
        else:
            group = f"📋 {dp.name}/"
        sections.append(_dir_section(dp, group, pandoc, out))

    page = INDEX_TEMPLATE.replace("${generated_at}", _esc(_now_iso())).replace("${sections}", "".join(sections))
    out.write_text(page, encoding="utf-8")
    print(f"✓ 索引页：{out}")
    return 0


# ============================================================
# CLI
# ============================================================
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="报告 Markdown → 响应式 HTML（手机/iPad 可读）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_conv = sub.add_parser("convert", help="md → 同名 .html（目录递归）")
    p_conv.add_argument("paths", nargs="+", help="md 文件或目录")
    p_conv.set_defaults(func=cmd_convert)

    p_idx = sub.add_parser("index", help="生成索引页（自动转换缺失/过期的 html）")
    p_idx.add_argument("dirs", nargs="+", help="要纳入索引的目录")
    p_idx.add_argument("-o", "--out", required=True, help="索引页输出路径（如 reports/index.html）")
    p_idx.set_defaults(func=cmd_index)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

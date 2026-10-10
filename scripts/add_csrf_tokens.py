#!/usr/bin/env python3
"""为 templates/ 下所有 method="post" 表单注入 CSRF hidden input（幂等）。

用法：
  python scripts/add_csrf_tokens.py           # 注入
  python scripts/add_csrf_tokens.py --check   # 只校验，缺 token 返回 1

只处理开标签含 method="post"（大小写/引号宽容）的 <form>；
GET 表单（含无 method 的默认 GET）不动。开标签与 </form> 之间
已有 csrf_token 字样的跳过。
"""
import re
import sys
from pathlib import Path

TPL_DIR = Path(__file__).resolve().parent.parent / "templates"
HIDDEN = '<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">'
TAG_RE = re.compile(r"<form\b[^>]*>", re.IGNORECASE | re.DOTALL)
METHOD_POST_RE = re.compile(r"""method\s*=\s*["']?post["']?""", re.IGNORECASE)


def _post_form_segments(text: str):
    """产出 (开标签 match, 开标签到 </form> 之间的文本)。"""
    for m in TAG_RE.finditer(text):
        if not METHOD_POST_RE.search(m.group(0)):
            continue
        close = text.find("</form>", m.end())
        seg = text[m.end(): close if close != -1 else len(text)]
        yield m, seg


def patch_text(text: str):
    out, pos, patched = [], 0, 0
    for m, seg in _post_form_segments(text):
        if "csrf_token" in seg:
            continue
        out.append(text[pos:m.end()])
        out.append("\n        " + HIDDEN)
        pos = m.end()
        patched += 1
    out.append(text[pos:])
    return "".join(out), patched


def main() -> int:
    templates = sorted(TPL_DIR.rglob("*.html"))
    if "--check" in sys.argv:
        missing = [
            f"{f.relative_to(TPL_DIR)} @offset {m.start()}"
            for f in templates
            for m, seg in _post_form_segments(f.read_text())
            if "csrf_token" not in seg
        ]
        if missing:
            print("MISSING csrf_token:")
            for x in missing:
                print("  -", x)
            return 1
        print(f"OK: {len(templates)} 个模板，所有 POST 表单均带 csrf_token")
        return 0

    total = 0
    for f in templates:
        text = f.read_text()
        new, n = patch_text(text)
        if n:
            f.write_text(new)
            print(f"{f.relative_to(TPL_DIR)}: +{n}")
            total += n
    print(f"共注入 {total} 个表单")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""scripts/agent_framework/mutation_check.py

变异验证执行器：把「我改坏过、测试真变红了」从自述变成机器证据。

为什么需要（2026-10-21）：
  workflow.md §②.4 要求「关键断言做变异验证」，但此前只存在人工做法
  （cp 备份 → 手改代码 → 跑 pytest → cp 还原），过程不留任何机器可读产物。
  对 CI 和 Prod Agent 而言，它与「我验过了」一句自述等价——无法区分
  真做过、做错了、还是根本没做。

  这不是「技术上验不了」，而是「手工做法天然留不下证据」。把声明固定成
  结构化输入后，整件事就是可执行的。

输入：PR body 的 `## 变异验证` 段，内含一个 ```json 代码块：

    ## 变异验证

    ```json
    [
      {"id": "M1",
       "file": "scripts/dailycheck_dashboard/ops_parse.py",
       "find": "return \\"auto\\" if ... else \\"manual\\"",
       "replace": "return \\"manual\\" if ... else \\"auto\\"",
       "test": "tests/test_dashboard_ops.py::test_classify_manual",
       "expect": "fail"}
    ]
    ```

  用 JSON 而非 YAML：find/replace 里全是引号和冒号（Python 代码），
  YAML 的引号/多行规则会引入解析歧义，JSON 零歧义。

输出：reports/mutation.json
  {"declared": 3, "verified": 3, "uncovered": 0, "results": [...],
   "diff_checked": true}

退出码：
  0  全部声明变异都被测试看守住（注入后测试真实 FAIL）
  1  有变异注入后测试仍然 PASS —— 说明该测试**根本没看守住**这条断言
  2  声明格式错误 / find 不唯一 / 变异文件不在 PR diff 内
  3  还原失败（工作树被污染，必须人工介入）
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
REPORTS_DIR = REPO_ROOT / "reports"
OUT_FILE = REPORTS_DIR / "mutation.json"

SECTION = "## 变异验证"
JSON_BLOCK_RE = re.compile(r"```json\s*\n(.*?)\n```", re.S)

# 单条变异跑测试的上限。只跑指定 node，正常在秒级；给足余量防 CI 抖动。
TEST_TIMEOUT_SEC = 300

# 允许混进 PR 的运行时/产物文件前缀（检查用，白名单之外一律提示）
REQUIRED_KEYS = ("id", "file", "find", "replace", "test")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fail(msg: str, code: int) -> None:
    print(f"[mutation] FAIL: {msg}", file=sys.stderr)
    sys.exit(code)


def parse_declarations(pr_body: str) -> list[dict]:
    """从 PR body 抽出变异声明。无该段 → 返回空列表（不适用，非错误）。"""
    if SECTION not in pr_body:
        return []
    tail = pr_body.split(SECTION, 1)[1]
    # 只取该段之后的第一个 json 块，避免误吞后续章节的代码块
    m = JSON_BLOCK_RE.search(tail)
    if not m:
        fail(f"'{SECTION}' 段后未找到 ```json 代码块", 2)
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        fail(f"变异声明 JSON 解析失败：{exc}", 2)

    if not isinstance(data, list):
        fail("变异声明必须是 JSON 数组（即使只有一条）", 2)

    for i, item in enumerate(data):
        if not isinstance(item, dict):
            fail(f"第 {i + 1} 条不是对象", 2)
        missing = [k for k in REQUIRED_KEYS if not item.get(k)]
        if missing:
            fail(f"第 {i + 1} 条缺字段：{', '.join(missing)}", 2)
        if item.get("expect", "fail") != "fail":
            # 只支持 expect=fail：本门禁的语义是「注入 bug 后测试必须变红」。
            # expect=pass（等价变异）没有看守价值，不给入口。
            fail(f"第 {i + 1} 条 expect 只支持 'fail'，收到 {item.get('expect')!r}", 2)
    return data


def changed_files() -> set[str] | None:
    """PR 改动文件集。取不到 base/head 时返回 None（跳过 diff 校验）。"""
    base = os.environ.get("BASE_SHA", "").strip()
    head = os.environ.get("HEAD_SHA", "").strip()
    if not base or not head:
        return None
    try:
        out = subprocess.run(
            ["git", "diff", "--name-only", f"{base}...{head}"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return {line.strip() for line in out.stdout.splitlines() if line.strip()}


def run_one(mut: dict, allowed: set[str] | None) -> dict:
    """注入一条变异并验证测试是否真变红。返回该条的判定结果。"""
    rel = mut["file"]
    path = (REPO_ROOT / rel).resolve()

    # 越界守卫：变异只能落在仓库内
    try:
        path.relative_to(REPO_ROOT.resolve())
    except ValueError:
        return _result(mut, "error", None, f"路径越出仓库：{rel}")

    if allowed is not None and rel not in allowed:
        return _result(mut, "error", None, "变异文件不在本 PR 改动范围内")

    if not path.exists():
        return _result(mut, "error", None, f"文件不存在：{rel}")

    original = path.read_text(encoding="utf-8")
    before = _sha256(original)

    occurrences = original.count(mut["find"])
    if occurrences != 1:
        return _result(
            mut, "error", None,
            f"find 在 {rel} 中出现 {occurrences} 次（要求恰好 1 次）",
        )

    mutated = original.replace(mut["find"], mut["replace"], 1)
    if mutated == original:
        return _result(mut, "error", None, "replace 结果与原文件相同（变异无效）")

    returncode: int | None = None
    try:
        path.write_text(mutated, encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", mut["test"],
             "-x", "-q", "--tb=no", "-p", "no:cacheprovider",
             f"--basetemp={os.environ.get('TMPDIR', '/tmp')}/pytest-mut"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=TEST_TIMEOUT_SEC,
        )
        returncode = proc.returncode
        output = proc.stdout + proc.stderr
    except subprocess.TimeoutExpired:
        return _result(mut, "error", None, f"测试超时（>{TEST_TIMEOUT_SEC}s）")
    finally:
        # 还原必须绝对可靠：哪怕上面抛异常也要还原，并校验哈希
        path.write_text(original, encoding="utf-8")
        if _sha256(path.read_text(encoding="utf-8")) != before:
            print(
                f"[mutation] 致命：{rel} 还原后哈希不匹配，工作树已被污染！",
                file=sys.stderr,
            )
            Path("/tmp/MUTATION_RESTORE_FAILED").write_text(rel, encoding="utf-8")
            sys.exit(3)

    # pytest 返回码语义：0=全绿 1=有用例失败 2=中断 3=内部错 4=用法错 5=未收集到用例。
    # 只有 1 才算「测试看守住了」。2/4/5 是环境或变异本身的问题，不能算验证成功
    # ——尤其 5（无用例）若被当作非 0 就放行，等于给了一个免费假绿通道。
    if returncode == 0:
        return _result(
            mut, "uncovered", 0,
            "注入变异后测试仍然通过 —— 该测试没看守住这条断言",
        )
    if returncode != 1:
        return _result(
            mut, "error", returncode,
            f"pytest 返回 {returncode}（非「用例失败」），变异或测试选择有问题",
        )
    if "failed" not in output and "error" not in output.lower():
        return _result(mut, "error", returncode, "返回码 1 但输出无失败摘要，存疑")
    return _result(mut, "verified", 1, "")


def _result(mut: dict, outcome: str, rc: int | None, note: str) -> dict:
    return {
        "id": mut.get("id", "?"),
        "file": mut.get("file", "?"),
        "test": mut.get("test", "?"),
        "outcome": outcome,
        "returncode": rc,
        "note": note,
    }


def render_table(results: list[dict]) -> str:
    lines = ["| 变异 | 文件 | 测试 | 判定 | 说明 |", "|---|---|---|---|---|"]
    mark = {"verified": "✅ 测试看守住", "uncovered": "❌ 测试没看守住",
            "error": "⚠️ 声明有误"}
    for r in results:
        lines.append(
            f"| {r['id']} | `{r['file']}` | `{r['test']}` | "
            f"{mark.get(r['outcome'], r['outcome'])} | {r['note']} |"
        )
    return "\n".join(lines)


def main() -> int:
    pr_body_file = os.environ.get("PR_BODY_FILE")
    pr_body = ""
    if pr_body_file and Path(pr_body_file).exists():
        pr_body = Path(pr_body_file).read_text(encoding="utf-8")
    else:
        pr_body = os.environ.get("PR_BODY", "")

    decls = parse_declarations(pr_body)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    if not decls:
        payload = {
            "declared": 0, "verified": 0, "uncovered": 0,
            "results": [], "diff_checked": changed_files() is not None,
            "note": "PR body 无「## 变异验证」声明",
        }
        OUT_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print("[mutation] 未声明变异验证（纯文档/无行为变更 PR 可接受，"
              "但 Prod Agent 应据此判断是否真的没有可变异点）")
        return 0

    allowed = changed_files()
    if allowed is None:
        print("[mutation] 警告：未取到 BASE_SHA/HEAD_SHA，跳过「变异必须在 diff 内」校验")

    results = [run_one(m, allowed) for m in decls]
    verified = sum(1 for r in results if r["outcome"] == "verified")
    uncovered = sum(1 for r in results if r["outcome"] == "uncovered")
    errored = sum(1 for r in results if r["outcome"] == "error")

    payload = {
        "declared": len(decls), "verified": verified,
        "uncovered": uncovered, "errored": errored,
        "results": results, "diff_checked": allowed is not None,
    }
    OUT_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[mutation] 声明 {len(decls)} 条："
          f"看守住 {verified} / 没看守住 {uncovered} / 声明有误 {errored}")
    print(render_table(results))

    if errored:
        print("[mutation] FAIL: 存在声明错误（find 不唯一/文件不在 diff 等）",
              file=sys.stderr)
        return 2
    if uncovered:
        print("[mutation] FAIL: 有变异注入后测试仍绿 —— 这些测试是假绿，"
              "不能作为该断言已被验证的证据", file=sys.stderr)
        return 1
    print("[mutation] ✅ 全部声明的变异都被测试真实看守住")
    return 0


if __name__ == "__main__":
    sys.exit(main())

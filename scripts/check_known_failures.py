#!/usr/bin/env python3
"""CI 零新增失败门禁：对比 pytest 输出与 tests/known_failures.txt。

用法：python scripts/check_known_failures.py <pytest_output.txt>
退出码：0 = 无新增失败；1 = 有不在已知清单里的失败；2 = 用法错误。

已知清单（tests/known_failures.txt）允许超出当前失败集（已知项修好后
应从清单删除，防止清单变成垃圾桶）。
"""
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
KNOWN_FILE = REPO_ROOT / "tests" / "known_failures.txt"

FAILED_RE = re.compile(r"^FAILED\s+(\S+)")


def extract_failed_ids(output_path: str) -> set:
    failed = set()
    for line in Path(output_path).read_text(errors="replace").splitlines():
        m = FAILED_RE.match(line.strip())
        if not m:
            continue
        nodeid = m.group(1)
        failed.add(nodeid)
        # 参数化用例同时登记去参基础 id，避免清单写死参数组合
        if "[" in nodeid:
            failed.add(nodeid.split("[")[0])
    return failed


def load_known_ids() -> set:
    if not KNOWN_FILE.exists():
        return set()
    known = set()
    for line in KNOWN_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            known.add(line)
    return known


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    text = Path(sys.argv[1]).read_text(errors="replace")

    # 防假绿：收集错误不产生 FAILED 行，坏依赖会让本脚本空过 → 必须拒绝。
    if "errors during collection" in text or "Interrupted:" in text:
        print("FAIL: pytest 存在收集错误（依赖缺失/导入失败），门禁拒绝放行：")
        for line in text.splitlines():
            if line.startswith("ERROR "):
                print(f"  - {line.strip()}")
        return 1
    if not text.strip():
        print("FAIL: pytest 输出为空（进程异常/重定向失败），门禁拒绝放行。")
        return 1
    m = re.search(r"(\d+) passed", text)
    # 完整输出（含 passed 摘要）才校验零用例；grep 后的 FAILED 行文件跳过此检查。
    if m and int(m.group(1)) == 0:
        print("FAIL: pytest 0 passed（无任何用例执行成功），门禁拒绝放行。")
        return 1

    failed = extract_failed_ids(sys.argv[1])
    known = load_known_ids()
    new = failed - known
    if not new:
        passed_info = f"，passed {m.group(1)}" if m else ""
        print(
            f"OK: 当前失败 {len(failed)} 个，全部在已知清单内"
            f"（清单 {len(known)} 条{passed_info}）"
        )
        return 0
    print(f"FAIL: 发现 {len(new)} 个新增失败（不在 tests/known_failures.txt）：")
    for nodeid in sorted(new):
        print(f"  - {nodeid}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

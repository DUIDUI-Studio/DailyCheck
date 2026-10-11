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
    failed = extract_failed_ids(sys.argv[1])
    known = load_known_ids()
    new = failed - known
    if not new:
        print(f"OK: 当前失败 {len(failed)} 个，全部在已知清单内（清单 {len(known)} 条）")
        return 0
    print(f"FAIL: 发现 {len(new)} 个新增失败（不在 tests/known_failures.txt）：")
    for nodeid in sorted(new):
        print(f"  - {nodeid}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

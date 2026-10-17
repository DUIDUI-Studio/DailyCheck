"""blue_green shell 脚本测试的宿主态隔离工具（Issue #44 / #81）。

`scripts/blue_green/*.sh` 在「本机 / CI」和「生产 VPS」上会走**不同分支**：
宿主有没有 systemctl、部署单飞状态目录是否已激活、真 nginx conf 是否存在。
用例若直接继承宿主态，同一份代码就会 dev 绿、prod 红——那是测试缺陷，不是
产品缺陷（Issue #44 现场 5 个 fail、Issue #81 现场 3 个 fail，同一根因族）。

本模块只做一件事：把这类宿主态**显式钉死**，让用例验证脚本自身逻辑。
"""
from __future__ import annotations

from pathlib import Path

# 只服务 `show -p <prop> --value <unit>`；其余子命令一律 exit 0
# （journalctl / ss 等仍走真命令，失败路径用例的语义不变）。
_FAKE_SYSTEMCTL = """#!/usr/bin/env bash
# blue_green 测试用 fake systemctl（见 tests/blue_green_testenv.py）
prop=''
for a in "$@"; do
  case "$a" in MainPID|Environment) prop="$a" ;; esac
done
if [[ "${1:-}" == "show" ]]; then
  case "$prop" in
    MainPID) echo "" ;;
    Environment) echo "DRY_RUN=false" ;;
  esac
fi
exit 0
"""


def install_fake_systemctl(bindir: Path) -> Path:
    """在 bindir 生成 fake systemctl，返回 bindir（调用方自行前置 PATH）。

    语义：MainPID 恒空（守卫走第②层 unit 配置取证）+ Environment 恒
    `DRY_RUN=false`（= 绿已「转正」的合法生产态）→ 守卫判 not_dry_run 放行。

    为什么不是「返回空 Environment」（判 unknown、仅告警放行）：那会让用例
    从「守卫放行」的**未知分支**通过，等于断言假绿。生产 VPS 上真实态是
    `DRY_RUN=true`（绿未转正），本 fake 只覆盖本文件契约内「切流量动作本身」
    的验证；守卫判定行为由 tests/test_blue_green_dryrun_guard.py 专项覆盖。
    """
    bindir.mkdir(parents=True, exist_ok=True)
    fake = bindir / "systemctl"
    fake.write_text(_FAKE_SYSTEMCTL, encoding="utf-8")
    fake.chmod(0o755)
    return bindir

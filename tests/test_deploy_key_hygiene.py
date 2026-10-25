"""部署链路凭据卫生（Issue #9）。

2026-10-24 首次真实部署演练暴露的完整链条：

1. ``ci.yml`` deploy job 把 ``$VPS_SSH_KEY`` 写成仓库根目录下的 ``vps_key``；
2. 紧接着的 ``tar ... -czf`` 打包当前目录，排除清单里没有它 → 私钥进包；
3. 包经 SSH 解到 ``/root/releases/<ts>/``，再由 ``deploy_vps.sh`` 的
   ``rsync --delete`` 复制进 ``/root/DailyCheck/vps_key``；
4. 此后任何 ``git add -A`` 都能把它提交进仓库 —— 事实上历史提交
   ``1a1f9e2 chore: auto-commit after health check`` 已经这么干过一次，
   而 ``--delete`` 不处理排除项，光加 exclude 清不掉已经躺在生产目录的那份。

本文件锁死剩余防线，任何一道被回退都会打红：

* CI 打包、rsync 切换、回滚代码备份三处排除清单都含 ``vps_key``；
* 部署脚本在切换前显式删除历史残留；
* ``.gitignore`` 挡住私钥类文件，``git add -A`` 收不进来。

2026-10-25 起 ``ci.yml`` 的 deploy job 已移除（部署归 Prod Agent 蓝绿体系），
CI 侧不再有私钥落盘路径；原先针对该 job 的两条用例随之删除，``deploy_vps.sh``
作为应急手动路径仍由下方用例守护。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SH = REPO_ROOT / "scripts" / "deploy_vps.sh"
GITIGNORE = REPO_ROOT / ".gitignore"


def test_deploy_rsync_excludes_credentials() -> None:
    """rsync 切换不能把凭据类文件带进应用目录。"""
    text = DEPLOY_SH.read_text(encoding="utf-8")
    rsync_block = text.split("RSYNC_EXCLUDES=(", 1)[1].split(")", 1)[0]
    assert "vps_key" in rsync_block, "RSYNC_EXCLUDES 缺 vps_key"
    assert "*.pem" in rsync_block, "RSYNC_EXCLUDES 缺 *.pem"


def test_deploy_code_backup_excludes_credentials() -> None:
    """回滚用的代码备份同样不能把私钥打进去长期留存。"""
    text = DEPLOY_SH.read_text(encoding="utf-8")
    backup_tar = re.search(r"tar\b.*?-czf \"\$CODE_BACKUP\"", text, re.S)
    assert backup_tar, "没找到回滚代码备份的 tar 命令"
    block = backup_tar.group(0)
    assert "./vps_key" in block, "代码备份 tar 缺 ./vps_key 排除项"
    assert "./*.pem" in block, "代码备份 tar 缺 ./*.pem 排除项"


def test_deploy_removes_residual_key() -> None:
    """rsync --delete 不删排除项，历史残留必须显式清理。"""
    text = DEPLOY_SH.read_text(encoding="utf-8")
    assert 'rm -f "$APP_DIR/vps_key"' in text, (
        "缺少显式清理：已经被复制进 APP_DIR 的 vps_key 不会被 rsync --delete 带走"
    )


def test_gitignore_blocks_private_key_material() -> None:
    """.gitignore 要挡住私钥类文件，避免下一次 auto-commit 又收进来。"""
    patterns = {
        line.strip()
        for line in GITIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    for required in ("vps_key", "*.pem", "id_rsa", "id_ed25519"):
        assert required in patterns, f".gitignore 缺私钥规则 {required!r}"

"""部署链路凭据卫生（Issue #9）。

2026-10-24 首次真实部署演练暴露的完整链条：

1. ``ci.yml`` deploy job 把 ``$VPS_SSH_KEY`` 写成仓库根目录下的 ``vps_key``；
2. 紧接着的 ``tar ... -czf`` 打包当前目录，排除清单里没有它 → 私钥进包；
3. 包经 SSH 解到 ``/root/releases/<ts>/``，再由 ``deploy_vps.sh`` 的
   ``rsync --delete`` 复制进 ``/root/DailyCheck/vps_key``；
4. 此后任何 ``git add -A`` 都能把它提交进仓库 —— 事实上历史提交
   ``1a1f9e2 chore: auto-commit after health check`` 已经这么干过一次，
   而 ``--delete`` 不处理排除项，光加 exclude 清不掉已经躺在生产目录的那份。

本文件锁死四道防线，任何一道被回退都会打红：

* CI 不把私钥写进打包工作区（临时文件 + trap 清理）；
* CI 打包、rsync 切换、回滚代码备份三处排除清单都含 ``vps_key``；
* 部署脚本在切换前显式删除历史残留；
* ``.gitignore`` 挡住私钥类文件，``git add -A`` 收不进来。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
DEPLOY_SH = REPO_ROOT / "scripts" / "deploy_vps.sh"
GITIGNORE = REPO_ROOT / ".gitignore"


def _deploy_run_script() -> str:
    """取出 ci.yml 里 deploy job 的脚本段。

    deploy 是 jobs 里的最后一个 job，取它到文件末尾即可。这里不引 yaml：
    CI 只装 ``requirements.txt``，里面没有 pyyaml——本地装了、CI 没装的依赖
    会让这些用例在本地绿、CI 红（2026-10-24 本文件首版就踩了这个坑）。
    """
    text = CI_YML.read_text(encoding="utf-8")
    idx = text.index("\n  deploy:")
    return text[idx:]


def test_ci_writes_key_outside_workspace() -> None:
    """私钥必须落在工作区之外，且退出即删。"""
    script = _deploy_run_script()

    # 反面：把 secret 重定向到工作区里的相对路径（vps_key / ./vps_key 都算）
    assert not re.search(r">\s*\./?vps_key\b", script), (
        "ci.yml 又把私钥写回了工作区根目录（Issue #9 的原始写法），"
        "打包会把它一起带走"
    )
    assert "mktemp" in script, "私钥应写到 mktemp 临时文件，不要落在仓库目录里"
    assert "trap" in script, "临时私钥必须用 trap 在退出时删除"


def test_ci_tar_excludes_vps_key() -> None:
    """即使工作区里有残留的 vps_key，打包也不能带走。"""
    script = _deploy_run_script()
    assert "--exclude='vps_key'" in script or '--exclude="vps_key"' in script, (
        "ci.yml 的 tar 打包缺 vps_key 排除项"
    )


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

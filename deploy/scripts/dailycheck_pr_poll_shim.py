#!/usr/bin/env python3
"""Hermes cron 薄包装：PR 兜底轮询。

真实现见 DailyCheck 仓库 ``scripts/prod_agent/pr_poll.py``。Hermes 的 cron
``script`` 字段被限定在 ``HERMES_HOME/scripts/`` 内（scheduler.py 的路径守卫，
会拒掉任何解析后不在该目录的文件），所以不能直接指向仓库路径。这层包装只
做一件事：把执行权交给仓库里那份有测试、可回滚的实现。

安装：

    cp deploy/scripts/dailycheck_pr_poll_shim.py /root/.hermes/scripts/dailycheck_pr_poll.py
    chmod 755 /root/.hermes/scripts/dailycheck_pr_poll.py

为什么值得多这一层：兜底轮询是「没人盯着时唯一的发现通道」，它自己坏掉时
没有任何人会知道。放在 git 里至少有 pytest 和 code review；留在
``~/.hermes/scripts`` 就只能靠人记得它长什么样。
"""
import os
import runpy
import sys

TARGET = os.path.join(os.environ.get("DAILYCHECK_SOURCE_DIR", "/root/DailyCheck"),
                      "scripts", "prod_agent", "pr_poll.py")

if __name__ == "__main__":
    try:
        runpy.run_path(TARGET, run_name="__main__")
    except FileNotFoundError:
        # 仓库没 pull 到位时宁可报错也不能静默 —— 静默等于兜底通道悄悄消失，
        # 而这正是「没人发现」型故障里最难查的一种。
        print(f"⚠️ DailyCheck PR 兜底轮询异常：找不到 {TARGET}（仓库未同步？）")
        sys.exit(1)

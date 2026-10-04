#!/bin/bash
# DailyCheck — 本地 dev 一键启动脚本 (Flask app, 走 CostReview/当前分支磁盘代码)
#
# 用法:
#   ./scripts/dev.sh              # 默认监听 0.0.0.0:5001
#   PORT=8080 ./scripts/dev.sh   # 自定义端口
#   ./scripts/dev.sh 8080        # 等价写法
#
# 解决的两个非显然坑 (详见前因):
#   1. app.py 第 114 行有 `if os.environ.get("RUNAPP") == "1": app.run(...)` 门控,
#      不设 RUNAPP=1 时 import 完 app 即静默退出 (无输出、秒退)。
#   2. 项目 .venv/bin/python 是 3.13.12 但未装 flask; flask 实际在
#      .venv/lib/python3.12/site-packages/, 故需用系统的 python3.12 + PYTHONPATH 指向它。
#
# 注意:
#   - app.py 的 app.run 写死 debug=False, 本脚本不提供热重载 (reload)。
#     要热更新需改 app.py 源码改用 flask --debug 或 Flask 的 reloader。
#   - 在 agent 沙箱里起的进程会被回收, 长期运行请在你自己的本地终端执行本脚本。
set -euo pipefail

# 无论在哪调用, 都切到仓库根目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_DIR"

# 选 python3.12 解释器
if command -v python3.12 >/dev/null 2>&1; then
  PY_BIN="python3.12"
else
  PY_BIN="/Users/ericmr/.local/bin/python3.12"
fi
[ -x "$(command -v "$PY_BIN" 2>/dev/null || echo "$PY_BIN")" ] \
  || { echo "!! 找不到 python3.12 (试过 PATH 与 /Users/ericmr/.local/bin/python3.12)" >&2; exit 1; }

# 指向 venv 里实际装有 flask 的 site-packages
VENV_SP="$REPO_DIR/.venv/lib/python3.12/site-packages"
[ -d "$VENV_SP" ] \
  || { echo "!! 找不到 $VENV_SP (flask 应装在此处)" >&2; exit 1; }

# 端口: 优先命令行参数, 其次 PORT 环境变量, 默认 5001 (与 app.py 一致)
PORT="${1:-${PORT:-5001}}"

# 前置检查
[ -f "$REPO_DIR/app.py" ] || { echo "!! 仓库根目录找不到 app.py" >&2; exit 1; }
if ! PYTHONPATH="$VENV_SP" "$PY_BIN" -c "import flask" 2>/dev/null; then
  echo "!! python3.12 + $VENV_SP 下 import flask 失败, 请确认依赖已安装" >&2
  exit 1
fi

BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
echo "==> 启动 Flask dev server"
echo "    分支:   $BRANCH"
echo "    解释器: $($PY_BIN --version 2>&1)"
echo "    端口:   $PORT (RUNAPP=1)"
echo "    访问:   http://localhost:$PORT"
echo

# 前台运行 (CTRL-C 退出); RUNAPP=1 触发 app.run
exec env PYTHONPATH="$VENV_SP" RUNAPP=1 PORT="$PORT" "$PY_BIN" -u app.py

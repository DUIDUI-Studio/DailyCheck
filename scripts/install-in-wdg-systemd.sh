#!/bin/bash
# DailyCheck — wdg-systemd 容器内一键安装/恢复脚本
#
# 用法 (在 host 上):
#   docker exec wdg-systemd bash /opt/dailycheck/scripts/install-in-wdg-systemd.sh
#
# 职责:
#   - 容器内 install 两个 systemd unit (dailycheck-app.service + dailycheck-mcp.service)
#   - daemon-reload + enable + restart
#   - 验证端口 + 进程
#   - 跑 R&D 仓库迁移 (idempotent, 见下)
#
# 何时需要跑:
#   - wdg-systemd 容器重建后 (unit 文件在 /etc/systemd/system/ 里, 不持久)
#   - 本仓库 unit 模板改了之后
#
# 不负责:
#   - pip install (镜像已含 python3, 容器重建后如缺依赖手动 pip install -r requirements.txt)
#   - 数据库初始化 (master.db 是 bind mount, 容器外已有)
#   - docker-compose.yml 配置 (那是 wdg-data-foundation 仓库的事)
#
# R&D 仓库迁移:
#   调用 scripts/migrate_add_warehouse_type.py,会:
#     - 若 warehouses.warehouse_type 列不存在 → ALTER TABLE (幂等)
#     - 若 rd_001 仓库不存在 → 创建 db/warehouses/rd_001.db 并写入 warehouses 表
#   脚本本身 idempotent,已存在的列/仓库会跳过。失败不影响服务启动 (仅打 warning)。
#
# 依赖:
#   - docker-compose.yml 里加了 ../DailyCheck:/opt/dailycheck bind mount
#   - 容器端口 8080 / 5100 已映射到 host
set -euo pipefail

DAILYCHECK_DIR="${DAILYCHECK_DIR:-/opt/dailycheck}"
UNIT_DIR="/etc/systemd/system"

echo "==> 前置检查..."
[ -d "$DAILYCHECK_DIR" ] || { echo "!! DailyCheck 目录不存在: $DAILYCHECK_DIR" >&2; exit 1; }
[ -f "$DAILYCHECK_DIR/app.py" ] || { echo "!! 找不到 app.py, bind mount 没生效?" >&2; exit 1; }
command -v systemctl >/dev/null || { echo "!! 需要 systemd 容器" >&2; exit 1; }

export PATH="/opt/dailycheck/.venv/bin:/var/www/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# www-data 是 systemd unit 跑进程用的用户,容器重建后没了
id www-data >/dev/null 2>&1 || useradd -m -s /bin/bash www-data
echo "www-data ALL=(ALL) NOPASSWD:ALL" >/etc/sudoers.d/www-data 2>/dev/null || true

# DailyCheck 目录归属 www-data (容器重建后变 root)
chown -R www-data:www-data "$DAILYCHECK_DIR" 2>/dev/null || true
# /var/www 也要建, pip 装到 www-data 用户的 ~/.local
mkdir -p /var/www && chown www-data:www-data /var/www

# 确保 python deps 已装 (idempotent: 跳过已存在的)
# 注意: service 用 /opt/dailycheck/.venv/bin/python 跑, 依赖必须装进 venv
echo "==> 检查 Python 依赖..."
VENV_PYTHON="$DAILYCHECK_DIR/.venv/bin/python"
if [ ! -f "$VENV_PYTHON" ]; then
  echo "==> 创建 venv..."
  sudo -u www-data python3 -m venv "$DAILYCHECK_DIR/.venv"
fi
if ! "$VENV_PYTHON" -c "import flask, mcp_server" 2>/dev/null; then
  echo "==> pip install -r requirements.txt (安装到 venv)..."
  "$VENV_PYTHON" -m pip install --break-system-packages --no-cache-dir \
    -r "$DAILYCHECK_DIR/requirements.txt"
else
  echo "    flask + mcp_server 已装"
fi

# R&D 仓库迁移 (idempotent). 仅在 master.db 已 bind mount 进来时跑;
# 新 dev 环境 master.db 还没建, 跳过 (用户后续跑 `flask bootstrap` 即可).
if [ -f "$DAILYCHECK_DIR/db/master.db" ]; then
  echo "==> 迁移 R&D 仓库 (rd_001)..."
  if "$VENV_PYTHON" "$DAILYCHECK_DIR/scripts/migrate_add_warehouse_type.py"; then
    :
  else
    echo "    !! migrate 失败, 服务仍会启动; 手动跑:"
    echo "       $VENV_PYTHON $DAILYCHECK_DIR/scripts/migrate_add_warehouse_type.py"
  fi
fi

# 写环境变量文件 (secret 不入仓). dev 用弱默认值;
# production 用强 key + DAILYCHECK_ENV=production 覆盖此文件.
ENV_DIR="/etc/dailycheck"
ENV_FILE="$ENV_DIR/app.env"
mkdir -p "$ENV_DIR"
if [ ! -f "$ENV_FILE" ]; then
  echo "==> 生成 $ENV_FILE (dev 默认值)..."
  cat > "$ENV_FILE" <<'ENVEOF'
# DailyCheck app/MCP environment. Not committed to the repo.
# Production: set a strong DAILYCHECK_SECRET_KEY, set DAILYCHECK_ENV=production,
# and DELETE DAILYCHECK_MCP_TOKEN to force per-token (agent_tokens) auth.
DAILYCHECK_SECRET_KEY=dev-secret-key-not-for-prod
DAILYCHECK_MCP_TOKEN=dev-mcp-token-for-testing
ENVEOF
  chown root:www-data "$ENV_FILE" 2>/dev/null || true
  chmod 0640 "$ENV_FILE"
else
  echo "    $ENV_FILE 已存在, 跳过"
fi

# 写 systemd unit (模板从本仓库 deploy/systemd/ 拷到 /etc/systemd/system/)
echo "==> 安装 systemd units..."
mkdir -p "$DAILYCHECK_DIR/deploy/systemd"
install -m 0644 "$DAILYCHECK_DIR/deploy/systemd/dailycheck-app.service" "$UNIT_DIR/"
install -m 0644 "$DAILYCHECK_DIR/deploy/systemd/dailycheck-mcp.service" "$UNIT_DIR/"

systemctl daemon-reload
systemctl enable --now dailycheck-app.service dailycheck-mcp.service
systemctl restart wdg.target 2>/dev/null || true

# 验证
sleep 3
echo
echo "==> 服务状态:"
for s in dailycheck-app dailycheck-mcp; do
  status=$(systemctl is-active "$s" 2>&1)
  echo "    $s: $status"
done

echo
echo "==> 端口检查 (容器内):"
ss -ltn 2>/dev/null | grep -E ':(8080|5100)\b' || netstat -ltn 2>/dev/null | grep -E ':(8080|5100)\b' || echo "    (ss/netstat 都不可用, 跳过)"

echo
echo "==> 入口:"
echo "    Flask:  http://localhost:8080"
echo "    MCP:    http://localhost:5100  (Bearer: dev-mcp-token-for-testing)"
echo
echo "==> 日志:"
echo "    docker exec wdg-systemd journalctl -u dailycheck-app -f"
echo "    docker exec wdg-systemd journalctl -u dailycheck-mcp -f"
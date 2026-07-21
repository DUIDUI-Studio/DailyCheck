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

# 容器镜像默认 PATH 没把 /var/www/.local/bin 包进来, pip 装的 mcp flask 等命令找不到
export PATH="/var/www/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# www-data 是 systemd unit 跑进程用的用户,容器重建后没了
id www-data >/dev/null 2>&1 || useradd -m -s /bin/bash www-data
echo "www-data ALL=(ALL) NOPASSWD:ALL" >/etc/sudoers.d/www-data 2>/dev/null || true

# DailyCheck 目录归属 www-data (容器重建后变 root)
chown -R www-data:www-data "$DAILYCHECK_DIR" 2>/dev/null || true
# /var/www 也要建, pip 装到 www-data 用户的 ~/.local
mkdir -p /var/www && chown www-data:www-data /var/www

# 确保 python deps 已装 (idempotent: 跳过已存在的)
echo "==> 检查 Python 依赖..."
if ! sudo -u www-data python3 -c "import flask, mcp_server" 2>/dev/null; then
  echo "==> pip install -r requirements.txt (容器内首次或缺包)..."
  sudo -u www-data pip3 install --break-system-packages --no-cache-dir \
    -r "$DAILYCHECK_DIR/requirements.txt"
else
  echo "    flask + mcp_server 已装"
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
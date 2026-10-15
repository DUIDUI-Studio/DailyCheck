# 报告 4/4：本地部署预演报告（Local Deploy Preview）

> **用途**：在 Mac mini Docker 上模拟一次完整部署 + 关键路径冒烟，证明 PR 可在干净环境跑起来。
> **触发**：Dev Agent 每次 PR 前必出。
> **门禁**：compose up 全绿 + 关键页面 200/302 + 关键 API 一次往返成功。

---

## 1. 元信息

| 字段 | 值 |
|------|-----|
| PR | # |
| 分支 | `xxx` |
| 镜像 tag | `dailycheck:pr-<num>-<sha>` |
| compose 文件 | `docker-compose.preview.yml`（与 prod 同构） |
| Dev Agent | 标识 |
| 生成时间 | ISO8601 |

---

## 2. 环境对齐检查

| 项 | Mac mini | wdg_vps (prod) | 一致性 |
|----|---------|----------------|--------|
| Python | 3.13.12 | 3.12.x | ⚠️ minor |
| SQLite | 3.45 | 3.37 | ⚠️ minor |
| 系统包 | alpine 3.20 | ubuntu 22.04 | ⚠️ 已知差异 |
| gunicorn workers | 2 | 2 | ✅ |
| 监听端口 | 8091 | 8090 | ✅（隔离） |

**已知差异说明**：见 `docs/DEV_ENV.md` §环境差异，**不影响**预演结论。

---

## 3. 启动日志摘要

```
$ docker compose -f docker-compose.preview.yml up -d
[+] Running 4/4
 ✔ Network dailycheck-preview  Created
 ✔ Container preview-app        Started
 ✔ Container preview-mcp       Started
 ✔ Container preview-nginx     Started

$ docker compose ps
NAME              STATUS              PORTS
preview-app       Up (healthy)        0.0.0.0:8091→8080
preview-mcp       Up (healthy)        0.0.0.0:5191→8091
preview-nginx     Up                  0.0.0.0:8091→80

$ curl -I http://localhost:8091/login
HTTP/1.1 200 OK
```

---

## 4. 关键路径冒烟

| 路径 | HTTP | 截图 | 备注 |
|------|------|------|------|
| `/login` | 200 | `screenshots/login.png` | 默认 admin 在 |
| `/items` | 200 | `screenshots/items.png` | 首屏渲染正常 |
| `/api/items` POST 新增 | 201 | — | 一条记录写入成功 |
| `/api/mcp/list_tools` | 200 | — | MCP 工具可枚举 |
| `/store-ordering` | 200 | `screenshots/store_ordering.png` | 受影响页面回归 |

**门禁**：任一 5xx 或超时 → 阻塞 PR。

---

## 5. 资源基线

| 指标 | 启动后 1 分钟 | 启动后 5 分钟 |
|------|--------------|--------------|
| CPU | 12% | 6% |
| 内存 | 180 MiB | 195 MiB |
| DB 连接 | 3 | 3 |

---

## 6. 回滚演练（清理干净）

```
$ docker compose -f docker-compose.preview.yml down -v
[+] Running 4/4
 ✔ Container preview-nginx     Removed
 ✔ Container preview-mcp       Removed
 ✔ Container preview-app       Removed
 ✔ Network dailycheck-preview  Removed
```

无残留进程、无残留 volume。

---

## 7. 报告签名

```
Dev Agent: <name> <commit-sha>
报告生成时间: <ISO8601>
下次预演：可重复执行，结果幂等
```

> **门禁**：冒烟任一失败 / 资源异常 / 清理失败 → 阻塞 PR。
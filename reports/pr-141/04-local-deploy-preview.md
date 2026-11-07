# 报告 4/4：本地部署预演

## 1. 元信息

PR #141: http://hub01:3000/eric/dailycheck/pulls/141；Issue #140，分支 `feat/issue-140-wms-owner-details`，代码候选 `1f995e2f0c6b962e27551c309e752ce8149ad9df`。
此报告记录本地 standalone Flask 预演。项目 `DEV_ENV.md` 允许 standalone 用于独立测试，不声称运行 Docker 或生产候选部署。

## 2. 环境对齐检查

本地 macOS、Python 3.12.13、SQLite 3.50.4、独立 `.venv`，使用临时应用库和合成 WMS schema/数据。服务监听 `127.0.0.1:5140`。没有载入 WMS 凭据或访问第三方库。生产配置、网络白名单、SQL Server 和真实页面由 Prod 验证。

## 3. 启动日志摘要

命令 `.venv/bin/python reports/issue-140/preview_app.py` 启动 Flask/Werkzeug，原始脚本及 stdout 已保存外部证据目录。启动行：

```
STUB UPSTREAM ONLY; temporary local DB; http://127.0.0.1:5140/_fixture_login
Running on http://127.0.0.1:5140
```

原脚本只在本地建立已登录 DC viewer 会话、挂接真实 Flask/Jinja 和 SQL 方言适配合成库，并加入“本地验证 · 合成 WMS 数据（非上游实数）”提示。生产源码没有加入 fixture 登录入口或测试数据。

## 4. 关键路径冒烟

| 路径/动作 | 结果 | 证据 |
|---|---|---|
| 默认库存、货主选择、下一页 | 200；保留 owner=200、limit=5，页 1 键 2/4/6/8/10，页 2 键 12/14/16/18/20 | browser-page2-proof.json、browser-mobile-page2-proof.json |
| 入库列表点击唯一 ID 7 -> 详情 -> 原筛选返回 | 200；头、货主、单位、数量、批次与库位可见 | desktop-inbound-detail.png、browser-inbound-proof.json |
| 出库列表点击唯一 ID 7 -> 详情 -> 返回 | 200；手机 390×844、coarse pointer；详情及库位分配可滚动查看 | mobile-outbound-detail.png、mobile-outbound-lines.png、browser-mobile-detail-proof.json |
| 手机库存 | 页面无横向溢出，宽表单独横向滚动 | mobile-stock.png、browser-mobile-stock-proof.json |
| 控制台 | 无日志条目 | browser-console.json |
| 未登录/非 DC/错误标识/越界/异常 | 测试覆盖 302/403/400/404/502/503，均无敏感回显 | focused-tests.txt、tests/test_wms_dashboard.py |

截图及浏览器调试 JSON 为本地运行工件，不提交 Git；文件名均相对于外部 writer-evidence 目录，路径见 verification.md。

## 5. 资源基线

未做资源或性能基准。此报告只证明本地受影响用户流程。查询延迟和多货主真实数据规模由正式候选及生产观察窗判断。

## 6. 回滚与清理

仅有代码、页面和文档变更，无数据库迁移或库存写入。预演服务停止后确认原生退出，临时库随上下文清理；生产回退按标准流程由 Prod 独立执行。

## 7. 报告签名

Dev Agent: Codex integration / task_c1ad7027772a / ctx_78107c9eea94。
2026-11-03。此为开发阶段证据，不是生产交付终态。

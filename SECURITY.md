# 安全策略

## 报告漏洞

**不要用公开 issue 报告安全漏洞。**

- 联系方式：仓库管理员 `eric`（自托管 Gitea，`http://hub01:3000`）——站内或直接找 Eric 取私密渠道。
- 响应承诺：48 小时内确认收到，7 天内给出初步评估。
- 请提供：影响范围、复现步骤（PoC 代码可加密附件）、你的联系方式。

## 范围

- DailyCheck Web 应用（Flask）、MCP Server（stdio / HTTP）、部署脚本。
- 不在范围：依赖库自身的漏洞（请直接报给上游，我们跟进升级）。

## 已知安全设计

- 鉴权：Flask session（HttpOnly + SameSite=Lax，prod Secure）+ MCP Bearer
  （pbkdf2 哈希 + per-token 仓库/路径 ACL）。
- CSRF：全站 form token（Flask-WTF）；rate limit：全局 IP + 登录锁定 + MCP per-token 配额。
- 迁移/部署：schema 迁移前自动热备；部署脚本数据库零触碰、健康检查失败自动回滚。

## 安全更新

安全修复走 `fix/security-*` 分支，CI 门禁通过后由所有者手动部署
（CI 由 Gitea Actions 完成；main 受保护，只能走 PR + 必需 check）。

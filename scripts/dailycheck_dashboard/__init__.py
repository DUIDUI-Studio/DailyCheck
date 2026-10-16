"""scripts.dailycheck_dashboard —— 项目任务仪表盘（PR webhook 触发）。

子命令：
  render  拉取 gh + git + 本地状态，渲染 HTML + 落 SQLite 快照
  history 列出最近快照（--limit N，--since 7d）
  diff    最近两次快照 KPI 对比（v1：仅 KPI 行 +N/-N）
"""

__version__ = "0.1.0"

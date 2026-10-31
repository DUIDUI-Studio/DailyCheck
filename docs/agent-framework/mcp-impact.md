# PR 的 MCP 影响声明

本规则由 workflow.md「MCP 影响评估」定义。使用 PR 模板的唯一标记块，JSON version=1、
changes 非空；每项字段为 category、tools、reason、evidence，删除/重命名另加 compatibility。
category 只能是新增/更新/不变/暂不开放。evidence 为非空字符串数组，填写实际测试命令与结果。

例如页面改动沿用查询，不需新工具：

```json
{
  "version": 1,
  "changes": [{
    "category": "不变",
    "tools": [],
    "reason": "只改页面呈现，查询、计算和权限仍沿用既有 MCP 契约",
    "evidence": ["python -m pytest tests/test_page.py: 3 passed（替换为本次实际结果）"]
  }]
}
```

暂不开放同样要写原因与证据，例如新页面依赖尚未稳定的工作流，当前不提供外部工具。
这些理由必须与实际 diff 相符，由独立审查核实，不能用声明替代真实验证。

新增/更新写统一台账内的准确名称。例如已有 items_list 输入契约变更归「更新」，tools 为
["items_list"]；实际新增工具用 HEAD 台账中的新名称归「新增」，不会因旧台账没有该名被拒绝。
一次 PR 可以分别列多项，必须覆盖每个实际变化的工具；声明工具不可重复。

删除或重命名示例（仅说明格式，名称必须替换为实际台账工具）：

```json
{
  "version": 1,
  "changes": [
    {
      "category": "更新",
      "tools": ["old_name"],
      "reason": "旧工具名退出使用",
      "evidence": ["迁移回归命令与实际结果"],
      "compatibility": "已迁移全部调用者到 new_name；旧名的移除时间与消费者确认记录见关联 Issue"
    },
    {
      "category": "新增",
      "tools": ["new_name"],
      "reason": "替代旧工具名",
      "evidence": ["新工具协议回归命令与实际结果"]
    }
  ]
}
```

以下情况拒绝：缺标记块、非法/重复 JSON、类别「无影响」、未知工具名、空/待填理由或证据、
schema/ACL/输出等台账实际变化却写不变、仅覆盖部分变化、删除旧名却未写 compatibility。
compatibility 声明不是兼容性结论；实际协议兼容仍需专项回归验证。

本地先运行 `python -m scripts.mcp_contracts --check`，再运行
`python scripts/agent_framework/check_mcp_impact.py --base origin/main --body-file /tmp/body.md`。
CI 在既有 reports job 执行相同两步，base 固定为事件提供的 main SHA，内部取 merge-base；
不能用其他基线把变化隐藏。不要求增加 branch protection，也不修改 known_failures 或 Bot 权限。

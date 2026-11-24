# 运行结果与验收

终端中的「运行结果」来自运行器。脚本使用 `luban run "任务" --json` 时，查看 `outcome` 和 `run` 事件。模型回复、文件改动、检查结果分别记录。只有本轮检查通过才标记为 `verified`。收到截断、内容过滤或没有结束原因的流时，本次响应里的工具不会执行。

## 计划、恢复与附件

用 `/plan on` 制定计划，用 `/plan execute` 执行。步骤需要成功的工具回执才会继续，下游依赖由执行器检查。计划可指定读取、修改或检查证据。执行中可以修订尚未开始的步骤。中断或阻塞后，按提示运行 `/resume` 和 `/continue`。多角色任务会复用已完成阶段的产物。旧版本留下的执行记录按原有日志恢复。

`@路径:起始行-结束行` 为附件。大段源码优先保留路径、行段和版本，小窗口下按需读取。恢复时若文件版本已变化，会提示重新读取。

## 验证基线

在本会话当前模型上启用 `/runtime verification_baseline true`。任务开始前会请求授权运行发现的项目检查，随后比较修改前后的失败证据。证据一致的既有失败会列在报告中。未授权、超时或没有诊断输出的检查无法建立基线。默认关闭，避免普通问答或小改动也支付整套检查的耗时。

## MCP 外部结果

没有明确回执的外部错误会暂停运行。可以使用 `/trace` 核对工具调用，再通过服务端确认结果，使用 `/ack` 后继续。

仅当服务端保证同一操作键去重时，才在信任的 `.luban/mcp.json` 服务器配置中配置 `idempotency`。配置变化后重新信任。可选的 `receipts` 指定只读查询，用于优先核实未知结果。以下字段名和工具名由 MCP 服务定义：

```json
{
  "example": {
    "command": "example-mcp",
    "idempotency": {"create_item": "idempotency_key"},
    "receipts": {"create_item": {"tool": "lookup_receipt", "field": "operation_id"}}
  }
}
```

回执工具的 `structuredContent` 需返回与查询键相同的 `operation_id`，以及 `status`（`succeeded`、`failed` 或 `unknown`）。无可信回执时，只有已配置幂等字段的工具可以在超时后用原键重试一次。

## 真实端点评测

```sh
luban eval https://example.invalid/v1 YOUR_MODEL --env LUBAN_EVAL_API_KEY --cases 1
```

评测只把临时 Git 仓库中的合成任务发给所选端点。按 JSON 产物、原文件保留情况和权限拒绝评分。允许不同编辑路径。命令和外部工具不授权。与自动化测试中的脚本化模型分开运行，输出 JSON，任何用例失败时退出码为 1。

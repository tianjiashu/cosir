# Tool 契约

## 工具结果构建契约

工具执行结果统一通过 `ToolObservation` 表达，并由统一工厂构建：

- 成功使用 `tool_success`；
- 失败使用 `tool_error`；
- 取消使用 `tool_cancelled`。
- app.core.workflows.workflow_operations.WorkflowOperations._to_model_message 统一根据不同状态，构建最终llm可见结果。
### `success`
- `status="success"`；
- `content` 只返回模型继续工作所需的信息；
- 没有额外信息时，模型消息为None 即可；
- 完整 diff、文件列表等 UI 数据放入 `display_data`；
- 文件快照、ChangeSet、回退数据放入 `artifact_data`。

### `error`

错误消息只保留关键信息：

```text
error: <发生了什么>
retryable: true|false
reason: <处理建议，不要包含must 等强制要求的信息>
```

字段职责：

- `error`：只描述错误事实，不写修复建议；
- `retryable`：仅作为模型提示，表示按 `reason` 修正或处理后是否可以再次调用；不触发自动重试，也不要求使用相同参数；
- `reason`：只描述下一步动作，不重复 `error`。

`retryable=True` 的错误可以是参数修正、状态修正或暂态故障；`retryable=False` 时，模型应停止、换方案或请求用户介入。已经产生部分副作用但结果不确定时，不应允许原样重放。

### `cancelled`

- `status="cancelled"` 表示用户或系统主动中止，不是工具故障；
- 不携带 `retryable` 提示；
- 模型消息只说明取消原因；
- 如果底层消息框架不支持 `cancelled` 状态，在边界层做兼容映射，但必须保留取消语义。

### 数据边界

- `content`：模型可见的必要文本或结构化结果；
- `error` / `reason`：模型可见的错误事实与处理建议；
- `display_data`：前端只读展示数据，不进入模型消息；
- `artifact_data`：后端快照、审计和回退事实，不进入模型消息或 Transport。


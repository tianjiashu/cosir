# Tool 契约

## 工具结果构建契约

核心原则：工具返回不是越详细越好，而是要让模型知道当前发生了什么、是否还能尝试，以及下一步应该做什么。

工具执行结果统一通过 `ToolObservation` 表达，并由统一工厂构建：

- 成功使用 `tool_success`；
- 失败使用 `tool_error`；
- 取消使用 `tool_cancelled`。
- WorkflowOperations._to_model_message 统一根据不同状态，构建最终llm可见结果。根据retryable true or false,添加hint: <可以重试并由模型结合上下文判断，或不要重试当前调用>。
  - `retryable=True` 的错误可以是参数修正、状态修正或暂态故障；`retryable=False` 时，模型应停止、换方案或请求用户介入。已经产生部分副作用但结果不确定时，不应允许原样重放。
- ToolObservation.display_data 仅承担对llm不可见的UI所需数据
- ToolObservation.artifact_data 仅承担对 llm 不可见的工具内部产物；文件展示数据不从此字段传递。

### `success`
- `status="success"`；
- `content` 只返回模型继续工作所需的信息（可以为None），不应和llm 调用的参数存在重复信息，可承担工具结果 or 额外提示。如WebExtractTool承担工具结果，ApplyPatchTool 成功时为None或者额外语法错误警告；

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
- `hint`：`True` 时提示可以重试但由模型结合上下文判断；`False` 时提示不要重试当前工具调用；
- `reason`：只描述下一步动作，不重复 `error`。
### `cancelled`

- `status="cancelled"` 表示用户或系统主动中止，不是工具故障；
- `tool_cancelled` 不接收调用方传入的 `reason` 或 `error`；
- 统一使用 `reason="the tool call was cancelled before completion; no result was produced."`；
- `content` 和 `error` 均为 `None`；
- 不携带错误重试提示；
- 如果底层消息框架不支持 `cancelled` 状态，在边界层做兼容映射，但必须保留取消语义。

## 工具参数宽容归一契约

模型下发的参数在进入校验前先做一次「形态适配」，用于消除「把数字/布尔写成字符串」这类无意义往返；
**strict 校验仍是唯一判定**——适配只改输入形态，不改变任何校验规则。

- 适配入口：`validation/argument_coercion.py::coerce_tool_arguments`（纯函数，目标类型只来自
  `args_model` 字段注解），由 `validation/arguments.py::validate_tool_arguments` 在 strict 校验前调用。
- 白名单（仅无损、无歧义）：`"30"`→数字、`30.0`→整数、`"true"`/`"false"`→布尔（忽略大小写与空白）、
  单值→单元素列表（元素按声明元素类型递归同规则）。
- 明确不归一：数字→字符串、`"1"`/`"0"`→布尔、`""`→`None`、`Literal` 枚举字段、未知字段、
  空串/纯空白→列表（避免构造长度合法但内容为空的列表而绕过 `min_length`）。
- 容器语义：只处理 `list[T]`；`tuple` 在元素无需归一时原样保留；嵌套 `list[list[T]]` 按元素
  注解递归归一。
- 不放宽的安全与业务约束：范围（`gt/ge/le`）、长度、枚举、`extra="forbid"`、模型内业务校验
  （如 `delegate_task.agent_name` 长度上限）、handler 内路径与命令安全策略。
- 可观测：发生适配时由门禁记 `tool_argument_coerced`（warning），字段为
  `tool` / `tool_call_id` / `coercions[].{field,from_type,to_type}`；**不记录参数值**。
- 幂等：合法原生参数不产生适配事实。

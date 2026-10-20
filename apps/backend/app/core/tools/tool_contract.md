# Tool 契约

## 模型可见参数契约

- 所有工具统一导出 `strict=True` 的 function 定义。参数 schema 复用 OpenAI SDK
  `pydantic_function_tool` 生成：对象封闭、所有属性列入 `required`；可空字段用 `null` 表达。
- 平台描述与枚举收窄基于统一 strict schema 生成；注册后随 Task 固化，模型调用期间不重写。
  旧 Task schema 不升级或补标记，需要新建 Task。
- 模型须显式填写所有字段；参数模型的默认值仍服务本地调用。模型侧 strict 不替代工具入口的
  Pydantic 校验与业务约束，也不同于下文的本地参数宽容归一。
- MCP 的开放参数通过 `arguments` JSON 对象字符串传入，在参数入口解析为对象；Agent Team
  的节点目标通过包含 `node_id`、`goal`、`summary` 的列表传入，再转换为领域映射。
  两者均不接受旧模型输入格式。Team 的用户批准数据与持久化领域映射不变。
- 上下文保存模型原始参数，归一化后的执行参数不反写上下文；本次契约不改变等待与恢复边界。

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
- 模型侧使用 OpenAI SDK 的 `tool` 消息格式，取消语义写入正文；生命周期状态由 observation 与
  Transport 各自的既有契约承载，不向 SDK 消息追加 `status` 字段。

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



## Tool 描述撰写标准

先按读者分层，避免把给 Agent 的操作说明和给开发者的实现说明混在一起。

| 位置 | 读者 | 应写内容 |
|---|---|---|
| `ToolDefinition.description` | Agent | 工具做什么、适合何时使用、关键输出形式，以及会影响调用决策的限制或副作用 |
| 参数 `Field.description` | Agent | 该参数的语义、格式、路径基准、单位、索引方式、默认值含义或与其他参数的关系 |
| Python docstring / 注释 | 开发者 | 契约归属、上下游保证、事实源、副作用、错误转换、生命周期及不明显的设计原因 |

### 写作规则

1. **先说调用决策。** 描述工具的用途和适用场景；当相近工具容易混淆时，说明如何选择。无需复述工具名或显而易见的实现步骤。
2. **工具描述与参数说明互补。** 工具描述讲整体能力和调用结果；参数说明只讲单个参数。不要在两处重复路径规则、分页方法等内容。
3. **参数说明补足 schema 没表达的语义。** 类型、必填、默认值、取值范围和枚举由 schema 表达；文字解释容易误解的含义，例如路径相对哪个根目录、数字从 0 还是 1 开始、默认值代表什么。
4. **只写会改变 Agent 决策的信息。** 重要的输出格式、分页续读方式、硬限制、不可逆操作、副作用或人工确认门槛要说明；内部类名、函数流程、日志实现等留在开发者文档。
5. **说明必须与实际行为和注册名一致。** 不要引用未注册的工具，也不要承诺工具不具备的能力。修改工具时应一并核对成功输出、失败恢复建议和参数 schema。
6. **简洁且可执行。** 优先使用明确动词、边界和具体单位；示例只用于消除歧义，不堆参数清单或重复规则。同一工具内保持术语和语言一致。

### 可用模板
- **工具描述：** `[能力与对象]。适用于[场景/与相近工具的选择]。返回[重要结果]。受[关键限制/副作用]约束。`
- **参数描述：** `[参数含义]；[格式、基准、单位或索引语义]；[默认值、参数关系或举例，如有必要]。`
- **开发者 docstring：** 用中文记录调用链契约、事实源、副作用和失败处理；省略显而易见的代码复述。

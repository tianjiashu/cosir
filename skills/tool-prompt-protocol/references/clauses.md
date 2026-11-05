# 工具提示词协议 · 详细条款

## 1. 装配链：模型到底看到什么

理解链路是为了知道哪一处文本会被发送、哪一处不会。

- 工具描述的事实源是 `HandlerBase.description` 类属性（`app/core/tool/tool_base.py`），经 `ToolDefinition.to_model_tool_definition()` 投影为 `{name, description, strict, parameters}`（`app/core/tool/schemas/tool_definition.py`）。
- 参数 schema 的唯一生成边界是 `strict_parameters_schema(args_model)`（`app/core/tool/schemas/model_tool_schema.py`）。pydantic 会把类 docstring 写成根对象与 `$defs` 条目的 `description`；该边界统一剥离这些模型级描述，所以模型可见说明只来自字段描述，docstring 只服务源码阅读。
- 平台化收窄在投影阶段完成：投影方可在 `parameters_schema` 上追加说明（先例见 `execute_terminal.py` 把本机可用 shell 追加进字段描述）。这是注入动态事实的唯一合法位置。
- 描述随 Task 固化进 `tasks.tool_schemas`（`app/core/tool/multi_agent/task_tool_scope.py`）。**描述是快照契约**：改文案不影响已存在的 Task，因此字面量里写死会变的事实，是长期错误而非短期瑕疵。
- 同一份 `description` 也被配置中心 UI 复用（`app/api/tools_api.py`）。写模型文案时需知道读者只有 Agent，UI 复用是既有取舍，不构成放宽标准的理由。

## 2. 「该不该进」的判据推导

错误的划分方式与后果：

| 划分方式 | 后果 |
|---|---|
| 按「是否内部标识」删 | 误删 `task_id` 这类 Agent 寻址必需字段，Agent 无法指定目标 |
| 按「是否开发者术语」删 | 误删 `session_id`、`offset` 游标，Agent 无法续做 |
| 按「是否重要」保留 | 判据无边界，最终什么都保留，token 预算失效 |

正确判据：**缺失会让 Agent 下一步动作做错，则进；只影响人类排查，则不进。**

按此判据的归属：

- 进模型通道：寻址键、状态枚举、边界的真实含义、失败后的可执行动作、会改变决策的副作用。
- 不进模型通道：调用 id、trace id、原始异常、内部产物路径、耗时、字节数、重试计数。

## 3. 三层归属的判定流程

对每条待写信息依次问：

1. 它是**某个参数**的属性（含义/格式/范围/示例）？→ 该字段 description。
2. 它是**某几个参数的组合用法**？→ 仍属参数层，写在同一工具的字段描述里并互相指认，**不要**升级为工具描述。
3. 它是**何时选这个工具而非另一个**？→ 工具 description，并写在被放弃方。
4. 它是**多个工具共同遵守的纪律**？→ 系统提示词 `work_layer`。
5. 它是**当前 Agent 有哪些扩展能力**（子 Agent、Team、MCP、Skill 目录）？→ 系统提示词 `tool_layer`。

第 3 与第 4 条是争议点，判据是决策视野：模型逐个读 `description` 决定调谁，此时它手里只有该工具的文本。所以「选我还是选它」必须出现在工具描述里；而「所有文件工具都别用 shell 代替」不属于任何单个工具，写进系统提示词。

重复的判定：同一语义若在两处出现，保留在**更靠近决策点**的一处，删除另一处。反例是 `replace_tool` 描述里的「别用 sed/awk」与 `work_layer` 里的「别用 sed/grep/find/ls」并存。

## 4. 参数描述：简单描述 vs 需要例子

判据是**取值空间能否用一句话说清**。

只需一句话：

```
limit: int = Field(default=50, ge=1, description="Maximum files to return.")
offset: int = Field(default=0, ge=0, description="Number of matching files to skip.")
```

必须给例子：结构化文本格式、嵌套对象、多字段协同、glob/正则语法。

```
patch: str = Field(
    description="<一行说明接受什么格式、什么被拒绝、拒绝时改用哪个工具>",
    examples=["<最小可运行示例，每行自带 \\n>"],
)
```

写法要点：

- 先写**接受什么**，再写**拒绝什么并改用哪个工具**，最后才是示例。拒绝清单比接受清单更能防错。
- 例子取最小可运行形态，不要写完整业务的真实数据。
- 多行例子必须每行自带 `\n`；相邻字符串字面量会被直接拼接，漏掉换行会塌成一行，模型照抄即被解析器拒绝。

## 5. 工具描述：只写决策所需

工具描述应覆盖且仅覆盖以下要素：

1. 它做什么（一句，动词开头）。
2. 执行语义对决策的影响：是否有状态、是否阻塞、超时行为、副作用范围。
3. 何时不该用它，以及该用什么。
4. 与兄弟工具的分界。

不应出现在工具描述里：

- 单个参数的含义与格式（字段描述负责）。
- 完整的格式样例（字段描述负责，重复必然漂移）。
- 平台细节（如本机 shell 名，由投影注入）。
- 全工具通用的纪律（系统提示词负责）。
- 面向人的说明（复杂度、内部实现、性能权衡）。

压缩手法：

- 用「Use X for ..., not for ...」一句同时表达选择与排除。
- 失败后果写可执行动作而非描述：「Narrow the path or pattern and call again」优于「搜索可能超时」。
- 同一个语义只用一种句式，避免同义词堆叠。

## 6. 输出侧

`ToolObservation.content` 与描述受同一判据约束：只放 Agent 后续动作所需的事实。

- 分页：给「已给到第几段 / 总量 / 下一页游标」，不给绝对路径与字节数。
- 截断：给「还有 N 条，用 offset=X 继续」。
- 失败：给原因与下一步动作，不给堆栈；原始异常进日志。
- 取消：给「已停止、可能有部分副作用、不要自动重试」。

## 7. 本仓已收敛项与仍需判断的点

已按本协议收敛：

- `apply_patch_args.py` 的 `patch` 字段描述改为英文，格式契约仍是唯一事实源。
- 模型级 description 由 schema 生成边界统一剥离，参数模型的类 docstring 不再泄漏成模型可见文本。
- `replace_tool.py` 的工具描述删去实现细节（模糊匹配策略数）、UI 说明、参数清单与 strict 契约矛盾的 "optional" 措辞，也不再重复 `work_layer` 的 shell 禁令。
- 取消占位文案不再携带 `tool_call_id`。
- 全量文案守卫：`tests/test_strict_tool_contract.py::test_registered_tool_prompts_are_english_and_documented`（无 CJK、字段必有描述、同层不重复、描述非空且无首尾空白）。

仍需判断：

- `tool_handler_runner` 把 handler 异常的 `str(exc)` 写进 `content`：异常里可能有可行动信息（权限、不存在），也可能是无意义的内部错误。当前保留，可排查性由同处的 `log.error` 兜住。
- `tool_output_budget` 把 `.cosir/tool-artifacts/<uuid>.txt` 路径写进正文：按判据必须保留——模型要靠它 `read_file` 取全文，不属于泄漏。
- MCP 的 `arguments`、terminal 的 `input` union 等结构化参数已用 prose 说清取值空间，按判据无需例子；新增结构化参数时再按第 4 节判据补。

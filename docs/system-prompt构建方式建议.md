# System Prompt 构建方式建议

生成时间：2026-07-29

调研对象：

- `G:\code\opencode`
- `H:\coding-agent\apps\backend\app`

约束：本文只做调研与方案建议，不改动代码。

---

## 一、结论

建议不要把 OpenCode 的多 prompt 文件体系原样照搬到本项目。OpenCode 已经是成熟 CLI coding-agent，存在 provider prompt、agent prompt、mode reminder、tool description、skill、MCP、compaction 等多层动态上下文。本项目当前后端仍处于 `text_only_v1` + ReAct-like Workflow 骨架阶段，应该先做一个小而清晰的 `SystemPromptBuilder`，把现在 `TextContextBuilder._build_system_prompt()` 里的字符串拼接升级成“分段、可测试、可扩展”的构建器。

推荐方向：

```text
AgentProfile 仍是 Agent 身份与目标事实源
ToolDefinition 仍是工具模型可见 schema 的事实源
TextContextBuilder 仍负责构建 RuntimeMessage 列表
新增 SystemPromptBuilder 只负责构建 system message 文本
LangChain bridge 仍只做 RuntimeMessage -> LangChain message 转换
```

也就是说，prompt 构建能力应收口在 `app.core.context`，不要下沉到 `tools`，也不要绑死到 LangGraph 节点。

---

## 二、OpenCode 的可借鉴点

OpenCode 的上下文构建是分层装配，而不是单一大 prompt。关键源码包括：

- `G:\code\opencode\packages\opencode\src\session\system.ts`
- `G:\code\opencode\packages\opencode\src\session\prompt.ts`
- `G:\code\opencode\packages\opencode\src\session\instruction.ts`
- `G:\code\opencode\packages\opencode\src\session\llm\request.ts`
- `G:\code\opencode\packages\opencode\src\agent\agent.ts`
- `G:\code\opencode\packages\opencode\src\tool\registry.ts`
- `G:\code\opencode\packages\opencode\src\tool\tool.ts`
- `G:\code\opencode\packages\opencode\src\session\reminders.ts`
- `G:\code\opencode\packages\opencode\src\session\compaction.ts`

OpenCode 最终 system prompt 的高层顺序是：

```text
agent.prompt 或 provider prompt
environment
instructions
MCP instructions
skills
structured output prompt，可选
user.system，可选
```

值得借鉴的机制：

1. **分层构建**：身份、环境、项目规则、工具策略、动态提醒分开生成，最后按固定顺序合并。
2. **Agent prompt 优先于 provider prompt**：如果专职 agent 有自己的 prompt，就不再使用通用 provider prompt。
3. **工具描述不塞进 system prompt**：工具描述跟随 tool schema 暴露给模型，system prompt 只写工具使用原则。
4. **动态 reminder 不是常驻 prompt**：plan/build 切换、max steps、structured output 这些运行态约束按需注入。
5. **compaction 是会话级摘要，不是每个工具结果都摘要**：普通工具结果先确定性截断与 artifact 落盘，只有上下文溢出时才用隐藏 agent 总结旧历史。
6. **provider 差异是 overlay，不是业务事实源**：不同模型的提示词措辞可以不同，但不应改变系统核心规则。

不建议当前照搬的部分：

1. **大量 provider txt**：本项目第一阶段主要 DeepSeek/OpenAI 协议，不需要立刻维护十多个模型专用 prompt。
2. **MCP/Skill prompt 注入链**：本项目虽规划 MCP，但后端当前系统 prompt 还没有成熟 instruction/skill 体系，先预留接口即可。
3. **command template 体系**：本项目不做 CLI，桌面客户端是主入口，不应引入 OpenCode 的 command markdown 机制。
4. **过早做完整 instruction discovery**：可以先支持项目根规则文件，后续再扩展到按文件读取时的局部规则注入。

---

## 三、本项目当前上下文构建现状

当前后端相关边界：

- `apps/backend/app/core/agents/agent_profile.py`
  - `AgentProfile` 持有 `agent_id`、`role`、`system_prompt`、`allowed_tools`、`context_policy`、`workflow`、`model_name`、`model_settings`、`max_steps`。
  - 默认模型已经是 DeepSeek：`deepseek-v4-flash` / `deepseek-v4-pro`。

- `apps/backend/app/core/context/text_context_builder.py`
  - `TextContextBuilder.build_messages()` 构建模型无关 `RuntimeMessage`。
  - 当前 `_build_system_prompt()` 是一段直接字符串拼接，内容较薄。

- `apps/backend/app/models/runtime_message.py`
  - `RuntimeMessage` 是模型无关消息协议，只有 `role`、`content_text`、`metadata`。

- `apps/backend/app/core/llm/langchain_bridge.py`
  - 唯一负责 `RuntimeMessage` 到 LangChain message 的转换。
  - `ToolDefinition.to_model_tool_definition()` 是工具 schema 投影入口。

- `apps/backend/app/core/workflows/react/workflow.py`
  - Workflow 只负责 LangGraph 编排。
  - 初始化时调用 `operations.build_messages()`，再转 LangChain 消息进入 graph state。

- `apps/backend/app/core/workflows/react/nodes.py`
  - `model` 节点直接消费 `state.messages`。
  - `tools` 节点执行工具后把 `tool_run.messages_for_model` 追加回 state。

- `apps/backend/app/tools/guard/tool_output_budget.py`
  - 工具输出已经有统一字符预算、脱敏、artifact spill。
  - 这部分应通过 system prompt 告诉模型“看到截断时如何继续查 artifact”，但不应把 artifact 全文塞进 system。

当前问题：

1. system prompt 内容太薄，只包含 agent id、role、目标、允许工具、context policy。
2. 没有分段结构，后续加入环境、规则、工具策略、DeepSeek 行为约束时容易变成大字符串。
3. 没有明确区分“系统级规则”和“工具 schema 描述”。
4. 没有 provider/model overlay，DeepSeek reasoning / chat 模型差异只能靠同一段 prompt。
5. 没有把项目规则、工具输出截断、审批、checkpoint、turn 历史等运行时事实显式告诉模型。

---

## 四、推荐架构

建议新增一个轻量构建层，不改变现有消息与 workflow 边界。

建议职责：

```text
app.core.context
  text_context_builder.py       # 仍负责 RuntimeMessage 列表
  system_prompt_builder.py      # 新增：构建 system prompt
  prompt_section.py             # 可选：PromptSection 值对象
  prompt_context.py             # 可选：SystemPromptContext 值对象
  prompt_templates/             # 可选：少量 markdown/txt 模板资产
```

如果坚持最小改动，第一阶段只需要：

```text
app.core.context.system_prompt_builder.SystemPromptBuilder
```

可选值对象可以等第二阶段再引入，避免过早抽象。

### 4.1 SystemPromptBuilder

职责：

- 从 `AgentProfile`、当前 `TurnRecord`、工具定义、执行上下文、运行时环境生成 system prompt 文本。
- 只产出字符串，不调用模型，不读写数据库，不执行工具。
- 固定 section 顺序，便于测试。

不负责：

- 不转换 LangChain message。
- 不筛选工具权限，工具筛选仍由 `AgentProfile.select_tools()` / `RuntimeOperations` / `ToolExecutionService` 负责。
- 不拼接历史消息，历史消息仍由 `TextContextBuilder` 加载。
- 不处理 LangGraph state。

### 4.2 Prompt Section 顺序

建议固定顺序：

```text
1. Identity / Role
2. Mission / Goal
3. Operating Principles
4. Workflow Contract
5. Tool Use Policy
6. Context Policy
7. Environment
8. Project Instructions
9. Output Contract
10. Provider Overlay
11. Runtime Reminders
```

说明：

- `Identity / Role` 和 `Mission / Goal` 来自 `AgentProfile`。
- `Tool Use Policy` 只写使用原则，不重复工具 schema。
- `Environment` 放 workspace、platform、date、model、turn/task id 等运行事实。
- `Project Instructions` 后续可接入 `AGENTS.md` / 项目规则。
- `Provider Overlay` 用于 DeepSeek / OpenAI 协议差异。
- `Runtime Reminders` 用于 max steps、工具错误上限、plan/review 模式等动态约束。

### 4.3 和 OpenCode 的对应关系

| OpenCode 机制 | 本项目建议 |
|---|---|
| provider prompt txt | 先做 `ProviderOverlay`，DeepSeek 专用规则少量覆盖 |
| agent prompt | 继续放在 `AgentProfile.system_prompt`，后续支持 profile 配置化 |
| environment block | 新增 `Environment` section |
| instructions | 先支持项目根 `AGENTS.md` / `rules` 摘要，后续做局部规则 |
| MCP instructions | 暂不实现，预留 section |
| skills | 暂不实现，预留 section |
| tool description txt | 不塞 system，继续由 `ToolDefinition` 投影 |
| reminders | 后续用 `RuntimeReminders` section |
| compaction | 后续单独做 `ContextCompaction`，不要塞进 SystemPromptBuilder |

---

## 五、推荐的 system prompt 结构

下面是建议生成出来的 system prompt 形态。

```text
你是 coding-agent 的本地开发 Agent。

<identity>
Agent ID: developer
Role: developer
Model: deepseek-v4-flash
</identity>

<mission>
完成本地 coding-agent 任务；优先保持代码清晰、可诊断、可扩展。
</mission>

<operating_principles>
- 你运行在用户本机的桌面 coding-agent 中，主要目标是完成工程任务。
- 优先理解现有代码结构，再提出或执行改动。
- 保持改动最小化，遵守项目已有目录边界、命名规则和工具链。
- 不把猜测当事实；涉及行为、边界、配置和测试时，以源码和工具结果为准。
- 遇到工具输出被截断时，不要臆测缺失内容，应按 artifact 路径或 offset/limit 继续读取。
</operating_principles>

<workflow_contract>
- 默认工作流是 ReAct-like：先分析当前任务，再按需调用工具，最后给出可执行结论。
- 如果需要查代码，先使用 read/search/list 类工具定位，再精读关键文件。
- 如果模型请求工具，必须等待工具结果后再继续判断。
- 如果工具连续失败，应收敛范围或解释阻塞原因，不要重复提交同一类无效调用。
- 达到 max_steps 或工具错误上限时，应停止扩展并总结已完成内容、剩余问题和下一步。
</workflow_contract>

<tool_policy>
Allowed tools: read_file, list_directory, search_files, write_file, patch, delete
- 工具 schema 是工具参数的唯一事实来源；调用工具时严格遵守 schema。
- 文件读取、搜索、修改优先使用专用工具，不用终端命令替代。
- 写入、删除、patch 属于高风险操作，必须基于已确认路径和当前任务目标。
- 工具输出可能被预算截断，完整输出可能保存为 artifact；需要时按路径继续读取。
</tool_policy>

<context_policy>
Policy: text_only_v1
- 当前版本只处理纯文本上下文。
- 历史消息来自同一 task 的前置 turns。
- 工具观察结果会作为 tool message 追加到后续模型上下文。
- 不要把旧工具结果中的缺失片段当作已经看过的事实。
</context_policy>

<environment>
Task ID: ...
Turn ID: ...
Workspace ID: ...
Workspace root: ...
Platform: win32
Today: 2026-07-29
</environment>

<project_instructions>
如果存在项目级规则，将在这里注入摘要或完整内容。
</project_instructions>

<output_contract>
- 面向用户输出应简洁、具体、可执行。
- 代码任务完成时说明改动点、验证结果和未完成风险。
- 未能执行测试或工具失败时必须明确说明。
</output_contract>

<provider_overlay>
DeepSeek:
- 对 deepseek reasoning 类模型，不要求展示完整思考过程。
- 工具调用前避免长篇推理，优先形成可验证的小步骤。
- 最终回答只输出结论和必要依据。
</provider_overlay>
```

---

## 六、DeepSeek 专用建议

本项目默认模型是 DeepSeek，因此比 OpenCode 更需要一个 DeepSeek overlay。

建议不要单独做很多 `deepseek-chat.txt` / `deepseek-reasoner.txt` 文件，第一阶段只做一个 `DeepSeekProviderOverlay`：

```text
<provider_overlay>
当前模型通过 OpenAI 协议接入 DeepSeek。
- 如果模型返回 reasoning_content，系统会作为 thinking delta 推给前端，但不会回灌给后续模型上下文。
- 不要依赖未回灌的推理内容作为后续事实；后续判断应基于 messages 中的可见文本和工具结果。
- 需要代码事实时优先调用工具，而不是凭记忆回答。
- 输出给用户时不要展示隐藏推理，只给结论、依据和下一步。
</provider_overlay>
```

这个 overlay 与当前 `nodes.py` 的实现一致：`_extract_reasoning_content()` 会流式发思考增量，`_finalize_ai_message()` 会剥离 `reasoning_content`，避免推理内容回灌。

---

## 七、Project Instructions 建议

OpenCode 会读取全局/项目 `AGENTS.md`、`CLAUDE.md`、`CONTEXT.md`，并在 read 文件时查找邻近 instruction。本项目不建议第一阶段做这么复杂。

推荐分三步：

### 第一阶段

只读取当前 workspace 根的有限规则文件，例如：

```text
AGENTS.md
README.md 中约定片段，可选
.coding-agent/instructions.md，可选
```

限制：

- 最大字符预算，例如 8k。
- 只注入文本，不解析复杂嵌套。
- 失败时记录日志，不阻塞主流程。

### 第二阶段

引入 `InstructionResolver`：

```text
workspace root instructions
subdirectory instructions
user/global instructions
```

但仍不要把 instruction 解析放进工具层。工具层可以返回“读取了某文件”的事实，context 层负责决定是否补规则。

### 第三阶段

结合 context compaction，把长期规则、当前任务状态、最近工具结果分开预算。

---

## 八、工具输出与 prompt 的关系

本项目已经有 `ToolOutputBudget`：

- 超长工具输出会截断。
- 有 workspace 时完整内容落盘到 `.coding-agent/tool-artifacts/...`。
- 终端输出会脱敏。

system prompt 应明确告诉模型：

```text
工具输出被截断时，不要猜测缺失部分。
如果返回 artifact_path，应使用读取工具按需查看完整输出。
优先搜索或读取相关片段，避免一次性读入巨大文件。
```

不建议：

- 每次工具执行后都用小模型总结结果。
- 把 artifact 全文塞回 system prompt。
- 在 tool description 和 system prompt 中重复大段工具说明。

原因：

- 每次工具后摘要会增加延迟和成本。
- 摘要会丢失错误日志、路径、行号等工程细节。
- 当前已有确定性截断和 artifact 机制，更符合可诊断原则。

---

## 九、分阶段落地建议

### P0：替换硬编码字符串

目标：让 `_build_system_prompt()` 不再直接拼长字符串。

建议：

- 新增 `SystemPromptBuilder`。
- `TextContextBuilder` 调用 builder。
- 保持输出仍是单条 `RuntimeMessage(role="system")`。
- 不改 LangGraph workflow、不改 LangChain bridge、不改工具系统。

验收：

- 单元测试固定 section 顺序。
- 默认 developer agent 构建出的 system prompt 包含 role、goal、allowed tools、context_policy。
- 缺失工具列表时显示 `none`。

### P1：加入运行环境与工具策略

目标：让模型知道运行边界。

建议加入：

- task_id / turn_id
- workspace root / workspace_id
- platform / date
- max_steps / tool_error_limit
- tool output truncation 规则

验收：

- 运行时日志仍显示 `messages_built`。
- system prompt 不包含 secret。
- artifact 路径提示只出现策略，不提前包含任何 artifact 内容。

### P2：DeepSeek overlay

目标：适配 DeepSeek reasoning 内容不回灌的实现事实。

建议：

- 按 `agent_profile.model_name` 或 model provider 判断。
- 只追加短 overlay，不替换主 prompt。

验收：

- DeepSeek 模型 system prompt 包含 reasoning 不回灌说明。
- 非 DeepSeek 模型不包含 DeepSeek 专属说明。

### P3：Project Instructions

目标：支持项目级长期规则。

建议：

- 先读 workspace 根 `AGENTS.md`。
- 设置最大字符预算。
- 失败记日志，不阻断。

验收：

- 有 `AGENTS.md` 时注入 `<project_instructions>`。
- 超预算时明确截断标记。
- 不读取 workspace 外路径。

### P4：Runtime Reminders

目标：支持 plan/review/subagent 等未来模式。

建议：

- 不做常驻大 prompt。
- 根据当前 workflow/mode 动态追加短 reminder section。

验收：

- plan 模式禁止编辑的规则只在 plan mode 出现。
- build 模式不携带 plan-only 约束。

---

## 十、建议测试清单

最少需要这些测试：

1. `SystemPromptBuilder` 默认 section 顺序稳定。
2. `AgentProfile.system_prompt` 被放入 mission，而不是覆盖所有系统规则。
3. `allowed_tools` 为空时不会生成语法怪异的 prompt。
4. DeepSeek overlay 只在 DeepSeek 模型出现。
5. Project instructions 超预算时会截断，并保留来源路径。
6. system prompt 不包含工具完整 JSON schema；工具 schema 仍只由 `ToolDefinition.to_model_tool_definition()` 暴露。
7. `TextContextBuilder.build_messages()` 仍输出第一条 system message，然后是历史 turn messages，最后是当前 user message。
8. 工具 artifact 只作为策略说明或工具结果 metadata 出现，不被 builder 主动读取全文。

---

## 十一、边界决策

建议确认以下设计决策：

1. **SystemPromptBuilder 属于 `core/context`**
   - 原因：它构建模型无关上下文，和 `RuntimeMessage` 同层。

2. **Prompt 模板可以是文件，但第一阶段数量要少**
   - 建议只保留 `base_developer.md`、`deepseek_overlay.md` 两类。
   - 不建议像 OpenCode 一样马上按十几个 provider 拆分。

3. **工具描述不进入 system prompt**
   - 工具 schema 已有单一事实源：`ToolDefinition`。
   - system prompt 只描述工具使用原则。

4. **动态运行态约束用 section，不改 AgentProfile**
   - AgentProfile 描述长期身份。
   - max steps、tool error limit、plan/build mode 属于运行态。

5. **Project instructions 要有预算**
   - 规则文件可能很长。
   - 第一阶段按字符预算截断即可，后续再做摘要/compaction。

---

## 十二、最终推荐

第一版推荐采用：

```text
SystemPromptBuilder
  .build(agent_profile, prompt_context) -> str
```

其中 `prompt_context` 可先用简单参数或 dataclass 表示：

```text
task_id
turn_id
workspace_id
workspace_root
platform
today
model_name
model_provider
allowed_tools
context_policy
max_steps
tool_error_limit
project_instructions
runtime_reminders
```

这条路线的价值：

- 比当前硬编码字符串清晰。
- 比完整照搬 OpenCode 轻。
- 保持本项目已有分层：`core/context` 构建消息，`llm/langchain_bridge` 转换类型，`tools` 提供工具事实源，`workflow` 只编排。
- 给未来 MCP、skills、subagent、context compaction、plan/review mode 留出稳定插槽。

一句话总结：

本项目应该借鉴 OpenCode 的“分层装配思想”，但不要复制它的“大量 txt + 多 provider prompt”形态。当前最合适的方案是在 `TextContextBuilder` 前引入一个轻量、可测试的 `SystemPromptBuilder`，按固定 section 生成 system message，并逐步接入 DeepSeek overlay、环境上下文、项目规则和运行态 reminders。

# 子 Agent 配置提案 Tool 改造方案

## 1. 决策摘要

本方案为对话页增加一次性的「创建子 Agent」能力：用户通过按钮显式进入配置提案模式，主 Agent 根据用户描述生成一份子 Agent 配置草稿，草稿在对话中以可编辑预览卡片展示。主 Agent 只负责生成以下四个字段：

- `agent_id`
- `role`
- `description`
- `system_prompt`

以下字段完全由用户在预览卡片中配置，主 Agent 不生成、不推断、不保存：

- 配置作用域（系统级或当前 workspace）
- `allowed_tool_groups`
- `max_steps`
- `model_config_id`
- `model_settings`

只有用户点击「保存子 Agent」后，前端才调用现有 Agent 配置 API，由后端 `AgentConfigurationService` 写入正式配置文件并刷新当前进程中的 Agent profile。

这是一份**临时提案**，不是新的配置事实，也不是 Agent 的常驻能力。方案不引入配置版本字段、兼容字段、数据库表或新的持久化层。

## 2. 目标与非目标

### 2.1 目标

1. 用户可以在对话中用自然语言描述想要的子 Agent。
2. 主 Agent 可以稳定地产生四个结构化字段，而不是依赖 Markdown 或 JSON 文本解析。
3. 用户可以在当前对话中预览和简单修改提案。
4. 用户明确点击保存后，才创建正式子 Agent 配置。
5. 未保存草稿不影响当前运行中的 Agent，不影响配置文件，也不进入正式 Agent profile registry。
6. 提案能力默认不在 Run 的 `allows_tools` 中，不进入系统提示词；其固定 schema 仅作为
   Task 的模型工具集合存在，等待用户显式开启。

### 2.2 非目标

- 不允许主 Agent 直接创建、更新或删除 Agent 配置。
- 不由主 Agent 决定工具权限、模型、最大步数或配置作用域。
- 不把草稿写入 SQLite、JSON 配置文件或 workspace 文件。
- 不新增 Agent 配置版本、迁移版本、草稿版本或兼容版本字段。
- 不为未保存草稿实现跨页面、跨进程或跨重启恢复。
- 不让普通对话在没有用户点击按钮的情况下自动触发该能力。

## 3. 用户流程

```text
用户点击「创建子 Agent」
        ↓
前端发送一次性提案命令，并临时开放提案 Tool
        ↓
用户描述目标，例如「创建一个只读代码审查 Agent」
        ↓
主 Agent 生成四个字段并调用 propose_agent_configuration
        ↓
对话中显示「子 Agent 配置草稿」卡片
        ↓
用户点击「编辑配置」
        ↓
用户补充作用域、工具组、模型、最大步数等字段
        ↓
用户点击「保存子 Agent」
        ↓
前端调用现有创建配置 API
        ↓
后端写入正式配置并返回已保存配置
```

按钮点击后建议允许用户先输入目标再发送，而不是点击按钮就立即生成空配置。按钮可以有两种交互形式：

- 推荐：点击后在输入框上方显示「创建子 Agent」模式标识，用户继续输入需求并发送。
- 简化：点击后直接插入一条不可见的结构化 command，用户在输入框补充需求。

无论采用哪种外观，后端都必须收到明确的提案命令，不能只依赖普通自然语言中的「帮我创建 Agent」来猜测能力范围。

## 4. Tool 设计

### 4.1 Tool 名称与职责

新增 Tool：`propose_agent_configuration`。

它只负责把主 Agent 产生的四个字段转换为受约束的提案结果，并生成供前端展示的 `display_data`。它不负责：

- 文件读写；
- Agent registry 注册；
- 配置作用域判断；
- 工具组、模型和最大步数的选择；
- 调用现有创建或更新配置 API；
- 修改 Agent context 或当前 Run 的模型配置。

Tool 名称使用 `propose` 而不是 `create`，从命名上阻止调用方把「生成草稿」误解成「已经保存」。

### 4.2 Tool 输入

Tool 输入只包含四个字段：

```json
{
  "agent_id": "code-reviewer",
  "role": "代码审查专家",
  "description": "检查代码质量、潜在缺陷和测试覆盖情况。",
  "system_prompt": "你负责审查代码……"
}
```

输入校验规则：

- 四个字段均为字符串；
- `agent_id` 非空，并遵守现有 Agent ID 规则；
- `role` 非空；
- `description` 非空；
- `system_prompt` 非空；
- 拒绝额外字段，避免模型把工具组、模型或路径偷偷塞进提案；
- 不在 Tool 输入中加入配置版本、草稿版本或兼容字段。

Tool 可以复用现有 `AgentConfigurationDocument` 的四字段校验逻辑，但不能调用 `create_document`，避免复用时产生写盘副作用。

### 4.3 Tool 输出

面向模型的 Tool 结果保持最小化，例如：

```json
{
  "accepted": true,
  "message": "子 Agent 配置草稿已生成，等待用户确认保存。"
}
```

完整的四字段只进入工具的 `display_data`，供对话卡片渲染：

```json
{
  "kind": "agent-configuration-draft",
  "agent_id": "code-reviewer",
  "role": "代码审查专家",
  "description": "检查代码质量、潜在缺陷和测试覆盖情况。",
  "system_prompt": "你负责审查代码……",
  "status": "draft"
}
```

这里的 `status` 是 UI 生命周期状态，不是 Agent 配置版本或 Run 状态。保存动作由前端完成后，卡片可以在本地变成 `saved` 展示态，但该状态不写入 Agent 配置文件。

`display_data` 是对话展示数据，不是 Agent context 的镜像。Tool 的模型可见结果只返回短消息，避免完整配置正文因为 Tool 回传而继续污染后续模型上下文。当前对话的 Tool 展示仍可以通过已有 Transport snapshot 重新渲染。

### 4.4 Task 固化与 Run 允许集合

为保持同一 Task 的模型请求前缀稳定，首次创建 Task 时把模型可见的完整工具 schema 固化到
`TaskModel.tool_schemas`。该字段只保存 `name`、`description`、`parameters`，不保存
handler、运行期权限对象或任何 version 字段；`TaskRuntimeSpace` 只作为进程内只读缓存。

每个 Run 单独计算 `allows_tools`：

```text
allows_tools = TaskModel.tool_schemas 的工具名集合 - 用户禁用的工具组
proposal Run 额外加入 propose_agent_configuration
```

`bind_tools` 始终使用 Task 固化的 schema，不因禁用工具或 proposal 模式改变。工具调用生命周期
和实际执行都使用同一份 `allows_tools` 快照；不在集合中的调用进入隐藏的协议闭合集合，并由
`ToolMessage(status=cancelled)` 结束协议。

因此，`propose_agent_configuration` 在 Task 创建时就进入固定 schema，但默认不在
`allows_tools` 中。用户点击按钮只改变本次 Run 的允许集合，不动态修改 `bind_tools`，也不把
完整 schema 重复放入系统提示词或持久化上下文。提案约束只作为本次模型请求的临时消息注入。

前端提交 `ban-tools` custom command，携带本次 Run 禁用的工具名集合；后端按 Task 固化
schema 校验其工具名，并计算最终 `allows_tools`。普通对话不携带 proposal 工具，proposal 命令只负责额外开放已固化的
提案工具。

## 5. 提案命令与 Agent context 边界

### 5.1 按钮产生结构化命令

按钮应沿用现有 Assistant command 的 `(task_id, command_id)` 幂等边界，增加一个明确的命令意图，例如：

```json
{
  "task_id": 11,
  "command_id": "cmd_xxx",
  "mode": "propose_agent_configuration",
  "user_message": "创建一个只读代码审查 Agent"
}
```

其中 `mode` 是本次 Run 的装配意图，不是 Agent 配置字段。相同 `(task_id, command_id)` 的不同 payload 仍按现有幂等规则拒绝。

### 5.2 不写入常驻提示词

提案规则应作为本次 Run 的临时运行约束传给 workflow：

```text
本次请求处于子 Agent 配置提案模式。
只生成 agent_id、role、description、system_prompt。
不要决定工具组、模型、最大步数和配置作用域。
不要写入文件或调用配置保存 API。
必须通过 propose_agent_configuration 输出结构化草稿。
```

这段约束不能写入：

- 全局 AGENTS.md；
- 主 Agent prompt；
- RuntimeContextManager 的长期系统上下文；
- Agent profile JSON；
- workspace 配置文件。

当前 Run 可以拥有这段临时约束和 Tool 调用记录；Run 结束后，常规主 Agent 不再获得这项能力。

### 5.3 不让模型读取用户后续编辑

用户在前端卡片中修改的内容不需要回写 Agent context，也不需要发回主 Agent。保存按钮直接使用当前卡片的本地草稿调用配置 API。

这样可以避免：

- 用户已经确认的字段再次被模型改写；
- 模型在保存前插入额外工具权限；
- 前端草稿和 Agent context 产生第二套配置事实。

## 6. 对话卡片设计

### 6.1 只读预览态

Tool 成功后，在对应 Tool 卡片中显示：

- 标题：`子 Agent 配置草稿`；
- 状态：`未保存`；
- Agent ID；
- 角色；
- 描述；
- system prompt 的 Markdown 预览或折叠摘要；
- 按钮：`编辑配置`、`放弃`。

预览卡片保持只读，不在通用 Tool renderer 中直接放输入框或保存逻辑。用户点击「编辑配置」后，打开对话页右侧的 Agent Configuration Workbench，在独立编辑面板中修改完整配置。Workbench 复用已有 Agent 编辑表单，避免系统配置页和对话页维护两套字段校验与 Markdown 编辑器。

### 6.2 Workbench 编辑态

用户点击「编辑配置」后，在右侧 Workbench 打开完整配置表单。对话流仍然保留在左侧或主区域，用户可以一边查看原始需求和主 Agent 的提案，一边补全配置。

Workbench 顶部应明确显示：

```text
子 Agent 配置草稿
未保存
保存目标：当前 workspace
```

表单包含：

```text
Agent ID             已由主 Agent 生成，可修改
角色                 已由主 Agent 生成，可修改
描述                 已由主 Agent 生成，可修改
系统提示词           已由主 Agent 生成，可修改
作用域               用户选择
允许的工具组         用户选择
最大步数             用户填写或使用 UI 默认值
模型                 用户选择
模型参数             用户配置
```

主 Agent 没有生成的字段必须在 UI 中保持明确的「待配置」或合法默认值状态，不应伪装成 Agent 已经决定的内容。

Workbench 中的编辑内容是前端 working copy，不回写主 Agent context，也不重新提交给主 Agent 参与保存。可以使用 Tool 的 `toolCallId` 作为草稿卡片与 Workbench 的 UI 关联标识，不新增草稿版本字段或持久化草稿 ID。

### 6.3 Workbench 生命周期

- 打开 Workbench 不执行保存，也不注册 Agent。
- 切换回对话区域不会丢失当前编辑中的 working copy。
- 关闭 Workbench 时，如果存在未保存修改，提示用户确认放弃。
- 用户点击「保存子 Agent」后，Workbench 调用正式配置 API。
- 保存成功后，Workbench 显示「已保存」，对话中的预览卡片同步显示「已保存」。
- 保存失败时保留当前 working copy，并展示受控错误，允许用户修正后重试。
- 保存新 Agent 不会让当前正在运行的 Run 切换 profile，后续 Run 才使用新配置。

推荐的页面关系如下：

```text
对话区域
  └─ 子 Agent 配置草稿 Tool 卡片（只读）
       └─ 编辑配置
            └─ 右侧 Agent Configuration Workbench
                 └─ 用户补全完整配置
                      └─ 显式保存
```

### 6.4 保存态

保存请求成功后：

- 卡片显示「已保存」；
- 显示实际保存作用域和 Agent ID；
- 禁用重复保存，或将按钮改为「打开配置」；
- 配置列表可以通过现有状态刷新机制更新；
- 当前正在执行的 Run 不隐式切换到新配置，后续 Run 才使用新配置。

保存失败时：

- 保留用户当前草稿；
- 展示受控错误信息；
- 不把原始异常、路径、堆栈或 provider 响应直接展示给用户；
- 允许用户修正后重试。

## 7. 保存边界

### 7.1 前端保存

卡片保存动作根据用户选择的作用域调用现有 API：

- 系统级：`createAgentConfiguration`；
- workspace 级：`createWorkspaceAgentConfiguration`。

前端不直接写 JSON，不直接访问 SQLite，也不通过普通文件工具写入 `.cosir/agents`。

### 7.2 后端校验

现有 `AgentConfigurationService` 继续是正式配置的唯一写入入口。保存时后端必须重新校验：

- Agent ID 合法性；
- 同作用域是否已存在同名配置；
- 工具组是否存在；
- 子 Agent 禁止工具规则；
- 模型配置是否存在且可用；
- 最大步数和模型参数范围。

Tool 提案阶段的校验只保证四字段可用于预览，不能替代保存阶段的正式校验。

### 7.3 同名配置

首版不默认覆盖已有 Agent：

- 新建时发现 Agent ID 已存在，保存失败并提示用户修改 ID；
- 用户若要修改已有 Agent，应从配置列表进入编辑流程；
- 对话草稿不自动推断这是新建还是更新。

## 8. 事实所有权与生命周期

```text
主 Agent 生成的四字段
    → Tool display_data（临时展示产物）
    → 前端草稿 working copy
    → 用户点击保存
    → AgentConfigurationService
    → 正式 Agent 配置文件 / AgentProfileRegistry
```

每层职责如下：

- 主 Agent：生成四字段，不拥有保存权限；
- Tool：结构化和展示提案，不写入配置；
- Transport snapshot：恢复对话卡片展示，不成为配置事实源；
- React：持有用户编辑中的草稿，不直接访问配置文件；
- AgentConfigurationService：校验并持久化正式配置；
- AgentProfileRegistry：进程内解析正式 profile，供后续 Run 使用。

未保存草稿只存在当前对话 UI 和当前 Tool 展示生命周期内。页面刷新、后端重启或 Run 结束后，草稿可以丢失，不需要增加数据库表或恢复协议。

## 9. 预期代码影响面

这是后续实施时的建议拆分，当前文档阶段不修改这些文件：

### 后端

- `apps/backend/app/core/tools/`：新增 `propose_agent_configuration` Tool 及四字段输入模型。
- `apps/backend/app/core/tools/display/`：新增 `agent-configuration-draft` display projection。
- `apps/backend/app/core/tools/tool_ui_display_contract.md`：登记新的稳定 `kind` 和失败态字段规则。
- `apps/backend/app/core/runtime/`：支持按提案 command 为当前 Run 装配临时 Tool，不改变常驻 Tool registry。
- `apps/backend/app/assistant_transport/`：识别提案 command，沿用现有幂等、Run 创建和 snapshot 投影边界。
- `apps/backend/app/service/configuration/`：复用现有正式配置保存服务，不新增草稿保存服务。

### 前端

- `apps/desktop/components/assistant-ui/`：新增草稿卡片和编辑状态管理。
- `apps/desktop/components/assistant-ui/tools/tool-renderer-registry.ts`：注册 `agent-configuration-draft` renderer。
- `apps/desktop/lib/assistant/contract.ts` 与 snapshot validation：增加 display_data 结构校验。
- `apps/desktop/lib/api/configuration.ts`：复用现有系统级和 workspace 级创建 API。
- `apps/desktop/components/system-configuration-page.tsx`：抽取通用 Agent 配置表单，供配置页和对话草稿卡片复用。
- 对话 composer：增加「创建子 Agent」按钮和一次性提案模式状态。

不新增：

- SQLite 表；
- 配置版本字段；
- Agent profile 版本字段；
- 兼容适配层；
- 独立网络服务；
- 前端直写配置文件的通道。

## 10. 实施顺序

1. 固定 `propose_agent_configuration` 的四字段输入模型和 `agent-configuration-draft` display contract。
2. 抽取当前 `AgentEditor` 为可复用的 Agent 配置表单，保持系统配置页行为不变。
3. 实现提案 Tool 的纯校验和 display_data 投影，确认无文件写入副作用。
4. 增加一次性提案 command 和当前 Run 的临时 Tool 装配边界。
5. 在对话 Tool renderer 中显示只读草稿卡片。
6. 接入用户编辑状态和现有保存 API。
7. 增加保存成功、保存失败、同名冲突、放弃草稿和页面刷新丢弃草稿的测试。
8. 增加普通对话不可见提案 Tool、子 Agent 不可使用提案 Tool、Tool 不写配置文件的验收测试。

## 11. 验收标准

1. 未点击「创建子 Agent」按钮时，主 Agent 的工具列表和系统提示词中不存在 `propose_agent_configuration`。
2. 点击按钮后，只有当前提案 Run 可以调用该 Tool；Run 结束后普通对话不可调用。
3. Tool 输入只能包含 `agent_id`、`role`、`description`、`system_prompt` 四个字段。
4. Tool 执行不会创建、更新、删除任何 Agent 配置文件，不会注册 profile，不会修改数据库。
5. 对话页可以展示结构化草稿，且不会从模型 Markdown 中猜测字段。
6. 用户可以修改四个字段，并独立配置作用域、工具组、模型、最大步数和模型参数。
7. 未点击保存前，任何用户编辑都不会进入正式 Agent 配置事实。
8. 点击保存后只通过现有配置 API 写入，后端正式校验失败时保留草稿并显示受控错误。
9. 同名 Agent 默认拒绝覆盖，不产生隐式更新。
10. 未保存草稿不跨刷新、重启或普通 Agent context 恢复，不为此增加版本字段或兼容字段。
11. 当前运行中的 Run 不因为保存新 Agent 而切换 profile；后续 Run 才能使用新配置。
12. 所有公共新增模块、类和函数的 docstring 与注释使用中文，并准确说明输入、输出和副作用。

## 12. 结论

推荐抽取 `propose_agent_configuration` Tool，但它必须是一个**按按钮临时启用、只生成四字段、只提供展示数据、不负责保存**的 Tool。

正式配置仍然由用户通过对话卡片补全并点击保存，现有配置服务继续拥有唯一写入权。这样既利用主 Agent 的理解和生成能力，又不会把工具权限、模型选择和运行成本交给模型，更不会为了草稿引入新的事实源、版本字段或兼容层。

# 对话中禁用工具分组计划

## 目标

允许用户在发送一条对话消息时，按 `ToolDefinition.group` 选择本次 Run 禁用的工具组。后端提供主 Agent 的工具目录接口，按 group 聚合返回工具集合；前端选择分组后展开为工具名列表，通过 `AssistantCommand` 携带 `ban_tools`。默认 `ban_tools` 为空，即不勾选任何组、现有工具均可用。

禁用策略必须同时满足：

- 不改变模型绑定的工具集合或顺序，不在运行中重建/替换 `bind_tools`；
- 运行期禁用工具调用不创建 Assistant Transport tool part，不在实时 UI 显示；
- 禁用工具调用不进入工具执行节点；
- 模型输出的工具调用协议仍能正确闭合，后续模型请求不留下未配对的 `AIMessage.tool_calls`。

实现范围按决策限定为 `ToolCallLifecycleManager` 的运行期处理。`ConversationTaskStateRebuilder` 保持不变，因此 backend 冷重建仍可能从 canonical `AIMessage.tool_calls` 投射禁用工具 part；本计划不承诺该路径隐藏。

本功能属于单用户本机桌面 Agent。配置随 Conversation Run 在本机后端处理，不引入远端服务或独立配置服务。

## 当前代码事实

- `ToolDefinition` 已有 `group` 字段；各 handler 在构建定义时提供分组。分组词表集中在 `app/core/tools/schemas/tool_groups.py`。
- `ReactLikeWorkflow` 在 graph 启动前，基于 `operations.model_tools` 和 `agent_profile.allowed_tools` 生成 schema，并执行一次 `base_model.bind_tools(..., strict=True)`。这是稳定的模型工具绑定边界。
- `model_node` 消费模型流时，将累计的 `tool_call_chunks` 交给 `ToolCallLifecycleManager.create`。对已注册工具，`create` 发出 `ToolCallCreatedEvent`，由此建立前端 tool part。
- 流结束后，`AIMessage.tool_calls` 再经 `ToolCallLifecycleManager.classify` 分类；合法调用进入 `running`，而 `tools_node` 只执行 lifecycle 中的 `running` 调用。
- `model_node` 在每次模型请求前，通过 `TaskRuntimeSpace.take_deferred_system_messages(run_id=...)` 取出延迟系统消息并追加进 `RuntimeContextManager`。该上下文消息会持久化并成为后续模型请求历史的一部分。
- `ConversationTaskStateRebuilder` 的冷重建路径会直接遍历 canonical `AIMessage.tool_calls` 构建 tool parts；它不会经过 `ToolCallLifecycleManager`。
- Assistant Transport 的 `/assistant` 请求由 `AssistantTransportRequest` 校验，并通过 `payload_hash()` 参与 command 幂等；当前 `custom`/通用 command 尚未绑定处理器，且请求校验目前只接受一个 `add-message`。
- `ConversationRunExtra` 是 Run 的本机持久化 JSON 扩展字段，目前保存展示文本和附件引用；需要扩展其值对象契约以持久化 `ban_tools`。

## 设计方案

### 1. 配置语义与持久化

将 `ban_tools` 作为本次 Run 的工具禁用配置，通过与 `add-message` 同批提交的 `AssistantCommand` 传递：

- 值为去重后的工具名列表；缺省或空数组代表不禁用任何工具。前端按所选 group 收集该组工具名后形成 `ban_tools`。
- 使用 Assistant UI 的 custom wire 形态：`type="custom"`、`name="ban-tools"`，`payload` 携带 `ban_tools` 工具名列表，并提供唯一的 `commandId`。后端为其定义专用 `BanToolsCommand` / payload schema，并将它显式加入 `AssistantCommand` 联合类型；请求允许它与本次唯一的 `add-message` 同批提交。后续新增命令时新增专用 schema 并显式加入联合类型，不使用宽泛的通用 `CustomCommand`。
- `add-message` 与 `ban-tools` 各自使用 `(task_id, command_id)` 作为幂等键；Run 命令 service 对当前请求的全部 command ID 做整批检查，并在创建/编辑 Run 的同一事务内写入全部记录、关联到同一 Run。命令批次整体共享一个 `payload_hash`，它覆盖消息、模型选择、Run 操作身份和禁用工具集合；command ID 本身不参与 hash。当前批次和 `ban_tools` 都按语义集合规范化顺序，因此相同命令只改变数组顺序仍视为同一载荷。重放必须是 ID 集合完整且所有记录载荷一致、Run 关联一致；部分命中不能静默复用或继续创建。
- 新建 Run 和编辑重跑都从命令取得 `ban_tools` 并写入该 Run 的 `ConversationRunExtra`。后续 resume/恢复读取 Run Extra 中已保存的配置，不要求前端再次发送配置命令。
- Run 执行期间从 `ConversationRunExtra.ban_tools` 读取并构建不可变拦截策略。不要写入共享 `AgentProfile`、全局工具注册表或 task 级可变状态。

`ConversationRunExtra` 应把 `ban_tools` 作为明确、校验过的字段读写；不能只放在 WebView 状态、command 临时变量或后端进程内存中，否则编辑重跑和业务续跑无法从目标 Run 重建策略。

### 2. 分组目录与前端选择

增加只读工具目录接口，返回主 Agent 当前允许使用的工具，并按 `ToolDefinition.group` 聚合。每组至少包含 group 标识/名称和工具集合；工具项提供稳定工具名，供前端构造 `ban_tools`。目录按主 Agent 的 `allowed_tools` 和实际注册的 `ToolDefinition` 生成，保持它与模型可用工具的边界一致。前端直接使用该响应构造组选项，不维护静态工具清单。

新建对话页与已有对话的普通/编辑 Composer 都展示 group 选择控件并维护当前选择。新建对话页在任务创建后，通过路由状态把首条消息的所选 group 和展开后的工具名传给 Runtime；首条消息发送的 `ban_tools` 命令必须使用这份选择，不能因 Runtime 工具目录异步加载而丢失。发送时将所选组对应的工具名去重后写入 `ban_tools` 命令。目录加载失败时应明确显示错误状态，不能退化成空列表后仍声称可选择。

### 3. 稳定 bind_tools

保持 workflow graph 构建时现有的工具 schema 生成和 `bind_tools` 调用不变。禁用组是模型外的运行时拦截策略：模型仍收到相同 schema、相同工具顺序和相同前缀引用。

运行期直接从 `ConversationRunExtra.ban_tools` 计算一个不可变的禁用工具名集合，并注入本轮 `ToolCallLifecycleManager`。目录接口只负责让 UI 按 group 选择；后端执行策略和持久化契约使用工具名列表。禁用判定由 manager 统一处理，避免各层分别解释分组。

### 4. 禁用处理统一放在 ToolCallLifecycleManager

把 Run Extra 读取出的 `ban_tools` 注入本轮 `ToolCallLifecycleManager`。**禁用判定与过滤只由该 manager 负责**；不在 React 前端、`ConversationEventProjector` 或 `tools_node` 另建禁用判断：

- 流式 `create` 遇到禁用工具时，不向 Assistant Transport 发 `ToolCallCreatedEvent`，不创建可见生命周期 part。
- 聚合 `classify` 对相同禁用工具执行同一过滤，保证它不会成为可执行的 `valid_tools` 调用。
- manager 对禁用调用产生的后续生命周期通知也必须按同一策略抑制，避免没有创建事件却发出孤立状态事件。
- 混合调用中只过滤禁用项，启用项保持现有事件、状态迁移和执行行为。
- manager 可保留必要的内部拦截信息供现有路由/上下文闭合使用，但不得把禁用调用放进会触发 UI 事件或工具执行的集合。
- 记录受控结构化日志，只记录 Run、step、工具名及数量，不记录参数正文。

`model_node` 继续负责收口模型流、按现有顺序调用 manager、消费 manager 提供的被拦截调用信息来闭合模型协议并排入 deferred system 提示，以及完成 workflow 路由；这些是上下文/编排职责，不重复实现 `ban_tools` 匹配。`tools_node` 保持现有“只执行 manager 提供的有效调用”行为。

### 5. 闭合模型工具调用协议并反馈

过滤生命周期投射不修改原始 `AIMessage`。对于每个被禁用的 call ID，后端仍需在 canonical context 内写入配对 `ToolMessage`，再延迟注入受控系统提示，说明该工具在本轮不可用并要求模型改用可用工具或直接回答。

收口顺序必须是：

```text
AIMessage(tool_calls)
  → 内部 ToolMessage × 被拦截调用数
  → deferred SystemMessage（禁用调用反馈）
  → 下一次 model request
```

这条路径不创建可见 lifecycle part、不发 tool lifecycle event，也不执行工具。占位 `ToolMessage` 的构造和持久化应复用现有上下文的 tool-call closure 契约，不能自行引入第二套状态机。多个并行调用应逐个闭合；混合启用/禁用调用需分别执行/反馈，并保持所有 tool message 都排在下一条 system message 之前。

新建 Run 和编辑重跑时，后端根据保存到 Run Extra 的 `ban_tools` 构造 system 消息，并通过 `TaskRuntimeSpace.defer_system_message` 排入该 task 的延迟消息队列。消息必须在 `additional_kwargs` 中携带当前 `run_id`，由 `model_node` 现有的 `take_deferred_system_messages(run_id=...)` 消费路径注入 `RuntimeContextManager`，从而进入 canonical context 并在首次模型请求前可见。被禁用调用的反馈也使用 `TaskRuntimeSpace.defer_system_message`，在对应 ToolMessage 闭合之后排入队列，并带同一 `run_id`。提示只表达可用性，不把禁用策略当作安全授权边界；执行拦截才是实际约束。

编辑重跑复用持久化 `run_id`，因此启动新一轮时要在 task operation lock 内先清除该 Run 上一轮遗留的 deferred system 消息，再排入新配置提示，避免相同 `run_id` 让过期反馈被误消费。

### 6. lifecycle 与路由行为

若模型只请求禁用工具，现有 workflow 路由必须能在补齐调用闭合并排入反馈后继续模型，不得执行禁用工具。若同一批含启用与禁用工具，则启用工具保持现有 tools/observe 链路，禁用调用只走协议闭合与提示注入。

### 7. 投射范围与冷重建边界

按本计划的聚焦要求，禁用筛选只在 `ToolCallLifecycleManager` 处理；`ConversationEventProjector` 和 `ConversationTaskStateRebuilder` 保持现有职责与行为不变。这样可以隐藏运行中由 lifecycle event 创建的禁用 tool part，但冷重建仍会从原始 `AIMessage.tool_calls` 投射 tool part。若之后要求 backend 重启后的冷读也隐藏这些 part，需要另行调整该边界；这不属于当前已确认范围。

## 计划改动边界

预计涉及以下层次，实施时根据实际依赖收敛具体文件：

1. **工具目录接口**：返回主 Agent 当前允许工具，按 `ToolDefinition.group` 聚合并提供工具名。
2. **桌面发送配置**：Assistant composer/runtime 的分组选项、将所选组展开为 `ban_tools`、`AssistantCommand` 序列化和编辑重发配置保留。
3. **Assistant Transport**：定义并接入 `AssistantCommand`、command 数量/关联规则、payload hash 和 Run 命令编排。
4. **Run 持久化与启动**：扩展 `ConversationRunExtra.ban_tools`，确保新建、编辑、resume 和执行器读取一致。
5. **Runtime 配置**：从 Run Extra 读取工具名集合，沿 executor → runner → workflow → model node 传递，不使用全局状态。
6. **Workflow lifecycle**：将 Run 的 `ban_tools` 注入 `ToolCallLifecycleManager`；由 manager 统一过滤可见 lifecycle event 与可执行调用；补充禁用调用协议闭合、延迟反馈与确定性路由。
7. **诊断**：对拦截数量、工具名和 Run/step 使用稳定 snake_case 事件；日志不带工具参数或模型正文。

不改 `bind_tools` schema、工具 handler 的业务实现、工具分组语义、Assistant UI 原生消息/part 契约和远端部署拓扑；Transport 只增加项目自己的 `ban-tools` custom command 处理。

## 实施顺序

1. 实现主 Agent 工具目录接口，确认按 group 聚合、工具名稳定且只返回当前允许工具。
2. 定义 `AssistantCommand`、与 add-message 同批传递规则、命令幂等 hash 和 `ConversationRunExtra.ban_tools` 契约。
3. 打通前端分组选项、工具名展开、命令发送及编辑重跑行为；缺省配置映射为空列表。
4. 让新建/编辑路径将 `ban_tools` 保存进目标 Run Extra，并通过 `TaskRuntimeSpace.defer_system_message` 排入带目标 `run_id` 的 system 消息；让后续执行从 Run Extra 读取策略。
5. 将策略贯穿到 Workflow，确认 `bind_tools` 参数不受禁用列表影响。
6. 在 `ToolCallLifecycleManager` 实现流式映射和最终聚合调用的统一过滤，保证禁用调用既无 UI lifecycle event 也不进入可执行列表。
7. 实现隐藏的 `ToolMessage` 协议闭合、deferred 拒绝反馈和“仅禁用调用”路由。
8. 补充 manager 结构化日志，并按验收场景补充对应层级的自动化验证；不修改 `ConversationEventProjector` 或 `ConversationTaskStateRebuilder`。

## 验收标准

- 默认请求不携带禁用组或携带空数组时，行为与现状一致。
- 切换禁用组前后，同一 Run 构建出的 `bind_tools` schema、工具顺序和模型绑定次数一致。
- 被禁用工具的流式和聚合调用经 `ToolCallLifecycleManager` 过滤后，不会发出 `ToolCallCreatedEvent`、不会进入可执行调用集合，也不会执行 handler。
- 启用工具在混合调用中仍能正常执行并展示；每个被禁用 call ID 均有隐藏的协议闭合消息。
- 仅产生禁用工具调用时，workflow 能带反馈回到模型，模型可正常继续或回答；不会留下未闭合的 tool call，也不会因空工具批次错误终止。
- 首次禁用组说明和每次拒绝反馈都通过 `TaskRuntimeSpace.defer_system_message` 排队，并由 `take_deferred_system_messages(run_id=...)` 注入正确 Run 的 canonical context；拒绝反馈位于对应 ToolMessage 闭合之后，其他 Run 不会消费该提示。
- 运行期 lifecycle 投射隐藏禁用调用；冷重建仍沿用当前 `ConversationTaskStateRebuilder` 行为，可能从原始 `AIMessage.tool_calls` 显示这些调用。
- 同一 command ID 携带不同 `ban_tools` 会被识别为 payload 冲突；相同 payload 重放保持幂等。
- 新建和编辑重跑都将 `ban_tools` 写入目标 Run Extra 并注入对应 system 消息；业务 resume 从该 Run Extra 读取相同配置，不依赖再次发送命令。
- 工具目录接口按 group 聚合返回主 Agent 当前允许工具；UI 分组选项与接口一致，前端提交的 `ban_tools` 只能包含目录中的工具名。

## 主要风险与处理

- **只过滤流式创建事件不够**：`ToolCallLifecycleManager` 必须同时过滤 `create`、聚合 `classify` 及禁用调用的后续状态事件；有效执行列表不得包含禁用工具。
- **只过滤工具调用却留下协议空洞**：每个被拦截 call ID 都要通过内部 ToolMessage 配对，再追加 system feedback。
- **配置在重连/编辑/续跑时丢失**：`ban_tools` 属于 Run 运行事实，必须落到 Run Extra，不能仅依赖 React state 或一次 HTTP 请求的临时对象。
- **命令与 add-message 幂等关系不清**：明确同一请求内 `AssistantCommand` 与唯一 add-message 的关联和 payload hash 规则；冲突配置不得被静默忽略。
- **前端分组列表和后端漂移**：由主 Agent 当前允许的定义动态生成分组目录，不复制一份静态工具表到前端。
- **冷重建重新显示禁用工具**：当前约束只改 `ToolCallLifecycleManager`，rebuilder 仍从 canonical `AIMessage.tool_calls` 重建所有 tool parts；若需覆盖 backend 重启后的冷读，需另开范围修改 rebuilder。
- **跨 Run deferred 队列污染**：统一经 `TaskRuntimeSpace.defer_system_message` 排队，system message 必须绑定 `run_id`；依赖 `take_deferred_system_messages(run_id=...)` 丢弃旧 Run 消息并只消费当前 Run 消息。
- **模型仍可能尝试调用禁用工具**：提示是模型引导；实际禁止由后端拦截保证，不依赖模型遵从提示。


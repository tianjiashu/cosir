# 对话中禁用工具分组计划

## 目标

允许用户在发送一条对话消息时，按 `ToolDefinition.group` 选择本次 Run 禁用的工具组。默认禁用组为空，即不勾选任何组、现有工具均可用。

禁用策略必须同时满足：

- 不改变模型绑定的工具集合或顺序，不在运行中重建/替换 `bind_tools`；
- 禁用工具调用不创建 Assistant Transport tool part，不在 UI 显示；
- 禁用工具调用不进入工具执行节点；
- 模型输出的工具调用协议仍能正确闭合，后续模型请求不留下未配对的 `AIMessage.tool_calls`。

本功能属于单用户本机桌面 Agent。配置随 Conversation Run 在本机后端处理，不引入远端服务或独立配置服务。

## 当前代码事实

- `ToolDefinition` 已有 `group` 字段；各 handler 在构建定义时提供分组。分组词表集中在 `app/core/tools/schemas/tool_groups.py`。
- `ReactLikeWorkflow` 在 graph 启动前，基于 `operations.model_tools` 和 `agent_profile.allowed_tools` 生成 schema，并执行一次 `base_model.bind_tools(..., strict=True)`。这是稳定的模型工具绑定边界。
- `model_node` 消费模型流时，将累计的 `tool_call_chunks` 交给 `ToolCallLifecycleManager.create`。对已注册工具，`create` 发出 `ToolCallCreatedEvent`，由此建立前端 tool part。
- 流结束后，`AIMessage.tool_calls` 再经 `ToolCallLifecycleManager.classify` 分类；合法调用进入 `running`，而 `tools_node` 只执行 lifecycle 中的 `running` 调用。
- `model_node` 在每次模型请求前，通过 `TaskRuntimeSpace.take_deferred_system_messages(run_id=...)` 取出延迟系统消息并追加进 `RuntimeContextManager`。该上下文消息会持久化并成为后续模型请求历史的一部分。
- Assistant Transport 的 `/assistant` 请求由 `AssistantTransportRequest` 校验，并通过 `payload_hash()` 参与 command 幂等；API 创建/编辑 Run 后，执行器目前以 `(run_id, execution_mode)` 启动。
- `ConversationRunExtra` 是 Run 的本机持久化 JSON 扩展字段，目前保存展示文本和附件引用，可用于承载此类 Run 输入配置，但需要扩展其值对象契约。

## 设计方案

### 1. 配置语义与持久化

将 `disabledToolGroups` 作为本次 add-message 请求的运行配置：

- 值为去重后的分组名数组；缺省或空数组代表不禁用任何组。
- 在 Transport 请求边界验证类型和分组名。分组名集合从当前工具目录/当前可用 `ToolDefinition` 中提供，不在前端重复维护另一份硬编码词表。
- 将规范化后的禁用组纳入 `payload_hash()`，使同一 `(task_id, command_id)` 带不同禁用组时触发幂等 payload 冲突，而不是误复用旧 Run。
- 把禁用组作为 Run 持久事实保存，建议扩展 `ConversationRunExtra`，并贯穿新建与编辑重跑；这样业务 resume、执行器启动和进程恢复边界都能从目标 Run 读取同一配置。
- 在执行器启动后，将 Run 的禁用组传入本次 Agent/Workflow 运行配置。不要写入共享 `AgentProfile`、全局工具注册表或 task 级可变状态。

如果实现时确认现有 Run extra 的职责不适合承载运行策略，则应改用明确的 Run 字段/值对象；不能只放在 WebView 状态或后端进程临时字典中，否则冷启动/业务续跑无法重建策略。

### 2. 分组目录与前端选择

前端需要展示当前运行环境实际可用的组，范围应与 `operations.model_tools` 一致：先按 workspace 能力和 Agent `allowed_tools` 确定候选定义，再对 `ToolDefinition.group` 去重。这样不可用工具组不会显示成可选能力，且新工具加入现有组时无需前端逐个登记工具名。

目前没有发现返回此目录的 Assistant Transport API。实现阶段应在现有后端配置/工具目录边界上提供只读分组列表，并由 composer 附近的控件维护当前发送选项。若页面拿不到目录时，应显示明确的加载/错误状态，不能退化成空列表后仍声称可选择。

### 3. 稳定 bind_tools

保持 workflow graph 构建时现有的工具 schema 生成和 `bind_tools` 调用不变。禁用组是模型外的运行时拦截策略：模型仍收到相同 schema、相同工具顺序和相同前缀引用。

运行期计算一个不可变的禁用工具名集合：从当前 Run 可用定义中选择 `group` 命中禁用组的工具。流式映射、最终分类和执行防线共用同一个集合/策略对象，避免各层分别解释分组。

### 4. 流式期间不创建 UI tool part

在 `model_node` 把增量工具调用映射到 lifecycle 之前识别禁用工具：

- 禁用调用不传入 `ToolCallLifecycleManager.create`，因此不发 `ToolCallCreatedEvent`，UI 不会建立对应 tool part。
- 对混合 chunk，只过滤禁用调用，其他启用工具仍照常映射。
- `StreamingPartStateMachine.tool_call()` 仍可用于关闭当前 text/reasoning part；该操作本身不会创建工具 UI 项。
- 记录受控结构化日志，只记录 Run、step、工具名/分组及数量，不记录参数正文。

### 5. 聚合调用不分类为可执行工具

流结束后对完整 `AIMessage.tool_calls` 使用同一策略：禁用调用不进入合法 `running` 记录，也不进入 `valid_tools`。不能只过滤 chunk 事件，因为聚合调用仍是 tools 节点执行决策的输入。

为防止未来绕过 lifecycle 的调用路径，在 `tools_node` 或其直接执行门面增加策略校验：命中禁用集合的调用不得送给 `run_tool_calls`。正常路径应在 model 分类时已经剔除；执行层命中属于防御性拒绝并记录错误日志，不执行 handler。

### 6. 闭合模型工具调用协议并反馈

拦截 UI 和执行映射后，原始 `AIMessage` 仍包含模型发出的工具调用。对于每个被禁用的 call ID，后端需要在 canonical context 内写入不暴露给 UI 的配对 `ToolMessage`，再延迟注入受控系统提示，说明该工具组在本轮不可用并要求模型改用可用工具或直接回答。

收口顺序必须是：

```text
AIMessage(tool_calls)
  → 内部 ToolMessage × 被拦截调用数
  → deferred SystemMessage（禁用调用反馈）
  → 下一次 model request
```

这条路径不创建 lifecycle record、不发 tool lifecycle event，也不执行工具。占位 `ToolMessage` 的构造和持久化应复用现有上下文的 tool-call closure 契约，不能自行引入第二套状态机。多个并行调用应逐个闭合；混合启用/禁用调用需分别执行/反馈，并保持所有 tool message 都排在下一条 system message 之前。

首次请求的禁用组说明也应通过 Run-scoped deferred message 在模型请求前注入，并由 `RuntimeContextManager` 持久化。提示应只表达可用性，不把禁用策略当作安全授权边界；执行拦截才是实际约束。

### 7. lifecycle 与路由行为

若模型只请求禁用工具，`model_node` 应在补齐调用闭合并排入反馈后返回继续模型的路由状态，不能误判成正常最终回答、非法空输出或进入空 `tools_node`。若同一批含启用与禁用工具，则启用工具进入现有 tools/observe 链路，禁用调用只走协议闭合与提示注入；所有结果/占位都处理完后再返回模型。

## 计划改动边界

预计涉及以下层次，实施时根据实际依赖收敛具体文件：

1. **桌面发送配置**：Assistant composer/runtime 的禁用组选项、请求序列化和编辑重发配置保留。
2. **Assistant Transport**：请求字段、组名校验、payload hash、Run 命令编排。
3. **Run 持久化与启动**：扩展 `ConversationRunExtra`（或明确的 Run 配置值对象），确保新建、编辑、resume 和执行器读取一致。
4. **Runtime 配置**：把规范化禁用组/工具名集合沿 executor → runner → workflow → model node 传递，不使用全局状态。
5. **Workflow lifecycle**：过滤流式工具映射和聚合分类；补充禁用调用协议闭合、延迟反馈与确定性路由。
6. **诊断**：对拦截数量、组名、工具名和 Run/step 使用稳定 snake_case 事件；日志不带工具参数或模型正文。

不改 `bind_tools` schema、工具 handler 的业务实现、工具分组语义、Assistant UI wire schema 和远端部署拓扑。

## 实施顺序

1. 定义 Transport 字段、默认值、规范化规则与 Run 持久化契约。
2. 打通前端分组目录和每次发送的配置传递，验证 command 幂等 hash 覆盖该配置。
3. 将策略贯穿到 Run/Workflow，确认 `bind_tools` 参数不受禁用组选项影响。
4. 实现流式映射过滤和最终聚合分类过滤。
5. 实现隐藏的 `ToolMessage` 协议闭合、deferred 系统提示和“仅禁用调用”路由。
6. 加入执行入口防线与结构化日志。
7. 按下面的验收场景补充对应层级的自动化验证。

## 验收标准

- 默认请求不携带禁用组或携带空数组时，行为与现状一致。
- 切换禁用组前后，同一 Run 构建出的 `bind_tools` schema、工具顺序和模型绑定次数一致。
- 被禁用工具的流式和聚合调用均不会发出 `ToolCallCreatedEvent`、不会出现在 snapshot/UI、不会进入 `running` lifecycle，也不会执行 handler。
- 启用工具在混合调用中仍能正常执行并展示；每个被禁用 call ID 均有隐藏的协议闭合消息。
- 仅产生禁用工具调用时，workflow 能带反馈回到模型，模型可正常继续或回答；不会留下未闭合的 tool call，也不会因空工具批次错误终止。
- 首次禁用组说明和每次拒绝反馈都出现在正确 Run 的 canonical context 中，顺序位于对应 ToolMessage 闭合之后；其他 Run 不会消费该提示。
- 同一 command ID 携带不同禁用组选项会被识别为 payload 冲突；相同 payload 重放保持幂等。
- 编辑重跑和业务 resume 使用该 Run 已保存的禁用组；新 Run 默认没有禁用组。
- UI 可选分组与后端当前 Agent/workspace 可用 ToolDefinition 分组一致；未知组不能静默接受。

## 主要风险与处理

- **只隐藏 UI 不阻止执行**：必须同时过滤 lifecycle create、最终 classify，并在执行边界保留防线。
- **只过滤工具调用却留下协议空洞**：每个被拦截 call ID 都要通过内部 ToolMessage 配对，再追加 system feedback。
- **配置在重连/编辑/续跑时丢失**：禁用组选项属于 Run 运行事实，必须落到 Run 持久化配置，不能仅依赖 React state 或一次 HTTP 请求的临时对象。
- **前端分组列表和后端漂移**：由当前可用定义动态生成目录，不复制一份静态工具表到前端。
- **跨 Run deferred 队列污染**：系统提示必须绑定 `run_id`，并覆盖取消、失败和 resume 的队列清理/消费路径。
- **模型仍可能尝试调用禁用工具**：提示是模型引导；实际禁止由后端拦截保证，不依赖模型遵从提示。


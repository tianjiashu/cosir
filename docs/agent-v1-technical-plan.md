# Agent 第一版技术方案

本文档记录第一版基础 Agent 的技术方案。它从 `docs/agent-implementation-ideas.md` 中已确认的想法整理而来，用于指导后续代码实现。

第一版目标不是一次性实现完整 coding-agent，而是先做出可运行、可诊断、可扩展的文本 ReAct-like 单 Agent 闭环，为后续 MCP、checkpoint、subagent、context compaction 和多模态输入留出稳定边界。

## 版本范围

### 必须做

- 支持纯文本用户输入。
- 支持单 Agent、单 Task 的 ReAct-like Workflow。
- 支持串行 tool call。
- 支持模型调用、工具调度、工具结果回注、继续判断和最终响应。
- 模型后端调用必须使用 streaming 输出。
- 后端到客户端的运行事件流第一版使用 SSE。
- 模型供应商 stream 和客户端 SSE 是两层边界：Model Adapter 消费供应商增量输出，Runtime 统一转换为内部运行事件，再由 API 层通过 SSE 推送给客户端。
- 工具执行必须经过 Tool Scheduler。
- 支持 max steps、取消、工具失败、模型无效输出等基础终止保护。
- 支持运行事件记录，事件可推送给 UI。
- 支持日志落盘，错误路径必须可排查。
- 支持最小状态模型：Session / Task / Turn / Step。
- 预留 Workflow、Tool、Model Adapter、Context、Checkpoint、Subagent 扩展点。

### 暂不做

- 图片输入。
- 文档附件输入。
- 多 Agent / subagent 执行。
- 并发 tool call。
- 完整 MCP 接入。
- 完整 context compaction。
- 文件级 checkpoint 回退。
- 多 Workflow 编排。

这些能力不进入第一版闭环，但代码边界不能阻碍后续接入。

## 核心概念

### Agent

Agent 是执行主体，描述谁在执行任务。

第一版 Agent 至少包含：

- `agent_id`
- `role`
- `goal`
- `allowed_tools`
- `context_policy`

第一版可以只内置一个默认 Agent，例如 `developer`。后续可扩展 reviewer、tester、researcher 等角色。

当前实现边界：

- `AgentProfile` 描述第一版 Agent 执行主体，包含 `agent_id`、`role`、`goal`、`allowed_tools` 和 `context_policy`。
- 默认内置 `developer` Agent。
- Task 创建时持久化 `agent_id`，API 返回任务状态时包含该字段。
- Runtime `run_started` 事件携带 Agent Profile，便于 UI 和日志定位执行主体。
- Context Builder 使用 Agent Profile 生成 system prompt，包含 `agent_id`、`role`、`goal`、`allowed_tools` 和 `context_policy`，避免 Agent 只停留在记录字段中。
- Runtime 会用 Agent Profile 过滤模型可见工具，并在执行工具前拦截不属于当前 Agent 的 tool call。
- 如果 pending task 持久化的 `agent_id` 与当前 runtime 可用 Agent Profile 不一致，Runtime 会以 `agent_profile_unavailable` 明确失败，不会静默改用其他 Agent 执行。
- 状态级 checkpoint snapshot 记录 Agent Profile 和 task.agent_id，后续 subagent / reviewer / tester 可以复用同一 Runtime 边界。

### Workflow

Workflow 是执行策略，描述 Agent 如何推进任务。

第一版只实现 `react_like`：

```text
model step
-> tool schedule
-> observation update
-> continue decision
```

Workflow 不直接执行工具，不直接写数据库，不直接操作 UI 事件通道。

### Runtime

Runtime 是执行底座，负责状态管理、模型调用、工具调度、事件、日志、终止保护和后续恢复扩展。

Runtime 不应被写死为 ReAct。ReAct-like 只是 Runtime 上运行的一个默认 Workflow。

### Tool Scheduler

Tool Scheduler 是工具执行边界。

模型只能产生 tool call；真正执行前必须经过 Tool Scheduler 做工具存在性校验、参数校验、权限检查、执行、超时、错误捕获和 observation 标准化。

## 状态模型

第一版采用四层状态：

```text
Session
  Task
    Turn
      Step
```

### Session

表示一个长期会话或项目工作上下文。

候选字段：

- `session_id`
- `project_path`
- `created_at`
- `updated_at`

### Task

表示用户发起的一次任务。

候选字段：

- `task_id`
- `session_id`
- `user_input`
- `status`
- `created_at`
- `updated_at`

### Turn

表示用户与 Agent 的一次交互轮次。

候选字段：

- `turn_id`
- `task_id`
- `input_text`
- `status`
- `created_at`
- `updated_at`

### Step

表示一次可追踪执行步骤。

候选类型：

- `model_call`
- `tool_call`
- `tool_result`
- `observation_update`
- `final_response`
- `error`
- `cancelled`

候选字段：

- `step_id`
- `turn_id`
- `step_type`
- `status`
- `started_at`
- `finished_at`
- `input_summary`
- `output_summary`
- `error`

## 执行流程

第一版 ReAct-like 执行流程：

```text
1. UI 提交纯文本输入
2. API 创建 Task / Turn
3. Runtime 初始化 Agent Run
4. Context Builder 构建模型上下文
5. Model Adapter 以 stream 方式调用 DeepSeek / OpenAI-compatible 模型
6. Runtime 解析模型增量输出和最终响应
7. 如果模型返回 final answer，结束任务
8. 如果模型返回 tool call，交给 Tool Scheduler
9. Tool Scheduler 执行工具并返回 observation
10. Runtime 将 observation 写回上下文
11. Runtime 判断是否继续下一轮模型调用
12. 达到完成、失败、取消或 max steps 后结束
```

第一版必须显式建模继续/结束决策：

```text
continue
finish
fail
cancel
max_steps_reached
invalid_model_output
tool_error_limit_reached
```

## 模型适配

第一版优先支持 OpenAI-compatible Chat Completions。

默认模型方向：

- `deepseek-v4-flash`

第一版模型调用要求：

- 后端到模型供应商必须使用 streaming 请求，不走一次性阻塞响应。
- Model Adapter 负责消费供应商 SSE/chunked stream，并输出项目内部 `ModelDelta`。
- Runtime 只依赖内部 `ModelDelta`，不直接依赖某个供应商的 stream 格式。
- API 层只负责把 Runtime 运行事件编码为客户端 SSE，不直接透传供应商原始 chunk。
- Runtime 调用 Model Adapter 时传入 model-facing tool definitions，Model Adapter 负责序列化为供应商工具 schema。

Model Adapter 职责：

- 接收标准内部消息结构。
- 接收 Runtime 过滤后的模型可见工具定义。
- 转换为供应商 API 请求。
- 使用 streaming 方式调用模型。
- 将模型增量输出转换为 Runtime 事件。
- 解析文本响应和 tool call。
- 标准化模型错误。
- 暴露模型能力信息。

第一版模型能力可以先记录：

```text
ModelCapability
  - text_input: true
  - image_input: false
  - document_input: false
  - tool_calls: true
  - json_output: true
  - thinking_mode: model dependent
  - max_context_tokens: model dependent
```

图片和文档不进入第一版输入范围。

## 消息与上下文

第一版只支持纯文本消息。

内部消息结构不要直接等同于模型 API 消息，建议保留项目内标准结构：

```text
RuntimeMessage
  - role
  - content_text
  - metadata
```

后续接入附件时再扩展：

```text
RuntimeMessage
  - role
  - content_text
  - attachments
  - metadata
```

Context Builder 第一版职责：

- 构建 system prompt。
- 注入用户输入。
- 注入必要历史消息。
- 注入工具 observation。
- 控制最大上下文长度。
- 避免把 `coding-agent-docs` 原理文档无差别塞入上下文。

## 工具系统

第一版工具系统先实现最小可用闭环，不急于接 MCP。

Tool Registry 负责：

- 注册内置工具。
- 暴露工具名称、描述、参数 schema。
- 根据 tool name 查找工具。

Tool Scheduler 负责：

- 校验工具是否存在。
- 校验参数 schema。
- 检查权限等级。
- 执行工具。
- 设置超时。
- 捕获异常。
- 标准化 observation。
- 写入事件和日志。
- 向 Runtime 暴露模型可见工具定义，用于模型调用前的 tool schema 注入；模型可见不等于可直接执行。

当前实现边界：

- `ToolDefinition` 保留 handler、permission、timeout 等执行侧字段。
- Runtime 将模型可见的 `ToolDefinition` 转换为不含执行细节的 `ModelToolDefinition`。
- OpenAI-compatible Model Adapter 将 `ModelToolDefinition` 序列化为 Chat Completions `tools` function schema，并设置 `tool_choice=auto`。
- OpenAI-compatible tool call 会保留 provider `tool_call_id`，Runtime 回注 observation 前会把 assistant `tool_calls` 消息和 `tool` observation 成对放回上下文，保证下一轮请求符合 Chat Completions 工具消息协议。
- 第一版只支持串行 tool call；OpenAI-compatible 请求显式设置 `parallel_tool_calls=false`，如果 provider 仍返回多个 tool calls，parser 会拒绝并进入失败路径。
- 权限拒绝的工具不会暴露给模型，即使 Registry 中存在对应定义；需要审批的工具可以模型可见，但执行前会返回 `approval_required`。

第一版可先内置少量安全工具，例如：

- `read_file`
- `list_directory`
- `search_text`

写文件、执行 shell、网络访问属于高风险工具，可以在第一版中先不启用，或只设计权限接口不开放执行。

## 权限策略

第一版需要先建立权限分级，即使不实现复杂审批 UI。

候选权限等级：

- `safe_read`：读取项目文件、列目录、搜索文本。
- `write_file`：创建或修改文件。
- `execute_command`：执行 shell 命令。
- `network_access`：访问网络。
- `external_tool`：调用外部 MCP 或第三方工具。

第一版建议只默认允许 `safe_read`，其他权限进入后续审批能力。

当前实现边界：

- `ToolApprovalPolicy` 将权限分为自动批准、需要用户审批、拒绝三类。
- 默认后端只自动批准 `safe_read`。
- 需要审批的工具对模型可见，但 Tool Scheduler 不会直接执行，而是返回 `approval_required` observation。
- Runtime 收到 `approval_required` 后发出 `tool_approval_required` 事件，并让当前任务进入失败收口；后续桌面 UI 接入审批后再扩展为暂停、批准、恢复。
- 被拒绝的权限不对模型暴露，即使 Registry 中存在对应工具定义。

## 事件与日志

第一版必须有事件流和日志落盘。

Event Stream 面向 UI 和后续持久化。

第一版通信链路：

```text
Model Provider Streaming
  -> Model Adapter
  -> Runtime Event Stream
  -> SSE
  -> Desktop UI
```

客户端到后端使用：

```text
HTTP API: 创建任务、查询状态、取消任务
SSE: 接收模型增量输出、工具状态、运行状态和最终结果
```

WebSocket 不进入第一版默认方案，只作为后续双向实时场景预留。

事件类型候选：

```text
RunStarted
StepStarted
ModelOutputDelta
ToolCallRequested
ToolCallStarted
ToolCallFinished
ObservationAdded
RunFinished
RunFailed
RunCancelled
```

日志面向排查：

- 写入 `logs/app.log` 或后端配置指定路径。
- 记录 run_id、session_id、task_id、turn_id、step_id。
- 记录模型调用开始/结束、工具调用开始/结束、错误堆栈。
- 不记录 API Key、Token、完整敏感内容。

## 存储

第一版可以先使用 SQLite 保存最小任务状态和事件。

候选表：

- `sessions`
- `tasks`
- `turns`
- `steps`
- `events`
- `tool_calls`

如果实现成本需要继续收窄，可以先持久化 events 和 task status，再逐步补齐 steps 与 tool_calls。

但不能只依赖内存；否则无法满足“失败可诊断”和后续 checkpoint 基础。

## Checkpoint

第一版不做完整文件级回退，但需要先实现状态级 checkpoint 边界。

建议方式：

- Runtime 在关键 step 后产生可序列化 run snapshot。
- checkpoint 持久化到 SQLite，应用重启后可以列出任务已有 checkpoint。
- checkpoint snapshot 至少包含 task 状态、step 列表、工具调用事件历史、上下文摘要、文件变更元数据占位和当前 Agent 阶段。
- Checkpoint Manager 后续可继续演化为独立订阅者或接收 Runtime hook。
- 第一版至少保证 task / step / event 足够重建执行过程。

暂不实现：

- 文件 diff 回滚。
- 自动恢复运行中任务。
- checkpoint UI。

当前实现边界：

- Runtime 在 `run_started`、模型请求工具、工具调用完成、最终完成和失败终止前生成状态级 checkpoint。
- checkpoint 通过 `checkpoints` SQLite 表持久化，并通过 `GET /tasks/{task_id}/checkpoints` 查询。
- checkpoint 写入成功会产生 `checkpoint_created` Runtime Event；写入失败会记录 error 日志并产生 `checkpoint_failed` Runtime Event。
- 文件变更元数据当前为空列表，后续接入写文件工具和文件级回退时扩展。

## Context Compaction 预留

第一版不实现完整 compaction，但需要保留插入点。

插入位置：

```text
Context Builder
  -> before model call
  -> check token budget
  -> later: compact if needed
```

第一版如果超出上下文预算，可以先失败并给出明确错误；后续再改为自动压缩。

当前实现边界：

- Runtime 在每次调用 Model Adapter 前执行上下文预算校验。
- 第一版使用字符数作为 token budget 的近似代理，配置项为 `CODING_AGENT_MAX_CONTEXT_CHARS`。
- 超预算时任务进入失败路径，关闭运行中的 step，并通过 Runtime Event 返回 `context_window_exceeded` 错误。
- 后续接入真正 tokenizer 或 context compaction 时，应复用这个 model-call 前置入口，而不是把压缩逻辑写进具体模型适配器。

## Subagent 预留

第一版不实现 subagent。

但状态模型要避免阻碍 child run：

- `Task` 后续可产生 child task / child run。
- `Run` 后续可记录 parent_run_id。
- Event 后续可携带 parent / child 关系。

Subagent 仍然复用 Agent、Workflow、Runtime、Tool Scheduler。

## 后端模块建议

当前 `apps/backend/app/` 不提前铺空目录。进入实现时，建议按能力逐步创建：

```text
apps/backend/app/
  api/
  runtime/
  workflows/
  models/
  tools/
  context/
  events/
  storage/
  logging/
  config/
```

创建原则：

- 有明确职责再建目录。
- 不为了完整感提前铺模块。
- 每个目录只承载一个能力边界。

## API 草案

第一版 API 可以保持很窄：

```text
POST /tasks
  input: { session_id?, text }
  output: { task_id, turn_id, status }

GET /tasks/{task_id}
  output: task status

GET /tasks/{task_id}/events
  output: event list

GET /tasks/{task_id}/stream
  output: SSE runtime event stream

POST /tasks/{task_id}/cancel
  output: cancellation status
```

后续再扩展附件、审批、checkpoint、MCP 配置等接口。

## 客户端范围

第一版客户端只做支撑基础闭环的最小 UI：

- 纯文本输入框。
- 发送按钮。
- Agent 输出流。
- 工具调用状态展示。
- 任务运行状态展示。
- 错误展示。
- 取消按钮。
- 左侧项目/任务导航。
- 顶部当前任务栏。
- 右侧 Outputs / Sources 信息面板。

暂不做：

- 图片附件卡片。
- 文档附件卡片。
- checkpoint 列表。
- MCP 管理页。
- 多 Agent 视图。

第一版客户端布局参考 Codex 桌面客户端：

```text
左侧导航栏
  -> 项目列表 / 任务列表 / 当前任务选中态

顶部任务栏
  -> 当前项目或任务标题 / 更多操作

中央主会话区
  -> 用户消息 / Agent 输出 / 执行状态 / 文件链接 / 代码块

右侧信息面板
  -> Outputs / Sources / 后续上下文引用和工具产物

底部输入区
  -> 纯文本输入 / 权限状态 / 模型选择 / 发送 / 取消
```

第一版右侧面板可以先做只读列表，用于展示当前任务产生的文档和引用来源，不要求实现完整文件预览、checkpoint 或工具详情。

## 验收标准

第一版完成时应满足：

- 用户输入一段纯文本任务后，后端能启动 Agent Run。
- Runtime 能调用模型并收到响应。
- 模型请求工具时，工具调用经过 Tool Scheduler。
- 工具结果能作为 observation 回注给模型。
- Agent 能在 max steps 内完成或明确失败。
- 所有关键步骤都有事件。
- 所有关键错误都有日志。
- 用户能取消运行中任务。
- 工具不存在、参数错误、工具执行失败、模型无效输出都有明确错误路径。
- 后续新增 Workflow 不需要重写 Tool Scheduler。
- 后续新增 Model Adapter 不需要重写 Runtime。
- 后续新增附件输入不需要重写 Agent Loop。

## 第一版不变量

- 不把 Agent 等同于 Workflow。
- 不把 ReAct-like 写死成 Runtime。
- 不绕过 Tool Scheduler 执行工具。
- 不把模型 API 消息结构直接当内部核心数据结构。
- 不把任务状态只保存在内存。
- 不把错误只返回 UI 而不写日志。
- 不提前实现 subagent，但也不设计成阻碍 child run。

## 开放问题

- 第一版是否立即持久化完整 Session / Task / Turn / Step，还是先持久化 Task / Event？
- 第一版是否启用写文件工具，还是只开放 safe read 工具？
- 第一版 Tool Scheduler 的审批接口是否先做后端抽象、UI 后续接入？
- LangGraph 在第一版中承载完整 ReAct-like 状态图，还是先作为 Workflow Engine 的可替换实现之一？

## 变更记录

- 2026-07-13：创建第一版技术方案，确认纯文本输入、单 Agent、ReAct-like Workflow、串行 tool call 和可扩展 Runtime 边界。
- 2026-07-13：确认第一版模型后端调用使用 streaming 输出；后端到客户端使用 SSE 推送 Runtime 事件。

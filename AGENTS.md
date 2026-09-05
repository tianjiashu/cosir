# AGENTS.md

`cosir` 的长期 Agent 入口指南。它只记录已确认的方向、稳定的架构边界和强制工程约定；会随实现快速变化的目录细节、接口清单和阶段性计划应放在 `docs/`。代码事实与本文件冲突时，先核实代码，再在同一次改动中修正本文件。


# 架构决策
Agent Runtime
    ├── RuntimeContextManager
    │       └── ConversationTaskContextService
    │
    └── ConversationEventProjector
            └── ConversationTaskSnapshotService
                    ├── Snapshot schema validation
                    ├── set / append-text
                    ├── SQLite persistence
                    ├── in-process working copy
                    └── Transport notification
不需要事务保持强一致性，只需要在读取的时候，保持最终一致性即可。context和snapshot允许不一致。比如，AI说：好，我来看看... 。还没完整message，这个时候无需保持一致，允许context有一定滞后。
ConversationEventProjector 和 ConversationTaskSnapshotService不要参与context修改。context是由app/core/context/runtime_context_manager.py维护，ConversationEventProjector
和ConversationTaskSnapshotService仅维护快照
ConversationStateSnapshot是负责和前端交互的快照
workflow 内统一使用event吐出，然后在workflow统一处理event、
RunStatusChangedEvent 的所有发布点收拢在 ``ConversationRunService``：以数据库状态更新的原子成功
（``update_status_if_in`` 返回记录）作为幂等闸门，更新未生效则不发布，重复调用不会重复发布。
工作流编排层（如 ``WorkflowOperations``）不再经 ``get_stream_writer`` 直接发布 run-status 事件，
仅把 ``usage_stats`` 透传给 ``ConversationRunService`` 用于拼装带用量的状态事件。
## 1. 项目愿景

`cosir` 是面向个人开发者的本地桌面 AI 编程助手底座。它借鉴成熟 coding-agent 的工程机制，但不绑定单一 Agent 范式；用户应能持续定制 Workflow、Context、Tool 和开发规则，使它成为长期协作的工程伙伴。

当前处于绿地开发期。第一阶段目标是具备生产级的任务执行、工具、审批、检查点、委派、上下文压缩、可观测性、审查和测试闭环；第二阶段再依据真实使用沉淀个人化能力。

## 2. 已确认的产品与技术决议

- 不做 CLI；以 Windows、macOS 桌面应用作为主要交付入口。本地前后端作为一个应用交付，不部署为公共服务。
- 技术底座为 Tauri 2、React、TypeScript、Vite、Python、FastAPI、LangGraph 和 SQLite。模型接入先支持 OpenAI 兼容协议与 DeepSeek，后续以可扩展方式增加厂商。
- 这是 0–1 项目：不兼容旧版本、旧数据或旧架构。开发期的设计清晰度优先于迁移与回退成本。
- LangGraph 是工作流编排硬依赖：默认实现可以是 ReAct-like，但 Runtime 必须能演进到其他 Workflow。checkpoint、审批中断/恢复和 subagent 均属于 Runtime 能力，不能因依赖缺失而静默降级。最低 Python 版本为 3.11。
- 工具系统保持自定义，不能被 LangGraph Tool 抽象绑死。`ToolDefinition` 是工具契约的单一事实来源；LangGraph 节点通过项目自有 Tool Runtime 调度工具。
- 首版必须按生产级深度设计 MCP、工具权限审批、checkpoint、subagent、context compaction、工具系统、任务执行与审查/测试闭环。

## 3. 对话重构决议

- 对话主链路直接重构，不保留外部适配层、双协议端点、feature flag 灰度或旧链路回退。新链路验收后，旧 SSE、`eventStore`、`useSSE`、timeline projector 与 `TurnTimeline` 整体退场。
- `Task` 是 workspace、ChangeSet、context usage 和 delegation 的唯一业务归属边界，并承担 Conversation Thread 职责；现有 `Turn` 重构为 `ConversationRun`。
- `ConversationMessage`、`MessagePart`、`TurnAttachment`、`HumanApprovalRequest` 是持久化的对话事实。不得从 `input_text`、运行时日志或前端投影反推这些事实。
- 模型节点、工具执行、delegation 与审批只能经 conversation event 由 `ConversationEventProjector` 投影到 `ConversationTaskSnapshotService`（Transport snapshot 的唯一 owner）。Runtime 执行过程不再使用通用 `RuntimeEvent` 作为领域事件、UI 投影或事实源；审计、诊断和可观测性统一使用结构化日志、trace，以及必要时独立的 `AuditRecord`。
- 后端通过 Assistant Transport 传输由 canonical conversation state 派生的状态更新；桌面端以 `@assistant-ui/react` 的 `useAssistantTransportRuntime` 渲染。Assistant UI 的类型和实现不得进入 `core`、`tools`、`models` 或 `storage`。
- 工具权限/隔离/资源锁、LangGraph checkpoint、ChangeSet、delegation 的父子边界和审批决策仍属服务端领域能力。前端不得启用客户端工具执行。
- `apps/shared` 已移除；`apps/desktop` 按新的对话契约重建为 Tauri 托管的静态 React/Vite 前端。前端不包含服务端代理、不执行客户端工具，直接连接 Tauri supervisor 提供的本机 FastAPI 地址。

### 前端职责决议

- 前端以 UI 渲染、用户交互和 Assistant Transport 连接为主要职责，不承载 Agent、Context、Tool、审批、checkpoint 或对话事实的业务编排。
- `@assistant-ui/react` 的 `useAssistantTransportRuntime` 是前端与后端之间的运行时适配边界；它负责把 Composer/Thread 的用户命令发送到后端，并把后端 state stream 转换为 UI 状态。前端不能绕过 runtime 直接调用模型或执行客户端工具。
- 前端 converter 只负责后端 canonical state 与 Assistant UI 消息模型之间的协议映射；不得在 converter 中推导或替代后端领域状态机。
- 后端是 Conversation、Task、Workspace、Provider、Model、Tool、Approval、Checkpoint 和运行状态的唯一 canonical state 来源。前端的 React state 和 `localStorage` 只允许保存临时 UI 状态与用户偏好，不能作为对话事实来源。
- 前端可以在 `useAssistantTransportRuntime` 内持有不可持久化的 `TransportState` 渲染副本，但不得把它作为业务事实、持久化数据或下一次请求的权威输入。后端领域层、Agent Runtime 和 storage 必须对 `TransportState` 保持透明；Assistant Transport 只允许存在于 API/传输适配边界。
- 后端 API 适配层可以在一次 stream 生命周期内维护临时 Transport 基线，用于生成 `set` / `append-text` 等协议操作，但不得将该 UI 投影反向写入领域模型或数据库作为第二事实源。
- Workspace/Task/Provider 等页面级 CRUD 仅由前端发起 API 请求并呈现结果；权限、校验、级联、持久化和生命周期规则必须由后端负责。
- 新增前端对话能力时，优先复用 Assistant UI primitives/runtime 与 Assistant Transport 协议；不得重新引入旧 SSE、客户端 event store 或前端 Tool 执行链路。

## 4. 后端架构边界

后端当前是实现事实的主来源。依赖方向必须保持单向：

```text
api → core / service / llm_provider
core → service / tools / models / config
service → storage / models / config / tools
llm_provider → config / models / storage
storage → models
tools → config / models / utils
models → utils
utils → 无 app.* 依赖
```

- `api` 是 HTTP/SSE/Transport 接入层，不直接访问 `storage` 或工具 handler。Assistant Transport 的 state 组装、协议编码和 stream 生命周期管理必须限制在该适配边界；`core`、`service` 和 `storage` 只处理领域事实与运行状态。
- API 层请求校验与错误响应约定：纯 wire 契约约束（字段取值、commandId 唯一性、thread/task 一致性、未支持命令类型等，不依赖 DB 的领域状态）由请求 Pydantic 模型用 `@model_validator(mode="after")` 在解析阶段自动校验，不依赖端点显式调用；校验失败抛结构化异常（`TransportRequestError` 携带 `status_code`/`code`/`message`/`retryable`），**不得退化成 Pydantic 默认 422**，以保持前端 `TransportError` 错误体 `{error:{code,message,retryable}}` 契约零改动。全局异常处理器统一放在 `app/api/middleware/`，采用 `install_*` 函数式注册器（参照 `api_logging.py`），由 `app/app.py` 在 `app = FastAPI(...)` 之后、域路由 `import_module` 之前调用；依赖 task/turn/command 持久化状态的领域校验仍留端点，不进 schema。
- `core` 负责 LangGraph Runtime、工作流、上下文、delegation 和可观测性编排；`service` 负责领域服务编排；`storage` 只负责 SQLite 持久化；`tools` 是独立的工具执行体系。
- `utils` 必须是叶子层。`config/logging` 是允许依赖 `storage` 的聚合例外。
- 跨层协作优先通过位于低耦合边界的 Protocol/端口完成，例如工具追踪与 delegation 执行；不得以反向 import 偷渡依赖。

## 5. 工程强制约定

### Python 与依赖

- 一文件一主类，文件名使用 snake_case；服务文件使用 `xxx_service.py`。强相关的值对象工厂或纯工具函数可以同文件存在，但禁止 `Utils`、`Helper`、`Common`、`Misc`、`Manager` 等模糊命名。
- 领域值对象自行提供 `from_xxx` 映射，不额外建立无状态 Mapper。业务函数和方法使用完整中文 docstring（参数、返回、异常、副作用），修改签名时同步更新。
- 使用绝对导入；`__init__.py` 只做薄 re-export；禁止 `import *`。
- 后端依赖与锁文件的单一来源是 `apps/backend/pyproject.toml` 和 `uv.lock`。新代码必须通过 Ruff，完整标注类型，并遵从严格 mypy 基线；新增 `app/` 文件适用无存量豁免的 mypy 门禁。

### 日志与可观测性

- 业务日志统一使用 `from app.config.logging.logger import log`。只有日志子系统的装配文件可以直接调用 `logging.getLogger(...)`。
- 日志以稳定英文 snake_case `event`、中文 `msg`、结构化 `data` 和唯一 `trace_id` 记录；异常使用 `.exception()` 保留堆栈，禁止空捕获、`print` 充当日志和输出密钥。
- spawn 子进程经 `process_bridge` 汇入父进程的日志管线，不共享 logger 对象。
- Langfuse 依赖只允许出现在 `core/observability/`，且必须惰性加载、失败安全；可观测性不可中断 ConversationRun。

### 工具与文件安全

- 所有内置工具继承 `HandlerBase`，由 `ToolDefinition` 声明模型可见契约、权限、风险和执行隔离策略。
- 需 OS 级故障隔离的工具使用 `process` 执行模式（子进程、硬超时和树杀）；其他工具默认 `thread`。工具前后必须经过 Hook 拦截，`DENY` 是硬拒绝，Hook 自身失败不阻断主流程。
- 文件工具必须经过路径边界、revision/stale、重复调用和写路径锁检查；写前生成反向快照，ChangeSet 按 task 聚合并支持单文件撤销/保留；写入前执行已支持语言的语法检查。
- 工具输出与展示数据受预算约束。Web 工具必须经 URL 安全校验；CodeGraph 不可用时返回可诊断的降级结果，不得导致 Runtime 崩溃。
- 工具执行链只有一个入口 `ToolExecutor.execute`（`app/core/tools/tool_execute/tool_executor.py`）：「权限门禁 + 参数校验 + PreToolUse Hook → 文件状态协调 → 隔离执行 → PostToolUse Hook → 输出预算」全链路在此编排。门禁收口在 `tool_access_gate.py`，进程/线程隔离与硬超时强杀收口在 `tool_handler_runner.py`，双通道预算收口在 `tool_observation_budget.py`；不再存在 `ToolScheduler` 中间层。
- 工具结果处理收敛在 `observe` 节点：工具观察的终态事件（`ToolCallStatusChangedEvent`）、模型上下文写回、连续失败计数与错误上限判定只在 `observe` 节点发生。`tools` 节点只负责「执行前取消 + running 事件 + 执行 + 产出可落 checkpoint 的摘要」。
- 三处预算必须分清：`MAX_TOOL_OUTPUT_CHARS` 是单条观察模型通道的硬闸门（执行层，含脱敏与 artifact spill）、`DisplayDataBudget` 管展示通道（执行层）、`TOOL_OBSERVATION_CONTEXT_LIMIT` 管进 checkpoint 的摘要（observe 节点）。脱敏与截断一律在**执行层**完成，未治理的原始观察禁止进入 graph state / checkpoint。

### 测试、契约与临时文件

- 测试使用 pytest；真实 LLM 端到端冒烟以 `@pytest.mark.llm` 标记，默认不进入普通测试。关键路径必须覆盖工具、checkpoint、审批、日志、Web、delegation 和对话状态写入。
- `docs/api/openapi.json` 等生成物由既有脚本或 pre-commit 自动维护，禁止手改。新的前后端契约位置必须在重建桌面端时一并明确，不能复用已删除的共享目录约定。
- 临时验证脚本只放 `apps/backend/temp/`，用完清理，禁止散落仓库根目录。

## 6. Agent 协作与交付原则

- Agent 是工程协作伙伴，不是单纯执行器。应基于代码事实提出风险、取舍和建议，并清楚区分“必须做 / 建议做 / 以后做”。
- 候选方案不是决议；影响架构、数据模型、对外契约或安全边界的决定，须经用户确认后才写入本文件。
- 以长期稳定迭代为最高判据。必要时可以做结构性重构并引入成熟依赖；同时不得重复造轮子或以“改动小”为理由保留难维护的补丁结构。
- 每次实现必须形成与风险相称的验证和独立审查闭环；开发者不能自行充当唯一裁判。
- 大知识库、代码检索和子 Agent 只服务于当前决策，先筛选再汇总，避免把未经验证的细节写成长期规则。

## 7. 非目标

- 不把产品收缩为命令行工具、代码补全或普通聊天问答。
- 不把 Agent 固定为单一 ReAct 流程，也不把 Agent Loop 与 ReAct 混为一谈。
- 不为尚未验证的个人偏好提前堆叠抽象。
- 不为兼容历史实现牺牲领域模型和系统边界的清晰度。

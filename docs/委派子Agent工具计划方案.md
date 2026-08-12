# 委派子 Agent 工具计划方案

> 本文是计划方案，不是最终技术方案。目标是先把生产级委派子 Agent 能力的边界、风险、解法和分阶段标准定清楚；字段、接口、数据库迁移、前端组件和测试用例细节留到后续技术方案。

## 1. 目标

开发一个生产级 `delegate_task` 工具，使父 Agent 能把明确子任务委派给子 Agent，并让子 Agent 复用当前项目已有的 Agent Runtime、Workflow、工具系统、运行时事件、checkpoint、取消、日志与 Langfuse 观测能力。

核心目标：

- 子 Agent 不是第二套运行时系统，而是当前 Agent 执行底座上的一等子执行单元。
- 委派过程必须可观测、可取消、可审查、可回放、可定位问题。
- 父 Agent 只接收结构化压缩结果，不把子 Agent 的完整上下文直接塞回父 Agent。
- 支持审查类、分析类、代码开发类委派，但默认受预算、权限、深度和并发限制约束。
- 遵守第零铁律：以长期稳定迭代为最终目标，不为最小改动牺牲结构清晰，也不重复造轮子。

## 2. 当前代码事实

以下事实来自当前仓库代码，不是推测：

- `AgentRuntime` 位于 `apps/backend/app/core/runtime/runner.py`，当前负责 turn 生命周期、runtime event、取消、工具调度、Langfuse trace、稳定文件变更发布等执行编排。
- `AgentProfile` 位于 `apps/backend/app/core/agents/agent_profile.py`，当前描述 `agent_id`、角色、目标、允许工具、workflow、模型配置和 `turn`。
- 默认工作流是 `ReactLikeWorkflow`，位于 `apps/backend/app/core/workflows/react/workflow.py`，基于 LangGraph `StateGraph`，支持 `interrupt()` / `Command(resume=)` 审批恢复与 `AsyncSqliteSaver` checkpoint。
- 工具系统统一由 `ToolSystem`、`ToolRegistry`、`ToolScheduler` 装配，位于 `apps/backend/app/tools/`。内置工具通过 `HandlerBase` 约束，并由 `ToolDefinition` 作为工具契约单一事实来源。
- 工具参数模型当前统一放在 `apps/backend/app/tools/tool_models/`。
- `AgentProfileRegistry` 当前由 `apps/backend/app/config/configuration.py` 的 `build_agent_registry()` 播种，内置 `developer` 与 `developer_pro`。委派子 Agent 应沿用该注册表扩展 profile，而不是另建一套委派类型目录。
- 工具执行编排在 `apps/backend/app/service/tool_execution/tool_execution_service.py`，它负责工具生命周期事件、工具 observation 到模型消息的转换、取消占位 observation、内部异常收口。
- 运行时事件模型位于 `apps/backend/app/models/event/runtime_event.py`，事件枚举在 `apps/backend/app/models/enums/event_type.py`。当前没有委派类事件。
- runtime event payload 通过 `apps/backend/app/models/payload/registry/runtime_event_payload_registry.py` 注册；新增 `EventType` 必须同步新增 payload 模型和 registry 映射，否则回放/反序列化存在运行期风险。
- `TurnRecord` 位于 `apps/backend/app/models/turn_record.py`。当前字段包括 `turn_id`、`task_id`、`input_text`、`status`、`end_reason`、`response_text`、`agent_id` 等，没有 `parent_turn_id`、`delegation_id` 或深度字段。
- `TurnModel` 位于 `apps/backend/app/storage/model/turn_model.py`。当前 `turns` 表也没有父子关系列。
- `ToolExecutionContext` 位于 `apps/backend/app/tools/schemas/tool_execution_context.py`。当前承载 `task_id`、`workspace_id`、`workspace_root`、`turn_id`，没有委派上下文字段。
- 运行时事件服务当前目录是 `apps/backend/app/service/agent_runtime_event/`，不是早期文档中曾出现的 `service/runtime_event/`。
- Langfuse 三方依赖已收口在 `apps/backend/app/core/observability/`，不应在 service 或 tools 层直接 import Langfuse。

## 3. 核心设计原则

1. 复用现有 Runtime

   子 Agent 必须通过现有 `AgentRuntime` 或从中抽出的窄执行入口运行，不新建平行 loop。若当前 `AgentRuntime` 因职责过宽不适合嵌套调用，应优先结构性拆分运行入口，而不是在委派工具里复制执行逻辑。

2. 工具入口，core 执行，service 记录

   `delegate_task` 作为模型可调用工具暴露，但 tools 层不能直接依赖 service 编排业务，更不能直接运行 `AgentRuntime`。委派执行入口应放在 `core/delegation/` 或 `core/runtime/` 的窄适配层，由 runtime 构建工具执行链时注入给 handler。`service/delegation/` 只负责委派记录、策略持久化、状态聚合和查询，不反向 import core。

   注入边界必须通过轻量协议表达：tools 层最多依赖 `DelegateTaskExecutor` 这类 Protocol，不 import `core/delegation` 的具体实现。具体 executor 由 core/runtime 装配并注入 handler，避免为了接线方便形成 `tools -> core` 或 `tools -> service` 依赖。

3. 父子关系持久化

   父子关系不能只存在于内存或 UI。需要在持久化模型或独立委派记录中表达 parent-child 关系，支持重启后回放和排查。

4. 权限默认收窄

   子 Agent 的工具权限必须由父 Agent 的权限、child `AgentProfile.allowed_tools`、委派参数和系统策略共同求交集，不能让子 Agent 获得父 Agent 没有的工具权限，也不能让单次委派突破 child profile 自身的职责边界。

5. AgentProfile 是委派扩展点

   三类委派不应硬编码成三套 `delegation_type` 分支。第一版应内置三类 child `AgentProfile`，后续新增审查、测试、安全、前端等专用子 Agent 时，只扩展 profile 和策略，不改委派工具主流程。`delegation_type` 只作为展示、统计和输出 schema 标签，不作为执行决策的核心来源。

   稳定注册 profile 与单次运行 profile 必须分离：`delegate_reviewer` / `delegate_analyst` / `delegate_coder` 是注册表中的稳定 profile；每次委派运行时再派生本次 child profile，只收窄 tools、预算和 prompt context，不修改 registry 中的稳定 profile。

6. 结果压缩

   子 Agent 的完整事件流、消息轨迹和工具输出用于回放与审计；回填给父 Agent 的 observation 必须是结构化摘要。

7. 可失败但不污染

   子 Agent 失败应该表现为委派工具 observation 的失败或部分失败，不应破坏父 Agent 的工具协议闭环，也不应把父 turn 留在不可恢复状态。

## 4. 推荐最终能力边界

### 4.1 内置 child AgentProfile

第一版即支持三类委派，但实现基础是三类内置 child `AgentProfile`，不是三套平行实现：

- `delegate_reviewer`：只读审查 Agent。默认仅开放 `read_file`、`search_files`、`list_directory`、CodeGraph 查询工具。
- `delegate_analyst`：分析规划 Agent。默认开放只读工具和 Web 工具，是否开放 Web 由环境配置和工具可用性决定。
- `delegate_coder`：代码开发 Agent。可开放写文件、patch、终端等工具，但必须继承现有工具审批、路径边界、危险命令拒绝和文件变更快照机制。

`delegate_task` 的核心选择参数应是 child `agent_id`，例如 `delegate_reviewer` / `delegate_analyst` / `delegate_coder`。`delegation_type` 可以从 profile 派生为 `review` / `analysis` / `coding`，用于 UI 分类、统计、默认输出 schema 和 Langfuse metadata，但不应成为执行分支的主键。

### 4.2 默认限制

- 默认最大委派深度：`1`。
- 第一版默认最大并发子 Agent 数：`1`；架构预留提升到 `3`，但不在同步闭环阶段同时落地多子并发。
- 默认单个子 Agent 最大 step 数：小于父 Agent 的 `max_steps`，具体值后续技术方案确定。
- 默认总预算：父 turn 必须有一个子 Agent 总预算，避免多个子 Agent 分别耗尽资源。
- 默认不允许子 Agent 再创建无限子 Agent。
- 默认父 turn 取消时，所有未完成子 Agent 级联取消。

## 5. 计划新增能力模块

以下是计划方向，不表示当前仓库已经存在这些模块。

```text
apps/backend/app/core/delegation/
  delegation_executor.py
  child_agent_runner.py
  child_agent_profile_builder.py

apps/backend/app/core/agents/
  delegate_agent_profiles.py

apps/backend/app/service/delegation/
  delegation_service.py
  delegation_policy.py
  delegation_result.py
  delegation_context.py

apps/backend/app/storage/
  model/delegation_model.py
  crud/delegation_crud.py

apps/backend/app/tools/tool_handler/delegation/
  delegate_task.py
  delegate_task_executor.py

apps/backend/app/tools/tool_models/
  delegate_task_args.py

apps/backend/app/models/
  delegation_record.py
  payload/delegation_started_payload.py
  payload/delegation_child_started_payload.py
  payload/delegation_finished_payload.py
  payload/delegation_failed_payload.py
  payload/delegation_cancelled_payload.py
```

职责边界：

- `core/delegation/delegation_executor.py`：委派执行的 runtime 侧入口，负责把工具调用转成 child Agent 执行，不复制 Agent loop。
- `core/delegation/child_agent_runner.py`：复用或适配 `AgentRuntime` 的子执行入口，只处理 core 层执行语义。
- `core/delegation/child_agent_profile_builder.py`：基于注册表 profile 和委派策略生成子 Agent profile，不污染全局 profile。
- `core/agents/delegate_agent_profiles.py`：声明第一版内置 child `AgentProfile`，并由现有 `AgentProfileRegistry` 注册。
- `service/delegation/delegation_service.py`：委派记录和状态聚合服务，不直接运行 `AgentRuntime`。
- `service/delegation/delegation_policy.py`：深度、并发、预算、agent_id 允许委派校验、权限求交集和策略标签；只依赖模型和配置，不依赖 core。
- `service/delegation/delegation_result.py`：面向父 Agent 的结构化结果值对象。
- `storage/model/delegation_model.py`：`delegations` 表 ORM 模型，承载委派参数、父子关系、状态、摘要和失败原因。
- `storage/crud/delegation_crud.py`：委派记录的单实体 CRUD；`service/delegation` 通过依赖装配使用它，不直接写 SQL，也不跨层访问数据库。
- `tools/tool_handler/delegation/delegate_task.py`：工具 handler 薄壳，只依赖委派执行 Protocol，只接收 runtime 注入的委派执行器并返回 `ToolObservation`。
- `tools/tool_handler/delegation/delegate_task_executor.py`：定义 `DelegateTaskExecutor` Protocol。该协议位于 tools 可依赖的低层位置，只允许依赖普通数据、`ToolExecutionContext`、`ToolObservation` 等 tools/models 层可见对象；`core/delegation` 只提供协议实现并在 runtime 装配时注入，handler 不 import core 实现。
- `tools/tool_models/delegate_task_args.py`：`delegate_task` 参数模型，遵守现有工具参数模型目录事实。

持久化接线要求：新增 `delegations` 表时，必须同步规划 schema 初始化/迁移、`store_engines`/CRUD 装配和 `service/depends.py` 接线。`service/delegation` 只能编排 CRUD 和领域规则，不能把 SQL、ORM session 或 storage model 逻辑塞进 service 文件。

## 6. 父子执行关系

推荐执行链路：

```text
父 Agent workflow
  -> model 请求 delegate_task(child_agent_id=delegate_reviewer|delegate_analyst|delegate_coder)
    -> ToolExecutionService 进入工具执行协议
      -> delegate_task handler
        -> runtime 注入的 DelegationExecutor
          -> service/delegation 记录委派与策略校验
          -> AgentProfileRegistry 解析 child AgentProfile
          -> core/delegation 派生本次 child AgentProfile
          -> core/runtime 兼容入口运行 child turn
          -> service/agent_runtime_event 持久化 child runtime events
          -> service/delegation 汇总 child result
        -> ToolObservation
  -> 父 Agent 消费 observation 并继续
```

关键要求：

- 委派工具必须和普通工具一样产生 `TOOL_CALL_STARTED` / `TOOL_CALL_FINISHED`。
- 子 Agent 自己仍产生 `RUN_STARTED` / `MODEL_*` / `TOOL_*` / `FINAL_RESPONSE` / `RUN_FINISHED` 等事件。
- 委派级事件用于 UI 折叠、审计和聚合状态；每个新增事件必须有 payload 模型和 registry 映射。
- 父工具 observation 必须和模型 tool call 协议一一闭合，即使子 Agent 失败、取消或超时，也要返回结构化 error observation。

executor 注入硬约束：`DelegateTaskExecutor` 必须是 per-runtime 或 per-turn 作用域，不能写入进程级全局 handler 单例，也不能污染全局 `ToolDefinition.handler`。后续技术方案必须在两条路径中选择一种并验证分层：一是由 runtime 构建 scoped `ToolDefinition` / `ToolSystem`；二是在 `ToolScheduler` / `ToolExecutor` 增加 tools 层协议化 dependency channel。无论选择哪条路径，handler 都只能看到 Protocol，不能 import core 实现。

## 7. 持久化与状态

计划需要表达两类关系：

1. 子执行归属

   子 turn 需要能回答：

   - 它属于哪个 task。
   - 它由哪个 parent turn 发起。
   - 它对应哪个 delegation。
   - 它使用哪个 child agent profile。

2. 委派生命周期

   委派记录需要能回答：

   - 谁发起。
   - child agent_id 是什么。
   - 从 child profile 派生出的委派标签是什么。
   - 子 Agent 是否创建成功。
   - 当前状态是 pending、running、completed、failed 还是 cancelled；超时第一版属于 failed 的结构化 reason，不进入主状态枚举。
   - 最终摘要、失败原因和子 turn 引用是什么。

第一版 child turn 仍归属于父任务的同一个 `task_id`，不新建 task。父子执行通过 `parent_turn_id` / `delegation_id` 区分。这样可以复用现有 workspace、任务列表、runtime event 查询和变更聚合路径，避免第一版就引入跨 task 聚合复杂度。

可选路径：

- 在 `turns` 表增加父子关系字段，同时新增 `delegations` 表保存委派参数与聚合结果。
- 或只新增 `delegations` 表，子 turn 通过 `delegation_id` 间接关联。

推荐方向：同时新增 `delegations` 表，并在 `turns` 表增加最小父子字段。理由是查询和 UI 折叠更直接，委派审计也不被塞进 turn 字段。

## 8. 事件协议

计划新增委派事件：

- `DELEGATION_STARTED`
- `DELEGATION_CHILD_STARTED`
- `DELEGATION_FINISHED`
- `DELEGATION_FAILED`
- `DELEGATION_CANCELLED`

暂不新增 `DELEGATION_CHILD_EVENT_LINKED`。child event 与父委派的关联优先通过 `parent_turn_id` / `delegation_id` 字段表达，避免复制或桥接每条 child event。

事件设计原则：

- 委派事件只表达父子关系、状态和摘要，不复制完整 child event。
- child runtime event 继续按 child turn 独立持久化。
- 新增 `EventType` 必须同步新增 payload 模型、`runtime_event_payload_registry.py` 映射和前端共享协议。
- 前端需要按 `parent_turn_id` / `delegation_id` 折叠展示 child timeline。
- 回放时应能从父 turn 还原委派卡片，再按需展开子执行过程。

live SSE 路由原则：当前事件流按 turn 订阅，child event 不会天然出现在 parent turn stream 中。第一版必须明确采用以下两种方案之一，不能让 UI 自行猜测：

- 父 turn stream 支持 child event multiplex：parent stream 可收到携带 `parent_turn_id` / `delegation_id` 的 child 关键事件，尤其是 child 审批、取消和终态。
- 父 turn stream 只推送委派摘要事件：child timeline 和 child 审批通过 child turn 独立订阅或按需回放加载，但 `DELEGATION_CHILD_STARTED` 必须携带足够字段让前端建立订阅。

无论采用哪种方案，child 审批的 live 路由必须在 Phase 1 技术方案中闭合，不能推迟到 UI 阶段再补协议。

## 9. 取消、超时与并发

必须覆盖的边界：

- 父 turn 被用户取消：所有 running/pending child turn 必须收到取消信号。
- 子 Agent 超时：委派 observation 返回超时失败，child turn 落 `failed`，并通过 end reason 或失败 payload 的 `reason="timeout"` 表达。
- 子 Agent 部分失败：父 Agent 获得结构化失败结果，而不是丢失 tool observation。
- 多个子 Agent 并发：Phase 1 先固定 per-parent-turn 并发为 `1`；Phase 2 提升到 `3` 时必须增加 per-parent-turn semaphore，避免同一父 turn 无限占用资源。
- 进程重启：pending/running 委派不能静默悬挂；技术方案必须给出可恢复策略。若 MVP 先标记失败，也必须写入明确 end reason、委派失败事件和可排查日志，并保留后续 checkpoint 恢复升级路径。

推荐策略：

- `DelegationPolicy` 统一校验 depth、并发、预算和工具权限。
- runtime 侧 `DelegationExecutor` 管理运行期子任务集合，service 侧只保存记录和状态。
- 父取消时先标记父 turn，再级联标记 child turn，最后让正在执行的工具链通过现有 `should_cancel` 协作退出。
- 第一版不新增 `DELEGATION_TIMED_OUT` 事件；超时统一用 `DELEGATION_FAILED` 携带 `reason="timeout"` 或等价结构化原因表达。只有后续 UI/统计需要把超时作为独立生命周期类型时，才新增专门事件。

## 10. 审批嵌套

coding 委派会触发子 Agent 工具审批。计划阶段必须固定以下原则，后续技术方案再展开字段和 UI：

- child 审批归属于 child turn，而不是 parent turn。
- 审批事件必须携带 `parent_turn_id` 和 `delegation_id`，使 UI 能在父任务内折叠展示并路由到 child resume。
- 父 `delegate_task` 工具调用在 child 审批期间保持 pending，不提前闭合 observation。
- 用户批准或拒绝 child 工具调用后，恢复的是 child turn 的 checkpoint；child turn 完成、失败或取消后，`delegate_task` 才回填父 tool observation。
- 若父 turn 在 child 审批期间被取消，child 审批必须失效，child turn 级联取消，父 observation 返回取消类 error observation。

## 11. 权限与安全

委派工具本身属于高影响工具，因为它能间接触发更多模型调用和工具调用。

必须满足：

- 子 Agent allowed tools = 父 Agent 可用工具、child `AgentProfile.allowed_tools`、请求参数工具列表、系统策略四者求交集。
- `coding` 类型必须走现有文件路径边界、文件协作状态、危险命令拒绝、工具审批和文件快照机制。
- 子 Agent 不能越过 workspace 根路径。
- 子 Agent 不能绕过 `ToolScheduler`。
- 子 Agent 不能直接访问 Langfuse SDK、storage CRUD 或 API 层。
- 委派参数和结果必须经过脱敏，不能把 secret 原文写入 runtime event、日志或 Langfuse。

分层输入约束：权限求交集的计算可以由 `service/delegation` 承担，但 service 不能接收或 import `AgentProfile`。`core/delegation` 负责解析父/子 `AgentProfile`，再把 `parent_tool_names`、`child_tool_names`、`requested_tool_names`、`child_agent_id` 等普通数据传给 policy。

## 12. 观测与日志

必须实现：

- 父 `delegate_task` tool span 记录委派输入、状态、耗时、child turn id 和摘要。
- child turn 自己有独立 Langfuse trace，metadata 带 `parent_turn_id`、`delegation_id`、`delegation_type`。
- 可选地在 Langfuse metadata 中记录 parent-child 关系，不要求手写通用 tracing 框架。
- 日志必须记录委派开始、子 turn 创建、策略拒绝、取消传播、完成、失败、超时。
- 日志仍使用 `from app.config.logging.logger import log`，禁止散落 `print` 或直接 `logging.getLogger`。

不做：

- 不在 service 或 tools 层直接 import Langfuse。
- 不为委派单独实现一套 tracing 框架。

## 13. 父 Agent 上下文回填

父 Agent 接收的 `ToolObservation.content` 应该是紧凑文本，`data` 应该是结构化字段。

建议结果结构：

```json
{
  "status": "completed",
  "delegation_id": "...",
  "child_turn_id": "...",
  "summary": "...",
  "findings": [],
  "changed_files": [],
  "artifact_refs": [],
  "error": null
}
```

强约束：

- 不回填 child 全量消息。
- 不回填 child 全量工具输出。
- 大结果通过 artifact 或 runtime event 回放查看。
- 对 coding 委派，必须明确 changed files 和风险摘要。

## 14. UI 展示计划

后续前端应把委派展示为父对话中的可折叠执行块：

- 默认显示委派名称、状态、子 Agent、耗时、摘要。
- 展开后显示 child timeline。
- child timeline 应复用现有对话过程 UI 投影能力，而不是写第二套渲染。
- 多子 Agent 并发时按 delegation started 时间排序，同时显示各自状态。
- child 审批请求应显示在对应委派块内，并能路由到 child turn resume。
- 取消父 turn 时 UI 必须能看到 child cancelled 状态。

本方案不实现 UI，但要求后续事件协议必须服务于该展示模型。

## 15. 关键边界与解法清单

| 边界问题 | 风险 | 解法 |
| --- | --- | --- |
| 子 Agent 变成第二套 runtime | 长期维护成本爆炸 | 复用 `AgentRuntime`，必要时在 core 层拆出子执行入口 |
| 工具层直接编排业务 | tools 反向依赖 service/core，破坏目录边界 | tools 只做契约和薄 handler，runtime 注入 `DelegationExecutor` |
| service 运行 AgentRuntime | service -> core 反向依赖 | 子执行放 `core/delegation`，service 只记录和聚合 |
| service policy 接收 AgentProfile | service 依赖 core/agents，破坏分层 | core 解析 profile 后传普通数据给 policy |
| 递归委派失控 | 无限模型调用和工具调用 | `delegation_depth`、`max_children`、总预算硬限制 |
| 委派类型硬编码 | 新增子 Agent 时主流程持续膨胀 | 以 `AgentProfile` 为扩展点，`delegation_type` 仅作派生标签 |
| 权限扩大 | 子 Agent 绕过父权限 | 权限求交集，所有工具仍经 `ToolScheduler` |
| 父取消但子还在跑 | 孤儿执行、文件继续变更 | 父子 cancellation 绑定，级联取消 |
| 子失败导致父工具协议断裂 | 模型后续对话崩溃 | 委派失败也必须返回 error observation |
| 上下文爆炸 | 父 Agent token 被 child 输出污染 | 只回填摘要和引用 |
| 事件重复复制 | 回放数据膨胀、UI 混乱 | child event 独立存储，父事件只存关联 |
| 事件 payload 缺失 | 新事件运行期反序列化失败 | 新增 EventType 必须同步 payload、registry 和共享协议 |
| Langfuse 关系丢失 | 无法观察完整执行树 | child trace metadata 带 parent/delegation 关系 |
| 写代码委派冲突 | 多 Agent 同时改同一文件 | 复用文件路径锁、revision/stale 检测和快照机制 |
| checkpoint 恢复语义不清 | 重启后状态不可信 | 技术方案必须明确恢复策略；MVP 标失败也要可审计且可升级 |
| timeout 事件膨胀 | 生命周期事件过细导致协议过早固化 | 第一版用 `DELEGATION_FAILED` + timeout reason 表达 |
| 审批嵌套 | 父工具调用等待，子工具又等待审批 | child 审批归属 child turn；父 observation 等 child 完成后闭合 |
| 观测故障 | Langfuse 故障影响执行 | 复用现有 observability 失败隔离约定 |

## 16. 建议分阶段

### Phase 1：同步委派闭环

- 新增 core 委派执行入口、三类内置 child AgentProfile、service 委派记录/策略、工具 handler、基础模型和事件。
- 子 Agent 同步执行，父 Agent 等待结果。
- 支持 `delegate_reviewer`、`delegate_analyst`、`delegate_coder` 三类 child Agent。
- 默认深度 1、并发 1。Phase 1 只做同步委派闭环，避免并发调度、UI 聚合和文件冲突语义同时进入首版。
- 完成父子取消、child 审批归属、失败 observation、日志和 Langfuse metadata。

### Phase 2：并发与 UI 折叠

- 将同一父 turn 的最大并发子 Agent 数从 1 提升到 3。
- 前端折叠展示 child timeline。
- 增加并发状态聚合和取消传播验证。
- 如果 Phase 1 选择父 stream 摘要 + child 独立订阅，Phase 2 再优化多 child timeline 的聚合展示；不得新增第二套 timeline projector。

### Phase 3：恢复与高级调度

- 明确进程重启后的委派恢复策略。
- 支持更细粒度预算。
- 评估是否引入 LangGraph `Send` / subgraph 作为 workflow 层并行委派能力。

## 17. 验收标准

后续开发必须满足：

- 代码审查通过：没有平行 runtime、没有 tools -> service/core 直接编排、没有 service -> core 反向依赖、没有跨层直接访问 storage、没有 service/tools 直接依赖 Langfuse。
- 单元测试覆盖策略拒绝、权限求交集、深度限制、结果压缩、失败 observation。
- 集成测试覆盖父 Agent 发起委派、子 Agent 工具调用、父取消级联子取消、子失败不破坏父 tool call 协议。
- 审批测试覆盖 child 审批归属于 child turn、父 observation 等待 child 终态后闭合。
- 事件回放测试覆盖父 turn 能关联 child turn。
- Langfuse 测试或 mock 验证 child trace metadata 携带 parent/delegation 关系。
- 文件变更测试覆盖 coding 子 Agent 的快照、stale、路径锁和最终 changed files 摘要。
- UI 测试覆盖委派折叠块不和普通对话消息混淆。

## 18. 后续技术方案必须回答的问题

- `AgentRuntime` 是否需要拆出 `run_child_turn()`、`run_claimed_turn()` 或 `DelegationExecutor`，以避免 `run_turn()` 的 pending claim 语义和子执行冲突。
- runtime 如何把 `DelegationExecutor` 注入 `delegate_task` handler，同时保持 `ToolDefinition` 仍是工具契约单一事实来源。
- `DelegateTaskExecutor` Protocol 的放置目录、精确方法签名，以及 runtime 注入到 handler 的生命周期与空实现/禁用策略；需评估放在 `tools/tool_handler/delegation/` 还是更中性的 tools 协议目录，避免 handler 目录承担协议聚合职责。
- executor 注入通道选择：scoped `ToolDefinition` / `ToolSystem`，还是 `ToolScheduler` / `ToolExecutor` 的协议化 dependency channel；必须避免全局可变 executor 和 handler import core。
- 三类内置 child `AgentProfile` 如何命名、注册、展示，以及是否放在 `core/agents/delegate_agent_profiles.py`。
- 稳定注册 profile 与单次派生 profile 的字段差异，以及派生 profile 如何避免污染 registry。
- 父子 turn 字段和 `delegations` 表的精确 schema。
- child turn 第一版复用父 `task_id` 的查询、回放和文件变更聚合影响范围。
- 委派事件 payload 的精确字段，以及 `runtime_event_payload_registry.py` 和前端共享协议如何同步。
- `delegate_task` 工具参数 schema 和显示协议，尤其是 `child_agent_id`、可选 `allowed_tools` 与派生 `delegation_type` 的关系。
- 子 Agent profile 如何在稳定内置 profile 与单次动态派生 profile 之间划界。
- child 审批事件如何通过 SSE 暴露、如何恢复 child checkpoint、如何在父 UI 内展示。
- parent turn stream 与 child turn stream 的 live 路由关系：必须在技术方案中从 multiplex 和“摘要 + 独立订阅”二选一，并明确 child 审批请求如何实时到达 UI，不能让前后端并行实现时各自猜协议。
- checkpoint 恢复时未完成 child turn 如何处理：必须细化 pending/running delegation 的重启扫描、失败审计、是否恢复 child checkpoint，以及 MVP 标失败策略如何保证可审计和可升级。
- 超时是否长期维持为 failed reason，还是后续升级为独立事件和状态。
- 并发执行是否使用 `asyncio.TaskGroup`，以及 Windows/Python 3.11 下的取消语义。
- 前端 timeline projector 如何复用现有事件投影。

## 19. 当前推荐结论

推荐建设方向是：基于 `AgentProfile` 扩展三类内置 child Agent，配合 `delegate_task` 工具契约、runtime 注入的 `DelegationExecutor`、`service/delegation` 记录/策略/聚合、子 turn 持久化，并复用 `AgentRuntime` 形成生产级闭环。

这比“只在 prompt 中让模型模拟子 Agent”成本更高，但它能给出可观测、可取消、可审计、可回放、可测试的真实子 Agent 能力，符合第零铁律。

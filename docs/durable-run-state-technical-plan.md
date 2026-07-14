# Durable Run State 技术方案

本文规划 `Durable Run State` 的代码落点、职责边界和演进顺序。目标不是写详细代码，而是在进入实现前先明确“什么能力应该写在哪个文件/目录”，避免审批、checkpoint、human-in-loop、文件编辑和命令执行在开发过程中堆进同一个 Runtime 文件。

本文遵守：

- `rules/Agent代码开发规范.md`
- `rules/Agent客户端代码开发规范.md`
- `docs/agent-runtime-loop.md`
- `docs/production-acceptance.md`

## 1. 核心决议

采用成熟机制，不自研 suspend / resume 内核。

本项目的中断恢复能力以 **LangGraph interrupt + durable checkpointer + thread_id + Command(resume=...)** 为核心机制。审批、human-in-loop、checkpoint 恢复、后续危险工具执行，都应围绕这个机制建设外围工程。

参考机制：

- LangGraph Interrupts: `https://docs.langchain.com/oss/python/langgraph/interrupts`
- LangGraph Persistence: `https://docs.langchain.com/oss/python/langgraph/persistence`

关键约束：

- `thread_id` 是恢复同一运行状态的稳定指针。
- graph state 必须通过 checkpointer 持久化。
- 恢复不是唤醒旧 Python 协程，而是用同一个 `thread_id` 和 `Command(resume=...)` 重新调度。
- interrupt 前不能执行不可重复的危险副作用；文件写入、命令执行、Git 写操作必须放在审批恢复之后。
- interrupt 前如确实有副作用，必须有幂等键和副作用记录。

## 2. 能力边界

`Durable Run State` 不是聊天历史，也不是 checkpoint 本身。

它的职责是让一次 `Task / Run` 在以下场景中可以安全恢复：

- 等待用户审批。
- 等待 human-in-loop 输入。
- 应用或后端重启。
- 模型流、工具调用或命令执行中断。
- checkpoint 恢复和回看。
- 后续 subagent 等待结果。

不属于本方案的职责：

- 不设计新的通用 workflow 引擎。
- 不替代 LangGraph checkpointer。
- 不在前端存储运行事实源。
- 不实现完整 MCP、subagent、文件编辑和命令执行细节。

## 3. 推荐目录结构

后端新增目录：

```text
apps/backend/app/runs/
  __init__.py
  records.py
  store.py
  state_machine.py
  graph_builder.py
  checkpointer.py
  invoke.py
  recovery.py
  resume.py
  langgraph_runtime.py

apps/backend/app/approvals/
  __init__.py
  records.py
  store.py
  service.py

apps/backend/app/human_input/
  __init__.py
  records.py
  store.py
  service.py

apps/backend/app/tool_execution/
  __init__.py
  records.py
  store.py
  policy.py
  policy_provider.py
  service.py

apps/backend/app/artifacts/
  __init__.py
  records.py
  store.py
  files.py
  retention.py

apps/backend/app/tools/file_edit/
  __init__.py
  definitions.py
  planning.py
  apply_patch.py
  write_file.py
  diff_preview.py

apps/backend/app/tools/command/
  __init__.py
  definitions.py
  policy.py
  process.py
  output.py
```

前端新增目录：

```text
apps/desktop/src/components/approvals/
  ApprovalCard.tsx
  ApprovalDetail.tsx
  ApprovalActions.tsx

apps/desktop/src/hooks/
  useApprovals.ts
  useRunRecovery.ts

apps/desktop/src/services/
  approvals.ts
  runs.ts

apps/desktop/src/stores/
  approvalStore.ts
  runStore.ts
```

共享类型新增：

```text
packages/shared/ts/runs.ts
packages/shared/ts/approvals.ts
packages/shared/ts/toolExecution.ts
```

理由：

- `runs/` 承载可恢复运行状态，不让 `runtime/runner.py` 继续膨胀。
- `runs/graph_builder.py`、`runs/checkpointer.py`、`runs/invoke.py` 拆开 LangGraph 图构建、持久化装配和调用恢复，避免 `langgraph_runtime.py` 变成新的核心垃圾桶。
- `approvals/` 只处理审批请求和决策，不直接执行工具。
- `human_input/` 为后续非审批类人工输入预留同构能力。
- `tool_execution/` 处理工具执行记录、策略和幂等，不混入具体工具实现。
- `artifacts/` 处理大输出、diff、命令日志、工具结果摘要等运行产物，避免塞进事件表或工具执行表。
- `tools/file_edit/` 与 `tools/command/` 是具体工具族，避免把危险工具堆进 `safe_read.py`。
- 前端审批 UI、hook、service、store 分层，避免 `ChatPanel.tsx` 继续承载审批业务逻辑。

## 4. 后端代码落点

### 4.1 `apps/backend/app/runs/records.py`

写入能力：

- `RunRecord`
- `RunStatus`
- `RunWaitReason`
- `RunContinuation`
- `ResumeCommandRecord`

职责：

- 定义可持久化 Run 状态值对象。
- 描述 `task_id`、`thread_id`、`status`、`active_step_id`、`active_wait_id`、`last_checkpoint_id`、`interruption_reason` 等字段。

不写入：

- 数据库 SQL。
- LangGraph 调用。
- API 响应格式化。

理由：

- 状态对象是跨 storage、runtime、API、测试共享的领域模型，独立文件能避免 `storage/records.py` 继续扩张成所有记录的容器。

### 4.2 `apps/backend/app/runs/store.py`

写入能力：

- `DurableRunStore`
- run 状态查询。
- run 状态更新。
- resume command 持久化和幂等查询。

职责：

- 封装 run 相关持久化读写。
- 对上提供语义化方法，例如 `mark_waiting()`、`mark_resuming()`、`mark_interrupted()`。

不写入：

- 状态流转规则。
- LangGraph 恢复逻辑。
- 审批业务规则。

理由：

- 数据层只做增删改查和数据映射，符合分层规范。

### 4.3 `apps/backend/app/runs/state_machine.py`

写入能力：

- `RunStateMachine`
- 合法状态流转校验。
- 非法流转错误类型。

职责：

- 定义 `created -> running -> waiting -> resuming -> completed/failed/cancelled` 等合法转换。
- 阻止从 `completed` 继续恢复、从 `cancelled` 执行工具等非法行为。

不写入：

- 数据库写入。
- 具体工具执行。

理由：

- 状态流转是业务规则，不应散落在 API、workflow 或 storage 中。

### 4.4 `apps/backend/app/runs/graph_builder.py`

写入能力：

- LangGraph graph 构建。
- 默认 ReAct-like graph 节点组装。
- graph 版本标识。

职责：

- 只负责把 workflow 节点、边和状态 schema 组装成 LangGraph graph。
- 将现有 ReAct-like workflow 逐步迁移为可被 LangGraph 调度的节点结构。

不写入：

- checkpointer 装配。
- `thread_id` 配置。
- HTTP/API 恢复逻辑。
- 审批 API。
- 文件编辑和命令执行细节。

理由：

- graph 构建会随 workflow 演进而变化，必须独立于持久化和调用恢复。

### 4.5 `apps/backend/app/runs/checkpointer.py`

写入能力：

- checkpointer 装配。
- LangGraph 官方 SQLite checkpointer 创建。
- checkpointer 初始化和 schema 准备。
- `thread_id` / checkpoint namespace 映射规则。

职责：

- 优先复用 LangGraph 官方 SQLite checkpointer，例如 `langgraph-checkpoint-sqlite` 能力。
- 统一管理 graph state 持久化位置，避免手写 LangGraph checkpoint 表。
- 明确 LangGraph checkpoint 数据库和本项目业务 SQLite 的连接复用或分库策略。

不写入：

- 本项目 `runs`、`approvals`、`tool_execution` 业务表的 CRUD。
- graph 节点构建。
- resume command 分发。

理由：

- checkpointer 是成熟机制的持久化适配层。如果不独立，初始化、schema 和 thread 配置很容易散落到 `storage/sqlite.py` 或 `langgraph_runtime.py`。

### 4.6 `apps/backend/app/runs/invoke.py`

写入能力：

- `thread_id` 配置封装。
- `interrupt()` 和 `Command(resume=...)` 的项目内适配。
- LangGraph `invoke` / `ainvoke` / streaming invoke 调用。

职责：

- 所有恢复调用都必须通过同一个 `thread_id` 和显式 resume command。
- 将 LangGraph 调用结果转换成 Runtime 事件或 service 可消费的结果。

不写入：

- 审批 API。
- 文件编辑和命令执行细节。
- SQLite schema 迁移细节。
- checkpointer 初始化。
- graph 构建。

理由：

- 调用恢复是运行时边界，独立后可以避免 API、审批服务或工具服务直接调用 LangGraph 原语。

### 4.7 `apps/backend/app/runs/langgraph_runtime.py`

写入能力：

- `graph_builder`、`checkpointer`、`invoke` 的 facade 组装。
- 对 `RuntimeRunner` 暴露单一运行入口。

职责：

- 作为 Runtime 层访问 LangGraph 的窄门面。
- 只做依赖组装和少量编排，不直接承载 graph 构建、checkpointer 初始化或 resume 调用细节。

不写入：

- graph 节点定义。
- checkpointer schema。
- 审批、human input、工具执行业务逻辑。

理由：

- 保留一个 facade 便于现有 Runtime 迁移，但必须把具体职责拆到子模块，避免形成新的核心屎山。

### 4.8 `apps/backend/app/runs/resume.py`

写入能力：

- `ResumeDispatcher`
- approve / deny / human input / retry / cancel 等恢复命令分发。
- resume command 幂等处理。

职责：

- 接收已验证的恢复命令。
- 更新 run 状态为 `resuming`。
- 调用 `runs/invoke.py` 用同一 `thread_id` 继续执行。

不写入：

- HTTP 参数校验。
- 审批决策规则。
- 具体工具 handler。

理由：

- 恢复是 Runtime 编排能力，必须独立于审批和具体工具。

### 4.9 `apps/backend/app/runs/recovery.py`

写入能力：

- `RecoveryManager`
- 后端启动时的 running / waiting / interrupted 状态对账。

职责：

- 找出重启前处于 `running`、`waiting`、`resuming` 的 run。
- 对 `waiting` 的审批或 human input 保持可恢复。
- 对无法确认副作用的运行标记为 `needs_review` 或 `interrupted`。

不写入：

- 前端展示逻辑。
- 具体审批按钮行为。

理由：

- 启动恢复是独立生命周期能力，不应塞进 `main.py`、`runner.py` 或 API dependency。

## 5. 审批代码落点

### 5.1 `apps/backend/app/approvals/records.py`

写入能力：

- `ApprovalRequestRecord`
- `ApprovalDecisionRecord`
- `ApprovalStatus`
- `ApprovalRiskLevel`

职责：

- 定义审批请求、审批结果、风险等级、展示 payload。

理由：

- 审批是 human-in-loop 的一种具体业务形态，不能继续只作为 `ToolObservation.status = approval_required`。

### 5.2 `apps/backend/app/approvals/store.py`

写入能力：

- 创建审批请求。
- 查询 pending approvals。
- 记录 approve / deny 决策。
- 幂等处理重复 approve / deny。

职责：

- 只处理审批数据持久化。

理由：

- 前端重启后恢复审批卡片必须以数据库为事实源，SSE 不能是唯一事实源。

### 5.3 `apps/backend/app/approvals/service.py`

写入能力：

- 创建审批 wait payload。
- approve / deny 的业务校验。
- 将审批决策转换成 `ResumeCommand`。

职责：

- 连接审批领域和 `ResumeDispatcher`。

不写入：

- 直接执行工具。
- 直接调用模型。

理由：

- 审批服务只负责“用户是否允许继续”，不承担工具执行职责。

## 6. Human-in-loop 代码落点

### 6.1 `apps/backend/app/human_input/records.py`

写入能力：

- `HumanInputRequestRecord`
- `HumanInputResponseRecord`
- 输入 schema 和选项定义。

职责：

- 定义后续“问用户一个问题”“请求用户选择”“请求用户补充信息”等通用人工输入请求。

理由：

- 审批不是唯一 human-in-loop。提前抽出 `human_input/`，避免未来把所有人工交互都堆到 `approvals/`。

### 6.2 `apps/backend/app/human_input/store.py`

写入能力：

- 创建 human input 请求。
- 查询 pending human input。
- 记录用户响应。
- 对重复响应做幂等处理。

职责：

- 只处理 human input 请求和响应的持久化。
- 支持应用重启后重新展示未完成的人工输入请求。

不写入：

- 审批业务规则。
- LangGraph resume 调用。
- 前端展示格式。

理由：

- human-in-loop 是核心恢复场景。独立 store 可以避免后续把人工输入持久化混进 `approvals/` 或 `runs/`。

### 6.3 `apps/backend/app/human_input/service.py`

写入能力：

- 创建 human input interrupt payload。
- 将用户输入转换为 `ResumeCommand`。

职责：

- 连接通用人工输入领域和 `ResumeDispatcher`。

不写入：

- human input 持久化 CRUD。
- 审批业务规则。
- 具体 LangGraph 调用。

理由：

- 与审批共享 LangGraph interrupt/resume 底座，但保留独立业务语义。

## 7. Tool Execution 代码落点

### 7.1 `apps/backend/app/tool_execution/records.py`

写入能力：

- `ToolCallRecord`
- `ToolExecutionRecord`
- `ToolExecutionStatus`
- `ToolEffectStatus`

职责：

- 记录工具调用、审批状态、执行状态、副作用状态、幂等键。

理由：

- 当前 `ToolCall` 和 `ToolObservation` 是内存值对象，无法支撑恢复和审计。持久化工具执行记录必须独立建模。

### 7.2 `apps/backend/app/tool_execution/policy.py`

写入能力：

- 工具策略编排。
- 是否需要审批。
- 是否禁止执行。

职责：

- 作为策略编排器，从通用权限策略和具体工具族 policy provider 收集结果。
- 输出统一的 `ToolPolicyDecision`，包含 allow / deny / approval_required、风险等级和原因。

不写入：

- 命令风险细节。
- 文件路径编辑规则细节。
- 用户审批决策持久化。
- 工具 handler 执行。

理由：

- 现有 `app/tools/approval.py` 只有 permission 字符串判断，生产级工具执行需要更细的策略边界。
- 通用策略层只做编排，避免和具体工具族重复实现命令、路径、diff 等风险判断。

### 7.3 `apps/backend/app/tool_execution/policy_provider.py`

写入能力：

- `ToolPolicyProvider` 协议。
- policy provider 注册。
- policy provider 调用上下文类型。

职责：

- 让 `tools/file_edit`、`tools/command`、未来 MCP 工具族提供自己的风险判断。
- 为通用 `tool_execution/policy.py` 提供稳定扩展点。

不写入：

- 具体命令黑名单。
- 具体文件编辑规则。
- 审批持久化。

理由：

- 这是避免重复造轮子的关键边界。命令工具和文件工具只写自己的 provider，通用工具执行层只消费 provider 结果。

### 7.4 `apps/backend/app/tool_execution/service.py`

写入能力：

- 创建 `ToolCallRecord`。
- 调用 policy 判断是否需要 interrupt。
- 执行已批准工具。
- 写入 tool observation。

职责：

- 作为 Runtime 和具体工具之间的业务编排层。

不写入：

- 具体文件编辑算法。
- 具体命令进程管理。

理由：

- Runtime 不应该知道每种工具的内部细节；具体工具也不应该知道 LangGraph 恢复细节。

## 8. Artifact 代码落点

### 8.1 `apps/backend/app/artifacts/records.py`

写入能力：

- `ArtifactRecord`
- `ArtifactKind`
- `ArtifactRetentionPolicy`
- artifact 元数据，例如来源 step、MIME 类型、大小、hash、存储路径。

职责：

- 定义运行产物的持久化记录。

理由：

- 大 diff、命令 stdout/stderr、工具结果摘要不能长期塞进事件 payload 或 tool execution 表。

### 8.2 `apps/backend/app/artifacts/store.py`

写入能力：

- 创建 artifact 元数据。
- 查询 artifact。
- 将 artifact 关联到 run、step、tool execution。

职责：

- 只处理 artifact 元数据持久化。

不写入：

- 文件系统读写。
- 内容截断和脱敏规则。

理由：

- 元数据和物理文件写入分离，避免 store 变成基础设施和业务混合层。

### 8.3 `apps/backend/app/artifacts/files.py`

写入能力：

- artifact 内容落盘。
- 路径生成。
- hash 校验。
- 大文件读取保护。

职责：

- 只负责 artifact 文件系统读写。

理由：

- 命令输出和 diff 预览可能很大，必须有统一落盘位置和大小保护。

### 8.4 `apps/backend/app/artifacts/retention.py`

写入能力：

- artifact 保留策略。
- 过期清理计划。
- 按 run / task 清理 artifact。

职责：

- 只负责 artifact 生命周期策略。

理由：

- artifact 是运行产物，需要可控增长，不能无限堆在 `storage/`。

## 9. 文件编辑工具代码落点

### 9.1 `apps/backend/app/tools/file_edit/definitions.py`

写入能力：

- `write_file`、`apply_patch`、`inspect_diff` 的 `ToolDefinition`。

职责：

- 只声明工具名称、描述、schema、permission、默认 timeout。

理由：

- 工具声明和工具实现分离，避免 `safe_read.py` 式文件继续承载多种职责。

### 9.2 `apps/backend/app/tools/file_edit/planning.py`

写入能力：

- 文件编辑计划对象。
- 目标路径解析。
- 文件 hash / mtime 预检查。
- diff 预览元数据。

职责：

- 在真正写入前形成可审批的编辑计划。

理由：

- 审批界面需要展示“将改什么”，不能在执行时才生成 diff。

### 9.3 `apps/backend/app/tools/file_edit/apply_patch.py`

写入能力：

- patch 解析。
- 上下文匹配。
- hash mismatch 处理。
- patch 应用结果。

职责：

- 只负责结构化 patch 应用。

理由：

- patch 应用比普通写文件复杂，必须独立文件，避免 `write_file.py` 膨胀。

### 9.4 `apps/backend/app/tools/file_edit/write_file.py`

写入能力：

- 新建、覆盖、追加。
- 临时文件 + 原子替换。
- 二进制拒绝。
- 大文件保护。

职责：

- 只负责整文件写入类操作。

理由：

- 整文件写入和 patch 编辑是不同风险模型，不能混写。

### 9.5 `apps/backend/app/tools/file_edit/diff_preview.py`

写入能力：

- 生成审批展示用 diff。
- 输出行数限制。
- 大 diff 摘要。

职责：

- 只负责 diff 展示数据，不执行写入。

理由：

- diff 生成会被审批 UI、日志和事件复用，独立后避免重复实现。

## 10. 命令执行工具代码落点

### 10.1 `apps/backend/app/tools/command/definitions.py`

写入能力：

- `execute_command` 的 `ToolDefinition`。

职责：

- 声明 schema：`argv`、`cwd`、`timeout_seconds`、`env_policy`、`stdin_policy`。

理由：

- 命令工具入口应和文件编辑工具一致，统一由 Tool Registry 注册。

### 10.2 `apps/backend/app/tools/command/policy.py`

写入能力：

- 命令风险分类。
- 禁止命令模式。
- shell 使用策略。
- cwd 限制。

职责：

- 判断命令是否可执行、是否需要审批、是否直接拒绝。

理由：

- 命令执行风险高于文件读写，策略必须独立，不应写进通用 `tool_execution/policy.py`。

### 10.3 `apps/backend/app/tools/command/process.py`

写入能力：

- 受控进程启动。
- stdout / stderr 捕获。
- 超时 terminate / kill。
- 用户取消。
- exit code 记录。

职责：

- 只负责进程生命周期。

理由：

- 这是基础设施层能力，不能混进业务 service 或 workflow。

### 10.4 `apps/backend/app/tools/command/output.py`

写入能力：

- stdout / stderr 截断。
- 输出摘要。
- artifact 写入策略。
- secret 脱敏。

职责：

- 只负责命令输出治理。

理由：

- 命令输出会影响上下文预算、日志安全和 UI 展示，必须独立复用。

## 11. API 代码落点

建议新增：

```text
apps/backend/app/api/runs.py
apps/backend/app/api/approvals.py
```

### 11.1 `api/runs.py`

写入能力：

- 查询可恢复 run。
- resume run。
- cancel run。
- 查询 run 状态。

不写入：

- 具体恢复逻辑。

理由：

- API 层只做输入校验、调用 service、格式化输出。

### 11.2 `api/approvals.py`

写入能力：

- 查询 pending approvals。
- approve。
- deny。

不写入：

- 工具执行。
- LangGraph 调用细节。

理由：

- 审批 API 是表现层边界，不应绕过 `approvals/service.py` 和 `runs/resume.py`。

`apps/backend/app/api/app.py` 只负责注册 router，不写审批或恢复逻辑。

## 12. Storage 代码落点

现有 `apps/backend/app/storage/sqlite.py` 已承担 Session / Task / Turn / Step / Event 的 SQLite 持久化。后续有两种路线：

推荐路线：

```text
apps/backend/app/storage/migrations/
  0001_existing_schema.sql
  0002_durable_runs.sql
  0003_approvals.sql
  0004_tool_execution.sql
  0005_human_input.sql
  0006_artifacts.sql
```

新增：

```text
apps/backend/app/storage/migrations.py
```

写入能力：

- schema version 表。
- migration 顺序执行。
- migration 幂等检查。
- artifact 元数据表和 human input 表的迁移。

理由：

- Durable Run State 会新增多张表，不能继续把建表 SQL 无边界塞进 `sqlite.py`。
- migration 是数据库基础设施能力，独立后便于测试和回滚。
- LangGraph checkpointer 的内部表不在这些业务 migration 中手写，统一由 `runs/checkpointer.py` 通过官方 SQLite checkpointer 初始化。

过渡策略：

- `sqlite.py` 保留现有存储接口。
- 新增 store 优先复用同一个 SQLite connection 管理方式。
- LangGraph checkpointer 优先和业务 SQLite 共用同一个数据库文件；如果官方 checkpointer 连接管理不适合共享连接，则使用同目录独立 SQLite 文件，并在 `runs/checkpointer.py` 中明确映射关系。
- 不在第一步强行重构所有现有 storage，避免牵连过大。

## 13. 日志代码落点

### 13.1 后端日志落点

涉及以下模块时，必须复用 `apps/backend/app/logging/configuration.py` 配置出的 logger，不新增散落 `print`：

- `runs/recovery.py`：记录启动对账、状态修正、无法确认副作用的 run。
- `runs/resume.py`：记录 resume command、幂等命中、非法恢复、恢复失败。
- `approvals/service.py`：记录审批请求创建、approve、deny、过期和非法决策。
- `human_input/service.py`：记录人工输入请求创建、响应、过期和非法响应。
- `tool_execution/service.py`：记录工具策略结果、执行开始、执行完成、失败和副作用状态。
- `tools/file_edit/*`：记录路径拒绝、hash mismatch、patch 失败、原子写失败。
- `tools/command/*`：记录命令风险等级、进程启动、退出码、超时、取消、stdout/stderr 截断。
- `artifacts/*`：记录 artifact 写入失败、hash mismatch、清理失败。

日志要求：

- 所有日志必须包含 `run_id` 或 `task_id`，有工具上下文时包含 `tool_call_id`。
- 禁止记录 API key、token、完整环境变量和命令输出全文。
- 命令输出、diff 和大结果必须进入 artifact 或截断摘要，日志只记录 artifact id 和摘要。

### 13.2 前端日志落点

前端必须复用 `apps/desktop/src/lib/logger.ts`，并通过已有 Tauri 日志命令落盘，不在组件里散用 `console.log`。

需要记录：

- `services/approvals.ts`：approve / deny API 成功、失败、耗时。
- `services/runs.ts`：resume / cancel / recoverable runs 查询成功、失败、耗时。
- `hooks/useApprovals.ts`：用户点击 approve / deny、重复点击被抑制、审批刷新失败。
- `hooks/useRunRecovery.ts`：启动恢复查询、恢复状态变化、恢复失败。
- 审批组件：只记录用户动作和错误，不记录完整敏感参数。

理由：

- 可排查日志是开发规范硬要求。审批和恢复属于长链路能力，如果不提前规划日志落点，后续问题只能靠 UI 状态猜。

## 14. Runtime 和 Workflow 改造落点

### 14.1 `apps/backend/app/runtime/runner.py`

调整方向：

- 保留运行入口和任务调度职责。
- 不再直接承担 LangGraph suspend/resume 细节。
- 调用 `runs/langgraph_runtime.py` 执行或恢复 graph。

理由：

- runner 是协调入口，不应成为所有状态机、审批和工具执行细节的容器。

### 14.2 `apps/backend/app/runtime/operations.py`

调整方向：

- 保留模型、事件、存储、工具执行等操作门面。
- 把审批判断、恢复命令、工具持久化下沉到对应 service。

理由：

- 该文件当前已经承担较多职责，后续不能继续把审批和命令执行堆进去。

### 14.3 `apps/backend/app/workflows/react_like.py`

调整方向：

- 逐步迁移为 LangGraph 节点或被 LangGraph 节点调用的策略函数。
- 危险工具调用点不再返回失败，而是触发 interrupt。

理由：

- ReAct-like 是默认 workflow，不是 Runtime 底座。中断恢复能力应该属于 LangGraph runtime 层。

## 15. Event 和共享类型落点

### 15.1 `apps/backend/app/events/types.py`

新增事件类型：

- `run_waiting`
- `run_resuming`
- `approval_requested`
- `approval_decided`
- `human_input_requested`
- `human_input_received`
- `tool_execution_planned`
- `tool_execution_started`
- `tool_execution_finished`
- `tool_execution_cancelled`
- `tool_execution_timed_out`
- `run_needs_review`

理由：

- 事件是 UI 和审计事实流。等待、恢复、审批决策和工具执行必须显式可见。

### 15.2 `packages/shared/ts/events.ts`

同步新增前端事件类型。

理由：

- 共享协议不能在前后端各自重复定义，避免双源真理。

### 15.3 `packages/shared/ts/runs.ts`

写入能力：

- Run 状态枚举。
- Wait reason 类型。
- Resume command 请求/响应类型。

### 15.4 `packages/shared/ts/approvals.ts`

写入能力：

- Approval 请求展示类型。
- Approve / deny API 类型。

### 15.5 `packages/shared/ts/toolExecution.ts`

写入能力：

- Tool call 状态。
- Tool execution 状态。
- 文件编辑和命令执行摘要类型。

## 16. 前端代码落点

### 16.1 `apps/desktop/src/services/approvals.ts`

写入能力：

- `fetchPendingApprovals(taskId)`
- `approveApproval(approvalId, payload)`
- `denyApproval(approvalId, payload)`

职责：

- 只封装 HTTP API。

理由：

- 组件不能直接 fetch；审批 service 独立后避免把 API 拼接写进 `ChatPanel.tsx`。

### 16.2 `apps/desktop/src/services/runs.ts`

写入能力：

- 查询 recoverable runs。
- resume run。
- cancel run。

职责：

- 只封装 run 相关 HTTP API。

### 16.3 `apps/desktop/src/stores/approvalStore.ts`

写入能力：

- pending approvals 状态。
- approve / deny 中间状态。
- 错误状态。

职责：

- 只管理审批领域状态。

不写入：

- HTTP 请求。
- JSX。

理由：

- Zustand store 是状态层，不应直接承担外部通信。

### 16.4 `apps/desktop/src/stores/runStore.ts`

写入能力：

- 当前 run 状态。
- recoverable runs。
- resume / cancelling 状态。

职责：

- 只管理 run 领域状态。

不写入：

- HTTP 请求。
- SSE 解析。
- Tauri / Rust IPC。
- JSX。
- 日志副作用。
- LangGraph 相关概念。

理由：

- Zustand store 是状态层，只保存和派生 run 状态。恢复查询、resume、cancel 和日志应由 hook/service 编排，避免 store 变成业务流程容器。

### 16.5 `apps/desktop/src/hooks/useRunRecovery.ts`

写入能力：

- 启动时加载 recoverable runs。
- 编排 resume / cancel 操作。
- 协调 SSE 事件回放去重。
- 记录恢复查询、恢复动作和失败路径日志。

职责：

- 连接 `services/runs.ts`、`runStore.ts` 和事件流状态。
- 给页面组件提供恢复状态和动作。

不写入：

- JSX。
- 直接 `fetch`。
- Tauri / Rust IPC。
- 持久化状态。
- 审批 approve / deny 业务。

理由：

- run 恢复是前端业务编排能力，不能塞进 `ChatPanel.tsx`、`runStore.ts` 或 `useApprovals.ts`。单独 hook 可以集中处理启动恢复、重复事件去重和日志。

### 16.6 `apps/desktop/src/hooks/useApprovals.ts`

写入能力：

- 审批列表加载编排。
- approve / deny 调用编排。
- 日志记录。

职责：

- 连接 service 和 store，提供给组件使用。

理由：

- 组件只调用 hook，不处理业务流程。

### 16.7 `apps/desktop/src/components/approvals/ApprovalCard.tsx`

写入能力：

- 审批卡片摘要展示。

职责：

- 只渲染一条审批请求。

### 16.8 `apps/desktop/src/components/approvals/ApprovalDetail.tsx`

写入能力：

- 展示工具参数、风险、diff、命令信息。

职责：

- 只渲染审批详情。

### 16.9 `apps/desktop/src/components/approvals/ApprovalActions.tsx`

写入能力：

- approve / deny 按钮。
- loading / disabled / error 状态。

职责：

- 只渲染审批动作。

理由：

- 将展示、详情、动作拆开，避免一个审批组件同时承担所有 UI 和业务状态。

### 16.10 `apps/desktop/src/components/chat/ToolCallCard.tsx`

调整方向：

- 保留工具调用状态只读展示。
- 移除“审批按钮后续实现”的业务占位。
- 审批动作迁移到 `components/approvals/`。

理由：

- 工具调用生命周期展示和审批决策是不同职责。

### 16.11 `apps/desktop/src/components/layout/ChatPanel.tsx`

调整方向：

- 不继续在 `ChatPanel` 内按事件临时拼审批 UI。
- 只组合消息流、工具状态组件和审批组件入口。

理由：

- `ChatPanel` 当前已经开始承担事件过滤、工具卡片、审批渲染等多个职责，后续必须收窄。

## 17. 测试代码落点

后端新增：

```text
apps/backend/tests/test_run_state_machine.py
apps/backend/tests/test_run_resume.py
apps/backend/tests/test_approvals.py
apps/backend/tests/test_tool_execution.py
apps/backend/tests/test_file_edit_tools.py
apps/backend/tests/test_command_tools.py
apps/backend/tests/test_recovery.py
apps/backend/tests/test_artifacts.py
apps/backend/tests/test_langgraph_checkpointer.py
```

前端新增：

```text
apps/desktop/src/tests/approvals.test.ts
apps/desktop/src/tests/runs.test.ts
apps/desktop/src/tests/runStore.test.ts
apps/desktop/src/tests/approvalStore.test.ts
apps/desktop/src/tests/approval-components.test.tsx
apps/desktop/src/tests/useRunRecovery.test.ts
apps/desktop/src/tests/approval-logging.test.ts
```

测试重点：

- run 状态非法流转被拒绝。
- approval pending 后重启仍可查询。
- approve 后通过同一 thread_id resume。
- deny 后形成 observation，不直接静默失败。
- 重复 approve 不重复执行工具。
- 文件 hash mismatch 不覆盖用户修改。
- 命令超时和取消不会留下 running step。
- 前端审批 API 失败会写日志。
- `services/runs.ts` 能处理恢复查询失败、resume 失败和 cancel 失败。
- `useRunRecovery` 能在启动时加载可恢复 run，并对失败路径写日志。
- SSE 事件回放不会重复创建审批卡片或重复触发 approve / deny。
- 前端日志落盘失败时有回退路径，不会吞掉审批错误。
- artifact 大输出只保存摘要和 artifact id，不把全文写入事件或日志。

## 18. 实施顺序

推荐分阶段推进：

1. 建立 `runs/` 目录、run 状态模型、状态机和 store。
2. 建立 `runs/checkpointer.py`，接入官方 SQLite checkpointer、`thread_id`、checkpoint namespace。
3. 拆出 `graph_builder.py`、`invoke.py` 和窄 facade `langgraph_runtime.py`。
4. 建立 approvals 后端模型、API 和前端审批组件。
5. 将危险工具请求从失败态改为 interrupt + waiting。
6. 建立 tool execution 持久化、policy provider 和幂等记录。
7. 建立 artifacts 模块，统一承接大输出、diff 和工具结果摘要。
8. 实现文件编辑工具族。
9. 实现命令执行工具族。
10. 做 recovery manager 启动对账。
11. 补齐前端 run 恢复状态面和日志落盘路径。
12. 完成审查和测试闭环。

不建议顺序：

- 不先写 `write_file`。
- 不先写 `execute_command`。
- 不先把审批按钮硬塞进 `ToolCallCard.tsx`。
- 不先把 LangGraph 调用散落到现有 workflow 文件里。
- 不手写 LangGraph checkpoint 表。
- 不把大输出临时塞进事件 payload 或日志。

原因：

- 没有 durable run state，危险工具无法生产级恢复。
- 没有审批恢复，文件编辑和命令执行只能做成 demo。
- 没有工具执行持久化，恢复后无法判断副作用是否重复。

## 19. 代码审查检查项

实现时每个 PR 或开发任务必须检查：

- 是否新增了职责明确的文件，而不是继续膨胀 `runner.py`、`operations.py`、`ChatPanel.tsx`。
- 是否复用了 LangGraph interrupt/checkpointer，而不是自研等待恢复机制。
- 是否所有危险副作用都在审批 resume 之后执行。
- 是否每个恢复命令都有幂等键。
- 是否所有状态变化都有 event 和日志。
- 是否共享类型写入 `packages/shared/ts`，没有前后端重复定义。
- 是否前端组件不直连 fetch / IPC。
- 是否新增函数有完整 docstring 或 TSDoc。
- 是否有正常、拒绝、重启恢复、重复提交、超时、取消、日志脱敏测试。

## 20. 最终目标

完成本方案后，项目应具备一个清晰的工程骨架：

```text
LangGraph 负责成熟的 durable interrupt/resume 机制
Run 模块负责项目内可恢复执行状态
Approval / HumanInput 模块负责用户参与
ToolExecution 模块负责工具副作用审计和幂等
具体 tools 只负责具体能力
前端只通过 service / hook / store / component 分层展示和操作
```

这样后续 checkpoint、审批、文件编辑、命令执行、human-in-loop、subagent、MCP 都能挂在同一条运行时主线上，而不是各自实现一套恢复逻辑。

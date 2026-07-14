# Tool Platform 技术方案

本文规划 coding agent 工具体系的代码落点、职责边界和演进顺序。目标不是写详细代码，而是在实现前明确“什么能力写在哪个文件”，避免把工具注册、工具检索、审批、文件编辑、命令执行、Git、测试执行和日志审计堆进同一个 Runtime 或 Scheduler。

本文遵守：

- `rules/Agent代码开发规范.md`
- `rules/Agent客户端代码开发规范.md`
- `docs/durable-run-state-technical-plan.md`
- `docs/production-acceptance.md`

## 1. 核心决议

第一版采用生产级 Tool Platform 架构，但具体工具族分批落地。

本项目不把全部工具一次性写入 Agent 提示词。成熟机制应是：

- Tool Registry 保存工具全集。
- Tool Catalog 保存工具元数据、标签、权限、风险和可见性。
- Tool Selector 按任务阶段、Run 状态、Workflow、权限和上下文选择本轮候选工具。
- Tool Search 在工具数量变多时从 Catalog 中检索候选工具。
- Model Call 只注入本轮候选工具 schema。
- Tool Runtime 在模型之外强制执行参数校验、权限、审批、幂等、日志、artifact 和错误归一化。

关键约束：

- 安全策略不能依赖 prompt 约束，必须由 Runtime 强制。
- 危险副作用必须在 LangGraph interrupt / approval resume 之后执行。
- 工具调用必须有幂等键，重试不能重复写文件、重复执行命令或重复创建 artifact。
- 大输出必须 artifact 化，返回给模型的 observation 只保留高信号摘要。
- MCP 本轮不做，但 Tool Platform 必须预留外部工具 provider 接口，避免以后接 MCP 时重写内置工具体系。
- 核心执行链采用 Tool v2 重构；现有 `ToolScheduler` 不继续扩展为完整平台，只保留为兼容门面。
- 第一版 Tool v2 必须预留并发工具调用能力，避免后续为并发重写执行模型。

## 2. 能力边界

Tool Platform 是模型调用工具的工程底座，不是某一个具体工具。

它负责：

- 工具定义注册。
- 工具目录和检索。
- 工具候选选择。
- 工具参数 schema 校验。
- 工具权限和风险决策。
- 审批 interrupt / resume 编排。
- 工具执行持久化。
- 工具执行隔离、超时、取消和输出归一化。
- 通过 `ToolCallExecutor` 统一调用 artifacts service 落盘。
- 工具日志、审计和调试信息。

它不负责：

- 模型如何思考。
- 具体 Workflow 的规划策略。
- MCP 协议接入细节。
- UI 组件直接执行工具。
- Git remote、PR、CI 云端集成的完整产品能力。

## 3. 现有代码复用原则

当前已有能力必须复用或演进，不新建平行系统：

- `apps/backend/app/tools/registry.py` 已有 `ToolRegistry`，继续作为 handler 注册入口。
- `apps/backend/app/tools/scheduler.py` 已有第一版调度器，后续收敛为调用 `ToolRuntime` 的兼容门面或逐步下沉职责。
- `apps/backend/app/tools/schema.py` 已有参数校验，继续保留；完整 JSON Schema 校验继续优先用 `jsonschema`。
- `apps/backend/app/tools/execution.py` 已有进程隔离执行，作为 Python handler 工具的基础执行器，但命令执行需要独立 PTY/process 层。
- `apps/backend/app/tools/safe_read.py` 已有只读文件工具，后续迁入 `tools/file_read/` 或保留为兼容入口，不能复制第二套 read_file。
- `apps/backend/app/tools/execution/` 已有持久化和策略基础，继续扩展，不把记录表迁回 `tools/`。
- `apps/backend/app/domain/approvals/`、`runs/`、`artifacts/` 已经提供审批、恢复和产物基础，危险工具必须接这些能力。

## 4. 重写与迁移决议

本项目采用 **核心执行链重写 + 小件复用 + 兼容门面迁移**。

应该重写：

- `ToolScheduler` 承担的执行编排链路。
- 工具选择、工具检索和模型可见工具注入。
- 工具调用状态机、幂等、审批恢复、artifact 和日志审计编排。
- 并发工具调用调度。

应该保留或演进：

- `ToolDefinition`、`ToolCall`、`ToolObservation` 的概念，但字段可按 Tool v2 扩展。
- `ToolRegistry` 的注册思想，但不承担选择和执行。
- `tools/schema.py` 的 JSON Schema 校验能力。
- `tools/execution.py` 的 Python handler 子进程隔离思路。
- `safe_read.py` 中的路径安全和只读工具逻辑，迁移到 `tools/file_read/` 后保留兼容导出。
- `tool_execution/` 的持久化基础，扩展为 Tool v2 的事实源。

迁移方式：

1. 新建 Tool v2 的 `ToolRuntime`、`ToolCallExecutor`、`ToolSelector`、`ToolConcurrentScheduler`、`ToolExecutionLifecycle`。
2. `ToolScheduler` 改为兼容门面，内部调用 Tool v2，不再新增业务能力。
3. 现有 Workflow 和测试短期继续依赖 `ToolScheduler`，降低一次性迁移风险。
4. 文件编辑、命令执行、Git、验证等新工具只接入 Tool v2。
5. 当所有调用点迁移完成后，再删除或冻结旧 Scheduler 行为。

理由：

- 现有 `ToolScheduler` 混合了工具查找、权限判断、参数校验、审批判断、handler 执行、错误归一化和日志。继续扩展会形成工具系统的上帝类。
- 并发工具调用需要独立的调度、资源锁、取消、状态记录和结果对齐能力，旧同步 Scheduler 不适合作为核心。
- Strangler Fig 迁移能避免大爆炸重写，同时防止新工具继续绑定旧抽象。

## 5. 推荐目录结构

后端新增或演进目录：

```text
apps/backend/app/tools/
  registry.py
  scheduler.py
  schema.py
  types.py
  runtime.py
  executor.py
  selector.py
  concurrent.py
  locks.py
  results.py
  errors.py

apps/backend/app/tools/catalog/
  __init__.py
  records.py
  store.py
  index.py
  search.py
  tags.py

apps/backend/app/tools/file_read/
  __init__.py
  definitions.py
  paths.py
  read_file.py
  list_directory.py
  search_text.py

apps/backend/app/tools/file_edit/
  __init__.py
  definitions.py
  paths.py
  planning.py
  diff_preview.py
  apply_patch.py
  write_file.py
  snapshot.py
  rollback.py
  policy.py

apps/backend/app/tools/command/
  __init__.py
  definitions.py
  policy.py
  process.py
  pty.py
  output.py
  cancellation.py

apps/backend/app/tools/git/
  __init__.py
  definitions.py
  status.py
  diff.py
  stage.py
  commit.py
  branch.py
  policy.py

apps/backend/app/tools/verification/
  __init__.py
  definitions.py
  test_runner.py
  lint_runner.py
  typecheck_runner.py
  build_runner.py
  result_parser.py

apps/backend/app/tools/execution/
  records.py
  store.py
  policy.py
  policy_provider.py
  service.py
  idempotency.py
  lifecycle.py
  concurrency.py

apps/backend/app/domain/artifacts/
  service.py
  store.py
  files.py
```

前端新增或演进目录：

```text
apps/desktop/src/components/chat/
  ToolCallCard.tsx

apps/desktop/src/components/tools/
  ToolResultPanel.tsx
  ToolArtifactLink.tsx

apps/desktop/src/components/approvals/
  ApprovalCard.tsx
  ApprovalDetail.tsx
  ApprovalActions.tsx

apps/desktop/src/services/
  tools.ts
  approvals.ts
  runs.ts

apps/desktop/src/stores/
  toolStore.ts
  approvalStore.ts
  runStore.ts

apps/desktop/src/hooks/
  useTools.ts
  useApprovals.ts
  useRunRecovery.ts
```

共享类型新增或演进：

```text
packages/shared/ts/tools.ts
packages/shared/ts/toolCatalog.ts
packages/shared/ts/toolExecution.ts
packages/shared/ts/approvals.ts
packages/shared/ts/artifacts.ts
```

理由：

- `tools/` 放工具平台和具体工具族，不直接存运行记录。
- `tool_execution/` 放工具调用事实、策略结果和执行生命周期。
- `approvals/` 只处理人类审批请求和决策。
- `artifacts/` 只处理大输出、diff、命令日志、测试报告等产物的元数据、文件存储和查询；创建入口由 `ToolCallExecutor` 统一调用。
- `components/chat/ToolCallCard.tsx` 已存在，继续作为聊天流中的工具调用摘要组件；`components/tools/` 只放右侧详情面板或复用型工具详情组件，不能复制第二个 ToolCallCard。
- `components/approvals/` 只承载审批操作面板和审批详情；聊天流中的工具摘要仍归 `components/chat/ToolCallCard.tsx`。
- 前端 store 只保存 UI 状态，service 只做 HTTP/IPC，组件只渲染。

## 6. 现有文件迁移表

本方案不是另起一套工具系统。已有文件按下表处理：

| 现有文件 | 处理方式 | 目标位置或职责 | 理由 |
| --- | --- | --- | --- |
| `apps/backend/app/tools/registry.py` | 保留并扩展 | 继续作为 handler 注册入口 | 避免重复实现 Registry |
| `apps/backend/app/tools/scheduler.py` | 重写为兼容门面 | 第一阶段保留旧接口，内部转调 `ToolRuntime`；不再新增业务能力 | 避免旧 Scheduler 继续膨胀，同时保护现有 workflow |
| `apps/backend/app/tools/schema.py` | 保留 | 继续负责参数 schema 校验 | 已有 jsonschema fallback，直接复用 |
| `apps/backend/app/tools/execution.py` | 保留 | Python handler 隔离执行器 | 命令执行另建 `tools/command/`，不混用 |
| `apps/backend/app/tools/safe_read.py` | 迁移或兼容保留 | 迁到 `tools/file_read/` 后保留导入兼容层 | 避免 read/list/search 双实现 |
| `apps/backend/app/tools/execution/service.py` | 扩展 | 工具调用事实和生命周期服务 | 不能把记录能力搬到 `tools/runtime.py` |
| `apps/desktop/src/components/chat/ToolCallCard.tsx` | 扩展 | 聊天流工具调用摘要 | 不新增 `components/tools/ToolCallCard.tsx` 平行组件 |
| `apps/desktop/src/hooks/useApprovals.ts` | 扩展 | 继续编排审批 service 和 store | 审批 hook 已存在，不能重复 |
| `apps/desktop/src/services/approvals.ts` | 扩展 | 继续承载审批 HTTP 通信 | 审批 service 已存在，不能重复 |
| `apps/desktop/src/stores/approvalStore.ts` | 扩展 | 继续保存审批 UI 状态 | 审批状态源保持单一 |

## 7. 后端平台层代码落点

### 7.1 `apps/backend/app/tools/types.py`

写入能力：

- `ToolDefinition`
- `ToolCall`
- `ToolObservation`
- `ToolVisibility`
- `ToolRiskLevel`

职责：

- 定义模型可见工具、Runtime 内部工具调用和工具结果的基础值对象。
- 保持跨 registry、selector、runtime、tests 可复用。

不写入：

- SQL。
- 具体工具 handler。
- 审批逻辑。

理由：

- 类型是工具系统公共契约，放在 `types.py` 比放在某个工具族里更清晰。

### 7.2 `apps/backend/app/tools/registry.py`

写入能力：

- `ToolRegistry`
- `register()`
- `get()`
- `list_tools()`
- `list_by_namespace()`

职责：

- 保存工具 handler 和执行所需 definition。
- 防止重复注册。
- 作为 Runtime 查找 handler 的唯一入口。

不写入：

- 工具检索排序。
- 权限策略。
- 工具执行。

理由：

- Registry 是内存索引，不应承担选择、审批或执行职责。

### 7.3 `apps/backend/app/tools/catalog/records.py`

写入能力：

- `ToolCatalogRecord`
- `ToolNamespace`
- `ToolTag`
- `ToolSearchDocument`

职责：

- 定义工具目录元数据：名称、namespace、短描述、长描述、tags、risk、permission、是否默认可见、是否需要审批、schema 摘要。

不写入：

- handler。
- SQLite 查询。
- 检索算法。

理由：

- Catalog 是“工具可被发现的信息”，Registry 是“工具可被执行的信息”，两者分开才能支持动态注入和后续外部工具。

### 7.4 `apps/backend/app/tools/catalog/store.py`

写入能力：

- `ToolCatalogStore`
- catalog 持久化读写。
- catalog 版本查询。

职责：

- 把工具目录元数据保存到 SQLite。
- 支持工具变更后重建索引。

不写入：

- FTS 查询。
- handler 注册。
- 模型可见工具选择。

理由：

- 数据层只做增删改查和映射，不混入业务策略。

### 7.5 `apps/backend/app/tools/catalog/index.py`

写入能力：

- `ToolCatalogIndexer`
- SQLite FTS5 index 初始化。
- catalog 文档入库和重建索引。

职责：

- 使用 SQLite FTS5 建立工具检索索引。
- 索引字段包括 name、namespace、description、tags、examples。

不写入：

- ranking 规则。
- Tool Selector。

理由：

- FTS5 是 SQLite 内置成熟能力，适合第一阶段；不需要一开始引入向量库。

### 7.6 `apps/backend/app/tools/catalog/search.py`

写入能力：

- `ToolSearchService`
- keyword / FTS 查询。
- 搜索结果排序和截断。

职责：

- 从工具目录中检索候选工具。
- 返回可解释的命中原因。

不写入：

- 工具执行。
- 权限审批。

理由：

- Search 只解决“可能相关”，Selector 再结合状态和权限决定“本轮可见”。

### 7.7 `apps/backend/app/tools/selector.py`

写入能力：

- `ToolSelector`
- `ToolSelectionContext`
- `SelectedToolSet`

职责：

- 根据 task 类型、run status、workflow phase、agent profile、allowed permissions、recent failures、用户意图和搜索结果选择本轮工具。
- 控制每轮注入工具数量，例如默认 3-8 个。

不写入：

- 工具检索索引。
- handler 执行。
- 审批请求持久化。

理由：

- Selector 是防止 prompt 被工具全集污染的核心边界，必须独立于 Registry 和 Runtime。

### 7.8 `apps/backend/app/tools/runtime.py`

写入能力：

- `ToolRuntime`
- `prepare_tool_call()`
- `execute_single_tool_call()`
- `resume_approved_tool_call()`
- `execute_tool_calls()`

职责：

- 作为工具系统唯一对外执行入口，编排单个或多个工具调用的完整生命周期：
  - 解析 tool call。
  - 查 registry。
  - 校验 schema。
  - 生成幂等键。
  - 写入 `tool_execution` 记录。
  - 调用 policy。
  - 需要审批时创建 approval interrupt。
  - 获批后调用 `ToolCallExecutor` 执行单个工具。
  - 多工具调用时生成 `ToolConcurrencyPlan`，再调用 `ToolConcurrentScheduler.run(plan, single_call_executor)`。
  - 持久化 `ToolCallExecutor` 返回的 artifact 关联关系。
  - 返回 `ToolObservation`。
- 固定单向调用链：
  - `runner -> ToolRuntime.execute_tool_calls() -> ToolConcurrencyPlanner -> ToolConcurrentScheduler.run(plan, single_call_executor) -> ToolCallExecutor.execute()`。
- `execute_single_tool_call()` 只做单个工具调用的生命周期包装、状态推进和审批恢复，不直接调用具体 handler；真正 handler 执行只能发生在 `ToolCallExecutor.execute()`。

不写入：

- 具体文件编辑逻辑。
- 具体命令执行逻辑。
- 并发 fan-out / fan-in 细节。
- 资源锁实现。
- UI 格式化。

理由：

- Runtime 是业务编排层；如果把这些逻辑放进 Scheduler，Scheduler 会继续膨胀成工具垃圾桶。
- Runtime 持有生命周期、审批、幂等和 artifact 关联编排，但不直接承担 artifact 落盘、并发调度和具体 handler 执行，避免 Runtime 与 Scheduler 循环依赖。

### 7.9 `apps/backend/app/tools/executor.py`

写入能力：

- `ToolCallExecutor`
- 单个 tool call 的 handler 调用。
- 单个 tool call 的异常捕获和执行耗时记录。

职责：

- 接收已经完成 registry、schema、policy、approval 和幂等准备的 `PreparedToolCall`。
- 调用具体工具 handler。
- 将 handler 原始结果中的 `ArtifactRequest` 交给 `artifacts` service 统一落盘。
- 将落盘后的 artifact id 交给 `ToolObservationBuilder` 生成 observation 摘要。
- 返回单个 `ToolRuntimeResult`。

不写入：

- 多工具并发计划。
- 资源锁申请和释放。
- 审批决策。
- 工具选择。

理由：

- 单工具执行是最容易被 Runtime、Scheduler 和具体工具族重复实现的部分，独立成执行单元可以让并发调度器只依赖一个窄接口。
- `ToolConcurrentScheduler` 只接收 `single_call_executor` 回调，不反向依赖完整 `ToolRuntime`，从结构上消除循环依赖。

### 7.10 `apps/backend/app/tools/concurrent.py`

写入能力：

- `ToolConcurrentScheduler`
- 并发工具调用 fan-out / fan-in。
- 最大并发数控制。
- per-call 超时和取消传播。
- tool_call_id 到 observation 的结果对齐。

职责：

- 执行同一模型响应中的多个工具调用。
- 消费 `ToolConcurrencyPlan`，按 group 顺序运行，group 内按规则并发。
- 通过 `ToolResourceLockManager` 申请和释放资源锁。
- 通过传入的 `single_call_executor` 执行单个工具调用。
- 记录 group 级开始、结束、失败、取消和冲突原因。

不写入：

- Registry。
- 工具策略细节。
- 具体工具 handler。
- Tool Runtime 生命周期编排。

理由：

- 并发是调度问题，不应放进 Registry、Selector 或具体工具族。
- 单独文件可以测试并发数限制、失败隔离、结果顺序和取消行为。
- Scheduler 不能回调完整 Runtime，否则会形成 `Runtime -> Scheduler -> Runtime` 的循环依赖；这里只允许依赖窄接口 `single_call_executor`。

### 7.11 `apps/backend/app/tools/locks.py`

写入能力：

- `ToolResourceLock`
- `ToolResourceLockManager`
- 文件路径锁。
- Git 工作区锁。
- PTY session 锁。
- 资源锁 key 规范化。

职责：

- 防止并发工具调用互相踩踏同一资源。
- 支持文件编辑、Git 写操作和命令执行的串行化约束。
- 根据 tool call 的 declared resources 生成锁 key。
- 为 `ToolConcurrentScheduler` 提供锁申请和释放接口。
- 在锁冲突时返回结构化冲突原因，供 `ToolConcurrencyPlan`、事件日志和调试页面使用。

不写入：

- 工具执行。
- 文件修改。
- Git 命令。
- 并发 group 执行。

理由：

- 并发工具调用必须有资源锁，否则两个写文件工具或 Git 工具可能破坏工作区。

### 7.12 `apps/backend/app/tools/results.py`

写入能力：

- `ToolRuntimeResult`
- `ToolObservationBuilder`
- observation 摘要裁剪。

职责：

- 把内部执行结果转换成模型可读、高信号、低 token 的 observation。
- 大输出只返回 artifact id、摘要和下一步建议。

不写入：

- 工具执行。
- artifact 文件写入。

理由：

- 工具输出治理是上下文工程的一部分，独立后能统一控制大输出和敏感信息。

### 7.13 `apps/backend/app/tools/errors.py`

写入能力：

- `ToolNotFoundError`
- `ToolSchemaError`
- `ToolPolicyDeniedError`
- `ToolApprovalRequired`
- `ToolExecutionFailed`

职责：

- 定义工具平台内可分类错误。
- 支持 Runtime 统一转 observation、日志和 API 错误。

不写入：

- 错误处理流程。
- 日志输出。

理由：

- 错误类型集中，避免散落字符串判断。

### 7.14 `apps/backend/app/domain/artifacts/service.py`

写入能力：

- `ArtifactService`
- `create_from_request()`
- artifact 元数据与文件存储的统一编排。
- artifact 摘要查询。

职责：

- 接收 `ToolCallExecutor` 传入的 `ArtifactRequest` 或 `ArtifactCandidate`。
- 调用 artifact store 和 file store 创建记录、写入文件并返回 artifact id。
- 提供 `api/artifacts.py` 所需的只读查询能力。

不写入：

- 工具 handler 执行。
- observation 生成。
- 审批和幂等决策。

理由：

- artifacts service 是产物落盘的业务服务，避免 `ToolCallExecutor` 直接拼接 store 和文件系统细节。
- 创建入口仍然只允许从 `ToolCallExecutor` 进入，API 路由和具体工具族不能绕过工具执行链创建 artifact。

## 8. `tool_execution/` 代码落点

### 8.1 `apps/backend/app/tools/execution/idempotency.py`

写入能力：

- `ToolIdempotencyKeyBuilder`
- 参数规范化。
- tool call key / execution key / artifact key 构造。

职责：

- 根据 run_id、step_id、tool_name、arguments_hash、approval_id 构造稳定幂等键。
- 使用 JSON canonicalization 和 SHA-256。

不写入：

- 具体工具执行。
- 数据库写入。

理由：

- 幂等键是危险副作用安全的基础，不能在各工具族里手写。

### 8.2 `apps/backend/app/tools/execution/lifecycle.py`

写入能力：

- `ToolExecutionLifecycle`
- pending / waiting_approval / approved / running / completed / failed / cancelled 状态推进。
- `ToolPolicyDecision.status` 到执行生命周期状态的映射。

职责：

- 校验工具调用状态流转。
- 防止已完成工具重复执行。
- 明确区分策略状态和执行状态：
  - `allow` 映射为 `approved`，随后进入 `running`。
  - `approval_required` 映射为 `waiting_approval`。
  - `deny` 在未执行副作用时映射为 `cancelled`；只有已经开始执行且失败时才映射为 `failed`。
  - 兼容旧记录中的 `allow / approval_required / deny`，读取时归一化为 lifecycle status。

不写入：

- SQL。
- 工具 handler。

理由：

- 状态机独立，避免 store 层承担业务规则。
- 现有 `ToolExecutionService.plan_tool_call()` 会直接写入 policy status，后续必须通过 lifecycle 映射迁移，避免持久化状态混用。

### 8.3 `apps/backend/app/tools/execution/service.py`

写入能力：

- 扩展现有 `ToolExecutionService`。
- `create_call()`
- `mark_waiting_approval()`
- `mark_running()`
- `mark_completed()`
- `mark_failed()`
- `list_running_for_run()`

职责：

- 封装工具执行记录读写和状态推进。
- 调用 `ToolExecutionStore` 与 `ToolExecutionLifecycle`。
- 保留 `plan_tool_call()` 兼容入口，但其内部必须调用 lifecycle 映射，不再直接把 `ToolPolicyDecision.status` 写入 tool call status。
- 为并发工具调用提供按 run_id / step_id 查询运行中调用的能力。

不写入：

- 具体工具逻辑。
- Selector。
- UI 响应格式。

理由：

- Service 是业务层，承接 Runtime 与 Store 之间的工具执行事实管理。

### 8.4 `apps/backend/app/tools/execution/concurrency.py`

写入能力：

- `ToolConcurrencyPlanner`
- `ToolConcurrencyPlan`
- `ToolConcurrencyGroup`
- 工具调用串并行分组。
- 并发冲突原因记录。

职责：

- 根据工具资源锁、风险等级和工具族规则，把多个 tool call 分成可并发组和必须串行组。
- 调用 `ToolResourceLockManager` 的 lock key 归一化能力，但不持有运行期锁。
- 记录每个 group 的串行或并行原因，例如同路径写冲突、Git 工作区写锁、命令互斥。
- 给 `tools/concurrent.py` 提供执行计划。

不写入：

- 实际执行。
- 运行期锁申请和释放。
- 具体工具 handler。

理由：

- 并发计划属于 tool_execution 的业务事实，实际调度属于 `tools/concurrent.py`。
- 独立后可测试“两个读文件并发、读写同一文件串行、Git 写操作全局串行”等规则。

## 9. 文件读取工具族

### 9.1 `apps/backend/app/tools/file_read/paths.py`

写入能力：

- `resolve_inside_project()`
- 忽略目录规则。
- 文本文件判断。

职责：

- 统一项目根目录路径约束。
- 防止路径逃逸。

不写入：

- read / search / list 的业务实现。

理由：

- 文件读写和搜索都需要同一套路径安全规则，避免重复造轮子。

### 9.2 `apps/backend/app/tools/file_read/read_file.py`

写入能力：

- `read_file()`
- 最大读取字节限制。
- 文本编码处理。

职责：

- 安全读取单个文本文件。

不写入：

- 搜索。
- diff。
- 写文件。

理由：

- 单一职责，替代或迁移现有 `safe_read.py` 中的同名能力。

### 9.3 `apps/backend/app/tools/file_read/search_text.py`

写入能力：

- `search_text()`
- 优先调用 `rg`。
- `rg` 不可用时 fallback 到 Python 搜索。

职责：

- 负责文本搜索。
- 控制最大匹配数量和忽略目录。

不写入：

- read_file。
- grep 结果 UI 格式化。

理由：

- 搜索是高频工具，应优先复用成熟 `rg`，不默认手写全仓扫描。

### 9.4 `apps/backend/app/tools/file_read/definitions.py`

写入能力：

- 文件读取工具 definitions。

职责：

- 把 read/list/search handler 注册为工具定义。

不写入：

- handler 实现。
- 工具执行。

理由：

- definitions 只负责把工具族暴露给 Registry，避免 handler 文件依赖 Registry。

## 10. 文件编辑工具族

### 10.1 `apps/backend/app/tools/file_edit/planning.py`

写入能力：

- `FileEditPlan`
- 目标文件列表。
- 预期 diff 摘要。
- 风险标记。

职责：

- 在实际写入前形成编辑计划。
- 提供审批和 UI 展示所需摘要。

不写入：

- 真正写文件。
- apply patch。

理由：

- 生产级文件编辑不能直接写，必须先计划和预览。

### 10.2 `apps/backend/app/tools/file_edit/diff_preview.py`

写入能力：

- unified diff 生成。
- diff 大小限制。
- diff `ArtifactRequest` 生成。

职责：

- 生成编辑前后的差异预览。

不写入：

- 文件落盘。
- 审批逻辑。

理由：

- diff 是审批、审查和回滚的共同输入，应独立。

### 10.3 `apps/backend/app/tools/file_edit/snapshot.py`

写入能力：

- 编辑前快照。
- 快照 `ArtifactRequest`。
- 文件 hash。

职责：

- 在修改前保存可恢复状态。

不写入：

- rollback 执行。
- diff 展示。

理由：

- 回滚能力依赖快照，快照不应散落在 write/apply patch 代码里。

### 10.4 `apps/backend/app/tools/file_edit/apply_patch.py`

写入能力：

- 应用统一 patch。
- patch 语法校验。
- patch 前后 hash 校验。

职责：

- 执行结构化 patch 写入。

不写入：

- 自由文本覆盖写文件。
- Git 操作。

理由：

- patch 是 Agent 编辑主路径，必须和 write_file 分开，避免一个文件承担两类写入语义。

### 10.5 `apps/backend/app/tools/file_edit/write_file.py`

写入能力：

- 创建新文件。
- 覆盖写文件。
- 可选 append。

职责：

- 处理非 patch 类文件写入。

不写入：

- patch。
- snapshot。
- rollback。

理由：

- 覆盖写文件风险更高，策略和审批应与 apply_patch 区分。

### 10.6 `apps/backend/app/tools/file_edit/rollback.py`

写入能力：

- 基于 snapshot 回滚文件。

职责：

- 恢复本次工具执行造成的文件修改。

不写入：

- Git restore。
- 通用撤销历史。

理由：

- 工具级 rollback 只处理工具自己产生的副作用，不能偷偷替代 Git。

### 10.7 `apps/backend/app/tools/file_edit/policy.py`

写入能力：

- 文件编辑策略 provider。
- 路径风险判断。
- 修改类型风险判断。

职责：

- 判断文件编辑是否自动允许、需要审批或禁止。
- 高风险路径如规则文件、配置文件、锁文件、`.env` 必须升级审批。

不写入：

- 具体写文件。
- 审批表 SQL。

理由：

- 策略独立，便于被 `ToolExecutionPolicy` 聚合。

## 11. 命令执行工具族

### 11.1 `apps/backend/app/tools/command/process.py`

写入能力：

- non-PTY 命令执行。
- timeout。
- cwd 限制。
- env 白名单。

职责：

- 运行短命令和测试命令。

不写入：

- PTY 交互。
- 输出 `ArtifactRequest`。
- 策略判断。

理由：

- 普通命令和交互式命令生命周期不同，必须分开。

### 11.2 `apps/backend/app/tools/command/pty.py`

写入能力：

- PTY session。
- stdin 写入。
- session poll。
- session terminate。

职责：

- 支持 dev server、交互式程序和长运行命令。

不写入：

- 普通一次性命令。
- UI 终端渲染。

理由：

- PTY 是有状态资源，不能塞进普通 process runner。

### 11.3 `apps/backend/app/tools/command/output.py`

写入能力：

- stdout / stderr 分片记录。
- 大输出截断。
- 输出 `ArtifactRequest` 生成。
- 高信号摘要。

职责：

- 统一命令输出处理。

不写入：

- 命令启动。
- 策略判断。

理由：

- 输出治理直接影响上下文大小和可排查性，应独立。

### 11.4 `apps/backend/app/tools/command/cancellation.py`

写入能力：

- 命令取消。
- 进程组终止。
- timeout 后 kill。

职责：

- 停止命令执行并记录原因。

不写入：

- 命令启动。
- 输出解析。

理由：

- 取消语义复杂，尤其涉及长运行命令和子进程组，不能散落在 process/pty 中。

### 11.5 `apps/backend/app/tools/command/policy.py`

写入能力：

- 命令风险分类。
- allowlist / denylist。
- destructive command 检测。

职责：

- 判断命令是否自动允许、需要审批或禁止。
- `rm -rf`、`git reset --hard`、修改全局配置、网络安装等高风险命令必须审批或禁止。

不写入：

- 命令执行。
- shell 解析实现。

理由：

- 安全策略应可测试、可审查、可独立演进。

### 11.6 `apps/backend/app/tools/command/definitions.py`

写入能力：

- `run_command`
- `start_pty_command`
- `write_pty_stdin`
- `cancel_command`

职责：

- 暴露命令工具定义。

不写入：

- 具体执行逻辑。

理由：

- definitions 只做工具定义组装。

## 12. Git 工具族

### 12.1 `apps/backend/app/tools/git/status.py`

写入能力：

- `git status --short`
- 当前分支查询。

职责：

- 只读 Git 状态。

不写入：

- diff。
- stage。
- commit。

理由：

- 高频只读工具应保持低风险、低权限。

### 12.2 `apps/backend/app/tools/git/diff.py`

写入能力：

- `git diff`
- staged diff。
- diff `ArtifactRequest`。

职责：

- 生成可审查 diff。

不写入：

- stage / commit。

理由：

- diff 是审查和审批输入，不应混入写操作。

### 12.3 `apps/backend/app/tools/git/stage.py`

写入能力：

- stage 指定文件。
- unstage 指定文件。

职责：

- 管理 index。

不写入：

- commit。
- reset hard。

理由：

- stage 是写操作，需要与 commit 分开审批和审计。

### 12.4 `apps/backend/app/tools/git/commit.py`

写入能力：

- 创建 commit。
- commit message 校验。

职责：

- 在用户明确要求后提交已 staged 修改。

不写入：

- stage。
- push。

理由：

- commit 是高影响操作，应单独策略和审批。

### 12.5 `apps/backend/app/tools/git/policy.py`

写入能力：

- Git 操作风险判断。
- 禁止或审批 destructive 操作。

职责：

- 保护用户改动，默认禁止 reset hard、checkout 丢弃修改、clean -fd。

不写入：

- Git 命令执行。

理由：

- Git 保护规则是项目铁律，必须独立可测。

## 13. 验证工具族

### 13.1 `apps/backend/app/tools/verification/test_runner.py`

写入能力：

- 运行测试命令。
- 测试结果归一化。

职责：

- 执行项目测试并返回摘要。

不写入：

- 通用命令执行细节。

理由：

- 测试是 command 的特化场景，但需要专门解析结果和绑定验收。

### 13.2 `apps/backend/app/tools/verification/lint_runner.py`

写入能力：

- lint 命令运行和结果解析。

职责：

- 归一化 lint 失败信息。

不写入：

- test / build。

理由：

- lint 失败结构和测试失败结构不同，分开能减少条件分支。

### 13.3 `apps/backend/app/tools/verification/build_runner.py`

写入能力：

- build 命令运行。
- build `ArtifactRequest` 摘要。

职责：

- 验证应用能否构建。

不写入：

- dev server 启动。

理由：

- build 是交付闭环，不应和普通命令混在一起。

### 13.4 `apps/backend/app/tools/verification/result_parser.py`

写入能力：

- pytest / unittest / vitest / npm build 输出解析。

职责：

- 将不同工具输出归一化为 `VerificationResult`。

不写入：

- 命令执行。

理由：

- 解析逻辑容易膨胀，独立后可逐步扩展。

## 14. API 与 Runtime 集成

### 14.1 `apps/backend/app/api/app.py`

写入能力：

- FastAPI 应用创建。
- include router 组装。
- shutdown 生命周期。

职责：

- 只做应用装配和全局生命周期。

不写入：

- 具体工具路由。
- artifact 路由。
- 审批路由。
- 业务逻辑。

理由：

- `app.py` 已经承载任务、SSE、审批等入口，后续必须逐步拆路由；工具平台不能继续向 `app.py` 追加路由，避免 API 文件膨胀。

### 14.2 `apps/backend/app/api/tools.py`

写入能力：

- 查询工具目录 API。
- 查询工具搜索 API。
- 查询工具选择调试 API。
- 查询工具执行详情 API。

职责：

- 校验工具相关 HTTP 输入。
- 调用工具 catalog/search/execution service。
- 格式化工具响应。

不写入：

- 工具执行逻辑。
- Selector 算法。
- SQL。

理由：

- 工具 API 独立路由，避免继续膨胀 `api/app.py`。

### 14.3 `apps/backend/app/api/artifacts.py`

写入能力：

- 查询 artifact 元数据 API。
- 读取 artifact 摘要 API。

职责：

- 校验 artifact 查询请求。
- 调用 artifacts service 的只读查询接口。
- 返回可展示的 artifact 信息。

不写入：

- artifact 文件写入。
- artifact 创建 API。
- 工具执行。

理由：

- artifact 会被工具、命令、测试和 diff 复用，应有独立路由边界。
- 第一阶段 artifact 创建只能发生在 `ToolCallExecutor` 调用 artifacts service 的执行链内，避免绕过审批、幂等和日志。

### 14.4 `apps/backend/app/api/approvals.py`

写入能力：

- 迁移现有审批列表和决策路由。
- 保持现有 URL/API 契约，或在确需变更时提供明确迁移映射。

职责：

- 只处理审批 HTTP 输入输出。

不写入：

- 工具执行。
- LangGraph resume 细节。

理由：

- 审批 API 已存在于 `app.py`，后续应迁出，保持审批边界清晰；前端已有 `services/approvals.ts`，迁移路由模块不应迫使客户端无意义重写。

### 14.5 `apps/backend/app/api/dependencies.py`

写入能力：

- 构建 Tool Registry。
- 构建 Tool Catalog。
- 构建 Tool Selector。
- 构建 Tool Runtime。
- 构建 Tool Call Executor。
- 构建 Tool Concurrent Scheduler。
- 构建 Tool Resource Lock Manager。
- 注入 Agent Runtime。

职责：

- 依赖组装。

不写入：

- 业务逻辑。
- 工具策略。

理由：

- 依赖构建器只做 wiring，保证运行时可测试。

### 14.6 `apps/backend/app/core/runtime/runner.py`

写入能力：

- 使用 Tool Selector 获取本轮工具。
- 将 selected tools 注入模型调用。
- 将模型 tool call 交给 Tool Runtime。
- 接收多个并发 tool call 时，交给 ToolRuntime 的并发入口，不直接自行调度。

职责：

- 任务生命周期协调。

不写入：

- 工具执行细节。
- 具体工具 handler。
- 工具检索算法。

理由：

- Runtime 是协调者，不是工具平台实现位置。

## 15. 前端代码落点

### 15.1 `packages/shared/ts/tools.ts`

写入能力：

- `ToolDefinitionSummary`
- `SelectedTool`
- `ToolObservation`
- `ToolRiskLevel`

职责：

- 前后端共享工具基础类型。

不写入：

- UI 状态。
- HTTP 逻辑。

理由：

- 避免前端 service 重复定义后端响应类型。

### 15.2 `packages/shared/ts/toolCatalog.ts`

写入能力：

- `ToolCatalogRecord`
- `ToolSearchResult`
- `ToolSelectionContext`

职责：

- 共享工具目录和搜索响应类型。

不写入：

- 具体组件 props。

理由：

- 工具目录后续会被 UI、调试面板和 selector 测试复用。

### 15.3 `apps/desktop/src/services/tools.ts`

写入能力：

- `fetchToolCatalog()`
- `fetchToolExecutions()`
- `fetchToolExecutionDetail()`

职责：

- 工具相关 HTTP 通信。

不写入：

- Zustand 状态。
- JSX。

理由：

- service 层只做外部通信，符合客户端规范。

### 15.4 `apps/desktop/src/stores/toolStore.ts`

写入能力：

- 工具目录状态。
- 工具执行列表状态。
- 当前选中的 tool execution。
- 并发工具调用组的展示状态。

职责：

- 保存工具领域 UI 状态。

不写入：

- fetch。
- JSX。

理由：

- store 只做状态，不做副作用。

### 15.5 `apps/desktop/src/hooks/useTools.ts`

写入能力：

- 加载工具目录。
- 加载工具执行详情。
- 错误状态编排。

职责：

- 编排 service 与 store。
- 调用统一 logger。

不写入：

- 组件渲染。
- Tauri IPC。

理由：

- hook 是前端业务编排边界。

### 15.6 `apps/desktop/src/components/chat/ToolCallCard.tsx`

写入能力：

- 扩展现有聊天流工具调用摘要展示。

职责：

- 渲染工具名、状态、风险、耗时和摘要。

不写入：

- 数据加载。
- 审批提交。

理由：

- 该组件已存在于聊天组件目录，继续扩展可避免 `components/tools/ToolCallCard.tsx` 平行实现。

### 15.7 `apps/desktop/src/components/tools/ToolResultPanel.tsx`

写入能力：

- 工具结果详情展示。
- artifact 链接展示。

职责：

- 展示工具执行详情和错误信息。

不写入：

- artifact 下载逻辑。
- 网络请求。

理由：

- 展示详情和数据加载分离，避免组件膨胀。

## 16. 工具选择策略

第一版选择策略：

```text
输入：
  task text
  run status
  workflow phase
  agent profile permissions
  recent tool failures
  current files / active task context

流程：
  1. 按权限过滤。
  2. 按 workflow phase 过滤。
  3. 按 tags 和 namespace 做规则召回。
  4. 需要时用 Tool Search 召回。
  5. 风险降级或升级审批。
  6. 截断到默认 3-8 个工具。

输出：
  selected tool definitions
  selection reason
  hidden tool count
```

默认阶段工具集：

- 只读分析：`read_file`、`list_directory`、`search_text`。
- 文件编辑：`read_file`、`search_text`、`diff_preview`、`apply_patch`。
- 命令验证：`run_command`、`run_tests`、`run_build`。
- Git 审查：`git_status`、`git_diff`。
- 提交阶段：`git_status`、`git_diff`、`git_stage`、`git_commit`。

理由：

- 模型每轮只看相关工具，降低错误调用和上下文污染。

## 17. 工具审批策略

默认权限分级：

```text
safe_read          自动允许
file_edit_low      可按路径和 diff 自动允许
file_edit_high     必须审批
command_read       可自动允许
command_write      必须审批
command_network    必须审批或禁止
git_read           自动允许
git_write          必须用户明确意图
destructive        默认禁止或强审批
```

审批触发条件：

- 修改文件。
- 执行可能改变文件系统的命令。
- 安装依赖。
- 启动长运行进程。
- 修改 Git index 或 commit。
- 修改规则文件、环境文件、锁文件、项目配置。

审批必须包含：

- 工具名。
- 参数摘要。
- 影响文件。
- 风险等级。
- 预览 diff 或命令摘要。
- 幂等键。
- 可取消或回滚说明。

理由：

- 审批是用户信任边界，不能只显示“是否允许工具执行”。

## 18. Artifact 策略

统一落盘规则：

- 具体工具 handler 和工具族模块只能返回 `ArtifactRequest` 或 `ArtifactCandidate`。
- `ToolCallExecutor` 是唯一允许调用 `artifacts` service 创建 artifact 记录和文件的工具执行层。
- `ToolRuntime` 只持久化 tool call 与 artifact id 的关联关系，不直接写 artifact 文件。
- `ToolObservationBuilder` 只消费已落盘的 artifact id 和摘要，不负责创建 artifact。
- `api/artifacts.py` 第一阶段只提供查询接口，不提供工具执行链外的 artifact 创建入口。

必须 artifact 化的内容：

- 大 diff。
- 命令完整 stdout/stderr。
- 测试完整日志。
- build 输出。
- 文件编辑前快照。
- 工具异常堆栈。

返回给模型的 observation：

- 状态。
- 关键摘要。
- artifact_id。
- 下一步建议。

不返回：

- 大段日志全文。
- 二进制内容。
- secret 或疑似敏感信息。

理由：

- artifact 保留可排查性，observation 控制上下文成本。

## 19. 日志与审计

Tool Platform 必须记录：

- tool_selected：选择了哪些工具以及原因。
- tool_call_created：模型请求了什么工具。
- tool_policy_decided：策略结果。
- tool_approval_requested：审批请求。
- tool_execution_started：执行开始。
- tool_execution_completed：执行完成。
- tool_execution_failed：执行失败。
- tool_artifact_created：产物落盘。
- tool_execution_cancelled：执行取消。
- tool_concurrency_planned：并发计划。
- tool_concurrency_group_started：并发组开始。
- tool_concurrency_group_finished：并发组结束。

日志必须包含：

- run_id。
- task_id。
- step_id。
- tool_call_id。
- tool_name。
- idempotency_key。
- duration_ms。
- status。
- risk_level。
- concurrency_group_id。

禁止记录：

- API key、token、cookie。
- 完整 `.env`。
- 大输出全文。

理由：

- 工具问题通常涉及模型、策略、执行器和文件系统，多维 ID 是排查所必需的。

## 20. 测试规划

后端测试：

```text
apps/backend/tests/test_tool_catalog.py
apps/backend/tests/test_tool_selector.py
apps/backend/tests/test_tool_runtime.py
apps/backend/tests/test_tool_concurrency.py
apps/backend/tests/test_file_read_tools.py
apps/backend/tests/test_file_edit_tools.py
apps/backend/tests/test_command_tools.py
apps/backend/tests/test_git_tools.py
apps/backend/tests/test_verification_tools.py
```

必须覆盖：

- 工具重复注册失败。
- 工具搜索召回。
- selector 不暴露高风险工具。
- schema 校验失败。
- 幂等重试不重复副作用。
- 审批前不执行危险工具。
- 审批 resume 后执行一次。
- 并发工具调用按 tool_call_id 对齐结果。
- 读工具可并发，写同一文件必须串行。
- 单个并发工具失败不吞掉其他工具结果。
- 文件路径逃逸被拒绝。
- 大输出生成 `ArtifactRequest`，并由 `ToolCallExecutor` 统一落盘为 artifact。
- 命令 timeout 和 cancel。
- Git destructive 操作被拒绝。

前端测试：

```text
apps/desktop/src/tests/toolStore.test.ts
apps/desktop/src/tests/toolsService.test.ts
apps/desktop/src/tests/useTools.test.ts
```

必须覆盖：

- service HTTP 错误写入 logger。
- store 只更新状态不发请求。
- hook 正确编排加载、失败和清理。
- 组件不直接 fetch。

## 21. 实施顺序

### 阶段一：Tool Platform 骨架

实现：

- Catalog records/store/index/search。
- Selector。
- Runtime。
- Tool Call Executor。
- Concurrent Scheduler。
- Resource locks。
- idempotency/lifecycle。
- shared types。
- `api/tools.py` 和 `api/artifacts.py` 路由模块。
- `api/approvals.py` 迁移现有审批路由，保持 URL/API 契约不漂移。
- `app.py` 只 include router，不新增工具路由正文。
- 现有 `ToolScheduler` 兼容调用 `ToolRuntime`。
- 新工具禁止直接接入旧 `ToolScheduler`。

不实现：

- 全量 Git。
- 复杂 PTY。
- MCP。

理由：

- 先确定所有工具接入形态，避免后续每个工具自己发明执行闭环。
- 同阶段必须完成迁移检查，不能留下新旧两套工具入口并行。
- 同阶段必须完成并发调度骨架，避免后续命令和文件编辑工具再次重写执行链。

### 阶段二：文件读取与文件编辑

实现：

- file_read 迁移现有 safe_read。
- file_edit plan/diff/snapshot/apply_patch/write/rollback/policy。
- 审批接入。

理由：

- 文件编辑是 coding agent 核心能力，且最依赖审批、artifact 和幂等。

### 阶段三：命令执行与验证

实现：

- command process/output/cancellation/policy。
- verification test/lint/build。
- 大输出 `ArtifactRequest`。

理由：

- 命令执行风险高，必须建立在 Tool Runtime 和审批闭环之后。

### 阶段四：Git 工具

实现：

- git status/diff。
- stage/commit。
- destructive 操作禁止策略。

理由：

- Git 写操作影响用户工作区，必须在文件编辑和验证稳定后再做。

### 阶段五：高级能力预留

预留：

- embedding rerank。
- 外部工具 provider。
- MCP adapter。
- subagent tool bridge。

理由：

- 本轮不做 MCP，但接口边界不能锁死。

## 22. 非目标

本方案不做：

- 一次性实现所有具体工具。
- 把所有工具 schema 全量注入模型。
- 让 prompt 承担安全策略。
- MCP。
- 云端 CI/CD。
- PR 创建和远程 GitHub 集成。
- 通用插件市场。
- 第一阶段不追求所有工具都并发执行；只建立并发调度和资源锁框架。

## 23. 验收标准

进入实现后，第一轮 Tool Platform 必须满足：

- 只看目录能理解工具平台、工具目录、具体工具族和执行记录的边界。
- Tool Registry 和 Tool Catalog 不混用。
- Tool Selector 能控制每轮模型可见工具数量。
- Tool Runtime 是唯一工具执行入口。
- `ToolRuntime`、`ToolConcurrentScheduler`、`ToolCallExecutor` 依赖方向单向，不能出现 `Runtime -> Scheduler -> Runtime` 循环调用。
- artifact 只能由 `ToolCallExecutor` 统一调用 artifacts service 落盘，其他层只能生成或传递 `ArtifactRequest`、artifact id 和摘要。
- Tool v2 核心执行链不继续堆进旧 `ToolScheduler`。
- `ToolConcurrentScheduler` 和资源锁边界存在，且有并发测试。
- `api/app.py` 不包含工具平台路由正文，只负责 include router 和应用生命周期。
- 现有 `safe_read.py`、`ToolScheduler`、`ToolCallCard`、`useApprovals`、`services/approvals.ts` 有明确迁移或扩展结果，不能出现平行实现。
- `ToolPolicyDecision.status` 与 `ToolExecutionLifecycle` 状态有明确映射，持久化状态不混用。
- 危险工具审批前不会产生副作用。
- 所有工具调用都有 tool_call 记录和幂等键。
- 大输出会生成 artifact。
- 错误日志包含 run/task/step/tool/idempotency 关键上下文。
- 前端组件不直接 fetch 或 IPC。
- 共享类型不在 service 中重复定义。
- 新增函数写完整中文 docstring，导出 TS 函数/类型写完整 TSDoc。

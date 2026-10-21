# Workflow 与 Assistant 解耦改造计划

## 目标

把 ReAct workflow 做成与对话无关的执行内核：任何使用方（对话、Agent Team 节点、以后的脚本或定时任务）都通过同一个门面驱动它，Assistant Transport 只是其中一个使用方（HTTP/SSE 接入 + 事件投影）。

本地单机、单用户：并发互斥只靠「Task 闸门 + Run 条件迁移」两件事，不叠加多层前置校验、快照分叉自愈、重复收敛。

## 输入语义（已确认）

| 输入 | 前置状态 | 行为 |
|---|---|---|
| 发送消息（立即） | 有执行者在跑（running） | 入 inbox（`immediate`），循环在下一个 MODEL 边界并入当前 Run 的上下文 |
| 发送消息（排队） | 有执行者在跑（running） | 入 inbox（`after_run`），当前 Run 不消费；Run 正常完成后由交接新开 Run 处理 |
| 发送消息（立即） | 等待审批（waiting_for_input） | 入 inbox（`immediate`），并以「驳回」决定恢复 Run：待决调用按驳回结算，消息在 MODEL 边界作为普通 user message 进入上下文，即「驳回 + 反馈」 |
| 发送消息（排队） | 等待审批（waiting_for_input） | 入 inbox（`after_run`），不影响审批；Run 结束后再处理 |
| 发送消息 | 无 Run 或最新 Run 已终态（completed / cancelled / failed） | 不排队，直接新开 Run；inbox 里残留的 `after_run` 消息按顺序一并并入这个新 Run |
| 编辑消息 | 最新 Run 已终态且无执行者；目标是最新一条已消费的用户消息 | 截断该消息及其后 context，写入新内容，重置同一 Run 并恢复运行 |
| 恢复续跑 | 最新 Run 为 cancelled / failed | 从 MODEL 边界继续同一 Run，不重放工具 |
| 用户决定（HIL） | waiting_for_input | 写入决定，从 wait 节点继续 |
| 取消 | 任意活动态 | 只发 Task 信号，循环在检查点落定（已完成） |

投递方式只在「有活动 Run」时有意义，由前端在发送时选择（立即 / 排队），作为 inbox 行的 `delivery` 字段持久化。规则：
- MODEL 边界只并入 `immediate` 消息；
- Run 正常完成（completed）且没有收到取消时，释放边界把 `after_run` 消息交给新 Run；
- Run 以 cancelled / failed 结束时，`after_run` 消息保留不自动启动（用户主动停止了），在下一次「发送消息」新开 Run 时一并并入；
- 续跑 cancelled / failed 的 Run 时，同样只消费 `immediate`，`after_run` 仍等这次续跑正常完成后交接。

由此带来的减法：
- 不再支持「运行中编辑排队消息」（`TaskInputService.edit_latest` 的 inbox 分支删除）。
- 取消/失败后发消息不再「只排队等继续」，直接新开 Run；`submit_input` 里的暂停态排队分支删除。

## Task / message / Run 语义

| 对象 | 含义 | 在接口中的角色 |
|---|---|---|
| Task | 一段对话 | 发送消息、恢复、取消、用户决定、订阅的目标，前端以 `task_id` 指定 |
| message | 一条用户消息 | 编辑的目标，前端以 `message_id` 指定 |
| Run | Task 内的一次执行，由后端创建和推进 | 只读：快照里用于消息分组、状态与用量展示；命令与订阅请求都不携带 `runId` |

现状 `runId` 兼任模式开关（有消息且有 runId 为编辑、只有 runId 为恢复、都没有为新建），后端还要校验它等于最新 Run，前端持有旧值时只会多一种失败。改造后前端只依赖 `taskId` 与 `message_id`，Run 身份完全由后端解析。

## 现状问题

- `core/workflow` 直接依赖外层：`assistant_transport.event`（`tools_node`、`streaming_part_state_machine`、`tool_observation_projection`）、`service.depends`（`workflow_operations`）、`TaskInputService`（`workflow.py`）、`task_runtime_spaces`（`workflow`、`model_node`、`tools_node`、`workflow_operations`）。
- 调度规则分散在 5 处：`assistant_api.assistant_transport`（预读状态 + `wait_released`）、`TransportAssistantService`（classify / ensure / prepare）、`ConversationRunCommandService`（submit / start / edit / resume / start_queued_run）、`TaskInputService.edit_latest`、`ConversationRunExecutor._release_and_handoff`。
- 执行入口有三层：`ConversationRunExecutor._execute` → `AgentRuntime.run_agent` → `ReactLikeWorkflow.run`；Agent Team 节点绕过执行器，直接 `asyncio.run(runner.run_agent)`，因此取消、释放要为它单独写分支。
- 防御性重复：`_assert_no_active_run` 与闸门、`_ensure_run_visible` 快照分叉重建、`settle_run_start_failure` / `_converge_failed_setup` / `_converge_unfinished_run` 三处收敛。
- Run 状态迁移：`_RUN_STATUS_TRANSITIONS` 状态图已存在，但迁移散在 `ConversationRunStateService` 的 8 个方法里，各自拼接字段、各自发事件；还有绕过状态图的写入：
  - `reset_for_edit` 硬编码 `completed / cancelled / failed → running`，而状态图里 `COMPLETED: ()`，状态图与事实不一致；
  - `ConversationRunService.recover_orphaned_runs` 直接调 CRUD 条件更新。
  - 写 Run 终态的调用点分布在 `model_node`（3 处）、`observation_node`、`wait_user_node`、`finalize_max_steps`、`workflow.py`、`runner`、执行器。
- 节点流转：`workflow.run` 用 `if/elif` 按 `ReactRoute` 分派，新增节点要改循环；取消检查和 inbox 并入写在循环分支里，与 `model` 节点内的取消检查重复。

## 目标结构

```text
core/events/                    执行事件事实模型 + EventSink 端口（工具层与 workflow 共用），不含投影
core/workflow/                  内核：不 import assistant_transport / service / task_runtime
  ports.py                      RunStore / Inbox / EventSink 三个 Protocol
  run_state_machine.py          Run 命名迁移表（来源 → 目标 + 附带写入）
  workflow.py, nodes/, node_helper/   节点注册表 + 按 ReactRoute 流转的循环；依赖只来自 RuntimeContext 注入
core/runtime/                   执行器（每 Task 单执行者、取消信号、释放与交接）与 AgentRuntime 装配
service/conversation_run/       对话适配：用 SQLite + RuntimeMessageContextManager 实现端口；
                                workflow_session.py = WorkflowSession（submit / edit / resume / decide / cancel）
assistant_transport/            HTTP ↔ WorkflowSession；EventSink 实现 = 现有 projector + projection/ 下的事件 planner
```

端口只有三个，其余直接复用已有的核心对象：

| 端口 | 职责 | 对话实现 |
|---|---|---|
| `RunStore` | Run 创建、条件迁移（claim / complete / fail / cancel / wait / resume / reset）、保存进度 | `ConversationRunStateService` + `ConversationRunService` |
| `Inbox` | 入队（带 `delivery`）、在 MODEL 边界并入 `immediate` 消息、新 Run 开始时放行 `after_run` 消息 | `TaskInputService`（只保留 enqueue / consume，加 `delivery` 过滤） |
| `EventSink` | 接收执行事件 | `ConversationEventProjector.process` |

直接复用、不再包一层：`RuntimeMessageContextManager`（上下文 owner，经 session 注入）、`ConversationRunCancellationRegistry`、`ToolCallCancellationRegistry`、工具执行器、`ToolContinuation` / `wait_user_node`。

### WorkflowSession（唯一调度入口）

所有方法都在 Task 闸门内完成「读状态 → 条件迁移 → 交给 driver」，调用方不再预读状态或等待执行释放。

```text
submit(task, message, delivery):                      # delivery: immediate | after_run
    latest = run_store.latest(task)
    if driver.is_running(task) or latest is waiting:
        inbox.enqueue(message, delivery)
        if delivery is immediate and latest is waiting:
            resume_waiting(latest, REJECT)                # 驳回 + 消息作反馈
        return                                            # immediate 在 MODEL 边界并入；after_run 等交接
    inbox.enqueue(message, immediate)                     # 终态或无 Run：不排队
    run = run_store.create(task)                          # 新 Run 首个 MODEL 边界并入全部待消费消息
    driver.start(run, "fresh")

edit(task, message_id, message):   要求最新 Run 终态、无执行者、目标是最新用户消息
    同一事务：截断 context(≥ 目标序号) + 写入新消息 + run_store.reset(run)
    driver.start(run, "resume")

resume(task):                       要求最新 Run 为 cancelled / failed
    run_store.resume(run); driver.start(run, "resume")

decide(task, request_id, decision): 要求 waiting_for_input
    run_store.resume_waiting(run, decision); driver.start(run, "resume_with_input")

cancel(task):                       driver.cancel(task)（现有实现搬入）
```

「执行释放时 Run 正常完成且有 `after_run` 消息 → 新开 Run」继续由 driver 的释放边界负责（现有交接逻辑），与 `submit` 共用 `run_store.create` + `driver.start`，不再经 `ConversationRunCommandService`。新 Run 开始时先把这些 `after_run` 消息转为 `immediate`，再按普通流程在 MODEL 边界并入。

### 执行器与 AgentRuntime（不合并）

实施时没有把 `ConversationRunExecutor` 与 `AgentRuntime.run_agent` 合并成一个 driver：两者职责不同（执行器负责每 Task 单执行者、取消信号、释放与交接；`AgentRuntime` 负责装配 operations、驱动 workflow 与失败归类），合并只会得到一个八百行以上、承担两类职责的文件。保留两者，只做：
- 交接改为调用 `WorkflowSession.open_queued_run`；
- 执行器启动失败的收敛统一在 `WorkflowSession._start`（迁移 `CANCEL` 后上抛），删除 `settle_run_start_failure`、`_converge_failed_setup`。
- Agent Team Coordinator 改走执行器的工作仍按阶段 5 进行。

`WorkflowSession` 放在 `service/conversation_run/` 而不是 `core/workflow/`：它编排 Run 创建、inbox、snapshot 发布等对话侧服务，属于用例编排层；放进内核需要再抽六七个端口，属于过度抽象。

## Run 状态机

Run 生命周期（持久化事实）与节点流转（进程内执行步骤）是两层，分开建模，互不引用。

用一张迁移表定义所有「命名迁移」，不引入状态机库：

| 迁移 | 来源 | 目标 | 附带写入 |
|---|---|---|---|
| `claim` | pending | running | — |
| `suspend` | running | waiting_for_input | 保存 continuation 与用量 |
| `resume_with_decision` | waiting_for_input | running | 写入决定 |
| `resume` | cancelled, failed | running | 清空终态字段 |
| `reset_for_edit` | completed, cancelled, failed | running | 清空终态字段，进度只保留步数 |
| `complete` | running | completed | 最终输出、结束原因、用量 |
| `fail` | pending, running | failed | 错误、最终输出、用量 |
| `cancel` | pending, running | cancelled | 结束原因、用量 |

- `RunStore.transition(run_id, RunTransition.X, **fields) -> Run | None`：一次条件更新（`WHERE status IN 来源`），命中后发一次 `RunStatusChangedEvent`；未命中返回 `None`，由调用方决定语义（多数是「已被别处落定」，直接忽略）。
- 传入外部 `session` 时加入调用方事务且不发布事件：编辑重置在提交后重建 snapshot，重启收敛时尚无订阅者。因此重启收敛不单列迁移，复用 `CANCEL`。
- 删除 8 个 `*_if_running` / `resume_*` / `reset_for_edit` 方法和 `_allowed_source_statuses`；可编辑、可续跑、是否活动等判断全部从这张表派生，不再在各处手写状态集合。
- 节点通过 `RunStore.transition` 写 Run 状态，不再各自拼字段：`model` 写 complete / cancel，`wait_user` 写 suspend，失败由 driver 统一写 fail。
- 不做的事：迁移审计表、迁移钩子、乐观锁版本号。单机单用户下，条件更新加 Task 闸门已足够。

## 节点流转、恢复与取消

保持现在的 `ReactRoute` 流转方式（类似 LangGraph 的「节点返回下一跳」），只把硬编码的 `if/elif` 换成注册表，不引入图对象、边校验或节点级 checkpoint。

```text
节点签名:   async (state) -> patch，patch 里带 next_node: ReactRoute
注册表:     nodes: dict[ReactRoute, Node]，ReAct 默认注册 model / tools / observe / wait_user / structured_output
扩展方式:   ReactRoute 新增一个成员 + 构造时经 nodes 传入该节点；上游节点返回该 route 即可流入
循环:       while next_node != END: patch = await nodes[next_node](state); 合并 patch; 保存进度
```

- 循环只做「按 route 取节点 → 执行 → 合并 → 保存进度」。现在写在循环里的 MODEL 前取消检查、inbox 并入，移进 `model` 节点开头（`model` 节点本来就有一次取消检查，合并为一处）；WAIT_USER 的取消分支移进 `wait_user` 节点。
- 不同 Agent 需要不同流程时，由装配方传入不同的注册表（例如 Team 节点不注册 `wait_user`）；不做更多抽象。

**恢复点**：只有两个。
- 等待用户决定的 Run：从 `wait_user` 节点恢复，消费已持久化的决定。
- 其他所有情况（新 Run、取消/失败后续跑、编辑重跑、交接新 Run）：都从 `model` 节点开始。

新增节点要满足这个前提：从 `model` 重新进入时可以安全重跑，或者在到达下一次 `model` 之前已经完成。

**取消**：全部依赖 Task 取消信号，只在 `model` 节点落定 cancelled；其他节点只负责尽快流转到 `model`：
- 工具检测到信号返回 cancelled 观察 → `observe` → `model`；
- `wait_user` 检测到信号，或因取消而以强制拒绝恢复时，按驳回结算 → `observe` → `model`；
- `model` 节点开头检测到信号 → `cancel` 迁移，结束本次驱动。

## 复用原则与清单

按第零铁律，改造以「搬迁、改名、收窄依赖」为主，不重写已经正确的逻辑。真正新写的只有：
- 三个 Protocol；
- 命名迁移表与 `transition`；
- 节点注册表；
- `WorkflowSession` 门面（调度规则本身也搬自现有方法）。

被取代的旧代码同一阶段删除，不保留双轨。

| 目标 | 复用的现有代码 | 处理方式 |
|---|---|---|
| 事件事实模型 | `assistant_transport/event/*` 各事件类的字段与校验 | 原样搬到 `core/events/` |
| 事件投影 | 各事件类的 `plan()` 方法体、信封里的 `_find_run` / `_find_tool` 等定位辅助 | 方法体原样搬成 `assistant_transport/projection/` 下的 planner 函数 |
| `EventSink` | `ConversationEventProjector.process`、`dispatch_conversation_event` | projector 即对话侧 sink，不新增实现 |
| `RunStore` 与迁移表 | `_RUN_STATUS_TRANSITIONS`、`ConversationRunCrud.update_status_if_in`、各迁移方法里的字段写入、`RunStatusChangedEvent` 发布 | 状态图扩成命名迁移表，字段写入并入表项，发布逻辑原样保留 |
| `Inbox` | `TaskInputService.enqueue` / `consume` / `consume_into_context`、`QueuedTaskInput` 载荷 | 原样实现端口；载荷加 `delivery` 字段，`consume` 加一个过滤条件；删掉 `edit_latest` 的 inbox 分支 |
| 消息上下文 | `RuntimeMessageContextManager`、`close_unclosed_tool_calls`、`ConversationTaskContextCrud.delete_from_sequence` | 直接注入，编辑截断沿用现有事务写法 |
| `WorkflowSession` | `submit_input`、`start_run`、`_activate_created_run`、`edit_latest` 已消费分支、`resume_run` 的校验与迁移 | 逻辑搬入五个方法，原处删除 |
| driver | `ConversationRunExecutor`（启动、取消、释放与交接、等待态强制拒绝恢复）、`AgentRuntime.run_agent` 的异常归类 `_failure_code_for` | 整体搬迁，去掉对 assistant_transport 的直接引用 |
| 取消 | Task 级 `cancellation_registry`、`ToolCallCancellationRegistry`、`FORCE_REJECT` 决定与 `wait_user_node` 的驳回结算 | 不改语义，只把检查点收口到 `model` / `wait_user` 节点 |
| 节点 | `model_node`、`tools_node`、`observation_node`、`wait_user_node`、`structured_output_node` | 函数签名不变，注册进 `nodes` 字典 |
| Task 互斥 | `task_runtime_spaces` 的 `async_operation` 闸门 | session 与 driver 共用，不新增锁 |
| 前端 | `lib/http/client.ts`、`cancel-task.ts` 的请求写法 | 新端点照同一写法各写一个调用函数 |

## 分阶段实施

每阶段独立可合并，阶段内补齐对应测试；不保留兼容层。

### 阶段 1：事件拆分，切断对 assistant_transport 的依赖
- 现有事件类同时承载「事实字段」和「投影成快照 mutation 的 `plan()`」，后者依赖 `assistant_transport.state`，不能整体下沉。拆成两半：
  - 事实模型下沉到 `core/events/`（信封、文本增量、part 收口、工具调用、工具运行期更新、Run 事件），只保留字段与校验；联合类型改名 `WorkflowEvent`。放在 `core/` 而不是 `core/workflow/` 下，是因为工具层（`tool_terminal_projection`）也产出事件，工具层位于 workflow 之下。
  - `UserInputAppendedEvent` 只由 Transport 产生和消费（parts 是 Transport 展示形状），留在 `assistant_transport/event/`。
  - 投影逻辑留在 `assistant_transport/projection/`：按事件类型注册 planner 函数，快照定位辅助（`_find_run`、`_find_tool` 等）一并迁入；projector 改为 `plan_event(event, state)`。
- `EventSink` Protocol；`dispatch_conversation_event` 与 `WorkflowOperations.process_event` 改为调用注入的 sink。
- `ToolRuntimeOutputChannelFactory` 只依赖 `EventSink`，留在适配层装配。
- 测试中直接调 `event.plan(state)` 的用例（约 10 个文件）改为 `plan_event(event, state)`。
- 新增一个架构测试：扫描 `app/core/**`，断言不 import `app.assistant_transport`；`app.service`、`app.task_runtime` 两项在阶段 2 结束后加入。

### 阶段 2：端口注入与 Run 状态机
- 定义 `RunStore`、`Inbox` Protocol，对话实现放在 `service/conversation_run/`。
- `RunStore.transition` 按「Run 状态机」一节的迁移表实现；删除各个 `*_if_running`、`resume_*`、`reset_for_edit` 方法，`recover_orphaned_runs` 与编辑重置都改走 `transition`，修正状态图与 `reset_for_edit` 不一致的问题。
- 端口注入到现有门面 `WorkflowOperations`（由 `AgentRuntime` 装配），不另建上下文对象：`run_store`、`inbox`、`task_space`（`TaskSpace` 协议，`TaskRuntimeSpace` 结构上直接满足，提供上下文 owner、运行期通知与固化工具 schema）；`workflow.py`、`model_node`、`tools_node`、`workflow_operations` 删除对 `service.depends`、`TaskInputService`、`task_runtime_spaces` 的直接引用。
- `WorkflowOperations` 的 `complete/fail/cancel/mark_waiting` 四个包装合并为 `transition_run(RunTransition.X, ...)`；`save_execution` 从 `ConversationRunService` 移到 `ConversationRunStateService`，Run 写入集中在一处。
- 架构测试：`app/core/workflow`、`app/core/events` 不 import `app.service`、`app.task_runtime`（工具层与执行器仍在装配层直接取 service，按后续阶段收口）。
- 测试：迁移表逐项覆盖（合法来源命中并发一次事件、非法来源返回 `None` 且不发事件）。

### 阶段 3：节点注册与取消、恢复收口
- `workflow.py` 的 `if/elif` 换成 `nodes: dict[ReactRoute, Node]` 注册表，构造时注入（不提供运行时注册入口）；`ReactRoute` 保留。
- MODEL 前的取消检查、inbox 并入从循环移入 `model` 节点开头；WAIT_USER 的取消分支移入 `wait_user` 节点。
- 入口模式只剩两种：`resume_with_input` 从 `wait_user` 开始，其余全部从 `model` 开始。
- 测试：
  - 注册一个测试节点并由上游返回其 route，流程即可经过它，不改循环；
  - 工具执行中、等待态、模型请求前三处取消都在 `model` 节点落定 cancelled；
  - 续跑、编辑重跑从 `model` 开始，等待恢复从 `wait_user` 开始。

### 阶段 4：WorkflowSession + 新语义（已实施，前端适配见阶段 6）
- 实现 `service/conversation_run/workflow_session.py`，按上面的语义表实现五个方法；inbox 载荷增加 `delivery`，模型边界只并入 `immediate`，新 Run 开始时 `release_for_new_run` 把待消费输入全部转为 `immediate`。
- `assistant_api` 改为直接调用 session，回执为 `{state, queued}`；删除：
  - `TransportAssistantService.classify_run_command`、`ensure_run_target`、`prepare_run_start`、`settle_run_start_failure`；
  - `ConversationRunCommandService` 的 `submit_input`、`start_run`、`edit_input`、`resume_run`、`start_queued_run`、`_ensure_run_visible`、`_assert_no_active_run`、`_converge_failed_setup`（整个类大概率可删）；
  - `TaskInputService.edit_latest` 的 inbox 分支；
  - API 层的状态预读与 `wait_released`。
- Transport 入口按「Task / message / Run 语义」拆成显式端点，命令只对 Task 或 message 寻址，不带 `runId`：

  | 端点 | 语义 |
  |---|---|
  | `POST /tasks/{task_id}/messages` | 发送消息，体内带 `delivery: immediate \| after_run` |
  | `POST /tasks/{task_id}/messages/{message_id}/edit` | 编辑重跑；校验是最新一条已消费用户消息且 Task 已到终态 |
  | `POST /tasks/{task_id}/resume` | 恢复 Task：最新 Run 为 cancelled / failed 时从 model 节点续跑 |
  | `POST /tasks/{task_id}/decisions` | HIL 决定，体内带 `request_id` 与 `decision`，以 `request_id` 定位待决请求 |
  | `POST /tasks/{task_id}/cancel` | 停止 Task 当前执行（已完成） |
  | `POST /tasks/{task_id}/tool-calls/{tool_call_id}/cancel` | 取消单个工具调用；后端按 Task 当前执行解析 run_id 后写 `ToolCallCancellationRegistry`，注册表不变 |
  | `POST /tasks/{task_id}/assistant/attach` | 订阅 Task 当前执行，Run 交接时跟随；去掉必填 `runId` |
  | `GET /tasks/{task_id}/assistant/state` | 不变 |

- 随之删除：`POST /assistant` 统一入口与 `AssistantTransportRequest` 的 `commands` / `runId`、`classify_run_command`、attach 请求的 `runId`、`/runs/{run_id}/tool-calls/{tool_call_id}/cancel`；`threadId` 两种格式（`task-{id}` 与 `conversation_run-{id}`）一并去掉，由路径中的 `task_id` 表达。
- 提交侧不再使用 assistant-ui 的 commands 协议，assistant-ui 只承担渲染与运行时状态，进一步解除与其 wire 格式的耦合。
- 测试：
  - 五种输入 × 前置状态的调度矩阵；
  - 运行中「立即」发送在下一个 MODEL 边界并入；「排队」发送不被当前 Run 消费，完成后交接新 Run；
  - 等待态「立即」发送：先驳回结算，再在 MODEL 边界并入消息；「排队」发送不影响审批；
  - 取消或失败后发消息：新开 Run，残留的 `after_run` 消息一并并入；取消结束时不自动交接；
  - 运行中编辑被拒；
  - 编辑后同一 Run 从截断点重跑。

### 阶段 5：Agent Team 走执行器（已实施）
- Coordinator 不再 `asyncio.run(runner.run_agent)`：节点 Run 经 `ConversationRunExecutor.start(..., agent_profile=节点 profile)` 在主事件循环上执行，Coordinator 在 `agent_team` 工具线程里经 `run_coroutine_threadsafe` 等待其释放；主事件循环由工具从执行上下文取出，经 `confirm_and_start` 显式传入。
- Coordinator 作为循环检查父 Task 的停止信号：每轮开始前与节点结束后检查，等待节点期间检出则 `executor.cancel(节点 Task)`；检出后 TeamRun 落定 cancelled。修复了此前「停止主 Run 时，`agent_team` 工具阻塞到 Team 结束才释放，Team 因此不会被停止」的问题。
- 删除：执行器 `cancel()` 的「无执行但 running」分支、Coordinator 对节点信号的手动 `consume`、未使用的 `_runtime_loops`。
- 不做「Team 节点不注册 wait_user」：节点挂起等待时 Coordinator 本就按节点失败收敛，不注册只会把失败点提前，结果相同却要给 profile 增加标志位。

### 阶段 6：前端
- 拆分后的端点各自一个调用函数（照 `cancel-task.ts` 的写法，经 `lib/http/client.ts`），替换 `submit-assistant-transport` 里按模式拼请求的逻辑。
- 命令与订阅只依赖 `taskId` / `message_id`，不再从快照读取 `current_run_id`；`runId` 只用于渲染分组与状态展示。
- 编辑入口只在「最新 Run 终态且是最新已消费用户消息」时可用；排队消息不可编辑。
- 有活动 Run 时，发送按钮提供「立即发送 / 排队到本轮结束后」两个选项（默认值存入现有偏好 storage module）；无活动 Run 时只有普通发送。
- 等待审批时「立即发送」即驳回 + 反馈，审批卡片随状态更新；「排队」不影响卡片。
- 排队消息在消息列表中标注投递方式，排队消息不可编辑。

### 阶段 7：文档
- 更新 `AGENTS.md` 的后端架构、数据与事实所有权、代码目录边界：
  - workflow 内核 + 端口 + session 门面，对话为适配层；
  - Run 迁移表是唯一的状态迁移入口；
  - 节点注册扩展方式、两个恢复点、取消只在 model 节点落定。

## 影响范围

- 后端：`core/workflow/**`、`core/runtime/**`（执行器、runner 迁出后基本只剩注册表）、`assistant_transport/{assistant_api,service,event}`、`service/conversation_run/{task_input_service,conversation_run_state_service}`、`agent_team/coordinator.py`、`service/depends.py`、`lifespan.py`。
- 前端：`lib/assistant/` 的提交、编辑、恢复、决定调用及编辑入口条件。
- 预计净删除代码多于新增：
  - 调度入口从 5 处收敛为 1 处；
  - 失败收敛逻辑从 3 处收敛为 1 处；
  - Run 状态写入从 8 个方法收敛为一张迁移表。

## 不做

- 不引入常驻的「每 Task 监听协程」：运行中在 MODEL 边界拉取、释放时交接、空闲时 `submit` 直接启动，三者等价于监听且没有额外生命周期。
- 不做跨进程或持久化的取消信号、不做重启后自动重放。
- 不为「快照与 canonical 分叉」之类的低概率情况保留自愈分支；出现即由显式 rebuild 边界处理。

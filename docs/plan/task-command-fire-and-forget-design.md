# Task 命令改为纯生产消费（去回执）设计增补

承接 `task-actor-dispatch-design.md`（TaskActor 调度已实施）。本稿改三件相互依赖的事：

1. 生产者与 actor 之间去掉逐条命令的回执：生产者只投递，结果与错误一律经 snapshot 投影给前端；
2. 订阅改为跟随 Task 的长订阅，前端「一轮输出结束」改由 Run 状态跳变判定，与连接断开分开；
3. 对话界面去掉 Run 身份，只看 Task 的当前活动；fork 改为按消息切分。

消息模型、`dispatch_policy` 规则、actor 的串行循环与两阶段处理都不变。

状态：设计稿，未实施。分三期，见「分期」（已按代码核对结果调整）。

## 目标

现在每个命令都带一个回执 future：生产者等 actor 处理完，拿到 `SessionReceipt`（`queued`）或异常（409 / 404），HTTP 再把提交后的 snapshot 一并返回。这让「命令返回 = 结果已定」，但也让生产者感知 actor 的处理结果，`WorkflowSession` 因此是一层门面而不是单纯的投递口。

本地单用户桌面应用不需要同步命令语义：snapshot 本来就是对话、Run 与错误的权威读取来源。改成纯生产消费后：

- 生产者只做「解析输入 → 投递」，不读 Run 状态、不等处理结果；
- actor 是唯一消费者，处理结果（新 Run、唤醒、拒绝原因）全部投影到 snapshot；
- 子 Agent、Team 节点直接面向 actor 投递，需要结论时订阅静止状态，不再「提交 → 等回执」。

## 已确认的决策

- **去掉逐条命令的回执**：`SessionReceipt`、信号的 `receipt` 字段、`resolve` / `reject` / `abandon` 一并删除。
- **生产者侧的同步校验只保留生产者自己的输入边界**：输入解析失败（附件不可用、模型不支持图片等）仍同步 400；Task 不存在在投递前查一次，404。这两项是生产者的入口校验，不依赖 actor 状态。
- **actor 的处理结果只经 snapshot 表达**：状态冲突（不可恢复、重跑目标已变、上一次重跑仍在停止）投影为 snapshot 顶层 `error`，前端照常显示。
- **订阅改为跟随 Task**：寻址本来就是按 Task（`POST /tasks/{id}/assistant/attach`），改的是订阅的生命周期，见下文。
- **生命周期等待不是回执，保留**：删除前「停下并退出」、Team 节点的「静止」是对 actor 状态的订阅，不随本稿删除。
- **「一轮输出结束」拆成两个信号**：业务上的一轮结束看 snapshot 里当前活动的状态跳变；连接断开一律视为异常、进入恢复。
- **对话界面去掉 Run 身份**：snapshot 改为 Task 视角（扁平消息 + 当前活动）；Run 只保留在诊断、上下文观测页与 Agent Team / 委派自身的身份里。
- **fork 按消息切分**：请求带 `messageId`，不再带 `runId`。

## 现状核对

- 前端 attach 不带 run_id；后端 `TransportAssistantService.attach` 在请求到达时读 snapshot 的 `current_run_id`，没有 Run 时 409 `NO_RUN`。
- `TransportStreamService.stream` 底层按 Task 订阅 snapshot 变化；`run_id` 只决定流何时结束：目标 Run 进入 `TERMINAL_RUN_STATUSES`（`completed` / `failed` / `cancelled`）就结束。**挂起（`suspended`）不是终态，流保持打开**，唤醒后同一个 Run 继续在这条流上输出。`stream_envelopes` 只在目标 Run `completed` 时等交接（`wait_run_settled`）并跟到下一个 Run；取消、失败即断流。
- 前端 `sendMessage` 用回执 snapshot 的 `current_run_id` 找 Run，只有 `pending` / `running` / `suspended` 才打开流（`use-task-assistant-transport-runtime.ts`）。
- 前端 `isRunning` 把 `suspended` 视为运行中（`transport-frame-store.ts` 的 `TERMINAL_STATUSES` 不含它），挂起时显示停止按钮，与后端「挂起可取消」一致。该集合还残留后端不存在的 `idle`、`interrupted`，前端状态联合里另有后端不产生的 `pending`（只用于乐观展示）。
- snapshot 顶层已有 `error: ConversationStateError | None`（`code` + `message`），**后端从不写入非空值**：只在初始化与 run 事件投影里置空。前端已把它渲染成消息列表末尾的错误条目（`transport-frame-store.ts`）。Run 自身的错误在 `runs[].error`。
- 交互命令的拒绝现在全部经 HTTP 返回：`SessionConflictError` → 409（带稳定码），重跑目标不是最新用户消息时是 `ValueError` → 通用 409 `TASK_STATE_CONFLICT`。`submit` 从不抛 `SessionConflictError`，`child_agent_send` 对它的捕获已不可达。

## 结构

```text
生产者（HTTP 命令 / 子 Agent 工具 / Team 节点 / 审批决定 / 配置广播）
  │  解析输入（同步 400）→ 校验 Task 存在（同步 404）→ actors.send(task_id, signal)
  ▼
TaskActor（唯一消费者，不变）
  │  推进 / 收尾 / 取消 / 恢复 / 重跑；拒绝原因 → 投影 snapshot.error
  ▼
snapshot（权威读取来源）──订阅──▶ 前端（按 Task 的长订阅流）
                         └─────▶ Coordinator / 删除流程（订阅 actor 的静止、退出事件）
```

`WorkflowSession` 只剩登记表职责（取得或拉起 actor、按 Task 投递、关闭全部 actor），改名为 `TaskActors`；输入解析留在生产者调用的 `UserInputService`，不再经门面转手。

## 订阅：跟随 Task 的长订阅

| 项 | 现状 | 改为 |
|---|---|---|
| 没有 Run | 409 `NO_RUN` | 先推 full 帧（`activity` 为 `null`），保持连接等活动出现 |
| 目标 Run 到终态 | 断流（仅 `completed` 时等交接后跟到下一个 Run） | 不断流；最新 Run 变化即跟随，停下只体现在帧里的 `activity.status` |
| 结束条件 | Run 终态 / 前端断开 / resync | 前端断开 / resync / Task 删除 |
| `wait_run_settled` | SSE 跨 Run 交接用 | 删除（订阅不再绑定单个 Run） |

前端：Task 页面在 runtime 建立后保持一条订阅；命令返回后不再根据回执判断是否 attach。「一轮输出结束」的判定见下一节；envelope 的 `target_run_*` 元数据随之删除。

安静期：模型或工具长时间无输出时连接保持打开，现有 `stream` 已支持；Task 空闲时同样保持（单用户本机，一个 Task 页面一条连接，成本可忽略）。

## 前端「一轮输出结束」的新定义

### 现状

「一条流 = 一个 Run」时，SSE 断开（EOF）基本等于 Run 结束，前端把两者合成一件事，由 `onFinish` 读连接级的 `targetRunId` / `targetRunStatus` 处理：

| 职责 | 现在的位置 |
|---|---|
| 运行中状态 `isRunning`（停止按钮 / 允许发送） | `transport-frame-store.ts` 按 `targetRunStatus` 是否终态计算 |
| 刷新侧边栏任务状态、清提示条 | `onFinish` 中 Run 已终态的分支 |
| 流断开而 Run 未结束时进入恢复 | `onFinish` 中非终态分支 → `reconcileAfterTransportFinish` |
| 流断开早于最后一帧时的终态补偿 | `reconcileTerminalSnapshot` 主动拉一次 snapshot |
| 性能探针结束计时 | `finishStream` |

长订阅不再因为 Run 结束而断开，这个合并的前提不成立。

### 新定义

1. **一轮结束 = 当前活动进入终态。** 活动态与终态直接沿用后端的两个集合：活动态 = `ACTIVE_RUN_STATUSES`（`running` / `suspended`），终态 = `TERMINAL_RUN_STATUSES`（`completed` / `failed` / `cancelled`）。`TransportFrameStore` 应用每一帧时比较 `activity.status`，从活动态进入终态时触发一次 `onActivitySettled({ status })`。
   - **挂起不是一轮结束**：同一个 Run 会被唤醒继续输出，与现在「挂起时流不断」的行为一致。
   - 前端状态集合同步收敛到后端的五个值，删除残留的 `idle`、`interrupted`；乐观展示用待提交命令表达，不再借用 `pending` 状态。
   - 首次导入与页面挂载只记录基线，不触发。
   - 断线重连拿到 full 帧时，若此前记录为活动态而现在已停下，跳变照常触发一次，取代 `reconcileAfterTransportFinish` 里的状态判断。
   - 交接（上一轮完成、下一轮紧接着开始）在后端同一批信号内完成，前端看到的通常是活动态不变；即使分两帧到达，也只是一次「停下」紧接一次「开始」，界面按帧如实显示。
2. **运行中 = 有待提交的命令，或 `activity.status` 是活动态（含 `suspended`）。** 不再依赖连接级元数据；挂起时仍显示停止按钮，行为不变。
3. **连接断开 = 异常，一律进入恢复。** 长订阅只在前端主动关闭、后端要求重同步（`resync_required`）或 Task 删除时结束：意外 EOF / 网络错误进入现有的有限重连预算，不看活动状态；`resync_required` 立即重连；Task 删除由页面卸载处理。

### 职责的新归属

| 职责 | 改为 |
|---|---|
| `isRunning` | 待提交命令 + `activity.status` |
| 刷新侧边栏、清提示条 | `onActivitySettled` |
| 断流恢复 | 连接断开触发，不看活动状态 |
| 终态补偿 `reconcileTerminalSnapshot` | 删除：最后一帧与后续活动都在同一条流上到达，不存在「流先断、帧后到」 |
| 性能探针 | 一次计时对应一轮活动：进入活动态开始，`onActivitySettled` 结束 |

### 边界

- **命令被拒、没有启动活动**：不会有状态跳变，待提交命令不能等「一轮结束」才清。清除条件改为：snapshot 里出现了这条消息（排队投影或消息列表），或顶层 `error` 被写入（命令被拒，草稿恢复给用户）。
- **用户主动取消**：原来靠 `cancelRequestedRunIdRef` 在 `onFinish` 里抑制提示；改为在 `onActivitySettled` 中判断「用户点过停止，且这次停下的状态是 `cancelled`」，不再比对 runId。

## 对话界面去掉 Run，只留 Task 活动

命令已经只按 Task 或消息寻址，订阅也跟随 Task。对话界面里剩下的 Run 身份大多只是「当前在干什么」的载体：`targetRunId`、`cancelRequestedRunIdRef`、`findTransportRun(..., runId)` 都在判断「这条流或这次取消是不是当前那一轮」；停止、恢复、配置锁定、用量、错误真正需要的是**当前活动的状态**。

### snapshot 改为 Task 视角

| 现在 | 改为 |
|---|---|
| `runs[]`，每个 Run 带自己的 `messages` | 扁平的 `messages[]`；每条消息带 `runId`，只供诊断使用，对话界面不读 |
| `current_run_id` + 在 `runs[]` 中查找 | `activity`：最新 Run 的摘要 `runId`、`status`、`launch`、`usage`、`contextWindowTotal`、`error`、`endReason`；Task 尚无 Run 时为 `null`。`runId` 只供事件路由与诊断，对话界面不按它比对 |
| 助手消息的状态由所属 Run 推导（`toMessageStatusForRun`） | 每轮回复的最后一条助手消息带 `outcome`（见下文「消息级结局」） |
| `queued_messages`、顶层 `error` | 不变 |
| `approvals`（前后端都恒为空对象，Team 审批已改按 TeamRun 读取） | 删除 |

`activity` 是最新 Run 的投影，不是新的状态机：事实源仍是 `ConversationRunModel.status`。增量帧的路径从 `runs[i].messages[j]` 改为 `messages[k]` 与 `activity.*`。

### 对话界面的用法

| 场景 | 改为 |
|---|---|
| 运行中 | 待提交命令，或 `activity.status` 是活动态（`running` / `suspended`） |
| 停止 | `/cancel` 本就按 Task；取消时抑制提示的判断见上一节 |
| 恢复 | `activity.status` 为 `cancelled` / `failed`，且对话里有用户消息或排队消息时显示按钮（沿用 `canResumeStoppedRun` 的条件） |
| 配置锁定 | 读 `activity.launch` |
| 用量 / 上下文窗口 | 读 `activity.usage` / `activity.contextWindowTotal` |
| 编辑重跑资格 | 最新一条用户消息，且 `activity.status` 已停下（不再查当前 Run 的消息） |

### 保留 Run 的地方

- **诊断信息**（`run-diagnostics-actions.tsx`）：输出 `run_id`、`end_reason`、`error_code` 供排查，从消息上的 `runId` 与 `activity` 取。
- **上下文观测页**：按 Run 查看上下文的调试页，Run 是它的主体，不变。
- **Agent Team 与委派卡片**：用 `team_run_id` 与 `child_task_id`，不是对话 Run，不受影响。

### 消息级结局（替代按 Run 推导）

核对代码发现，对话界面有三处按「每个 Run」展示，不能只靠当前活动替代：

- 助手消息的状态由所属 Run 的 `status` / `endReason` / `error` 推导（`converter.ts` 的 `toMessageStatusForRun`）：历史上被取消的回复显示「已取消」，失败的显示错误；
- 每个 Run 最后一条消息下显示该轮用量（`thread.aui.tsx` 的 `RunUsageDisplay`，按 `isLastRunMessage`）；
- 每个 Run 最后一条消息下有 fork 按钮，且只在该 Run 已终态时可用。

扁平化后改为**消息级结局**：Run 到终态时，投影把结局写到该 Run 最后一条助手消息上，`outcome = { status, endReason, error, usage }`。

- 历史回复的「已取消 / 失败 / 用量」照常显示，不丢信息；
- 正在进行的回复没有 `outcome`，状态取 `activity.status`；
- fork 入口挂在带 `outcome`（即已终态）的消息上，与「按消息切分」一致；
- 消息上的 `runId` 仍保留，只供诊断与日志（如委派卡片日志里的 `parent_run_id`）。

会丢掉的只有「按 Run 分组」这层结构本身；逐轮信息由 `outcome` 承载，更细的历史交给上下文观测页。

## 第二轮核对补充的改动点

### 后端

| 位置 | 现状 | 改为 |
|---|---|---|
| 旧 Run 的迟到事件（`run_event_projection.py`，测试 `test_late_previous_run_events_cannot_overwrite_current_run`、`test_historical_run_status_event_updates_its_own_run`） | 按 `runs[i]` 定位，只改自己那个 Run | 按事件的 `run_id` 路由：与 `activity.runId` 相同才改 `activity`；旧 Run 的状态 / 结果事件只更新它那条消息的 `outcome` |
| `RunInitializedEvent.replace_existing` 与 `plan_run_initialized` 的替换分支 | 无生产者（编辑重跑已改为新建 Run，Run 只前进） | 删除 |
| 帧元数据 `source_run_id` / `current_run_id` / `current_run_status`（`stream/transport_frame.py`、envelope、前端解析） | 前端据此推断当前 Run | 删除；当前活动只经 `activity.*` 增量表达 |
| `projection/snapshot_navigation.py`（`find_run`、`find_assistant_message`、`find_tool` 按 Run 下标） | 先找 Run 再找消息 | 在扁平 `messages[]` 中按 `runId` 与消息身份查找 |
| `conversation_task_state_rebuilder.py` | 按 Run 分组重建 | 按 context 序号输出扁平消息，终态 Run 的结局写到其最后一条助手消息 |
| 任务列表 API（`TaskResponse.current_run_id` / `execution_status`） | 侧边栏与 Workbench 状态 | 不在本稿范围：它是 Task 列表的读取接口，不是对话 snapshot |

### 前端

| 位置 | 现状 | 改为 |
|---|---|---|
| Workbench 子 Agent 只读查看（`workbench-agent-run-surface.tsx`、`use-agent-reader.ts`、`lib/workbench/agent-status.tsx`、`assistant-attach-controller.ts`） | 按子 Task 的 runId 请求 / 取消 attach，Run 变化或流结束时重新 attach | 只读 runtime 同样常驻订阅子 Task，`AssistantAttachController` 的按 runId 去重与重连逻辑删除 |
| 取消（`stop-button.tsx`、`use-runtime-cancellation.ts`、`use-cancellation-confirmation.ts`） | 记录被取消的 runId，并轮询 snapshot 直到该 Run 变为 `cancelled` | Task 级「取消请求中」标记；由 `onActivitySettled` 的 `cancelled` 结束确认，删除轮询 |
| 业务恢复（`use-business-resume.ts`） | 按 runId 跟踪在途恢复 | Task 级在途标记；`activity.status` 变为活动态即视为已接受 |
| 打开页面时是否 attach（`assistant-runtime-transport-host.tsx` 的 `resumeOnMount`） | 当前 Run 活动时才 attach | 删除：页面总是常驻订阅 |
| 工具 renderer 上下文（`tool-renderer-registry.ts` 的 `runId` / `runStatus`；`delegation-tool-row.tsx` 用 `runId` + `runCancelling` 控制「取消子 Agent」按钮；`agent-team-tool.tsx` 只用于日志） | 由所属 Run 提供 | 运行判断读 `activity` 与 Task 级「取消请求中」；日志用消息上的 `runId` |
| 消息元数据（`converter.ts` 的 `custom.runId` / `runStatus` / `isLastRunMessage`）与 `thread.aui.tsx` 的 fork、用量 | 按 Run 推导 | 读消息 `outcome`；`forkingRunId` / `onForkRun` 改为按消息 |
| 诊断日志（`use-runtime-diagnostics.ts`、`use-assistant-initial-state.ts`） | 记录 `current_run_id` 与 Run 数量 | 记录 `activity` 与消息数 |
| 契约与校验（`contract.ts`、`snapshot-validation.ts`） | `runs`、`current_run_id`、`approvals` 必填 | `messages`、`activity`、`queued_messages`、`error`；状态联合收敛到后端五个值 |
| 性能探针（`assistant-performance-probe.ts`） | 以 `targetRunId` / `targetRunStatus` 结束计时 | 一次计时对应一轮活动（见「一轮输出结束」） |

## fork 按消息切分

现在 `POST /tasks/{id}/fork` 带 `runId`，复制「到该 Run 为止」的全部 Run 与 context（`TaskService._copy_fork_history`）。对话界面去掉 Run 后，入口改用消息身份：

- 请求改为 `{ messageId }`：从这条消息（含）为止分叉，入口挂在消息上。
- 复制范围：序号不晚于该消息的 context 条目；复制拥有这些条目的 Run，最后一个 Run 只带切分点之前的条目。
- 前置条件不变：被复制的 Run 都须已到终态（切分点所在 Run 运行中时 409，沿用 `RUN_NOT_READY`）。
- 克隆出的最后一个 Run 保留源 Run 的终态与结束原因；其 `final_output` 等结束事实可能对应切分点之后的内容，属展示层面的差异，fork 的对话以复制的 context 为准，可接受。

## 错误投影

- actor 拒绝一个命令时写 snapshot 顶层 `error`：`code` 沿用现有稳定码 `RUN_NOT_RESUMABLE`、`RUN_NOT_STOPPED`；重跑目标已不是最新用户消息现在只是 `ValueError`（通用 `TASK_STATE_CONFLICT`），改为新增稳定码 `EDIT_TARGET_STALE`。`message` 为面向用户的短提示；同时记结构化日志。
- 前端无需新增展示：顶层 `error` 已渲染为消息列表末尾的错误条目，只是后端此前从未写入。
- 清空时机：actor 下一次成功处理用户发起的命令（新建、唤醒、投递、恢复、重跑）时置空。Run 自身的失败在 `activity.error`，与顶层命令错误分开：顶层 `error` 只承载命令被拒；run 事件投影里对它的置空随之删除，清空只由 actor 负责。
- `edit_queued` 不经 actor（只是一次条件更新），`MESSAGE_CONSUMED` 仍同步 409：它是生产者直接对队列行的操作，结果就在手边，不需要绕 snapshot。

## 调用方变化

| 调用方 | 现状 | 改为 |
|---|---|---|
| HTTP 命令（发送 / 重跑 / 恢复 / 决定） | 等回执，返回 `CommandAccepted{state, queued}` | 投递后 202 `{}`；不返回 snapshot |
| fork | `POST /tasks/{id}/fork` 带 `runId` | 带 `messageId`，按消息切分（见上文） |
| `/cancel` | 有可停止对象时 200 `{task_id, cancelled: true}`，没有时 409 | 运行中仍在入口同步打信号；一律 202，是否停下看 snapshot |
| Coordinator 启动节点 | `submit` 返回后立即读 `current_run_id` | 投递后 `wait_idle`，再读节点 Task 最新 Run 记录 `conversation_run_ids` |
| `child_agent_create` / `child_agent_send` | 经 `run_coroutine_threadsafe(...).result()` 等回执；`child_agent_send` 捕获 `SessionConflictError`（已不可达） | 投递即返回；删除不可达的捕获，结论仍经 `agent_result` 回投 |
| `AgentTeamDecisionService` | `post_async` 等回执 | 投递即返回 |
| 删除 Task / 工作区 | `stop_tasks` 等 `Stop` 回执 | 发 `Stop`，订阅 actor 的退出事件（`actor.stopped`），等到后再删 |
| 后端关闭 | 取消回执 future | 无回执可取消，只取消执行与 actor |

## 删除清单

- `task_signals.py`：`Receipt`、各信号的 `receipt` 字段、`SessionReceipt`、`resolve` / `reject` / `abandon`；`SessionConflictError` 只剩 `edit_queued` 一处使用，随之收窄。
- `workflow_session.py`：`_request` / `_receipt`、`wait_run_settled`、`post_async` 与 `post_nowait` 的区分（统一为投递）。
- `task_actor.py`：`_stop_waiters`、`abandon`、对回执的兑现；`_flush` 只负责合并推进。
- `assistant_api.py`：`_accept` 的大部分异常映射与回执 snapshot 读取。
- `transport_assistant_service.attach` 的 `NO_RUN`；`transport_stream_service` 的 `stream_envelopes` 交接循环与 `target_run_*` 元数据。
- `assistant_transport/state/`：`ConversationRunSnapshot` 作为对话 wire 结构（`runs[]`、`current_run_id`）、`approvals`；run 事件投影里对顶层 `error` 的置空；`RunInitializedEvent.replace_existing` 及其替换分支；帧元数据 `source_run_id` / `current_run_id` / `current_run_status`。
- 前端：`TaskCommandAccepted.state` / `queued` 的使用、`sendMessage` 里按回执决定 attach 的分支；`targetRunId` / `targetRunStatus`、`cancelRequestedRunIdRef`、`reconcileTerminalSnapshot`、对话组件里按 runId 查找 Run 的逻辑（`findTransportRun`、`getTransportRunId`、`toMessageStatusForRun` 等）；`resumeOnMount`；取消确认轮询；`AssistantAttachController` 的按 runId 去重。

## 影响范围

| 区域 | 变化 |
|---|---|
| `service/conversation_run/`（`workflow_session.py` → `task_actors.py`、`task_actor.py`、`task_signals.py`） | 去回执；错误投影；退出事件 |
| `assistant_transport/`（`assistant_api.py`、`service/transport_assistant_service.py`、`service/transport_stream_service.py`（含 envelope 的 `target_run_*`）、命令错误的投影） | 202 回执；Task 长订阅；顶层 `error` 写入与清空 |
| `assistant_transport/state/`、`assistant_transport/projection/`、`assistant_transport/service/conversation_task_state_rebuilder.py` | snapshot 改为扁平 `messages[]` + `activity`；增量路径从 `("runs", i, "messages", j, ...)` 改为 `("messages", k, ...)` 与 `("activity", ...)` |
| `api/tasks_api.py`、`api/schemas/request/ForkTaskRequest.py`、`task_runtime/service/task_service.py`（`_copy_fork_history`） | fork 按 `messageId` 切分 |
| `agent_team/coordinator.py`、`service/agent_team/agent_team_decision_service.py` | 投递 + 等静止 |
| `core/tools/tool_handler/child_task/` | 投递即返回 |
| `api/tasks_api.py`、`api/workspaces_api.py`、`lifespan.py`、`service/depends.py` | 删除前等退出事件；关闭逻辑 |
| 前端 `lib/assistant/`（`task-commands.ts`、`use-task-assistant-transport-runtime.ts`、`transport-frame-store.ts`、`contract.ts`、`snapshot-validation.ts`、`converter.ts`、`conversation-actions.ts`、`run-launch.ts`、`transport-state-operations.ts`、`assistant-attach-controller.ts`、`assistant-performance-probe.ts`）、`components/assistant/`、`components/assistant-ui/`（含 `thread.aui.tsx` 与工具 renderer）、Workbench（`workbench-agent-run-surface.tsx`、`lib/workbench/`）、`components/layout/`（fork 动作）、`hooks/use-assistant-initial-state.ts` | 命令不再返回 snapshot；Task 长订阅；`onActivitySettled`；读 `activity` 与消息 `outcome`；取消与恢复改 Task 级标记；fork 入口挂在消息上 |
| 测试 | 后端：actor 与 session 测试去掉回执断言，改为断言 snapshot；新增「错误投影与清空」「无 Run 时订阅等待」「跨取消 / 挂起持续跟随」「fork 按消息切分」；投影与重建测试按新 snapshot 形状重写；前端：frame store 的状态跳变（基线不触发、重连补触发、交接）、transport runtime 单测与 E2E 模拟后端（attach 不再 409、命令 202、snapshot 新形状） |
| `AGENTS.md` | 「返回 JSON 回执」「回执 snapshot 只用于首次导入」「`/assistant/attach` 只订阅 Task 当前 Run」「前端只把 Run 用于渲染分组」等描述 |

## 风险

- **前端改造是主要风险**：`onFinish`、终态补偿、恢复预算都围绕「一条流 = 一个 Run」写成，snapshot 形状也一起变；frame store、消息转换与十几个组件同时改。长订阅与去掉 Run 都要重写 frame store，必须放在同一期做，避免重写两遍。
- **快照形状变更涉及前后端同时切换**：后端投影与前端校验必须同一期落地；绿地项目不做兼容，但 E2E 模拟后端要同步改。
- **命令错误的可见性**：拒绝不再以 HTTP 错误出现在发起处；前端需在对话区显示顶层 `error`，否则用户点「继续」后无反馈。
- **乐观显示**：命令返回不代表已生效；前端的 pending 消息要等 snapshot 排队投影或新 Run 出现再清，不能在 202 时清。
- **Coordinator 读 Run**：改为静止后读最新 Run，若节点在静止前被交接出多个 Run，只记录最后一个；Team 只关心节点结论，可接受。

## 分期

核对代码后，原先「第一期后端长订阅、前端暂不改、行为兼容」不成立：前端的 `onFinish`（刷新侧边栏、清提示条、断流恢复）只在流结束时触发，`isRunning` 依赖连接级的 `targetRunStatus`。后端单独改成长订阅后，Run 结束时流不再结束，这些都不会触发，交接后 `targetRunStatus` 还停在旧 Run 上。依赖链是：

```text
HTTP 去回执 ─需要─▶ 前端总是常驻订阅 ─需要─▶ 后端长订阅 ─需要─▶ 前端「一轮结束」重定义
                                                         └─同时重写 frame store─▶ Task 视角 snapshot
```

因此按「能独立验证」重新分期：

1. **后端内部调用方去回执**：Coordinator 改为投递 + `wait_idle` 后读节点最新 Run；`child_agent_create` / `child_agent_send`、`AgentTeamDecisionService` 投递即返回；删除流程改为订阅 actor 的退出事件。HTTP 命令此期仍保留回执，前端不动。
2. **对话链路整体切换**（前后端同一期）：后端长订阅、snapshot 改为扁平 `messages[]` + `activity`、HTTP 命令 202、actor 拒绝投影到顶层 `error`；前端常驻订阅、`onActivitySettled` 取代 `onFinish` 的业务职责、读 `activity`、状态集合收敛、pending 清理。此期结束后回执机制整体删除。
3. **fork 按消息切分**：后端 `_copy_fork_history` 按消息切分、请求改带 `messageId`；前端 fork 入口挂到消息上。独立于前两期，可任意时点进行。

## 待定

- 命令错误复用 snapshot 顶层 `error`，还是新增独立的 `command_error` 字段？顶层 `error` 后端从未写入、前端已有渲染，Run 错误在 `activity.error`；复用没有语义冲突，建议复用。
- `edit_queued` 是否也改走 snapshot 错误以统一形态？建议不改：它不经 actor，同步结果已在生产者手上。

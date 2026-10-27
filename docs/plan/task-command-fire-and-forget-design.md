# Task 命令改为纯生产消费（去回执）设计增补

承接 `task-actor-dispatch-design.md`（TaskActor 调度已实施）。本稿改三件相互依赖的事：

1. 生产者与 actor 之间去掉逐条命令的回执：生产者只投递，结果与错误一律经 snapshot 投影给前端；
2. 订阅改为跟随 Task 的长订阅，前端「一轮输出结束」改由 Run 状态跳变判定，与连接断开分开；
3. 对话界面去掉 Run 身份，只看 Task 的当前活动；fork 改为按消息切分。

消息模型、`dispatch_policy` 规则、actor 的串行循环与两阶段处理都不变。

状态：已实施（三期一次完成，见文末「实施记录」）。验收标准见 `task-command-fire-and-forget-acceptance.md`。

## 修订（2026-10-26）：用户命令改回同步返回

本节推翻下文「去掉逐条命令的回执」「actor 的处理结果只经 snapshot 表达」两条决策，其余（Task 长订阅、Task 视角 snapshot、按消息 fork）不变。

- **原因。** 信号队列已删除（见 `task-actor-dispatch-design.md` 修订），入口直接 await actor 的一轮，结果天然可得；拒绝原因是「这次请求」的结果而不是 Task 状态，经 snapshot 回传需要 `seq`、清除时机、重建保留等一整套配套规则。
- **契约。** 用户命令（发送、编辑、重跑、恢复、审批决定）处理完才返回：400 / 404 / 409，成功 200 `{}`；取消仍是纯投递（202）。
- **删除。** snapshot 顶层 `error`、`ConversationCommandError` 与 `seq`、`set_command_error`、actor 的 `_report_failure` / `COMMAND_FAILED`；前端的 `TransportCommandError`、`subscribeRejection`、按 `seq` 判定被拒与错误条目渲染。被拒的发送类命令由提交方按 HTTP 结果收回乐观消息、恢复草稿。
- **跨 Task 投递不变。** 子 Agent 结论、后台结果、审批决定消息、取消传播仍只发起、不等待。

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

## wire 契约（实施基准）

前后端都以本节为准；与上文表述不一致时以本节为准。

### snapshot

```text
ConversationStateSnapshot = {
  messages: Message[],            // 扁平，按 context 序号排列
  activity: Activity | null,      // 最新 Run 摘要；Task 尚无 Run 时为 null
  error: { code, message, seq } | null, // 只承载命令被拒，由 actor 写入与清空
  queued_messages?: QueuedMessage[]
}
Message = {
  id: string, role: "user" | "assistant", parts: Part[],
  runId: number,                  // 只供诊断、日志与 fork 定位，对话界面不按它比对
  outcome: Outcome | null         // 仅「该 Run 最后一条助手消息」在 Run 终态后非空；user 消息恒为 null
}
Outcome  = { status: "completed" | "failed" | "cancelled", endReason, error: {code,message} | null, usage: Usage | null,
             contextWindowTotal: number | null }   // 该 Run 冻结的窗口，与 usage 同源
Activity = { runId, status: "running" | "suspended" | "completed" | "failed" | "cancelled",
             endReason, error: {code,message} | null, usage: Usage | null,
             contextWindowTotal: number | null, launch: { modelConfigId, reasoningEffort, banTools } }
QueuedMessage = { id, role: "user", parts, delivery: "immediate" | "next_run" }
```

- 消息 id：用户消息为 inbox 行 id 的字符串；用户消息之后的助手回复为 `assistant-{用户消息 id}`；Run 内第一条用户消息之前的助手回复（唤醒、结果续跑等没有用户输入的 Run）为 `assistant-run-{runId}`。
- Part 结构不变（text / reasoning / image / file / tool-call）。
- 顶层 `error`：每次命令被拒都分配新的 `seq`（进程内单调递增，不持久化）并发帧，原因相同也发；前端按 `seq` 变化（从无到有，或 `seq` 不同）判定「又被拒了一次」。只有下一次成功处理的用户命令清空它；新开 Run、编辑重跑等强制重建 snapshot 时原样保留（命令错误不是 canonical 事实，冷重建恒为空）。后端重启后丢失，可接受。
- 上下文占用的「已用 / 上限」只能取自同一个 Run：当前活动有用量时取 `activity` 的一对；新一轮还没有用量时回落到最近一条带用量的 `outcome`，用它自己的 `contextWindowTotal`；那一对窗口未知时不显示，不跨 Run 拼。
- `outcome` 写入时机：`activity` 进入终态时，同步写到该 Run 最后一条助手消息；Run 被恢复（`cancelled` / `failed` → `running`）时清空为 `null`；已完成 Run 的结果更新（结束原因、用量）同步更新两处。旧 Run（不是 `activity.runId`）的迟到事件只更新它的 `outcome`，不碰 `activity`。

### 增量帧与 SSE envelope

- mutation 路径只允许：`("messages", k, …)`（`k == len` 的 `set` 表示追加）、`("activity",)` / `("activity", 字段)`、`("queued_messages",)`、`("error",)`。
- SSE 每条 `data:` 为 `{ task_id, kind: "full" | "mutation" | "resync_required", mutations: [{kind, path, value}], resync_reason, state? }`；`full` 帧带 `state`。不再有 `source_run_id`、`current_run_id`、`current_run_status`、`target_run_id`、`target_run_status`。

### 接口

| 接口 | 契约 |
|---|---|
| `POST /tasks/{id}/assistant/attach` | 长订阅：先推 full 帧（Task 无 Run 也推），之后持续推送，直到客户端断开、`resync_required` 或 Task 删除；不再 409 `NO_RUN` |
| `GET /tasks/{id}/assistant/state` | 返回上面的 snapshot |
| `POST /tasks/{id}/messages`、`POST /tasks/{id}/messages/{mid}/rerun`、`POST /tasks/{id}/resume`、`POST /tasks/{id}/decisions`、`POST /tasks/{id}/cancel` | 投递后 **202**，响应体 `{}`；投递前被拒的输入（附件不可用、模型不支持等）400、Task 不存在 404，错误体沿用 `{"detail": {"error": {code, message, retryable}}}`；wire 形状错误由 FastAPI 请求校验返回 422 |
| 例外：`POST /tasks/{id}/messages/{mid}/rerun` | 目标不是最新一条已投影的用户输入时同步 **409** `TASK_STATE_CONFLICT`：入口要读目标消息的展示绑定才能复用附件，这一步已在生产者手上，不再转交 actor。停止当前 Run 期间目标变旧（`EDIT_TARGET_STALE`）、Task 尚未停下（`RUN_NOT_STOPPED`）仍由 actor 写顶层 `error` |
| 例外：`POST /tasks/{id}/decisions` | 决定由 `AgentTeamDecisionService` 在入口确定性执行（批准即确认并后台运行 Team），TeamRun 归属不对或已不在待确认态时同步 **409**，TeamRun 不存在 404；执行完才以 `decision` 消息投递唤醒主 Agent |
| `PATCH /tasks/{id}/queued-messages/{mid}` | 不经 actor：成功 **200** `{}`；消息已并入 409 `MESSAGE_CONSUMED` |
| `POST /tasks/{id}/fork` | 请求 `{ "messageId": string }`（wire 消息 id），响应不变（新 Task） |
| `POST /tasks/{id}/tool-calls/{call_id}/cancel` | 不变 |

## 第二轮核对补充的改动点

### 后端

| 位置 | 现状 | 改为 |
|---|---|---|
| 旧 Run 的迟到事件（`run_event_projection.py`，测试 `test_late_previous_run_events_cannot_overwrite_current_run`、`test_historical_run_status_event_updates_its_own_run`） | 按 `runs[i]` 定位，只改自己那个 Run | 按事件的 `run_id` 路由：与 `activity.runId` 相同才改 `activity`；旧 Run 的状态 / 结果事件只更新它那条消息的 `outcome` |
| `RunInitializedEvent.replace_existing` 与 `plan_run_initialized` 的替换分支 | 无生产者（编辑重跑已改为新建 Run，Run 只前进） | 删除 |
| 帧元数据 `source_run_id` / `current_run_id` / `current_run_status`（`stream/transport_frame.py`、envelope、前端解析） | 前端据此推断当前 Run | 删除；当前活动只经 `activity.*` 增量表达 |
| `projection/snapshot_navigation.py`（`find_run`、`find_assistant_message`、`find_tool` 按 Run 下标） | 先找 Run 再找消息 | 在扁平 `messages[]` 中按 `runId` 与消息身份查找 |
| `conversation_task_state_rebuilder.py` | 按 Run 分组重建 | 按 context 序号输出扁平消息，终态 Run 的结局写到其最后一条助手消息 |
| 任务列表 API（`TaskResponse.current_run_id` / `execution_status`） | 侧边栏与 Workbench 状态 | 去掉 `current_run_id`（前端唯一读取处 `AgentRunSummary.runId` 是死字段）；保留 `execution_status` 与 Workbench 轮询：它的语义就是「Task 最新 Run 的状态」，与 `activity.status` 同口径，不是 Run 身份泄漏。后端 `TaskRecord.current_run_id` 是库里的当前 Run 指针，内部照用，只是不进 HTTP 响应 |

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

- actor 拒绝一个命令时写 snapshot 顶层 `error`：`code` 沿用现有稳定码 `RUN_NOT_RESUMABLE`、`RUN_NOT_STOPPED`；重跑第二阶段（当前 Run 停下之后）发现目标已不是最新用户消息时用新增稳定码 `EDIT_TARGET_STALE`；入口在投递前就能判定的过期目标仍同步 409（见「接口」表例外行）。`message` 为面向用户的短提示；同时记结构化日志。
- 每次拒绝带新的 `seq`（见「wire 契约」）：同样的原因连续被拒时值只差 `seq`，前端靠它判定被拒、收回乐观消息并复位按钮。
- 前端无需新增展示：顶层 `error` 已渲染为消息列表末尾的错误条目，只是后端此前从未写入。
- 清空时机：actor 下一次成功处理用户发起的命令（新建、唤醒、投递、恢复、重跑）时置空。Run 自身的失败在 `activity.error`，与顶层命令错误分开：顶层 `error` 只承载命令被拒；run 事件投影里对它的置空随之删除，清空只由 actor 负责。
- `edit_queued` 不经 actor（只是一次条件更新），`MESSAGE_CONSUMED` 仍同步 409：它是生产者直接对队列行的操作，结果就在手边，不需要绕 snapshot。

## 调用方变化

| 调用方 | 现状 | 改为 |
|---|---|---|
| HTTP 命令（发送 / 重跑 / 恢复 / 决定） | 等回执，返回 `CommandAccepted{state, queued}` | 投递后 202 `{}`；不返回 snapshot |
| fork | `POST /tasks/{id}/fork` 带 `runId` | 带 `messageId`，按消息切分（见上文） |
| `/cancel` | 有可停止对象时 200 `{task_id, cancelled: true}`，没有时 409 | 运行中仍在入口同步打信号；一律 202，是否停下看 snapshot |
| Coordinator 启动节点 | `submit` 返回后立即读 `current_run_id` | `deliver` 投递后订阅 `wait_new_run`，新 Run 一出现就记入 `conversation_run_ids`，再 `wait_idle` 等节点静止。不能等静止后再记：执行时间线（`agent_team_execution_timeline.py`）要显示运行中的节点访问，Team 取消（`AgentTeamRunState.cancel`）也按它判断是否给节点打信号 |
| `child_agent_create` / `child_agent_send` | 经 `run_coroutine_threadsafe(...).result()` 等回执；`child_agent_send` 捕获 `SessionConflictError`（已不可达） | 投递即返回；删除不可达的捕获，结论仍经 `agent_result` 回投 |
| `AgentTeamDecisionService` | `post_async` 等回执 | 投递即返回 |
| 删除 Task / 工作区 | `stop_tasks` 等 `Stop` 回执 | 发 `Stop`，订阅 actor 的退出事件（`actor.stopped`），等到后再删 |
| 后端关闭 | 取消回执 future | 无回执可取消，只取消执行与 actor |

## 删除清单

- `task_signals.py`：`Receipt`、各信号的 `receipt` 字段、`SessionReceipt`、`resolve` / `reject`；`SessionConflictError` 保留，供 `edit_queued`（同步 409 `MESSAGE_CONSUMED`）与 actor 内的状态冲突（写顶层 `error` 的稳定码）共用。
- `workflow_session.py`：`_request` / `_receipt`、`wait_run_settled`、`post_async` 与 `post_nowait` 的区分（统一为投递）。
- `task_actor.py`：`_stop_waiters`、对回执的兑现；`_flush` 只负责合并推进。`TaskActor.abandon` 保留但改了用途：进程关闭时丢弃未处理信号并置位 `stopped`，放行删除流程等等待方，不再兑现回执。
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
- **Coordinator 记录节点 Run**：按「投递后出现的新 Run」记录；节点 Task 只由 Coordinator 顺序投递、投递时必然静止，所以这个新 Run 就是本次节点 Run。投递已处理而没有新 Run 时 `wait_new_run` 抛 `RuntimeError`，由 Coordinator 收敛为 TeamRun `failed`。

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

## 实施记录

- **第一期（已完成）**：
  - `WorkflowSession` 新增 `deliver`（解析后发信号即返回，解析错误仍同步抛出）与 `wait_new_run`（订阅投递后出现的新 Run；投递已处理而无新 Run 时抛 `RuntimeError`）；`TaskActor.send` 让旧的静止结论作废，投递方紧接着订阅不会读到投递前的状态。
  - Coordinator 改为 `deliver` + `wait_new_run` 记节点 Run + `wait_idle`；停止节点改为 `call_soon_threadsafe(cancel_nowait)`，不等回执。
  - `child_agent_create` / `child_agent_send` 改为 `deliver`，删除 `child_agent_send` 里不可达的 `SessionConflictError` 捕获。
  - `AgentTeamDecisionService` 的 Team 结果改为 `post_nowait`；`/decisions` 自身仍走回执（HTTP 命令，第二期处理）。
  - 删除流程：`stop_tasks` 发不带回执的 `Stop`，等 `actor.stopped`；进程关闭时 `abandon` 也置位，等待方不会挂起。
  - 实施中发现：`TaskRuntimeSpace` 构造时读取 Task，Task 不存在或已删除时取闸门即抛 `KeyError`，原实现会杀死 actor 循环、留下死 actor、让等待方永久挂起（带回执时同样存在）。现在 actor 把它作为终止条件：拒绝全部待处理信号、注销自身并置位 `stopped`。
- **第二、三期（一次完成）——后端**：
  - 改名：`WorkflowSession` → `TaskActors`（`service/conversation_run/task_actors.py`，`workflow_session.py` 删除），依赖入口 `get_task_actors`。入口：`submit` / `post` / `broadcast` / `rerun` / `resume` / `cancel` 投递即返回；`edit_queued` 仍同步（不经 actor）；`stop_tasks`、`wait_idle`、`wait_new_run` 订阅 actor 状态。信号（`task_signals.py`）不再带回执，`Execution.settled` 删除。
  - HTTP：`assistant_api.py` 的发送、重跑、恢复、审批决定、取消投递即 202、响应体 `{}`；恢复与取消投递前确认 Task 存在（不存在 404）；`assistant_transport/response/`（`CommandAccepted`）整包删除。重跑入口先取最新 Run 再比对目标消息：Task 不存在时是 404，而不是被误判为「目标不是最新消息」的 409（实施中发现，已修）。
  - 命令错误：actor 处理用户命令失败时经 `ConversationTaskStateService.set_command_error` 写顶层 `error`（`SessionConflictError` 带稳定码，其余用 `COMMAND_FAILED` 安全文案；Task 已不存在时不投影），同值不发帧；用户命令成功处理（发送入队、恢复、重跑第二阶段）后清空。
  - snapshot：`conversation_run_snapshot.py` 删除，新增 `conversation_state_activity.py`（`activity` 与 `launch`）、`message_identity.py`（消息 id 编码与 `turn_of` 反解，投影、冷重建与 fork 共用）。投影改为扁平 `messages`：`ensure_assistant` 在没有用户输入的 Run 第一次输出时建 `assistant-run-{runId}`；只有当前活动且未终态的 Run 能继续流式输出与工具预览（`active_run_id`）；旧 Run 的迟到状态事件只更新它已有的 `outcome`。`RunInitializedEvent.replace_existing` 删除。
  - 订阅：`TransportStreamService.stream` 按 Task 长订阅，帧去掉 `source_run_id` / `current_run_id` / `current_run_status`；attach 不再返回 `NO_RUN`。
  - fork：请求 `{ messageId }`，非法 id 422 `FORK_MESSAGE_ID_INVALID`，找不到 404；`TaskService._copy_fork_history` 经 `_resolve_fork_cut` 把消息换算成「所在 Run + 这一轮最后一条 context 的序号」，`clone_for_fork(max_sequence=…)` 截断。「一轮」= 用户消息及其回复，到同一 Run 的下一条用户输入之前；用户消息与它的回复从哪条分叉结果相同。`extra.fork` 改为 `{source_task_id, source_message_id}`。
  - 测试：调度侧测试去掉回执断言，改为等 actor 的 `state_changed` 后断言事实或 snapshot；新增 `test_task_command_delivery.py`（B1 / B3）、`test_task_subscription_and_command_error.py`（长订阅与命令错误）以及 fork 按消息切分用例。后端全量 0 失败。
  - 说明：wire 形状错误（缺字段、空 parts）仍由 FastAPI 请求校验在进入路由前返回 422，验收 B1 的「400」指投递前被拒的输入（附件不可用、模型不支持等）。
- **第二、三期（一次完成）——前端**：
  - 契约：`contract.ts` / `snapshot-validation.ts` 改为 `messages` / `activity` / `error` / `queued_messages`，状态只剩后端五个值；回复的已取消 / 失败 / 用量读消息自己的 `outcome`。
  - 订阅与命令：新增 `lib/assistant/task-transport-session.ts`（脱离 React 的 Task 长订阅 + 命令投递），hook 只装配，对外为 `onActivitySettled` / `onCommandRejected` / `onDisconnect`；`task-commands.ts` 命令只返回 `void`。「一轮结束」由 frame store 在 `activity` 从活动态落到终态时触发一次；一个 full 帧里旧 Run 已结束、新 Run 已开始时先补 `settled` 再 `started`。
  - 恢复：新增 `components/assistant/runtime/subscription-recovery.ts`（有限重连、背压重同步，连接稳定 3 秒后才清零重连预算，避免「接受即关闭」变成无延迟死循环）；取消改为 Task 级标记；删除 `assistant-attach-controller.ts`、`use-cancellation-confirmation.ts` 与全部按 runId 的比对逻辑。
  - 乐观消息：202 不带消息 id，所以按数量对账——snapshot 里每新出现一条用户消息（排队或已并入），就去掉最早的一条乐观命令；顶层 `error` 出现新的拒绝（按 `seq` 判定）时全部去掉，被拒命令交回恢复草稿。
  - fork：`forkTask(taskId, messageId)`，入口挂在带 `outcome` 的消息上；`extra.fork.source_message_id`。
  - E2E 模拟后端（`test-server.mjs`）改为 Task 级长订阅、命令 202。
  - 遗留待定：无（原先的两项见下方「第二轮修复」）。
- **验收后修复**：
  - 命令错误加 `seq`：后端 `ConversationCommandError`（`conversation_state_error.py`）由 `set_command_error` 每次拒绝分配新 `seq` 并发帧；前端 `TransportCommandError` 与 `isNewCommandError`（`contract.ts`），frame store 按 `seq` 判定被拒。修复了「连续两次同样的拒绝」与「已有错误时又被另一原因拒绝」两种情况下乐观消息一直挂着、`isRunning` 不复位的问题。
  - 拒绝通知不再要求有乐观命令：恢复、取消等命令不产生乐观消息，frame store 照样通知；「继续运行」按钮按 `seq` 认出这次恢复被拒后立即复位（日志 `composer_resume_rejected`），不再等 15 秒超时。
  - 强制重建 snapshot（`rebuild_state`）把旧副本里的命令错误带入新副本：已完成 Task 收到后台结果新开 Run 时，不会把未处理的命令错误清掉。
  - 重跑入口的同步 409 与 `/decisions` 的同步校验写进「接口」表的例外行。
- **第二轮修复**（按第零铁律比较后选结构化 / 减法）：
  - 上下文占用同源：`outcome` 增加 `contextWindowTotal`（Run 终态投影取当前 `activity` 的窗口，旧 Run 迟到事件保留原值，冷重建取 `run.context_window_total`）；前端 `latestMeasuredContextUsage` 的「已用 / 上限」成对取自同一个 Run，不再拿新窗口配旧用量。补丁式「比较 `launch.modelConfigId` 再决定是否回落」只修换模型一种情况，没有采用。
  - `TaskResponse` 去掉 `current_run_id`；前端 `WorkspaceTask` 去掉该字段，Workbench 的 `AgentRunSummary` 收窄为 `AgentTaskStatus = { status }`。为状态推送新建 workspace 级订阅不值得（单用户本机，轮询成本低），保留轮询。
- **前端审查后收敛**：
  - 订阅归 runtime 所有：`useTaskAssistantTransportRuntime` / `ReadonlyTransportRuntime` 接收 `connectionKey`（后端不可用为 null，换实例即变化），挂载即打开、变化即重开、卸载即关闭，并直接返回 `{ runtime, open }`。删除 `onAttachReady` 回调、`TransportSubscriptionBridge` 与两处 `setTimeout(0)` 时序补丁。
  - 后端恢复不再由 recovery 重开订阅（runtime 已按新实例打开），`SubscriptionRecovery.restored` 只清零预算并提示；`resync` 只服务「重新同步」按钮。
  - Workbench 阅读页复用 `SubscriptionRecovery` 与 `TransportStatus`：只在首屏读一次 snapshot，断流重连只重开订阅，已显示内容与 runtime 保留；删除 `use-agent-reader.ts` 里重复的重试计数与延迟表。


# Task actor 调度设计

承接 `task-mailbox-suspend-wake-design.md`：消息模型、`delivery` / `project_to_transport`、挂起与唤醒规则（第 7 节规则表）、`RunMailbox` 与 `prepare_input` 的消费确认**全部不变**。本设计只改「谁执行推进规则、怎么串行化」，并把编辑拆成两个命令。

状态：已实施（第一步 TaskInbox + TaskActor 与等待方改造；第二步编辑拆成两个命令）。

## 修订（2026-10-26）：去掉信号队列

本节优先于下文中关于信号、批处理与 `RunFinished` 的描述。

- **没有信号队列。** `task_signals.py` 与 `asyncio.Queue` 删除；入口直接调用 `TaskActor` 的方法，每次调用是一轮，轮与轮由 Task 闸门串行。消息事实在外部队列，取消意图在 `cancellation_registry`，信号原本只是「醒来看看」，每轮重读事实源即可替代。
- **执行结束不再是信号。** Run 结束后由执行收尾那一轮注销登记、取走取消信号并接续调度。
- **用户命令同步返回。** `submit` / `rerun` / `resume` await 这一轮，拒绝抛 `SessionConflictError`（API 映射 409）；`submit` 返回新建或唤醒的 Run，Coordinator 据此记下节点 Run，`wait_new_run` 删除。
- **编辑重跑不再记待办。** 运行中先打 `edited` 取消，在闸门外等当前执行收尾（有超时），再拿闸门完成移出与重跑；等待期间 `_rerun_pending` 阻止推进，排队消息随重跑出的新 Run 并入。
- **跨 Task 调用只发起不等待。** `post` / `cancel` / `broadcast` 另起任务交给对方 actor；持自己的闸门去等对方的闸门会让父取消子、子回投父互等死锁。
- **用户命令编排归 Transport。** 排队消息编辑、重跑前的目标校验与输入解析、存在性检查移到 `TransportAssistantService`；`TaskActors` 只保留多类投递方共用的入口与登记表。

## 目标

`WorkflowSession` 现在同时承担入队、Task 闸门、执行判定结果、执行登记、收尾交接、取消传播与 `wait_idle`。复杂度集中在两处：

1. 多个入口（发送、系统消息、编辑、取消、执行收尾）并发争抢同一 Task 闸门；
2. 执行「Run 已落定、执行仍在收尾」的窗口。为此存在 `_wait_finishing_execution`、`wait_released`、`_after_run` 在闸门内再 `_advance`（防丢唤醒）、`RUN_NOT_STOPPED` 冲突分支。

目标：每个 Task 只有一个调度消费者，所有改变 Task 运行状态的动作在它内部串行执行，上述机制随之删除；判定规则仍是 `dispatch_policy` 纯函数。

## 已确认的决策

- **投递与决策分离**：生产者只负责「消息落库 + 发信号」，不判断 Run 状态。
- **决策者是 Task 级 actor，不是 workflow**：`ReactLikeWorkflow` 是单个 Run 的执行体，Run 之间不存在，无法决定「新建 / 唤醒」。workflow 仍只在 `prepare_input` 消费 mailbox，不感知 actor。
- **actor 按需拉起、阻塞队列挂起**：首次收到信号时创建；没有信号时挂在 `await signals.get()` 上，不占 CPU；先不做空闲退出。
- **回执保持同步**：信号携带 future，actor 处理完该信号后兑现，HTTP 回执语义与前端不变。
- **编辑拆成两个命令**：编辑排队消息与编辑重跑语义不同，分开寻址；已并入消息仍按「移出 + 作为新消息入队」处理，不原地改写 context。

## 结构

```text
生产者（用户命令 / 子 Agent / Team 节点 / 系统消息 / 配置广播）
  │  TaskInbox：写 task_messages（事实源）→ notify(task_id, signal)
  ▼
TaskActor（每个 Task 一个，单消费者）
  │  signals: asyncio.Queue[Signal]   ← 只传「醒来看看」与回执 future，不传消息正文
  │  读最新 Run + pending → dispatch_policy.next_action → 执行判定
  ▼
Run 执行（actor 的子 asyncio task）：RunDriver.drive → ReactLikeWorkflow → RunMailbox → prepare_input
  └─ 结束时 RunDriver 调用 on_finished，由 actor 向同一信号队列放入 RunFinished
```

两层队列各管一件事：

| 队列 | 持久化 | 内容 | 丢失后果 |
|---|---|---|---|
| `task_messages`（`TaskMessageQueue`） | 是，事实源 | 消息正文、`delivery`、发起配置 | 不允许丢 |
| `TaskActor.signals` | 否 | 唤醒原因、`by_user`、回执 future、编辑 / 取消参数 | 无害：actor 醒来总是重读事实源 |

## 信号

| 信号 | 来源 | actor 处理 |
|---|---|---|
| `Arrived(by_user, receipt)` | `submit`、`post_async`、`post` | 推进：`next_action` → 投递 / 唤醒 / 新建 / 不动 |
| `RunFinished(run_id)` | Run 子 task 的 done callback | 收尾（原 `_after_run`）后推进 |
| `Cancel(reason, receipt)` | `cancel`、`stop_tasks` | 见「取消」 |
| `Rerun(message_id, body, receipt)` | 编辑重跑命令 | 见「编辑」 |
| `Resume(receipt)` | `/resume` | `cancelled / failed → running` 并启动 |
| `Stop` | Task 删除、后端关闭 | 取消子 task，兑现剩余 future（异常），退出循环 |

`cancel_tool_call` 不经 actor：它只读当前执行的 run_id 并标记工具级信号，是对执行的旁路控制。

## 循环

```python
async def _loop(self) -> None:
    while True:
        signals = [await self._signals.get()]
        while not self._signals.empty():          # 一次取光，合并成一次判定
            signals.append(self._signals.get_nowait())
        if not await self._handle(signals):       # Stop 时返回 False
            return
```

- **批量合并**：一批 `Arrived` 只做一次 `next_action`，`by_user` 取这批的 any；判定完统一兑现这批 future。
- **唯一等待点**：Run 结束不靠 `await RunDriver.drive()`，而是 `RunFinished` 信号，所以 actor 在 Run 运行期间仍能处理投递与取消。
- **处理函数只做短 IO，不 await Run**：需要「等 Run 停下」的动作（取消、编辑重跑）拆成两阶段，第二阶段由 `RunFinished` 触发。否则一个慢处理会堵住其后的取消。
- **异常边界**：单批处理异常记录日志、以异常兑现该批 future，循环继续；不让一次失败杀死 actor。Run 失败的唯一边界仍是 `RunDriver.drive`。

## 推进与收尾

推进（原 `_advance`）在 actor 内执行，规则表不变。`StartRun` 仍「先登记执行再发布快照」，快照发布失败只记录。

收尾（原 `_after_run`），由 `RunFinished` 触发，执行登记在此注销并取走 Task 取消信号（信号的唯一清理点不变）：

| Run 落定状态 | 处理 |
|---|---|
| `suspended` | 有迟到取消 → `CANCEL` 并传播；否则推进（同批里已到的可唤醒消息直接唤醒，防丢唤醒不再需要特判） |
| `completed` | 有取消信号 → 不交接；否则推进；随后向父 Task 回投结论 |
| `cancelled` | 传播；`reason == user` 时入队「用户已取消」通知；向父回投（`parent` 除外） |
| `failed` | 向父回投 |

向父 Task 回投 = 对父 Task 的 inbox 发 `Arrived`。只是入队 + 发信号，不嵌套任何闸门，原「闸门外回投」的约束自然消失。

## 取消

- **运行中**：入口同步调用 `cancellation_registry.request_cancel(task_id, reason)`（不排队，立即生效，同现 `_signal_execution`），再发 `Cancel` 信号兑现回执。Run 在检查点落定后经 `RunFinished` 收尾。
- **挂起中**：没有执行者，`Cancel` 信号由 actor 直接 `CANCEL` 迁移并传播。
- **判定是否有执行**：只有 actor 读写执行登记，入口侧的同步打信号只需读「登记是否存在」，事件循环单线程下与注销互斥。

## 编辑

拆成两个命令，均按消息寻址：

| 命令 | 目标 | 处理 |
|---|---|---|
| 编辑排队消息 `PATCH /tasks/{id}/queued-messages/{message_id}` | 仍在 `task_messages` 的 `user_input` | 条件更新该行（身份、`delivery`、发起配置不变）→ 刷新排队投影；行已被确认删除时 409 `MESSAGE_CONSUMED`，**不会**转成重跑 |
| 编辑重跑 `POST /tasks/{id}/messages/{message_id}/rerun` | 最新一条已投影的 `user_input` | 发 `Rerun` 信号，actor 两阶段处理 |

`Rerun` 两阶段：

1. 有执行 → 以 `edited` 打取消信号，actor 记 `pending_rerun`，本批结束；
2. 无执行（立即或在 `RunFinished` 收尾后）→ 同一事务内 `exclude_from` + 新消息入队 → 推进（`by_user=True`），按本次请求的配置 `StartRun`，兑现回执。

修复的现存问题：当前 `edit` 先查「是否仍在队列」，不在则走已并入分支；排队消息恰在编辑前被并入且正是最新用户输入时，请求会静默变成「停止当前 Run 并重跑」。

不原地改 `ConversationTaskContextModel` 的理由（已讨论确认）：其后的回复与工具条目仍须移出；原地改写丢原文（观测页需要）；改后的消息归旧 Run、回答归新 Run，违背「Run 只前进」；已在 context 的消息没有可投递的内容，需另造「无输入启动 Run」的入口与配置来源。

## 与 Task 闸门的关系

`TaskRuntimeSpace.lock` 不只服务调度：Task 删除、fork、临时 Task 清理在工作线程里用同一把锁与 Run 创建互斥。因此：

- actor 在**会改变 Run 状态的短临界区**（新建、唤醒、恢复、挂起态取消、`Rerun` 第二阶段）内仍取该锁，保证与删除互斥；
- 调度入口之间不再争锁（只有 actor 一个取锁方），锁只剩「调度 vs 删除」这一种竞争；
- 删除流程：先对子树各 Task 发 `Stop`（替代 `stop_tasks` 的取消 + `wait_released`），等 actor 退出后再取锁删除。

## 生命周期

- **创建**：`TaskInbox.notify` 时若无 actor 则创建并登记；跨线程生产者（配置广播、工具线程）经 `loop.call_soon_threadsafe` 转到事件循环。
- **常驻**：actor 数量上限为本进程碰过的 Task 数，单用户桌面可忽略；不做空闲退出。以后需要时用 `wait_for(signals.get(), idle)`，超时后「检查队列为空」与「从 registry 注销」之间不得有 await。
- **回收**：Task 删除发 `Stop`；lifespan 关闭时对全部 actor 发 `Stop` 并等待（先打取消信号，线程 / 子进程工具只认信号）。`close_sync`（依赖重配置）跨线程投递 `Stop`。
- **重启**：actor 与信号队列不跨重启；`recover_orphaned_runs` 规则不变，重启后首个信号按需拉起 actor 并读事实源。

## 等待方

| 等待方 | 现状 | 改为 |
|---|---|---|
| Coordinator 等节点结论 `wait_idle(task_id, s)` | 闸门内判定 + 释放信号 | actor 维护「静止」事件（无执行者且最新 Run 终态），按 Task 订阅；语义不变 |
| SSE 跨 Run 订阅 `wait_released(task_id, run_id)` | 等执行 task 结束 | 等该 Run 的 `RunFinished` 收尾完成（actor 发出的「Run 已交接」事件） |
| 删除 `stop_tasks` | 取消 + `wait_released` | `Stop` + 等 actor 退出 |

## 不变量

1. 同一 Task 至多一个执行者；新建、唤醒、恢复只在该 Task 的 actor 内发生。
2. `task_messages` 是事实源；信号可丢可重，不影响正确性。
3. actor 的处理函数不等待 Run；Run 结束只经 `RunFinished` 进入 actor。
4. 取消对运行中执行立即生效，不排在其他信号之后。
5. 原设计的不变量 1–5、7 不变；原不变量 6、8（闸门内新建、闸门内复查防丢唤醒）由本设计的 1、3 取代。

## 删除清单

- `WorkflowSession._locked`、`_gate`（调度侧用法）、`_wait_finishing_execution`、`wait_released`、`_release_signals`；
- `_after_run` 里「闸门内释放再推进」与「闸门外回投」的特殊顺序；
- `_edit_consumed` 的 `cancel` + `wait_released` + `RUN_NOT_STOPPED` 冲突分支；`edit` 的排队 / 已并入自动分流；
- `submit` 里的 `is_active` 判断与「先排队投影再投递」的顺序约束（改为 actor 推进前统一刷新排队投影）。

## 影响范围

| 区域 | 变化 |
|---|---|
| `service/conversation_run/workflow_session.py` | 拆为 `task_inbox.py`（入队 + notify，对外入口）与 `task_actor.py`（循环、推进、收尾、执行登记）；`WorkflowSession` 退化为 inbox 门面或直接改名 |
| `service/conversation_run/dispatch_policy.py` | 不变 |
| `service/conversation_run/message_edit.py` | 不变，被 `Rerun` 第二阶段复用 |
| `assistant_transport/assistant_api.py`、`task_command_request.py` | 编辑拆为两个端点与请求模型 |
| `assistant_transport/service/transport_stream_service.py` | `wait_released` 改为等 Run 交接事件 |
| `agent_team/coordinator.py` | `submit` 不变；`wait_idle` 改订阅静止事件 |
| `core/tools/tool_handler/child_task/child_agent_create.py`、`child_agent_send.py` | `submit` 调用不变 |
| `service/agent_team/agent_team_decision_service.py`、`task_runtime/broadcaster/runtime_notice_broadcaster.py` | `post_async` / `post` 调用不变 |
| `api/tasks_api.py`、`api/workspaces_api.py`、`task_runtime/service/task_service.py` | 删除前 `stop_tasks` 改为 `Stop` + 等 actor 退出 |
| `lifespan.py`、`service/depends.py` | 关闭与 `close_sync` 改为对 actor 发 `Stop` |
| 前端 `lib/assistant/task-commands.ts` 与编辑入口 | 按消息是否排队调用不同命令 |
| 测试 | `test_workflow_session_execution`、`test_waiting_run_cancellation`、`test_child_agent_send_release_window`、`test_task2_*`、`test_tool_call_cancellation`、`test_task_input_queue`、`test_assistant_transport_api` 等按 actor 语义重写；新增「信号批量合并」「运行中取消不被慢处理阻塞」「Rerun 两阶段」「删除与 actor 退出」测试 |
| `AGENTS.md` | 「调度规则只在 `WorkflowSession`」「Task 闸门」「编辑」相关段落随实施更新 |

绿地项目：不保留旧入口与兼容层。

## 风险

- **回执 future 悬挂**：actor 异常退出或被 `Stop` 时必须以异常兑现所有未决 future，否则 HTTP 请求挂起。
- **同步锁在事件循环上**：actor 取 `TaskRuntimeSpace.lock` 仍需经 `async_operation`（线程获取），不能在事件循环线程直接阻塞获取。
- **两阶段 Rerun 期间的新输入**：`pending_rerun` 期间到达的 `Arrived` 先入队不推进，待 `Rerun` 第二阶段一并推进；需测试顺序（编辑后的消息在其前还是其后）并在文档中固定。
- **改动面**：`workflow_session.py` 基本重写，相关测试大面积调整；建议先落 actor 与等待方，再拆编辑命令，两步各自可测。

## 实施记录

- **第一步（已完成）**：
  - 新增 `task_actor.py`（`TaskActor`：信号循环、推进、收尾、取消、恢复、编辑重跑两阶段、执行登记）与 `task_signals.py`（信号、`SessionReceipt`、`SessionConflictError`）；`WorkflowSession` 保留类名，退化为 inbox 门面（解析输入、发信号、等回执），对外签名不变。
  - 系统消息的 `admit` 过滤、入队与排队投影都在 actor 内完成：投递方不读 Run 状态。排队消息原地编辑不经 actor（只是一次条件更新）。
  - 等待方：SSE 跨 Run 订阅改为 `wait_run_settled(task_id, run_id)`（等该 Run 的 `RunFinished` 收尾含交接完成）；`wait_idle` 读 actor 每批处理后重算的静止状态；`stop_tasks` 发 `Stop` 并等 actor 停下退出。
  - actor 等 Task 闸门不设超时（不能丢 `RunFinished`），`_accept` 的 `TASK_BUSY` 映射随之删除；编辑重跑第二阶段在 actor 内复核「最新 Run 已终态、目标仍是最新用户输入」，取代原 `RUN_NOT_STOPPED` 的闸门外重查；并发的第二次编辑重跑仍以 `RUN_NOT_STOPPED` 拒绝。
  - 编辑仍是单一端点，按「是否仍在队列」分流；拆分为两个命令留到第二步。
- **第二步（已完成）**：
  - 后端：`/messages/{message_id}/edit` 拆为 `PATCH /tasks/{id}/queued-messages/{message_id}`（`EditQueuedMessageRequest`，只带 `message`，不接受配置字段）与 `POST /tasks/{id}/messages/{message_id}/rerun`（`RerunMessageRequest`）；`WorkflowSession.edit` 拆为 `edit_queued` 与 `rerun`，不再按「是否仍在队列」自动分流。
  - `edit_queued` 命中已并入的消息时直接 409 `MESSAGE_CONSUMED`，修复了「排队消息恰在编辑前被并入、又恰是最新用户输入时，请求静默变成停止当前 Run 并重跑」的问题。
  - 前端：`lib/assistant/task-commands.ts` 提供 `editQueuedTaskMessage`（PATCH，只带正文）与 `rerunTaskMessage`；`use-runtime-transport.ts` 按 snapshot 的 `queued_messages` 判定命令，排队编辑不读模型选择、不要求已选模型。

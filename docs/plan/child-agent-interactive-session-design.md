# Child Agent 交互式 Session 技术方案

> 状态：待实现技术方案
>
> 目标：主 Agent 委派 Child Agent 后立即继续推理，并可在同一个父 Run 内点名 Child Agent 发送消息、读取状态和关闭 Child Agent；父 Run 结束或应用重启时统一收敛遗留 Child Agent。
>
> 范围：后端运行时、Task/Run/checkpoint 事实边界、工具契约、生命周期恢复、Assistant Transport 展示和 Workbench UI。
>
> 非目标：本方案不引入云端队列、Redis、Postgres、多租户认证或独立 Child Agent 服务进程；不把 Child Agent 设计成可脱离父 Run 永久后台运行的任务。

## 1. 设计结论

采用“Child Agent Session”模型，复用现有交互式 Terminal Session 的生命周期思想：

```text
父 Run LangGraph checkpoint
  └─ Child Agent session metadata

ChildAgentSessionService（当前 backend 进程）
  └─ runtime registry / asyncio task / mailbox / closing fence

Task + ConversationRun（主业务库）
  └─ Child Agent 的持久化身份、父子关系和生命周期事实
```

核心决定：

1. `delegate_task` 改成“创建并异步启动 Child Agent”，不再阻塞等待 Child Agent 完成。
2. 新增 `child_agent_send`、`child_agent_status`、`child_agent_wait`、`child_agent_close` 四个模型侧工具。
3. Child Agent 的真实生命周期唯一由 `ConversationRun.status` 表达；checkpoint 不演化第二套 Run 状态机。
4. 删除 `DelegationModel`、`DelegationRecord`、`DelegationCrud` 和 `DelegationService`。
5. 父子关系使用已有的 `Task.parent_task_id + Task.parent_run_id`；Child Run 通过 `ConversationRun.task_id` 归属 Child Task。
6. checkpoint 只保存 Child Agent session 的可序列化定位信息，用于父工具调用、Transport 冷读和启动恢复。
7. Child Agent 运行中收到的消息进入进程内 mailbox，只能在 Child workflow 的安全边界被消费，禁止外部直接修改 `RuntimeContextManager`。
8. 父 Run 完成、失败、取消或执行器关闭时，统一关闭该父 Run 遗留的 Child Agent；Child Agent 不跨父 Run 继续后台运行。
9. 应用重启时，先按现有规则收敛遗留 active Run，再扫描每个主 Task 最近一次 Run 的 Child Agent checkpoint，补齐 Child Agent session 的关闭投影。

这套方案保留现有 `delegate_task`、`delegation_ref` 和 Workbench Agent Tab 的展示命名，避免为了删除数据库模型而扩大前端协议重命名范围；“delegation”在这些位置只是工具/展示术语，不再代表独立持久化实体。

## 2. 当前代码事实与改造原因

### 2.1 当前委派是同步阻塞模型

- `apps/backend/app/core/delegation/delegation_executor.py` 在 `delegate_task` 内创建 delegation、Child Task、Child Run，然后直接调用 `ChildAgentRunner.run_child()`。
- `apps/backend/app/core/delegation/child_agent_runner.py` 通过 `asyncio.run()` 驱动 Child Agent，并等待其执行结束。
- 因此父 workflow 只有在 Child Agent 完成后才能进入下一轮模型调用，无法在同一父 Run 中再次点名 Child Agent。
- 当前 `DelegationModel` 同时承担并发额度、父子 locator、取消级联、启动恢复和 Transport 冷读重建，形成独立于 Run 的第二套委派生命周期。

### 2.2 现有运行时已经提供可复用的生命周期边界

- `ConversationRunExecutor` 已经维护进程内 Run 执行注册表、取消信号和 Run finally 收尾。
- `TerminalSessionService` 已经将 checkpoint 元数据与进程内 worker registry 分离，并提供 `begin_run()`、`close_run_terminals()`、`shutdown()` 和启动恢复路径。
- `ReactGraphState.terminal_sessions` 已经证明 Run 级 checkpoint 可以保存资源 locator，而不保存进程句柄或实时 IO 状态。
- `Assistant Transport attach` 和现有 Workbench 子 Agent 只读标签页已经提供 Child Run 的只读展示基础。

### 2.3 不能直接把消息写进 Child context

`RuntimeContextManager` 是 Task 级 working copy owner，负责 context sequence、内存 entries、持久化和 listener。父工具线程直接向 Child context append 消息会绕过 Child workflow 的上下文边界，带来 sequence 竞争、模型正在调用时上下文突变和 checkpoint/canonical context 不一致。

因此发送消息必须是 Child Agent runtime 的 mailbox 操作，由 Child workflow 在安全边界消费。

## 3. 事实所有权与数据模型

### 3.1 Task/Run 事实

Child Task 使用已有字段：

```text
task_type       = "delegate_task"
parent_task_id  = parent Task id
parent_run_id   = parent ConversationRun id
workspace_id    = parent workspace id
title           = delegate_task.title
```

Child Run 使用已有字段：

```text
task_id        = Child Task id
agent_id       = child profile id
input_text     = delegate_task.prompt
status         = pending/running/completed/failed/cancelled
final_output   = Child Agent 最终输出
end_reason     = 稳定终止原因
```

`TaskModel.delegation_id`、对 `delegations` 表的外键和唯一索引删除。Child Task 的唯一身份就是 Task 主键；Child Run 的唯一执行身份就是 Run 主键。

### 3.2 父 Run checkpoint

在 `ReactGraphState` 增加：

```python
child_agents: dict[str, dict[str, Any]]
```

建议以父工具调用 id 作为稳定 key：

```json
{
  "child_agents": {
    "tool-call-abc": {
      "child_task_id": 101,
      "child_run_id": 202,
      "agent_id": "delegate_reviewer",
      "title": "审查代码",
      "status": "running"
    }
  }
}
```

checkpoint 允许保存的字段：

- `child_task_id`
- `child_run_id`
- `agent_id`
- `title`
- `status` 的最近一次展示投影
- `end_reason` 的稳定短码

checkpoint 禁止保存：

- asyncio Task、Future、event loop、mailbox 对象；
- 模型 client、HTTP session、subscriber、线程和进程句柄；
- 未脱敏 prompt、完整模型输出或工具原始内容；
- 任何替代 `ConversationRun.status` 的生命周期事实。

checkpoint 中的 `status` 是 session 展示投影，不是 Run 状态源。读取 Child Agent 当前状态时必须回查 Child Run。

### 3.3 进程内 Child Agent Session

新增 `ChildAgentSessionService`，持有：

```text
ChildAgentSessionRuntime
  session_key       = (parent_run_id, tool_call_id)
  parent_task_id
  parent_run_id
  child_task_id
  active_run_id
  generation
  mailbox
  closing flag
  queued message ids
  child execution future
```

这些数据只存在当前 backend 进程。应用重启后不恢复旧 asyncio task，不重放旧 mailbox，不隐式恢复 Child Agent。

checkpoint 中的 `child_run_id` 表示最近一次已投影的 Child Run；进程内 runtime 一律使用 `active_run_id` 表示当前实际执行的 Run。Child Run 完成并创建 follow-up 后，先更新 runtime 的 `active_run_id`，再在下一个父 checkpoint 投影边界更新 `child_run_id`，避免多轮 follow-up 混淆历史 Run 与当前 Run。

## 4. Child Agent 生命周期

### 4.1 创建和启动

`delegate_task` 执行流程：

1. 解析 child profile，并执行现有深度、Agent 白名单和有效工具收敛策略。
2. 在 `ChildAgentSessionService` 的 registry lock 内预留父 Run 的并发额度。
3. 创建 Child Task 和首个 pending Child Run。
4. 将 `child_task_id/child_run_id` 写入父 workflow 的 `child_agents` checkpoint 投影。
5. 发布现有 `delegation_ref` Transport 事件，使父 tool part 立即获得 Child locator。
6. 通过父运行时 event loop 调度 Child Agent，不使用 `asyncio.run()`。
7. 立即返回 `started` observation，父 Agent 进入下一轮模型调用。

Child 启动失败时必须：

- 将 Child Run 收敛为 `failed` 或 `cancelled`；
- 释放 session 并发额度；
- 返回受控工具错误；
- 不遗留只有 Task 没有可执行 Run 的“幽灵 Child”。

#### 同步 Tool Handler 到异步 Child Runtime 的精确契约

当前 Tool Handler 仍然是同步接口，且 `WorkflowOperations` 已经把同步工具移到线程中执行。因此第一版不把 `delegate_task` 改成 async handler，而是在现有 `ToolExecutionContext.runtime_dependencies.runtime_event_loop` 上提交一个短生命周期启动协程：

```text
ToolHandlerRunner worker thread
  -> ChildAgentSessionService.start_child(...)
       1. registry lock 预留 slot
       2. 同步创建 Child Task + pending Child Run
       3. asyncio.run_coroutine_threadsafe(launch_child, runtime_event_loop)
       4. 仅等待“Child Run 已 claim + executor.start 已登记”的启动结果
       5. 返回 child_task_id / child_run_id

runtime_event_loop
  -> launch_child
       1. claim_pending_run(child_run_id)
       2. ConversationRunExecutor.start(child_run_id, child runner callback)
       3. 把 asyncio.Task 和 generation 写入 registry
       4. 不 await child execution
```

`start_child()` 的同步等待只等待固定的 `CHILD_START_TIMEOUT_SECONDS`，不等待 Child Agent 完成。启动协程异常、超时或 `executor.start()` 拒绝时，必须按顺序执行：设置 session closing fence、条件取消 Child Run、释放 reservation、记录结构化错误，然后返回 `child_agent_start_failed`。超时后迟到的 launch callback 必须用 generation/fence 检查拒绝，不得重新把 Child Run 拉回 running。

launch acknowledgement 的固定返回结构为：

```python
ChildAgentStartResult(
    child_task_id: int,
    child_run_id: int,
    generation: str,
    reservation_acquired: bool,
)
```

其中 `reservation_acquired` 只表示当前 session 已占用并发额度，不表示 Child Run 已完成；Child Run 的实际状态仍由 `ConversationRun.status` 读取。

`ChildAgentRunner` 拆成两个明确入口：

- `claim_and_register_child()`：只负责认领 pending Run 并向 `ConversationRunExecutor` 登记异步执行；
- `run_child_workflow()`：只负责执行已登记的 workflow，并把异常转换为 Run 的 canonical failed/cancelled 收口。

禁止在 Tool Handler 或 `ChildAgentRunner` 内调用嵌套 `asyncio.run()`；Child Agent 必须复用父 backend 的 event loop 和 `ConversationRunExecutor`。

### 4.2 发送消息

`child_agent_send(child_task_id, message, message_id)` 的校验：

1. Child Task 必须属于当前父 Task。
2. Child Task 的 `parent_run_id` 必须等于当前父 Run。
3. Child Agent session 不得处于 closing/closed。
4. `message_id` 在同一 session 内相同 payload 必须幂等，不同 payload 必须拒绝。

消息投递规则：

```text
Child Run 正在模型/工具调用
  -> message 进入 mailbox
  -> Child workflow 下一个安全边界消费

Child Run 已完成
  -> 在同一个 Child Task 上创建新的 ConversationRun
  -> 新 Run 使用该 message 作为输入

Child Run 已取消且 session 已关闭
  -> 返回 child_session_closed
```

安全边界定义为 Child workflow 准备下一次 model node 输入之前。Child 正在执行长时间 terminal/tool 时，不强行修改当前模型上下文；关闭操作则通过现有 Run cancellation 和 terminal cleanup 让其尽快退出。

Mailbox 的可验证契约：

- session 具有单调递增 `generation`；创建新 active Run、关闭和恢复投影都会更新 generation。
- 每条消息带 `message_id` 和 session 内递增的 `message_seq`；返回 `accepted_seq`，消息按 `message_seq` 顺序消费。
- mailbox 使用有界队列 `MAX_PENDING_CHILD_MESSAGES`；满时返回 `child_agent_busy_or_queue_full`，不得无限积压。
- `send` 先在 registry lock 内校验 `closing=False`、generation 未过期和 owner 关系，再入队；关闭 fence 设置后，任何迟到 send 都拒绝。
- `message_id` 相同且 payload 相同返回已有 `accepted_seq`；相同 id 的 payload 不同返回 `child_agent_message_conflict`。
- 当前 active Child Run 同时只能有一个；follow-up Run 的创建必须经过 Child Task 的 operation lock 和 active Run 条件校验，不能由两个完成 callback 并发创建。
- Child workflow 在 model node 前一次性按 `message_seq` 顺序 drain 当前 mailbox，将本次 drain 得到的每条消息作为独立 `HumanMessage` 追加到 Child context，然后只发起一次下一次模型调用；因此同一安全边界前到达的多条消息属于一个 FIFO batch，但不会合并成一条文本。
- 如果 Child Run 已经进入终态，completion callback 只能在 session 未关闭时按 FIFO 启动一个 follow-up Run；后续消息继续留在 mailbox，直到前一个 follow-up 终态。

工具参数中的 `child_task_id` 只用于定位，父 `task_id`、父 `run_id` 和 workspace 必须从 `ToolExecutionContext` 推导，不能由模型传入或信任模型自行提供。

### 4.3 等待 Child Agent 消息

`child_agent_wait` 是父 Agent 主动等待 Child Run 终态消息的阻塞式模型工具，语义参考交互式 Terminal 的 wait 操作，但不读取流式 token。Child Run 每次进入终态都视为一条可等待消息；`completed` 消息的正文唯一来自该 Child Run 已持久化的 `ConversationRun.final_output`，`failed/cancelled` 消息只返回状态和 `end_reason`。

工具参数契约：

```python
child_agent_wait(
    targets: list[ChildAgentWaitTarget] | None = None,
    wait_mode: Literal["any", "all"] = "any",
    timeout_seconds: float = 30,
)

ChildAgentWaitTarget(
    child_task_id: int,
    after_run_id: int | None = None,
)
```

参数和目标集合规则：

- `targets` 传一个元素表示等待指定 Child Agent；传多个元素表示等待指定集合；传 `None` 表示在调用开始时快照当前父 Run 的全部直接 Child Agent。目标集合固定在本次调用开始时，新创建的 Child Agent 不加入本次等待，避免 `all` 永久扩张。
- `wait_mode="any"` 在任一目标产生新的终态消息时立即返回；`wait_mode="all"` 要求所有目标都产生新的终态消息后返回。
- `after_run_id` 是调用方持有的 at-least-once 读取游标，不是服务端持久化消费记录，也不承诺 exactly-once。它必须属于同一个 `child_task_id`、当前父 Run 和当前 Child session；不按 Run id 数值大小判断“之后”，而是按同一 Child Task 的 `(created_at, id)` 稳定顺序查询其后的最早终态 Run。省略时从该 session 最早的终态 Run 开始读取；没有终态才等待 active Run。
- 同一 Child Task 已有多个终态 follow-up 时，wait 永远返回游标之后最早的一条，不跳过中间 Run；调用方将返回的 `child_run_id` 作为下一次 `after_run_id`。调用方崩溃或丢失游标时允许重复读取，这是 at-least-once 的明确代价。
- `targets=None` 且当前父 Run 没有直接 Child Agent 时返回稳定错误 `child_agent_no_targets`，不进入无期限等待。
- `targets=[]`、同一 `child_task_id` 重复出现、`after_run_id` 不属于该 Child Task/session，分别返回 `child_agent_no_targets`、`child_agent_duplicate_target`、`child_agent_invalid_cursor`。
- 目标必须经过与 `child_agent_send/status/close` 相同的父 Task、父 Run、workspace 所有权校验；只允许等待直接 Child Agent，不允许借此读取其它 Task 或 workspace 的 Run。

返回结构固定为：

```python
ChildAgentWaitResult(
    timed_out: bool,
    messages: list[ChildAgentTerminalMessage],
    pending: list[ChildAgentPendingState],
    interrupted_by: Literal[None, "parent_cancelled", "session_closed", "shutdown"],
)

ChildAgentTerminalMessage(
    child_task_id: int,
    child_run_id: int,
    status: Literal["completed", "failed", "cancelled"],
    final_output: str | None,
    end_reason: str | None,
)
```

`wait_mode="any"` 只返回一个按 `(created_at, id, child_task_id)` 最早满足条件的消息；其它目标保留在 `pending`。`wait_mode="all"` 对每个目标返回一条游标之后最早的消息，结果按 `targets` 的输入顺序排列。`timed_out=True` 时允许 `messages` 带有已经完成的部分目标，同时在 `pending` 中列出未满足等待条件的目标；超时不是 Child Run 的取消，也不改变任何 Run 状态。返回的 `child_run_id` 由父 Agent 作为下一次调用对应 target 的 `after_run_id`，从而能继续读取同一 Child Task 后续 follow-up Run 的新消息。工具不维护服务端消费游标，因此只能保证调用方正确传递游标时不重复读取，不能承诺 exactly-once。

等待实现必须复用 Child session registry 的条件通知，而不是在 Tool Handler 线程中忙轮询：

```text
ConversationRunStateService（唯一 canonical finalization hook owner）
  1. 在业务事务中写入 ConversationRun.status/final_output/end_reason
  2. 提交事务成功后调用显式注入的 RunFinalizationObserver
       a. ChildAgentSessionService.notify_waiters(parent_run_id)
       b. 非阻塞投递 completion callback（不得持有 wait condition 锁）
  3. wait condition 按 target cursor + any/all 谓词唤醒等待线程
  4. tool 从 Run service 重新读取 canonical 记录并组装结果
  5. callback 在独立的 Child Task operation lock 中检查 fence/generation，决定是否创建 follow-up
```

`ConversationRunStateService` 是运行过程中所有 `completed/failed/cancelled` 状态写入的唯一终态入口；正常 workflow、Child 失败、用户取消和 executor 兜底收敛都必须通过同一个 `RunFinalizationObserver`。该 observer 是必需的显式 Protocol 依赖，不由 Transport SSE/UI 事件触发，也禁止使用 `getattr(..., None)` 静默探测。启动 recovery 是独立的 post-commit recovery hook：它在 Child session registry 初始化前批量收敛遗留 Run，只做状态更新和恢复投影，不调用 live-run observer、不触发 follow-up，也不消费历史 `final_output`。waiter 的通知失败只记录结构化日志，不回滚已提交 Run；等待线程仍可通过下一次 canonical 状态检查读到结果。

`child_agent_wait` 是第一个显式 async-capable model tool：复用现有 ToolDefinition、ToolAccessGate、ToolObservation 和 ToolExecutionContext，但通过正式的 `AsyncToolHandler` Protocol 在 workflow event loop 上 `await ChildAgentSessionService.wait_async()`，不进入 `asyncio.to_thread`，也不阻塞 backend event loop。ToolDefinition 增加必填语义字段 `handler_kind: Literal["sync", "async"]`；注册期校验 async handler 的 Protocol 和 `parallel_mode="serial"`，不允许运行期猜测或 `getattr` 探测。等待 condition 同时监听 Child 终态、父 Run cancellation、Child session closing 和 backend shutdown；所有中断都返回带 `interrupted_by` 的结构化部分结果，不转换为工具错误，且不继续阻塞到完整 timeout。`ChildAgentSessionService.close()`、`cancel_descendants()` 和 `shutdown()` 都必须唤醒 async waiters。

等待工具不创建 Run、不启动 follow-up、不修改 Child `RuntimeContextManager`，也不把 Child 输出自动追加到父 context；`final_output` 只作为本次工具结果提供给父模型。session service 仍设置显式 `MAX_CONCURRENT_CHILD_WAITS` waiter slot，slot 在 async handler 的 `try/finally` 中获取和释放；超限返回稳定工具错误 `child_agent_wait_concurrency_exceeded`。因为等待不占用同步工具线程池，父 Agent 等待 Child、Child 又等待 Grandchild 不会形成线程池饥饿，仍必须覆盖嵌套等待测试。

Async tool 管线的具体复用和分流契约：

```text
WorkflowOperations.run_tool_calls
  ├─ sync ToolDefinition
  │    └─ 现有 asyncio.to_thread(_execute_tool_call)
  └─ async ToolDefinition（当前只有 child_agent_wait）
       └─ await ToolExecutor.execute_async(call, execution_context, ...)

ToolExecutor.execute_async
  -> 复用 ToolAccessGate / 参数校验 / PreToolUse Hook
  -> await AsyncToolHandler(...)
  -> 复用 ToolObservation budget / PostToolUse Hook / terminal projection
```

`ToolExecutor.execute()` 遇到 `handler_kind="async"` 必须返回明确的装配错误，不能把 coroutine 当作普通结果；`ToolExecutor.execute_async()` 遇到 sync tool 也必须拒绝。两条入口共享同一套门禁、参数、Hook、观察结果和取消投影，只有 handler 调度方式不同。`WorkflowOperations` 在同一 model tool batch 中保持现有串行/并行语义：async wait 强制 serial，普通 sync tool 仍按现有 `to_thread` 或临时并行池执行；父 Run 取消时由 async execution path 收口工具观察，`wait_async()` 的 `finally` 必须释放 waiter slot 和 signal registration。

`AsyncChildAgentWaitCoordinator` 负责跨线程/跨 event loop 边界：

- `wait_async()` 在唯一 backend event loop 上注册按 `parent_run_id` 的 async waiter，并先做一次 canonical 查询。
- `ConversationRunStateService` 的 finalization observer、session close、parent cancel 和 shutdown 可能来自其它线程；它们只能调用 `loop.call_soon_threadsafe(signal.bump)`，不能直接操作 asyncio condition。
- signal 使用单调 `version` + 每个 waiter 独立的 `asyncio.Event`；waiter 必须先注册自己的 Event 并记录 `version_before`，再通过独立的 `run_read_executor` 执行短的同步 SQLite canonical 查询，禁止在 event loop 直接调用同步 Run/CRUD service。
- 查询循环固定为：读取 predicate；若满足则返回；在 event-loop lock 下比较 `signal.version` 与 `version_before`，版本变化则立即重新查询；版本未变化才 await 自己的 Event。通知只 set 已注册 waiter 自己的 Event，不清理共享 Event；waiter 唤醒后只清理自己的 Event 并更新 `version_before`。因此通知早于查询、查询期间发生通知、多个 waiter 同时等待同一事件都不会 lost wakeup。`close/shutdown` 在 signal 中标记中断原因并唤醒全部 waiter。
- `run_read_executor` 只承载有界的 Run 历史/状态读取，不承载等待阻塞；backend shutdown 先标记 shutdown、唤醒 waiter、关闭该 executor，再关闭其它 service dependencies。

completion callback 必须在 Run 记录提交并通知 wait condition 之后再决定是否创建 queued message 的 follow-up Run，且仍受 session generation/closing fence 和 Child Task operation lock 约束。observer 在 session lock 内先登记 `(session_key, finalized_run_id, generation)` 的 `follow_up_pending`，再非阻塞调度 callback；callback 成功创建 follow-up 后才清除 pending。调度失败或 callback 异常保留 pending，由 event-loop 上的 bounded retry/sweep 重新投递；session close、shutdown 或 recovery fence 设置后清除并丢弃 pending，不得启动新 Run。callback 不读取“当前 active Run”来替代 Run 历史查询；已完成的旧 Run 即使已经创建了新的 active follow-up，仍按 `(created_at, id)` 保持可等待。callback 不持有 wait condition 锁执行数据库写入。

### 4.4 状态读取

`child_agent_status(child_task_id)` 读取：

- Child Task 归属；
- 当前/最近 Child Run；
- `ConversationRun.status`；
- `final_output`、`end_reason` 的安全投影；
- mailbox 队列长度和 session closing 状态。

不能用 checkpoint 或前端 runtime state 推断 Child Run 是否完成。

### 4.5 关闭 Child Agent

`child_agent_close(child_task_id)` 必须幂等：

1. 设置 closing fence，阻止迟到的 send 和新的 follow-up Run。
2. 标记 Child Run 的 cancellation signal。
3. 递归处理未来允许的 Child 后代。
4. 复用 `ConversationRunExecutor` 和 `TerminalSessionService` 的取消/终端清理逻辑。
5. 等待有限时间；Run 状态由 canonical writer 收敛为 `cancelled`。
6. 更新父 checkpoint 的 session 投影为 `closed`。

关闭标签页只影响前端 Workbench，不调用此工具；只有明确的 Agent 工具调用或用户显式 Stop 操作才会关闭后端 Child Agent。

关闭与启动竞态采用同一个 session generation：关闭先在 registry lock 内设置 `closing=True` 并移除可接受的 mailbox 写入，再标记当前 Child Run cancellation signal；如果启动协程尚未完成，启动协程看到旧 generation 后只能收敛 Run，不能登记新的 execution。

## 5. 父 Run 收尾与应用恢复

### 5.1 父 Run 收尾

在 `ConversationRunExecutor._execute()` 的 finally 中统一调用：

```text
ChildAgentSessionService.close_children(parent_run_id)
TerminalSessionService.close_run_terminals(parent_run_id)
_converge_unfinished_run(parent_run_id)
```

必须覆盖正常完成、异常、取消、工具失败、executor 关闭和 workflow 启动失败。

Child session 关闭需要设置 closing fence 后再发取消信号，避免 parent 收尾与 child send/创建 follow-up 产生竞态。关闭等待必须有上限，超时只记录 warning；下次启动由 recovery sweep 继续收敛。

父 Run 的取消级联不再读取 `DelegationService`。`ConversationRunExecutor.cancel()` 调用 `ChildAgentSessionService.cancel_descendants(parent_run_id)`；该 service 先从 registry 获取进程内 session，再通过 `TaskService.list_child_tasks(parent_task_id, parent_run_id)` 查询持久化 child 关系，最后对每个 active Child Run 标记 cancellation signal。遍历使用 queue + visited，防止异常父子数据形成环。

### 5.2 应用崩溃/重启

启动顺序：

```text
initialize_service_dependencies
  -> recover_orphaned_runs(runtime_restarted)
  -> list_latest_runs()
  -> recover_orphaned_child_sessions(latest parent runs)
  -> recover_orphaned_terminal_checkpoints()
  -> initialize hooks/tools/runtime
```

`recover_orphaned_runs()` 是 Run 状态收敛的唯一入口，先将所有遗留 `pending/running` Run 置为 `cancelled`，不得隐式重放。

启动期这次批量更新不走运行期 `RunFinalizationObserver`：当时旧 session registry 和 waiters 尚未装配，直接在恢复事务提交后调用 `ChildAgentRecoveryHook` 完成 locator 投影与日志记录。该 hook 明确禁止创建 follow-up、唤醒不存在的旧 waiter 或消费历史 `final_output`。

Child recovery 以“每个主 Task 最近一次 Run”为扫描边界：

- 读取该 Run 的 `child_agents` checkpoint；
- 根据 child locator 回查 Task/Run；
- 对仍非终态的 Child Run 执行条件取消；
- 将 checkpoint 中仍为 `starting/running` 的 session 投影为 `closed`；
- 通过 `parent_run_id` 查询做 checkpoint 缺失时的安全兜底；
- 记录 `orphaned_child_agents_recovered` 结构化日志。

恢复路径不得创建新的 Child Run，不得重放 mailbox，不得恢复旧 Agent 执行。

### 5.3 后端优雅关闭

`ConversationRunExecutor.close()` 是 Child Agent execution 的唯一进程级关闭 owner；lifespan 不直接重复关闭 Child registry。关闭顺序调整为：

1. 触发 `SESSION_END`。
2. `ConversationRunExecutor.close()` 调用 `ChildAgentSessionService.shutdown(reason="backend_shutdown")`，为所有 session 设置 closing fence、取消 Child Run、有限等待收敛。
3. `ConversationRunExecutor.close()` 取消并等待自身登记的 parent/child execution task；每个 `_execute()` finally 仍做一次幂等 child/terminal cleanup。
4. 关闭 `TerminalSessionService`，作为未被 Run finally 覆盖的 worker 兜底。
5. flush observability，关闭 service dependencies。

Child session cleanup 失败只记日志，不阻断其它资源关闭，但必须保留 child run 的后续恢复能力。`ChildAgentSessionService.shutdown()` 的等待上限固定且可测试；超时不会阻塞 backend 关闭，也不会把 timed-out Child Run 伪装成 completed，残余 active Run 由下一次 `recover_orphaned_runs()` 收敛。

### 5.4 Task 子树删除边界

Task 删除仍由 `TaskService` 编排，不由 session service 直接删除数据库行：

1. 在 workspace/task operation lock 内收集根 Task 的完整子树，包含 `task_type='delegate_task'` 的 Child Task。
2. 在主库事务提交前调用 `ChildAgentSessionService.close_for_task_ids()`，设置 closing fence 并停止当前进程资源；该调用失败只记录日志，但不能跳过即将删除的 Task ids。
3. 在同一主库事务内先解除 `tasks.parent_run_id` 等外键环，再删除 context、commands、runs 和 tasks。
4. 主库事务提交后，按已有 checkpoint GC 规则回收不再被任何 Run 引用的 checkpoint thread；checkpoint GC 失败不回滚已提交的业务删除。
5. 如果事务回滚，session registry 中已关闭的 Child 不能自动重建；调用方只记录 `task_delete_child_cleanup_before_rollback`，后续由用户重新委派。数据库事实不会出现“已删除但 session 仍可运行”的状态。

该流程复用现有 `TaskService` 的任务树收集、外键环解除和提交后 checkpoint GC，不新增 delegation 专用删除事务。

## 6. 并发、幂等和错误契约

### 6.1 并发额度

不再通过 `DelegationModel` 做额度计数。`ChildAgentSessionService` 在当前进程内以锁保护 session reservation；创建 Child Task/Run 前完成额度判断，创建失败时释放 reservation。

由于项目是单用户、单 backend 进程，跨进程分布式锁不是本方案目标。启动恢复先收敛遗留 active Run，再允许新 session 创建。

如果未来需要“应用崩溃后保证消息不丢”的 at-least-once mailbox，则应新增明确的 command/outbox 持久化事实，不得把 checkpoint 当作并发消息队列。

并发额度与数据库关系查询的具体 owner：

| 原 `DelegationModel` 职责 | 新 owner | 具体规则 |
|---|---|---|
| active delegation 计数 | `ChildAgentSessionService` | registry lock + reservation；只统计 `pending/running` Child session |
| 父 Run 查询子 Agent | `TaskService.list_child_tasks()` | 查询 `parent_task_id + parent_run_id + task_type='delegate_task'` |
| 子 Agent 当前状态 | `ConversationRunStateService` | 只读 `ConversationRun.status/final_output/end_reason` |
| 取消级联 | `ChildAgentSessionService.cancel_descendants()` | session registry 与 Task/Run 查询合并，BFS + visited |
| Task 删除清理 | `TaskService` | 先调用 child session cleanup，再按 `parent_task_id` 收集子树并删除 Run/context/Task |
| Transport 冷读 locator | `ConversationTaskStateService` | 通过窄 `ChildAgentCheckpointReader` 读取 parent checkpoint，再投影到 snapshot |
| tool-call 到 child locator 映射 | parent `ReactGraphState.child_agents` | key 为 `(parent_run_id, tool_call_id)`，不新增持久化 delegation 表 |

数据库只新增/保留关系查询索引，不新增 Child Agent 表：`tasks(parent_task_id, parent_run_id, task_type)`；`conversation_runs(task_id, status)` 用于 active child 查询。所有状态更新仍由 Run service 的条件更新完成。

### 6.2 工具调用幂等

- `delegate_task` 使用父 `(run_id, tool_call_id)` 作为 session reservation key。
- `child_agent_send` 使用 `(child_task_id, message_id)` 幂等。
- 相同 key、不同 payload 必须拒绝。
- `child_agent_close` 是状态幂等操作。

`delegate_task` 的 reservation key 在 session service 内必须在“创建 Child Task/Run + 登记 launch”期间保持；如果父工具调用因线程取消而失去返回值，迟到 launch 仍通过 reservation/fence 收敛，不能产生第二个 Child Task。

### 6.3 工具错误

稳定错误码至少包括：

```text
child_agent_not_found
child_agent_not_owned
child_agent_session_closed
child_agent_busy_or_queue_full
child_agent_message_conflict
child_agent_no_targets
child_agent_duplicate_target
child_agent_invalid_cursor
child_agent_wait_concurrency_exceeded
child_agent_start_failed
child_agent_concurrency_exceeded
```

模型诊断文本、UI `status_hint` 和 Run lifecycle status 三通道继续分离，不向前端泄漏异常堆栈、原始 prompt 或 provider 响应。

## 7. Assistant Transport 与 UI 方案

### 7.1 复用现有 Workbench Agent Tab

父消息流中的 `delegate_task` 保持紧凑 tool activity row：

```text
[Agent 图标] 审查代码       运行中      打开
```

点击后打开现有 Workbench Agent Tab，而不是把 Child Agent 的完整消息嵌入父消息流。Tab 使用：

- `child_task_id` 作为稳定 locator；
- 现有 `/tasks/{task_id}/assistant/state` 获取 canonical snapshot；
- 现有 attach SSE 订阅 Child Run；
- active-only SSE 和切回时重同步策略；
- 未知/失败状态走既有通用 fallback。

### 7.2 参考交互式 Terminal UI

参考 `TerminalPanel` 的结构，但不复制终端的 xterm/output cursor 逻辑：

```text
Workbench Agent Tab
┌──────────────────────────────────────┐
│ ← 审查代码   ● 运行中     [停止] [×] │
├──────────────────────────────────────┤
│ Child Agent 的只读消息与工具活动流   │
│                                      │
├──────────────────────────────────────┤
│ 当前状态：等待模型 / 执行工具 / 完成 │
└──────────────────────────────────────┘
```

UI 规则：

- Tab 关闭只解除订阅，不关闭 Child Agent。
- `[停止]` 是明确的用户动作，先通过 Child session close fence 再调用现有 `POST /runs/{child_run_id}/cancel`，不由 SSE 断开触发；显式 Stop 不允许 queued message 再创建 follow-up。
- Child Agent 的 send message 第一版只由主 Agent 工具完成，Workbench 不新增用户输入框，避免把“用户直接操作 Child Agent”和“主 Agent 控制 Child Agent”混成一个权限边界。
- 运行中显示状态，完成后显示 `final_output` 摘要；失败显示受控 `status_hint`。
- 不为 Child Agent 新建左侧 Task 树节点；继续使用 Workspace Workbench 临时 Tab。
- 不维持所有 Child Tab 的完整 SSE；只有当前激活 Tab 保持流，其他 Tab 切回时 attach + snapshot 重同步。

Child session 与 terminal 的 owner 映射固定为 Child Run：

```text
ChildAgentSession.child_run_id
  == ToolExecutionContext.run_id（Child Agent 执行 terminal 工具时）
  == TerminalSessionRecord.run_id
```

因此：

- `child_agent_close` 调用 `close_run_terminals(child_run_id, reason="child_agent_closed")`；
- 父 Run 收尾先关闭 Child session，再由每个 Child Run 的 finally 幂等关闭其 terminal；
- Workbench Stop 先设置 Child session closing fence，再调用现有 `POST /runs/{child_run_id}/cancel`；Run executor 会关闭该 Child Run 的 terminal，Child session finalization observer 只投影 `closed/cancelled`，不得启动 queued follow-up；
- Workbench Tab 关闭和 SSE 断开只移除 subscriber，不调用 cancel，也不触碰 Child session registry。

### 7.3 Transport 事件

复用现有 `delegation_ref` 事件的定位字段：

```text
child_task_id
child_run_id
title
agent_role
```

Child Run 自身的状态通过既有 Run status event 和 Child Task snapshot 展示，不新增第二套 Child Agent 状态机。必要时增加一个受控的 `child_agent_session` display kind，但不得按工具名在前端新增专用状态分支；仍由 `display_data.kind + presentation.expand_layout` 路由。

## 8. 代码改造边界

### 新增

- `apps/backend/app/service/child_agent/child_agent_session_service.py`：进程内 session registry、额度 reservation、mailbox、close/recovery。
- `apps/backend/app/service/child_agent/child_agent_run_finalization.py`：实现显式 `RunFinalizationObserver`，在 canonical Run 终态提交后通知 waiters 并非阻塞调度 completion callback。
- `apps/backend/app/service/child_agent/child_agent_recovery.py`：实现启动期 post-commit recovery hook，只收敛遗留 Child Run 和 checkpoint projection，不恢复 runtime、不触发 completion callback。
- `apps/backend/app/core/tools/tool_handler/child_agent_send.py`：消息投递工具。
- `apps/backend/app/core/tools/tool_handler/child_agent_status.py`：状态读取工具。
- `apps/backend/app/core/tools/tool_handler/child_agent_wait.py`：等待一个或多个 Child Agent 终态消息的工具。
- `apps/backend/app/core/tools/tool_handler/child_agent_close.py`：显式关闭工具。
- `apps/backend/app/core/tools/tool_models/child_agent_session_args.py`：四个工具的参数契约。
- `apps/backend/app/core/tools/schemas/tool_definition.py`：增加显式 `handler_kind` 契约，注册期校验 async handler 与串行调度约束。
- `apps/backend/app/core/tools/tool_execute/tool_executor.py`：增加 `execute_async()`，与同步入口共享门禁、Hook、预算、观察和终态投影。
- `apps/backend/app/core/workflows/workflow_operations.py`：按 `handler_kind` 将 async tool 留在 event loop、sync tool 保持现有线程/进程执行路径。
- `apps/backend/app/service/child_agent/async_child_agent_wait_coordinator.py`：维护 async waiter signal、跨线程 `call_soon_threadsafe` 唤醒、独立短查询 executor 和 shutdown 顺序。
- 对应的 display projector、单元测试和生命周期集成测试。

### 修改

- `apps/backend/app/core/delegation/delegation_executor.py`：改为创建 session 并异步调度，不再等待 child。
- `apps/backend/app/core/delegation/child_agent_runner.py`：拆分“启动 child”和“等待 child 完成”，移除嵌套 `asyncio.run()`。
- `apps/backend/app/core/workflows/react/state.py`：增加 `child_agents` checkpoint allowlist。
- `apps/backend/app/core/workflows/react/workflow.py`：在 model 安全边界消费 mailbox，并在 checkpoint 中投影 session metadata。
- `apps/backend/app/assistant_transport/service/conversation_run_executor.py`：父 Run finally 关闭 child，取消级联改为按 Task/Run 关系遍历。
- `apps/backend/app/service/task/conversation_run_state_service.py`：所有终态条件更新成功后调用显式 `RunFinalizationObserver`，统一覆盖正常完成、失败、取消和 executor 兜底收敛。
- `apps/backend/app/service/task/conversation_run_service.py`：保留 `recover_orphaned_runs()` 的启动期批量收敛边界，并在提交后交给 `ChildAgentRecoveryHook` 做 Child locator 投影，不调用 live observer。
- `apps/backend/app/lifespan.py`：增加 Child Agent startup recovery 和 shutdown cleanup。
- `apps/backend/app/storage/model/task_model.py`、Task CRUD/Service：删除 `delegation_id` 及其外键/唯一索引，保留 `parent_task_id/parent_run_id`。
- `apps/backend/app/storage/init_schema.py`：删除 `DelegationModel` 注册，绿地项目允许直接移除旧 `delegations` 表。
- `apps/backend/app/assistant_transport/service/conversation_task_state_service.py` 和 rebuilder：从 Child Task/Run + parent checkpoint/transport locator 冷读恢复 Child locator。
- `apps/backend/app/core/tools/tool_registry.py`：注册期拒绝 async handler 的非法 Protocol、并行声明或缺失参数契约。
- `apps/desktop/components/workbench-agent-run-surface.tsx` 及相关 Workbench/assistant tool renderer：复用 terminal 的 surface 生命周期、状态栏和显式停止动作。

`ConversationRunExecutor` 的具体依赖方向固定为：executor 持有 `ChildAgentSessionService`；`ChildAgentSessionService` 依赖 Task/Run service 和 terminal service；`ConversationRunStateService` 只依赖窄的 `RunFinalizationObserver` Protocol；具体 `ChildAgentRunFinalizationCoordinator` 依赖 session service 和 Run reader，由 composition root 显式注入；Tool Handler 只通过 session service，不直接访问 CRUD；lifespan 只通过 executor 的 `close()` 触发进程级收尾。启动 recovery 使用独立的 `ChildAgentRecoveryHook`，在 session registry 初始化前完成 post-commit 收敛，禁止触发 live completion callback。禁止 executor、Tool Handler、Transport SSE 和 API 各自维护一份 child registry 或 completion callback。

### 明确不修改

- 不修改 Tauri IPC 和 backend supervisor 进程边界。
- 不新增独立 Child Agent 进程。
- 不新增 Child Agent 数据库表。
- 不让前端直接写 SQLite、checkpoint 或 Agent context。
- 不把 Child Agent 输出复制成父 context 的大量消息。

## 9. 实施顺序

### 阶段一：异步启动骨架

1. 增加 `ChildAgentSessionService` 和进程内 registry。
2. 将 `delegate_task` 改为创建 Child Task/Run 后立即调度。
3. 直接使用 Task/Run 作为唯一持久化 Child Agent 事实，确保异步启动、父 Run 继续和 Child Run 终态都可验证；不保留 `DelegationModel` 过渡适配。
4. 增加父 Run finally 的 child cleanup。

### 阶段二：交互工具与 checkpoint

1. 增加 `child_agent_send/status/wait/close`。
2. 实现安全边界 mailbox 消费。
3. 增加 `child_agents` checkpoint 投影和恢复。
4. 补齐工具幂等、所有权校验、队列上限和日志。

### 阶段三：清理旧 Delegation 残留并验证 schema

1. 将取消级联、并发统计、冷读恢复全部改为 Task/Run 查询。
2. 删除 delegation storage model/CRUD/service/value object。
3. 删除 Task 的 `delegation_id` 外键和唯一索引。
4. 直接重建本地 SQLite schema，删除旧 `delegations` 表。
5. 全仓搜索并删除旧 `DelegationModel`、`DelegationService`、`delegation_id` 和旧表名的残留引用，确认阶段一后不存在第二套委派生命周期。
6. 更新 Workbench/Transport 冷读逻辑和所有测试。

### 阶段四：UI 验收

1. 父 tool row 即时展示 Child locator。
2. Workbench Agent Tab active-only attach。
3. 复用 terminal 的 header/status/stop/close 交互。
4. 验证 Tab 关闭、Task 切换、SSE 断开不会关闭 Child Run。
5. 验证明确 Stop 才会取消 Child Run。

## 10. 验收标准

### 后端行为

- `delegate_task` 在 Child Agent 尚未完成时返回，父 Agent 能继续调用模型。
- 同一父 Run 能对两个 Child Agent 并行委派，并能通过 `child_task_id` 定点操作。
- Child Agent 运行中发送的消息不会直接改写当前 context，而是在下一个 model 安全边界消费。
- Child Agent 已完成后发送消息，会在同一 Child Task 下创建新的 Child Run。
- `child_agent_wait` 可以等待一个、指定多个或当前父 Run 的全部直接 Child Agent；完成消息正文来自对应 Child Run 的 `final_output`。
- `child_agent_wait` 的 `any/all`、固定目标快照、超时部分返回、Run 历史顺序和 `after_run_id` at-least-once 游标语义稳定可测。
- Child Agent 关闭操作幂等，并能关闭其 terminal session。
- 父 Run 正常完成、失败、取消和 executor shutdown 都不会遗留 active Child Run。
- 应用重启后，最近一次主 Run 的遗留 Child Agent 全部为 `cancelled/closed`，不自动重放。
- Child Run 状态只来自 `ConversationRun.status`，checkpoint 只作为 session locator。
- 并发额度、工具调用和消息投递均有确定的幂等/冲突测试。
- 启动协程超时、迟到 callback、父 Run 取消与 Child completion 并发发生时，generation/fence 能阻止重复登记和 follow-up Run。

### Transport/UI 行为

- 父 tool part 在 Child 创建后立即显示 `child_task_id/child_run_id`。
- Workbench Tab 可以通过 snapshot + attach 查看 Child Agent。
- 关闭 Tab、切换 Task、SSE 断开都不会取消 Child Agent。
- 明确点击 Stop 才会调用 Child Run cancel。
- Child Agent 完成、失败、取消状态不会让父消息流崩溃；未知 display kind 走 fallback。
- UI 不展示原始异常、堆栈、凭据、原始 prompt 或大段模型正文。

### 必须落地的测试场景

- `delegate_task` 的同步 handler 只等待 launch acknowledgement，不等待 Child workflow；父 Run 在 Child 未结束时继续进入下一次 model node。
- launch acknowledgement 失败、超时和迟到 callback 都只产生一个 Child Task/Run，并最终落为明确终态。
- 不同 parent Run、不同 child_task_id 和不同 workspace 的 `child_agent_send/status/close/wait` 均被拒绝；合法 parent/child 关系才可操作。
- mailbox 消息按 `message_seq` FIFO 消费；重复 message id 幂等，payload 冲突拒绝，队列满返回稳定错误。
- Child Run 终态提交后才唤醒 `child_agent_wait`；`any/all`、超时、部分结果、Run 历史顺序、游标重复读取、无目标和非法 cursor 均有测试。
- `child_agent_wait` 读取的是 canonical `ConversationRun.final_output`，不会从 checkpoint、SSE 缓存或前端 runtime state 推断正文。
- canonical finalization observer 对正常完成、失败、用户取消、executor 兜底都能通知/唤醒；父取消、session close 和 shutdown 通过 session service 唤醒并返回结构化 `interrupted_by`，recovery 不触发 follow-up。
- `child_agent_wait` 作为显式 async-capable tool 不占用同步工具线程池；父 Run 等 Child、Child 等 Grandchild 的嵌套 wait 在 waiter slot 上限内可完成，超限返回稳定错误。
- async tool 管线会拒绝 sync/async handler 错配，且 `child_agent_wait` 仍经过 ToolAccessGate、参数校验、Hook、预算、观察和终态投影；同步 SQLite 查询不会在 event loop 直接执行。
- finalization observer、session close、parent cancel 和 shutdown 从其它线程唤醒 async waiter 时只经过 `loop.call_soon_threadsafe`；通知早于等待、多个 waiter 共享一次通知、取消、超时、异常和 shutdown 都有测试。
- active Child Run 与 follow-up Run 不能并发；Child completion 与 queued message 竞态只能创建一个 follow-up。
- close fence 设置后，迟到 send、迟到 launch 和 queued follow-up 都不会重新启动 Child。
- 父 Run 正常完成、workflow 异常、用户取消、executor shutdown 四条路径都关闭 Child 和 Child Run terminal。
- 启动 recovery 在 parent/child/terminal checkpoint 同时存在时只收敛，不创建、不重放、不丢失已完成历史。
- Workbench Tab close、SSE disconnect、Task 切换不触发 Child cancel；明确 Stop 才触发 Child Run cancel 和 terminal cleanup。

## 11. 风险与决策记录

### 风险一：父 Run 结束过快

异步 `delegate_task` 返回后，如果父 Agent 立即给出最终回答，父 Run 收尾会关闭尚未完成的 Child Agent。这是本方案的明确生命周期契约，不是异常；如果产品未来需要“父 Run 结束后 Child 继续运行”，必须另建独立后台任务生命周期，不能悄悄修改当前 session 语义。

### 风险二：运行中消息的持久化

第一版 mailbox 只保证当前 backend 进程内的有界投递；应用崩溃时父/子 active Run 都会被取消，未消费 mailbox 不重放。如果未来要求 at-least-once 消息投递，应设计独立 command/outbox 事实并定义消费确认，不能把 checkpoint 当作并发队列。

`child_agent_wait` 不依赖 mailbox 保存 Child 输出。Child Run 的 `final_output` 在 Run 终态提交时写入主业务库，等待通知只是进程内唤醒机制；应用崩溃后的父 Run 会按既有恢复规则取消，因此不会在重启后继续等待或隐式重放旧消息。

### 风险三：Transport 冷读定位

运行期 `delegation_ref` 事件不能单独承担刷新后的恢复。实现必须保证 Child locator 同时可由父 checkpoint 或 canonical context/snapshot 冷读恢复；否则只能在当前 SSE 连接仍存活时打开 Workbench，这不满足重连验收。

### 风险四：不要用静默可选钩子

Child cleanup、mailbox close、checkpoint projection 都是正式契约，装配期必须显式注入 `ChildAgentSessionService`。禁止用 `getattr(..., None)` 探测缺失方法后静默跳过；失败必须有明确异常分支和结构化日志。

## 12. 审查清单

- [ ] 是否仍存在第二套 Child Agent 生命周期状态机？
- [ ] `ConversationRun.status` 是否保持唯一状态源？
- [ ] checkpoint 是否只保存可序列化 locator，而没有句柄或完整内容？
- [ ] 父 Run 的所有退出路径是否都关闭 Child Agent？
- [ ] 应用重启是否不会隐式重放 Child Agent？
- [ ] `child_agent_send` 是否避免直接修改 Child `RuntimeContextManager`？
- [ ] Child Task/Run 的归属校验是否阻止跨父 Run 操作？
- [ ] UI 是否复用了现有 Assistant attach、Workbench 和 terminal 生命周期模式？
- [ ] SSE 断开、Tab 关闭和 Run cancel 是否保持语义分离？
- [ ] 删除 `DelegationModel` 后，冷读、取消级联、并发额度和删除任务是否都有替代路径？
- [ ] 关键生命周期是否都有结构化落盘日志和可验证测试？

## 13. 本轮审查整改记录

首次子 Agent 验收为“不通过”，指出异步启动、mailbox、DelegationModel 职责迁移、父 Run/重启顺序和 Terminal/Workbench owner 映射缺少可执行契约。本版已针对这些意见补充：

- 同步 Tool Handler 到父 event loop 的 launch acknowledgement 时序与超时收敛；
- mailbox generation、closing fence、FIFO、容量、幂等和 follow-up Run 串行规则；
- 原 DelegationModel 每项职责的新 owner、关系查询字段和索引；
- executor close owner、父 Run finally、启动 recovery 的精确顺序；
- Child Run 与 `TerminalSessionRecord.run_id`、Workbench Stop/Tab close/SSE disconnect 的明确映射；
- 可直接转化为单元、集成和 E2E 测试的验收场景。

复审结论：有条件通过，无 P0/P1 阻断问题。进入实现前必须以第 10 节测试场景验证 launch 超时/迟到 callback、completion 与 follow-up 竞态、父 Run 四类收尾路径和启动恢复；这些行为不能只依赖文档约定。

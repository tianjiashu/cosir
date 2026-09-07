# Task 并行运行与 Assistant Transport 重连改造方案

> 状态：设计方案
>
> 基准日期：2026-09-08
>
> 目标：Tauri 桌面端前端可以流畅切换 Task；不同 Task 的 Agent 可以并行运行；桌面端前端与本机 FastAPI 之间的 HTTP/SSE transport 断开时，不影响后端 Agent 继续执行。

本方案针对单用户本地桌面应用，不针对浏览器访问的公网 SaaS。React 前端运行在 Tauri 托管的 WebView 中；FastAPI 运行在桌面端 supervisor 管理的本机进程中。这里的“连接断开”指桌面前端页面生命周期、Tauri WebView、HTTP/SSE subscription 或本机后端连接发生变化，不代表 Agent 进程被停止。

## 1. 结论先行

本方案采用两个彼此独立的生命周期：

1. **ConversationRun 生命周期**：由后端负责。创建、执行、取消、完成、失败以及“取消后的业务续跑”都属于这一层。
2. **Transport subscription 生命周期**：由前端和 Assistant Transport 负责。连接、断开、重新订阅只影响状态传输，不改变 Run。

因此必须明确区分：

```text
用户点击“继续”
  -> business resume
  -> 只允许 cancelled Run
  -> 恢复 checkpoint 并继续执行

桌面前端切换 Task / HTTP-SSE subscription 断开 / Tauri 前端重新连接
  -> transport attach/reconnect
  -> 不创建新 Run
  -> 不调用 cancel
  -> 不调用 business resume
  -> 后端已有 Run 继续执行
```

不同 Task 可以并行；同一个 Task 仍然维持一个 active Run 的串行约束。这与当前 `TaskRuntimeSpace.run_lock` 和 `start_or_attach` 的领域边界一致。

## 2. 设计依据

### 2.1 当前代码事实

| 层 | 当前事实 | 对改造的影响 |
| --- | --- | --- |
| 前端 Task 容器 | `workspace-shell.tsx` 使用 `key={activeTaskId}` 渲染 Assistant | 切换 Task 会卸载旧 runtime，不能再把卸载等同于取消 Run |
| 前端初始化 | `use-assistant-initial-state.ts` 通过 `GET /tasks/{id}/assistant/state` 加载 snapshot | 可以把 snapshot 作为重新挂载/重新 attach 的基线，但它不是前端事实源 |
| 前端 runtime | `assistant-runtime.tsx` 当前仍会在 mount、EOF 或非终态 snapshot 时调用 `resumeRun` | 这是本方案需要移除的自动业务恢复行为 |
| 前端 transport | `useAssistantTransportRuntime` 的 `resumeApi` 当前指向 `/assistant` | 该入口同时承载 command 和业务 resume，语义不清，必须增加独立 attach 入口 |
| 前端取消 | `stop-button.tsx` 明确调用 `POST /runs/{runId}/cancel` | 取消只能由明确的用户动作触发 |
| 后端创建 Run | `POST /assistant` 收到 `AddMessageCommand` 后创建 Run、写入基线 snapshot，并调用 `start_run` | 创建执行与 HTTP subscription 已经解耦 |
| 后端 stream | `TransportAssistantService.stream()` 只订阅 snapshot；无 idle timeout；没有 mutation 时保持连接 | 当前 server stream 断开不会自动取消 Run |
| 后端 executor | `ConversationRunExecutor.start()` 创建独立 asyncio task；HTTP 断开不会调用 `cancel()` | 已具备“传输断开不影响执行”的核心能力 |
| 后端 cancel | `POST /runs/{runId}/cancel` 是独立显式入口 | 保持不变，不能在 stream cleanup 或 Task 切换中调用 |
| 后端 resume | `TransportAssistantService.resume_run()` 明确只允许 `CANCELLED` Run | 这是业务契约，不能用来实现 transport reconnect |
| snapshot subscriber | `ConversationTaskSnapshotService` 支持同一个 Task 的多个 subscriber；stream 再按 `runId` 过滤 | 可以支持多个 Task 同时连接，也可以支持同一 Run 的重新订阅 |
| 运行边界 | Tauri 前端通过 supervisor 提供的本机 FastAPI 地址通信；Agent executor 属于本机后端进程 | HTTP/SSE 断开与后端进程停止必须分别建模 |
| 进程关闭 | executor shutdown 会停止本地 asyncio task；启动时当前策略不是自动重放旧 Run | 必须把“HTTP/SSE 断开”和“后端进程崩溃”分开处理 |

当前代码已经具备后端执行与订阅解耦，主要缺口在于：前端把 runtime 卸载、stream EOF 和 snapshot rehydrate 误接到了 `resumeRun`；后端也缺少一个语义明确的“只订阅已有 Run”的 transport attach API。

### 2.2 Assistant UI 官方模型

Assistant UI 官方文档的核心边界是：

```text
AssistantTransport
  -> state stream
  -> ExternalStoreRuntime
  -> Thread / Composer / ConnectionState 等 UI
```

后端发送 canonical state 的 snapshot/增量；前端把它转换为 Assistant UI 消息模型。UI 是渲染层，不应该成为 Run 或 Context 的事实源。

官方文档还明确区分了两类行为：

- transport reconnect：连接丢失后重新连接仍在服务端运行的任务；
- application resume：由应用自己的后端契约决定是否允许继续执行。

因此 assistant-ui 的 `resumeRun` 这个名字不能直接等同于项目的“取消后续跑”。在本项目中，它最多只能映射到一个只读的 transport attach/reconnect 入口；真正的 `CANCELLED -> RUNNING` 业务续跑仍然走独立的后端业务 API。

官方参考：

- [Assistant Transport](https://www.assistant-ui.com/docs/runtimes/custom/assistant-transport)
- [External Store Runtime](https://www.assistant-ui.com/docs/runtimes/custom/external-store)
- [Threads](https://www.assistant-ui.com/docs/runtimes/concepts/threads)
- [Runtime Architecture](https://www.assistant-ui.com/docs/runtimes/concepts/architecture)
- [Connection State](https://www.assistant-ui.com/elements/connection-state)

## 3. 目标运行模型

### 3.1 三个独立对象

前端需要同时维护但不能混为一谈的三个对象：

| 对象 | 所有者 | 可否因 Task 切换改变 |
| --- | --- | --- |
| `ConversationRun` | 后端数据库和 executor | 否 |
| `TransportSession` | 前端 session registry + 后端 subscriber | 可以 attach/detach，但不改变 Run |
| 当前 Thread 视图 | React/Assistant UI | 可以立即切换 |

`TransportSession` 是前端对某个 `taskId + runId` 的临时连接状态，例如 `connected`、`connecting`、`disconnected`、`terminal`、`error`。它不是数据库事实，也不能被写回后端作为 Run 状态。

### 3.2 典型流程

```text
发送 A
  -> POST /assistant(AddMessageCommand)
  -> 后端创建 A.run-1
  -> executor 在后台执行
  -> A 的 TransportSession 接收 snapshot/mutation

切换到 B
  -> 当前 Thread 视图切换到 B
  -> A 不 cancel、不 business resume、不重发 command
  -> A session 可以继续订阅；即使订阅断开，A.run-1 仍执行

发送 B
  -> POST /assistant(AddMessageCommand)
  -> 后端创建 B.run-1
  -> A.run-1 与 B.run-1 并行执行

切回 A
  -> 若 A session 仍连接：直接显示缓存的最新 UI state
  -> 若 A session 已断开：GET snapshot + POST /assistant/attach
  -> 接收当前基线和后续 mutation
  -> 不创建新 Run，不重复发送用户消息，不调用 business resume

如果中间发生的是本机后端进程重启或整个应用崩溃，则流程不同：

```text
重启后切回 A
  -> GET /tasks/{task_id}/assistant/state
  -> 后端已将遗留 active Run 收敛为 CANCELLED
  -> 前端完整替换缓存并显示 Continue
  -> 用户点击 Continue
  -> POST /assistant(runId=A.run-1)
  -> 新的 transport subscription 展示恢复后的执行
```
```

### 3.3 Task 切换的推荐策略

推荐采用“**session 持续存在，Thread 视图单实例**”的模型：

- 所有有 active Run 的 Task 保留轻量 TransportSession；
- 只为当前 Task 渲染完整 `Thread`、Composer 和消息 DOM；
- 非当前 Task 不渲染完整 Thread，不保留大量隐藏 DOM；
- 非当前 Task 只接收 snapshot、更新轻量运行摘要，并在切回时提供最新状态；
- 已终态且没有未读 UI 需求的 Task 可以释放 transport session，之后按 snapshot 重新 attach。

这样既能实现 A/B 并行，又不会因为“所有隐藏 Thread 都继续完整渲染”导致 React reconciliation、Markdown 渲染和 tool card 渲染成本线性增长。

## 4. 后端改造方案

### 4.1 新增只读 transport attach API

建议新增：

```http
POST /assistant/attach
```

请求只描述要订阅的已有 Run，不携带新的 user command：

```json
{
  "taskId": "task-a",
  "threadId": "task-task-a",
  "runId": "run-a-1"
}
```

命名可以是 `/assistant/attach` 或 `/assistant/subscribe`，但必须和业务 `resume` 分开。推荐 `/assistant/attach`，因为它表达的是连接到既有服务端执行。

attach 服务应执行以下步骤：

1. 校验 `taskId`、`threadId`、`runId` 的归属关系。
2. 校验 `runId` 是否为该 Task 当前可展示的 Run。
3. 读取 canonical snapshot。
4. 注册 Task-level subscriber。
5. 注册后再次读取 snapshot，避免“读取与注册之间丢 mutation”。
6. 发送完整 root snapshot。
7. 持续发送属于该 `runId` 的 mutation。
8. Run 进入终态后结束该 subscription。
9. finally 只移除 subscriber，不调用 executor cancel。

attach API 不得执行以下操作：

- `start_run`；
- `claim_or_resume_run`；
- 修改数据库 Run status；
- 写入 Context；
- 追加用户消息；
- 调用 `resume_run`；
- 因 HTTP client disconnect 调用 `cancel()`。

现有 `stream()` 已接近该语义，应抽取一个共同的订阅响应构造路径，让“新消息后的 stream”和“attach stream”复用同一套 snapshot 顺序和 runId 过滤逻辑。仓库现有 `docs/plan/conversation-task-snapshot-single-source-plan.md` 的“hydrate → register → reread → root set → mutations”顺序应作为实现验收标准。

### 4.2 保持业务 resume 契约不变

现有业务 resume 保持：

```text
CANCELLED
  -> POST business resume
  -> 恢复 checkpoint
  -> 同一个 Run 继续执行
```

该 API 不应被前端以下行为调用：

- 切换 Task；
- React component unmount；
- `useAssistantTransportRuntime` mount；
- stream EOF；
- 本机 FastAPI 服务恢复或 Tauri 前端重新连接；
- GET snapshot 发现 `pending/running`。

如果 assistant-ui 的 `resumeApi` 必须配置，则将它指向 `/assistant/attach`，并在代码注释中明确：这是 transport reconnect，不是项目业务 resume。项目自己的“继续执行”按钮仍应调用业务 resume API，完成后再 attach 新的执行流。

### 4.3 并行边界

后端应保持以下不变量：

- `task-a.run-1` 和 `task-b.run-1` 可以同时存在于 executor；
- A 的 subscriber 只能收到 A 的 snapshot/mutation；
- B 的 subscriber 只能收到 B 的 snapshot/mutation；
- 同一个 Task 不允许同时创建两个 active Run，除非未来明确修改领域模型；
- attach 不应令 executor 的本地 task 数量增加；
- 同一 Run 多次 attach 不应产生重复 Context、重复 user message 或重复 tool execution。

### 4.4 后端崩溃与 transport 断开必须分开

本方案保证的是：**Tauri 前端与本机 FastAPI 的 transport 断开不影响 Agent**。

保证链路如下：

1. `POST /assistant` 创建 Run 后，先把 Agent 交给本机 `ConversationRunExecutor` 的独立 asyncio task；HTTP/SSE response 只是该 Run 的 snapshot subscriber。
2. `TransportAssistantService.stream()` 的 `finally` 只注销 subscriber，不调用 executor cancel。
3. Task 切换、React runtime 卸载、Tauri 页面重建和本机 HTTP/SSE 断开都只能结束 subscription，不能改变数据库 Run status。
4. 唯一的用户取消入口仍是 `POST /runs/{runId}/cancel`；只有用户明确点击 Stop 才能触发它。

本机 FastAPI 进程重启、Tauri 应用退出或整个应用崩溃属于进程级故障，不可能依靠原进程内 asyncio task 继续执行。本项目对此采用“后端先收敛，用户手动恢复”的契约：

```text
后端正常关闭
  -> executor.close() 收束本地执行
  -> active Run 持久化为 CANCELLED
  -> projector 更新 canonical snapshot

后端硬崩溃后再次启动
  -> recovery sweep 或首次 state read 发现 DB 中 pending/running
     但当前进程没有 executor
  -> 原子地收敛为 CANCELLED(runtime_restarted)
  -> 投影 RunStatusChangedEvent
  -> /tasks/{task_id}/assistant/state 返回已收敛 snapshot
  -> 用户明确调用 /assistant(resume, runId)
```

这里的 `CANCELLED` 是“可由用户恢复”的业务状态，不是前端断线产生的状态。`/assistant` 的无 `AddMessageCommand` 分支继续调用 `resume_run()`，并由后端强制校验：只有最新 Run 且 status 为 `CANCELLED` 才能恢复。任何 state read、attach、stream EOF 或本机服务重新连接都不得触发它。

当前代码中 `ConversationTaskSnapshotService.read()` 在发现“数据库仍为 pending/running、但 executor 不在本进程”时还只是记录 `conversation_run_recovery_required`。要满足本节契约，必须把这条路径改为幂等的状态收敛，并在返回 `/tasks/{task_id}/assistant/state` 前完成 snapshot 投影；不能仅记录日志后把 pending/running 返回给前端。

状态收敛必须可重复执行：如果进程在 Run 状态提交和 snapshot 投影之间再次崩溃，下一次 recovery 或 state read 仍能根据数据库 Run 状态补齐投影，最终返回一致结果。不要把 attach 误宣传为跨进程 durable execution。

### 4.5 UI 缓存与 canonical snapshot 的一致性

前端的 `initialState`、Assistant UI runtime state、`latestStateRef` 和未来的 `TaskSessionRegistry` 都是**渲染缓存**，不是对话事实。它们无法保证在 transport 断开期间实时等于后端，只能保证在重新建立一致性点后被后端 snapshot 覆盖。

必须遵守以下规则：

1. **完整替换，不做本地合并**：首次加载、重新 attach、后端重启后的 `/assistant/state` 读取都以完整 snapshot 替换当前 Task session；不能把本地旧消息、旧 run status 或本地 optimistic state merge 回 canonical snapshot。
2. **以 `taskId + runId` 识别状态**：只接受属于当前 Task 和当前 session Run 的 stream mutation。Run 变化时必须先丢弃旧 session 的增量，再应用新 root snapshot。
3. **以 `sessionGeneration` 丢弃迟到事件**：每次 detach、重新 attach、后端 URL 变化或 state rebase 都递增 generation。旧 HTTP/SSE subscription 到达的事件即使内容合法，也不能覆盖新 generation 的 state。
4. **明确处理 optimistic command**：`pendingCommands` 只表示尚未得到后端确认的 UI 请求。snapshot rebase 后，未出现在后端 `messages` 中的 optimistic message 必须清除；不能为了“看起来连续”继续保留。
5. **命令重试依靠 commandId**：如果 POST 已提交但 response 在本机连接断开时丢失，前端先读 snapshot 判断消息/Run 是否已存在；不得盲目重新生成 command。确需重试时复用同一个 commandId，由后端幂等判断。
6. **终态优先于本地运行态**：如果 `/assistant/state` 返回 `CANCELLED`、`COMPLETED`、`FAILED` 或 `INTERRUPTED`，前端必须立即导入该终态并停止 attach 重试；只有用户点击 Continue 后才通过 `/assistant` resume。
7. **断线期间允许显示旧缓存，但必须标记连接状态**：旧 UI 可以短暂保留用于避免闪烁，同时显示 disconnected/reconnecting；它不能继续伪装成当前后端运行状态，也不能据此自动 resume。

为长期严格丢弃跨连接的旧增量，目标契约必须增加持久化的单调 `snapshotRevision`。当前 snapshot schema 没有 revision，只有单连接内的队列顺序和前端 generation 防护；第一阶段可以依靠“一 Task 一个当前 subscription + generation + root snapshot”工作，但这不是多个连接交错时的完整全序保证。正式方案应让每个 Task 的 revision 在 snapshot 提交时递增，并在 root snapshot、mutation 和 `/assistant/state` 中携带，前端只接受大于等于当前 revision 的状态。

一致性点应当是：

```text
GET /tasks/{task_id}/assistant/state 返回
  -> 后端已完成 Run 状态与 snapshot 的读边界收敛
  -> 前端校验 snapshot
  -> 清理旧 pendingCommands
  -> 以完整 snapshot 替换 Task session
  -> 新 generation 的 attach 才能继续写入
```

本项目当前不做后端重启后的自动执行恢复。后端重启只负责把遗留 active Run 收敛为可手动恢复的 `CANCELLED`，然后等待用户通过 `/assistant` resume。只有未来产品明确要求“无需用户操作、跨进程自动继续执行”时，才另立 executor recovery 设计，处理 checkpoint、幂等恢复、工具副作用和启动恢复策略；不能通过扩大前端 `resumeRun` 的语义来解决。

## 5. 前端改造方案

### 5.1 引入 TaskSessionRegistry

在 `apps/desktop` 建立一个前端外部 store，例如：

```ts
type TaskSession = {
  taskId: string;
  threadId: string;
  runId: string | null;
  state: TransportState | null;
  phase: "idle" | "connecting" | "connected" | "disconnected" | "terminal" | "error";
  sessionGeneration: number;
  lastError: TransportError | null;
};
```

它只保存后端 canonical state 的不可持久化渲染副本和连接元数据。它不能替代后端 snapshot，也不能被作为下一次命令的权威 Context。

Registry 负责：

- 按 `taskId` 保存 session；
- 为每个 Task 维护独立 `AbortController`、连接 generation 和重连退避；
- 忽略旧 stream 在新 attach 后抵达的迟到事件；
- 让 sidebar 订阅轻量 selector，而不是让整个 Workspace 因某个 Task 的 token 增量重渲染；
- 在当前 Task 切换时复用 session，而不是销毁 Run；
- 在切回 Task 时提供最新 snapshot 和连接状态。

Registry 不能保存：

- Agent Context 事实；
- tool 执行结果的独立副本；
- 用于决定业务 resume 的客户端状态；
- 代替后端的 Run status。

### 5.2 调整 Assistant runtime 的挂载方式

当前 `workspace-shell.tsx` 的 `key={activeTaskId}` 会销毁并重建 Assistant runtime。目标结构应改为：

```text
WorkspaceShell
  ├── TaskSessionHost(task-a)   // 轻量 transport host，可无完整 Thread
  ├── TaskSessionHost(task-b)   // 轻量 transport host，可无完整 Thread
  ├── TaskSidebar               // 只读 session summary
  └── ActiveTaskView            // 只渲染当前 Task 的完整 Assistant UI
```

第一阶段可以继续复用 `useAssistantTransportRuntime`，但必须把每个 Task 的 transport host 与完整 Thread DOM 解耦：

- host 可以在后台接收状态；
- active view 从该 Task 的 session state 渲染；
- Task 切换不触发 command，不触发 cancel，不触发 business resume；
- `key` 只能控制当前视图的 React 生命周期，不能控制后端 Run 生命周期。

第二阶段可以将 transport state 完整迁移到基于 `ExternalStoreRuntime` 的集中 session store，使 runtime 与 UI 更彻底分离。该阶段适合在第一阶段验证协议和性能后进行，不应在没有 attach API 的情况下先做大量 UI 重构。

### 5.3 删除当前自动业务恢复路径

`assistant-runtime.tsx` 中下列路径应删除或改造成 transport attach：

- backend URL 改变后发现 pending/running 就调用 `resume()`；
- `onFinish` 发现 snapshot 非终态就调用 `resume()`；
- mount 时 `resumeOnMount` 自动执行 `resumeRun`；
- GET snapshot 发现 pending/running 就将它解释为“需要恢复执行”。

正确处理是：

```text
snapshot.status == pending/running
  -> Run 已经在后端执行或等待执行
  -> 若需要显示，调用 attach
  -> 若 attach 暂时失败，标记 disconnected 并重试 attach
  -> 永远不调用 business resume
```

### 5.4 stream 断开与显式取消

前端必须将两种动作分开：

| 事件 | 前端行为 | 后端行为 |
| --- | --- | --- |
| Task 切换 | 切换 active view；保留或 detach session | Run 不变 |
| stream EOF/网络断开 | session 标记 `disconnected`；按退避 attach | Run 继续执行 |
| runtime unmount | 关闭当前 HTTP subscription | 不 cancel |
| 用户点击 Stop | 调用 cancel API | Run 进入 cancelling/cancelled |
| 用户点击 Continue | 调用 business resume | 只允许 cancelled Run 恢复 |

attach 重试必须带有：

- 每个 Task 独立的 AbortController；
- generation 检查；
- 指数退避和上限；
- 已终态后停止重试；
- 不重复发送 command；
- 不因重复 attach 增加 Run 或 Context。

如果用户快速 A → B → A，旧 A stream 的事件到达时，必须因 generation 不匹配被丢弃，不能覆盖新的 A session state。

## 6. 性能与用户体验策略

### 6.1 不把“并行运行”实现成“并行渲染完整 UI”

两个 Task 的 Agent 可以同时运行，但不需要同时渲染两个完整 `Thread`。建议：

- 后台 Task 只维护 snapshot、运行状态、未读计数和最近一条摘要；
- 只有 active Task 渲染 Markdown、tool card、代码 diff 和 Composer；
- 对外部 store 使用按 Task 的 selector；
- 消息列表使用稳定 key，避免整个 Thread 因 token 增量重建；
- terminal Task 不再保持长期 stream；
- 先不引入复杂虚拟化，只有在真实消息量和 profiler 证明必要时再加。

### 6.2 连接数量策略

第一版建议：

- 所有存在 active Run 的 Task 保持轻量 subscription；
- 不活跃但仍在运行的 Task 可以持续收集 snapshot；
- 仅在真实使用中出现连接数或渲染压力后，再增加 session attach 上限和 LRU detach；
- LRU detach 只关闭 transport subscription，不 cancel Run；切回时重新 attach。

这是本地桌面单用户模型，不应按浏览器 SaaS 的多租户连接治理设计；但 session store 仍必须预留 detach/re-attach 能力，以应对 Task 视图切换、Tauri 前端页面重建和本机连接短暂中断。

## 7. 失败场景与不变量

### 7.1 桌面前端与本机后端的 transport 断开

这里的断开包括 Tauri WebView 页面重建、前端 runtime 卸载、到本机 FastAPI 的 HTTP/SSE 连接断开，以及 supervisor 提供的本机服务地址短暂不可用。它不等同于用户停止桌面应用，也不等同于后端进程退出。

预期结果：


- executor 仍运行；
- snapshot 持续落 SQLite；
- Tauri 前端稍后用 GET snapshot 获取基线；
- attach 后继续接收后续 mutation；
- 不重复发送最后一条用户消息。

### 7.2 attach 期间发生 mutation

必须保证注册 subscriber 与发送初始 snapshot 的顺序不会丢事件。建议实现：

```text
ensure snapshot
  -> register subscriber
  -> reread snapshot
  -> send root snapshot
  -> send register 后的后续 mutation
```

如果 snapshot service 只提供当前 snapshot 而不提供 mutation sequence，至少要通过版本号或 revision 检测 root 与后续 mutation 的覆盖关系，避免旧数据覆盖新数据。

### 7.3 旧 stream 迟到

每个 TaskSession 必须有 `sessionGeneration`。新 attach 或新 active view 建立后，旧 stream 的所有事件都要经过 generation 校验。后端事件也必须带 `taskId/runId`，不能只依赖当前 active Task。

### 7.4 Run 已经终态

attach 可以发送一次终态 root snapshot 后立即结束；前端进入 `terminal`，不再自动 attach。终态 Run 的业务“继续”只能由明确的用户动作触发，并遵守 cancelled-only 契约。

### 7.5 后端进程崩溃

当前方案不承诺后端进程级继续执行。supervisor 重新拉起本机 FastAPI 后，后端 recovery 必须把遗留的 pending/running Run 幂等收敛为 `CANCELLED`，并让 `/tasks/{task_id}/assistant/state` 返回与 Run 数据库状态一致的 snapshot。Tauri 前端展示该状态和 Continue 操作；只有用户明确操作后，才通过 `/assistant` resume。

## 8. 测试与验收标准

### 8.1 后端测试

必须覆盖：

1. 启动 Task A 的 Run，断开 HTTP subscription，executor 仍执行到终态。
2. 对同一个 active Run attach，得到当前完整 snapshot 和后续 mutation。
3. attach 不调用 `start_run`、`claim_or_resume_run`，不改变 Run status。
4. A/B 两个 Task 同时执行，两个 stream 不交叉事件。
5. 同一 Run 重复 attach 不重复写入 Context、消息或工具调用。
6. attach 的 `taskId/runId` 不匹配时返回结构化错误。
7. 取消只由 cancel endpoint 触发；stream finally 不触发 cancel。
8. 只有 `CANCELLED` Run 可以进入 business resume。
9. subscriber 注册和 root snapshot 读取之间发生 mutation 时，不丢最终状态。
10. 后端正常关闭和重启后，遗留 pending/running Run 最终收敛为 `CANCELLED`，且 `/tasks/{task_id}/assistant/state` 返回的 snapshot 与 Run 状态一致。
11. Run 状态提交与 snapshot 投影之间再次崩溃时，下一次 recovery/state read 可以幂等补齐投影。

### 8.2 前端测试

必须覆盖：

1. 启动 A，切换 B，启动 B；请求日志中没有因切换产生的 cancel 或 business resume。
2. A/B 同时运行时，两个任务的状态、消息和运行指示互不覆盖。
3. A 的 stream 在中途断开，A 后端继续运行；切回 A 后可以看到最新 snapshot 并继续接收流。
4. 快速 A → B → A 切换时，旧 stream 事件不会污染新 session。
5. 切换 Task 不会重复发送 user command。
6. 隐藏 Task 不渲染完整 Thread DOM，只更新轻量 session summary。
7. 用户点击 Stop 才调用 cancel；用户点击 Continue 才调用 business resume。
8. active Task 初次加载、重新 attach、终态加载三条路径都能正确渲染 Assistant UI connection state。

### 8.3 性能验收

至少记录：

- Task 切换耗时；
- active Thread 首次可见耗时；
- A/B 并行 token 增量时的 React commit 次数；
- inactive Task snapshot 更新是否触发 active Thread 重渲染；
- reconnect 期间是否出现重复 command 或重复 Run。

## 9. 实施顺序

### Phase 0：契约和回归测试

- 固化“stream disconnect 不 cancel”的后端测试。
- 固化 `CANCELLED` only business resume 测试。
- 为 attach 请求和错误码定义 Pydantic 契约。
- 清理旧文档中关于“5 秒 idle timeout”和“非终态自动 resume”的过时描述。

### Phase 1：后端 attach

- 新增 `/assistant/attach`。
- 抽取新消息 stream 与 attach 共用的订阅逻辑。
- 实现注册后 reread snapshot 的竞态保护。
- 增加 task/run 归属校验和结构化错误。
- 增加后端启动 recovery 与 `/assistant/state` 读边界 recovery：遗留 pending/running Run 收敛为 `CANCELLED`，并在响应前补齐 snapshot 投影。

### Phase 2：前端 TaskSessionRegistry

- 将 Task session 从 `activeTaskId` 单一 runtime 中提取出来。
- 移除 mount/EOF/snapshot rehydrate 到 business `resumeRun` 的自动路径。
- 为 active Run 建立独立的 attach/reconnect 生命周期。
- 保留单一 active Thread 视图，后台只维护轻量 transport/session。

### Phase 3：体验和性能

- 增加连接状态、未读状态和重连提示。
- 使用 Task selector，阻断 inactive Task 更新导致的全局重渲染。
- 根据 profiler 决定是否引入 LRU detach、消息虚拟化或更细粒度 store。

### Phase 4：可选的自动进程级恢复

当前产品只做“进程重启后收敛为 `CANCELLED`，用户手动 `/assistant` resume”。如果未来需要后端重启后无需用户操作就继续执行，再另行设计 executor recovery、checkpoint 恢复和工具副作用幂等；不要把它混进本次 transport 改造。

## 10. 非目标与明确取舍

- 不允许前端执行 Agent、工具或审批。
- 不把前端 `localStorage` 当作 Conversation 事实源。
- 不支持同一 Task 多个 active Run，除非后续明确修改领域模型。
- 不用 Task 切换触发 cancel 或 business resume。
- 不通过隐藏多个完整 Thread 来实现并行运行。
- 不把 assistant-ui 的 `resumeRun` 名称直接解释为项目的 cancelled-only business resume。
- 不在本次改造中引入 Redis、Postgres、云队列或分布式执行。

## 11. 完成定义

当以下条件全部满足时，本方案才算完成：

```text
Task A 与 Task B 可以同时运行
且
切换 Task 不取消、不重启、不重复发送任何 Run
且
任意 transport disconnect 都不改变后端 Run 生命周期
且
切回 Task 可以从 snapshot + attach 恢复可见状态
且
只有用户明确操作才会触发 cancel 或 cancelled-only business resume
且
后台 Task 不渲染完整隐藏 Thread，active Thread 交互保持流畅
```

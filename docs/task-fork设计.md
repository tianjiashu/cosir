# Task 历史 Run Fork 设计

## 1. 设计结论

本方案把 fork 定义为：

> 在当前 Task 的某个历史 `run_id` 处，复制该 Run 及其之前的 Task context 和 Assistant Transport snapshot，创建一个新的 Task；新 Task 后续继续使用现有 `/assistant` 端点创建新的 ConversationRun。

本方案明确：

- 不新增 `task_forks` 表；
- 不新增 `TaskForkService`，fork 编排直接放在 `TaskService`；
- 不新增 `GET /tasks/{task_id}/runs`；
- 不复制 LangGraph checkpoint 内容；
- 不生成新的 `checkpoint_thread_id`，复制 Run 时保留源 Run 的 `checkpoint_thread_id`；
- 新 Task 使用 `task_type = "fork"`；
- 新 Task 使用 `Task.extra` 保存 fork 来源关系，不新增 `task_forks` 表；
- fork 前提是源 Task 的所有 Run 都处于终态；
- 第一版只 fork 对话上下文和对话 snapshot，不回滚或复制 workspace 磁盘文件。

父 Task 不被修改。新 Task 的第一次新输入仍然通过现有 Assistant Transport 流程创建新的 ConversationRun。

## 2. 当前项目边界

桌面端是 Tauri 托管的静态 React/Vite 应用，前端直连本机 FastAPI backend。backend 进程负责 Task、ConversationRun、context、snapshot 和 fork 事务；主库为 SQLite，LangGraph checkpoint 使用独立 SQLite 文件，workspace 文件保存在 workspace 的 `root_path`。

当前关键事实：

- 一个 Task 对应一个 `TaskRuntimeSpace` 和一个 `RuntimeContextManager`；
- 一个 ConversationRun 对应一条 `conversation_runs` 记录；
- `conversation_task_contexts.run_id` 是 `conversation_runs.id` 的外键；
- Assistant snapshot 中的消息带有 `runId`；
- `ConversationRunModel.checkpoint_thread_id` 当前带有全局唯一约束；
- Task 切换由前端导航到 `/tasks/{taskId}`，并重新挂载 Assistant runtime。

相关代码：

- [TaskService](../apps/backend/app/task_runtime/service/task_service.py)
- [RuntimeContextManager](../apps/backend/app/core/context/runtime_context_manager.py)
- [ConversationRunModel](../apps/backend/app/storage/model/conversation_run_model.py)
- [ConversationTaskContextModel](../apps/backend/app/storage/model/conversation_task_context_model.py)
- [ConversationTaskSnapshotService](../apps/backend/app/assistant_transport/service/conversation_task_snapshot_service.py)
- [桌面端 Task 导航](../apps/desktop/components/workspace-shell.tsx)

## 3. Task 类型和标题

新建 fork Task 的字段：

```text
task_type       = "fork"
parent_task_id  = NULL
parent_run_id   = NULL
delegation_id   = NULL
workspace_id    = 源 Task.workspace_id
extra           = fork 来源元数据
```

不使用 `parent_task_id`，因为它当前表示 delegation 父任务；使用该字段会让 fork Task 被误判为 delegation 子任务。

标题采用源 Task 标题加数字：

```text
原任务
原任务 1
原任务 2
原任务 3
```

单进程下标题编号可以复用源 Task 的 Task runtime lock 和同一个 SQLite 写事务完成。当前项目不需要
分布式锁。若未来允许不同源 Task 并发生成相同标题，标题编号可作为展示性编号，不应把它设计成跨 Task
的强一致业务标识。

当前 `TaskCrud.list_by_workspace()` 只查询 `task_type = "user"`，实现时必须改为包含：

```text
task_type IN ("user", "fork")
```

`delegation` 仍然排除在普通 Task 列表之外。前端直接根据 `TaskResponse.task_type` 显示 Fork 标记，
Fork 标记和任务操作入口统一使用 `lucide-react` 的 `GitFork` 图标。

`parent_task_id` / `parent_run_id` 继续保留 delegation 语义，因此 fork 不复用这两个字段。
fork 来源写入 `Task.extra`，例如：

```json
{
  "fork": {
    "source_task_id": 10,
    "source_run_id": 12
  }
}
```

这要求 `TaskModel` 新增 nullable JSON 字段 `extra`，语义类似 `ConversationRunModel.extra`；同时
`TaskRecord`、`TaskCrud.create()` 以及 Task 到领域对象的映射都必须支持该字段。
该字段只记录来源，不参与 Task 列表筛选和运行时状态计算。来源关系只保存源 Task / 源 Run 的 ID，
不复制源对象；源 Task 删除后，目标 Task 的历史数据仍然独立存在，但来源元数据可能无法再解析。

如果允许从 fork Task 继续 fork，则以当前被 fork Task 的 title 作为新标题前缀，不通过
`parent_task_id` 推导关系。编号作用域、原始标题已带数字、以及不同源 Task 同时生成相同标题的处理规则，
必须在实现中固定下来。

## 4. Fork API

新增一个 Task 领域端点即可：

```http
POST /tasks/{task_id}/fork
```

请求体：

```json
{
  "runId": 42
}
```

其中：

- `task_id` 是源 Task；
- `runId` 是历史边界 Run，复制范围包含该 Run；
- 标题由后端按编号规则生成，不由前端传入。

请求体只需要 `runId`。不传 `commandId`，也不传 title；fork 的来源关系由后端写入目标 Task 的 `extra`。

返回新 Task：

```json
{
  "task_id": 108,
  "workspace_id": 3,
  "title": "原任务 1",
  "task_type": "fork"
}
```

现有 Task 查询响应应增加一个 Task 级能力字段：

```json
{
  "fork_available": true
}
```

`fork_available` 表示该 Task 当前所有 `conversation_runs` 都处于终态。终态包括：

```text
completed / failed / cancelled / interrupted
```

它不是某一条 Assistant snapshot 的 `run.status`，也不表示存在可 fork 的历史 Run；具体消息是否可 fork
仍由消息上的 `runId` 决定。当前 `TaskResponse.execution_status` 在 `TaskRecord.from_model()` 中没有真正
从 Run 派生，因此不能直接把它当作 `fork_available` 使用。

错误语义建议固定为：

- `404`：源 Task 不存在，或 `runId` 不属于源 Task；
- `409`：源 Task 存在任意非终态 Run（当前包括 `pending` / `running`），指定 Run 不满足可 fork 的终态条件，或 snapshot 尚未就绪；
- `422`：请求体缺少 `runId`、类型错误或 `runId <= 0`。

不新增 runs 查询 API。前端从当前 Assistant snapshot 中已有的 `runId` 对消息分组，并在历史 Run 的操作区域发起 fork 请求。

## 5. 前置校验

`TaskService.fork_task()` 在源 Task runtime lock 内完成以下校验：

1. 源 Task 存在；
2. `runId` 属于源 Task；
3. 源 Task 的所有 Run 都处于已知终态；任意非终态（当前包括 `pending` / `running`）即拒绝；
4. `runId` 是已结束的历史 Run；
5. snapshot 存在且可以通过现有校验；snapshot 缺失或不可校验时返回 `SNAPSHOT_NOT_READY`，不创建目标 Task；
6. `cancelled` / `interrupted` 可以作为 fork 边界；fork 与后续 resume 不冲突，是否允许恢复源 Task
   的某个 `cancelled` Run 由现有 resume 规则独立决定；
7. context 中的工具调用状态满足当前 snapshot/context 校验要求。

当前 `ConversationTaskContextService.recover_interrupted_run()` 不接收外部 Session。由于 fork 不需要修改
源 Task，也不需要为了 fork 自动恢复源 Run，因此第一版不在 fork 流程中调用该修复方法；只校验可复制的
context 状态。后续如果需要在 fork 前自动修复 context，再增加 session-aware 的修复内部方法。

resume 和 fork 是两个独立操作：源 Task 所有 Run 终态时可以 fork；源 Task 之后仍可按现有规则 resume
可恢复的 Run。fork 创建的目标 Task 不恢复 cloned historical Run。

如果源 Task 有活动 Run，返回 409，不进行部分复制。

## 6. ConversationRunModel 复制和 Run ID 映射

必须复制源 Task 从首个 Run 到 `runId` 的全部 `ConversationRunModel` 行，而不是只复制选中的一行。

前缀按当前 `ConversationRunCrud.list_by_task()` 的顺序确定：

```text
created_at ASC, id ASC
```

不能使用 `run.id <= runId` 判断前缀，因为 Run ID 是全局主键，不是 Task 内的序号。

例如：

```text
源 Task:    10 -> 11 -> 12
新 Task:   101 -> 102 -> 103
```

维护一次性的映射：

```text
10 -> 101
11 -> 102
12 -> 103
```

复制 Run 时不能调用普通 `ConversationRunService.create_run()`。该方法会发布
`RunInitializedEvent` / `UserInputAppendedEvent`，会重新触发事件投影。fork 应通过支持外部 Session 的
内部 clone CRUD 方法静默复制数据库行。

复制 Run 时：

| 字段 | 处理方式 |
|---|---|
| `id` | 不复制，使用数据库新生成的 ID |
| `task_id` | 改为新 Task ID |
| `checkpoint_thread_id` | 保留源值，不生成新的值 |
| `input_text` | 复制 |
| `status` | 复制终态 |
| `end_reason` | 复制 |
| `final_output` | 复制 |
| `agent_id` / `provider_id` / `model_name` | 复制 |
| `image_paths` / `reasoning_effort` / `extra` | 复制 |
| `created_at` / `updated_at` | 保留历史时间 |

`conversation_task_contexts.run_id` 和 snapshot 中的 `messages[*].runId` 必须使用这张映射表改写。

不能让新 Task 的 context 继续引用源 Task 的 Run ID，否则：

- 删除源 Task 会级联删除这些 Run；
- 新 Task 的历史消息会引用另一个 Task 的 Run；
- 后续按 Task 查询 Run 时会出现归属不一致。

## 7. checkpoint_thread_id 决策

本方案不复制 checkpoint SQLite 中的 LangGraph graph state，也不为 fork 出来的历史 Run 生成新的 `checkpoint_thread_id`。

复制的历史 Run 保留源 Run 的 `checkpoint_thread_id`，原因是这些 Run 仅作为历史记录展示和 context 归属，不会在新 Task 中重新执行或恢复。

当前 `ConversationRunModel` 的唯一约束是：

```text
UNIQUE(checkpoint_thread_id)
```

当前模型没有名为 `run_id` 的列；Run ID 实际是继承自 `StorageBase.id` 的主键。因此不能改成：

```text
UNIQUE(run_id, checkpoint_thread_id)
```

如果把 `run_id` 替换为 `id`，联合唯一约束会因为 `id` 已经是主键而变成冗余约束。
实现上应直接移除 `checkpoint_thread_id` 的 `unique=True`，允许 cloned historical Run 共享源 Run 的值。
同时必须更新模型中“checkpoint 身份必须唯一”的现有注释和相关测试。由于 SQLAlchemy 的
`create_all()` 不会修改已经存在的 SQLite 表，`initialize_app_schema()` 还会检测当前本机库的
旧 `UNIQUE(checkpoint_thread_id)` 并在一个 SQLite 写事务中重建 `conversation_runs` 表、保留数据和
当前索引；因此不要求用户手工删除本地数据库。

这不仅是 Python model 定义变化：如果已有 SQLite 数据库已经创建了旧的唯一索引，单纯修改
`unique=True` 不会自动删除该索引。当前项目不承诺旧数据兼容，因此实现阶段应明确采用重建开发库，或执行
SQLite 表 / 唯一索引重建，确保旧的 `UNIQUE(checkpoint_thread_id)` 实际消失。

这正是本方案的有意选择，但它意味着：

- fork 出来的历史 Run 不能调用 resume；
- 源 Task 的 resume 与 fork 不冲突；fork 完成后源 Task 仍可按现有规则 resume 可恢复的 Run；
- 新 Task 的 snapshot 必须将当前运行指针重置为 `runId = null`、`status = idle`；
- 现有恢复端点不能把这些 cloned historical Run 作为可恢复 Run；
- 新 Task 后续新建的 ConversationRun 仍应按当前运行流程处理 checkpoint 身份，不能复用历史 cloned Run 的执行状态。

换言之：保留 `checkpoint_thread_id` 只服务于历史字段复制，不代表复制 checkpoint，也不代表允许恢复这些历史 Run。

## 8. Context 复制

先根据按 `created_at + id` 排序的 Run 前缀建立源 Run ID 集合，再复制这些 Run 对应的 context entries。
不要只依赖“某个 Run 的最后一条 context entry”作为边界，因为 context sequence 是 Task 级序列，且可能存在
`run_id = NULL` 的运行时系统内容。

复制规则：

- `task_id` 改为目标 Task；
- `run_id` 按 Run ID 映射表改写；
- `message_json` 使用已有 LangChain 序列化结果复制；
- `include_in_context` 原样保留；
- sequence 在目标 Task 中重新分配，起始值不作业务约束，但必须保持严格递增且无重复；
- system prompt 不从源 manager 的内存对象复制，由目标 Task 的 `RuntimeContextManager` 重新构建；
- 不共享源 Task 的 `_entries`、listener 或 current run 状态。

`RuntimeContextManager.fork_context_manager(task_id, run_id)` 的建议职责是：

- 将 `task_id` 解释为目标 Task；
- 将 `run_id` 解释为源 Task 的历史边界（边界归属与终态校验由 `TaskService` 在事务前完成）；
- 基于已复制的持久化 context 构造目标 manager；
- 目标 manager 初始 `current_run_id = None`。

它必须纳入 `TaskService.fork_task()` 的 fork 流程，用于为目标 Task 构造独立的 fork context manager，
并明确目标 manager 的 fork 身份。目标 `RuntimeContextManager` 应能从目标 Task 的
`task_type = "fork"` / `extra.fork` 识别并保留 `is_fork` 运行时标记，供运行时日志、调试和后续上下文策略
使用；任务列表中的可视化 Fork badge 仍由前端负责。目标 manager 初始 `current_run_id = None`，后续用户输入
才开始新的 Run。
它不负责创建 Task、复制 Run、写 snapshot 或开启数据库事务。Task 创建、Run 复制、context 持久化和 snapshot
复制仍由 `TaskService.fork_task()` 统一编排。由于 `TaskRuntimeSpace` 当前会按 Task 懒加载
`RuntimeContextManager`，fork 提交后由目标 TaskRuntimeSpace hydrate 该 manager，不复制源 manager 的内存
`_entries`、listener 或 current run 状态。

## 9. Snapshot 复制

复制源 snapshot 中属于源 Run 前缀的全部消息，并改写消息上的 `runId`：

```text
源 snapshot: user-10, assistant-10, user-11, assistant-11, user-12, assistant-12
目标 snapshot: user-101, assistant-101, user-102, assistant-102, user-103, assistant-103
```

目标 snapshot 的运行状态重置为：

```json
{
  "run": {
    "runId": null,
    "status": "idle"
  },
  "error": null,
  "approvals": {}
}
```

历史消息的 completed/failed/cancelled 状态保留。`usage` 和 `context_usage` 建议清零，避免把源 Task 最近一次 Run 的运行指标误认为目标 Task 当前运行指标。

snapshot 复制必须通过 `ConversationTaskSnapshotService` 的 owner 能力完成，不能在 API 层直接读写 snapshot CRUD，也不能由 `ConversationEventProjector` 反向修改 context。

当前 snapshot service 有进程内 `_states` cache。使用外部 Session 写入时，目标 cache 只能在数据库事务提交后
更新；事务回滚时必须清理目标 cache，不能在提交前发布 target snapshot 或让 cache 领先于数据库。

源 snapshot 缺失或无法通过现有校验时，fork 返回稳定错误码 `SNAPSHOT_NOT_READY`（允许稍后重试），不创建目标 Task，
也不将缺失 snapshot 当作空历史。该错误表示等待 snapshot 投影完成后可以重试，不表示源 Task 或 runId
不存在。

目标 snapshot 为 idle，但目标 Task 中最后一个 cloned Run 仍然保留源 Run 的终态。文档和响应模型需要明确
`TaskResponse.execution_status` 的展示规则，不能让 snapshot 的 idle 与 Task 列表中的运行态产生矛盾。

## 10. 事务边界

`TaskService.fork_task()` 先获取源 Task 的 `TaskRuntimeSpace.run_lock`，再在线程池中使用一个主库写事务，顺序为：

1. 获取与 `ConversationRunExecutor` 共用的 `run_lock`，等待当前执行或取消收束；
2. 在同步工作线程获取源 Task runtime lock，校验所有 Run 均为终态；
3. 读取并校验源 Task、按 `created_at + id` 排序的 Run 前缀、context、snapshot；
4. 创建目标 Task；
5. 复制 ConversationRunModel 并建立 Run ID 映射；
6. 复制 context；
7. 复制并改写 snapshot；
8. 提交事务；
9. 提交后更新目标 snapshot cache；若已物化源 manager，则尝试预热目标 manager，失败只记录日志，后续按 Fork Task 懒加载恢复。

事务内任一步失败都必须回滚 Task、Run、context 和 snapshot，不能留下半成品 fork Task。提交后的
manager 预热和 cache 仅是进程内优化，不属于持久化成功条件；其失败不得把已提交的 Fork 报成失败，
否则会让用户误以为可以安全重试而产生重复 Task。

Fork 与执行/取消的并发边界由同一个 `run_lock` 保证：取消即使先把 Run 行更新为 `cancelled`，
后台执行仍未释放 `run_lock` 时 Fork 也不会进入复制事务，避免复制到未收束的 context 或 snapshot。
Fork 的同步 SQLite worker 由 shielded asyncio task 承载；即使 HTTP 协程被取消，也必须等待 worker
完成后才释放 `run_lock`。

Task、Run、context、snapshot 的数据库写入必须复用同一个外部 Session。TaskService 不应直接拼 SQL，
而应调用各自 CRUD/service 的 session-aware 内部方法。snapshot 的内存 cache 和 subscriber 通知必须遵循
提交后的处理规则。

本 API 最小契约不包含 `commandId`。因此同一个 `task_id + runId` 可以被用户主动 fork 多次，每次生成一个新的 Fork Task；前端应在请求期间禁用重复点击。若未来需要把网络重试识别为同一次操作，再单独增加幂等键，不改变 fork 的领域参数。

## 11. 文件和 ChangeSet 范围

第一版不复制：

- `file_snapshots`；
- ChangeSet 状态；
- workspace 磁盘文件；
- delegation 记录；
- `conversation_commands`。

因此 fork 的语义是“对话历史分支”，不是代码工作区回滚。

目标 Task 与源 Task 共享同一个 workspace，目标 Task 后续工具看到的是 fork 时刻的当前磁盘状态。UI 应明确提示：

```text
将复制指定 Run 之前的对话上下文；不会回滚当前工作区文件。
```

真实的代码状态 fork 留到后续独立设计，可选择 Git worktree 或独立 workspace 副本。

## 12. 前端流程

前端不新增 ThreadList 后端适配，也不新增 runs API。

前端以 Task 级 `fork_available` 控制所有历史消息的 Fork 操作，而不是根据单条消息或当前 snapshot
推断是否可 fork。历史消息已经携带 `runId`，在对应历史 Run 的操作菜单中提供：

```text
从此处 Fork 新任务
```

单条消息的 Fork 操作启用条件为：

```text
message.runId != null
AND task.fork_available == true
AND 当前 Assistant runtime 没有正在发送的命令
AND 当前没有正在执行 fork 请求
```

Fork 操作统一使用 `lucide-react` 的 `GitFork` 图标，不使用 `Split` 或其他相近图标，避免与
assistant-ui 内置的消息 branch / reload 操作混淆。图标放在每个 Run 的最后一条可展示消息的 action bar
或 Run footer 中，按钮 tooltip 使用：

```text
从此处 Fork 新任务
```

如果 `fork_available == false`，建议仍保留操作项但置灰，并提示：

```text
当前 Task 仍有运行中的 Run，所有 Run 完成后才能 Fork
```

即使用户选择的是较早的历史 Run，只要当前 Task 还有任意一个非终态 Run，所有历史消息
都不能 Fork。当前流式响应期间可以由 assistant-ui 的 `ActionBarPrimitive.Root hideWhenRunning` 隐藏 action bar，
但这只是即时 UI 状态，不能代替后端校验。

当前 `TransportMessage` 有 `runId`，但 `apps/desktop/lib/assistant/converter.ts` 转换为 assistant-ui
`ThreadMessage` 时没有把它写入 message metadata。因此操作菜单不能假设直接从转换后的 message 对象取得
`runId`。实现时应选择：

- 推荐在 UI converter 的 metadata 中保留 `runId`，例如：

  ```text
  metadata.custom.runId = message.runId
  ```

  然后在 message action 内通过 assistant-ui 的 message metadata 读取。

- 也可以从原始 `TransportState.messages` 建立 `messageId → runId` 映射，但不能让后端领域层依赖
  assistant-ui 类型。

这属于前端适配层，不应把 assistant-ui 类型引入后端领域层。

当前 workspace Task 列表的前端 `WorkspaceTask` 类型没有 `task_type`，实现 Fork 标记前需要补充该字段。
当 `task_type === "fork"` 时，在标题旁显示 `GitFork` badge；任务列表仍通过现有 workspace Task 查询加载，
不新增 ThreadList 后端适配。

Fork 按钮只在每个 `runId` 分组的最后一条可展示 assistant 消息上出现一次。当前实现由
`apps/desktop/lib/assistant/converter.ts` 在转换 snapshot 时反向扫描每个 `runId`，把
`isLastRunMessage` 和 `runId` 写入 assistant-ui message metadata；`thread.aui.tsx` 的
`ActionBarPrimitive.Root` 读取该 metadata 后渲染自定义 `GitFork` 按钮。因此不能继续使用全局
`autohide="not-last"` 来表达 Run 边界；它只用于 action bar 的悬停显示策略，Run 边界由 converter
的 metadata 决定。

当前 `assistant-runtime.tsx` 在 Transport 结束后会重新读取 Assistant snapshot，但这不会自动刷新
Task 列表或 Task 元数据。因此 Run 开始时前端应立即把当前 Task 的 Fork 操作视为不可用；Run 完成、失败、
取消或中断后，重新读取现有 `GET /tasks/{taskId}`，更新 `fork_available`。

当前实现由 `WorkspaceShell` 持有 Task 元数据刷新和 Task 列表刷新职责，`AssistantRuntime` 通过
`TaskStateBridge` 在 run 开始时临时关闭 Task 级 Fork，并由 Transport `onFinish` 在终态时触发
`GET /tasks/{taskId}` 刷新；Thread 只负责展示 message action 和触发 fork，不自行维护 Task 级事实。

成功后：

1. 暂时禁用当前 Task 的所有 Fork 按钮；
2. Fork 按钮切换为 `GitFork` + loading 状态，调用 `POST /tasks/{taskId}/fork`，请求体只有 `{ "runId": messageRunId }`；
3. 成功后刷新 workspace Task 列表；
4. 导航到 `/tasks/{newTaskId}`；
5. 现有 Assistant 初始化流程读取目标 snapshot；
6. 目标 snapshot 是 idle；
7. 用户下一次输入复用现有 `/assistant` 端点创建新 Run。

如果 POST 期间源 Task 新启动了 Run，后端返回 `409`。前端不能自行重试 fork；应重新读取 Task 元数据，
恢复 Fork 按钮状态，并提示当前 Task 仍有运行中的 Run。

Assistant UI 的 message branch 和 Task fork 是两个层次：前者是同一 thread 内的消息分支，后者是新的 Task/thread。参考官方文档：[ThreadList](https://www.assistant-ui.com/docs/primitives/thread-list)、[Edit a sent message](https://www.assistant-ui.com/elements/edit-message)、[Assistant Transport](https://www.assistant-ui.com/docs/runtimes/custom/assistant-transport)。

## 13. 需要覆盖的验证

后端：

- source Run 不属于当前 Task 时拒绝；
- Task 存在任意非终态 Run 时拒绝；
- `cancelled` / `interrupted` 的终态语义与现有 resume 规则独立，源 Task 可在 fork 后继续 resume 可恢复 Run；
- 从中间 Run fork 只复制前缀；
- 多个源 Run 能正确建立新 Run ID 映射；
- context 的 run_id 全部指向目标 Task 的 cloned Run；
- snapshot 的消息 runId 全部完成映射；
- snapshot 缺失或不可校验时返回 `SNAPSHOT_NOT_READY`，且不创建目标 Task；
- cloned Run 保留原 checkpoint_thread_id；
- fork 不复制 checkpoint 内容；
- 目标 snapshot 初始为 idle；
- 目标 context sequence 保持严格有序且无重复；
- `Task.extra.fork` 正确保存 source_task_id / source_run_id；
- cloned historical Run 不能通过 resume/recovery 端点恢复；
- 目标 Task 第一次新输入创建新的 ConversationRun 和新的 checkpoint_thread_id；
- 事务内失败不会留下目标 Task；提交后的 manager/cache 预热失败不伪装成 Fork 失败；
- 同一个 `task_id + runId` 重复 fork 会创建多个独立 Task，不会被错误去重；
- 删除源 Task 不影响目标 Task 的 cloned Run、context 和 snapshot；
- snapshot 事务回滚后，目标 snapshot cache 不残留；
- `checkpoint_thread_id` 不再存在全局唯一约束；
- 源 Task 和目标 Task 互不影响；
- fork Task 能出现在 workspace Task 列表。
- 目标 RuntimeContextManager 能识别并保留 fork 运行时标记。

前端：

- 历史 Run 操作菜单能调用 fork；
- Task 存在任意非终态 Run 时，所有历史消息的 Fork 操作都不可用；
- Task 的所有 Run 进入终态后，历史消息的 Fork 操作恢复可用；
- `runId` 能从 message action 的 metadata 或等价映射中正确取得；
- workspace Task 类型包含 `task_type`，Fork Task 显示 `GitFork` badge；
- 每个 Run 只有最后一条可展示消息显示一次 `GitFork` Fork 操作；
- Run 完成后前端会刷新 Task 级 `fork_available`，而不是只刷新 Assistant snapshot；
- fork 成功后导航到新 Task；
- 新 Task 显示正确的历史前缀和 Fork 标记；
- 新 Task 首次输入使用新的 Task ID 和新的 ConversationRun；
- fork 竞态触发 409 时，前端不会创建重复 Task，并会刷新 Task 状态；
- Fork 操作统一渲染为 `GitFork` 图标，正常、禁用和 loading 状态视觉一致；
- 父 Task 的消息和运行状态不被改变。

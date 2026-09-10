# Assistant Transport 职责整理与逻辑审查报告

日期：2026-09-09

审查范围：

- `apps/backend/app/assistant_transport/assistant_api.py`
- `apps/backend/app/assistant_transport/service/transport_assistant_service.py`
- `apps/backend/app/assistant_transport/service/conversation_task_snapshot_service.py`
- `apps/backend/app/assistant_transport/service/conversation_run_command_service.py`

关联核对：`ConversationEventProjector`、`ConversationRunExecutor`、
`ConversationRunService`、`TaskRuntimeSpace`、桌面端 Assistant runtime。

本次基于本报告完成了现有文件内的改造，没有新增生产代码文件；此前为验证缺陷创建过一次性临时探针，运行后已删除。

## 结论

目前代码的主方向是对的：`/assistant` 仍然可以作为发送新消息、编辑重跑和业务恢复的唯一对话入口；snapshot 也已经成为前端 Transport 的 canonical state。现有核心 Transport 测试通过，流的终态消费、断连不取消、终态兜底等边界已有较好覆盖。

此前审查发现职责混乱和 3 个已由测试复现的逻辑缺陷。本次已完成对应改造：

1. 当前 wire 校验把合法的“带最近 `runId` 的 add-message 重放”错误地绑定到 `sourceId`；不带 `sourceId` 会被拒绝。
2. 编辑重跑的 run、context、snapshot、command 写入不是一个事务；command 写入失败时，前 3 个写入已经完成。
3. snapshot 流首帧没有核对请求的 `runId`，订阅旧 run 时可能先把新 run 的完整 snapshot 发给客户端。

1. wire 和 `/assistant` 已改为按 `runId` 判定新建、最近 run 重放和业务续跑，`sourceId` 不再参与 backend 业务分支或幂等指纹。
2. 编辑重放的 run、context、snapshot、command 写入已共享一个事务，并在 commit 后才更新缓存/发布 snapshot。
3. snapshot stream 首帧已增加 run identity guard；首帧不匹配时不会发送错误 state。
4. controller mutation 已从 `ConversationTaskSnapshotService` 移到 `TransportAssistantService`，Transport 不再调用 snapshot service 私有方法。

由于这是单用户本地桌面 Agent，本次没有新增 HTTP 入口、认证、队列或跨进程服务；改动限制在现有文件和已有 service/CRUD 签名调整内。

## 运行边界

功能运行在一个由 Tauri supervisor 管理的本机 FastAPI/uvicorn backend 进程中。桌面端通过 supervisor 获取动态 loopback 地址，Assistant UI 直接请求该地址；HTTP/Assistant Transport 只是桌面进程与 backend 进程之间的本机边界，不是公网服务边界。

持久事实存放在本地 SQLite，包括 Task、ConversationRun、ConversationMessage/Context、Command 和 Task snapshot。snapshot 的进程内 subscriber、working copy 和 executor registry 只属于当前 backend 进程。Tauri 负责启动、停止和重启 backend；backend 启动时由 `recover_orphaned_runs` 把遗留的 `pending/running` run 收敛为 `cancelled`，读取 snapshot 时再补齐终态投影。backend 崩溃后的恢复不是由前端 state 推断，而是依赖 SQLite 状态和 backend 启动/读取边界。

业务入口保持如下边界：

```text
POST /assistant
  ├─ 新消息       → command/run application orchestration → executor start
  ├─ 编辑重跑     → command/run application orchestration → executor start
  └─ 空 commands  → business resume orchestration          → executor start
                    ↓
              Transport snapshot subscription/encoding
```

`/tasks/{task_id}/assistant/attach` 是已有运行的纯订阅控制入口，`/tasks/{task_id}/assistant/state` 是读取入口，`/runs/{run_id}/cancel` 是控制入口；它们不应演化成另一条发送消息链路。

## 新增：新建、编辑重放与续跑的判定边界

本节结合当前前后端实现以及 [Assistant UI Assistant Transport 官方文档](https://www.assistant-ui.com/docs/runtimes/custom/assistant-transport)、[Message Editing 官方文档](https://www.assistant-ui.com/docs/guides/editing) 得出。核心结论是：Assistant UI 的 `sourceId` 和后端的 `runId` 属于不同层次，不能用前者替代后者作为业务分支条件。

### 1. Assistant UI 的语义与本项目业务语义不同

Assistant UI 的 `AssistantTransport` 把前端操作抽象成 commands；`add-message` 命令包含 `parentId` 和 `sourceId`。官方文档中，`sourceId` 表示被替换的消息，编辑操作的通用语义是从历史消息处重新提交、丢弃其后的消息并产生新的分支。

本项目采用更窄的业务规则：一个 task 只允许对“最近 run”重放，run 本身已经记录 user message，重放时重置该 run 的执行结果并重新执行。因此：

- `sourceId` 是 Assistant UI 编辑器恢复原消息、构造协议 command 所需的 UI/Transport 元数据；
- `runId` 才是本项目识别一次执行、校验 task 所有权、限制“只能最近 run 重放”的领域身份；
- `parentId`/`sourceId` 不应进入 core、run、context 或 snapshot 的业务判断。

这意味着不建议强行从 Assistant UI wire command 中删除 `sourceId`：官方协议和当前前端的失败恢复逻辑都使用它。但 backend 应忽略它的值，不再用“`sourceId` 非空”判断编辑重放。前端仍可在 `prepareSendCommandsRequest` 中依据当前编辑态把最近的 `runId` 放入请求；这只是 Transport adapter 把 UI 操作映射为本项目业务命令。

### 2. `/assistant` 应采用显式判定矩阵

`/assistant` 仍是发送消息、编辑重放和业务续跑的唯一入口，建议只根据“是否有 add-message”和“是否有 runId”分类：

| 请求形态 | 业务操作 | run 约束 | executor 动作 |
|---|---|---|---|
| `add-message`，无 `runId` | 新建 run | task 可创建新 run，不能有冲突的活动 run | `start(fresh)` |
| `add-message`，有 `runId` | 编辑重放/重放最近 run | `runId` 必须存在、属于 task，且等于 task 最近 run；该 run 的 snapshot 必须有 user message | `start(fresh)`，但先 reset 该 run |
| 无 `add-message`，有 `runId` | 业务续跑 | `runId` 必须是 task 最近 run，且 run 状态满足可续跑条件（当前实现为 cancelled 等检查） | `resume` |
| 无 commands、无 `runId` | 非法请求 | 无法确定续跑对象 | 不启动 executor |
| 非法/多个消息 command | 非法请求 | 由 wire schema 拒绝 | 不启动 executor |

这里的“编辑重放”不再由 `sourceId` 命名或触发；它由 `add-message + runId` 唯一确定。`sourceId` 即使缺失也不改变这个分类。后端仍应在 command/run service 内验证最近 run，不能因为请求带了任意 `runId` 就允许重放。

### 3. `resumeApi` 是传输续接，不是业务续跑

Assistant UI 官方文档把 `resumeApi` 定义为页面刷新或重新连接时，重新订阅仍在生成的 active run；配置 `resumeStateApi` 时还要求服务端返回该活动 run 的起始 state 和 run identity。本项目的 `POST /tasks/{task_id}/assistant/attach` 正对应这个语义：

```text
Assistant UI resumeRun()
  → POST /tasks/{task_id}/assistant/attach
  → 校验 task/run/snapshot identity
  → 只订阅已有 executor 的 snapshot
  → 不创建 run、不 reset context、不 resume executor
```

用户点击“继续运行”则是另一种业务动作，由桌面端显式 POST `/assistant`，发送空 commands 和最近 `runId`；backend 进入上表第三行，执行 `resume`，完成后再由 Transport 层返回 snapshot stream。当前 `assistant-runtime.tsx` 已经把 `resumeApi` attach 与 `resumeBusinessRun` 分开，这个方向应保留并在命名、注释和测试中固定下来。

### 4. 推荐的代码编排方式（不新增文件）

在现有文件内增加一个明确的 operation 判定结果即可，不需要再建入口或代码文件：

```text
assistant_api.py
  └─ classify request by (add-message?, runId?)
       ├─ NEW_RUN
       ├─ REPLAY_LATEST_RUN
       └─ BUSINESS_RESUME
            ↓
conversation_run_command_service.py
  └─ validate task/latest-run/status/idempotency and mutate domain state
            ↓
  ConversationRunStartResult(run, initial_state, execution_mode)
            ↓
assistant_api.py starts executor when required
            ↓
transport_assistant_service.py
  └─ only builds response, subscribes snapshot, encodes stream
```

具体建议：

1. 把 `assistant_transport_request.py` 中基于 `has_edit_message` 的校验改成基于 command 是否存在和 `runId` 是否存在的结构校验；Pydantic 不判断数据库中的“最近 run”，该判断留给 command service。
2. 将 `assistant_api.py` 当前 `if command.sourceId is not None` 分支改为 `if request.runId is not None` 分支，并调用不接收 `source_id` 的重放用例。
3. 让 `ConversationRunCommandService` 统一提供新建、最近 run 重放、业务续跑三个用例结果；`TransportAssistantService.resume_run()` 中的业务资格判断和 executor 启动移回 command/run 编排层，保留它的 attach/stream/encoding 职责。
4. 保留 `sourceId` 在前端 command 中用于 Assistant UI 编辑态和失败恢复；backend 不读取其值，也不把它写入 ConversationRun、Context 或 snapshot 作为第二事实源。
5. 为四种边界补回归测试：新建、带/不带 `sourceId` 的最近 run 重放、旧 run 拒绝、空 commands 的业务续跑；另加一组 attach 测试，证明 attach 只订阅已有活动 run。

### 5. 当前实现对应的实际问题

当前 `assistant_api.py` 以 `command.sourceId is not None` 进入编辑分支，而 `assistant_transport_request.py` 也以 `sourceId` 决定 `runId` 是否允许。这与上面的业务规则冲突，已经由测试复现：`add-message + runId` 且没有 `sourceId` 会得到 `RUN_ID_WITH_MESSAGE_UNSUPPORTED`。因此这不是命名偏好，而是会拒绝合法重放请求的实际 wire/业务边界缺陷。

相反，`ConversationRunCommandService.edit_or_restart()` 已经按 `latest_run.id != run_id` 限制最近 run，且 `ConversationRunService.create_run()` 通过 `RunInitializedEvent` 创建 user message 骨架，再由 `UserInputAppendedEvent` 写入 user 文本。这个现有事实支持移除 backend 对 `sourceId` 的业务依赖；应把这条最近 run 规则保留并前移为三种操作矩阵的显式契约。

## 当前职责地图

| 文件/类 | 当前实际承担的职责 | 主要混乱点 |
|---|---|---|
| `assistant_api.py` | 请求解析、任务/workspace 校验、选择新消息/编辑/恢复、调用 command service、启动 executor、创建 Assistant response、错误映射、取消/attach/state 路由 | `/assistant` 同时编排业务流程和 Transport response；fresh/resume 的 response 创建重复；路由知道过多生命周期细节 |
| `TransportAssistantService` | snapshot queue 订阅、轮询取消/终态、controller mutation 应用、SSE 日志、attach 校验、response builder | 不再负责 executor start 或 business resume；仍保留 `resume_run` 历史实现的删除已完成，运行编排由 API/command service 负责 |
| `ConversationTaskSnapshotService` | schema validation、SQLite snapshot 读写、mutation planner 应用、fork clone、edit reset、subscriber 注册/发布、终态读取对账 | canonical snapshot owner 与 Transport controller 适配已分离；`read()` 仍包含最终一致性 recovery，`_states` 仍需后续决定是否作为有效 working copy |
| `ConversationRunCommandService` | command 幂等查询、run 创建、task active-run 检查、snapshot baseline、最近 run 编辑重放、业务续跑资格和状态迁移 | 主要编排已统一；仍可进一步把三种 operation 抽成显式 enum/value object，减少 API 条件分支 |

## 建议的整理方案

### 1. 保留 4 个文件，不新增代码文件；按“入口—用例—状态—编码”重排

目标不是再增加一层 facade，而是把现有类的边界收紧，并在已有文件内增加少量私有方法/值对象。

#### `assistant_api.py`：只做 HTTP adapter

保留 `/assistant` 作为唯一对话业务入口，但把路由收敛为 4 步：

1. 取得已经通过 Pydantic wire 校验的 command/request。
2. 调用 `ConversationRunCommandService` 的统一用例方法，得到 `run + initial_state + execution_mode`。
3. 若用例要求执行，调用 executor；executor 仍是进程内后台执行唯一入口。
4. 调用 `TransportAssistantService` 的统一 response/stream builder。

任务不存在、workspace 不匹配、command 冲突、run 不可恢复等错误继续在 API 边界映射为统一 `{error:{code,message,retryable}}`；service 不应导入 FastAPI 或 `assistant-stream`。

`attach/state/cancel` 三个路由保留，但分别只负责路径身份校验、调用对应 service 和 HTTP 错误映射。

#### `ConversationRunCommandService`：统一 command/run 用例编排

将新消息、编辑重跑、业务恢复都视为同一类“Conversation command use case”，建议统一返回一个现有 `ConversationRunStartResult` 可扩展的结果：

- `run`
- `initial_state`
- `created`/`attached`
- `execution_mode`（`fresh` 或 `resume`）

该类负责：task 级操作闸门、command 幂等、active-run 仲裁、run 状态迁移、context/snapshot/command 的事务边界，以及把 executor 启动所需的信息交给 API/执行器。它不创建 `AssistantTransportResponse`，也不操作 controller。

编辑/重放用例不需要接收 `source_id`，只在同一个 task 锁内校验：

- `run_id` 是 task 最新 run；
- 该 run 的 canonical snapshot 中存在对应 user message；
- run 的 user message 是由 `RunInitializedEvent` + `UserInputAppendedEvent` 建立的，重放时只替换该 run 的 user 输入并清空 assistant 执行结果。

`sourceId` 如果继续随 Assistant UI 命令发送，可以作为前端编辑框恢复所需的临时 UI 元数据；backend 不应使用它决定重放对象。若完全从前端命令移除，则前端需要改用自己的编辑态标记来决定是否附带最近 `runId`，但这不是 backend 领域身份。

校验失败使用现有 service 文件内定义的结构化 application/domain error，由 API 映射为稳定错误码，不把它变成 500。

#### `ConversationTaskSnapshotService`：只做 canonical snapshot owner

保留以下职责：

- snapshot schema validation；
- 基于最新 state 规划并应用 `set`/`append-text`；
- SQLite 持久化和提交后通知 subscriber；
- fork clone、edit reset 等 snapshot mutation；
- subscriber 生命周期和 `SnapshotChange` 发布。

建议将 `apply(controller, mutation)`、`_apply_snapshot_change()` 移到同文件的 `TransportAssistantService`，因为它们依赖 Assistant Stream controller，而不是 snapshot 事实。`ConversationEventProjector` 只继续调用 `apply_planned()`。

终态读取对账可以保留在 snapshot 的“read consistency boundary”，但拆成显式私有步骤：`_read_snapshot()`、`_reconcile_terminal_runs()`、`_read_with_task_operation()`，并通过依赖注入取得 run/projector；不要在 `read()` 中混写所有流程。

`_states` 若继续保留，应明确它是 working copy 并真正用于减少重复读；若 canonical 读始终来自 SQLite，就应删除该无效缓存职责。两者不能同时作为模糊的“也许会用”的第二事实源。

#### `TransportAssistantService`：只做 snapshot subscription 和 Transport encoding

保留：

- `stream()`：按 `task_id/run_id` 消费 `SnapshotChange`，处理客户端断连、task 删除、终态结束；
- subscription lifecycle logging；
- 将 mutation 应用到 `assistant-stream` controller；
- 统一构造 fresh/attach/resume 的 `AssistantTransportResponse` 和 headers。

移出：

- `start_run()`；
- business `resume_run()` 的资格判断和 executor 启动；
- 未使用的 `_projector`、`task_service` 字段。

attach 资格检查可以留作 Transport 订阅前的 run/snapshot identity guard，但业务恢复资格应归 command/run use case。所有 stream 建立路径都必须先确认 initial snapshot 的 `runId` 与目标 run 相同，不能先发错误首帧。

`/assistant` 的 command 分支建议明确按以下规则判定：有 add-message 且无 `runId` 是新消息；有 add-message 且有 `runId` 是最近 run 重放；无 add-message 且有 `runId` 是业务 resume。`sourceId` 不参与这三个 backend 分支。

### 2. 统一 edit transaction；发布必须发生在 commit 之后

现有 `start_or_attach()` 已经把 run、command、snapshot baseline 放在一个事务中；编辑路径应达到相同标准：

```text
task operation lock
  └─ one SQLite transaction
       ├─ reset ConversationRun
       ├─ delete old ContextEntry rows
       ├─ reset Task snapshot
       └─ create idempotency command
  commit
  └─ update working copy and publish one SnapshotChange
```

为此只需调整已有 service/CRUD 的可选 `session` 传递，不需要新文件。关键规则是：事务内可以准备 `SnapshotChange`，但不能在 commit 前通知 subscriber；rollback 后不能留下 working copy、seen command 或前端已收到的 pending reset。

### 3. 统一 response builder 和 run identity guard

fresh、attach、resume 当前都重复创建 `create_run(...)`、response 和两个 header。保留一个已有类内的私有 builder，例如按 `state/run_id/thread_id` 统一构造。builder 前增加强制 identity guard：

- `state.run.runId is None` 只允许 idle 首屏，不允许作为某个 run 的执行流首帧；
- `state.run.runId != requested_run_id` 直接返回结构化冲突，不发送 state；
- queued change 的 run 不匹配也应记录并结束/拒绝，不能让新旧 run 的 state 混流。

## 已复现的逻辑缺陷（本次已修复）

### [已修复][P1] 当前 wire 契约会拒绝不带 `sourceId` 的合法重放请求

历史证据：旧版 `assistant_transport_request.py:130-155` 用 `has_edit_message`（其定义完全依赖 `sourceId`）判断是否允许 `runId`；一个带 `add-message + runId` 但不带 `sourceId` 的请求实际抛出 `RUN_ID_WITH_MESSAGE_UNSUPPORTED`。一次性校验探针已得到该错误码。

这证明旧实现把 `sourceId` 错误地当成了 backend 重放模式开关。本次已改成按 `runId` 判定，并继续由 `edit_or_restart` 的 `latest_run.id != run_id` 检查保证只能重放最近 run。`RunInitializedEvent` 会在 snapshot 中先建立 user/assistant 消息骨架，随后 `UserInputAppendedEvent` 写入 user 文本；因此 backend 不需要再依赖 sourceId 来定位消息。`sourceId` 也已从 payload hash 排除。

修复验收：add-message 带最近 runId、没有 sourceId 时进入编辑重放；不带 runId 的 add-message 仍创建新 run；非最近 runId 仍被拒绝；空 commands + runId 仍走业务 resume。

### [已修复][P1] 编辑重跑非原子，command 失败会留下部分状态

证据：`conversation_run_command_service.py:195-212` 依次调用 run reset、context delete、snapshot reset，最后才创建 command；这些调用没有共享 `Session`。一次性探针把 command create 强制为失败，实际调用序列为：

```text
run → context → snapshot → command(failed)
```

探针结果：`3 passed`，其中该场景确认前三个副作用已经发生。该问题会造成 run 已被重置为 pending、context 已删除、snapshot 已发布，但 command 没有幂等记录；后续请求可能看到一个没有对应 command 的活动 run。

修复验收：编辑路径现在使用同一 session；人为让后续写入失败时事务回滚，且 commit 前不调用 snapshot publish。

### [已修复][P1] 流首帧可发送其他 run 的 snapshot

证据：`transport_assistant_service.py:95-126` 取得 initial snapshot 后直接 `yield`；只在后续 queue change（约 `191-204`）检查 `change.state["run"]["runId"]`。一次性探针请求 `run_id=1`，snapshot 实际为 `runId=2`，首个 `anext(stream)` 返回了 `runId=2`。

这不是单纯日志问题：Assistant UI 会先把新 run 的完整 state 应用到旧 run 的 Transport runtime，然后流才结束或等待后续事件，可能出现消息/状态闪烁或错误归属。

修复验收：目标 run 与 initial snapshot 不一致时，`stream()` 直接抛出 mismatch，首帧不发送；attach 的 response builder 也执行同一 identity guard。

## 本次实施后的验收结论

子 Agent 按本报告独立复验通过：

- 请求边界：新建、带/不带 `sourceId` 的最近 run 重放、空 commands 业务续跑均按 `runId` 正确分流；旧 run 被拒绝；
- 编辑事务：active-run 检查、run reset、context delete、snapshot reset、command create 共用事务，commit 后才发布；
- Transport：首帧 run identity guard 生效；attach 只订阅，不创建、reset、resume 或启动 executor；
- 结构边界：controller mutation 已归 Transport，snapshot service 仅维护 canonical snapshot。

目标测试共 `41 passed`。后端全量测试为 `167 passed, 2 failed, 10 errors`；剩余失败来自既有 `ContextUsageComputeListener` 构造/storage 初始化问题，以及 Windows pytest 临时目录权限问题，不属于本次改造范围。

## 已验证但未发现缺陷的场景

以下现有测试全部通过，当前没有证据要求重写其逻辑：

- queue 中已经有终态 change 时，终态 change 先于流结束；
- run 已在数据库终态但 snapshot 尚未投影时，流等待终态 snapshot；
- 没有 mutation 时 active run 不因 idle polling 提前结束；
- Transport 断连只注销 subscriber，不取消后台 run；
- queue 通知丢失但 snapshot 已终态时，会发送终态兜底 snapshot；
- 事件 projector 的文本、reasoning、tool lifecycle、usage 和重复 event 处理；
- Assistant request 的 commandId、thread/task、runId、model selection 和 custom command wire 校验；
- 4 个目标文件 Ruff 检查通过。

## 验证记录

### 目标范围测试

```text
uv --cache-dir H:\coding-agent\.uv-cache run pytest \
  tests/test_assistant_transport_api.py \
  tests/test_assistant_transport_request.py \
  tests/test_conversation_event_projector.py -q
41 passed in 1.41s
```

### 一次性缺陷探针

```text
uv --cache-dir H:\coding-agent\.uv-cache run pytest \
  temp/transport_audit_probe.py -q
3 passed in 1.26s
```

探针运行后已删除，不作为仓库代码保留。

### 后端全量测试

```text
uv --cache-dir H:\coding-agent\.uv-cache run pytest -q
167 passed, 2 failed, 10 errors
```

全量结果未通过，但失败项不在本次 4 个目标文件的测试范围：2 个失败来自 `ContextUsageComputeListener` 当前构造契约不匹配；10 个 error 来自其他 tool executor 测试在当前 Windows 临时目录上的权限错误。目标范围的 39 项测试仍为全通过。

## 后续建议

1. 将 `/assistant` 的三种 operation 判定进一步抽成现有文件内的显式 enum/value object，降低路由中的条件分支阅读成本。
2. 为真实 SQLite session 增加失败注入测试，覆盖事务回滚后缓存和 subscriber 均不变。
3. 单独修复全量测试中的 `ContextUsageComputeListener` 契约问题和 Windows pytest 临时目录权限问题；不应放宽 Transport 断言。

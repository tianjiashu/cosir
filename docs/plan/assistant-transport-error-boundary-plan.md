# Assistant Transport 错误边界与输入回滚改造方案

> **执行约束：** 每个任务必须先补失败测试，再实现，再运行对应测试；本项目未明确授权时不得提交 git commit。

**目标：** 修正 Assistant Transport 请求在“尚未被后端接受”时的前端状态，使新建对话不提前进入 Task 页面，普通发送和编辑重跑不把被拒绝的消息显示到对话中，并统一通过弹窗展示结构化错误，同时完整保留用户输入、图片和文件附件。

**架构：** 不新增 task-less Assistant Transport wire 模式，也不改造图片 locator。NewConversation 先通过现有 Task 创建 API 创建一个带 `creation_command_id` 的 provisional Task，但不导航；随后复用现有 Task-scoped attachment 上传和 `/assistant` 请求。`/assistant` 在 2xx 前失败时，前端使用 `taskId + creationCommandId` 调用现有 `delete_task` API 的受控清理分支；后端只在标记匹配且未形成已接受业务事实时删除 provisional Task，FastAPI schema/request parsing failure 也沿用同一前端清理路径。清理失败只记录结构化日志，不覆盖原始错误。前端只有收到 2xx 后才导航。前端引入明确的“尚未接受 / 已接受 / canonical”请求阶段，只对“尚未接受”的请求执行 optimistic 回滚。SSE 在后端接受后断开不再被误判为业务拒绝，也不删除 Task 或 Run，而是通过现有 snapshot/attach 恢复。

**技术栈：** FastAPI/Pydantic、SQLAlchemy/SQLite、React、assistant-ui external store runtime、SSE、Vitest、Playwright。

## 全局约束

总体规则
情况	历史	编辑框	处理
提交前拒绝（未收到 2xx）	不变	恢复原编辑内容	清 pending + 弹窗；New Task 另行清理 provisional Task 记录
已产生部分后端事实但未能确认 accepted	以 canonical 对账	可保留快照等待用户处理	不伪造回滚；收敛 Run + 弹窗
2xx 后 SSE 失败	不回滚	不伪造恢复	attach/snapshot 对账

- 图片附件接口不需要改；`/assistant` 失败时不补偿删除已上传图片，图片文件继续保留。
- `DELETE /tasks/{task_id}` 不负责删除图片附件：无论普通任务删除还是 provisional cleanup，都不得调用 `AttachmentService.delete()` 或 `collect_workspace_attachment_orphans()`；附件清理不属于本次 Task 删除，未来如需清理必须另行设计明确入口。
- 后端 `ConversationRunModel.status` 仍是 Run 生命周期唯一事实源。
- 前端不得把 assistant-ui optimistic message 写回后端事实；pending command 只能存在于 Transport runtime-local projection。
- HTTP/SSE 断开不自动取消业务 Run；只有明确的 business cancel 才能取消 Run。
- 错误响应继续使用 `{error:{code,message,retryable}}`；原始异常、堆栈、凭据和完整请求正文不得返回前端。
- provisional Task 容器创建是独立的容器用例；该阶段不创建 command、Run、context 或 snapshot baseline。`/assistant` 接受已有 provisional Task 后，command、Run 和数据库 context 事实仍由同一应用用例事务编排；CRUD 不承载跨表状态迁移。
- 新建流程允许先落一个 provisional Task 作为附件上传容器；它不是用户可见的成功对话，只有 `/assistant` 2xx 后才允许前端导航。失败清理必须校验 `creation_command_id`，不能按裸 `task_id` 删除。清理 provisional Task 时保留已经上传的图片；Task 删除 API 本身不删除附件。
- 本地单用户场景接受极少量清理失败后的死记录；清理失败必须有结构化日志，且不能阻塞原始错误返回。死记录不应成为正常前端导航目标。
- “消息绝不显示”严格适用于收到 2xx 之前的 request rejection。2xx 后 SSE 失败表示业务已接受，不能安全回滚 canonical facts；若产品要求该阶段也绝不显示消息，必须另增两阶段 admission/commit 协议，本方案不把它伪装成前端回滚。
- 不新增第二套业务状态机；请求阶段只属于前端 Transport 控制状态，不进入 SQLite、context 或 snapshot。
- 未经明确授权不提交 git；本方案只描述改造，不包含本次代码实施。

## 一、当前事实与根因

### 1. 生产后端已经有 Task 创建能力，Assistant Transport 只需复用已有 Task

当前仓库已有创建和删除 Task 的能力；本方案不再增加 task-less Assistant Transport：

- `TaskService.get_or_create_task()` 支持创建 Task；
- `WorkspaceService.create_task()` 暴露普通 Task 容器创建；
- `TaskModel.creation_command_id` 已有 `(workspace_id, creation_command_id)` 唯一索引，可作为 provisional Task 的来源标记；
- `WorkspaceService.create_task()` 当前只接收 workspace 和标题，需要补充可选 `creation_command_id` 透传；
- `TaskService.delete_task()` 已提供 Task 级联删除核心，可由受控 provisional cleanup 复用；附件删除不属于该核心的职责；
- child-agent 创建路径已经复用 `get_or_create_task()`。

生产 `AssistantTransportRequest.taskId` 当前是必填，且 `assistant_transport()` 进入后立即调用 `ensure_run_target()`，该方法直接读取已有 Task。因此当前 `/assistant` 实际只处理已有 Task 的 new/edit/resume。本方案保留这一契约，不把 `taskId` 改为可空。

`assistant_transport_request.py` 的注释已经描述了 `taskId is None + workspaceId` 的新建设计，但字段定义、`threadId` 校验和 service 分支尚未实现该契约。E2E 测试服务中存在 `body.taskId === undefined` 的旧创建分支，不能当作生产后端事实。

`TaskService.get_or_create_task()` 当前没有按 `(workspace_id, creation_command_id)` 查找已有 Task 的逻辑；本方案只需为 provisional cleanup 增加受控的创建标记校验，不把现有创建方法描述成 Assistant Transport 的 new-task 幂等用例。

### 2. 图片上传接口按 Task 定位 workspace，但附件文件实际存储在 workspace

前端图片 adapter 通过 `POST /tasks/{taskId}/attachments` 上传，接口借助 `taskId` 定位所属 workspace；Assistant Transport 图片 part 只接受 `cosir-attachment://...` locator。先创建 provisional Task 是复用现有接口的最小路径：图片仍按当前 Task-scoped API 上传，不新增 staged attachment、descriptor、TTL 或新的 locator 格式。图片文件实际位于 workspace 附件目录，没有 SQLite 附件元数据表；该接口按图片字节的 SHA-256 自动复用同一 asset，重试同一图片会得到同一 asset/locator。这里的幂等是内容去重语义，不新增 request-level `commandId` 上传协议，文件名等展示元数据仍沿用当前请求。provisional Task 记录清理不会删除已上传图片。

### 3. 新建页面当前先创建 Task，再异步发送首条消息

`NewConversation.submit()` 先调用 `createWorkspaceTask()`，然后通过路由 state 进入 Task 页面；Task 页面挂载后，`InitialMessageBridge` 才调用 composer send。这样 `/assistant` 失败时，页面已经完成导航，且 Task 已经落库。

### 4. 普通发送和编辑重跑会先显示 pending command

Transport runtime 在发起 fetch 前调用 `TransportFrameStore.setPendingCommands()`。HTTP 非 2xx 时，`onError` 会恢复 composer，但 `importState()` 当前不会清理 pending command，因此失败消息可能继续显示在对话里。

### 5. 当前错误反馈混合了三种语义

- 后端尚未接受命令的 HTTP 错误；
- 已接受 Run 后的 SSE 断开；
- canonical Run 的失败状态。

三者不能共用“恢复输入并删除消息”的处理。只有第一种可以安全回滚 optimistic UI。

## 二、目标行为与请求阶段契约

### 1. 请求阶段

前端 Transport runtime 为每次命令维护以下本地阶段：

```text
prepared → request-sent → accepted → canonical
                    └→ rejected
accepted ──SSE/attach 断开→ reconciling
```

阶段语义：

- `prepared`：命令已从输入框生成，但后端尚未收到有效响应；
- `accepted`：收到 2xx 响应头，后端已经完成本次 Run 的业务接受；
- `canonical`：snapshot/frame 中出现了后端事实；
- `rejected`：收到非 2xx，后端未接受本次命令；
- `reconciling`：已接受的 Run 连接断开，正在通过 snapshot/attach 对账。

### 2. HTTP 非 2xx 的统一行为

当错误发生在收到 2xx 响应头之前：

1. 清除本次 pending command；
2. 不把命令写入 canonical projection；
3. 恢复发送前的文本、图片和文件附件；
4. 弹出后端安全错误文案；
5. 保留当前页面和其它草稿状态。

### 3. 2xx 之后的断流行为

收到 2xx 后，即使 SSE 读取失败：

1. 不恢复为“发送前”状态；
2. 不删除已经显示的用户消息；
3. 不删除 Task 或 Run；
4. 读取 snapshot 或重新 attach；
5. 仅显示连接恢复状态。

这是为了避免把已经执行或已经持久化的业务命令误回滚。

### 4. 对“assistant_transport 报错”的严格边界

本方案把用户要求中的“报错且消息不能显示”定义为 **HTTP 响应尚未达到 2xx 的 request rejection**。这是当前协议能够可靠判定的失败边界。

如果产品也要求把 **2xx 之后的 SSE 断流/解析失败** 视为“消息不能显示”，当前协议无法安全满足：后端可能已经提交 command、Run 或用户消息事实，而前端不能据连接异常推断这些事实不存在。届时必须另立两阶段 admission/commit 协议，或由后端提供明确的未提交 provisional Run 事实；不能把它作为本方案的前端回滚分支。验收必须分别覆盖两种语义，不得用一个“assistant_transport error”断言混淆它们。

## 三、后端改造方案（尽量少改）

### Task 1：为新建流程增加 provisional Task 标记

**文件：**

- 修改：`apps/backend/app/api/schemas/request/CreateTaskRequest.py`
- 修改：`apps/backend/app/api/workspaces_api.py`
- 修改：`apps/backend/app/service/task/workspace_service.py`
- 修改：`apps/backend/app/task_runtime/service/task_service.py`
- 测试：`apps/backend/tests/test_task_provisional_cleanup.py`
- 测试：现有 workspace/task API 测试

**接口契约：**

- NewConversation 生成本次尝试唯一的 UUID，并同时将它作为 Task 创建请求的 `creationCommandId` 与 Assistant Transport `add-message.commandId`；
- `POST /workspaces/{workspace_id}/tasks` 接受可选 `creationCommandId`，并将其写入已有 `TaskModel.creation_command_id`；
- 前端 `CreateTaskInput` 仅新增 `creationCommandId?: string`，普通创建调用不变；
- 普通任务创建不携带该字段，行为保持不变；
- 该字段只表示“可由同一 command 在 Assistant Transport 尚未接受前清理”，不是新的 Task 状态机；
- 不修改 `AssistantTransportRequest.taskId`，不增加 `taskId=null`、`new-{commandId}` 或新的图片 locator。

**实现步骤：**

- [ ] 先补任务创建携带 `creationCommandId` 和普通创建不携带该字段的测试。
- [ ] 让 workspace service 透传 `creation_command_id`，复用已有 TaskModel 字段和 Task 创建事务。
- [ ] 在 TaskService 增加显式 `cleanup_provisional_task(task_id, creation_command_id)` 用例：校验创建标记、workspace/task 闸门和没有已接受业务事实后，复用现有 Task 树删除核心。
- [ ] 让现有 `DELETE /tasks/{task_id}` 支持带 `creationCommandId` 的受控 provisional cleanup 分支；该分支只允许删除匹配标记的临时 Task，不允许把它当作普通 Task 删除的绕过参数。
- [ ] 复用 Task 树删除核心，并明确 Task 删除 API 的统一附件边界：跳过 `collect_workspace_attachment_orphans()`，不调用 `AttachmentService.delete()`；不为 Task 删除增加任何附件补偿删除分支。
- [ ] 清理失败使用后端结构化日志和前端 `frontendLog` 记录 `task_id`、`command_id` 和原因，不覆盖原始 Assistant Transport 错误；已上传图片不作为清理失败的补偿对象。
- [ ] 运行后端任务创建/清理测试，确认普通 Task 的数据库删除行为和已有 Task 不受受控清理影响，并确认 Task 删除不触发附件删除。

### Task 2：通过现有 delete_task API 受控清理 provisional Task

**文件：**

- 修改：`apps/backend/app/api/tasks_api.py`
- 修改：`apps/backend/app/task_runtime/service/task_service.py`
- 修改：`apps/desktop/lib/api/workspaces.ts`
- 测试：`apps/backend/tests/test_task_provisional_cleanup.py`
- 测试：`apps/backend/tests/test_tasks_api.py`

**核心规则：**

- `AssistantTransportRequest` 保持现有 `taskId` 必填、`threadId=task-{taskId}` 和现有图片 locator；不新增 task-less wire schema。
- `/assistant` 不负责 provisional Task 清理，保持现有 Assistant Transport 的错误映射、Run 收敛和 SSE 生命周期边界。
- 只有 NewConversation 持有 `creationCommandId` 的 provisional 流程，才可在 Assistant Transport 收到非 2xx、请求失败或附件预上传失败时调用现有 `DELETE /tasks/{task_id}`；普通 Task 发送和编辑重跑不得调用该删除 API。不允许裸 `taskId` 清理 provisional Task。
- delete_task API 在带 `creationCommandId` 时进入受控清理分支：必须满足 `Task.creation_command_id == creationCommandId`，且不存在已接受的其它 command/Run/canonical 用户消息；否则拒绝清理，防止网络竞态或错误重试删除正式 Task。
- provisional Task 清理只删除 Task、Run、command、context 等数据库事实和进程内 Task runtime；不调用 `AttachmentService.delete()`，也不执行 `collect_workspace_attachment_orphans()`。已经上传的图片文件保持存在；附件清理不属于本次 Task 删除。
- `/assistant` 返回 2xx 后，前端不得再调用该删除分支；SSE 断开、客户端断开或 workflow 失败不删除 Task/Run，只按现有状态和 snapshot/attach 机制处理。
- 清理成功不改变前端原始错误展示；清理失败只记录结构化日志并保留原始错误。允许本地单用户环境留下少量死记录，但不能让它们成为当前导航目标。

**实现步骤：**

- [ ] 先补 delete_task API 的 provisional cleanup 测试：匹配标记可删除、已有 Task 不删除、同 Task 存在已接受 Run/command 时拒绝删除。
- [ ] 补测试确认普通 Task 删除和 provisional cleanup 都不调用附件删除/孤儿 GC，已上传图片仍保留。
- [ ] 补前端测试确认 Assistant Transport 非 2xx、网络失败和附件预上传失败都会 best-effort 调用带 `creationCommandId` 的 delete_task API。
- [ ] 补测试确认清理失败不覆盖原始 Assistant Transport 错误，并记录结构化日志。
- [ ] 明确 `/assistant` 不包清理逻辑、不使用无条件 `finally` 删除 Task；前端只对未 accepted 的请求执行清理。
- [ ] 运行 `cd apps/backend && pytest tests/test_task_provisional_cleanup.py tests/test_tasks_api.py -q`。

### Task 3：收紧 Assistant Transport 错误映射

本任务不是 provisional Task 清理，也不改变 `/assistant` 的 Task 创建、附件、导航或 accepted/SSE 语义；只修正当前已有的安全错误契约问题。若某项契约已经满足，则只保留对应测试，不新增功能分支。

**文件：**

- 修改：`apps/backend/app/assistant_transport/assistant_api.py`
- 修改：`apps/backend/app/assistant_transport/service/transport_assistant_service.py`
- 修改：`apps/backend/app/assistant_transport/request/assistant_transport_request.py`
- 测试：`apps/backend/tests/test_assistant_transport_api.py`

**规则：**

- 保留统一错误体 `{error:{code,message,retryable}}`；
- 将 `ValueError` 的字符串推断替换为显式领域错误或稳定错误映射；
- `RUN_START_FAILED` 不得把 `str(exc)` 返回前端；当前 `assistant_api.py` 的 executor 启动失败分支需要改为稳定的安全文案，原始异常只写后端日志；
- 原始异常只通过 `log.exception(..., extra={...})` 记录；
- 新建流程失败至少覆盖 `TASK_NOT_FOUND`、`MODEL_SELECTION_REQUIRED`、`MODEL_NOT_SUPPORTED`、`ATTACHMENT_UNAVAILABLE`、`TASK_BUSY`、`RUN_START_FAILED`；provisional cleanup 失败不能替换这些原始错误；
- 只有确定“请求尚未接受”的错误才标记为前端可回滚；`retryable` 仍只表达业务条件是否可重试，不表达 SSE 是否已经接受。

**实现步骤：**

- [ ] 为每个错误码补 HTTP status、用户文案和 retryable 断言。
- [ ] 补测试确认响应体不含异常堆栈、provider 原始响应或附件绝对路径。
- [ ] 修正 `assistant_transport_request.py` 中仍描述 `taskId=None`/task-less 的注释和 docstring，使其与 taskId 必填事实一致；不因此新增 task-less wire 模式。
- [ ] 运行 `cd apps/backend && pytest tests/test_assistant_transport_api.py -q`。

## 四、前端改造方案

### Task 4：抽取可区分 accepted/rejected 的 Transport 请求层

**文件：**

- 修改：`apps/desktop/lib/assistant/use-task-assistant-transport-runtime.ts`
- 修改：`apps/desktop/lib/assistant/transport-frame-store.ts`
- 修改：`apps/desktop/components/assistant/runtime/use-runtime-transport.ts`
- 修改：`apps/desktop/components/assistant/runtime/runtime-types.ts`
- 测试：`apps/desktop/tests/unit/transport-frame-store.test.ts`
- 测试：`apps/desktop/tests/unit/use-task-assistant-transport-runtime.test.ts`

**接口：**

将当前 `onError(error, {commands, updateState})` 扩展为带明确阶段的回调：

```ts
type TransportFailurePhase = "request-rejected" | "accepted-stream-failed";

type TransportErrorParams = {
  commands: readonly unknown[];
  phase: TransportFailurePhase;
  updateState: (updater: (state: TransportState) => TransportState) => void;
  clearPendingCommands: () => void;
};
```

`openStream()` 内部使用局部 `accepted = false`：

- `response.ok === false`：调用 `onError(..., phase: "request-rejected")`；
- `onResponse()` 执行后：设置 `accepted = true`；
- 之后 `readSse()` 抛错：调用 `onError(..., phase: "accepted-stream-failed")`。

**状态规则：**

- `request-rejected` 必须清除本次 pending command；
- `accepted-stream-failed` 不得清除已可能成为 canonical 的用户消息；
- `importState()` 不能在普通 snapshot 对账时无条件清除 pending，必须由 full frame 或明确的 reject/accept barrier 清理；
- 失败恢复必须按 command identity 处理，不能清除其它并发或后续命令；
- 任何恢复都不能把 optimistic message 写入 Transport snapshot。

**实现步骤：**

- [ ] 先补 store 测试：HTTP 拒绝后 pending 消失且 canonical history 不变；已接受后导入 snapshot 不误删 canonical 消息；编辑命令失败不产生重复用户消息。
- [ ] 运行 `cd apps/desktop && npm run test:unit -- tests/unit/transport-frame-store.test.ts tests/unit/use-task-assistant-transport-runtime.test.ts`。
- [ ] 修改 stream lifecycle，使错误阶段由响应头 barrier 决定，而不是由异常类型猜测。
- [ ] 将 `useRuntimeTransport.handleSendError()` 拆为 request-rejected 与 accepted-stream-failed 两条路径。
- [ ] request-rejected 路径恢复 composer；accepted-stream-failed 路径只触发 snapshot/attach recovery。
- [ ] 重新运行上述单测。

### Task 5：新建对话使用 provisional Task，accepted 之前不导航

**文件：**

- 修改：`apps/desktop/components/new-conversation.tsx`
- 修改：`apps/desktop/components/workspace-shell.tsx`
- 修改：`apps/desktop/lib/api/workspaces.ts`
- 新增或抽取：`apps/desktop/lib/assistant/submit-assistant-transport.ts`（非 React 的 request/accepted barrier；由现有 runtime hook 与 NewConversation 共同复用）
- 复用：`apps/desktop/lib/assistant/attachments/image-attachment-adapter.ts`
- 新增：`apps/desktop/components/assistant/transport-error-dialog.tsx`
- 修改：`apps/desktop/tests/e2e/test-server.mjs`（删除旧 task-less 创建分支，要求 `taskId`；测试服务必须与生产 Assistant Transport 契约一致）
- 测试：`apps/desktop/tests/e2e/new-conversation.spec.ts`

**流程：**

当前 `createWorkspaceTask()` 不再触发立即导航，但仍作为 provisional Task 分配步骤。NewConversation 应：

1. 固定保存当前输入框、图片、文件和工具组选择的不可变发送快照；
2. 生成本次尝试唯一的 UUID，同时作为 `creationCommandId` 和首个 `add-message.commandId`；
3. 调用现有 `createWorkspaceTask()`，携带 `creationCommandId`，得到 provisional `taskId`，但不导航；
4. 复用已有 Task-scoped attachment adapter，以该 `taskId` 上传图片；文件继续使用当前本机文件引用；
5. 从发送快照构造完整 `add-message + ban-tools` 命令，使用 `threadId=task-{taskId}` 调用共享的非 React Assistant Transport submitter；
6. submitter 收到 2xx 且确认 `X-Cosir-Task-Id` 与 provisional `taskId` 一致后，停止读取当前 SSE subscription 并返回 accepted 结果；不得调用 business cancel；
7. 只有 accepted 结果返回后才导航到 `/tasks/{taskId}`；Task 页面随后通过现有 snapshot/attach 重新订阅同一 Run；
8. 导航时不再通过 `initialMessage`/`initialAttachments` 发送第二次首条消息。

**失败行为：**

- 非 2xx：停留在 NewConversation；
- 文本 state 不变；
- 图片对象、文件附件和工具组选择不变；
- Assistant Transport 非 2xx 或请求失败：前端调用带 `creationCommandId` 的 delete_task API，后端受控删除 provisional Task 记录但不删除图片；如果清理失败，前端仍停留当前页面并保留输入，前后端记录结构化日志；
- FastAPI schema/request parsing 非 2xx：虽然 `/assistant` 未进入 route，前端仍可使用已保存的 `taskId + creationCommandId` 调用受控 delete_task API；如果删除请求也失败，允许留下少量死记录；
- `submitting` 解除；
- 使用 `TransportErrorDialog` 显示安全错误文案；
- Assistant Transport 非 2xx 由共享 submitter best-effort 调用带 `creationCommandId` 的 delete_task API；后端 provisional cleanup 只清理 Task 业务记录，不删除已经上传的图片。Assistant Transport 之前的附件上传失败也复用同一清理路径，仍保留已落盘图片，不影响输入恢复。普通 Task 发送和编辑重跑只回滚前端 pending，不调用 delete_task。

**协议异常：**

- 2xx 但缺少 `X-Cosir-Task-Id` 或 header 与 provisional `taskId` 不一致视为 Transport protocol error；
- 该错误必须记录 trace、task_id 和 creation_command_id 诊断信息并弹窗；
- 该情况已经收到 2xx，不能再按 request rejection 删除 Task；前端不导航，后端 Run 通过既有事实和日志继续排查；
- 后端测试必须保证成功 response 一定携带该 header，且前端不能导航到不匹配的 Task。

**共享 submitter 边界：**

- submitter 只负责 fetch、响应头 accepted barrier 和订阅关闭，不拥有 React 页面状态，也不调用 business cancel；
- 2xx 后关闭 SSE 只是订阅断开，后端 Run 继续执行；Task 页面挂载后通过 attach 恢复；
- HTTP 非 2xx 或响应头校验失败返回结构化错误，NewConversation 保持本地快照；
- 现有 `use-task-assistant-transport-runtime` 复用同一阶段判定，避免 NewConversation 和 TaskPage 各自实现一套 accepted 语义。

**实现步骤：**

- [ ] 先补 E2E：创建 provisional Task 后 `/assistant` 返回 409 时 URL 不变、弹窗可见、文本和图片附件仍在，前端调用带 `creationCommandId` 的 delete_task API。
- [ ] 运行该 E2E，确认现状会先进入 Task 页面；改造后只在 2xx 且 Task header 匹配时导航。
- [ ] 删除 `test-server.mjs` 中旧的 task-less 创建分支，测试服务统一要求 `taskId`，避免 E2E 掩盖生产协议错误。
- [ ] 补图片上传失败时的 best-effort provisional cleanup 测试；确认原始图片仍在 NewConversation composer。
- [ ] 复用现有 Assistant command 序列化规则，确保图片、文件和 ban_tools 与 Task runtime 一致。
- [ ] 补 2xx 后 submitter 关闭 SSE、TaskPage attach 且后端 Run 不被取消的测试。
- [ ] 将成功导航条件绑定到 accepted response，而不是 Task 创建成功。
- [ ] 接入共享错误弹窗并保留当前表单 state。
- [ ] 运行新建对话完整 E2E。

### Task 6：普通发送与编辑重跑只对未接受请求回滚

**文件：**

- 修改：`apps/desktop/components/assistant/runtime/use-runtime-transport.ts`
- 修改：`apps/desktop/components/assistant/runtime/assistant-runtime-session.tsx`
- 修改：`apps/desktop/components/assistant/assistant-runtime.tsx`
- 修改：`apps/desktop/components/assistant/runtime/assistant-runtime-bridges.tsx`
- 修改：`apps/desktop/components/assistant-ui/elements/thread.aui.tsx`（仅在需要传递错误展示入口时修改）
- 使用：`apps/desktop/components/assistant/transport-error-dialog.tsx`
- 测试：`apps/desktop/tests/e2e/new-conversation.spec.ts`
- 测试：`apps/desktop/tests/e2e/frontend-regressions.spec.ts`

**普通发送：**

- request-rejected：清除 pending，恢复发送前的文本和附件，canonical message 数量不变，弹窗显示错误；
- accepted-stream-failed：保留已接受命令，执行 snapshot/attach recovery，不恢复为旧草稿；
- 顶部草稿与本次发送命令必须分别保存，失败恢复不能覆盖用户在发送期间形成的新草稿。

**编辑重跑：**

- request-rejected：原历史消息不变；重新打开对应 message-level edit composer；恢复编辑文本、图片和文件；顶部 composer 草稿保持不变；弹窗显示错误；
- accepted-stream-failed：不把编辑操作当作未提交，等待 canonical snapshot 对账；
- sourceId 只用于前端恢复编辑状态，后端仍以 runId 和 canonical 最近 Run 规则判定编辑资格。

**实现步骤：**

- [ ] 先补 E2E：普通发送 409 时对话不出现新用户消息，输入框及图片附件保持不变。
- [ ] 补 E2E：编辑重跑 409 时历史消息不变，编辑框恢复，顶部草稿不被覆盖。
- [ ] 将现有编辑失败 E2E 的 `role="status"` 错误断言改为共享 TransportErrorDialog 的弹窗断言。
- [ ] 补 E2E：2xx 后主动断开 SSE 时消息不回滚，最终 snapshot 可恢复。
- [ ] 将现有 `handleSendError()` 的“恢复 composer”逻辑限制在 `request-rejected` 阶段。
- [ ] 将弹窗事件与 `TransportStatus` 分离：command reject 走弹窗，连接恢复走状态条。
- [ ] 运行 `cd apps/desktop && npm run test:e2e -- tests/e2e/new-conversation.spec.ts tests/e2e/frontend-regressions.spec.ts`。

## 五、测试验收矩阵

| 场景 | 后端结果 | 页面 | 对话消息 | 输入/附件 | 错误展示 |
|---|---|---|---|---|---|
| New Task route-level Assistant 请求被拒绝 | 非 2xx，未接受；前端调用受控 delete_task，清理 provisional Task 记录并保留图片 | 留在 NewConversation | 无 | 文本、图片、文件、工具选择不变；已上传图片仍存在 | 弹窗 |
| New Task schema/request parsing 失败 | 非 2xx；前端仍用 `taskId + creationCommandId` 调用受控 delete_task，失败则允许留死记录 | 留在 NewConversation | 无 | 文本、图片、文件、工具选择不变 | 弹窗 |
| New Task 附件预上传失败 | Assistant 尚未调用；best-effort 清理 provisional Task 记录，保留已落盘图片 | 留在 NewConversation | 无 | 原始图片仍在 composer；已上传图片不补偿删除 | 弹窗 |
| New Task 已接受后 SSE 断开 | 2xx，Run 已创建 | 进入 Task | 保留已接受事实 | 不回滚 | 状态条 + 对账 |
| Task 普通发送被拒绝 | 非 2xx，未创建 Run | 留在当前 Task | 不新增用户消息 | 输入和附件恢复 | 弹窗 |
| Task 普通发送已接受后断流 | 2xx，Run 已创建 | 留在当前 Task | 不删除已接受消息 | 不误恢复旧草稿 | 状态条 + 对账 |
| 编辑重跑被拒绝 | 非 2xx，旧 Run 不变 | 留在当前 Task | 历史消息不变 | 编辑内容和附件恢复，顶部草稿不变 | 弹窗 |
| 编辑重跑已接受后断流 | 2xx，Run 已重置/创建 | 留在当前 Task | 以 canonical 为准 | 不做伪回滚 | 状态条 + 对账 |
| 已有 Task 重复 commandId | 复用现有 Assistant command 幂等规则 | 留在当前 Task | 不重复 | 不丢输入 | 无重复错误 |
| provisional cleanup 失败 | 保留原始 Transport 错误并记录清理失败 | 留在 NewConversation | 无 | 输入不变 | 弹窗 |

## 六、实施顺序与边界

建议按以下顺序实施，每一步都有独立测试边界：

1. provisional Task 标记透传与受控清理；
2. 前端失败清理、delete_task API 受控校验和安全错误映射；
3. 前端 accepted/rejected Transport 阶段和 pending 清理；
4. NewConversation provisional Task 创建、附件预上传和 accepted 后导航；
5. 普通发送/编辑重跑回滚与共享弹窗；
6. 完整 E2E 和日志验收。

不做以下改造：

- 不新增独立的前端业务消息队列；
- 允许前端在未 accepted 的 Assistant Transport 失败路径调用带 `creationCommandId` 的 Task 删除 API；`DELETE /tasks/{task_id}` 只删除受控的 Task 业务记录，不删除图片附件，也不触发附件孤儿 GC；
- 不新增 task-less Assistant Transport wire 模式、staged attachment descriptor 或新的图片 locator；
- 不把 Assistant UI message 直接持久化到后端；
- 不修改 canonical snapshot 结构来携带前端 pending 状态；
- 不把 SSE 断开一律视为请求失败；
- 不把后端原始异常直接展示给用户。

## 七、自检结论

- “新建 Task 失败不导航”由 Task 5 覆盖；
- “输入文本/图片/文件不变”由 Task 5、Task 6 的 composer 快照与附件测试覆盖；
- “普通发送失败不显示消息”由 Task 4、Task 6 的 pending 清理和 E2E 覆盖；
- “编辑重跑失败不修改历史且恢复编辑框”由 Task 6 覆盖；
- “后端改动尽量少”通过复用现有 Task 创建、Task-scoped attachment、`/assistant`、Run/Command/context 事务、`TaskModel.creation_command_id` 和 Task 删除 API/核心；新增范围仅是 provisional 标记透传、delete_task 受控清理分支和前端失败调用；`/assistant` 不增加清理职责；
- “不把 accepted 断流误回滚”由请求阶段契约和断流 E2E 覆盖；
- “new task 图片不丢失”由 provisional Task 在导航前承载现有附件上传、前端本地 composer 快照和失败恢复测试覆盖；
- 如果验收标准把 accepted 后 SSE 失败也定义为“消息不得显示”，本方案判定为协议需求未决，不宣称现有改造已经满足；
- 不需要新增 Transport snapshot 事实字段，也不需要引入新的持久化状态机。

# Assistant Transport 三种 Run 模式的 Command 方案报告

日期：2026-09-09

状态：推荐方案已落地第一版；custom command 方案仍为备选。桌面端后端启动链的循环导入与重试取消问题已修复。

本次落地内容：`/assistant` 增加 API 边界的 `new/edit/resume` 分类函数；
`ConversationRunStartResult` 增加业务 `mode` 字段，并区分 `mode="edit"` +
`execution_mode="fresh"`；补充分类与 resume 回归测试。现有 Assistant UI wire 形态保持兼容。

启动修复：`delegate_task` 改为由 `ToolSystem` 注入 Agent 摘要，避免配置层与工具 handler
循环导入；backend supervisor 的失败重试不再复位关闭状态或重复推进 generation，并在 spawn
失败后重新检查生命周期状态。

## 结论

可以通过 Assistant Transport 的 command batch 形态和 `runId` 区分“新建 run、编辑重跑、中断续跑”；但 Assistant UI 的 `command.type` 本身不能区分新建与编辑，因为二者都是 `add-message`。不建议把三种模式都改造成新的 `command.type`。更稳妥的方案是：

1. 保留 Assistant UI 原生的 `add-message`：它天然对应发送新消息，也天然承载编辑消息。
2. 以“是否存在 `add-message`”和根级 `runId` 形成后端明确的三路判别矩阵。
3. 在 API 边界把矩阵归一化为显式的 `RunCommandMode`（`new` / `edit` / `resume`），再交给 service；不要让 `sourceId` 充当领域模式开关。
4. `resumeApi` 的重新订阅仍与业务“中断续跑”分开；前者只 attach 已有执行，后者才恢复 cancelled run。

这里的“通过 command”是项目在 Transport 边界上的归一化，不是声称 Assistant UI 原生提供 `new/edit/resume` 三种 command type。这样既能显式记录业务模式，又不破坏 Assistant UI 的 Composer、编辑、乐观消息和取消生命周期。

## 运行边界与事实来源

本功能运行在 Tauri 管理的本机 FastAPI/uvicorn backend 进程中。桌面 WebView 通过动态 loopback 地址请求 backend；HTTP/Assistant Transport 是本机进程边界，不是公网服务。

持久事实在本地 SQLite：

- `conversation_runs`：一次 Agent 执行、输入、模型、状态和终止原因；
- `conversation_commands`：`(task_id, command_id)` 幂等占用、`command_type` 和关联 `run_id`；
- conversation context、canonical message 和 task snapshot：由 backend service/core 维护。

Tauri 负责 backend 的启动、停止和重启。backend 启动时通过 `recover_orphaned_runs` 收敛遗留的 `pending/running` run；前端重新读取 snapshot 后，只能由用户明确点击“继续运行”触发业务 resume。backend 崩溃不应由前端 state 猜测为继续执行。

## Assistant UI 约束

Assistant UI 官方 [Assistant Transport 文档](https://www.assistant-ui.com/docs/runtimes/custom/assistant-transport) 定义：

- 请求包含 `commands`、`threadId`、可选 `parentId`、`state`、`config` 等 envelope；
- 内建 `add-message` 和 `add-tool-result`，也允许通过 `useAssistantTransportSendCommand` 定义 custom command；
- `capabilities: { edit: true }` 开启编辑；编辑产生的 `add-message` 带 `parentId` 和 `sourceId`；
- `resumeApi` 用于刷新/断线后重新连接仍在生成的 run，不等同于业务层重新执行；
- custom command 遵循同一 command queue、in-flight、error/cancel 生命周期。

因此 `sourceId` 是 Assistant UI 的消息替换元数据，`runId` 是本项目的 Conversation Run 身份，两者不能互换。
官方 `resumeRun` 的请求形态由 runtime/adapter 决定；本项目约定的“空 `commands` + `runId` → 恢复 cancelled Run”是项目自己的业务协议，不应描述为 Assistant UI 官方 resume 语义。

官方编辑处理是：以 `parentId` 为截断边界，丢弃其后的消息，再追加编辑后的用户消息。本项目目前采用更窄的业务策略：只允许编辑 task 的最近 Run，并原地重置该 Run（保留原 `run.id`，但重新执行；不是创建新的 Conversation Run）。这是有意的项目特化，不是 Assistant UI 的通用分支/编辑实现。若继续采用该策略，service 至少应在 `runId` 目标校验时核对：有值的 `sourceId`/`parentId` 必须指向该最近 Run 的用户消息；是否允许二者缺失的直接 API 重放，需要作为明确的项目契约，而不能默认为官方语义。

## 当前代码事实

### Backend

`apps/backend/app/assistant_transport/assistant_api.py` 当前已经按以下事实分流：

- `add-message` 且无 `runId` → `start_or_attach`，创建 fresh run；
- `add-message` 且有 `runId` → `edit_or_restart`，校验 task 最近 run，原地重置该 run 后 fresh 执行；不会创建新的 Conversation Run id；
- 无 command 且有 `runId` → `resume_latest_run`，只允许当前 task 最近的 cancelled run；
- `/tasks/{task_id}/assistant/attach` → 只重新订阅已有本地执行，不触发业务 resume；
- `/runs/{run_id}/cancel` → 唯一的显式取消入口。

`ConversationRunCommandService` 已分别存在 `start_or_attach`、`edit_or_restart`、`resume_latest_run` 三个用例；`ConversationRunStartResult.execution_mode` 目前只描述执行方式（`fresh` / `resume`），不能完整表达“新建还是编辑”。`conversation_commands.command_type` 目前保存的是 Transport command 类型，编辑与新建都可能是 `add-message`。

### Frontend

`apps/desktop/components/assistant/assistant-runtime.tsx` 已配置 `capabilities: { edit: true }`、`api=/assistant` 和独立 `resumeApi=/tasks/{taskId}/assistant/attach`。当前业务继续运行使用原始 `POST /assistant`，发送空 `commands` 与 `runId`，随后调用 transport attach。

`apps/desktop/components/assistant-ui/elements/thread.aui.tsx` 使用：

- Assistant UI 的 `ActionBarPrimitive.Edit` 进入编辑；
- `isEditableLatestRunUserMessage` 限制只能编辑最近 run 的用户消息；
- cancelled 且有用户消息时显示“继续运行”；
- `StopButton` 先调用 `/runs/{runId}/cancel`，成功后才触发 Assistant UI 的本地 Cancel。

当前 adapter 在 `prepareSendCommandsRequest` 中仍通过 `sourceId` 判断 `hasEditCommand`，再决定是否注入当前 `runId`。这与 backend 已采用的“`add-message + runId` 即重放”规则不完全一致：`sourceId` 缺失时，前端可能无法把一个合法的编辑/重放请求标成带 `runId` 的请求；但也不能简单地给所有 `add-message` 都注入当前 `runId`，否则普通新消息会被 backend 当成编辑重放。前端必须保留可靠的编辑态判定，并在编辑态同时处理 `sourceId`/`parentId` 与目标 `runId`。

## 推荐方案：Command 形态 + Run 身份判别

### 判别矩阵

| commands | `runId` | 归一化模式 | 业务用例 | executor |
|---|---:|---|---|---|
| 一个 `add-message` | 无 | `new` | `start_or_attach` | `fresh` |
| 一个 `add-message` | 有 | `edit` | `edit_or_restart`（原地重置既有 Run） | `fresh` |
| 空数组 | 有 | `resume` | `resume_latest_run` | `resume` |
| 空数组 | 无 | 非法 | 返回 `RUN_ID_REQUIRED` | 不启动 |
| 其他/多个业务 command | 任意 | 非法或后续扩展 | schema/service 拒绝 | 不启动 |

这里的“通过 command”是指通过 Transport command batch 的形态和其关联的 run 身份判别；不是把 Assistant UI 的原生 `add-message` 替换成三个不兼容的自定义 command。严格说，`new` 与 `edit` 的差异来自 `add-message` 是否带项目约定的目标 `runId`，而不是来自不同的 Assistant UI `type`。

### 建议的内部类型

在 request/service 边界增加一个明确的值对象或类型别名（放在现有 request/service 文件即可）：

```text
RunCommandMode = "new" | "edit" | "resume"

RunCommandIntent {
    mode: RunCommandMode
    command: AddMessageCommand | None
    run_id: int | None
}
```

建议增加 `classify_run_command(request)`，只做纯 wire 判别；数据库相关约束仍由 `ConversationRunCommandService` 负责。`ConversationRunStartResult` 增加 `mode` 或 `command_mode`，并保留已有 `execution_mode`：

```text
mode: new | edit | resume       # 业务语义与审计
execution_mode: fresh | resume  # AgentRuntime 执行方式
```

这样 API 不再重复解释 `sourceId`，日志、命令记录、测试和未来 UI telemetry 都能直接记录三种业务模式。

### 必须保持的边界

- `sourceId` / `parentId` 继续留在 Assistant UI wire command 中，用于编辑 UI 和失败恢复；backend 不用它们替代 `runId` 选择 run，也不写入 context/snapshot 作为第二事实源。但在项目特化的“只编辑最近 Run”策略下，若字段存在必须校验它们与目标 Run 的用户消息一致；否则直调 API 可能形成错误编辑目标。若明确允许字段缺失的重放，则要把它记录为项目扩展，而非 Assistant UI 官方编辑语义。
- `runId` 只作为请求目标身份，service 必须继续校验它属于 task、是最近 run、状态和 snapshot 满足对应资格；不能相信客户端带来的任意 ID。
- 新建和编辑都使用 `add-message` 的 Assistant UI 原生 optimistic message；converter 继续从 `pendingCommands` 渲染 pending 用户消息。
- 业务 resume 不应依赖 `resumeApi`。`resumeApi` 仅 attach；用户 Continue 才调用 `/assistant` 的 resume 用例。
- cancel 仍由 `/runs/{run_id}/cancel` 负责，Transport HTTP 断连和 Assistant UI Cancel 不能直接改变数据库 Run 状态。

## 是否要使用显式 custom command

如果产品明确要求请求日志或 wire 中出现可读的 `command.type`，Assistant UI 支持如下 custom command 方向：

```text
cosir-start-run   { message, providerId, modelName, ... }
cosir-edit-run    { runId, message, sourceId?, ... }
cosir-resume-run  { runId }
```

但这是第二方案，不是当前首选，原因如下：

1. `ComposerPrimitive.Send` 和 `ActionBarPrimitive.Edit` 默认产生 `add-message`；改成 custom command 需要重写发送和编辑 UI，不能只改 Python schema。
2. custom command 需要在 `@assistant-ui/react` 中做 module augmentation，并由 `useAssistantTransportSendCommand` 发出；当前 backend 的 `CustomCommand` 只有通用 `type/name/payload`，且 validator 当前明确拒绝 custom command。
3. backend 必须为 custom command 定义严格的 discriminated union，不能保留 `extra=allow` 的宽泛 payload；否则会削弱 wire 契约和审计能力。
4. 当前 `resume` 使用空 command，没有新的 `commandId` 持久记录；若改用 `cosir-resume-run`，应为 resume 也生成稳定 `commandId`，纳入幂等 hash，并决定是否把每次用户 Continue 作为 `conversation_commands` 记录。
5. `payload_hash`、command 错误恢复、optimistic state、onError/onCancel 以及 e2e 测试都需要同步扩展。

如果未来要采用 custom command，建议只先引入 `cosir-resume-run`：它能消除“空 command 表达业务动作”的歧义，同时不影响新建和编辑的原生 `add-message`。不建议一开始就把三种操作全部迁移到 custom command。

## 实施步骤

### P0：固定当前兼容契约

- 后端新增纯函数/值对象，把三路判别集中到一个位置；API 只消费判别结果。
- 修正 frontend `prepareSendCommandsRequest`：编辑是否带 `runId` 不应由 `sourceId` 决定；编辑态应由当前 runtime 的目标 run 绑定，`sourceId` 仅保留为 UI 元数据。
- `payload_hash` 明确包含会改变业务执行的 message、runId、模型选择；`commandId` 继续排除。`parentId`/`sourceId` 只有在“`runId` 已唯一确定最近 Run，且 service 已校验二者一致或明确允许缺失”的项目策略下才能排除；未来支持任意历史分支时必须纳入 hash 或做等价一致性校验。
- 为 `ConversationRunStartResult` 增加 `mode`，日志和持久化命令类型中明确区分 `new`、`edit`、`resume` 的审计含义。若不改表结构，至少在服务日志和结果对象保留该字段。

### P1：后端契约与幂等测试

覆盖：

- `add-message` 无 `runId` 创建新 run；
- `add-message` 有最近 `runId`，有/无 `sourceId` 都能编辑重跑；
- 非最近 `runId` 被拒绝；
- 空 commands + `runId` 只恢复 cancelled 最近 run；
- attach 不创建、不 reset、不 resume、不 cancel；
- 同一 `commandId` 重试不创建第二个 run，payload 变化返回冲突；
- 编辑路径 run/context/snapshot/command 事务失败时全部回滚；
- 两个 Continue 请求并发时只有一个有效恢复。

### P2：前端行为与 e2e

- 记录每次请求的 `mode`（由 adapter 计算或由后端响应/日志确认），但不把 snapshot state 回传为事实源；
- 验证普通发送、编辑重跑、Continue、页面刷新 attach、用户 Stop 五条路径；
- 验证断开 stream 不会触发 cancel，backend 重启后只显示可手动恢复的 cancelled 状态；
- 验证失败重试时 `commandId`、pending message 和编辑草稿不会错配。

## 最终建议

短期采用“原生 `add-message` + `runId` 判别 + 内部显式 `RunCommandMode`”方案。它与现有代码、Assistant UI 文档和本地单用户架构最一致，改动集中在请求分类、结果对象、adapter 和测试，不改变 Transport wire 的核心语义。

只有在产品确实要求“请求 JSON 的 command type 必须直接显示 `new/edit/resume`”时，再引入 custom command；届时优先只迁移业务 resume，并保留新建/编辑的 Assistant UI 原生 `add-message`。

## 参考

- [Assistant UI Assistant Transport](https://www.assistant-ui.com/docs/runtimes/custom/assistant-transport)
- [Assistant UI Assistant Transport API Reference](https://www.assistant-ui.com/docs/api-reference/transport/assistant-transport)
- [Assistant UI Runtime Architecture](https://www.assistant-ui.com/docs/runtimes/concepts/architecture)
- `apps/backend/app/assistant_transport/assistant_api.py`
- `apps/backend/app/assistant_transport/request/assistant_transport_request.py`
- `apps/backend/app/assistant_transport/service/conversation_run_command_service.py`
- `apps/backend/app/storage/model/conversation_command_model.py`
- `apps/desktop/components/assistant/assistant-runtime.tsx`
- `apps/desktop/components/assistant-ui/elements/thread.aui.tsx`
- `apps/desktop/lib/assistant/conversation-actions.ts`

# Conversation Task Snapshot：Task Context 与 Run Scope 设计

## 1. 目的

本文定义 Assistant Transport snapshot 的下一版结构，解决以下问题：

- 历史 Run 的 token usage 在新 Run 开始后无法继续显示；
- Task 级上下文窗口占用与单个 Run 的模型 token 消耗语义混淆；
- 平铺的 `messages`、当前 `run` 和顶层 `usage` 容易在事件投影时发生错配；
- tool、message、Run 状态事件缺少统一的 Run 所属边界。

本文是结构化改造设计，不是只调整前端显示条件的补丁方案。

## 2. 架构边界

本项目是单用户、本机运行的桌面 Agent：

```text
Tauri Rust 主进程
  └─ 管理 FastAPI 后端子进程生命周期

FastAPI 后端
  ├─ Agent Runtime / workflow
  ├─ Conversation Run 与 tool 执行
  ├─ ConversationEventProjector
  └─ ConversationTaskSnapshotService

React WebView
  └─ 通过 Assistant Transport 读取 snapshot 并渲染
```

行为所有者和事实所有权保持不变：

- 后端拥有 Run、消息、tool、usage 和 Task context 的业务事实；
- `ConversationTaskSnapshotService` 是 Transport snapshot 的唯一 owner；
- `ConversationEventProjector` 只把事件投影成 snapshot；
- React 不计算、不累加、不持久化 token usage，只按 Run ID 渲染；
- 不新增认证、远程服务、Redis、队列或其他公网基础设施；
- backend 重启后不重放旧 Run，遗留 active Run 仍按现有策略收敛为 cancelled。

## 3. 两类 usage 的语义

### 3.1 Task 级 context usage

Task 级 context usage 表示当前 Task 有效上下文相对于当前模型窗口的占用情况：

```text
context_usage_ratio  = context_usage_used / context_window_total
```

字段定义：

| 字段 | 类型 | 语义 |
|---|---|---|
| `context_usage_ratio` | `number \| null` | 当前 Task 上下文占用比例；可大于 1 表示超额 |
| `context_usage_used` | `integer \| null` | 当前有效上下文已用 token |
| `context_window_total` | `integer \| null` | 当前有效上下文窗口上限 |

这三个字段属于当前 Task context，不属于任何单独 Run 的 token 消耗。前端只对比例做视觉 clamp，不修改后端事实。

`context_usage_used` 和 `context_window_total` 保留，供详情 popover 和诊断使用；主显示使用显式命名的 `context_usage_ratio`，不再使用含义模糊的 `context_usage`。

### 3.2 Run 级 token usage

Run usage 表示一次 `Conversation Run` 的累计模型 token 消耗：

```python
class ConversationStateUsage(TypedDict):
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_hit_tokens: int
    cache_miss_tokens: int | None
    reasoning_tokens: int
```

没有可靠 provider usage 时，Run snapshot 的 `usage` 为 `null`，不能用全零对象伪装成真实的零消耗。

`RunStatusChangedEvent.usage_stats` 遵守“该 Run 的完整累计值替换”语义，不是 Task 累计值，也不是前端增量；它只在 Run 终态时发送。

## 4. 目标 snapshot 结构

建议将现有平铺的 `messages` / `run` / 顶层 `usage` 重构为 Task snapshot 包含 Run snapshot 列表：

```python
class ConversationRunSnapshot(TypedDict):
    runId: int
    status: str
    endReason: str | None
    messages: list[ConversationStateMessage]
    usage: ConversationStateUsage | None


class ConversationStateMessage(TypedDict):
    id: str
    role: Literal["user", "assistant"]
    parts: list[ConversationStatePart]


class ConversationStateSnapshot(TypedDict):
    runs: list[ConversationRunSnapshot]
    current_run_id: int | None
    approvals: dict[str, object]

    context_usage_ratio: float | None
    context_usage_used: int | None
    context_window_total: int | None

    # Task/snapshot 级错误，不代表某个 Run 的模型失败
    error: ConversationStateError | None
```

示例：

```json
{
  "runs": [
    {
      "runId": 101,
      "status": "completed",
      "endReason": null,
      "messages": [],
      "usage": {
        "input_tokens": 1000,
        "output_tokens": 500,
        "total_tokens": 1500,
        "cache_hit_tokens": 0,
        "cache_miss_tokens": null,
        "reasoning_tokens": 0
      }
    },
    {
      "runId": 102,
      "status": "completed",
      "endReason": null,
      "messages": [],
      "usage": {
        "input_tokens": 1800,
        "output_tokens": 700,
        "total_tokens": 2500,
        "cache_hit_tokens": 200,
        "cache_miss_tokens": null,
        "reasoning_tokens": 0
      }
    }
  ],
  "current_run_id": 102,
  "approvals": {},
  "context_usage_ratio": 0.42,
  "context_usage_used": 42000,
  "context_window_total": 128000,
  "error": null
}
```

### 4.1 Run snapshot 的权威字段

`runs[*]` 按 Run 创建顺序排列，`runId` 是该对象的身份。父 Run snapshot 的 `runId` 是消息归属的主要事实。

`ConversationStateMessage` 只保留消息自身的事实：

```text
id
role
parts
```

以下字段从 message 中移除：

- `runId`：父对象 `ConversationRunSnapshot.runId` 已经是唯一归属事实；
- `status`：当前模型一个 Run 只有 user / assistant 消息，消息状态可由父 Run 状态派生；
- `endReason`：终态原因属于 Run，不应在 message 上重复保存。

`parts[*].status` 不删除。text、reasoning 和 tool part 仍然需要独立表达流式、完成、失败或取消等 UI 生命周期。

前端 converter 只在渲染适配对象的 metadata 中派生 `runId`，不改变、不回写 canonical snapshot：

```text
parent.runId      -> message.runId
parent.status     -> assistant message.status
parent.endReason  -> assistant message.endReason
```

wire snapshot 本身不再输出上述消息级字段。

每个 Run snapshot 的 `usage` 只能由对应 `run_id` 的事件更新，不能由当前活动 Run 的顶层快捷字段覆盖。

### 4.2 当前 Run

`current_run_id` 是 Task 当前活动或最近一次 Run 的索引，不是第二套 Run 状态机。

前端当前 Run 渲染只从 `runs[current_run_id]` 派生：

backend adapter 与 desktop converter 直接消费 `runs[current_run_id]`；不得同时维护
独立的 `run`、`messages` 或顶层 `usage` 可变副本。

## 5. 事件投影生命周期

### 5.1 RunInitializedEvent

新 Run 初始化时：

1. 向 `runs` 追加一个新的 Run snapshot；
2. 新 Run 的 `messages` 创建 user / assistant 骨架；消息只包含 `id`、`role` 和 `parts`；
3. 设置 `current_run_id`；
4. 新 Run 的 `usage` 初始化为 `null`；
5. 不修改历史 Run 的 messages、status 或 usage；
6. Task context 字段进入“等待本次 Run 重新测量”的状态。

### 5.2 RunStatusChangedEvent

`RunStatusChangedEvent.usage_stats` 的投影顺序：

```text
找到 runs[run_id]
更新 runs[run_id].status
更新 runs[run_id].endReason
若 usage_stats 不为空，则按累计值规则更新 runs[run_id].usage
```

用量只在终态事件中附带；重复投递或恢复流程中的较小摘要不能覆盖已经确认的累计值。

### 5.3 ContextUsageUpdatedEvent

只更新 Task 级字段：

```text
context_usage_ratio
context_usage_used
context_window_total
```

它不更新 `runs[*].usage`。

Context event 在同一个 backend projector 锁内串行投影，因此本结构不引入 revision、
版本或内部 metadata 字段。事件生产侧必须遵守已有 Task operation 顺序，projector
只接受已知 Run 的 Run 事件，并将最新 context 测量写入 Task 级字段。

## 6. 前端渲染设计

前端 adapter 将 Run snapshot 列表转换成 assistant-ui 所需的平铺消息：

```text
ConversationStateSnapshot.runs
    -> flatten messages
    -> 为每条 message 补 runId / metadata.runId
    -> 标记每个 Run 的最后一条 assistant message
    -> 渲染 RunUsageDisplay
```

`RunUsageDisplay` 接收自身 assistant message 的 `runId`，读取对应 Run snapshot：

```ts
const usage = snapshot.runs.find((run) => run.runId === messageRunId)?.usage ?? null;
```

不再依据 Task 当前 Run 去筛选 usage；每个 footer 只读取自己的父 Run。

期望行为：

- Run 101 显示 Run 101 的 usage；
- Run 102 显示 Run 102 的 usage；
- 新 Run 开始后，Run 101 的 footer 不消失、不改变；
- 当前 Run 没有 provider usage 时显示“用量统计中…”或“本次用量暂无”；
- 历史 Run 没有可靠数据时显示“本次用量暂无”，不显示伪造的 0。

## 7. 数据重建策略

本项目处于 0-1 阶段，本次改造不提供旧 snapshot 的读取迁移、字段兼容或双写。
开发/测试环境删除并重建 snapshot 数据库，使首个可读 snapshot 直接满足本文件定义的
新结构。旧结构中的历史 usage 不被推断、不伪造，也不进入新数据。

代码中不得保留旧平铺字段、兼容分支、迁移分支或任何 schema/version 字段。

## 8. 实施范围

后端：

- `apps/backend/app/assistant_transport/state/conversation_state_snapshot.py`
- 新增或调整 Run snapshot / context 类型文件；
- `apps/backend/app/assistant_transport/event/run_event.py`
- `apps/backend/app/assistant_transport/event/usage_event.py`
- `apps/backend/app/assistant_transport/event/message_event.py`
- `apps/backend/app/assistant_transport/event/tool_call_event.py`
- `apps/backend/app/assistant_transport/event/conversation_event_envelope.py`
- `apps/backend/app/assistant_transport/service/conversation_task_snapshot_service.py`
- `apps/backend/app/assistant_transport/service/transport_stream_service.py`
- 全新数据库初始化与 strict validation。

前端：

- `apps/desktop/lib/assistant/contract.ts`
- `apps/desktop/lib/assistant/snapshot-validation.ts`
- `apps/desktop/lib/assistant/converter.ts`
- `apps/desktop/components/assistant/usage-display.tsx`
- `apps/desktop/components/assistant-ui/elements/thread.aui.tsx`
- 相关 conversation action、resume、cancel 和 tool renderer 测试。

本阶段不新增独立 `conversation_run_usages` 表。Transport snapshot 已经是本项目的持久化读取源，足以支持历史 UI 展示。

如果未来需要计费审计、按 provider 查询、成本报表或独立灾备，再增加独立 Run usage 持久化模型；那属于另一个需求，不应与本次 UI snapshot 改造混在一起。

## 9. 测试验收标准

后端单元测试：

- 两个 Run 的消息分别进入各自 `runs[*].messages`；
- 终态 `RunStatusChangedEvent(run_id=101, usage_stats=...)` 只更新 Run 101；
- `RunStatusChangedEvent.usage_stats` 写入对应 Run；
- 新 Run 初始化不会清空历史 Run usage；
- 终态 usage 乱序不能回退累计值；
- 未知 Run ID 的 usage event 被拒绝并记录日志；
- context ratio 更新不影响任何 Run usage；
- context event 在 projector 串行边界内只更新 Task 级 context 字段；
- snapshot 持久化、重启读取后历史 Run usage 仍存在；
- 读取重建后的 snapshot 不包含旧平铺字段或任何版本字段。

前端单元测试：

- converter 能将 `runs` flatten 成 assistant-ui messages；
- 每个 message 的 footer 读取自身 `runId` 对应的 usage；
- 当前 Run 更新会触发 footer 重新渲染；
- 历史 Run footer 不受当前 Run usage 更新影响；
- `context_usage_ratio` 只影响 Task context meter；
- `null` usage 不显示虚假 token 数字；
- 快照契约拒绝 malformed usage 和错误类型。

Playwright E2E：

1. 第一个 Run 返回 `1.5k`；
2. 第二个 Run 返回 `2.5k`；
3. 第二个 Run 完成后，页面同时显示 `1.5k` 和 `2.5k`；
4. 刷新页面后两个数字仍然存在；
5. context meter 的比例变化不会改变两个 Run footer；
6. failed / cancelled Run 保留最后一次已知 usage；
7. 窄窗口和消息重挂载不产生重复 footer。

## 10. 完成判据

只有同时满足以下条件，才认为改造完成：

1. Task context 与 Run token usage 在类型、事件和 UI 文案上完全分离；
2. `runs[*]` 是消息、Run 状态和 Run usage 的唯一组合事实；
3. 前端不再依据当前 Run 顶层 usage 渲染历史 Run；
4. 不存在旧 snapshot 兼容逻辑、迁移逻辑或版本字段；
5. context event 通过既有 projector 串行边界更新 Task 级字段；
6. targeted backend tests、desktop unit tests、build、lint 和关键 E2E 全部通过。

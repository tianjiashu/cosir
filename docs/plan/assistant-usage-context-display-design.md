# Assistant 用量与上下文占用展示技术方案

**状态**：实现完成，主Agent最终验收通过  
**日期**：2026-09-09  
**适用范围**：`apps/backend` Assistant Transport、Task Context 与 `apps/desktop` assistant-ui 任务对话页

## 1. 结论摘要

本节的缺陷列表是实施前审计基线；“当前”在这些段落中指实施前代码。实现结果和最新验证以本文末尾及配套验收文档为准。

当前后端的两条计量链路在“正常、未重启、事件按顺序到达”的情况下可以更新：

```text
模型节点
  -> ConversationRunUsageStats 累加
  -> UsageUpdatedEvent
  -> ConversationEventProjector
  -> snapshot.usage
  -> Assistant Transport

RuntimeContextManager
  -> ContextUsageComputeListener 重算完整 context
  -> ContextUsageUpdatedEvent + tasks.context_usage_used
  -> snapshot.context_usage / TaskResponse
```

实施前不能直接认为计量契约已经适合用户展示，原因是：

1. `usage` 是 snapshot 顶层的单份字段，没有显式 `run_id`；`RunInitializedEvent` 不清零它，新 run 开始前会短暂展示上一轮用量。
2. `ContextUsageUpdatedEvent.used_tokens` 被生产但没有投影到 snapshot；snapshot 只有比例，没有“已用 / 总窗口”，无法正确使用 assistant-ui 的 token meter 或 breakdown。
3. 后端 `TaskResponse` 已声明 `context_usage_used`、`context_window_total`，但当前任务接口没有注入 `context_window_total`，桌面端 `WorkspaceTask` 也没有声明这两个字段。
4. `ConversationRunUsageStats.cache_miss_tokens` 需要区分 provider 明细缺失与真实 0；当前实现已在缺失明细时以 `null` 透传。
5. 上下文 token 是基于消息正文长度和 tool call 字符串的估算，不是 provider 返回的精确 tokenizer 结果；UI 必须标记为估算或避免暗示“精确计费”。
6. 后端现有上下文监听器测试仍按旧的 `publish_event` 构造参数编写，当前针对性测试结果为 **63 passed / 2 failed**，失败发生在测试与当前实现不一致，不应把它当作功能已验收。

推荐先完成“契约修正 + 任务级上下文环 + run 级用量详情”的最小闭环，再考虑真实 tokenizer 和费用统计。用户看到的两个数字必须明确分工：

| 展示 | 语义 | 位置 | 生命周期 |
|---|---|---|---|
| 上下文占用 | 当前 Task 的 context window 压力 | Composer rail / footer，常驻但低干扰 | 跨 run，随 Task context 变化 |
| 本次用量 | 当前一次 Conversation Run 的输入、输出、缓存、推理 token | 最后一条 assistant 消息的 meta/footer | 仅当前 run，run 切换即重置 |

## 2. 进程、事实源与数据边界

本项目是单用户本地桌面 Agent，不按公网 SaaS 设计：

```text
Tauri / React WebView
  -> localhost Assistant Transport
本机 FastAPI backend
  -> LangGraph workflow / RuntimeContextManager / tools
  -> SQLite
```

| 问题 | 决策 |
|---|---|
| Agent 与计量运行在哪个进程 | LangGraph、模型调用、context 计算和 snapshot 投影都在本机 FastAPI 进程；React 只渲染 |
| run 用量事实源 | `RuntimeConfig.usage_stats` / `ConversationRunUsageStats`，由模型节点在完整 `AIMessage` 产生后累加 |
| Task context 事实源 | `RuntimeContextManager` 的 `_entries` 与 `used_tokens`；持久化事实为 context service 和 `tasks.context_usage_used` |
| UI snapshot owner | `ConversationTaskSnapshotService`；`ConversationEventProjector` 只把事件投影为 snapshot |
| 持久化 | snapshot、context、Task 字段进入 SQLite；assistant-ui store 只是不可持久化渲染副本 |
| 启动/停止 | Tauri 负责启动和停止 FastAPI；窗口隐藏不停止 backend，真正退出才停止进程树 |
| backend 崩溃恢复 | 内存 working copy、订阅和未提交的实时更新会丢失；重新从 SQLite hydrate，未落库的 run 用量不能凭空恢复 |
| 网络与数据外发 | 本方案不新增公网服务或队列；模型请求仍按既有 provider 配置离开本机 |

不能由前端依据历史消息自行“重算”后端事实，也不能把 UI 计算结果写回 backend。若 provider 没有返回 usage，必须显示“暂无用量/统计中”，不能伪造 0。

## 3. 当前代码事实审查

### 3.1 `ConversationStateUsage` 的更新链路

已确认的代码事实：

- `apps/backend/app/core/workflows/nodes/model_node.py` 在完整 assistant 消息形成后调用 `rc.usage_stats.add_usage_metadata(...)`，随后发出 `UsageUpdatedEvent`。
- `ConversationRunUsageStats` 会跨同一 run 的多次模型调用累加 input、output、total、cache hit 和 reasoning。
- `ConversationEventProjector` 将 `usage_updated` 列为已知事件，并调用事件自身的 `plan` 写入 `snapshot["usage"]`。
- 终态 `RunStatusChangedEvent` 也可以携带 `usage_stats`，因此成功、失败、取消路径在传入累加器时能补写最终值。
- `ConversationTaskSnapshotService` 在 mutation 后校验、写 SQLite、通知订阅者；正常情况下前端能收到 usage 更新。

因此，“模型节点是否会发、projector 是否会写、Transport 是否能传”这条主链路是成立的。

### 3.2 `ConversationStateUsage` 的实施前缺陷与修正

#### P1（已修正）：新 run 未重置上一轮 usage

`RunInitializedEvent.plan()` 只建立消息骨架和 `run` 状态，不清零 `usage`。新 run 的第一个 usage event 到达前，snapshot 会继续保留上一轮的 token；如果新 run 在首个模型响应前失败、取消或 backend 重启恢复，旧数字可能继续留在界面。

同时，顶层 `usage` 没有 `usage_run_id`，无法仅凭 snapshot 判断它属于哪个 run。当前实现事实上是“最近一次写入的 run usage”，不是可追溯的“明确属于某个 run 的 usage”。

**修正**：在 `RunInitializedEvent` 中清零并设置 `usage_run_id = run_id`；`UsageUpdatedEvent` 和终态状态事件都带上相同关联。若 provider 没有 usage，UI 显示“暂无用量”；cache miss 明细缺失时使用 `null`，不要解释为模型确实消耗了 0 token。

#### P1（已确认边界）：backend 重启后 run usage 无法完整恢复

run 用量按现有注释“不落库”，只依赖 snapshot 已经投影的值。若进程在模型调用后、usage snapshot commit 前崩溃，恢复逻辑只能恢复 run 状态和 context，不能从 `ConversationRunRecord` 重新得到该次 token 用量。

**修正策略**：第一阶段接受“最后一个已提交 usage 是 best effort”，但 UI 必须显示“统计可能不完整”；第二阶段如产品需要审计/计费，再为每个 run 增加 SQLite usage 记录，不把这项职责塞进 assistant-ui。

#### P1（已修正）：`cache_miss_tokens` 的可用性必须保留

`ConversationRunUsageStats.add_usage_metadata()` 只能在 provider 提供 `cache_read` 明细时推导 `cache_miss_tokens`；缺少该明细时不能把估算结果伪装成精确数字。

**修正**：在 provider 契约明确 input token 含义后计算 `max(0, input_tokens - cache_hit_tokens)`，并在测试中覆盖“无缓存详情、缓存命中、重复模型调用”三种情况。

本方案收敛为：snapshot 保持六个 usage 字段，其中五个计数为非负整数，`cache_miss_tokens` 在无法证明口径时为 `null`；UI 不渲染“缓存未命中”的精确数字，只展示 `—`。前后端严格契约已同步该 nullable 字段。

#### P2：总量与 provider 明细缺少一致性策略

当前允许 provider 的 `total_tokens`、`input_tokens`、`output_tokens` 和 reasoning/cache details 不完全相加。实现应保留 provider 原始主量，不为了 UI 强行修正 `total_tokens`；UI breakdown 需要标注“provider details”，并对负差额、NaN、非整数输入做防御。

### 3.3 `context_usage` 的更新链路

已确认的代码事实：

- `RuntimeContextManager.__post_init__()` 会对加载后的有效 context 触发一次 `LOAD_HISTORY`；`begin_run()` 根据当前 run model 设置 `total_tokens`，`add_message()` 和压缩后会重新触发完整 context 计算。
- `ContextUsageComputeListener` 只处理 `add_message`、`load_history`、`context_compressed`，按完整 entries 重算，而不是增量累加，避免重复统计。
- 比例公式是 `used / total`；`total` 来自 `CapabilityService.get_model_context_window()`，并受设置中的软上限影响；超额比例允许大于 1。
- listener 同时调用 projector 写入 snapshot，并调用 `TaskService.update_context_usage()` 回写 `tasks.context_usage_used`。
- `ConversationStateSnapshot.validate_snapshot()` 和前端 `parseTransportState()` 都拒绝负数；前端还拒绝非有限数字。

因此，在 run 已设置模型窗口、context 事件正常到达时，`context_usage` 能实时变化，并且 Task 数据库有最近一次 absolute used 值。

### 3.4 `context_usage` 的实施前缺陷与产品限制

#### P0（已修正）：绝对 token 在 Transport snapshot 中被丢弃

监听器生产了 `ContextUsageUpdatedEvent.used_tokens`，但 `plan()` 只设置 `context_usage`，没有设置 used token。前端只能知道比例，无法展示 `42k / 128k`，也不能实现官方 Context breakdown 的 headroom 计算。

**修正**：保留现有 `context_usage` 兼容字段，并在 snapshot 增加：

```ts
context_usage_used: number | null;
context_window_total: number | null;
```

事件同时携带并投影 `used_tokens` 与 `total_tokens`。`null` 表示本轮尚未完成有效测量；0 表示确实测量到 0。比例仍由 backend 计算，前端不再自行用近似值反推绝对 token。

#### P1（已修正）：Task API 已有字段，但没有形成可用契约

`TaskResponse` 已声明 `context_usage_used` 和 `context_window_total`，但 `/tasks/{task_id}` 与工作区任务列表当前调用 `TaskResponse.from_record()` 时没有注入窗口上限；桌面端 `WorkspaceTask` 类型也没有声明这两个字段。

**修正**：把 snapshot 作为对话页实时事实源；同时补齐 `WorkspaceTask` 类型和 API 映射，供首屏历史/侧栏显示。Task API 无法确定当前模型时允许返回 `null`，不要使用固定兜底窗口。

#### P1（产品边界）：上下文估算并非 provider 精确 token

`ContextUsageComputeListener._message_tokens()` 使用正文长度除以 4，并把 tool calls 转成字符串后计入。这适合作为轻量压力估算，但不能与 provider billing token 等价。不同语言、代码、图片、工具 schema 和 tokenizer 会产生显著偏差。

**修正**：第一阶段 UI 文案使用“上下文占用（估算）”，tooltip 说明估算口径；第二阶段在模型 adapter 已有 tokenizer 能力时替换计算端口，并保留 source/precision 标记。

#### P1（已修正）：snapshot 与 Task DB 更新不是一个事务

listener 先经 projector 提交 snapshot，再写 `tasks.context_usage_used`。写回失败时，实时对话快照和重新打开任务的 Task 数据可能短暂不一致；当前 listener 的直接 `get_task_service().update_context_usage()` 没有本地捕获异常，写回失败可能向上抛出并中断本次 context 变更，这不是理想的旁路失败安全。

这符合项目允许的最终一致性边界，但修正后需要可恢复：以 RuntimeContextManager 重新计算值为准，在下一次 context 变更或任务重新加载时修复；不能让 Task API 的旧值覆盖活动 Transport snapshot。

#### P2（已修正）：事件到达顺序没有 context revision

projector 有 event id 去重和进程内锁，但没有“上下文版本”字段。若未来允许同 Task 并行恢复或异步写事件，迟到的旧 context event 可能覆盖新值。

**修正**：暂不引入全局消息队列；在事件中增加由 RuntimeContextManager 单调递增的 `context_revision`，snapshot 只接受大于等于当前版本的 context update。单用户单 Task 串行执行是当前默认边界，但 revision 能保护后续恢复路径。

#### P2（已修正）：初始 manager 装载时会出现一次 0 比例

`__post_init__()` 的历史加载发生在 listener 注册前，仍不会单独产生业务投影；首次 Task snapshot read 现在会经 `TaskRuntimeSpace` 物化 manager、注册 listener，并以最新 run 的有效窗口执行一次幂等 `LOAD_HISTORY` reproject。UI 同时区分“尚未测量”与“0%”。

## 4. assistant-ui 用户体验设计

Assistant UI 官方文档的核心原则适合本项目：Context meter 放在 composer rail/footer/sidebar；run usage 放在 assistant message 的 metadata/footer；显示组件通过 `useAuiState` 订阅需要的最小状态；没有真实 usage 时不渲染假数字。

参考：[Context display](https://www.assistant-ui.com/elements/context-display)、[Context breakdown](https://www.assistant-ui.com/elements/context-breakdown)、[Composer context](https://www.assistant-ui.com/elements/composer-context)、[Assistant Context API](https://www.assistant-ui.com/docs/guides/context-api)。

### 4.1 Task context meter

放置在当前已有 `Composer` 底部操作行，与 `ComposerControls` 和发送按钮同一 rail：

```text
┌──────────────────────────────────────────────┐
│ 输入任务…                                     │
│                                              │
│ [模型 / 设置]                 [◔ 42%] [发送] │
└──────────────────────────────────────────────┘
```

行为：

- 默认显示紧凑 Ring 或 Text；鼠标 hover、键盘 focus 或点击打开详情 popover。
- 详情显示：`上下文 42k / 128k`、剩余 `86k`、估算说明和状态（正常/接近上限/已超额）。
- 阈值沿用 assistant-ui 语义：低于 65% 正常，65%–85% amber，超过 85% red；比例超过 100% 时进度视觉 clamp 到 100%，文字明确显示“已超出 128k”。
- 没有有效测量时显示 `上下文 —`，不显示绿色 0%。
- 发送按钮、停止按钮、恢复按钮的布局不因 meter 变化跳动；移动/窄窗口只保留 ring，详情仍可访问。
- context meter 是 Task 级，不放到每一条消息中，也不与本次 run token 相加。

官方 `ComposerContext` 组件假设 system/tools/messages 三段都由应用持有，当前项目只有 aggregate context 估算，不能凭空伪造三段。因此优先使用官方 `ContextBreakdown` 的单段“当前上下文” + headroom，或实现同样信息架构的项目 wrapper；只有后端真实追踪了三类成本后才使用三段 preset。

### 4.2 Run usage footer

放在最后一条 assistant message 的 action/footer 附近，不打扰正文：

```text
本次用量 18.4k tokens · 输入 14.8k · 输出 3.6k     [详情]
```

详情 popover：

| 项 | 展示 |
|---|---:|
| 输入 | `input_tokens` |
| 输出 | `output_tokens` |
| 总计 | `total_tokens` |
| 缓存命中 | `cache_hit_tokens` |
| 缓存未命中 | `cache_miss_tokens`；只有口径可确认时展示，否则显示 `—` |
| 推理 | `reasoning_tokens` |

状态规则：

- run 运行中且还没有 provider usage：`用量统计中…`，不显示 0。
- 收到 `UsageUpdatedEvent`：实时替换为当前累计值；多个模型 step 显示 run 累计，而不是单 step。
- run 完成/失败/取消：保留最终已知值；若没有有效 provider usage，显示 `本次用量暂无`。
- 历史消息只显示它自己的 run usage；不能把当前 task 的 context percentage 放在这行。

### 4.3 为什么不直接把 snapshot usage 塞进 assistant-ui `metadata.steps`

assistant-ui 官方 runtime usage hook 读取 assistant message 的 `metadata.usage` 或 `metadata.steps`。本项目的 authoritative usage 是自定义 Assistant Transport snapshot 顶层字段，并且 `ConversationStateUsage` 是 run 累计值，不是 assistant-ui 的单 step 结构。

推荐第一阶段在 UI 边界新增 `useTransportUsage()` / `useTransportContextUsage()`，通过 `useAuiState((s) => s.thread.state)` 读取经过校验的 Transport state；不要在 converter 中为每次 snapshot 更新重建大量 metadata，也不要让 assistant-ui 的派生 step 成为第二个事实源。若未来 adapter 原生提供 assistant-ui step usage，再增加无损映射和一致性测试。

## 5. 推荐数据契约

### 5.1 snapshot 目标结构

保留现有字段并做显式扩展，避免把两个不同生命周期的指标合并：

```json
{
  "messages": [],
  "run": { "runId": 42, "status": "running" },
  "usage_run_id": 42,
  "usage": {
    "input_tokens": 14800,
    "output_tokens": 3600,
    "total_tokens": 18400,
    "cache_hit_tokens": 9200,
    "cache_miss_tokens": 5600,
    "reasoning_tokens": 1200
  },
  "context_usage": 0.42,
  "context_usage_used": 42000,
  "context_window_total": 100000,
  "approvals": {},
  "error": null
}
```

契约规则：

- `usage_run_id` 与 `usage` 同步更新；新 run 初始化时 `usage_run_id` 设置为新 run，usage 全字段清零。
- `context_usage` 是 backend 计算的 ratio，允许大于 1；`context_usage_used`、`context_window_total` 为非负整数或 null。
- `context_window_total` 是本次 context 事实使用的有效窗口，包含设置软上限后的最终值，不能让前端用模型目录自行猜测。
- snapshot 校验增加 finite 检查；NaN、Infinity、负数和布尔值都拒绝。
- 对旧 SQLite snapshot 做一次读时迁移/兼容填充：旧数据的新增详情为 null，usage_run_id 从当前 `run.runId` 推断仅用于展示，并记录兼容日志；不能把推断写成 provider 事实。

### 5.2 事件修改

`UsageUpdatedEvent` 保持“完整累计值替换”语义，新增/明确 `run_id` 关联，projector 写入 `usage_run_id` 与 `usage`。

`ContextUsageUpdatedEvent` 增加 `context_window_tokens` 或等价字段，`plan()` 同时写入 `context_usage`、`context_usage_used`、`context_window_total`；现有 `used_tokens` 不能再只是 debug dead field。

## 6. 代码改造清单

### 6.1 后端

1. `app/assistant_transport/state/conversation_state_snapshot.py`：扩展 snapshot TypedDict、empty baseline、严格校验和旧数据兼容策略。
2. `app/assistant_transport/state/conversation_state_usage.py`：补齐 `usage_run_id` 的邻接类型/文档，明确“run 累计，不是 Task 累计”。
3. `app/assistant_transport/event/run_event.py`：`RunInitializedEvent` 清零 run usage；`RunStatusChangedEvent` 在带 usage 时同步 run id；恢复终态不制造虚假 usage。
4. `app/assistant_transport/event/usage_event.py`：补投影 `usage_run_id` 和 context absolute/window 字段，增加有限数校验。
5. `app/core/workflows/conversation_run_usage_stats.py`：实现 cache miss 口径，补足 provider details 异常值防御和单元测试。
6. `app/core/context/context_listener/context_usage_compute_listener.py`：传递 absolute/window，保留估算来源；将 Task DB 写回改为失败安全的最终一致性旁路；修复模块 docstring 中已经不存在的 `publish_event` 描述。
7. `app/core/context/runtime_context_manager.py`：确保 begin_run 后的实际窗口进入 context event；如实施 revision，则在这里递增 context revision。
8. `app/api/schemas/response/TaskResponse.py`、`app/api/tasks_api.py`、`app/api/workspaces_api.py`：若保留 Task API 上下文字段，统一注入可确定的窗口值，不确定时返回 null。
9. 相关 backend tests：覆盖 fresh/resume、新 run 清零、usage event 累计、终态/取消、context absolute/window、超额、NaN/Infinity、防乱序和 backend restart 后 best-effort 语义。

### 6.2 前端

1. `lib/assistant/contract.ts`、`lib/assistant/snapshot-validation.ts`：同步新增字段和严格校验，区分 null 未测量与 0 已测量。
2. `lib/api/workspaces.ts`：补齐 `WorkspaceTask.context_usage_used` 与 `context_window_total`；仅作首屏/侧栏 fallback，不覆盖活动 snapshot。
3. 新增 `components/assistant/usage-display.tsx` 或同等 assistant-ui 边界组件：提供 `useTransportUsage`、`useTransportContextUsage`、数字格式化、阈值和状态文案。组件只用 `useAuiState` 订阅最小 selector。
4. `components/assistant-ui/elements/thread.aui.tsx`：在当前 Composer rail 加 Task context meter；在 assistant message footer 加 run usage display。
5. `lib/assistant/converter.ts`：继续透传完整 Transport state；不要删除新增字段，也不要把顶层 run usage伪装成历史 message step。
6. 单元测试：格式化、null/0、65%/85%/100%/超额、run 切换隔离、snapshot 更新触发渲染、ARIA meter 属性。
7. E2E：用 test server 推送两次 usage、两次 context update 和一次新 run，验证 UI 更新、旧 run usage 不残留、详情可键盘访问、窄窗口不溢出。

## 7. 分阶段实施

```text
Phase 0  契约与审查修复
  -> usage reset/run association
  -> context used/window projection
  -> cache miss semantics
  -> fix stale tests and validation

Phase 1  UI first usable
  -> Composer context meter
  -> assistant run usage footer
  -> null/unknown and overage states

Phase 2  恢复与历史一致性
  -> Task API fallback fields
  -> restart/reconcile tests
  -> optional context revision

Phase 3  精度增强（可选）
  -> provider/model tokenizer adapter
  -> precision/source indicator
  -> per-run persisted usage only if audit/billing requires
```

## 8. 不应采用的方案

- 不把 `context_usage` 当作本次 run token 使用量；二者分母和生命周期不同。
- 不在前端用 `ratio * 128000` 反推出绝对 token；窗口可能是动态软上限，也可能发生模型切换。
- 不把所有历史 assistant message 的 `metadata.steps` 相加来模拟当前 Task context；assistant-ui 的 steps 是模型调用 usage，不等于 RuntimeContextManager 的有效 context。
- 不为显示三段 system/tools/messages 而编造固定数字；官方 Context breakdown 要求每段输入确实可追溯。
- 不在 SQLite、Transport snapshot、React store 各维护一套独立 usage 累加器。
- 不以“失败时显示 0”掩盖 provider 未返回 usage 或 backend 崩溃造成的不完整统计。

## 9. 完成定义

只有同时满足以下条件，才认为本项完成：

1. backend contract、projector、snapshot persistence、Task API 和 frontend validation 对新增字段一致。
2. 新 run 绝不会在首个 usage event 前展示上一轮 usage；usage 有明确 run 归属。
3. context meter 可以展示 `used / window`，且未测量、正常、预警、超额、provider unknown 都有明确状态。
4. run usage 在 running、completed、failed、cancelled、resume 和历史载入场景都不互相污染。
5. targeted backend tests 与 desktop unit tests 全部通过；关键 E2E 覆盖至少两个连续 run。
6. 按验收文档完成“代码事实、需求覆盖、契约一致性和测试证据”核对，并通过全量自动化验证；本次按用户最新指示由主Agent完成最终验收。

## 10. 实现与最终验证结果

- 后端已完成 snapshot 契约迁移、run usage 隔离与乱序保护、context used/window 投影、resume/重启首次读取重投影，以及未知 cache miss 语义。
- 桌面端已完成 Composer context meter、assistant run usage footer、unknown/overage/阈值/ARIA/窄屏处理，并按 `usage_run_id` 隔离连续 Run。
- 2026-09-09 主Agent最终验收：backend pytest `208 passed`，Ruff 通过；desktop unit `53 passed`、build、lint 通过；Playwright E2E `16 passed`。

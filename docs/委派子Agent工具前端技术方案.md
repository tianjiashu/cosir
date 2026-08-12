# 委派子Agent工具前端技术方案

> 本文是 `docs/委派子Agent工具计划方案.md` 的前端落地方案。它只定义高层技术路线与可执行 checklist，不展开组件级实现细节。

## 目标

前端第一版要让用户在对话过程中看清楚：

- 父 Agent 何时发起委派、委派给哪个 child Agent。
- child Agent 的实时执行过程、审批等待、失败/取消/完成状态。
- 父 turn 与 child turn 的关系，且历史回放、任务切换、重连后不丢状态。

界面必须复用现有 conversation UI、`eventStore`、turn 级 timeline projector 和工具卡片体系，不为 subagent 另起一套平行渲染框架。

## 当前代码事实

- 客户端是 Tauri 2 + React + TypeScript + Vite，状态管理使用 Zustand。
- 运行事件通过 `apps/desktop/src/services/sse.ts` 的 `fetch + ReadableStream` 消费，不使用 `EventSource`。
- `apps/desktop/src/hooks/useSSE.ts` 目前管理单个 turn 级 SSE 连接，并将事件批量写入 `eventStore.appendEvents()`。
- `apps/desktop/src/stores/eventStore.ts` 已按 `task_id` 与 `turn_id` 分片保存事件，支持历史回放与实时事件去重合并。
- `apps/desktop/src/hooks/useTask.ts` 打开任务时先加载 task/turns，再异步拉取 `api.listTaskEvents(taskId)` 回填历史事件。
- `apps/desktop/src/components/layout/TurnTimeline.tsx` 按 turn 独立投影和渲染，投影逻辑在 `apps/desktop/src/services/timeline/projector.ts`。
- `apps/shared/ts/events.ts` 是后端 payload 生成物，前端不能手工维护事件类型。
- 右侧面板已有 `apps/desktop/src/components/right-panel/SubagentBlock.tsx` 占位能力，但第一版对话过程展示应优先在主 timeline 可见。

## 技术路线

### 1. 协议先行，前端只消费共享事实源

新增 delegation 事件后，先由后端 payload model 生成 `apps/shared/ts/events.ts`。前端只 import 生成后的类型，并在 API service 中使用共享协议，不在组件内硬编码散落事件字段。

第一版前端需要消费：

- `delegation_started`
- `delegation_child_started`
- `delegation_finished`
- `delegation_failed`
- `delegation_cancelled`

其中 `delegation_child_started` 是建立 parent/child UI 关系的关键事件，必须包含 `delegation_id`、`parent_turn_id`、`child_turn_id`、`child_agent_id`。

### 2. SSE 路由采用“父流摘要 + child turn 独立订阅”

前端不要求后端把 child 全量事件混入父 turn stream。父 turn 的实时流只驱动委派摘要；收到 `child_turn_id` 后，前端为 child turn 建立独立 SSE 订阅。

这个订阅必须调用后端明确的 subscribe-only 通道，而不是复用“pending turn 认领并执行”的语义。当前 `/turns/{turn_id}/stream` 对非 pending turn 会 409；child turn 通常由后端 delegation executor 启动，前端拿到 `child_turn_id` 时可能已经是 running。因此前端方案以 `GET /turns/{child_turn_id}/events/stream` 这类只订阅 endpoint 为目标契约；如果后端最终改造原 `/stream`，前端也只依赖其 running subscribe-only 分支。

这要求把当前 `useSSE` 的“单连接”假设拆成可复用的连接管理能力：

- 保留主 turn SSE 的现有行为。
- 新增按 `turn_id` 管理的 child SSE 连接池或 delegation stream hook。
- 所有连接收到的事件仍写入同一个 `eventStore`，由 `event_id` 去重。
- 全局 `connectionState` 不再代表所有 child stream；child stream 状态应挂到 delegation UI 或独立 store。

历史回放时不自动重放 SSE。`useTask.openTask()` 仍通过 task events + turns 回填，delegation 关系由已持久化事件重建。

child stream 漏接或异常结束时必须有补偿加载策略，不能依赖当前 `useTask.openTask()` 的缓存命中跳过逻辑：

- 收到 `delegation_child_started` 后，如果 child stream 未成功连接，标记该 `delegation_id` 需要 backfill。
- child stream 异常结束、缺少 child 终态事件、或用户切回任务时发现 delegation 未终态，都强制调用 `api.listTaskEvents(taskId)` 合并回填，不受普通 task events 缓存命中影响。
- backfill 仍写入 `eventStore.setEvents(events, taskId)`，依赖 event_id 去重，不另建事件事实源。
- backfill 完成后按 child turn 是否已有终态清理待补偿标记。

### 3. Timeline 展示复用现有投影体系

新增 `DelegationTimelineEntry` 或等价结构放在现有 `projector.ts` 输出模型中。父 turn 中的 `delegate_task` 工具卡片展示：

- 委派目标 child profile。
- 当前状态：pending/running/waiting_approval/completed/failed/cancelled。
- child turn 简要摘要与耗时。
- 可展开的 child timeline。

展开区域复用 `TurnTimeline` 或提取后的共享 timeline 渲染单元，数据来自 `eventStore.eventsByTurnId[child_turn_id]`。不要复制一份 subagent 专用 markdown、tool card、terminal renderer。

### 4. 审批与取消交互

child 运行中的 human approval 属于 child turn：

- approval UI 需要显示在 child timeline 内。
- approval action 使用 child turn/request id，不发到 parent turn。
- 父 turn 取消时，UI 同步显示 child delegation 被取消，并禁用未完成 approval 操作。

如果 child stream 尚未连上但历史事件已包含 `human_input_requested`，前端也必须能通过历史回放显示等待审批状态。

### 5. 状态与性能

delegation 关系建议放在轻量 selector 或单独 store 中，从 `eventStore` 派生：

- key 使用 `delegation_id`。
- parent 维度记录 `parent_turn_id`。
- child 维度记录 `child_turn_id`。
- stream 状态独立记录，避免污染现有 `eventStore.connectionState`。

渲染性能约束：

- 不让 child delta 触发整棵任务 timeline 重投影。
- child timeline 仍按 turn 粒度 memo。
- 展开态局部保存，不因 `tool_call_finished` 或迟到事件重置。
- 长输出继续复用现有 `ToolCallCard`、`TerminalCallCard`、`MarkdownStream` 的预算与 memo 机制。

## 前端实现 Checklist

- [ ] 等后端生成新的 `apps/shared/ts/events.ts` 后，再接入 delegation 类型。
- [ ] 扩展 `projector.ts`，把 delegation 事件投影为父 turn 的委派条目。
- [ ] 新增或扩展 `DelegationBlock`/`SubagentBlock`，用于主 timeline 中展示委派摘要和可展开 child timeline。
- [ ] 抽取可复用 child stream 管理能力，支持多个 child turn SSE 同时存在，但第一版按后端并发 1 验收。
- [ ] child stream 使用后端 subscribe-only endpoint，不调用会认领执行 pending turn 的接口语义。
- [ ] child stream 事件统一写入 `eventStore.appendEvents()`，不另建事件事实源。
- [ ] 为 delegation stream 维护独立连接状态，不复用全局 `connectionState` 表达 child 状态。
- [ ] `useTask.openTask()` 历史回放后可从 events/turns 重建 delegation 关系。
- [ ] child stream 未连接、异常结束、缺少终态或切回任务时未终态，强制 backfill `api.listTaskEvents(taskId)` 并合并到 eventStore。
- [ ] approval UI 路由到 child turn，并在 parent cancelled/child terminal 后禁用。
- [ ] 取消操作后父/child UI 状态一致，不保留可点击的过期审批。
- [ ] 右侧 `SubagentBlock` 仅做辅助总览，主对话 timeline 必须能看清执行过程。
- [ ] 补 projector 单元测试：started、child_started、finished、failed、cancelled、乱序到达。
- [ ] 补 eventStore/stream 测试：父流与 child 流重复事件去重、按 turn 分片正确、child stream 漏接后 backfill 不被缓存跳过。
- [ ] 补组件测试：展开/折叠 child timeline、审批等待、child 失败、父取消。
- [ ] 做一次真实浏览器视觉验证，重点检查长中文、工具卡片、child timeline 展开时不重叠。

## 验收标准

- [ ] 父 turn 中能看到委派发起、child profile、child turn 状态和终态摘要。
- [ ] 展开后能看到 child Agent 的模型输出、工具调用、审批等待与最终结果。
- [ ] 父流和 child 流同时到达时，事件不重复、不串 turn、不击穿历史缓存。
- [ ] 切换任务再回来，delegation UI 可由历史事件完整重建。
- [ ] child stream 漏接或异常结束后，强制 backfill 能补全 child timeline 和终态。
- [ ] child approval 不会误提交到 parent turn。
- [ ] 父取消或 child 终态后，过期 approval 操作不可继续点击。
- [ ] 对话区在窄屏和宽屏下没有文本、工具卡片、child timeline 的 UI 重叠。

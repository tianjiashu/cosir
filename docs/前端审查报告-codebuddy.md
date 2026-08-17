🔴 高严重（直接影响正确性/数据/首次体验）
H1. 多 task 并发下 streamingTurnId 全局单值串扰
位置：stores/turnStore.ts:17,84-86；影响 InputBar.tsx:43,48,174、useSSE.ts、useTask.ts
问题：streamingTurnId 是跨所有 task 共享的单一字段。项目已支持多 task 并发流式，并发时一个 task 的 setStreamingTurn 会覆盖另一个；某 task 终态时 setStreamingTurn(null) 还会误清空别的仍在跑的 task。
影响：用户无法停止并发中的其它 task；「停止」按钮闪烁/失效。
修复：改为 Record<taskId, turnId>；InputBar 取当前 activeTaskId 对应的 streaming turn；syncRuntimeStatus 仅在 task_id 匹配时改动可见 streaming 态。
H2. 单实例 useSSE 在多 task 并发时互相断开连接
位置：hooks/useSSE.ts:74,90-225；hooks/useTask.ts:124,161-164
问题：全局仅一份 useSSE()；createTask/createTurn 每次先 disconnect() 再 connect()。后台 task A 流式时，前台 task B 发消息会 abort 掉 A 的连接，A 实时流被掐断。
影响：并发场景事件丢失、状态不一致、UI 跳动。
修复：SSE 连接按 turn 维度独立（Map<turnId, SSEConnection>），disconnect 只断指定 turn。
H3. DELETE/PUT 被 ky 自动重试，破坏性操作被静默重放
位置：services/httpClient.ts:64-67；api.ts:427 deleteTask / api.ts:272 deleteWorkspace
问题：retry.methods 含 put,delete 且 limit:2。deleteTask/deleteWorkspace 是级联删除，后端删成功但响应 5xx/代理超时时会再发 2 次 DELETE。
影响：数据已删却提示「删除失败」或触发二次级联副作用，UI 与真实状态不一致。
修复：methods 收窄为 ["get","head","options"]；DELETE 若需重试改用 shouldRetry 仅对网络层错误重试。
H4. prepareWorkspace 被前端 30s 超时误杀（超时预算倒挂）
位置：services/httpClient.ts:63（timeout:30000）；api.ts:295-299（prepareWorkspace 未覆盖 timeout）；httpClient.ts:28（注释描述的覆盖机制不存在）
问题：post() 签名不支持 timeout 参数，注释说的「调用处覆盖 timeout」机制根本不存在。后端 PREPARE_TIMEOUT_SECONDS=60 且「大仓库首次 init 可达数分钟」，前端 30s < 后端 60s。
影响：首次建仓库索引必然 30s 抛 TimeoutError → workspace 置 error 并断开 SSE；后端其实仍在跑并会推 workspace_ready，但用户永久卡在错误态，只能重启。直接影响首次使用体验。
修复：给 post() 加 timeoutMs 参数透传；prepareWorkspace 传 timeout:false 或 ≥90000；修正失效注释。
🟠 中严重（协议漂移 / 性能 / 不可达 UI）
M1. client_disconnected 是死协议分支（前后端漂移）
位置：services/timeline/projector.ts:469、useSSE.ts:321-323、StatusBadge.tsx:40
问题：生成协议 events.ts 的 RuntimeEventType 没有 client_disconnected。后端实际发的是 run_failed + payload.error/end_reason。三处判断是死代码；更糟的是后端两条路径字段不一致（turn_stream_service.py 不填 end_reason，runner.py 填），靠 error 字段侥幸兜住。
影响：UI 无法区分「网络断开」与「Agent 真失败」；类型收窄在 projector.ts:469 被绕过。
修复：删前端死分支统一依赖 run_failed + end_reason；后端补齐 end_reason 字段并重新生成 TS。
M2. ServiceError.cause 覆盖原生 Error.cause，错误链/堆栈丢失
位置：services/types.ts:17,19-28；lib/logger.ts:332
问题：手写 public readonly cause? 覆盖原生语义，且构造未 super(message,{cause})。normalizeToServiceError 保存的底层 ky HTTPError/TimeoutError 堆栈在 logError 落盘时完全丢失。
影响：违反「错误日志必须有堆栈/上下文」铁律，线上排障只能看到表层 message。
修复：删自定义 cause，用 super(message,{cause})；logger.ts 递归展开 error.cause。
M3. RightPanel 的 Sources tab 完全不可达
位置：components/right-panel/RightPanel.tsx（activeTab = selectedChildTurnId ? "subagent" : "outputs" + handleTabChange 中 clearSelection()）
问题：点 Sources tab → handleTabChange("sources") → 非 subagent → clearSelection() → selectedChildTurnId=null → 重渲染 activeTab="outputs"，Radix 受控 value 被强制拉回 outputs。同理点 subagent tab 在无选中时也无反应。
影响：用户永远看不到 Sources（引用来源/规则文件），MCP/Context 内容也不可达。
修复：tab 状态与 selectedChildTurnId 解耦，Sources 应独立可达。
M4. LogsPage VirtualList getKey 在 trace_id 非空时返回重复 key
位置：pages/logs/LogsPage.tsx（getKey={(entry,index)=>entry.trace_id || \
𝑒
𝑛
𝑡
𝑟
𝑦
.
𝑡
𝑠
:
:
entry.ts::{index}`}`）
问题：同一 trace 的多条日志 trace_id 相同 → key 完全相同 → React 重复 key 警告 + 列表渲染错乱/展开状态串台。
修复：key 始终含 index，如 \
𝑒
𝑛
𝑡
𝑟
𝑦
.
𝑡
𝑟
𝑎
𝑐
𝑒
𝑖
𝑑
?
?
𝑒
𝑛
𝑡
𝑟
𝑦
.
𝑡
𝑠
:
:
entry.trace 
i
​
 d??entry.ts::{index}``。
M5. contextUsageStore 未随 openTask 切换清理
位置：hooks/useSSE.ts:183（reset() 仅在 connect 调用）；hooks/useTask.ts:307-317（openTask 不 reset）
影响：切历史任务时，前一个流式 task 的 context_usage 事件可能覆盖新 task 的圆环显示，短暂显示旧占用。
修复：openTask 切任务前 reset()，且 SSE flush 时校验 event.task_id === activeTaskId 才写。
M6. 乐观临时 ID 用 temp-${Date.now()}，同毫秒并发碰撞
位置：hooks/useTask.ts:120,217
影响：并发发消息时临时 turn/task id 重复 → upsertTurn/replaceTurnId 互相覆盖/误删，偶发丢消息。
修复：用 crypto.randomUUID()。
M7. ChatPanel.timelineTurns 流式期每帧 O(events) 重算
位置：components/layout/ChatPanel.tsx（useMemo 依赖 events，每帧遍历全量 events 收集 childTurnIds + sort）
影响：长会话高频事件下性能隐患。
修复：memo childTurnIds 或用更廉价信号触发。
M8. 后端断连/重连无 UI 反馈，transportError 存了没人渲染
位置：components/backend/BackendErrorBanner.tsx（仅 failed 时渲染）；backendStore.transportError 无任何组件消费
影响：SSE 断开、后端命令失败只在 store 里，用户看不到任何提示。
修复：渲染 transportError；补充「disconnected/reconnecting」横幅。
🟡 低严重（可访问性 / 视觉 / 可维护性）
L1：ToolCallCard.tsx:212-276 isChangeLayout 折叠态外层 div[role=button] 内嵌套真实 <Button>，可访问性语义混乱（键盘 Tab/屏幕阅读器）。
L2：CodeBlock 的 isLastLeaf 恒为 false（AgentMessage 始终传 false），以代码块结尾的消息光标渲染在 div 外空白区，视觉割裂。
L3：conversationTraceStore.ts docstring 中文乱码（GBK 错误编码存储），文档不可读 → 以 UTF-8 重新保存。
L4：eventStore.processedEventIds 的 Set 只增不减，长会话缓慢涨内存 → 按 task 分桶或惰性清理。
L5：useDelegationStreams.forceBackfill 每个 child turn 失败都全量 listTaskEvents，N 个并发失败 → N 次全量拉取 → 按 taskId 聚合去重。
L6：delegationStore.selectedChildTurnId 跨 task 不清理 → 切 task 后旧选中残留。
L7：lib/markdown/highlight.ts 高亮无跨实例缓存，超大代码块 lowlight.highlight 同步阻塞主线程（已 memo，但建议加缓存）。
L8：workspaceEventStore 模块级 setStatusRef/clearInflight 裸调 setState，绕过 store actions → 收归为内部 action。
L9：useChanges.applyUpdatedDiff 预览强制 last_tool_call_id:""，去抖刷新前短暂不一致。
L10：MarkdownStream 注释声称用 rAF 实际仅 setTimeout（轻微）。
建议的修复优先级
立即修（高）：H4（首次体验硬伤）、H3（数据安全）、H1/H2（并发串扰，你已确认桌面端支持并发）。
尽快修（中）：M1（协议一致性）、M2（日志铁律）、M3（功能不可达）、M4（React 重复 key）、M8（断连无反馈）。
排期修（低）：L1–L10 可维护性/视觉/性能优化。

---

## 独立复核结论（2026-08-17，基于当前代码事实）

主 Agent 派发 4 个独立审查子 Agent，依据 CodeGraph + 读源码逐项核实。结论：**报告写于修复落地之前，H1–H4、M3/M4/M5/M6/M7/M8 均已修复；真正仍残留问题的是 M1、M2（修复了但问题还在）**。

| 项 | 报告判定 | 复核结论 | 关键证据 |
|----|---------|---------|---------|
| H1 | 高·未修 | ✅ 已修复 | `turnStore.ts:28` `streamingTurnIds: Record<taskId,turnId>`；`setStreamingTurn(taskId,turnId)` 按 task 维度且有 `if(!(taskId in...))return state` 防护；InputBar:44 读 `streamingTurnIds[activeTaskId]`；`syncRuntimeStatus(useSSE.ts:307)` 用 `event.task_id` 作用域 |
| H2 | 高·未修 | ✅ 已修复 | `useSSE.ts:87` `connectionsRef=useRef<Map<turnId,SSEConnection>>`；`connect` 只断同 turnId；`disconnectTurn(turnId)` 仅断单 turn；useTask 用 `disconnectTurn(temporaryTaskId)` 无全局 kill |
| H3 | 高·未修 | ✅ 已修复 | `httpClient.ts:68-76` `retry.statusCodes:[]` + `shouldRetry:!isHTTPError` → 仅网络层错误重试，送达后 5xx 不重放级联删除 |
| H4 | 高·未修 | ✅ 已修复 | `httpClient.ts:32` 注释机制已落地；`api.ts:83/100` `post()` 透传 `timeout`；`api.ts:324` `prepareWorkspace` 传 `{timeout:false}`；后端 `workspaces_api.py:267` `PREPARE_TIMEOUT_SECONDS=60` 兜底 |
| M1 | 中·未修 | ⚠️ **修复了但问题还在** | 前端死分支已删（`projector.ts:465`/`useSSE.ts:337`/`StatusBadge.tsx:69` 统一读 `run_failed+end_reason`）；**但后端 `RunFailedPayload`（run_failed_payload.py:29-43）无 `end_reason` 字段**，`runner.py:391` 与 `turn_stream_service.py:_emit_run_failed` 都没塞 `end_reason` 进 payload，`end_reason` 只落 `turns` 表行（`runner.py:425`）。前端 `payload.end_reason` 永远 `undefined`，区分「网络断开 vs 真失败」能力未真正生效 |
| M2 | 中·未修 | ⚠️ **修复了但问题还在** | 构造器已修：`types.ts:28` `super(message,{cause})` 用原生 cause；`httpClient.ts:58/62` 传 `cause:error`。**但 `logger.ts:327-347` `logError` 只取 `error.stack`/`error.message`，无 `error.cause` 递归展开**，底层 ky `HTTPError/TimeoutError` 堆栈落盘丢失，仍违反日志铁律 |
| M3 | 中·未修 | ✅ 已修复 | `RightPanel.tsx:89-91` `activeTab` 改本地 `useState`；`handleTabChange` 先 `setActiveTab` 再 `clearSelection`（仅非 subagent）；Sources tab(127-130) 与 `SourcesTab` 内容已接 |
| M4 | 中·未修 | ✅ 已修复 | `LogsPage.tsx:255` `getKey` 改为 `` `${entry.trace_id??"no-trace"}::${entry.ts}::${index}` `` 始终含 index |
| M5 | 中·未修 | ✅ 已修复 | `useSSE.ts:199` connect 内 `reset()`；`useTask.ts:313-323` openTask 填历史前已 `reset()` |
| M6 | 中·未修 | ✅ 已修复 | `useTask.ts:120/220` 两处 `temp-${crypto.randomUUID()}` |
| M7 | 中·未修 | ✅ 已修复 | `ChatPanel.tsx:149-205` `childTurnIds` 增量缓存；`timelineTurns` 依赖收窄为 `[activeTask,turns,childTurnIds]`（:243），不再依赖原始 `events` |
| M8 | 中·未修 | ✅ 已修复 | `BackendErrorBanner.tsx:54/58-69` 已读 `transportError` 并在 running/ready 时渲染琥珀色「连接中断·正在恢复」横幅 |

### 待办（真正残留，按优先级）
1. **M1（协议一致性·中）**：后端给 `RunFailedPayload` 增加 `end_reason: str | None = None` 字段，并在 `runner.py:391`（client disconnect 路径）与 `turn_stream_service.py:_emit_run_failed` 填入 `end_reason="client_disconnected"`；重新生成 `apps/shared/ts/events.ts`。需后端改动 + TS 协议重生成。
2. **M2（日志铁律·中）**：`logger.ts:logError` 递归展开 `error.cause`（沿 `.cause` 链逐层收集 stack/message），避免底层 ky 错误堆栈落盘丢失。纯前端改动，风险低、优先级高。

### 说明
- H1–H4 的「已修复」状态与报告描述冲突，原因是报告写于修复前；本次复核以读源码为准。
- L1–L10（低严重）本次未逐项复核，不在本次范围。
# 长历史对话与流式渲染性能改造方案

> 状态：方案阶段，尚未实施。
>
> 本方案针对 `coding-agent` 长历史对话下的流式渲染卡顿、滚动追赶和首屏加载成本。
> 它建立在现有 Assistant Transport 增量快照方案之上，不改变 Conversation snapshot 与
> Runtime context 的职责边界，也不引入远程服务、队列、Redis 或其他分布式设施。

## 1. 目标与结论

长对话的性能问题不是一个单点问题，而是四个成本叠加：

```text
模型 chunk
  -> snapshot mutation / SQLite commit
  -> Transport 通知与 flush
  -> frontend state converter
  -> message identity / GroupedParts
  -> Markdown、tool UI、DOM、滚动
```

目标是让一次文本增量只影响：

1. 当前正在生成的 text/reasoning part；
2. 必要的运行状态和自动滚动；
3. 视口附近真正需要挂载的消息。

历史消息必须保持稳定引用、稳定 row identity 和可恢复事实，但不应在每个增量上重新
转换、重新处理或重新绘制。

推荐的实施顺序是：

```text
Phase 0  建立性能基线
Phase 1  converter 结构共享与消息身份稳定
Phase 2  （可选）Transport 文本增量合帧
Phase 3  长线程消息虚拟化与滚动控制
Phase 4  历史窗口/分页与首屏加载治理
```

Phase 0 和 Phase 1 是默认必须项；Phase 3 在数百条消息或重型 tool UI 的 Task 上按基线
启用；Phase 2 不进入默认实施路线，只有性能基线证明 Transport 帧频或 backend flush 是
瓶颈时才考虑；Phase 4 在确实达到数千条消息、单 snapshot 或首屏 JSON 已成为瓶颈时实施。

## 2. 架构边界

### 2.1 进程与事实所有权

本项目仍是单用户本地桌面 Agent：

```text
Tauri / React UI 进程
  -> localhost Assistant Transport
本机 FastAPI backend 进程
  -> LangGraph / Runtime / Tool subprocess
  -> SQLite
```

- backend 进程拥有 Agent 执行、Conversation snapshot、Run、checkpoint、工具和文件事实。
- `ConversationTaskSnapshotService` 继续是 Transport/UI snapshot 的唯一 owner。
- `RuntimeContextManager` 继续是 LLM context 的唯一事实源；UI snapshot 不能反向写入 context。
- React state、assistant-ui runtime state 和性能缓存都只是不可持久化的渲染副本。
- Assistant UI 类型和实现只存在于 `apps/desktop`；backend 不依赖 React 或 assistant-ui。

本方案会跨越现有的本机 HTTP/Assistant Transport 进程边界，但不会改变边界。后端由现有
Tauri supervisor 启动、停止和报告 readiness；backend 崩溃后，内存 snapshot/notifier/HTTP
连接丢失，新的 backend 从 SQLite 恢复，前端重新从 state endpoint 或 attach 流 hydrate。
可选的性能合帧不能成为恢复事实，未提交的 Transport 更新可以丢失，已提交 snapshot 必须
可重新读取。

模型供应商请求仍按现有 provider 配置离开本机；本方案不新增任何网络数据流。

### 2.2 不改变的持久化原则

后端持久化与 Transport 推送必须保持：

```text
领域 mutation
  -> snapshot/context/Run 按现有 owner 规则提交
  -> SQLite commit 成功
  -> 通知 Transport subscriber
```

如果未来启用合帧，它只允许发生在“已提交 snapshot 到某个 HTTP subscriber 的协议输出”
之间。默认路线不依赖合帧；无论是否启用，都不能为了获得更平滑的 UI，把 snapshot 延迟
提交、只写内存，或重新引入 outbox/revision/distributed queue。

这和已有的 [incremental Assistant Transport snapshot plan](./incremental-assistant-transport-snapshot-plan.md)
一致：`controller.state` 是单个连接的协议副本，不是新的业务事实源。

## 3. 当前代码事实

### 3.1 Assistant Transport 是全 state + converter 模型

当前桌面端在
`apps/desktop/components/assistant/assistant-runtime.tsx` 使用
`useAssistantTransportRuntime`，协议为 `assistant-transport`，并提供：

- `initialState`：由 `useAssistantInitialState` 从
  `/tasks/{taskId}/assistant/state` 首屏加载；
- `resumeApi`：`/tasks/{taskId}/assistant/attach`；
- `converter`：`toTransportThreadView`；
- `body` / `prepareSendCommandsRequest`：发送 Task、workspace、provider、model 和 run 信息。

这符合 assistant-ui 官方 Assistant Transport 模型：backend 流式传输 agent state snapshot，
frontend converter 将 snapshot 转成 UI message。官方文档明确说明 Assistant Transport
是建立在 `ExternalStoreRuntime` 之上的 state-streaming protocol，而不是直接把后端消息
组件化渲染。

参考：[Assistant Transport](https://www.assistant-ui.com/docs/runtimes/custom/assistant-transport)。

### 3.2 converter 每次更新都会重建完整历史

`apps/desktop/lib/assistant/converter.ts` 的 `toTransportThreadView` 当前会：

```ts
const messages = state.messages.map((message) =>
  toThreadMessage(message, ...),
);
```

`toThreadMessage` 会继续创建新的 metadata、parts、tool artifact 和派生字段。即使历史
消息内容没有变化，新的 `ThreadMessage` 和 `parts` 引用仍然会产生。

当前运行时内部会在每个 Assistant Transport state chunk 后重新计算 converted state。对
`ExternalStoreRuntime` 而言，新的 messages array 和新 message object 会使历史消息也进入
adapter 更新路径；`MessagePrimitive.GroupedParts` 收到新的 parts array 后也无法复用旧 tree。

因此，当前最先需要治理的是对象身份与结构共享，而不是先增加更多 Markdown 优化。

### 3.3 当前 Thread 会挂载完整消息列表

`apps/desktop/components/assistant-ui/elements/thread.aui.tsx` 当前使用：

```tsx
<ThreadPrimitive.Messages>{() => <ThreadMessage />}</ThreadPrimitive.Messages>
```

当前代码中没有发现 `unstable_useThreadMessageIds`、
`ThreadPrimitive.Unstable_MessageById`、`MessageByIndex` 或
`@tanstack/react-virtual`。因此历史消息全部处在 React tree 中；即使浏览器可以通过
`content-visibility` 减少部分 paint，React 更新和自定义 tool UI 的成本仍然存在。

当前 viewport 还带有 `scroll-smooth`。在流式内容高度变化时，它可能放大自动跟随底部的
视觉追赶，但它不是 converter 重建的根因。

### 3.4 backend 当前没有文本增量合帧（可选优化的背景）

`apps/backend/app/core/workflows/nodes/model_node.py` 对 `model.astream(messages)` 的
每个 chunk 发布 Assistant text/reasoning delta。

`ConversationTaskSnapshotService` 将 mutation 应用到内存快照、校验、写 SQLite，并在
`_apply_snapshot_change` 的路径上调用 `controller.flush()`。`TransportAssistantService`
则从 subscriber queue 逐个取 `SnapshotChange`，当前没有按文本窗口合并相邻 change 的
逻辑。

这会让前端收到不均匀的 state update：模型或 SQLite 形成 burst，React 再对每个 burst 做
一次完整 converter/render 工作，于是体感上出现“卡一下、跳一段”。但合帧只是在减少
更新次数，不能解决 converter 对整段历史的重建，因此属于可选的后置优化，不是长历史
对话的主修复路径。

### 3.5 Markdown 已经启用 defer，但它只能降低优先级

`apps/desktop/components/markdown-text.tsx` 已启用：

```tsx
mode="streaming"
defer
```

官方 Streamdown 文档对 `defer` 的定义是使用 `useDeferredValue` 把 Markdown parsing 放到
较低优先级，以便保持输入和滚动响应。它不能阻止上游把所有历史 message 重新转换，也
不能减少 backend state frame 数量。

参考：[Streamdown Markdown Renderer](https://www.assistant-ui.com/docs/guides/streamdown)。

## 4. 目标运行模型

### 4.1 四类更新分开治理

| 更新 | 是否影响 canonical snapshot | 是否需要每次立即显示 | 允许合并方式 |
| --- | --- | --- | --- |
| 当前 text/reasoning append | 是 | 需要保持自然流式显示 | 默认逐 mutation；未来可在同一路径短窗口合并 |
| 新 message / 新 part | 是 | 需要尽快显示 | 默认立即传输；若未来合帧，不得越过结构边界 |
| tool 状态、approval、error | 是 | 需要立即显示 | 不延迟 terminal/approval/error |
| run completed/failed/cancelled | 是 | 必须立即收尾 | 强制 flush，确保最终状态先于流结束 |

默认目标是降低每次 UI 更新的计算量，而不是减少 text mutation 数量；首先依靠稳定引用
和按需挂载。若后续启用合帧，目标才是降低 UI 连接看到的无意义 frame 数量，并保证最后
一个可见状态和 durable snapshot 一致。

### 4.2 更新后的数据流

```text
model chunk
  -> Runtime mutation
  -> snapshot owner: validate + SQLite commit
  -> Transport subscriber（默认逐 mutation）
  -> controller.state apply mutations
  -> controller.flush()
  -> stable converter output
  -> only active message / visible rows update
```

如果 Phase 2 后置启用，每个 HTTP 连接再拥有独立的 coalescer 和 `controller.state` 副本。
无论是否启用，一个连接的慢渲染不能阻塞另一个连接，也不能改变 SQLite snapshot。

## 5. Phase 0：性能基线与可观测性

在任何结构性改造前，先记录同一个 Task 的以下指标：

### 5.1 frontend 指标

在开发诊断模式中增加非生产噪声日志或 profiling hook：

- 初始 `messageCount`、`partCount`、总文本字符数；
- 每秒收到的 Transport state update 数；
- 每次 converter 执行耗时；
- 新建的 message/part 数量与可复用数量；
- React commit 次数、最长 commit 时长；
- Markdown parse 次数和耗时；
- viewport scroll event 次数及是否处于底部跟随；
- 当前可见消息数与已挂载消息数。

禁止在日志中输出 prompt、token、tool 参数或文件内容；只记录 count、size、duration、
run_id 和 trace_id。

### 5.2 backend 指标

在结构化日志中增加：

- model delta 到达时间；
- snapshot mutation apply / SQLite commit 耗时；
- commit 后 publish 时间；
- subscriber queue 入队、出队和 Transport flush 数量；若启用 Phase 2，再记录合帧数量；
- 每个 flush 包含的 mutation 数、字符数和时间跨度；
- stream 的 first snapshot、first text、terminal snapshot 时间。

### 5.3 验收基线

使用三组本地 fixture：

1. 20 条普通消息；
2. 200 条消息，包含普通 Markdown；
3. 1000 条消息，包含 reasoning、tool trace、diff 和 terminal 展示。

每组分别测试：首屏加载、历史滚动、发送新消息、纯 text streaming、reasoning streaming、
tool 生命周期和断线重连。

## 6. Phase 1：converter 结构共享与消息身份稳定

### 6.1 设计

把当前导出的无状态转换函数拆成“每个 runtime/task 一个生命周期 converter”：

```ts
type TransportViewConverter = (
  state: TransportState,
  metadata: ConnectionMetadata,
) => AssistantTransportState;

function createTransportViewConverter(): TransportViewConverter;
```

在 `RuntimeSession` 或 `useTaskAssistantTransportRuntime` 内用 `useMemo` 按 `taskId` 创建，
task 切换时销毁，单个 Task 的一次运行期间保持 converter cache。不得使用 module-global
cache，避免不同 Task 互相持有消息和 tool artifact。

### 6.2 复用规则

缓存必须以 backend message id、part id/稳定位置和影响展示的字段为依据：

- 未改变的 message 复用上一次 `ThreadMessage` 对象；
- 未改变的 parts 复用上一次 parts array 和 part object；
- 只有当前 append 的 text/reasoning part 创建新 part；
- tool artifact 只有状态、args、result 或 presentation 变化时才重建；
- `error`、`isRunning`、latest-run 标识等 message-level 派生值变化时，只重建受影响消息；
- pending command 消息单独缓存，不要让它导致所有历史消息重建；
- 删除、重排、run 切换或无法证明结构共享时，按 message 粒度安全失效，不直接假设全局
  引用有效。

优先依赖 Assistant Transport accumulator 的 immutable snapshot / path update 语义，使未
修改数组项保持引用；如果某条后端路径会复制整个 `messages` 数组，则使用稳定 message
id + 内容 fingerprint 作为兜底。fingerprint 只能用于 UI cache，不能写入 domain state。

### 6.3 converter 输出约束

converter 仍然只做中性 snapshot 到 assistant-ui 的适配：

- 不把 Assistant UI 类型带入 backend、storage、core 或 service；
- 不把 UI cache 当作下一次请求的 authority；
- 不从 `result`、`args` 或字段存在性猜测 tool 领域状态；
- 不在 converter 中做 context compaction、历史裁剪或事实修复；
- `state` 返回值继续指向当前 Transport state，供 runtime 处理，不持久化到 localStorage。

### 6.4 配套渲染调整

- 保持 `assistantMessageGroupBy` 为模块级稳定函数；如果改回按 type 分组，优先采用
  assistant-ui 的 `groupPartByType`，因为官方文档说明该 helper 提供稳定 memo fingerprint。
- `MESSAGE_COMPONENTS`、Markdown `security`、`linkSafety` 配置提升为模块级常量，避免
  每次 render 重建 plugin/config 对象。
- 检查 `ToolPart`、reasoning、diff、terminal 等组件的 props，确保它们只接收稳定字段，
  不把每次 converter 创建的新 wrapper object 作为 props。

参考：[Message primitives / GroupedParts](https://www.assistant-ui.com/docs/primitives/message)。

### 6.5 Phase 1 验收

- 200 条历史消息 streaming 时，历史 message 的转换命中率应接近 100%；
- 每个 text chunk 只有当前 text part 及其父 message 发生引用变化；
- 历史 tool、diff、terminal 组件不因当前文本增量重复 mount；
- converter 的单次耗时不随历史消息数量线性增长，或增长仅来自必要的轻量 id 扫描；
- 不改变 snapshot wire contract、resume、cancel、tool lifecycle 和现有 unit tests。

## 7. Phase 2（可选）：Transport subscriber-local 合帧

本阶段不属于默认实施路线。由于 text/reasoning 的增量合帧会改变前端看到的更新粒度，默认
保留当前逐 mutation 的流式显示，以优先保证自然的打字感和最小的协议语义变化。只有
Phase 0–1、containment 和必要的虚拟化完成后，性能数据仍明确显示“Transport frame 数量
或 backend flush 调度”是瓶颈，才启动本阶段。

### 7.1 合帧位置

合帧放在：

```text
ConversationTaskSnapshotService commit/publish
  -> TransportAssistantService subscriber stream
  -> assistant-stream controller
```

不要在 `ConversationTaskSnapshotService` 中为了 UI 而延迟 canonical snapshot commit，也不
要让 `ConversationEventProjector` 知道 React 帧率。

建议新增明确命名的本地组件，例如：

```text
apps/backend/app/assistant_transport/service/transport_frame_coalescer.py
```

它只接收已经提交的 `SnapshotChange`，输出有序的 mutation batch；不保存业务事实，不跨
Task 共享状态，不写数据库。

### 7.2 合并规则（仅在 profiling 证明必要时使用）

如果启用，目标窗口应接近一个渲染帧（约 8–16ms）；约 50ms 只能作为硬上限，不能作为
默认刷新周期。还需要最大字符数和最大 mutation 数上限，不能让低速模型或无新事件的流
无限等待。合帧窗口必须通过用户体感和 profiling 校准，不能为了降低 frame 数牺牲流式
打字效果。

允许：

- 同一 text/reasoning path 上相邻的 `append-text` 合并为一个 append；
- 同一连接窗口内连续、互不冲突的最小 `set` 按原顺序发送；
- 同一路径的非终态展示字段在不改变语义时保留最后一次值。

禁止：

- 把 `set` 和 `append-text` 在结构边界两侧错误合并；
- 丢弃新 message、新 part、tool result、error、approval 或 run terminal 状态；
- 用完整 root snapshot 代替正常增量路径；
- 在 subscriber 还没收到 terminal 状态时结束 stream。

遇到结构变化、tool 状态、错误、取消或终态时，先把前面的文本 batch flush，再立即发送
结构/终态 mutation。终态处理必须最终 flush 一次，确保 controller state 与已提交 snapshot
一致。

### 7.3 与断线和崩溃的关系

- 合帧中的未发送 mutation 不是 durable state；连接关闭时可以丢弃。
- attach/reconnect 仍从最新 SQLite snapshot hydrate，不依赖旧 subscriber queue。
- backend 崩溃后 Tauri supervisor 按现有生命周期重启；新连接拿到完整 snapshot，不能从
  旧进程内存 coalescer 恢复。
- 取消时先停止后续模型/工具工作，flush 已提交的取消状态，再关闭本地 stream。

### 7.4 Phase 2 验收（启用本阶段时才执行）

- 纯 text streaming 的 wire frame 数明显下降，且最终文本完全一致；
- `append-text` value 仍然只包含新增文本；
- tool pending/running/completed/failed/cancelled、error 和终态没有被延迟到错误顺序；
- SQLite snapshot 与重连后 UI 完全一致；
- 流关闭、客户端取消、backend 重启不产生悬挂 subscriber 或永不结束的请求。

## 8. Phase 3：长线程虚拟化与滚动控制

### 8.1 assistant-ui 官方建议的适用范围

assistant-ui 官方 Thread Virtualization 文档指出：默认 kit 已使用
`content-visibility: auto` 和 `contain-intrinsic-size` 降低屏幕外消息的 paint 成本；只有当
React mount/update 本身成为瓶颈，通常是数百到数千条消息或重型消息内容时，才需要虚拟化。

当前自定义 Thread 没有发现这些 CSS，也没有虚拟化实现。因此建议：

1. 先补齐轻量的屏幕外内容 containment；
2. 在达到阈值的 Task 上使用虚拟列表；
3. 不把虚拟化当作 Phase 1 converter 问题的替代品。

参考：[Thread Virtualization](https://www.assistant-ui.com/docs/guides/virtualization)。

### 8.2 推荐实现形态

新增 desktop UI 内部的 `VirtualizedThreadMessageList`，不把 virtualizer 类型泄漏到
backend 或 domain。实现遵循官方文档：

- 使用 `unstable_useThreadMessageIds` 获取稳定 message id 数组；
- 优先使用 `ThreadPrimitive.Unstable_MessageById`，以 message id 作为 row identity；
- `MESSAGE_COMPONENTS` 保持模块级稳定引用；
- 使用 `@tanstack/react-virtual` 的正常文档流 spacer + `measureElement`，不使用绝对定位
  覆盖消息 CSS；
- 对 user message 加后续 assistant/tool/reasoning response 组成稳定 turn row，避免流式
  高度变化导致大量 row 重排；
- active streaming message 保持 mounted，并留出合理 overscan；
- 删除、重排、分支和新消息插入时以 id 重新计算 row，不复用过期 index。

assistant-ui 文档明确提醒：内置 `ThreadPrimitive.Viewport` 的 auto-scroll 假设所有消息
都 mounted；虚拟化时应由自定义 scroll owner 管理 auto-follow、测量调整和 run-start jump。

因此虚拟化版本不应继续简单叠加当前的 `ThreadPrimitive.Viewport` 自动滚动：

- 去掉 `scroll-smooth`，先使用确定性的 instant/auto 行为；
- 仅在用户仍位于底部时跟随流式高度变化；
- 用户向上滚动后解除 sticky follow；
- 用户返回底部后重新 armed；
- virtualizer re-measure 时禁止和 auto-scroll 互相写 scrollTop；
- run 开始时在 paint 前跳到底部，避免新 assistant message 闪现到 fold 外。

### 8.3 是否默认启用

建议不要在运行中随着消息数量跨阈值突然切换两套 DOM 结构。可选策略按优先级为：

1. 首选：统一使用稳定的 message list 抽象，短线程使用普通列表，长线程在首次挂载时按
   初始消息数选择 virtualized renderer；
2. 如果后续实测切换会导致滚动/selection 问题，则统一使用虚拟列表，利用较大的 overscan
   覆盖普通 Task；
3. 暂不为少量历史消息引入虚拟化，只使用 containment + Phase 1 结构共享。

阈值必须来自 profiling，不写成产品事实；初始实验可以从 200 条消息或 100 个 turn 开始。

### 8.4 Phase 3 验收

- 1000 条消息时，mounted message 数量接近 viewport + overscan，而不是 1000；
- 用户在历史顶部、中部、底部滚动时不出现 row 错位、重复或丢失；
- streaming 在底部时跟随正常，用户上滑后不会被强行拉回；
- re-measure 不产生 rubber-band 或 scrollTop 争抢；
- tool/diff/terminal 等重型展示的展开、折叠、复制和跳转仍然可用；
- attach、cancel、retry、edit、branch 行为不依赖 index 稳定性。

## 9. Phase 4：历史窗口、分页与首屏治理

虚拟化只减少 DOM/React mount 成本，不能减少以下成本：

- `/assistant/state` 首屏传输完整历史；
- Assistant Transport root `set` 携带完整 `messages`；
- converter 对完整 state 做 id/状态扫描；
- snapshot 单行 JSON 的读取、解析和深拷贝；
- context 构建和 compaction 的 token 成本。

因此，当历史达到数千条或单 Task snapshot 达到实际预算时，再设计“canonical full history
+ bounded UI window”，而不是在前端简单 `slice()` 丢掉旧消息。

### 9.1 目标形态

```text
backend canonical snapshot/context
  └── 完整、可恢复、按现有 owner 写入

Assistant UI transport projection
  └── 当前窗口 + has_more + history cursor
```

旧消息仍由 backend 事实源保存；UI 只加载当前窗口。向上滚动时通过明确的 history API 或
custom command 请求更早消息，加载后按 message id 前插，并保持当前 scroll anchor。

### 9.2 需要单独设计的协议问题

Assistant Transport 当前契约是 full state snapshot + converter，不能把分页字段偷偷塞进
现有 `messages` 语义。Phase 4 需要单独确定：

- history cursor 与 Task/run/thread 的绑定和失效规则；
- 前插历史是否通过 custom command、专用 API，或扩展 state schema；
- append-text 路径在窗口前插和消息重排后的定位规则；
- 当前 active run 期间加载历史是否允许；
- 删除、编辑、分支、重连时 cursor 如何失效；
- 断线后是从完整 snapshot 还是窗口 snapshot hydrate；
- SQLite 是否继续使用单 JSON snapshot，还是将归档历史拆为可分页的本地表/文件。

这些会影响事实模型、数据库和对外 Transport 契约，不能在本性能方案中擅自决定。

### 9.3 与 context compaction 的关系

UI history window 不等于 LLM context compaction：

- UI 可以显示较小窗口，但 context 仍按 `RuntimeContextManager` 规则构建；
- context compaction 不能由 converter 或前端触发；
- system prompt、tool result、checkpoint 和 context sequence 不从 UI window 推导；
- 两者都需要历史 cursor/boundary 时，应由 backend 明确产生并持久化，而不是让前端猜测。

## 10. 文件与模块边界

### 10.1 Phase 0/1 前端

主要涉及：

```text
apps/desktop/lib/assistant/converter.ts
apps/desktop/components/assistant/assistant-runtime.tsx
apps/desktop/lib/assistant/use-task-assistant-transport-runtime.ts
apps/desktop/components/markdown-text.tsx
apps/desktop/components/assistant-ui/elements/thread.aui.tsx
apps/desktop/components/assistant-ui/tools/tool-part.tsx
```

建议新增的 cache/converter 工厂保持在 `lib/assistant/`，不要把 assistant-ui 适配类型放入
`lib/api` 或 backend 契约以外的共享 domain 层。

### 10.2 Phase 2 backend（后置可选）

主要涉及：

```text
apps/backend/app/assistant_transport/service/transport_assistant_service.py
apps/backend/app/assistant_transport/service/conversation_task_snapshot_service.py
apps/backend/app/assistant_transport/assistant_api.py
```

仅在 Phase 2 被 profiling 选中后，才建议新增 `transport_frame_coalescer.py` 或等价的
subscriber-local service。它必须不依赖 React，不进入 Runtime context，不拥有 SQLite
canonical state。

### 10.3 Phase 3/4

Phase 3 仅修改 `apps/desktop` Thread composition、样式和依赖；Phase 4 才讨论 backend
history API、snapshot storage 或 schema。不要为了 Phase 3 提前改变 Conversation facts。

## 11. 测试与验收闭环

### 11.1 frontend unit

新增/扩展：

- converter structural sharing：未变化 message/parts 引用保持不变；
- active text append：只有当前 message/part 变化；
- run status、error、tool lifecycle 导致正确粒度失效；
- pending command 不使历史消息失效；
- task 切换销毁 cache，不串 Task；
- virtual row id 在插入、删除、重排、分支后正确；
- history anchor 在前插消息后保持可见位置。

### 11.2 backend unit/integration（默认路线）

无论是否启用可选合帧，都必须覆盖：

- snapshot commit 失败不会发送对应 mutation；
- 多个 subscriber 互不共享可变 controller state；
- slow subscriber 不阻塞 snapshot owner 或其他连接。

如果 Phase 2 被 profiling 选中，再额外覆盖：

- 连续 append-text 合帧后文本完全一致；
- 合帧跨结构边界时不会改变 mutation 顺序；
- terminal/error/cancel/approval 强制 flush；
- subscriber 断开会清理 coalescer；
- reconnect 从 SQLite snapshot 恢复，不依赖内存 queue。

### 11.3 浏览器性能验收

用 Playwright/Chrome Performance 和 React Profiler 验证：

- 20/200/1000 条消息的 first contentful paint、首屏完成时间；
- streaming 时每秒 React commit 数、最长 commit、converter 总耗时；
- Markdown/tool/diff/terminal 的 mount/update 次数；
- scroll event、scrollTop 变化和用户上滑后的 sticky 状态；
- memory 使用和 Task 切换后的 cache 是否释放。

性能验收应使用固定 fixture 和固定模型输出，不以一次人工“感觉变顺”作为唯一标准。

## 12. 风险与取舍

### 12.1 过早虚拟化

assistant-ui 官方文档将虚拟化 API 标为 experimental，并建议只有 React mount/update 成为
瓶颈时使用。过早引入会增加 scroll owner、测量、anchor、selection 和 tool UI 交互复杂度。
因此先做 converter 结构共享和 containment，再按基线决定默认策略。

### 12.2 可选合帧造成延迟

合帧不是默认优化。若未来启用，窗口过大时用户会感到首 token 或工具状态延迟；窗口过小
时，React 仍被高频唤醒。必须使用时间、字符数、mutation 数三重上限，并对结构/终态提供
立即 flush。若没有明确 profiling 证据，应保持逐 mutation，以保留自然流式显示。

### 12.3 单 JSON snapshot 写放大

即使启用可选 Phase 2，也只减少 Transport 输出帧，不减少现有 durable mutation 次数。若
profile 证明 SQLite 写放大才是主要瓶颈，应另行设计“事务内批量 mutation”，仍须满足
commit-before-publish，并由 snapshot owner 统一实现，不能在 Transport 层偷偷绕过持久化。

### 12.4 full state Transport 与分页冲突

Phase 4 会影响 state schema、重连和 message path 稳定性，不能把前端截断当成解决方案。
在 Phase 4 之前，完整历史仍以 backend canonical snapshot 为准，UI 性能由 Phase 1 和
按需启用的 Phase 3 治理；Phase 2 不是前置条件。

## 13. 建议的落地顺序

1. 先实施 Phase 0，保存 20/200/1000 条消息的基线。
2. 实施 Phase 1 converter cache，并先用引用稳定性单测证明收益。
3. 补齐当前自定义 Thread 的 `content-visibility` containment，移除 `scroll-smooth` 做
   A/B 验证。
4. 若 200 条以上仍因 mounted/update 成本明显卡顿，再实施 Phase 3；优先按官方 id-based
   virtualization 和自定义 scroll owner 组合。
5. 如果上述改造完成后，profiling 仍显示 Transport frame/flush 调度是瓶颈，才评估是否
   启用 Phase 2；启用后补充合帧顺序、终态和重连测试。
6. 只有完整 state 的传输/解析/SQLite 成为实际瓶颈时，才启动 Phase 4，并先单独确认
   history window 的协议和事实模型决策。

## 14. 当前需要用户确认的架构决策

本方案可以直接进入 Phase 0/1 的实现设计；以下事项不应在未确认前写入长期架构规则：

1. 长线程是否默认启用虚拟化，还是仅对超过 profiling 阈值的 Task 启用；
2. 在 profiling 证明必要时，是否接受 Phase 2 带来的极短文本更新延迟；
3. Phase 4 是否需要真正的后端历史分页，以及是否允许为此调整 snapshot storage 形态。

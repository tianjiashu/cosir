# 长历史对话与流式渲染性能改造方案（按当前代码重设计）

> 状态：重新设计，尚未实施。
>
> 本文是性能改造方案，不是执行指令集合。它以当前工作树代码为事实基础；工作树中其
> 他未提交改动不属于本文范围，也不应因实施本文而回退。
>
> 明确决策：本轮不做 Transport subscriber-local 合帧。后端继续按已提交的
> `SnapshotChange` 逐次向订阅者发送；任何合帧、节流或背压改造另立方案。

## 1. 结论与范围

长历史对话的第一瓶颈在 WebView 内的“状态更新后如何转换和挂载”，而不是先改变
Assistant Transport 协议。当前应按以下顺序推进：

```text
P0  建立可复现的前后端基线与诊断
P1  稳定 Assistant Transport converter，并复用 assistant-ui 消息转换缓存
P2  修正自定义 Thread 的屏幕外 paint 成本与滚动行为
P3  只有 profile 证明 React mount/update 成为瓶颈时，才引入虚拟列表
P4  只有完整 state 传输/解析/SQLite 成为瓶颈时，另行设计历史窗口
```

本轮包含 P0–P2 的设计，P3 是有门槛的后续阶段，P4 不作为本轮实现前置条件。
Transport subscriber-local 合帧从默认路线和可选路线中都移除。

目标是让一次文本增量主要影响：

1. 当前发生变化的 assistant message/part；
2. 必要的运行状态和滚动跟随；
3. 当前视口真正需要绘制的内容。

不以“把历史从 canonical snapshot 中删除”或“让前端自行维护另一份对话事实”为代价。

## 2. 运行拓扑与不变边界

本项目仍是单用户、本机运行的桌面 Agent：

```text
Tauri Rust 主进程
├─ WebView2 / React
└─ FastAPI backend 子进程
   ├─ Agent Runtime / LangGraph workflow
   ├─ SQLite
   └─ 按需创建的工具子进程
```

- Tauri 是 backend 子进程生命周期的唯一所有者，负责启动、readiness、有限恢复和退出清理。
- React 只通过动态 runtime config 访问 localhost backend；不创建、停止或重启 backend。
- `ConversationTaskSnapshotService` 是 Transport snapshot 的唯一 owner。
- `ConversationEventProjector` 只把 conversation event 投影为 snapshot mutation，不维护
  第二套 Run 状态机，也不触碰 `RuntimeContextManager`。
- `RuntimeContextManager` 继续负责 LLM context 的 working copy、序列、消息存储和持久化；
  UI snapshot 不反向写入 context。
- `ConversationRunModel.status` 是 Run 生命周期唯一事实源；snapshot 读取边界只做现有的
  终态对账。
- React state、assistant-ui runtime state 和 converter cache 都是渲染副本，不写入
  `localStorage`，也不成为业务恢复依据。

backend 崩溃或重启时，进程内 subscriber、controller state 和 converter cache 都会丢失；
新进程从 SQLite 恢复，遗留 active Run 按现有规则收敛，不隐式重放。前端通过 state 端点和
attach 重新建立渲染状态。性能优化不得改变这一恢复语义。

## 3. 当前代码事实

### 3.1 当前 Transport 链路

当前实际数据流是：

```text
model.astream()
  → ModelChunkProcessor
  → LangGraph custom stream
  → WorkflowOperations.process_event()
  → ConversationEventProjector
  → ConversationTaskSnapshotService
       validate + SQLite commit + _publish
  → Subscriber.queue（每个 HTTP 连接独立）
  → AssistantTransportStreamService
       apply set / append-text + controller.flush()
  → assistant-stream decoder / accumulator
  → useAssistantTransportRuntime
  → converter
  → ExternalStoreRuntime / assistant-ui Thread
```

相关实现：

- workflow：`apps/backend/app/core/workflows/react/workflow.py`
- 运行时门面：`apps/backend/app/core/workflows/workflow_operations.py`
- 事件投影：`apps/backend/app/assistant_transport/service/conversation_event_projector.py`
- snapshot owner：`apps/backend/app/assistant_transport/service/conversation_task_snapshot_service.py`
- SSE 订阅与 mutation 编码：`apps/backend/app/assistant_transport/service/transport_stream_service.py`
- HTTP 入口：`apps/backend/app/assistant_transport/assistant_api.py`
- 前端 runtime 装配：`apps/desktop/components/assistant/runtime/use-runtime-transport.ts`

`ConversationTaskSnapshotService` 在事务提交后才发布 `SnapshotChange`。首帧是 root `set`，
后续文本使用 `append-text`，结构、工具状态、错误和终态使用 `set`。本轮不改变这些
mutation 语义，不把渲染性能策略放进 snapshot owner 或 projector。

### 3.2 当前前端 runtime 与关键问题

当前 runtime session 已完成以下职责拆分：

- `AssistantRuntimeSession` 在 `AssistantRuntimeProvider` 下装配 transport、recovery、
  cancellation、state commit bridge 和 Thread；
- `RuntimeControlBridge` 区分 attach/resume 控制；
- `useRuntimeRecovery` 负责业务恢复；
- `useRuntimeTransport` 负责 API、错误、终态补偿和 Assistant Transport callbacks；
- `TransportStateCommitBridge` 只把 runtime 当前 state 同步到进程内最新引用，不是新的事实源。

但当前还有两个确定的渲染成本：

1. `useRuntimeTransport` 将
   `converter: (state, connectionMetadata) => toTransportThreadView(...)` 作为 inline
   函数传入。该函数身份随 hook render 改变；assistant-ui 的 `useConvertedState` 会把
   converter 身份作为 memo 依赖，因此即使 state 本身没有实质变化也可能重新转换。
2. `toTransportThreadView` 每次都会遍历所有 Run 和所有 message，并直接调用
   `toThreadMessage`。未修改历史 message 虽然通常保持输入对象引用，但当前转换层没有
   按输入对象复用转换结果的机制。

这比“当前后端没有合帧”更适合作为第一修复目标。后端当前确实在
`AssistantTransportStreamService._apply_snapshot_change` 中对每个 change 应用 mutation
后立即 `controller.flush()`，但本轮只测量，不修改它。

### 3.3 assistant-stream 的引用事实

当前桌面安装的 `assistant-stream` accumulator 对嵌套 mutation 使用结构共享：

- `append-text` 会复制从 root 到目标 path 的数组/对象；
- 未命中的兄弟分支保持原引用；
- 因而历史 Run、未修改 message 和未修改 part 通常能保持引用稳定；
- 当前活动 text/reasoning part 变化时，只有其祖先路径及目标对象需要变化。

这使“以稳定输入身份为键的前端转换缓存”可行，但不能直接假设所有对象永远稳定：root
set、编辑/重排、重连 hydrate、错误投影和不兼容的 future mutation 都必须触发安全失效。

### 3.4 当前 Thread 与 Markdown

`apps/desktop/components/assistant-ui/elements/thread.aui.tsx` 当前使用官方的
`ThreadPrimitive.Viewport` 和 `ThreadPrimitive.Messages` children render function；没有
虚拟列表，也没有发现 `content-visibility` 或 `contain-intrinsic-size` 样式。当前 viewport
还使用 `scroll-smooth`。

assistant-ui 官方文档说明：默认 kit 使用 `content-visibility: auto` 与
`contain-intrinsic-size` 降低屏幕外消息的 paint 成本；这不等于减少 React mount/update。
当前自定义 Thread 不自动获得默认 kit 的全部样式，因此要先补齐轻量 containment，再测量
是否仍需虚拟化。

当前 `apps/desktop/components/markdown-text.tsx` 使用
`StreamdownTextPrimitive` 的 `mode="streaming"`、`defer`、caret 和链接安全配置。
`defer` 只能降低 Markdown 解析优先级，不能修复上游重复转换，也不能减少 Transport 更新数。

### 3.5 当前依赖事实

当前锁定/安装的关键版本包括：

- `@assistant-ui/react` `0.15.17`
- `@assistant-ui/core` `0.3.16`
- `@assistant-ui/react-streamdown` `0.3.13`
- desktop `assistant-stream` `0.3.40`（由 npm 依赖树提供）
- backend `assistant-stream` `0.0.36`（`apps/backend/pyproject.toml`）

JavaScript 与 Python 包版本号不相同本身不代表不兼容；但每次依赖升级必须执行真实的
`set`、`append-text`、终态、取消和重连互操作测试。不要因为性能方案而盲目升级或锁成相同
版本号。

## 4. Assistant UI 官方依据与适用结论

本方案以以下官方文档和当前安装包源码为准：

- [Assistant Transport](https://www.assistant-ui.com/docs/runtimes/custom/assistant-transport)：
  Assistant Transport 是建立在 `ExternalStoreRuntime` 上的 state-streaming protocol；
  backend 发送 state，frontend converter 映射为 UI messages。
- [Message conversion API](https://www.assistant-ui.com/docs/api-reference/external-store/message-conversion)：
  `unstable_createMessageConverter` 可按 external message 输入复用转换结果；该 API 仍为
  unstable，使用时必须锁定版本并保留回归测试。
- [Thread](https://www.assistant-ui.com/docs/primitives/thread)：
  普通线程使用 `ThreadPrimitive.Viewport` 管理 auto-scroll；`Messages` children render
  function 是当前推荐的消息迭代方式。
- [Thread Virtualization](https://www.assistant-ui.com/docs/guides/virtualization)：
  默认不需要虚拟化；只有 React mount/update 本身成为数百到数千条消息或重型内容的瓶颈时
  才使用。虚拟化时使用 `unstable_useThreadMessageIds` 和
  `ThreadPrimitive.Unstable_MessageById`，并由自定义 scroll owner 管理滚动。
- [Message primitives](https://www.assistant-ui.com/docs/primitives/message)：
  `GroupedParts` 适用于相邻 part 分组，`groupPartByType` 自带稳定 memo fingerprint；
  非相邻特殊分组才使用 unstable API。
- [Markdown text](https://www.assistant-ui.com/elements/markdown-text)：
  MarkdownText 读取当前 message part context；streaming/defer 是渲染策略，不是 state
  持久化策略。

由此得到三个边界：

1. 不把 assistant-ui 类型下沉到 backend、storage、workflow 或 context。
2. 不为了缓存把 canonical state 转为前端私有事实；缓存只存渲染对象。
3. 不在普通列表阶段提前接管 assistant-ui viewport；只有真正虚拟化时才切换到
   `ViewportProvider` + 自定义 scroll owner。

## 5. P0：基线与诊断

### 5.1 固定测试数据

使用同一套可复现 fixture，至少包含：

1. 20 条普通 user/assistant 消息；
2. 200 条消息，含多段 Markdown；
3. 1000 条消息，含 reasoning、tool trace、diff、terminal 和失败工具状态。

每组测试首屏、历史滚动、发送新消息、纯 text streaming、reasoning streaming、tool 生命周期、
cancel、attach/reconnect、edit 和 fork。模型输出使用固定本地 stub，不用一次人工体感作为
唯一结论。

### 5.2 前端只记录摘要指标

在开发诊断开关下增加计时或 profiler hook，沿用项目的 `frontendLog`，只记录：

- task/run/trace 标识；
- message、part、字符总数；
- Transport state update 次数；
- converter 调用次数、耗时、缓存命中/失效数量；
- React commit 次数、最长 commit；
- Markdown parse 次数/耗时；
- 已挂载消息数、可见消息数；
- scroll event、是否处于底部跟随、Task 切换后的缓存条目数。

禁止记录 prompt、token 内容、tool args、文件内容、完整模型响应或凭据。正式日志默认
采样，性能 fixture 可通过内存计数器和 React Profiler 获取，不把高频计时写成生产噪声。

### 5.3 backend 只观察，不合帧

复用现有结构化日志，补充或核对以下摘要：

- model delta 到 event projector 的耗时；
- snapshot apply、SQLite commit、commit-to-publish 延迟；
- subscriber queue 入队/出队数量与深度；
- `controller.flush()` 次数、每次 mutation 数和消息数量；
- first snapshot、first text、terminal snapshot 时间。

这些指标用于判断瓶颈是否在 backend durable path。P0 不修改 queue、flush 频率或 mutation
顺序；尤其不引入 subscriber-local coalescer。

## 6. P1：稳定 converter 与结构共享

### 6.1 先修 converter 身份

将 `useRuntimeTransport` 中的 inline converter 改为模块级稳定引用，或用无动态依赖的
`useCallback`，首选直接传入稳定的命名函数：

```ts
converter: toTransportThreadView
```

该改动先单独验收，确认未发生 state 更新时不再因 converter 函数身份变化而重复转换。
它不改变 `TransportState`、request body、resume、cancel 或错误处理。

### 6.2 用官方 helper 做 message-level 缓存

在 `apps/desktop/lib/assistant/converter.ts` 内增加 task/runtime 生命周期内的 converter
实例，优先复用官方 `unstable_createMessageConverter`。实例通过 `useMemo` 在
`AssistantRuntimeSession` 或 `useTaskAssistantTransportRuntime` 的 task 生命周期内创建，
不得把带有当前调用上下文的可变闭包做成 module-global singleton。

当前 message 没有独立的 `runId` 字段，而 message status 和 fork metadata 依赖父 Run。因此
不能简单把 raw `TransportMessage` 直接作为唯一 cache key。建议使用 adapter-owned 的
稳定输入对象：

```ts
type TransportMessageInput = {
  source: TransportMessage;
  runStatus: string;
  endReason: string | null;
  isLastRunMessage: boolean;
};
```

规则：

- 同一 `source` 且 `runStatus`、`endReason`、`isLastRunMessage` 未改变时复用 input；
- `append-text` 使当前 message/source 改变时，只重建该 message 的转换结果；
- 当前 Run 的文本继续增长时，不能因为父 Run 对象整体被复制而让该 Run 的所有历史
  message input 都失效；cache signature 只包含真正影响 message 映射的字段；
- tool part 的 `status`、`args`、`display_data`、presentation 或 error 改变时，由改变后的
  message/source 触发该 message 失效；
- Run 状态、终态原因或 fork 标记改变时，只失效受影响的 message；
- 删除、重排、编辑、root set、重连 hydrate 或结构校验失败时，以 message id/sequence
  重建 adapter cache，不复用无法证明正确的条目。

官方 helper 的 `toOriginalMessage` 若返回 adapter-owned input，应在本项目文档中说明其为
UI 适配对象，不把它当 backend domain message；若当前 UI 需要取原始 Transport message，
在 adapter 边界显式保留 `source` 映射并写测试。

### 6.3 pending command 与 snapshot error

pending user command 不在 canonical snapshot 中，继续使用当前 command object 的稳定幂等
ID。为 pending message 增加 runtime-local `WeakMap<object, ThreadMessage>`，使 pending
command 队列变化不导致历史 message 失效。

snapshot error 仍是受控的 assistant-ui error message；按 error object/稳定错误字段缓存，
不得把原始异常、provider 响应或堆栈写入 UI。error 消失、替换或 root snapshot 重置时清理
对应条目。

### 6.4 converter 输出约束

`toTransportThreadView` 继续只做中性 Transport state 到 assistant-ui state 的适配：

- `state` 原样作为 runtime state 返回，不复制、不写回 backend；
- `MessageStatus`、tool artifact 和 UI-only sentinel 只在 desktop converter 产生；
- 不从 `args`、`result` 或字段存在性猜测工具生命周期；
- 不在 converter 中做历史裁剪、context compaction、数据库读取或事实修复；
- 未知 Run/tool 状态保持显式非成功，不降级为 completed；
- `createdAt` 仍不是当前 Transport canonical 字段，本性能改造不擅自引入持久化事实。

### 6.5 part 与 GroupedParts

当前 assistant message 使用 `MessagePrimitive.GroupedParts`，`assistantMessageGroupBy` 是
模块级稳定函数，并依据 tool artifact 的 `presentation.surface` 把 standalone tool 排除在
trace group 外。保留这个业务行为；不要为了追求 memo fingerprint 强行改成错误的分组。

如果以后分组规则可以完全按 part type 表达，再迁移到官方 `groupPartByType`。在此之前，
只确保 `groupBy`、renderer map、Markdown 配置和工具 renderer 引用稳定；不要在每个 render
创建新的 components/config 对象。

### 6.6 P1 验收

- 同一 fixture 下，未改变历史 message 的 converter cache 命中率接近 100%；
- 单个 text/reasoning append 不重建其他历史 message 的 `ThreadMessage`；
- Run 终态变化只更新受影响 Run 的 message status/fork metadata；
- tool lifecycle 更新只更新对应 message/part；
- pending command、snapshot error、edit、cancel、attach、retry、fork 行为保持正确；
- converter 不依赖 module-global task 状态，切换 task 后旧条目可回收。

## 7. P2：普通 Thread 的 paint 与滚动治理

### 7.1 先补 containment

在当前自定义 message root 的样式边界增加经过浏览器验证的：

```css
content-visibility: auto;
contain-intrinsic-size: auto <fixture-derived-size>;
```

具体 selector 和 intrinsic size 以实际消息布局测试确定，不能把固定高度当成消息事实，也
不能遮挡工具展开内容。该优化只减少屏幕外 paint，不宣称减少 React 更新。

### 7.2 保留官方 viewport，单独验证 smooth scroll

普通列表继续使用 `ThreadPrimitive.Viewport` 和 `ThreadPrimitive.ViewportFooter`，因为当前
消息列表未虚拟化，官方 viewport 的 auto-scroll 语义适用。对当前 `scroll-smooth` 做 A/B：

- 用户在底部 streaming 时应自然跟随；
- 用户向上滚动后不得强行拉回；
- cancel、terminal、attach 和 Thread 切换不能出现滚动跳跃；
- 如果 smooth behavior 放大高度变化造成的追赶，移除 class，使用默认 instant/auto 行为；
- 是否移除以浏览器 trace 和回归用例决定，不凭直觉写入协议或 backend。

本阶段不自定义 `scrollTop`，不引入 ResizeObserver scroll owner，也不把 viewport 自动滚动
改成业务状态机。

### 7.3 Markdown 与重型 tool UI

继续使用现有 `StreamdownTextPrimitive` 的 streaming/defer；不重复实现 Markdown parser、
不把 Markdown AST 放入 snapshot。对 diff、terminal、details 等重型 tool UI：

- 流式期间只渲染必要的状态摘要；
- 需要最终参数/结果才有意义的昂贵 UI，按官方 Tool UI deferred rendering 模式在完成后
  才挂载；
- 展开、复制、错误提示和 tool status 继续从 `ToolObservation.display_data` / artifact
  契约读取，不从结果字符串反推。

### 7.4 P2 验收

验证 200/1000 条消息时：

- converter 成本已由 P1 隔离，屏幕外消息 paint 成本下降；
- streaming 底部跟随和用户上滑锁定行为稳定；
- Markdown、reasoning、tool group、diff、terminal 不因 containment 错位或丢失；
- Task 切换、retry、attach、cancel、edit、fork 无新增滚动回归。

## 8. P3：按基线决定的虚拟化

### 8.1 启动条件

只有满足以下条件才启动 P3：

- P1/P2 完成且测试通过；
- React mount/update 或已挂载 message 数仍是主要耗时；
- 典型 Task 达到数百至数千消息，或单条重型 UI 导致 typing latency 明显下降；
- 通过 profile 能证明虚拟化收益大于滚动、测量、selection 和交互复杂度。

不把“消息很多”本身当成阈值；阈值来自固定 fixture 和真实 trace。

### 8.2 官方组合

虚拟化版本新增 desktop-only 的 message list，使用：

- `unstable_useThreadMessageIds()`：利用 content-only update 时稳定的 id 数组；
- `ThreadPrimitive.Unstable_MessageById`：以 message id 而不是 index 作为 row identity；
- `@tanstack/react-virtual`：仅在 P3 决策后加入依赖；
- 普通文档流 spacer + `paddingTop`/`paddingBottom` + `measureElement`，不使用绝对定位
  覆盖当前 message CSS；
- stable `MESSAGE_COMPONENTS`/renderer 引用。

assistant-ui 官方将上述 id API 标为 experimental，必须封装在一个 desktop 组件内，不能把
experimental 类型扩散到 backend、domain 或 storage。未知/已删除 id 应允许 renderer 返回
`null`。

### 8.3 scroll owner 变化

虚拟化不能直接叠加当前 `ThreadPrimitive.Viewport` 的默认 auto-scroll。按官方建议改为
`ThreadPrimitive.ViewportProvider` 加自有 scroll element，并实现最小的：

1. sticky-follow：用户在底部时跟随内容高度变化；
2. disarm：用户上滑后停止追底，返回底部后重新 armed；
3. measurement guard：virtualizer re-measure 不与追底逻辑争写 scrollTop；
4. run-start jump：新 Run 开始时避免新消息在首帧落到 fold 外。

这些逻辑只存在于 desktop Thread 组件。先实现单消息 id row；只有 row 数量和高度测量证明
必要时，再按 user turn 组合 row。若按 turn 组合，必须以稳定的 `{id, role}` 结构建组，
不能把数组 index 当事实身份。

### 8.4 P3 验收

- 1000 条消息时 mounted rows 接近 viewport + overscan，而不是完整历史；
- 顶部、中部、底部滚动不出现错位、重复或丢失；
- streaming 时底部跟随、上滑锁定、重新回底均稳定；
- re-measure 不 rubber-band；
- tool 展开/折叠、复制、terminal/diff 交互和 action bar 仍可用；
- attach、cancel、retry、edit、fork 不依赖 index 稳定性。

## 9. P4：未来的历史窗口（本轮不实施）

虚拟化只减少 DOM/React mount，不减少完整 Transport state 的传输、解析、converter 的轻量
扫描或 SQLite JSON 读写。只有 profile 证明这些才是主要瓶颈时，才另立 history-window 方案。

未来目标应保持：

```text
backend canonical snapshot/context
  └─ 完整、可恢复、按现有 owner 写入

assistant-ui transport projection
  └─ 明确的当前窗口 + history cursor
```

不能在前端直接 `slice()` 后把旧消息当作不存在。单独方案必须决定：

- history cursor 与 task/run/thread 的绑定、失效和重连；
- 前插历史使用专用 API、custom command 还是新 state schema；
- 当前 active Run 是否允许加载历史；
- 编辑、删除、fork、root set 如何使 cursor 失效；
- SQLite 是否仍用单 JSON snapshot，还是引入可分页的本地事实表；
- UI history window 与 `RuntimeContextManager` 的 context/compaction 如何保持边界。

P4 不应由 converter、assistant-ui runtime 或 Transport subscriber 临时决定。

## 10. 明确非目标：Transport subscriber-local 合帧

本轮明确不做以下改动：

- 不新增 `transport_frame_coalescer.py` 或等价 subscriber-local 合帧器；
- 不在 `ConversationTaskSnapshotService` 延迟 SQLite commit 或 publish；
- 不合并、丢弃或重排 `append-text`、结构、tool、error、cancel、terminal mutation；
- 不修改 `Subscriber.queue` 的生命周期和每连接独立性；
- 不用完整 root snapshot 替代正常增量路径；
- 不因为 SSE/HTTP 断开而取消或重放业务 Run。

P0 可以记录 frame/flush 数量和 queue 延迟，但这些指标只用于后续独立决策。若未来证明
Transport 调度是瓶颈，应另写方案，重新定义终态 flush、断线、slow subscriber、背压和
assistant-stream 互操作测试，不能把它混入 P1/P2。

## 11. 文件边界与实施顺序

### 11.1 本轮预计涉及的前端文件

```text
apps/desktop/lib/assistant/converter.ts
apps/desktop/components/assistant/runtime/use-runtime-transport.ts
apps/desktop/components/assistant/runtime/assistant-runtime-session.tsx
apps/desktop/components/assistant-ui/elements/thread.aui.tsx
apps/desktop/components/markdown-text.tsx       # 仅必要的诊断/样式边界
apps/desktop/tests/unit/converter.test.ts
```

P1 的 cache 只放在 `lib/assistant` 的 assistant-ui 适配边界；不放入 domain、storage 或
backend。P3 的 virtual list 和 scroll owner 只放在 `components/assistant-ui`。

### 11.2 本轮 backend 边界

P0 只允许增加遵循既有结构化日志规范的摘要指标，主要观察：

```text
apps/backend/app/assistant_transport/service/transport_stream_service.py
apps/backend/app/assistant_transport/service/conversation_task_snapshot_service.py
```

除非基线发现现有日志缺失，否则不改 projector、snapshot owner、Subscriber 或 workflow。

### 11.3 实施顺序

1. 先实现 P0 fixture 和诊断，保存 20/200/1000 的结果。
2. 先改 inline converter 为稳定引用，单独运行现有 unit/E2E。
3. 实现 P1 message-level cache，补充引用稳定性和状态失效测试。
4. 补 P2 containment，并对 `scroll-smooth` 做 A/B；只保留有证据的选择。
5. 重新 profile。若 React mount/update 仍为瓶颈，才评审并实现 P3。
6. 若 state 传输/解析/SQLite 才是瓶颈，停止扩展本方案，另开 P4 设计。

## 12. 测试与验收矩阵

### 12.1 frontend unit

- stable converter function 不因 runtime render 改变身份；
- 未改变 message/source 的 `ThreadMessage` 引用保持稳定；
- text/reasoning append 只使目标 message/part 变化；
- Run status/endReason/isLast metadata 只使受影响 message 变化；
- tool pending/running/completed/failed/cancelled 映射和 artifact 不变；
- pending command、snapshot error 的身份、清理和重连行为正确；
- root set、编辑、删除、重排、未知状态安全失效；
- task 切换不共享带上下文的 cache。

### 12.2 backend integration

- snapshot commit 失败不会 publish 对应 change；
- Subscriber 和 controller state 按连接隔离；
- 首帧 root set 与后续 mutation 不重复应用；
- append-text 的顺序、终态和取消行为保持不变；
- attach 只订阅，不触发业务 resume；
- backend 重启后 state read/终态对账符合现有规则；
- 本轮不新增或修改 subscriber-local 合帧测试，因为本轮不实现该能力。

### 12.3 浏览器性能验收

用 Playwright、Chrome Performance 和 React Profiler 固定采集：

- 首屏完成时间和首次可交互时间；
- 每秒 state update、converter 调用/命中率和总耗时；
- React commit 次数、最长 commit；
- Markdown/tool/diff/terminal mount/update 次数；
- 已挂载/可见消息数；
- streaming 底部跟随、用户上滑和回到底部的 scrollTop 行为；
- Task 切换后的内存和 cache 回收迹象。

验收记录必须同时保存 fixture、依赖版本、浏览器版本和测试配置，避免把一次环境差异
误判为架构收益。

## 13. 风险与决策门槛

### 13.1 assistant-ui unstable API

`unstable_createMessageConverter`、`unstable_useThreadMessageIds` 和
`Unstable_MessageById` 都可能随版本变化。P1 首选前者但必须封装和锁版本；P3 默认延后，
只有 profile 证明收益时才接受额外兼容成本。

### 13.2 cache 键错误导致旧状态

只按 message id 缓存会漏掉 Run status、endReason 和 fork metadata 的变化；只按整个父
Run 对象缓存又会在每个 text append 时失效整个 Run。必须使用 raw source identity 加“真正
影响 UI 的字段签名”，并用状态迁移测试覆盖。

### 13.3 containment 不是虚拟化

`content-visibility` 主要减少 paint；若 profile 显示 React 仍在遍历/更新全部 message，
继续调 CSS 不会解决问题，应进入 P3 评审。

### 13.4 full state 与历史窗口

P1/P2/P3 都不能偷偷改变 full snapshot 的事实和恢复语义。P4 需要新的协议/存储决策，
不能在性能修复中用前端截断代替。

## 14. 当前可直接执行的决策

本方案不等待额外架构确认即可进入：

1. P0 基线与摘要诊断；
2. 稳定 converter 引用修复；
3. 基于官方 `unstable_createMessageConverter` 的 P1 适配设计与单测；
4. 当前 Thread 的 containment 与 `scroll-smooth` A/B。

以下决策必须等待 profile：

1. 是否引入 `@tanstack/react-virtual`；
2. 是否采用 id-based virtual rows 还是按 turn 分组；
3. 是否启动历史窗口和本地存储形态调整。

Transport subscriber-local 合帧在本轮决策中为“不做”，不作为待确认项。

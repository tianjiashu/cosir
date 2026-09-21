# 长对话流式渲染性能改造方案

状态：实施中；第一至第三阶段核心代码已落地，待最终子 Agent 验收与长对话 E2E 固化

验收状态：已完成前置只读审查；其发现的 frame wire contract、bounded queue resync 和 top-anchor 集成风险已纳入本方案。当前按绿地项目处理：新契约直接替换旧实现，不保留代码或运行兼容层。最终验收仍需检查代码事实、长期迭代铁律和测试结果。

本文只覆盖三项改造：

1. 减少后端每个流式 mutation 的成本；
2. 让前端 Transport projection 真正增量化；
3. 在确有必要时引入 Assistant UI 消息虚拟化。

不改变 Run、context、Transport snapshot、LangGraph checkpoint 和文件变更记录的事实所有权，也不把本机桌面应用改造成公网 SaaS。

## 1. 运行边界与约束

本项目是单用户本机桌面 Agent：

```text
Tauri Rust 主进程
├─ 拥有 FastAPI 子进程生命周期
├─ 等待 bootstate / health
└─ 退出时清理后端进程树

WebView2 / React
├─ Assistant UI runtime
├─ 本机 HTTP/SSE 客户端
└─ 只负责交互状态和渲染

Python / FastAPI 子进程
├─ Agent workflow / LangGraph
├─ Transport snapshot 与 SSE
├─ SQLite canonical records
└─ 按需创建的工具执行子进程
```

运行数据仍位于 Tauri `app_data_dir()/.cosir/`：业务 SQLite、日志、checkpoint、runtime config 和 bootstate 分离保存。React 不直接访问 SQLite、Agent Runtime 或工作区文件。

本方案因此不引入 Redis、Postgres、云端队列、认证、多租户、Kubernetes 或公网安全层。HTTP/SSE 只是本机进程边界。

### 1.1 事实所有权不能被性能缓存替代

- `ConversationRunModel.status` 仍是 Run 生命周期唯一事实源。
- `RuntimeContextManager` 仍是 Agent context 的运行时协调入口；本方案不把 Transport projection 写回 context。
- `ConversationTaskStateService` 仍拥有进程内 snapshot working copy，但它不是数据库事实源。
- `ConversationEventProjector` 仍只把 workflow conversation event 投影为 Transport mutation。
- Assistant UI 只消费后端 snapshot / mutation 的投影视图，不能反向成为业务事实源。
- projection cache、virtualizer rows、subscriber queue 都是可丢弃的进程内协调状态，后端重启后必须从 canonical records 重建。
- Tauri 已有的宿主进程 lifecycle generation 仍只属于进程控制面；本方案不修改它，也不把它暴露到 Assistant Transport、FrameStore 或 SSE 字段。

## 2. 当前代码事实

以下事实以当前仓库代码和已安装的 `@assistant-ui/react@0.15.17` 为准。

### 2.1 后端 frame 已完成增量化

`StreamingPartStateMachine` 已把同一 text/reasoning 通道的增量按字符数或时间合并；默认阈值是 32 字符或 50ms。模块 docstring 明确说明每个下游事件都会触发 snapshot projection、SSE flush 和前端渲染，因此合并事件数可以同比例降低这些成本。

参见：

- `apps/backend/app/core/workflows/react/nodes/helper/streaming_part_state_machine.py`
- `apps/backend/app/config/constant.py` 中 `DEFAULT_TEXT_FLUSH_MIN_CHARS` 与 `DEFAULT_TEXT_FLUSH_MAX_INTERVAL_SECONDS`

当前 `ConversationTaskStateService.apply_planned()` 在 `_lock` 下直接修改 task working copy：纯 `append-text` 不再执行完整 snapshot 校验，也不复制完整 state；结构性 mutation 仍执行完整校验。普通订阅只收到 mutation frame，full frame 只发生在原子 attach、recovery、显式 full publish 或终态边界。

`Subscriber` 使用有界 mutation lane、control lane 和相邻 append 合并；overflow 发送 `resync_required` 并结束当前连接。参见：[conversation_task_state_service.py](../apps/backend/app/assistant_transport/service/conversation_task_state_service.py)、[subscriber.py](../apps/backend/app/assistant_transport/stream/subscriber.py)

### 2.2 前端已切换到单一增量 FrameStore

`TransportFrameStore` 是唯一 projection owner，直接消费 full/mutation/resync frame：full 重建基线，mutation 只复制受影响路径和对应 Run 的 message entries；每次提交创建新的浅消息数组，但未变化的消息对象保持稳定。旧的 `transport-view-converter.ts` 和 `toTransportThreadView()` 已删除，不保留双轨 projection。

参见：[transport-frame-store.ts](../apps/desktop/lib/assistant/transport-frame-store.ts)、[use-task-assistant-transport-runtime.ts](../apps/desktop/lib/assistant/use-task-assistant-transport-runtime.ts)

### 2.3 Thread 已接入独立虚拟化 viewport

当前普通 Thread 与 Workbench readonly Thread 均：

- `turnAnchor="top"`；
- `autoScroll={false}`；
- 消息根使用 `[content-visibility:auto]`；
- 用户消息使用 `contain-intrinsic-size:auto_6rem`；
- assistant 消息使用 `contain-intrinsic-size:auto_24rem`；
- assistant parts 使用 `MessagePrimitive.GroupedParts`。
- 通过 `unstable_useThreadMessageIds`、`ThreadPrimitive.Unstable_MessageById` 和 `@tanstack/react-virtual` 只挂载可见 rows；消息 id 作为稳定身份，virtualizer 只存在于 UI 适配层。

参见：[thread.aui.tsx](../apps/desktop/components/assistant-ui/elements/thread.aui.tsx)

当前 Markdown 边界使用 `StreamdownTextPrimitive` 的 `mode="streaming"` 和 `defer`，不能用自研 Markdown parser 替换它。参见：[markdown-text.tsx](../apps/desktop/components/markdown-text.tsx)

## 3. 官方 Assistant UI 约束

本方案依据以下官方文档：

- [Thread Virtualization](https://assistant-ui.com/docs/guides/virtualization)
- [State](https://assistant-ui.com/docs/store/state)
- [State Hooks](https://assistant-ui.com/docs/api-reference/hooks/state)
- [Rendering Lists](https://assistant-ui.com/docs/store/rendering-lists)
- [External Store Runtime](https://assistant-ui.com/docs/runtimes/custom/external-store)
- [Markdown text](https://assistant-ui.com/elements/markdown-text)
- [Tool UI](https://assistant-ui.com/docs/tools/tool-ui)

必须遵守的原则：

1. 默认先使用 `content-visibility: auto`；虚拟化只在 React mount/update 成为瓶颈时启用。
2. `useAuiState` selector 返回窄且稳定的值；不能返回每次新建的 object/array。
3. Assistant UI 的列表 primitive 已经使用 lazy accessor 和 propless memoization，不能再自研一套消息订阅系统。
4. 虚拟化优先用稳定 message id 和 `ThreadPrimitive.Unstable_MessageById`，而不是把 index 当长期身份；该 API 调用必须同时提供稳定的 `components` map。
5. 官方虚拟化组合需要自行拥有 scroll element；内置 viewport 的 auto-follow 与 virtualizer 测量可能相互竞争。
6. `unstable_useThreadMessageIds` 与 `ThreadPrimitive.Unstable_MessageById` 是 experimental API，必须封装在单一 UI 适配模块中，不能让它们扩散到 domain、storage 或 Transport wire 层。
7. Markdown streaming 应继续使用官方 deferred parsing；昂贵 tool UI 可等到完成或展开后再挂载。

当前 `@assistant-ui/react@0.15.17` 的 transport runtime 只把 decoder 累积后的完整 `unstable_state` 传给 converter；公开 state operation 仍是 `set` 和 `append-text`。这只是当前实现事实，不是新架构约束。绿地方案直接定义项目自己的 Assistant Transport frame wire contract：`kind` 和 `resync_required` 由本机 SSE 明确传输；前端在 `apps/desktop/lib/assistant/transport/` 维护 frame decoder、projection store 和 Assistant UI runtime bridge，最终仍只向 Assistant UI 提供它需要的 state/runtime。不能把 Python 类型泄漏到 frontend，也不能继续用“每帧先拼完整 state、再让 converter 猜变化”的接口。

## 4. 目标数据流

目标不是把全量 snapshot 从系统中删除，而是把“事实快照”和“流式传输 frame”明确区分：

```text
workflow event
    │
    ▼
ConversationEventProjector
    │  产生有序 mutation
    ▼
ConversationTaskStateService
    │  修改唯一的 task-local working copy
    ├──────────────► canonical full snapshot（首帧 / attach / recovery / terminal）
    └──────────────► mutation batch（正常 streaming）
                              │
                              ▼
                    AssistantTransportStreamService
                              │  有序、可合并、可恢复的 frame
                              ▼
                      FrameDecoder / FrameStore
                              │
                              ▼
                   Assistant UI runtime bridge
                              │
                              ▼
                 Thread / virtualized Thread
```

完整 snapshot 仍然是重连和恢复的权威边界；mutation batch 只是对当前 snapshot 的有序增量。

## 5. 第一阶段：后端 frame 与 snapshot mutation 优化

### 5.1 设计目标

对于正常的 text/reasoning 增量，目标复杂度从：

```text
当前：O(完整 task snapshot) + O(完整 snapshot 深拷贝) + O(完整 snapshot 校验)
目标：O(mutation path 深度 + 当前文本长度) + O(队列/编码成本)
```

当前 `_apply_mutation()` 的 `current + mutation.value` 会复制已有 Python 字符串，因此只移除 snapshot 深拷贝不能声称单个长文本 append 是 O(delta)。本方案第一阶段不新增文本缓冲基础设施；先通过移除正常 mutation 的全量深拷贝、降低不必要的全量校验、相邻 mutation 合并和减少 full frame 物化来降低每帧成本。对长文本 append，目标复杂度诚实地保留为与当前文本长度相关；只有 profiling 明确证明它仍是主成本时，才另立专门设计评估。

不以牺牲事件顺序、重连正确性、Run 状态唯一事实源或失败恢复为代价。

### 5.2 新 frame 与订阅顺序

`SnapshotChange` 当前是 backend 实现中的三字段 dataclass：`task_id`、完整 `state`、`mutations`。它只是迁改前的代码事实，不是需要保留的公共契约。绿地改造直接删除它，统一由 backend stream、SSE encoder、前端 decoder 和测试使用同一套新 frame 语义；不增加 positional adapter、旧类型别名或双轨发送路径。

新的 frame 同时是 backend stream 的领域边界和 Assistant Transport 的 wire 输入，至少包含：

```python
class TransportFrame:
    task_id: int
    kind: Literal["full", "mutation", "resync_required"]
    mutations: tuple[ConversationStateMutation, ...]
    state: ConversationStateSnapshot | None
    source_run_id: int | None
    current_run_id: int | None
    current_run_status: str | None
    resync_reason: str | None
```

`TransportFrame` 是 task-wide frame，不携带某个 SSE 订阅目标 Run 的终态。连接级 SSE envelope 另外携带 `target_run_id` 与 `target_run_status`；这两个字段由 `AssistantTransportStreamService` 从 canonical Run status 填充，只服务当前连接的关闭判断，不进入 task snapshot、projection message 身份或数据库事实。

语义固定为：

- `kind="full"`：`state` 非空，用于首帧、attach、recovery、终态补偿和显式重同步。
- `kind="mutation"`：正常流式期间只携带 mutations，不携带完整 state。
- `kind="resync_required"`：当前订阅者已经无法证明连续消费 mutation；它是可恢复的传输控制 frame，不是 Run 取消或失败。
- `source_run_id`：该 mutation batch 的来源 Run；full frame 为产生当前边界的 Run（没有则为空）。`current_run_id/current_run_status` 是 task snapshot 边界观察到的 task 当前 Run，不代表指定 subscriber 只会收到该 Run 的事件；它们不能替代 canonical Run status。

同一 subscriber 内，frame 由单一 producer 按入队顺序串行消费；coalescer 只合并尚未发送的相邻 text mutation，不跨 lifecycle barrier。attach 请求的目标 Run 只属于 subscriber/stream connection，stream service 在连接级 envelope 中维护并发送 `target_run_status`，据此决定终态关闭；不能把 `current_run_status` 当作目标 Run 状态。新连接永远先收到一个 full frame，旧连接关闭后其队列不再向新连接转发；backend 重启由现有 Tauri/FastAPI 生命周期关闭旧连接并重新 attach，不需要在 wire 或 projection 中增加额外排序字段。重复事件或空 mutation 返回内部 no-op（对 subscriber 不发布 mutation frame），full snapshot 仍可作为恢复基线。

SSE JSON envelope 只暴露上述稳定字段及其 JSON 化的 state/mutations；不暴露 Python dataclass。前端 decoder 只验证 `task_id`、`kind`、mutation schema 和连接状态；收到 `resync_required` 或 SSE EOF 时停止应用当前连接的后续数据，重新发起原子 attach/recovery 并以其首个 full frame 重建。Assistant UI 不负责解释 frame metadata；FrameStore 是唯一负责连接顺序、重同步和 projection 增量应用的模块。

### 5.3 `ConversationTaskStateService` 的职责调整

保留它作为唯一 task-local snapshot working copy owner，但调整热路径：

1. `apply_planned()` 在 working copy 上应用 mutation。
2. 对 mutation 做路径、类型、状态边界校验；事件本身仍由 Pydantic discriminated contract 校验。
3. 正常 mutation frame 不执行完整 `copy.deepcopy(state)`。
4. 仅在 full frame 边界生成隔离副本。
5. 将 `source_run_id`、`current_run_id`、`current_run_status`、是否 terminal 等轻量元数据写入 frame。

不能把可变 working copy 的引用直接暴露给 subscriber。优先采用“full frame 深拷贝、mutation frame 不携带 state”的方案；只有 benchmark 证明 full frame 也成为瓶颈时，才评估受控 copy-on-write/persistent representation。禁止在 Python dict 上简单共享引用并依赖调用方自律。

### 5.4 完整校验的分层策略

当前实现已经按 mutation 形状分层：

- 纯 `append-text`：由 mutation 的路径存在性、目标字符串类型和非空 delta 校验保护，不重复遍历完整 snapshot；
- `set`、根替换、part/tool/run 状态变化：继续执行 `validate_snapshot()`，保留结构和生命周期不变量；
- full frame、attach、recovery：执行完整 snapshot 校验并只在边界生成隔离副本；
- 未来若引入更细的局部状态迁移校验，必须先有 profiling 和对应回归测试，不能用猜测削弱校验强度。

每次修改 frame 或校验职责时，必须在同一个 PR 同步更新 docstring、类型注释、结构化日志和测试，不延迟到后续清理 PR。

### 5.5 task-wide subscriber 的安全合并和背压

当前 subscriber 是 task-wide：一个 task 下的 subscriber 会收到该 task 的所有 snapshot change，而不是只收到指定 run 的事件；不能假设 queue 中只有一个 run。

coalescer 必须只合并相邻且满足以下全部条件的 frame：

- 同一个 subscriber、同一条连接；
- frame 全部是 `append-text` mutation；
- 所有 mutation 指向同一个 run/message/part/text path；
- 中间没有 `current_run_id`、run status、part closed、tool lifecycle、usage、full frame 或其他 task-level mutation barrier。

合并只发生在 frame 尚未发送前；客户端只需按当前 SSE 连接顺序应用 frame，不维护额外的排序字段。

当前 subscriber 使用无界 `asyncio.Queue`。不要简单替换为 bounded queue 后继续在 `call_soon_threadsafe(...put_nowait...)` 中直接写入，因为 `QueueFull` 可能让控制 frame 也无法入队。采用“两条通道 + 显式 subscriber 状态机”：mutation lane 有界且允许合并，control lane 固定保留一个高优先级槽位，用于 `resync_required` 或终态 full frame。

```text
NORMAL
  ├─ 可合并增量：合并到 pending batch
  ├─ 普通 barrier：先 flush pending，再入队 barrier
  └─ mutation lane 满：清空可丢弃增量，写入 control lane，进入 RESYNC_REQUIRED

RESYNC_REQUIRED
  ├─ 不再接收普通增量
  ├─ backend working copy 继续正常推进
  ├─ 若 terminal full 已产生，优先写入 terminal full
  ├─ 否则写入一个不可丢失的 resync_required control frame
  ├─ 发送 control frame 后结束当前 SSE
  └─ 客户端重新发起原子 attach，首帧为 full
```

`resync_latch` 必须是 subscriber 自身的线程安全状态，不依赖再次 `put_nowait()` 成功。多个 overflow 幂等。terminal 优先级严格限定在 control frame 尚未发送之前；一旦 `resync_required` 已发送，后续 terminal 不再尝试写入旧 SSE，由 recovery 读取 canonical terminal snapshot。这样既不静默丢字，也不会把“取消请求已接受”误报为 Run 已终止。

terminal full 必须进入 control lane，不能进入可丢弃的 mutation lane；连接级 `target_run_status` 与 terminal full 必须由同一个 control item 发出，避免客户端先看到终态再丢失最后一帧。

单用户本地应用也需要该背压，因为后台 Agent 可能继续运行，而 WebView 可能被系统挂起或主线程阻塞。resync 是可恢复的订阅失败，不是业务 Run 取消。

### 5.6 后端实现顺序

1. 直接以 `TransportFrame` 替换 `SnapshotChange`，同时更新 state service、stream service、SSE encoder、测试和 docstring。
2. 实现明确的 full/mutation/resync wire envelope，以及前端 decoder 的 schema/连接状态校验；禁止先构造完整 state 再猜 delta。
3. 在 stream service 中实现 control lane、mutation lane、coalescer 和 resync 状态机；验证 terminal full frame 的优先级。
4. 移除正常 mutation path 的全量深拷贝，并保留 full frame 只用于基线、attach、recovery 和 terminal。
5. 对 append-only 热路径跳过重复的全量 snapshot 遍历；结构性 mutation 和 full/recovery 边界继续全量校验，后续再以 profiling 决定是否细化局部校验。

这是一组一次性替换的绿地契约。每个阶段仍有独立测试边界，但不保留旧 wire、旧 frame 或旧 fallback 实现。

### 5.7 新 recovery 的唯一 owner

新协议不再把恢复交给旧 `assistant-stream` decoder 的隐式累积行为。`TransportFrameStore` 是前端恢复 owner：收到 `resync_required` 或 SSE EOF 后，先停止应用当前连接的后续 frame，调用一次原子 attach/recovery API；该 API 使用当前 `ConversationTaskStateService._lock` 这一实际串行化边界，与 `apply_planned()` 和 publish 共用同一把锁，先注册 subscriber、生成并返回 full frame，然后才允许后续 mutation 进入该 subscriber；不能拆成“先 GET snapshot、再建立 attach”，否则中间 mutation 无法由连接顺序补回。旧连接先通过 AbortController、连接对象身份和 unsubscribe 关闭；该流程不发送 command、不创建新 Run、不触发 business resume；业务 resume 由现有 `useBusinessResume` 完成后再调用 attach 控制引用。

后端 `assistant_api.py`/response serializer 必须把 snapshot read、attach 和 stream response 统一编码为新 frame envelope；attach/recovery 的 full frame 与 subscriber 注册必须由同一个 use-case 完成；恢复完成后由 FrameStore 原子地替换 连接状态、normalized state 和 projection indexes，再通知 Assistant UI runtime。恢复失败只进入可观测的 error state，不清除 canonical state，也不把订阅失败改写成 Run failure。

## 6. 第二阶段：前端 Transport projection 增量化

### 6.1 设计目标

前端必须继续向 assistant-ui 提供消息数组，但正常 streaming 更新不能每帧重新构造整条历史的 projection。

直接用 task-scoped 的 `TransportFrameStore` 承担原 `createTransportViewConverter()` 的全量扫描职责，放在 `apps/desktop/lib/assistant/transport-frame-store.ts`，不把 Assistant UI 类型泄漏到 backend/domain/storage。它消费新的 `TransportFrame` wire envelope，不再让 converter 从完整 state 猜出变化。

projection 的输入是明确的 frame：

```ts
applyFrame(frame: TransportFrame): ProjectionCommit
```

`TransportFrameStore` 维护 task-local state、稳定的 per-run message entries 和 pending overlay；`mutation` 直接按 mutation path 更新受影响的 run/message；`full` 重建基线；`resync_required` 进入 recovery 状态并阻止旧 frame 继续写入。消息数组仍按 external store 契约在每次提交时创建新的浅数组，但每帧只重新构造受影响 Run 的 message entry；不会为未触达消息重新执行 converter。

Assistant UI 接入统一改为官方 `useExternalStoreRuntime`：FrameStore 暴露稳定的 `messages`、`isRunning` 和 `convertMessage`，普通 runtime 注册 `onNew`、`onEdit`、`onCancel`、`onRefetchThread`，未实现的 `onReload`/`onResume` 不注册，因此对应 capability 保持关闭。FrameStore 内部通过 `useSyncExternalStore` 订阅并在 commit callback 更新 lifecycle ref；不再让 `useAssistantTransportRuntime` 直接消费旧 `set`/`append-text` 协议。业务 resume 由现有 `useBusinessResume` 调用 business POST 后，再由 attach 控制引用建立只读订阅；attach-only recovery 是 FrameStore 的内部动作。Workbench 的 readonly surface 在挂载时由同一 FrameStore 直接执行 attach-only atomic recovery，不调用 `aui.thread.resumeRun`/`onResume`，也不注册写入 handler。

handler 边界固定为：

| handler | 责任 | 禁止行为 |
| --- | --- | --- |
| `onNew` | 创建幂等 command、建立或复用当前 Run 的 attach、把 canonical frame 交给 FrameStore | 不把本地 pending 直接当成 Run 已创建 |
| `onEdit` | 提交 edit command，按 parent message 重建可见分支并等待 canonical frame | 不把草稿写入 Transport snapshot |
| `onReload` | 当前不注册，重新生成入口尚未纳入本次改造 | 不把 attach-only recovery 当重新生成 |
| `onCancel` | 提交 cancel command，保留 cancelling 状态直到 canonical Run status 收敛 | 不本地伪造 `cancelled` |
| `onResume` | 当前不注册；business resume 由现有 resume service 负责 | 不用来修复断开的 SSE |
| `onRefetchThread` | 调用原子 attach/recovery，full replace FrameStore | 不发送 command、不创建 Run |

官方 `queue` 只用于“Run 进行中用户继续发送”的 composer queue；`pendingByCommandId` 只用于 command 幂等/回显关联，不再自研第二套消息队列。Workbench readonly 不注册这些写入 handler。

External Store 不支持的可选能力（例如 `onDelete`、工具审批或其他当前项目未实现的 handler）必须不注册，并保持对应 capability 为 false；不能因为 runtime bridge 统一而误开启 UI 操作。

### 6.2 缓存结构

只维护一层项目级 FrameStore，不重复维护 Assistant UI 的 `ThreadMessage` 转换缓存：

```text
TransportFrameStore:
  state: TransportState
  runItems: FrameStoreItem[][]
  items: readonly FrameStoreItem[]
  pendingCommands: readonly UserAddMessageCommand[]
  targetRunStatus: string | null
  resyncRequired: boolean
```

`StableTransportMessage` 只在对应 mutation 触达时替换引用；未触达消息继续复用。FrameStore 输出给 `useExternalStoreRuntime` 的 `messages` 数组在 membership 变化或内容更新时都创建新的浅数组，内容更新只替换受影响的 message 引用；不能原地修改数组。项目提供稳定的 per-message `convertMessage`，或使用官方 `useExternalMessageConverter`，负责把 transport message 转成包含 tool/error/render context 的 `ThreadMessageLike`。Assistant UI 负责 runtime/list primitive 的消费和 lazy accessor；项目不再维护 `runCache`、`messageCache`、`flatViewCache` 三套等价缓存。

FrameStore 内部身份使用明确 namespace，pending 不得与 canonical 消息共用 key：

```text
canonical:<runId>:<messageId>
pending:<commandId>
snapshot-error:<errorSignature>
```

FrameStore 只接受当前连接对象的 frame；连接关闭、backend 重启或 task 切换时，旧连接的 producer 和 queue 一并销毁，新连接必须先安装 full snapshot 才能继续应用 mutation。连接状态只是运行时控制状态，不进入 wire payload、业务数据库或 projection message 身份；不把任何 projection cache 写入 localStorage 或 SQLite。

缓存必须有明确的 invalidation：

- message 不再出现在当前 task state 时，从 `messagesById` 移除；当前代码的 fork 是创建新 task，不把 branch 当作同一 task 内的现有 TransportState 概念；
- full snapshot/recovery 后按当前可见 message ids 清理旧索引；
- task 切换时整体丢弃；
- backend 重启或连接切换时整体丢弃；

### 6.3 按 update 类型增量处理

projection 先识别 update 类别：

#### A. 仅当前 text/reasoning append

- 复用所有历史 `ThreadMessage` 对象；
- 只重建受影响 message；
- 只复制扁平数组中当前 message 对应位置；
- 保持 message ids 和历史数组结构引用稳定。

#### B. part status / tool artifact 变化

- 只重建包含该 part 的 message；
- `ToolPart` 的 route 仍由 `display_data.kind` 和 `presentation` 决定；
- 不从工具名创建专用路由分支。

#### C. 新 message 或新 run 追加

- 只构造新增 run/message；
- 更新受影响 run 的 `lastAssistantMessageId`；
- 仅在“上一条 assistant message 的 `isLastRunMessage`”变化时重建上一条消息。

#### D. context usage、pending command 或 isSending 变化

- 不触碰 canonical message cache；
- 只更新 `isRunning` 或 usage 相关 projection。

#### E. full snapshot / recovery

- 允许完整重建；
- 以当前连接建立新的 FrameStore 基线；
- 不从 frontend 自行猜测缺失 mutation；FrameStore 通过原子 attach/recovery 收到 full frame 后继续消费同一连接。

### 6.4 稳定 selector 与现有桥接组件

官方文档要求 `useAuiState` selector 返回稳定引用或 primitive。现有 `ThreadPrimitive.Messages` children render function 应继续保留，因为 Assistant UI 内部已经实现了 lazy item accessor。

第二阶段必须同时重构这些组件：

- 当前 `TransportStateCommitBridge` 已删除；FrameStore 的 commit callback 直接更新 lifecycle ref，不能再依赖 Assistant UI 的宽 `runtimeState.thread.state` 触发 React bridge。
- lifecycle ref 由 FrameStore commit 直接更新；React 只订阅 UI 真正需要的 `isRunning`、当前 run、cancel settling 和 recovery error 等稳定字段。
- 不使用 Assistant UI 未公开的内部 store API；通过官方 `useExternalStoreRuntime` 和项目自己的 external store 完成连接。
- `ToolTraceGroup` 的 selector 应保持返回稳定 primitive；不要改成每次返回新数组或对象。
- 取消、attach-only recovery、business resume 和 cancel settling 由新 runtime bridge 明确分工；FrameStore 不得清除 `cancellingRunId` 等本地取消标记，也不能把“取消请求已接受”转换成终态。

生命周期边界必须保持可区分：

| 场景 | projection 行为 | 禁止的推断 |
| --- | --- | --- |
| 新消息 / edit business run | 以 canonical state 的新 run/status barrier 更新 projection | 不从 pending command 直接推断 Run 已创建 |
| attach-only recovery | 用 full snapshot 重新建立 cache，然后继续订阅 | 不重放旧 command，不重复发送业务请求 |
| business resume | 由现有 resume service 产生新的 canonical event 流 | 不把 transport attach 当作业务 resume |
| cancel requested | 保留本地 `cancellingRunId`，等待 canonical status | 不把“取消已接受”渲染成 `cancelled` |
| backend restarted / connection changed | 丢弃 task-scoped projection cache，等待新 full snapshot | 不把旧连接 frame 继续拼接到新连接 |
| fork | 当前代码通过创建新 task 隔离事实 | 不在同一 task projection 内模拟不存在的 branch 状态 |

### 6.5 前端 projection 测试

新增或扩展单元测试：

- 1000 条历史消息 + 当前消息连续 1000 次 append，只重建当前 message；
- 新 assistant message 追加时，只失效上一条和新消息；
- run status 变化只失效对应 run；
- context usage 变化不失效任何 canonical message；
- backend 重启或连接切换后不会复用旧 projection cache；
- 删除/新 task 切换后不会显示 stale message；
- pending command 被 canonical message 接收后不会重复显示；
- snapshot error 的添加/清除不影响 canonical message 对象；
- pending command identity 和 canonical message 不互相污染；
- full recovery 后缓存不会跨 task 或跨 runtime session 复用；
- `onNew/onEdit/onCancel/onRefetchThread` 分别映射到正确 command、cancel 或原子 attach/full recovery；无 active Run 时 `onRefetchThread` 才读取 idle full snapshot；未注册的 `onReload`/`onResume` 保持 capability 关闭，attach-only recovery 不重复发 command；
- Workbench readonly runtime 不注册写入 handler，且与普通 Thread 共用同一 FrameStore decoder。

## 7. 第三阶段：消息虚拟化

### 7.1 启用条件

Assistant UI 官方建议只有在 React mount/update 成为瓶颈时才虚拟化。当前项目已经有 `content-visibility`，因此不要仅依据“消息很多”自动切换。

先用测试 fixture 测量：

- 200、1000、5000 条消息；
- 普通 Markdown、长代码块、reasoning、tool trace 混合；
- streaming 期间 CPU、React commit、long task、DOM 数量、滚动稳定性；
- 编辑、fork、cancel、resume、pending user message；
- sticky footer/composer、`ScrollToBottom`、Workbench `ReadonlyThread` 和 task 切换/recovery。

再由配置决定是否启用，例如：

```text
VIRTUALIZED_THREAD_MIN_MESSAGES
```

阈值必须是可调的 frontend presentation policy，不写入 Transport contract，也不作为业务事实。

### 7.2 组件边界

不要让 `Thread` 根据阈值重新挂载 `AssistantRuntimeProvider`。建议保持：

```text
AssistantRuntimeProvider
└─ ThreadShell
   ├─ StandardThreadViewport
   └─ VirtualizedThreadViewport
```

只替换 viewport/message list，不替换 runtime、task session 或 transport connection。

建议新增 UI 层模块：

```text
apps/desktop/components/assistant-ui/thread/
├─ thread-shell.aui.tsx
├─ standard-thread-viewport.aui.tsx
├─ virtualized-thread-viewport.aui.tsx
├─ virtualized-thread-rows.ts
└─ virtualized-thread-scroll.ts
```

如果当前文件拆分会导致重复渲染代码，应先把 UserMessage、AssistantMessage、Composer 和工具 group 提取为共享组件，再接入两种 viewport。这不是可选清理：`ThreadPrimitive.Unstable_MessageById` 需要显式的 `components` map，当前基于 `ThreadMessage` 内部 `useAuiState` 的 role 分派不能直接当作该 map。

### 7.3 官方 API 组合

虚拟化 viewport 使用：

- `unstable_useThreadMessageIds()` 获取稳定 message id 列表；
- `ThreadPrimitive.Unstable_MessageById` 以 `{ messageId, components: MESSAGE_COMPONENTS }` 渲染可见消息；
- `@tanstack/react-virtual` 管理可见 rows 和测量；
- `virtualizer.measureElement` 测量动态高度；
- virtualizer wrapper 的总高度与绝对定位 row 表示未挂载区域；
- 当前实现把定位限制在 virtualizer wrapper，并使用 `measureElement` 重测动态高度；如果后续 top-anchor 或 sticky footer 的实测滚动修正不稳定，再切换为 spacer 的 `paddingTop/paddingBottom` 布局，而不是扩散定位逻辑。

官方 API 是 experimental，因此所有调用必须集中在 `virtualized-thread-viewport.aui.tsx`，未来升级 Assistant UI 时只需要修改这一层。

### 7.4 row 设计

优先按 user turn 分组：一个 user message 加上其后直到下一个 user message 前的 assistant/tool/reasoning 消息组成一个 row。官方文档这样做是为了让虚拟化 item 更稳定、尺寸更有意义。

row 的稳定身份只包含：

```ts
type MessageRow = {
  id: string;
  messageIds: readonly string[];
  role: "user" | "assistant" | "system";
};
```

row 列表只在 message membership 或 role 变化时重建；当前 assistant 的 text append 不应重建整条 rows 数组。

### 7.5 scroll 与当前 top-anchor 语义

官方虚拟化示例要求自行拥有 scroll element，因为内置 auto-follow 的测量调整可能和 virtualizer 争夺 scroll position。

本项目当前不是默认 bottom auto-scroll，而是 `turnAnchor="top"`、`autoScroll={false}`。因此不能直接复制官方 bottom-follow 示例。实现时必须二选一并写清楚：

1. 保留当前 top-anchor 产品语义：通过自有 scroll controller 注册当前 turn anchor，并在 active row 高度变化时保持 anchor 的视觉位置；或
2. 明确产品改变为 bottom-follow，并完整迁移 auto-follow、用户上滚解锁和 run-start jump 语义。

默认选择方案 1。当前 top-anchor 并非只由 viewport 属性决定；Assistant UI 还依赖 `MessagePrimitive.Root` 注册 anchor/target。因此 virtualized POC 必须同时挂载 active turn 的前置 user message 和当前 assistant target，并验证 row 重测量后 anchor 仍稳定。若无法仅用公开 API 稳定实现，不启用虚拟化，继续使用 standard viewport + `content-visibility`，不能使用 undocumented internal API 绕过。

虚拟化 scroll controller 必须覆盖：

- 用户滚动时不强行拉回；
- streaming 时 active turn 始终挂载；
- active turn 的 user anchor 和 assistant target 始终挂载；
- row 重测量不造成跳动；
- run start 不出现新 user message 闪烁；
- 切换 task、resume、recovery 后 scroll position 可解释；
- `ScrollToBottom` 与 composer footer 不互相覆盖。

POC 还必须验证 sticky `ViewportFooter` 与 top/bottom spacer 的关系、编辑态 Composer 的定位和提交、`ScrollToBottom` 是否绑定自有 scroll element、以及 `content-visibility:auto` 对 `measureElement` 的影响。Workbench `ReadonlyThread` 默认不单独复制一套虚拟化实现：如果它也需要虚拟化，必须复用同一 viewport/row 组件并单独通过 fixture 验证；否则明确保持 standard viewport。

### 7.6 虚拟化下的复杂内容

- 当前 active assistant message 不卸载；
- 正在编辑的 user message 不卸载；
- 展开的 reasoning/tool row 在用户操作期间保持挂载；
- 大型 diff/terminal 仍采用 deferred body rendering，不能因为虚拟化而一次性 mount 全部复杂组件；
- `content-visibility` 继续保留在 row/message 层，但必须以实测结果决定是否对 virtualizer 测量关闭；不能默认假设两者叠加后仍能准确测高。

## 8. 观测、测试和验收门槛

### 8.1 后端指标

只记录结构化元数据，不记录 prompt、token、完整模型输出或密钥：

- `snapshot_projection_duration_ms`
- `snapshot_full_copy_duration_ms`
- `snapshot_validation_duration_ms`
- `snapshot_frame_kind`
- `snapshot_mutation_count`
- `snapshot_coalesced_mutation_count`
- `subscriber_queue_depth`
- `subscriber_resync_count`
- `transport_frame_encode_count`
- `transport_frame_emit_count`
- `transport_mutation_bytes`
- `transport_full_snapshot_bytes`
- `transport_recovery_count`
- `transport_recovery_latency_ms`
- `sse_flush_count`（仅作为底层 HTTP 写出诊断，不再作为主要业务指标）

按 task/run 记录标识和计数即可，遵守项目既有日志规范。backend 使用 `log.info`/`log.warning`/`log.error` 的结构化 `extra`；frame/resync 行为对应的 docstring、日志 event 和测试必须在同一 PR 更新。

### 8.2 前端指标

- `transport_projection_duration_ms`
- `transport_projection_rebuilt_message_count`
- `transport_projection_reused_message_count`
- `transport_projection_full_rebuild_count`
- `transport_decoder_resync_count`
- `transport_projection_commit_count`
- `transport_recovery_latency_ms`
- `assistant_ui_commit_duration_ms`
- `assistant_ui_message_count`
- `assistant_ui_mounted_message_count`
- `assistant_ui_dom_node_count`
- `assistant_ui_long_task_ms`
- `virtualizer_measurement_count`
- `virtualizer_scroll_correction_count`

指标应采样记录，不能每个 delta 都打印大对象。React 侧统一通过 `frontendLog(level, event, msg, { traceId, data, error })` 记录，复用当前 task 的 trace id；不依赖浏览器控制台。落盘路径沿用 `app_data_dir()/.cosir/logs/frontend-YYYY-MM-DD.log`，日志失败不得阻断渲染。性能计时器应作为 diagnostics/observability 旁路，可在生产降低采样率或关闭。

### 8.3 正确性测试

后端：

- 同一连接内 mutation 顺序；
- task 冷启动、backend restart、task 删除时的连接创建与清理；
- 重复 event/no-op 不产生 mutation frame；
- append-text 合并不改变文本顺序；
- part closed/tool status/run status 不被跨 barrier 合并；
- queue overflow 只产生 resync，不产生静默丢字；
- resync latch 会关闭当前 SSE，下一次原子 attach/recovery 的 full 首帧能恢复任意 overflow 状态；
- overflow 后 terminal full 与 resync marker 的严格优先级和发送顺序；
- coalescer 不跨 task-level barrier，terminal/status 不会被静默丢弃；
- backend restart 后旧 stream/frame 不会写入新连接；
- backend restart 后不会重放旧 Run。

前端：

- projection cache 的对象引用复用上界；
- mutation-only frame 与 full snapshot 结果一致；
- backend restart/connection change 会清空旧 projection cache，并以 full snapshot 建立新基线；
- tool renderer 不按工具名产生第二套路由；
- virtualized 与 standard viewport 显示相同消息、状态和操作；
- 编辑、fork、cancel、resume、recovery、pending command 全部覆盖。

### 8.4 性能验收

不要先写死一个脱离设备的绝对数字。使用同一台开发机、同一模型输出 fixture 和同一浏览器版本，比较改造前后：

1. 后端单次 mutation p95；
2. SSE flush 次数和 bytes；
3. projection p95 和每帧重建 message 数量；
4. streaming 期间 React commit p95；
5. 主线程 long task；
6. 首次打开、重连、切换 task 的耗时；
7. 1000/5000 条消息下的 mounted message 数量。

只有在 benchmark 证明虚拟化收益稳定且 scroll correctness 不退化时，才打开默认阈值。

## 9. 实施顺序与交付边界

### PR 1：新 frame wire contract 与后端增量热路径

范围：

- `apps/backend/app/assistant_transport/stream/`
- `ConversationTaskStateService`
- `AssistantTransportStreamService`
- `apps/backend/app/assistant_transport/service/conversation_event_projector.py`
- `apps/backend/app/assistant_transport/service/conversation_run_command_service.py`
- `apps/backend/app/assistant_transport/service/transport_assistant_service.py`
- `apps/backend/app/assistant_transport/assistant_api.py`
- Assistant Transport response serializer / snapshot read / attach routes
- subscriber resync latch / coalescer
- backend connection 生命周期
- 对应后端单元/集成测试
- SSE JSON envelope 与前端 decoder 的契约测试

交付判定：`SnapshotChange` 被删除；`assistant_api.py`、`TransportAssistantService`、`AssistantTransportStreamService`、`AssistantTransportResponse`/serializer 和 create/attach 路径全部切换到新 frame SSE；mutation-only、full、resync_required 三类 frame 均能被同一条新链路处理；没有旧 wire 或 feature flag 双轨。

### PR 2：前端 incremental projection store

范围：

- `apps/desktop/lib/assistant/transport/` 下的新 frame decoder、projection store、Assistant UI runtime bridge
- 已删除的 `apps/desktop/lib/assistant/converter.ts` 中旧 `toTransportThreadView()` projection
- `apps/desktop/lib/assistant/use-task-assistant-transport-runtime.ts`
- `apps/desktop/components/workbench-agent-run-surface.tsx` 的 readonly/transport 入口
- `apps/desktop/components/assistant/runtime/` 的 commit bridge 性能收敛
- backend connection 变化时的 projection cache 清理
- projection 单元测试

交付判定：`useRuntimeTransport()` 与 Workbench readonly 入口都接入同一 `TransportFrameStore`/external runtime；连续 append 只重建受影响 Run 的 projection entry，full frame 只作为基线/recovery 输入；FrameStore commit callback 不依赖每帧完整 AUI state。

### PR 3：独立 virtualized viewport（核心代码已落地）

范围：

- `components/assistant-ui/elements/thread.aui.tsx` 与 `readonly-thread.aui.tsx` 的共享消息 viewport 适配层
- `@tanstack/react-virtual`
- Playwright 长对话 fixture 和滚动回归测试（待补齐）

交付判定：普通 Thread 与 Workbench readonly 均通过公开的 message-id/MessageById API 和 `@tanstack/react-virtual` 渲染可见 rows；runtime、消息组件、工具组件和 projection state 保持共享。长对话滚动与测量回归仍需 E2E fixture 完成后关闭该验收项。

### PR 4：按实测结果清理与固化

- 删除重复 projection 逻辑；
- 固化 frame、连接生命周期和 resync control lane contract；
- 固化性能指标；
- 确认每个前置 PR 已同步更新 docstring、类型契约、结构化日志和测试，不把职责更新延后；
- 升级 Assistant UI 时优先检查 experimental virtualization API 适配层。

## 10. 不做的事情

- 不把 Assistant UI wire schema 泄漏到 workflow、service 或 storage；
- 不让 React 直接管理后端进程；
- 不把 projection cache 写入业务数据库；
- 不用 React.memo 代替稳定 selector 和增量 projection；
- 不为了减少 flush 而跨越 lifecycle barrier；
- 不删除 Streamdown 的 `defer`；
- 不默认对所有规模的 Thread 启用虚拟化；
- 不用 undocumented Assistant UI internal API 实现 top-anchor；
- 不把 context compression 当作 UI rendering 优化；
- 不为了本地性能引入 Redis、Postgres、云队列或公网部署层。

## 11. 最终判断

本方案的长期维护核心是三条边界：

1. backend 只优化 snapshot frame 的复制、校验和传输，不改变 workflow/domain 事实；
2. frontend 只优化 Transport-to-Assistant-UI projection，不建立第二套 conversation state；
3. virtualization 只存在于 Assistant UI presentation adapter，不把 experimental API 扩散到业务层。

第一阶段和第二阶段应先实施。第三阶段只有在 profiling 证明 mount/update 成本超过 `content-visibility` 能解决的范围后才实施。这样既利用成熟依赖和官方 API，又不会为追求局部 benchmark 而制造长期难以演进的第二套状态系统。

# Assistant 流式渲染隔离改造实施计划

> **面向执行代理：**按任务顺序实施；每个任务完成后运行对应测试和性能验收，不提交 git。

**目标：**在不修改后端接口和 Assistant Transport wire schema 的前提下，把流式更新限制在 active row 与必要状态组件，消除长对话中由父级 React 重渲染造成的历史消息和 Composer 扇出。

**架构：**TransportFrameStore 继续作为前端唯一的流式投影和事实读取入口，保留 mutation 合并、稳定消息对象身份和终态收敛逻辑。新增明确的 React 更新边界：transport host 可以随流更新，Thread shell、历史 row 和 Composer 不再因为 transport host 的父级重渲染而被动更新；active row 通过 assistant-ui 的消息级订阅更新。开发期性能探针只负责验证边界，不参与生产运行时。

**技术栈：**React 19、assistant-ui external store runtime、`useSyncExternalStore`、TanStack Virtual、Vitest、真实 Tauri/WebView 开发模式验收。

## 全局约束

- 只修改 `apps/desktop` 前端；不修改 Python 后端、Rust 宿主、HTTP/SSE 接口和 Transport wire schema。
- 绿地项目不保留旧渲染路径、兼容分支、版本字段或 feature flag；完成结构替换后删除旧路径。
- 不新增依赖；不把渲染快照写入 localStorage、SQLite 或后端。
- 运行环境是 Tauri WebView，不使用浏览器专属运行拓扑描述；动画帧调度注释统一使用“宿主 WebView 的动画帧”。
- 源码注释和 docstring 使用中文，并准确说明更新边界、副作用和生命周期。
- 开发期探针只在 `import.meta.env.DEV` 创建和记录；生产构建不产生探针实例。
- 不提交 git；执行阶段由调用方单独决定是否提交。

## 当前根因和改造判据

当前 `useTaskAssistantTransportAdapter` 通过 `useSyncExternalStore` 订阅 `TransportFrameStore`，其每次 `view` 变化都会让调用它的 `AssistantRuntimeSession` 重新执行。于是包含 Thread、虚拟列表和 Composer 的整棵 JSX 子树都参与更新；即使 assistant-ui 的消息级订阅最终能复用部分消息对象，父级边界仍会重复提交。

改造完成后必须同时满足：

1. `TransportFrameStore` 可以每帧发布 active row，但 transport host 的重渲染不能重新执行稳定的 Thread shell。
2. 历史 row 组件用稳定 `messageId` 和稳定 `components` 接收数据；active row 之外的消息级 Profiler 在流式期间不再产生 commit。
3. Composer 单独位于稳定渲染边界，只在输入、运行状态、取消状态或必要控件状态变化时更新。
4. 终态只产生一次完整收敛；收敛后不得有排队的 active publication 再触发第二次完整树更新。

## 文件职责地图

- 修改 `apps/desktop/components/assistant/runtime/assistant-runtime-session.tsx`：只负责任务级上下文、恢复/取消控制和稳定的运行时 host 参数，不再直接承载随 transport view 变化的 UI 子树。
- 新建 `apps/desktop/components/assistant/runtime/assistant-runtime-transport-host.tsx`：唯一调用 `useRuntimeTransport` 并装配 `AssistantRuntimeProvider`；该文件允许随流执行，但只向下渲染稳定的 surface 组件。
- 新建 `apps/desktop/components/assistant-ui/elements/assistant-thread-surface.tsx`：承载 RuntimeControlBridge、各类 bridge、Thread shell 和 Composer surface；使用稳定 props，避免由 transport host 的函数重新执行导致整棵 UI 重新执行。
- 修改 `apps/desktop/components/assistant-ui/elements/thread.aui.tsx`：将 Thread 导出为稳定 memo 边界，保留 assistant-ui 交互契约；不在这里承载 transport 生命周期。
- 新建 `apps/desktop/components/assistant-ui/elements/virtualized-thread-message-row.tsx`：单条虚拟消息 row，负责 message-id provider、测量 ref、Profiler 和 row padding；用 `memo` 隔离历史 row。
- 修改 `apps/desktop/components/assistant-ui/elements/virtualized-thread-messages.tsx`：只负责 message id、虚拟范围和 active tail 范围计算，把 row 细节下沉到独立组件。
- 修改 `apps/desktop/lib/assistant/assistant-performance-probe.ts`：把“父级边界提交”和“消息 row 提交”分开统计，增加终态收敛计数和流式期间历史 row 提交计数。
- 修改 `apps/desktop/lib/assistant/transport-frame-store.ts`：只在必要处强化发布阶段和终态收敛的可观测边界；不改变 wire 解析和状态投影语义。
- 修改 `apps/desktop/lib/assistant/use-task-assistant-transport-runtime.ts`：保持 transport runtime 对外接口不变，仅确认 adapter 变化不会把稳定 UI props 带入 host 外层。
- 修改 `apps/desktop/tests/unit/transport-frame-store.test.ts`：补齐帧合并、稳定历史 item 身份、终态一次收敛的测试。
- 修改 `apps/desktop/tests/unit/assistant-performance-probe.test.ts`：补齐历史 row、active row、Composer、终态收敛指标的聚合测试。
- 新建 `apps/desktop/tests/unit/assistant-render-boundary.test.tsx`：用最小可控组件树验证 transport host 更新不会导致 Thread shell、历史 row 和 Composer 的父级重新执行；不依赖真实后端。
- 修改 `docs/plan/long-conversation-streaming-performance-plan.md` 或新增验收说明：记录 Tauri WebView 的性能指标和运行方式，删除浏览器措辞。

---

### 任务 1：建立 transport host 与稳定 UI surface 的边界

**文件：**

- 修改：`apps/desktop/components/assistant/runtime/assistant-runtime-session.tsx`
- 新建：`apps/desktop/components/assistant/runtime/assistant-runtime-transport-host.tsx`
- 新建：`apps/desktop/components/assistant-ui/elements/assistant-thread-surface.tsx`

**接口：**

- `AssistantRuntimeSession` 继续接收现有 `RuntimeSessionProps` 和 `setIssue`，输出任务级 Assistant UI。
- `AssistantRuntimeTransportHost` 接收稳定的 `RuntimeSessionContext`、`RuntimeRecovery`、`selectedAllowsTools` 和现有 bridge 回调，内部调用 `useRuntimeTransport`。
- `AssistantThreadSurface` 接收 Thread 展示和操作 props；不接收 `TransportFrameStore`、`view` 或任意 token 级数据。

- [ ] 将 `useRuntimeTransport` 从 `AssistantRuntimeSession` 移入 `AssistantRuntimeTransportHost`，使 transport store 的 `useSyncExternalStore` 更新只发生在 host 子树。
- [ ] 将 `AssistantRuntimeProvider`、`RuntimeControlBridge`、`ComposerRestoreBridge`、`TaskStateBridge`、`RuntimeRenderDiagnostics` 和 Thread UI 组装移动到 surface 文件，保证职责按“运行时装配”和“展示 surface”分开。
- [ ] 将 host 下发的 UI props 用 `useMemo`/`useCallback` 固定身份；运行中变化的只允许 `cancellingRunId`、运行状态桥接和 active message 的 assistant-ui 消息订阅。
- [ ] 不新增第二份 Transport state；surface 只能通过 assistant-ui runtime 读取渲染状态。
- [ ] 增加最小测试，验证 transport host 的 store 更新不改变稳定 Thread surface 的 props 身份。
- [ ] 运行 `npm run test:unit -- --runInBand` 或项目实际 Vitest 命令，确认现有 runtime/transport 测试通过。

### 任务 2：把 Thread shell 和 Composer 变成稳定更新域

**文件：**

- 修改：`apps/desktop/components/assistant-ui/elements/thread.aui.tsx`
- 修改：`apps/desktop/components/assistant-ui/elements/assistant-thread-surface.tsx`

**接口：**

- `Thread` 的 props 保持当前业务语义，但导出值改为 memoized component。
- 新增 `ThreadComposerSurface`，只接收 `autoFocus`、`taskId` 和 `workspaceRoot` 等静态 props；运行状态由 Composer 自身的 assistant-ui selector 读取。

- [ ] 将 `Thread` 改为 `memo(function Thread(...))`，自定义比较只比较真实展示 props；禁止把整个 runtime、transport state 或新建对象作为 props。
- [ ] 将 Composer 从会随 message list 执行的 JSX 层拆成独立 memoized surface，避免 Thread list 的更新重新执行 Composer subtree。
- [ ] 保留发送、取消、恢复、编辑和工具选择行为，不通过额外 state 镜像 assistant-ui 的 composer 或 run 状态。
- [ ] 将性能 Profiler 从 Thread shell 外层移到明确的 message-list 和 composer surface 边界，避免把父级重新执行误记为实际 row commit。
- [ ] 测试空会话、运行中、取消中、完成后四种状态下 Composer 的可用性和按钮行为。

### 任务 3：建立 memoized 虚拟消息 row

**文件：**

- 新建：`apps/desktop/components/assistant-ui/elements/virtualized-thread-message-row.tsx`
- 修改：`apps/desktop/components/assistant-ui/elements/virtualized-thread-messages.tsx`

**接口：**

- `VirtualizedThreadMessageRowProps`：`messageId: string`、`index: number`、`components: MessageComponents`、`rowPaddingBottom: string`、`measureElement: (element: HTMLElement | null) => void`、`performanceProbe`。
- Row 只通过 `ThreadPrimitive.Unstable_MessageById` 获取对应消息，不接收完整消息对象，不接收整个 thread state。

- [ ] 把 `ThreadPrimitive.Unstable_MessageById` 和 `virtualizer.measureElement` 下沉到独立 row 组件。
- [ ] 对 row 使用 `memo`，比较 `messageId`、`index`、`components`、`rowPaddingBottom` 和测量回调身份；active 内容变化必须通过 assistant-ui message subscription 进入 row 内部，而不是改变 row props。
- [ ] 将单 row Profiler 放在 memoized row 内部，记录真实 row 更新；历史 row bailout 时不产生历史消息 commit。
- [ ] `VirtualizedThreadMessages` 只在 message id 序列、虚拟范围、active tail id 或滚动测量发生变化时重新计算；不要在每次 token 更新时新建所有 row 的 props 对象。
- [ ] 保留 active tail 的 range extractor，确保流式 active message 即使不在当前可视范围也能更新；不扩大 overscan 作为性能补丁。
- [ ] 增加测试：内容 mutation 只改变 active row 的数据身份；历史 row 的 `FrameStoreItem` 和 row props 身份保持不变。

### 任务 4：重定义开发期性能探针，使指标对应验收标准

**文件：**

- 修改：`apps/desktop/lib/assistant/assistant-performance-probe.ts`
- 修改：`apps/desktop/components/assistant-ui/elements/assistant-thread-surface.tsx`
- 修改：`apps/desktop/components/assistant-ui/elements/virtualized-thread-message-row.tsx`
- 修改：`apps/desktop/components/assistant/runtime/assistant-runtime-session.tsx`
- 修改：`apps/desktop/tests/unit/assistant-performance-probe.test.ts`

**接口：**

- `AssistantPerformanceReport` 至少包含：`frameCount`、`publicationCount`、`activeRowCommitCount`、`historicalRowCommitCount`、`composerCommitCount`、`messageListBoundaryCommitCount`、`terminalConvergenceCount`、`maxReactActualDurationMs`、`maxApplyFrameMs`、`maxPublicationMs`、`targetRunId`、`targetRunStatus`。
- `recordReactCommit` 必须区分 `active-row`、`historical-row`、`composer`、`message-list-boundary`。
- `recordTerminalConvergence` 只在完成/失败/取消/中断的最终完整状态收敛点调用一次。

- [ ] 删除会把父级 Profiler commit 当作历史消息 commit 的旧计数语义，改成明确的 row boundary 统计。
- [ ] 继续只在开发期累计统计，并在一次流结束后异步汇总输出；不在 token 粒度写日志。
- [ ] 测试 1000 个历史消息、active message 连续更新、Composer 状态切换和终态回调的计数边界。
- [ ] 将报告字段与验收标准一一对应，避免使用“baseline”这种无法判断是否通过的笼统名称。

### 任务 5：强化 TransportFrameStore 的终态和身份契约

**文件：**

- 修改：`apps/desktop/lib/assistant/transport-frame-store.ts`
- 修改：`apps/desktop/tests/unit/transport-frame-store.test.ts`

**接口：**

- 保留 `parseTransportFrame`、`TransportFrameStore.applyFrame`、`flushScheduledPublication` 和现有 wire 类型。
- 允许新增开发期内部统计钩子，但不得向 `TransportFrameEnvelope` 增加字段。

- [ ] 保证 active message content mutation 只排队一个 WebView 动画帧 publication；同一帧内多个 token 不重复通知。
- [ ] 结构 mutation、resync、终态 mutation 取消排队 publication，并执行一次明确的同步收敛。
- [ ] 明确终态收敛顺序：先取消 pending frame，再发布最终 snapshot，再记录一次 `terminalConvergence`，不得在终态后补发旧 active frame。
- [ ] 增加 1000 条历史消息的测试，确认内容 mutation 不替换历史 `FrameStoreItem`，历史 message id 和 converter cache 输入身份不变。
- [ ] 增加同一动画帧多个 mutation、终态紧跟排队 frame、resync 中断排队 frame 的测试。

### 任务 6：端到端开发期性能验收

**文件：**

- 修改：`apps/desktop/tests/e2e/frontend-regressions.spec.ts` 或新增专用前端性能验收 spec
- 修改：`apps/desktop/tests/e2e/test-server.mjs`（仅在现有内存测试服务确实需要注入 1000 条消息时）
- 修改：`docs/plan/long-conversation-streaming-performance-plan.md`

**接口：**

- 测试输入通过现有前端测试服务产生，不改真实后端 Transport wire schema。
- Tauri WebView 验收通过开发期 frontend log 的单次聚合报告判定，不读取浏览器专属性能 API。

- [ ] 构造 1000 条历史消息和一个持续流式 active message，确认历史消息 row commit 为 0。
- [ ] 确认流式期间仅有 active row、必要 Composer/status 组件更新；Composer 不再按 publication 次数增长。
- [ ] 确认每个动画帧最多一次 active row commit。
- [ ] 确认 Run 完成后 `terminalConvergenceCount === 1`，且完成后没有第二次完整 message-list 收敛。
- [ ] 确认后端请求、响应、SSE frame JSON 和 Transport envelope 与改造前完全一致。
- [ ] 在 Tauri 开发模式执行一次真实 WebView 验收；不把 Vite 浏览器页面测试结果当作桌面性能结论。
- [ ] 记录改造前后报告：`historicalRowCommitCount`、`activeRowCommitCount`、`composerCommitCount`、`maxReactActualDurationMs`、`terminalConvergenceCount`。

## 验收门槛

以下任一条件不满足，都不能宣称改造完成：

- 1000 条历史消息流式输出时，历史 row commit 必须为 0；允许 message-list boundary 记录存在，但不能导致历史 row 进入 Profiler。
- 流式期间 Composer commit 不能与 publication 一一增长，只允许输入、运行/取消和必要 status 状态改变触发。
- 每个 WebView 动画帧最多一个 active row commit。
- 每个 Run 的最终完整状态收敛恰好一次。
- 后端接口、SSE、Transport frame envelope 和现有 wire schema 无任何修改。
- 生产构建不创建性能探针、不输出性能聚合日志、不改变渲染时序。

## 方案自检

- 性能根因覆盖：任务 1、2、3 直接消除 transport host 到 Thread/Composer/历史 row 的父级扇出。
- 流式合帧覆盖：任务 5 保留并强化现有动画帧发布契约。
- 1000 条历史消息覆盖：任务 5 的单元测试和任务 6 的 Tauri WebView 验收共同覆盖。
- 终态一次收敛覆盖：任务 4 的计数指标和任务 5 的终态顺序测试共同覆盖。
- 后端与 wire 不变覆盖：全局约束和任务 6 的请求/响应断言共同覆盖。
- 没有兼容分支、版本字段、新依赖或生产期观测旁路。

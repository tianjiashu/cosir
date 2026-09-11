# 长历史对话流式性能第一版技术实现方案

## 1. 文档定位

本文是第一版性能优化的实现方案，不是对旧方案的直接执行指令。旧方案中涉及的 Transport subscriber-local 合帧在本版本明确排除；本版本只优化已经确认的前端转换热点，并保持现有后端协议、Run 语义和恢复语义不变。

**目标状态：第三轮子 Agent 只读验收为“通过”，可以进入实现。**

## 2. 第一版目标与边界

### 2.1 目标

在长历史对话持续流式输出时，减少每个 `SnapshotChange` 到达后对历史消息的重复转换和对象重建，使未变化的历史消息保持引用稳定，降低 React/assistant-ui 后续渲染压力。

第一版的成功标准不是把全链路改成“批量刷新”，而是让一次增量更新只重新转换真正变化的消息，并用固定测试和 Tauri 桌面运行测量证明行为没有回退。

### 2.2 明确不做

- 不做 Transport subscriber-local 合帧、节流、去抖或批量 `flush`。
- 不修改 FastAPI、SSE、`SnapshotChange`、`ConversationEventProjector`、SQLite 提交顺序或 Run 状态机。
- 不引入虚拟列表、历史分页、TanStack Virtual 或新的 UI 基础设施。
- 不把 assistant-ui runtime state 写回后端，不改变 attach/resume/cancel 语义。
- 不重写当前工具 UI 分组规则、Markdown 渲染器和滚动行为。
- 不通过深比较或 JSON 序列化生成缓存 key。

这些能力可以在后续版本单独评估；第一版必须能独立回滚，且不会把性能优化与业务协议变更绑在一起。

## 3. 当前代码事实与运行边界

### 3.1 运行拓扑

本改造只发生在 Tauri 管理的 WebView2 React 前端进程：

```text
Tauri Rust 主进程
  └─ 管理 Python/FastAPI 子进程生命周期
       └─ 产生 ConversationTaskSnapshot 的 SSE/Assistant Transport

WebView2 React
  └─ useRuntimeTransport
       └─ assistant-stream accumulator
            └─ useAssistantTransportRuntime
                 └─ converter
                      └─ assistant-ui ExternalStoreRuntime / Thread
```

数据库、LangGraph checkpoint、Run 生命周期和后端 snapshot 仍由后端所有；本次只在 WebView 内增加一个 task/runtime 生命周期内的转换缓存。桌面启动、停止、有限重启、崩溃恢复均不改。后端重启后前端按现有 boot gate 和 snapshot 恢复流程重新建立 runtime，旧缓存随 runtime 卸载释放，不跨重启恢复。

### 3.2 已确认的热点

当前实现位于：

- `apps/desktop/components/assistant/runtime/use-runtime-transport.ts`
  - `useRuntimeTransport` 将一个 inline `converter` 传给 `useTaskAssistantTransportRuntime`。
  - 每次 hook 重新渲染都会产生新的 converter 函数身份。
- `apps/desktop/lib/assistant/converter.ts`
  - `toTransportThreadView` 对 `state.runs` 和每个 `run.messages` 做完整 `flatMap`。
  - 每次调用都会为消息重新执行 `toThreadMessage`，即使历史消息的外部对象未变化。
- `apps/desktop/components/assistant-ui/elements/thread.aui.tsx`
  - 当前使用 `ThreadPrimitive.Viewport`、`ThreadPrimitive.Messages` 和自定义 `GroupedParts`；没有虚拟列表。
  - assistant-ui 的消息分组规则包含项目特有的 artifact 展示判断，不能在第一版随意替换。
- `assistant-stream` 当前 accumulator 对 `set` / `append-text` 使用路径级结构共享；未变化的消息对象可以保持引用稳定。

因此第一版最小且长期可维护的落点是：把“纯消息映射”和“Transport view 的缓存/遍历”拆开，在 Transport runtime 的生命周期内持有一个稳定 converter。

## 4. 设计方案

### 4.1 代码职责拆分

新增文件：

`apps/desktop/lib/assistant/transport-view-converter.ts`

职责：

- 创建 task/runtime 级别的 Transport view converter。
- 以外部 `TransportMessage` 对象身份缓存消息转换结果。
- 组装每次 view 所需的稳定输入，并维护必要的状态签名。
- 不负责 React、不负责 HTTP、不负责日志、不负责业务状态迁移。

保留文件：

`apps/desktop/lib/assistant/converter.ts`

职责收敛为纯映射：

- `toTextPart`、`toReasoningPart`、`toToolCallPart`。
- 工具状态、错误状态和 message part 的纯转换。
- 从稳定的消息输入转换成 assistant-ui `ThreadMessage`。

`apps/desktop/components/assistant/runtime/use-runtime-transport.ts`

只负责在 hook 生命周期内创建一次 converter，并将稳定函数身份传给 `useTaskAssistantTransportRuntime`；不在组件文件里实现缓存算法。

### 4.2 稳定 converter 生命周期

在 `useRuntimeTransport` 中使用 `useRef` 或惰性 `useState` 创建一次 converter 实例：

```ts
const converterRef = useRef<TransportViewConverter | null>(null);
if (converterRef.current === null) {
  converterRef.current = createTransportViewConverter();
}

return useTaskAssistantTransportRuntime({
  // 其他现有选项保持不变
  converter: converterRef.current,
});
```

实际实现应遵循当前 hook 的 TypeScript 类型和 lint 规则；上面的代码只表达生命周期要求，不要求照抄。

缓存生命周期与当前 runtime session 一致：

- 同一 task 的普通流式更新复用同一个 converter 和缓存。
- task 切换、runtime session 卸载或显式重建时释放缓存。
- 不使用 module-global cache，避免不同 task 的消息、工具状态或错误互相污染。
- 后端重启、前端 recovery 或初始 snapshot 重新建立 runtime 时允许重新转换全部消息。

### 4.3 官方消息转换能力与项目缓存边界

当前安装版本的 `unstable_createMessageConverter` 不能直接成为本项目普通 `converter` 的缓存实现。官方文档中的缓存路径位于 React hook `useThreadMessages`；其 `toThreadMessages` 是命令式转换入口，本身不承诺缓存。当前 Transport runtime 只接收普通 `converter(state, connectionMetadata)`，不能在该回调内调用 React hook。

因此第一版的主路径是 adapter 自己维护 runtime-local `WeakMap`。官方 helper 只作为 API 参考、cache-miss 的等价转换候选或后续升级实验，不得在方案或代码中声称 `toThreadMessages` 自动提供缓存。

若未来在独立 React 边界使用官方 `useThreadMessages`，必须显式传入 `joinStrategy: "none"`。官方默认 join 行为可能合并相邻 assistant/tool 消息，改变 message ID、tool 归属、artifact 分组和状态语义；第一版不允许发生这种变化。当前普通 converter 边界无法安全表达该 hook 调用，因此不为接入 helper 重构 runtime。

adapter 使用以下输入类型描述 canonical message 的最小渲染上下文：

```ts
type TransportMessageInput = {
  source: TransportMessage;
  runId: TransportRun["runId"];
  runStatus: TransportRun["status"];
  endReason: TransportRun["endReason"];
  isLastRunMessage: boolean;
};
```

这里的 `TransportRun` 必须直接复用 `apps/desktop/lib/assistant/contract.ts` 中的现有类型；第一版不新增不存在的 `TransportRunStatus`，也不把当前数字 `runId` 改成字符串。

设计要点：

1. `source` 是 assistant-stream accumulator 产生的外部消息对象；同一消息未发生路径更新时，保持同一 `source` 对象身份。
2. `runStatus`、`endReason`、`isLastRunMessage` 是当前 `toThreadMessage` 计算 UI status 所需的最小上下文，不能只把 `source` 当 key，否则 Run 完成、取消或错误时可能得到过期状态。
3. 消息缓存使用 `WeakMap<TransportMessage, CachedMessage>`；缓存值至少保存渲染签名。source 相同但 status 相关签名改变时，允许并且必须重新计算该消息。
4. pending command 和 snapshot error 不属于 canonical message，必须使用独立的 runtime-local 缓存/签名：pending 必须同时使用稳定的 command identity、command type、message parts、`parentId`、`sourceId` 等展示语义签名，不能只依赖对象身份；snapshot error 至少区分出现、值/身份变化和消失。
5. `isRunning`、pending command 列表和 snapshot error 的变化只影响临时消息、顶层运行态或相关消息；不能因此让所有 canonical 历史消息失效。
6. 转换回调继续调用项目已有的纯映射函数，不把 assistant-ui wire schema 泄漏到 workflow、service 或 storage。
7. 每次状态更新仍可能产生新的顶层 `messages` 数组；第一版不承诺数组引用稳定，验收关注消息对象和 part 对象的稳定性。

如果后续确需使用官方 helper，必须把 `unstable_` API 隔离在同一个 adapter 文件内，锁定当前 `@assistant-ui/react` 版本，并用 `joinStrategy: "none"` 的 golden tests 证明输出不变；否则继续使用项目自己的等价 `WeakMap` 实现。未来 assistant-ui 升级只需要适配这一层，而不是修改 Thread、runtime 或业务类型。

### 4.4 输入失效规则

缓存失效必须由结构共享和显式渲染签名共同决定：

| 变化 | 允许重新转换 | 不应重新转换 |
| --- | --- | --- |
| 当前 assistant 文本 `append-text` | 被 append 的当前消息及其受影响 part | 其他历史消息 |
| 新增 user/assistant message | 新消息；若前一条 assistant 的 `isLastRunMessage` 改变，也重新转换前一条 | 其他既有历史消息 |
| tool pending/running/completed/failed/cancelled | 对应 tool part/message | 无关消息 |
| Run status / endReason 改变 | 该 Run 中 status 依赖该字段的消息 | 其他 Run |
| `pendingCommands` 增加、变化或清除 | 对应临时 user message、顶层 `isRunning` 相关计算 | 无关 canonical 消息 |
| `snapshot error` 出现、变化或清除 | 对应临时 assistant error message | 其他 canonical 消息 |
| `context_usage` 或不影响展示的 snapshot 字段改变 | 0 条 canonical 消息 | 全部历史消息 |
| attach / initial snapshot / reconnect | 允许全量建立新缓存 | 不要求跨 session 保持引用 |
| task 切换 | 丢弃旧缓存 | 禁止跨 task 复用 |

实现中不得用数组下标作为缓存身份；消息重排、分支或重连时必须以 message/run 的稳定 ID 和当前外部对象身份为准。当前 adapter 仍可完整扫描 `runs` 生成顶层 view，但扫描不应触发未变化消息的转换。

### 4.5 保持现有 UI 和协议语义

- `converter` 的输入仍是 `useTaskAssistantTransportRuntime` 当前的 Transport state 和 metadata。
- `toTransportThreadView` 当前产生的 pending command、snapshot error、message status、tool lifecycle 和 assistant-ui message shape 必须保持等价。
- `ThreadPrimitive.Viewport`、`ThreadPrimitive.Messages`、现有 `assistantMessageGroupBy`、`StreamdownTextPrimitive` 保持不变。
- 后端继续在 SQLite commit 后 publish；每个 subscriber 继续按当前 `SnapshotChange` 接收并 flush。
- HTTP/SSE 断开仍只表示订阅中断；不因性能改造自动 cancel 或 business resume。

## 5. 实施步骤

### Step 1：建立纯转换基线

- 将当前 `toTransportThreadView` 的输出逻辑拆成可单测的纯函数。
- 保留当前所有 tool、error、pending、status、branch/edit/reconnect fixture。
- 为每个 fixture 记录转换结果的结构快照；不记录 token、prompt、凭据或完整模型正文到日志。

### Step 2：新增 runtime-local converter adapter

- 新建 `transport-view-converter.ts`。
- 实现 `TransportMessageInput`、稳定输入生成和 converter factory。
- 以 adapter-local `WeakMap` 作为主缓存；不在普通 converter 中调用官方 React hook。
- 若保留官方 helper 的兼容试验，必须设置 `joinStrategy: "none"`，并证明其输出与当前逐条映射等价；否则不进入运行时主路径。
- 单独处理 pending command、snapshot error 和 `isRunning` 元数据；第一版不把 assistant-ui 的 `toolStatuses` 纳入 canonical message cache signature，也不使用它覆盖后端 `TransportMessage.parts[*].status`。当前项目后端 tool status 是权威来源；只有未来引入客户端工具执行状态时才扩展该失效规则。
- 将缓存生命周期限定在 `useRuntimeTransport` 的一次挂载内。

### Step 3：接入 Transport runtime

- 删除 `use-runtime-transport.ts` 中的 inline converter。
- 传入稳定的 adapter converter。
- 不改该 hook 的 API 请求、resume、cancel、terminal reconcile 和 error handler。

### Step 4：加入可重复的性能诊断

测试环境可以统计 converter callback 次数和单次转换耗时；生产环境不记录逐消息内容。若增加前端诊断，使用项目约定的 `frontendLog`，只记录 `task_id`、`run_id`、消息数量、命中/未命中计数和耗时摘要。

### Step 5：完成验证后再做 Tauri 桌面 A/B

先建立当前工作树的单元、类型、构建和真实 Tauri E2E 基线，再通过同一套检查确认没有新增失败；不能把已有失败误报为本次回归。仓库现有 `npm run test:e2e` 是 Vite + 内存测试服务的 renderer-only 参考测试，不得冒充 Tauri E2E。随后使用由 Rust/Tauri 启动的同一应用和固定 fixture 对比改造前后。若 converter 命中正确但 WebView2 中的 React commit 仍是主要瓶颈，本版本不顺手引入虚拟化，而是记录为下一版本候选。

## 6. 验收条件

验收必须同时满足行为正确性、引用稳定性、范围控制和性能指标；只“能编译”不算通过。

### 6.1 功能等价性（硬门槛）

- 现有 assistant-ui runtime、Thread、composer、工具展示、编辑、分支、取消、错误和重连相关单元/类型/构建检查通过。真实 Tauri E2E 必须先记录当前基线，并确保改造没有新增失败；当前 Playwright `test:e2e` 只作为 renderer-only 参考，不满足桌面验收。已知 seeded tool/search 的历史加载失败若仍存在，标记为既有基线失败，不得归因于本次改造。
- 对相同 Transport fixture，优化前的参考纯转换器与新 adapter 输出深度等价，至少覆盖：纯文本、reasoning、tool call、工具结果、pending command、Run 完成、取消、失败、snapshot error、空消息和多 Run 历史。
- 现有 Transport integration tests 证明后端 commit-before-publish、attach-only、cancel 和 terminal reconcile 语义未变；后端无需新增合帧行为。
- task 切换后不能看到前一个 task 的消息、工具状态、错误或缓存命中结果。

### 6.1.1 真实 Tauri E2E 基线（前置条件）

- 基线必须来自“不含本次 converter 改造”的独立代码快照或 baseline revision；不得直接在混有其它未提交改动的工作树上宣称基线干净。
- 测试必须由 `cargo tauri dev` 或对应 Tauri debug binary 启动，覆盖 Rust 主进程、WebView2、真实 Tauri IPC、BackendSupervisor 动态 backend URL、Assistant Transport 流和退出清理。
- 当前 `npm run test:e2e` 使用 Vite + `tests/e2e/test-server.mjs`，属于 renderer-only 参考测试；它不能满足本节的产品 E2E 基线。
- 基线运行必须使用隔离的测试 app data/runtime 目录和可复位的测试后端，禁止复用开发中的 SQLite、backend 进程、端口或旧测试产物；测试结束后必须确认 Tauri、backend 和测试服务进程树均已退出。
- 必须保存具体构建 revision、启动命令、WebView2/Windows 版本、测试用例结果、失败原因和诊断 artifact。当前工作树已有失败只能作为参考，不能直接充当改造前基线。
- Windows 当前缺少 `tauri-driver`/`msedgedriver`，仓库也没有 WebdriverIO Tauri harness；在补齐桌面 WebDriver 入口前，真实 Tauri E2E 验收状态为未建立，不得以 Playwright 浏览器结果替代。

### 6.2 引用稳定性与缓存正确性（硬门槛）

使用固定包含 `N` 条 canonical 历史消息的 fixture，并对最后一条 assistant 消息连续执行增量 append：

- 初次建立允许调用 `N` 次 canonical 消息转换；之后每次只改变当前消息时，历史未变消息的转换回调次数为 `0`，当前受影响消息至多为 `1` 次。
- 新增一条 assistant 消息时，新消息和因 `isLastRunMessage: true → false` 受影响的前一条 assistant 消息各至多重新转换一次；不能把整个历史 Run 全量重建。
- 所有未变历史 `ThreadMessage` 对象引用保持稳定；其未变 parts 和 tool parts 引用也保持稳定。
- Run status/endReason 改变时，只允许重新计算受该 Run 状态影响的消息；不能因为一个 Run 完成而全量重建所有历史消息。
- tool 状态从 pending 到 completed/failed/cancelled 的每个阶段，目标 tool part 的展示结果和 status 与参考转换器一致。
- pending command 增加、变化、清除，以及 `isRunning` 改变时，临时 user message、顶层运行态和 canonical 消息的失效范围分别符合第 4.4 节。
- snapshot error 出现、变化、清除时，临时 error message 与参考转换器一致；不影响的 canonical 消息不重新转换。
- `context_usage`、不影响消息展示的 snapshot 更新不得导致 canonical 消息转换回调增加。
- attach、reconnect、root `set`、edit 和 branch 场景不依赖旧引用；必要时允许新 session 全量转换，但不能返回旧 task 的缓存对象。

### 6.3 性能门槛

在同一 Tauri debug/release 构建模式、同一 Windows/WebView2 环境、同一 fixture 下记录改造前基线，并比较 `N = 200` 和 `N = 1000` 两组。性能采集必须发生在真实 Tauri 窗口的 WebView2 页面内；renderer-only Vite 页面不能作为最终证据：

- 在只包含 append-only 当前消息更新、排除终态 status、新增 assistant、pending/error 变化的测试中，连续 100 次更新的 canonical 消息转换回调总数必须满足 `callbackCount ≤ N + 100`；另行计数 pending/error 临时消息的命中与失效。
- 预热 1 次后固定运行 5 次，统一报告 converter 总耗时、单次更新耗时 P50/P95 和 Thread 最长 React commit 的 median/P95；不再根据未定义的“噪声超过 10%”临时决定是否重跑。
- 新方案的 converter 总耗时和 Thread 最长 React commit 的 median 不得较基线回退超过 10%；P95 只作诊断记录，不作为本版本硬门槛，但出现明显回退时必须记录为后续性能调查项。
- WebView2 长任务、滚动跟随、发送/停止、工具结果展示不能出现由本改造引入的可见回归。
- 测试或诊断输出不得包含完整 prompt、模型正文、工具原始结果、密钥或 Token。

### 6.4 代码整洁度门槛

- 缓存算法只出现在 `apps/desktop/lib/assistant/transport-view-converter.ts`；React hook 不包含 `WeakMap`、深比较或消息遍历细节。
- 纯映射仍在 `apps/desktop/lib/assistant/converter.ts`，不反向依赖 React runtime、HTTP client 或组件。
- assistant-ui 的 `unstable_` API 只在 adapter 边界出现，不能扩散到业务层。
- 若运行时确实依赖 `unstable_` API，`@assistant-ui/react` 必须锁定精确版本并以 lockfile 为验证依据；第一版采用本地 WeakMap 主路径时不新增该 API 依赖。
- 不新增后端服务、数据库字段、Transport wire 字段、协议版本或 npm 依赖。
- 为新增失效规则补充注释/docstring 和单测；不要用“性能优化”理由删除现有错误处理或生命周期日志。
- TypeScript 类型检查、lint、单元测试和构建通过；真实 Tauri E2E 相对改造前基线不新增失败，既有失败必须单独记录且不计入本次回归；renderer-only E2E 仅作为辅助诊断；无临时 debug 输出和未使用的兼容函数。

## 7. 推荐测试落点

建议新增或调整：

- `apps/desktop/lib/assistant/transport-view-converter.test.ts`
  - factory 生命周期、命中/未命中、对象引用、Run 状态失效、task 隔离。
- `apps/desktop/lib/assistant/converter.test.ts`
  - 继续覆盖纯映射和 golden output，不承载缓存实现细节。
- `apps/desktop/components/assistant/runtime/use-runtime-transport.test.tsx`
  - 验证 runtime options 收到稳定 converter，且 task/session 重建时边界正确。
- 现有 backend assistant transport tests
  - 只做回归验证，不修改为合帧测试；重点确认本版本没有触碰后端 commit/publish 顺序和 attach 语义。

如果项目现有测试脚本名称与上述文件不同，以 package.json 和当前测试目录事实为准；不得为满足文档命名而搬动无关测试。

## 8. 风险与回滚

### 风险

- `unstable_createMessageConverter` 及其 React hook 属于 assistant-ui 实验性 API，升级时可能变更；当前普通 converter 边界不能直接复用其 hook 缓存。
- 官方 helper 的默认 message join 可能改变当前逐条 Transport message 语义。
- 当前 message status 依赖 Run 上下文，不完整的 cache key 可能造成“正文正确但状态过期”。
- 自定义 artifact/tool grouping 可能受 assistant-ui 默认 message join 行为影响。
- 稳定消息引用减少后，如果仍有组件自行按深层对象重算，收益可能低于预期。

### 控制措施

- adapter 隔离官方实验性 API。
- 用 `TransportMessageInput` 显式携带 status 相关签名，并为 pending/error/`isRunning` 建立独立失效规则。
- 以项目 adapter-local `WeakMap` 为主路径，降低对 assistant-ui unstable hook 和默认 join 行为的运行时依赖。
- golden tests 对比旧参考输出，重点覆盖工具和错误生命周期。
- 先测 adapter 总耗时、converter callback/commit，再决定是否需要下一版的 content-visibility 或虚拟化。

### 回滚

回滚只需要恢复 `useRuntimeTransport` 的 converter 装配和删除新 adapter 文件；不涉及数据库迁移、后端进程、Transport 协议或用户数据。回滚后必须重新运行第 6 节的功能和回归测试，确认旧 converter 仍可用。

## 9. 官方文档核对依据

- [Assistant UI Message Conversion](https://www.assistant-ui.com/docs/api-reference/external-store/message-conversion)：确认 `unstable_createMessageConverter` 同时提供 `useThreadMessages` 和命令式 `toThreadMessages`；前者才是 React hook 缓存路径，后者不能被描述为自动缓存。
- [Assistant UI Assistant Transport](https://www.assistant-ui.com/docs/runtimes/custom/assistant-transport)：Transport state、converter 边界和 `useAssistantTransportRuntime`。
- [Assistant UI Thread](https://www.assistant-ui.com/docs/primitives/thread)：Viewport、Messages iterator、消息渲染边界。
- [Assistant UI Virtualization](https://www.assistant-ui.com/docs/guides/virtualization)：只有确认 React mount/update 是瓶颈时才进入虚拟化；不是第一版范围。
- [Assistant UI Message Part Grouping](https://www.assistant-ui.com/docs/guides/part-grouping)：当前自定义 tool/artifact 分组需保持，不直接替换。

本方案以当前仓库实际安装版本和源码行为为准；官方文档中的 `unstable_` API 必须通过 adapter 和测试隔离，任何 helper 试验都必须显式验证 `joinStrategy: "none"` 或采用项目本地缓存实现。

## 10. 子 Agent 验收清单

子 Agent 只读审查本方案与当前仓库，不修改代码，必须回答：

1. 方案描述的文件、版本、调用链和结构共享行为是否与当前代码一致。
2. `unstable_createMessageConverter` 的使用方式、限制和文档链接是否符合官方 Assistant UI 文档。
3. `TransportMessageInput` 是否覆盖当前 message status、tool lifecycle、error、pending 和 reconnect 的失效因素。
4. 验收条件是否可执行、可测量，是否存在无法证明或互相矛盾的指标。
5. 是否违反“Transport subscriber-local 合帧暂时不做”、单机本地进程边界、数据所有权或代码职责边界。
6. 是否有应当阻塞实现的事实错误；若有，给出文件和行号及最小修订建议。

验收结论必须分为 `通过`、`有条件通过` 或 `不通过`，并列出证据和未决风险。

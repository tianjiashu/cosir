# SSE 连接管理层重构计划（消除双 Connection 重复）

> **文档性质**：改造方案（设计阶段，尚未实施）
> **关联报告**：`docs/frontend-opensource-replacement-report.md` 第 2.2 节
> **状态**：待独立审查 Agent 基于代码事实 + 官方/GitHub 资料复核
> **日期**：2026-08-17

---

## 0. 背景与决策依据

`frontend-opensource-replacement-report.md` 第 2.2 节建议用 `@microsoft/fetch-event-source`
替换 `SSEConnection` 与 `DelegationStreamConnection` 两份手写连接管理。本计划**不采纳该具体库**，
理由（已与用户确认，并经独立审查 Agent 用 GitHub API / npm 核实）：

1. **（主论据，基于代码语义，独立成立）内置自动重连 + Last-Event-ID 与本项目的「断开=结束」语义冲突。
   本项目 SSE 模型为「run 与 SSE 绑定，断开即标 `client_disconnected` 失败」（见工作记忆「Turn/SSE 编排」），
   **刻意不造重连协议**，`@microsoft/fetch-event-source` 默认带重连重试逻辑，会打乱这个有意的断连兜底。**
2. **（辅助论据，事实已核实）该库维护停滞**：经 GitHub API 核实，`@microsoft/fetch-event-source`
   `archived` 字段为 `false`（**未归档**），最后 release 为 **v2.0.1（2021-04-25）**（非报告所称 2022 年，
   且 README 无「no longer maintained」声明）；`pushed_at` 为 2026-02 仍有零星提交但近 4 年无新发版。
   引入一个近 4 年无 release、无官方维护承诺的库替换可工作代码，违反「引入依赖必须评估长期可维护性」底线
   （第零铁律）。——注意：此条为辅助论据，拒绝该库的决定不依赖 archived 状态，主论据是第 1 条的语义冲突。
3. **真正的重复在「连接管理」层，不在「协议解析」层**。协议已由 `eventsource-parser@4.0.0`
   承担（已完成改造），剩下的 `fetch + ReadableStream + AbortController + 终态检测 + 异常上报`
   两份高度相似，应通过**抽取项目内部共享基类**消除，属 Rule of Three 触发后的标准动作，
   无需引入外部连接管理库。

**结论**：用项目内部的 `SSEConnectionBase` 抽象类统一两份 Connection，协议层保留 `eventsource-parser`，
连接管理层零新增依赖。

---

## 1. 现状事实（基于代码，非推测）

### 1.1 `SSEConnection`（`apps/desktop/src/services/sse.ts:96`）

职责与私有状态：
- `_state: SSEConnectionState`（IDLE/CONNECTING/STREAMING/CLOSED 四态机）
- `_abortController`：`disconnect()` 只 `abort()` 不置 null，置 null 由 `connect()` 的 `finally` 统一回收
- `_terminalReceived`：终态标记
- `_aborted`：主动取消标记（区分「主动取消」与「后端崩溃」）
- `_seenEventIds: Set<string>`：单流内 event_id 去重，重复则 `logWarn("sse_stream_dup_event")`
- 公开 `state` getter、`onStateChange` 回调、`_setState` 去重通知

连接读取流程（`connect()`）：
1. 重置终态/取消/去重状态（支持实例复用）
2. `buildTraceHeaders` + `onTrace` + `useConversationTraceStore.recordTrace`
3. `fetch` + `Accept: text/event-stream` + trace headers
4. `recordBackendTrace(readBackendTraceHeaders(...))`
5. `reader` 循环 + `frameParser.feed(decoder.decode(value, {stream:true}))`
6. EOF：`frameParser.feed("\n\n")` + `frameParser.reset()`（注：eventsource-parser@4.0.0 官方 README 推荐 EOF 用 `reset({consume:true})`，但本项目采用 `feed("\n\n")+reset()` 是有意沿用既有代码、非官方推荐；`consume:true` 会在残留非合法行时触发 `onError` 改变既有语义，本轮不作为重构对象，实施时不得「修正」为官方写法）
7. `_reportStreamEndIfAbnormal` + `_setState(CLOSED)`
8. `catch`：AbortError → CLOSED；其他 → logError + onError + throw
9. `finally`：`_abortController = null`

`parseSSEEvent`：`JSON.parse(frame.data)`，失败 `logWarn`（含 task_id/turn_id/event_type/data_preview/error）。
`_markTerminalIfNeeded`：终态 = `run_finished | final_response | run_failed | run_cancelled`（4 类）。

### 1.2 `DelegationStreamConnection`（`apps/desktop/src/services/delegationStream.ts:76`）

职责与私有状态：
- `abortController`：`disconnect()` 直接 `abort()` 且 `connect()` 的 `finally` 置 null
- `terminalReceived`：终态标记（仅 3 类，见下）
- `aborted`：主动取消标记
- `connecting`：布尔（替代状态机，无 CONNECTING/STREAMING 细分，无 `onStateChange` 回调）

连接读取流程（`connect()` → `readEvents(body)`）：
1. `connecting` 防重入（布尔，非状态机）
2. `buildTraceHeaders` + `useConversationTraceStore.recordTrace`（**无 `onTrace` 暴露**）
3. `fetch` + `Accept: text/event-stream`
4. `recordBackendTrace(readBackendTraceHeaders(...))`
5. `readEvents(body)`：`reader` 循环 + `frameParser.feed(...)`，EOF `feed("\n\n")` + `reset()`（同 §1.1 注：非官方推荐写法，有意沿用，本轮不重构）
6. `reportAbnormalEndIfNeeded` + `logInfo`
7. `catch`：AbortError → return（不置状态、不抛）；其他 → logError + onError + throw
8. `finally`：`connecting = false` + `abortController = null`

`parseRuntimeEvent`：`JSON.parse(frame.data)`，失败 `logWarn`（含 task_id/delegation_id/child_turn_id/event_type/data_preview/error）。
`isChildTerminalEvent`：终态 = `run_finished | run_failed | run_cancelled`（**3 类，缺 `final_response`**）。

### 1.3 两份实现的真实重复与差异（提取基类前必须对齐）

| 维度 | SSEConnection | DelegationStreamConnection | 基类统一策略 |
|---|---|---|---|
| fetch + headers | 相同 | 相同 | 基类模板方法 |
| trace headers + recordTrace | 相同（含 onTrace） | 相同（无 onTrace） | 基类提供，onTrace 可选 |
| backend trace 记录 | 相同 | 相同 | 基类模板方法 |
| ReadableStream 读取循环 | 相同 | 抽成 `readEvents(body)` | 基类统一 `readStream` |
| frameParser.feed + EOF flush | 相同 | 相同 | 基类统一 |
| 终态检测 | 4 类 | 3 类（缺 final_response） | **统一为 4 类**（修复差异，见 §4 风险） |
| 异常结束上报 | `_reportStreamEndIfAbnormal` | `reportAbnormalEndIfNeeded` | 基类统一（策略钩子） |
| 状态机 | 4 态 + onStateChange | 布尔 connecting，无回调 | **保持差异**（见 §3） |
| event_id 去重 | 有 | 无 | **保持差异**（见 §3，子类可选覆盖） |
| 断连兜底 catch 分支 | AbortError→CLOSED+throw 外部 | AbortError→return 不抛 | **保持差异**（行为语义不同，见 §4） |

---

## 2. 目标设计

### 2.1 新增文件 `apps/desktop/src/services/sseConnectionBase.ts`

单一职责：**承载「SSE 连接生命周期」的通用骨架**——fetch、trace 注入、backend trace 记录、
ReadableStream 读取循环、帧解析器接入、EOF flush、终态/异常结束判定钩子。
不承载任何具体业务帧语义（JSON.parse 后的 RuntimeEvent 处理由各子类负责）。

```ts
/** 终态事件类型（两子类统一为 4 类）。 */
const TERMINAL_EVENT_TYPES = ["run_finished", "final_response", "run_failed", "run_cancelled"] as const;

/** 通用连接选项（基类关心的部分）。 */
export interface SSEBaseConnectionOptions {
  taskId: string;
  onError?: (error: Error) => void;
  onTrace?: (traceId: string) => void;
  buildPath: () => string;          // 子类提供具体路径
  buildLogContext: () => Record<string, unknown>;  // 子类提供日志上下文（差异化字段）
  recordEvent: (event: RuntimeEvent) => void;       // 子类消费解析后的事件
  isTerminal: (event: RuntimeEvent) => boolean;     // 子类可覆盖终态判定
  onAbnormalEnd?: (context: Record<string, unknown>) => void;  // 子类可定制异常结束上报
}

export abstract class SSEConnectionBase {
  protected abortController: AbortController | null = null;
  protected terminalReceived = false;
  protected aborted = false;

  /** 模板方法：统一连接骨架，子类通过钩子差异化。 */
  protected async runConnection(path: string, context: Record<string, unknown>): Promise<void> { /* fetch + read loop + EOF flush */ }

  /** 帧解析器回调：基类统一处理 decode/feed/flush，子类只实现 recordEvent。 */
  protected abstract handleFrame(frame: ParsedSSEFrame): void;

  /** 主动断开：统一 abort + 标记，置 null 由 runConnection 的 finally 回收。 */
  disconnect(): void { this.aborted = true; this.abortController?.abort(); }

  /** 终态判定默认 4 类，子类可覆盖。 */
  protected isTerminalEvent(event: RuntimeEvent): boolean {
    return TERMINAL_EVENT_TYPES.includes(event.event_type);
  }
}
```

### 2.2 `SSEConnection` 改造为继承 `SSEConnectionBase`

- **字段复用规则（避免重复定义）**：终态/取消标记直接复用基类 `protected terminalReceived` /
  `aborted` / `abortController`，**不另起私有字段**。`_reportStreamEndIfAbnormal` 改为读
  `this.terminalReceived || this.aborted`（基类字段）。仅 `_state` 状态机 + `onStateChange` 回调 +
  `_seenEventIds` 去重作为 `SSEConnection` 自身扩展保留（这两项 **delegation 不需要**）。
- `connect()` 改为调用 `super.runConnection(path, context)` 并传入钩子；
  或在基类模板内通过 `protected abstract buildConnectionContext()` 等钩子取差异化字段。
- `parseSSEEvent` + `_markTerminalIfNeeded` + `_reportStreamEndIfAbnormal` + `_setState` 保留在子类
  （终态判定沿用 4 类；异常结束上报走基类 `onAbnormalEnd` 钩子，子类提供具体 logError 文案）。

### 2.3 `DelegationStreamConnection` 改造为继承 `SSEConnectionBase`

- 删除 `abortController`/`terminalReceived`/`aborted`/`connecting` 中通用部分。
- `connecting` 布尔防重入 **保留为子类私有**（基类不提供状态机）。
- `readEvents(body)` 合并进基类 `runConnection`（不再单独持有 body 读取方法）。
- `parseRuntimeEvent` + `handleFrame` + `reportAbnormalEndIfNeeded` 保留在子类。
- **终态判定统一改为 4 类**（移除 `isChildTerminalEvent` 仅 3 类的差异，或让 `isChildTerminalEvent`
  调用 `super.isTerminalEvent` 以继承 `final_response`）——见 §4 风险点 R1。

---

## 3. 刻意保留的差异（不强行统一）

为避免「为统一而统一」破坏语义，以下差异**在基类中留钩子、不抹平**：

1. **状态机 vs 布尔**：`SSEConnection` 有 4 态 + `onStateChange`（父 turn 主 SSE，UI 需感知连接态）；
   `DelegationStreamConnection` 只 `connecting` 布尔（child 流不写全局连接态，避免污染父 turn）。
   → 基类不定义状态机，`SSEConnection` 自行维护 `_state`。
2. **event_id 去重**：仅 `SSEConnection` 做单流内去重（父 turn 主事件流重复推送需告警）；
   child 流暂未做。→ 基类不提供去重，`SSEConnection` 在 `recordEvent` 钩子里自行实现。
3. **断连 catch 语义**：`SSEConnection` 的 AbortError 置 CLOSED 并让外部感知；
   `DelegationStreamConnection` 的 AbortError 静默 return（child 断开不影响父）。
   → 基类 `runConnection` 的 catch 调用 `protected abstract onAbort()` 钩子，两子类各自实现。

---

## 4. 风险与待确认点

- **R1（终态差异，已核实结论：保留 3 类，不统一）**：`DelegationStreamConnection` 终态判定
  为 3 类（不含 `final_response`），**这是有意的设计差异，不是缺失，实施时保留**。
  核实过程与结论：
  - 后端 `child_agent_runner.py::_consume_child_events` 确认 child turn 的 runtime 事件流**会下发**
    `final_response`（EventType.FINAL_RESPONSE 被消费）——后端确实下发该事件。
  - 但前端消费语义不同：`final_response` 对**父 turn 主 SSE 流**是终态（父 run 结束），
    对 **child 订阅流不是**——child run 在 `final_response`（最终回复信号）之后仍会继续发
    `run_finished`（运行结束信号）。若把 child 终态统一为 4 类（含 `final_response`），
    会导致 child 流在 `final_response` 过早误判终态、漏收其后的 `run_finished`。
  - 权威测试契约：`apps/desktop/src/tests/useDelegationStreams.test.tsx:284`
    `keeps child stream open when final_response arrives before run_finished` 断言 child 流收到
    `final_response` 时**保持打开**等 `run_finished`。该用例是现行契约，统一 4 类会破坏它。
  - **结论**：基类 `isTerminalEvent` 默认 4 类（父流 `SSEConnection` 使用）；
    `DelegationStreamConnection` 覆盖为 3 类（不含 `final_response`），保留差异。
    这与本方案 §3「不强行统一、保留差异」一致。方案初版「统一 4 类是修复缺失」的判断有误，
    以本 R1 核实结论为准（实施记录已确认）。
- **R2（钩子耦合）**：基类模板方法 + 多个 `protected abstract` 钩子，若钩子过多会导致
  「基类比子类还难读」。约束：钩子数 ≤ 5，超出则退回「各子类保留独立 connect、仅抽
  `readStreamLoop` 纯函数」的更轻方案。
- **R3（回归面）**：共用回归测试 `sse.lifecycle.test.ts` / `useSSE.*` /
  `deriveDelegationStreams.test.ts` / `useDelegationStreams.*` 必须全绿；
  行为语义（终态标记、异常结束上报、去重告警）不得改变。
- **R4（不引入外部库）**：本计划零新增 npm 依赖，与第零铁律「敢引依赖但不退化成什么都引」一致。
  不采纳 `@microsoft/fetch-event-source`（死库 + 重连语义冲突）。

---

## 5. 实施步骤（TDD，开发-审查-测试闭环）

1. **开发子 Agent（加载 test-driven-development skill）**：
   - 先写/扩 `SSEConnectionBase` 的单元测试（mock fetch + ReadableStream，验证
     fetch/headers/trace/EOF flush/异常结束判定通用路径）。
   - 新建 `sseConnectionBase.ts`，把通用骨架从两子类上提。
   - 改 `sse.ts` / `delegationStream.ts` 继承基类，保留 §3 差异钩子。
   - 确保 `sseParser.test.ts` 等既有测试不受影响。
2. **独立审查 Agent**：基于代码事实 + 本项目编码规范复核：
   - 重复是否真正消除（协议解析无残留手写）；
   - §3 差异是否保留（不抹平语义）；
   - R1 终态差异是否正确处理；
   - docstring 四段式同步；零新增依赖。
3. **独立测试 Agent**：
   - 跑全量前端测试确认无回归；
   - 补充基类边界测试（跨分片 feed、异常结束、AbortError 两类分支）；
   - 确认 `sseConnectionBase.ts` 覆盖率 > 80%。
4. 闭环反复直到审查 + 测试均「通过」。

---

## 6. 验收标准

- [ ] `sse.ts` 与 `delegationStream.ts` 的 `fetch/ReadableStream/AbortController/EOF flush`
      通用代码只存在于 `sseConnectionBase.ts` 一处（无复制）。
- [ ] 协议解析仍 100% 走 `eventsource-parser`，无 `split("\n\n")` 残留。
- [ ] `SSEConnection` 的 4 态机、`onStateChange`、`_seenEventIds` 去重保留且行为不变。
- [ ] `DelegationStreamConnection` 的「不污染父 turn 状态」「AbortError 静默 return」保留。
- [ ] 零新增 npm 依赖（package.json 不变）。
- [ ] 全量前端测试 + 既有 SSE/delegation 回归测试全绿。
- [ ] R1 终态差异已核实并处理（child 流终态为 3 类、不含 `final_response`；基类默认 4 类由父流使用；见 §4 R1 核实结论）。

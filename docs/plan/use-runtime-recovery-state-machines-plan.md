# `useRuntimeRecovery` 状态机拆分技术方案

**状态：** 方案设计，前端结构已部分落地，剩余高风险项见第 10 节  
**日期：** 2026-09-19  
**范围：** `apps/desktop/components/assistant/runtime/` 及其快照读取复用；不改后端协议、不改数据库、不改业务行为。

## 1. 结论

原始实现中的 `useRuntimeRecovery` 把三个生命周期不同、失败语义不同的职责放在一个 Hook 中：

1. 后端实例变化与 Assistant Transport 断流后的快照对齐、有限重连；
2. 取消请求后的 Run 终态确认轮询；
3. 用户点击“继续运行”时的业务续跑。

它们各自拥有独立的计数器、AbortController、定时器、generation 和完成回调，却共享一个 `RuntimeRecovery` 对外类型。这会带来以下维护风险：

- `resetRecovery` 只重置重连状态，但从名称看容易被误用为“重置全部恢复流程”；
- 取消确认与断流恢复的互斥关系依赖 Hook 内部的隐式判断，后续改动容易出现重复恢复或错误取消；
- 业务续跑是一次有明确业务语义的命令，却和 transport 恢复共用同一个“recovery”抽象；
- 相同的 `/tasks/{taskId}/assistant/state` 请求、超时、解析和过期响应防护分散在多个文件中；
- Hook 返回值不断膨胀，调用方必须知道不相关状态机的接口。

建议拆成“一个 Hook 一个状态机”，并将快照读取提炼为无状态的共享边界。第一阶段只做行为保持的结构重构，不改变重试次数、时序、后端接口或状态判定。

本方案不是简单的 Hook 文件移动。业务续跑是“业务启动 → transport attach”的两阶段操作，必须保留已接受业务续跑后的 attach-only 重试语义；transport recovery 与 cancellation confirmation 的 timer 回调必须在触发时重新校验 generation、taskId、runId 和取消状态。

## 2. 已核对的代码事实

### 2.1 原问题与当前实现状态

原始版本的 `apps/desktop/components/assistant/runtime/use-runtime-recovery.ts` 同时包含：

- `reconcileAfterTransportFinish`：读取 canonical snapshot；active Run 继续 attach；terminal Run 导入 snapshot；有限重试；
- `confirmCancellation` / `reconcileCancellation` / `cancellationSettled`：独立的取消确认轮询、总超时和回调；
- `resumeBusinessRun`：向 `/assistant` POST 空 `commands`，等待业务续跑请求被接受后再调用 runtime resume；
- `resetRecovery`：只重置 transport 重连 generation、计数器、定时器和请求。

当前工作树已经完成第一轮拆分：`useRuntimeRecovery` 现在主要负责 transport recovery；取消确认已移到 `useCancellationConfirmation`，业务续跑已移到 `useBusinessResume`。但文件名暂时保留为 `useRuntimeRecovery`，后续可以在不改变职责的前提下决定是否重命名为 `useTransportRecovery`。

当前实现已把三套状态机拆开；剩余问题主要是 Hook 级专门测试和 attach-only E2E 执行证据尚未补齐。业务续跑的 attach-only 语义已经由 `useBusinessResume` 的 accepted-run 记录与 transport 的 bounded recovery 共同保护。

### 2.2 后端协议语义不能合并

后端实际语义如下：

- `POST /assistant`，空 `commands` 且带 `runId`：业务续跑，会经过 `prepare_run_start` 和 executor，属于会改变业务执行状态的命令；
- `POST /tasks/{task_id}/assistant/attach`：只订阅已有 Run，不携带业务命令，不触发业务续跑；
- `GET /tasks/{task_id}/assistant/state`：读取后端 canonical Transport snapshot。

因此：

- Assistant UI 的 `resumeApi` 只能承担既有 Run 的 attach/resume stream；
- “继续运行”按钮的业务续跑不能被无条件替换成 `runtime.thread.resumeRun()`；
- 也不能把业务续跑的 `/assistant` 请求和 transport attach 请求抽象成一个通用“恢复请求”。

### 2.3 当前 runtime 装配与 assistant-ui 约束

`use-runtime-transport.ts` 已配置：

- `initialState`；
- `api: /assistant`；
- `resumeApi: /tasks/{taskId}/assistant/attach`；
- `protocol: "assistant-transport"`；
- `RuntimeControlBridge` 通过 `thread.resumeRun()` 和 `thread.importExternalState()` 暴露控制。
- `BackendRuntimeSnapshot.available` 现在作为本机后端可用性闸门；后端从不可用恢复时，即使 generation 没有变化，也会触发一次 canonical snapshot 对齐。
- `lib/async/abort-timeout.ts` 已统一请求超时、AbortController 和 timer 清理样板；后续拆分必须复用它，不得在新 Hook 中重新封装同类 timeout helper。

官方 assistant-ui 文档说明：

- Assistant Transport 是 state-streaming transport，服务端 snapshot 是外部状态输入，UI 不应自行成为事实源；
- `resumeRun()` 应通过配置的 `resumeApi` 重新订阅 active stream，不能把它当作业务续跑；
- `importExternalState()` 用于把外部 canonical state 导入 runtime；
- `initialState` 用于 runtime 初始化，任务切换或需要重新装载首屏状态时应保持现有 remount 边界；
- 断流本身不等于后端 Run 取消，应先同步状态，再决定 attach、导入终态或提示用户。

参考：

- [Assistant Transport runtime](https://www.assistant-ui.com/docs/runtimes/custom/assistant-transport)
- [Connection state](https://www.assistant-ui.com/elements/connection-state)
- [AssistantRuntimeProvider](https://www.assistant-ui.com/docs/api-reference/context-providers/assistant-runtime-provider)

## 3. 目标与非目标

### 目标

- 每个独立状态机只拥有自己的 refs、计时器、AbortController、generation 和清理逻辑；
- 保留后端 canonical snapshot 唯一事实源，不在前端制造 Run 终态；
- 保留现有有限重试、总超时、过期响应丢弃和 StrictMode 清理行为；
- 复用快照读取、HTTP 错误处理和 schema 解析逻辑；
- 让 `AssistantRuntimeSession` 只负责组合窄接口；
- 使后续修改某一状态机时不需要理解其它状态机的全部内部状态。

### 非目标

- 不修改后端 API、Assistant Transport wire schema、Run 状态机或数据库；
- 不新增 `resumeStateApi`，除非后端明确提供“保留起始状态”的独立协议；
- 不把取消确认、断流恢复、业务续跑合成一个新的通用 manager；
- 不顺手重写 `useRuntimeTransport`、Workbench readonly runtime 或整个 Assistant runtime；
- 不改变当前重试参数和用户可见提示。

## 4. 目标结构

### 4.1 共享的快照读取边界

已落地一个无 React 状态的窄模块：

`apps/desktop/lib/assistant/assistant-snapshot-client.ts`

职责仅为：

```ts
requestAssistantSnapshot(taskId, { signal, traceId }): Promise<TransportState>
```

内部统一调用现有 `requestJson` 和 `parseTransportState`。它不拥有计时器、重试、React state、runtime import 或业务决策。

共享 helper 不负责 timeout、retry、expected-run 校验、task/generation 校验、错误展示、snapshot import 或 attach 决策。调用方必须在 `await` 前后保留自己的生命周期和业务语义。

以下调用方可复用它：

- transport 断流/后端重启后的对齐；
- 取消确认轮询；
- transport `onError` 的快照补偿；
- terminal snapshot compensation；
- 手动 retry 的首屏快照读取。

超时 AbortController 暂时仍由各状态机拥有，避免共享 helper 反过来隐藏取消责任。只有在测试证明超时策略完全一致后，才考虑进一步抽取一个明确的 `withTimeout` 边界。

### 4.2 `useTransportRecovery`

方案目标是保留一个只负责 transport 的 Hook。当前实现仍使用原文件名：

`apps/desktop/components/assistant/runtime/use-runtime-recovery.ts`

只保留：

- backend runtime generation 变化后的 canonical 对齐；
- backend availability 从 `false` 恢复到 `true` 后的 canonical 对齐；availability 只表示控制面当前可用，不是 Run 的业务状态，也不能被投影为终态；
- transport finish 后的 snapshot 读取；
- active Run 的有限 attach/resume；
- terminal snapshot 的 `importExternalState`；
- transport recovery 自己的 reset、计数器、timer、AbortController 和 cleanup。

建议公开：

```ts
type TransportRecovery = {
  reconcileAfterTransportFinish(): Promise<void>;
  resetTransportRecovery(): void;
};
```

它可以依赖一个窄的 `isCancellationPending` 或 `shouldDeferRecovery` 判断，但不能直接读取或修改取消确认的内部 refs，也不能拥有取消状态。

### 4.3 `useCancellationConfirmation`

从当前 Hook 提取只负责取消确认的 Hook，例如：

`apps/desktop/components/assistant/runtime/use-cancellation-confirmation.ts`

只拥有：

- 被确认的 `runId`；
- 取消确认轮询次数和总 deadline；
- 取消确认 timer、AbortController、generation；
- 轮询成功、终态、超时和未确认回调。

它不负责：

- transport 断流恢复；
- 业务续跑；
- 直接决定 UI 的 `cancellingRunId`；
- 创建第二套 Run 状态机。

当前实现把“取消确认”和“终态交接”放在同一个小 Hook 中：轮询读取的 snapshot 写入 `latestStateRef`，终态判断来自该 snapshot，并负责 `importExternalState`、清理取消 pending 状态以及处理 newer active Run。该边界有意保持紧凑，避免为了拆分而增加另一层只转发回调的 Hook；后续修改必须继续保证它不拥有普通 transport recovery 或业务续跑状态。

### 4.4 业务续跑保持为独立命令

`resumeBusinessRun` 不应继续作为 `RuntimeRecovery` 的成员。它应成为 session 层传给 Thread 的独立命令回调，或位于独立的 `useBusinessRunResume` 小 Hook 中，但不应和 transport recovery 共享 reset、attempt 或 abort 状态。

当前已提取为 `useBusinessResume`，并拥有独立的 in-flight、generation、AbortController、`business-starting → attach-pending` 阶段以及业务请求结果不确定时的 snapshot 复核路径。由于 assistant-ui 的 `resumeRun()` 返回 `void`，attach-only 失败由 `useRuntimeTransport` 的空命令错误处理转交已有 bounded transport recovery；该路径只重新调用 `resumeApi`，不重新 POST `/assistant`。

行为保持要求：

1. 读取并固定本次操作的 canonical `runId`；
2. 向 `/assistant` 发送空 `commands` 的业务续跑请求，且一次用户操作最多启动一次；
3. `/assistant` 成功后记录 operation generation 和返回的 run 标识，进入 `attach-pending`，随后只调用 `runtimeControlsRef.current?.resume()` 订阅其 stream；
4. attach 失败时只能重试 `resumeApi` attach，不得再次 POST `/assistant`；只有确认业务启动请求尚未被接受时，才允许重新开始业务续跑；
5. 业务续跑拥有独立的 AbortController、in-flight gate、generation 和 cleanup，过期响应不能继续调用 `resume()` 或更新 issue；
6. 使用自己的 timeout 和错误映射。业务启动请求成功后调用命名明确的 `resetTransportRecoveryBudget()`；attach-only 失败重试不重置该预算，不重新启动业务，也不重复创建 Run。该 reset 的调用次数仍应由专门测试固定下来。

在实现前必须保留后端测试所证明的区别：`/assistant` 是业务续跑，`/attach` 是纯订阅。不能因为 assistant-ui 官方建议使用 `resumeRun()`，就把业务续跑请求删除或改成 attach。

### 4.5 调用方组合

`assistant-runtime-session.tsx` 最终只组装窄能力：

```text
TransportRecovery       -> useRuntimeTransport
CancellationConfirmation -> useRuntimeCancellation
BusinessRunResume        -> Thread 的 onResumeBusiness
```

`useRuntimeCancellation` 继续拥有 UI 的 `cancellingRunId` 和 `cancelRequestedRunIdRef`，但只接收取消确认能力及 transport recovery 的必要窄回调，不再依赖包含所有恢复行为的 `RuntimeRecovery`。

## 5. 必须保持的并发与生命周期不变量

1. **独立 reset：** `resetTransportRecoveryBudget()` 不能清除取消确认的 timer、deadline、runId 或 callback；取消确认结束也不能重置 transport 的 retry budget；调用方不得把它扩展成全局恢复 reset。
2. **取消优先：** 取消确认进行中时，transport recovery 的所有入口和已排队 timer 都不得 attach。timer 触发时必须重新检查当前 cancellation pending、generation、taskId 和 expected runId；取消确认完成后，对 newer active Run 最多执行一次受保护的 attach/resume。
3. **canonical only：** 前端不根据请求成功、SSE 断开或按钮状态直接制造 `completed`、`cancelled` 等终态；所有终态来自 snapshot。
4. **过期响应丢弃：** 每个异步操作必须同时检查 `disposed`、所属 generation、所属 `runId`，并确认当前 task 未变化。
5. **单次进行中请求：** 每个状态机内部最多一个 snapshot 请求；timer 触发前清除 timer 引用，finally 只清理属于自己的 controller。
6. **timer 可清理：** cancellation confirmation 的 newer-run attach timer 也属于该状态机，必须保存引用，并在 stop、cleanup、task switch 和 generation 变化时清除。
7. **有界恢复：** 保留现有最大重试次数和总超时；不得通过拆分 Hook 意外重置预算或形成无限轮询。
8. **任务切换安全：** runtime remount/cleanup 后，旧请求不能导入新 task 的 runtime；`initialState` 仍由 runtime mount 边界负责，不在 recovery Hook 内强行改写。
9. **终态补偿隔离：** `useRuntimeTransport` 的 terminal snapshot compensation 仍需保留 expected run 校验，不能被取消确认的轮询逻辑替代。
10. **业务续跑与 attach 隔离：** 对一次用户业务续跑操作，`/assistant` 业务启动最多一次；其后的 `resumeApi` attach 可按 attach-only 策略重试，但不得重新触发业务启动。当前代码通过 `useBusinessResume` 的 accepted-run 记录和 `useRuntimeTransport` 的空命令 bounded recovery 共同满足该不变量。重复 `resumeRun()` 由 assistant-ui single-flight 机制合并，方案不得再制造第二套 attach 调度状态机。
11. **availability 闸门：** backend unavailable 时不得发起新的 attach/resume；availability 恢复时必须重新读取 canonical snapshot。所有已排队 timer 在触发时仍需再次检查 availability，不能只依赖创建 timer 时的判断。

## 6. 实施顺序

### 阶段 0：补充可测试边界（已基本完成）

- 已新增 `assistant-snapshot-client.ts`，统一 HTTP 请求和 schema 解析；
- recovery、transport、取消确认、业务续跑、手动 retry 和 Workbench 已复用该客户端；
- 仍需为 helper 补充成功、非 2xx、非法 snapshot、AbortError 测试。

### 阶段 1：提取 transport recovery（已基本完成）

- `useRuntimeRecovery` 已缩减为 transport recovery，并保留现有文件名；
- backend generation、availability 恢复、断流对齐、重连 timer 和 cleanup 已集中在该 Hook；
- 已保留“取消确认进行中则暂缓 transport recovery”的显式输入；
- `resetTransportRecoveryBudget` 已替代含义过宽的 `resetRecovery`；仍需通过专门测试固定普通新命令、business-started 和 attach-only 三种路径的调用边界。

### 阶段 2：提取 cancellation confirmation（已完成，边界按紧凑方案落地）

- `useCancellationConfirmation` 已拥有 cancellation refs、deadline、轮询、回调、availability 处理和 cleanup；
- newer active Run 的 attach timer 已保存并可清理；
- `useRuntimeCancellation` 只接收取消确认能力和 transport recovery 的窄接口；
- 仍需补充 runId/generation、后端重启和 newer active Run 的专门测试。

### 阶段 3：隔离业务续跑（代码完成，验收补充中）

- `/assistant` 空命令请求已移到 `useBusinessResume`；
- 独立 timeout、in-flight、generation、AbortController 和不确定结果复核已存在；
- 已明确业务请求成功后再调用 transport resume，并区分 `business-started` 与 `attach-only`；
- 空命令 attach 失败会读取 canonical snapshot 并进入 bounded transport recovery，只重试 attach；
- 已增加可控首次 attach 失败的 E2E fixture 和请求计数断言，待 E2E 环境可用时执行。

### 阶段 4：复用快照读取（主要完成）

- `useRuntimeTransport` 的错误补偿、终态补偿、`AssistantRuntime` 手动 retry、初始 state 和 Workbench 已迁移到共享客户端；
- 各调用方仍保留自己的 expected run、timeout、remount 和错误展示策略；
- `RuntimeRecovery` 类型仍保留，但已不再包含取消确认和业务续跑接口；
- 仍需补充状态机专门测试，并在可用 E2E 环境中执行 attach-only 失败路径验证。

## 7. 验证方案

### 单元测试

- transport recovery：active snapshot、terminal snapshot、请求失败重试、达到上限、backend generation 变化、reset 后旧响应丢弃；
- cancellation confirmation：pending/running 继续轮询、terminal 成功、deadline、达到上限、AbortError、旧 run 响应、newer active Run；
- 独立 reset：取消确认进行时 reset transport 不影响取消确认；取消确认结束不重置 transport retry budget；目标行为是 business-started 只调用一次 `resetTransportRecoveryBudget()`，attach-only 不调用；仍需由专门测试固定该边界；
- 两阶段业务续跑：已增加 `/assistant` 成功后首次 `/attach` 失败、后续只重试 attach 的 E2E fixture；仍需在可用 E2E 环境中执行，另需补充双击/卸载级别的专门测试；当前已有任务切换和 generation 变化的取消/丢弃保护；
- cleanup：卸载、任务切换、React StrictMode effect replay 后无残留 timer/request/callback；
- snapshot helper：HTTP 状态、解析失败、超时和取消。

### 集成/E2E

- 断流后只 attach 既有 Run，不创建新业务 Run；
- “继续运行”只走一次 `/assistant` 业务续跑，再由 `resumeApi` attach；
- 取消 accepted 后不把 HTTP/SSE 断开误判为 cancelled，直到 canonical snapshot 终态；
- backend generation 变化后 active Run 能恢复订阅，terminal Run 只导入状态；
- 手动 retry 仍以 remount 重新使用 `initialState`，旧 runtime 不接收新 task 状态。

### 工程检查

- TypeScript `tsc --noEmit`；
- 前端业务目录 ESLint；
- runtime/cancellation/transport 相关 Vitest；
- 如存在 Assistant UI adapter 级测试，确认 `resumeRun()` 使用配置的 `resumeApi`，而业务续跑仍使用 `/assistant`。

## 8. 验收标准

- `useRuntimeRecovery` 不再同时持有三套独立状态机；
- 每个 Hook 的公开 API 只表达自身职责；
- 业务续跑明确建模为 `idle → business-starting(runId) → attach-pending(runId) → attached`，attach-only 重试不会再次 POST `/assistant`；
- 快照请求、解析和 HTTP 边界有单一复用实现；
- 没有新增前端事实源，没有把 runtime state 原样写回后端；
- 所有旧的重试上限、总超时、日志事件、错误提示和 race guard 均有对应实现或测试；
- `/assistant` 业务续跑与 `/attach` transport attach 语义仍可从代码和测试中清晰区分；
- 不引入与本地单用户桌面架构无关的认证、服务、队列或持久化能力。

## 9. 复审结论与剩余高风险点

此前已由 GPT-5.6 Luna 子 Agent 结合当前代码、后端接口和官方 assistant-ui 文档完成复审。结论是拆分方向正确，但以下风险仍需在实现完成前解决：

1. `resumeBusinessRun` 手动 POST `/assistant` 后再调用 `resume()` 是否确实是当前后端要求，是否存在重复启动/重复订阅窗口；
2. assistant-ui `resumeApi`、`resumeRun()`、`importExternalState()` 与当前 `RuntimeControlBridge` 的调用方式是否匹配当前安装版本；
3. 拆分后取消确认与 transport recovery 的互斥是否仍然成立，尤其是 newer active Run 场景；
4. 共享快照 helper 是否会错误地抹平不同调用方的 expected-run 校验、超时或错误展示语义；
5. 是否可以在不引入新抽象层的前提下进一步减少代码，避免把一个大 Hook 变成多个过度封装的小 Hook。

## 10. 当前工作树变更对本方案的影响

本轮前端变更不会推翻拆分方案，但改变了实现前提：

### 已经落地或基本落地的内容

- `BackendStatusBanner` 不再自己轮询和直接管理 Tauri 生命周期，改为订阅 `runtime-config` 的集中状态；原先的前端双重生命周期 owner 风险已明显降低。
- `runtime-config` 增加单飞 refresh、operation epoch、generation/URL 冲突保护以及 `available` 投影。方案中的“后端实例变化后重新对齐”现在必须覆盖两种触发：generation 变化，以及同一 generation 的 unavailable → available 恢复。
- `RuntimeControlBridge` 和 Workbench readonly attach 都增加了 backend availability gate；这属于 transport 控制面保护，不改变后端 canonical Run 状态。
- `createTimeoutAbort` 已被 recovery、transport error/terminal compensation、手动 retry、cancel 请求、取消确认和业务续跑复用。后续重构应直接复用该模块，不能重复实现 timeout 样板。
- `useRuntimeRecovery` 已缩减为 transport recovery；`useCancellationConfirmation` 和 `useBusinessResume` 已分别承载取消确认与业务续跑；`useRuntimeCancellation` 只接收窄接口。
- `assistant-snapshot-client.ts` 已统一 snapshot 请求与解析，并被多个调用方复用。
- recovery、取消确认、RuntimeControlBridge 和 transport compensation 已增加 availability、generation、runId 及 timer 触发时的过期保护。

### 仍然未完全落地的内容

- 业务续跑的 `business-starting → attach-pending` 以及 attach-only bounded recovery 已落地；仍缺少 Hook 级专门测试和可运行的 E2E 验收证据来固定该行为。
- reset 已收敛为 `resetTransportRecoveryBudget`；仍需通过测试证明普通新命令和 business-started 各只重置一次，attach-only 不重置。
- `useCancellationConfirmation` 同时负责取消确认和终态交接；这是当前为精简代码保留的选择，文档和后续实现必须保持该边界一致，不要再引入重复投影逻辑。
- 新增 Hook 的专门单元测试尚未建立；E2E fixture 已增加独立的首次 attach 失败开关，但正常 attach fixture 仍复用了 `handleResume()`，因此必须结合请求次数断言解释其覆盖范围。

### 本轮审查结论

文档的主结论仍有效，当前状态应表述为“结构拆分和核心 attach-only 逻辑已完成，专门 Hook 测试和 E2E 执行仍待补齐”。实现时必须把 availability 恢复纳入 transport recovery 的触发条件，把 `assistant-snapshot-client.ts` 和 `createTimeoutAbort` 视为项目级复用能力，并避免再次把取消确认、业务续跑和 transport recovery 合并回一个 Hook。

本轮验证：前端 Vitest 39 个测试文件、181 个测试通过；`tsc --noEmit` 和相关前端 ESLint 通过；E2E fixture 已补充但本地 E2E 环境启动后无进度，未作为验收通过证据。本轮同时修改了实现代码和文档，未修改后端协议或数据事实源。

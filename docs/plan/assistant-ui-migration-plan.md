# 对话流迁移到 @assistant-ui/react —— 方案

> 状态：**待拍板**（尚未开始编码）。
> 依赖：`@assistant-ui/react@0.15.17`（已安装至 `apps/desktop/node_modules`，**尚未提交** `package.json`，HEAD 中不存在）。
> 关联：Codex 审查 `docs/前端审查.md`；上下文圆环验收 `docs/plan/context-usage-frontend-acceptance.md`。

---

## 〇、为什么写这份方案

用户目标（原文）：**重写对话流**，优先使用 `@assistant-ui/react`，判断准绳是「**后续迭代是否好走，自研是否容易走弯路埋坑**」，明确不考虑改动量。

因此本方案不追求最小改动，只回答一件事：**哪些能力交给库，哪些必须自己守住，边界画在哪。**

---

## 一、关键事实（决定了方案的分叉）

以下均为对已安装包的实际核验结果，非推测。

### 事实 1：有两套 runtime，**契约完全不同**

| | `useExternalStoreRuntime` | `useAssistantTransportRuntime` |
|---|---|---|
| 位置（react 包） | `dist/legacy-runtime/…` | `dist/assistant-transport.d.ts`（顶层） |
| 位置（core 包） | `dist/react/runtimes/useExternalStoreRuntime.js` | `dist/react/runtimes/assistant-transport/…` |
| `@deprecated` 标记 | **无** | 无 |
| 状态来源 | 外部传入 `messages: readonly T[]` | 外部传入 `initialState: T`，库自己 fetch |
| 发送入口 | `onNew(message: AppendMessage)` | `api: string`（**必填**）+ 命令队列 |
| 流解码 | 外部自己做 | **库内置**（`DataStreamDecoder` / `AssistantTransportDecoder`） |
| 错误回滚 | 外部自己做 | `onError(err, { commands, updateState })` **内置** |
| 断线恢复 | 外部自己做 | `resumeApi` / `resumeStateApi` **内置** |
| 后端改造 | **零** | **必须新增协议端点** |

> 注意：「legacy」是 `@assistant-ui/react` 的**内部目录名**，不是 API 级废弃标记——`core` 包中两者平级导出，均无 `@deprecated`。但它确实传递了方向信号：assistant-transport 是新架构。

### 事实 2：`AssistantTransportOptions.api` 是**必填**的

```74:89:apps/desktop/node_modules/@assistant-ui/core/dist/react/runtimes/assistant-transport/types.d.ts
type AssistantTransportOptions<T> = {
  initialState: T;
  api: string;
  resumeApi?: string;
  /** Endpoint that returns the retained initial state and run ID for a resume stream. A 204 response means no run is active and the resume is skipped. */
  resumeStateApi?: string;
  protocol?: AssistantTransportProtocol;
  strict?: boolean;
  converter: AssistantTransportStateConverter<T>;
  headers: HeadersValue | (() => Promise<HeadersValue>);
  body?: object | (() => Promise<object | undefined>);
```

`api: string` 无 `?`。走 assistant-transport 就**必须**提供 HTTP 端点，且响应需符合 `data-stream` 或 `assistant-transport` 协议（`AssistantTransportProtocol = "data-stream" | "assistant-transport"`）。

### 事实 3：协议本身有官方逃生舱

`AssistantStreamChunk` 的 part 类型含 `data`：

```35:40:apps/desktop/node_modules/assistant-stream/dist/core/AssistantStreamChunk.d.ts
} | {
  readonly type: "data";
  readonly name: string;
  readonly data: ReadonlyJSONValue;
  readonly parentId?: string;
};
```

且 `@assistant-ui/react` 导出 `DataRenderers` / `DataMessagePartComponent` / `DataMessagePartProps` —— **自定义 data part 渲染是一等公民能力，不是 hack**。这决定了我们的差异化能力（subagent / 变更集 / 上下文占用）有正当落点。

### 事实 4：现有自研投影层是「自研弯路」的实证

`services/timeline/projector.ts` **1040 行 / 18 个导出**，`components/layout/TurnTimeline.tsx` 顶部 70 行设计说明全在处理增量投影、引用稳定、`turn_id` 硬不变量防串味。这些正是 assistant-ui 的主业。

---

## 二、事件映射表（27 种 → 协议）

后端 `app/models/payload/` 现有 27 种事件。映射到 `AssistantStreamChunk`：

| 后端事件 | 协议落点 | 评价 |
|---|---|---|
| `step_started` | `step-start` | ✅ 原生 |
| `model_output_delta` | `text-delta`（text part） | ✅ 原生 |
| `model_thinking_delta` | `text-delta`（reasoning part） | ✅ 原生 |
| `model_tool_call` | `part-start{type:"tool-call"}` + args delta + `tool-call-args-text-finish` | ✅ 原生 |
| `tool_call_started` / `tool_call_finished` | `result` | ✅ 原生 |
| `model_completed` | `step-finish`（带 usage） | ✅ 原生 |
| `model_failed` / `run_failed` / `run_cancelled` | `error` | ✅ 原生 |
| `run_finished` | `message-finish`（带 finishReason + usage） | ✅ 原生 |
| `final_response` | text part 累积 + `message-finish` | ✅ 原生 |
| `delegation_*`（5 个） | `data` part + `DataRenderers` | ✅ 官方扩展点 |
| `file_change_updated` / `file_change_stable` | `data` part + `DataRenderers` | ✅ 官方扩展点 |
| `context_usage` | `step-finish.usage` 或 `data` part | ✅ 可选 |
| `tool_output_delta`（终端实时流） | `data` part | ⚠️ 需自定义 |
| `run_started` / `model_requested` / `observation_added` | `update-state` 或 `data` part | ⚠️ 需自定义 |
| `human_input_requested` / `human_input_received`（工具审批） | 待验证（见 §六 R3） | ❓ 需 spike |

**结论**：映射面比预期好——`data` part 是官方逃生舱，三个原本担心的差异化能力（subagent / 变更集 / 上下文）都有正当落点，不是硬塞。

---

## 三、技术选型：推荐 assistant-transport（路径 B）

### 判断依据

用户的判断准绳是「后续迭代」，不是改动量。据此：

**路径 A（external-store）虽然后端零改造，但自研面几乎没缩小。**

它只是把 `projector.ts` 的 1040 行从「投影成 TimelineEntry」改写成「converter 成 ThreadMessage」。以下全都还得自己写：

- SSE 连接与解析（现有 `sseConnectionPool` 保留）
- 命令队列与乐观更新（现有 `useTask` 的乐观 turn 逻辑保留）
- 失败回滚（**这正是当前 P0 与草稿丢失缺陷的所在**）
- 断线恢复（现有 `useStartupTaskResume` 保留）

等于：**换了一套渲染，埋的坑一个没少。**

**路径 B（assistant-transport）把上述四项交给库**：`api` 驱动发送、内置命令队列（`pendingCommands` / `isSending` / `CommandQueueState`）、`onError` 带 `updateState` 内置回滚、`resumeApi` / `resumeStateApi` 内置恢复。

自研面收敛到只剩两件事：

1. `converter: (state: T, metadata) => AssistantTransportState` —— 纯函数，易测
2. `data` part 的渲染器 —— 差异化 UI

### 代价（诚实列出）

| 代价 | 说明 |
|---|---|
| 后端必须新增协议端点 | 见 §四 |
| Python 侧需自写编码器 | `assistant-stream` 是 TS 包，Python 无现成实现，需自写 `RuntimeEvent → data-stream` 编码 |
| 0.x API 风险 | `unstable_*` 前缀与 `@deprecated` 标注若干，见 §六 R1 |

---

## 四、后端改造清单（路径 B 必需）

> 原则：**新增并行端点，不替换现有 `/turns/{id}/stream`**。现有 SSE 链路（含 `turn_stream_service` 编排）保持可用，可灰度、可回退。

### B1 · 协议编码器（新增）

新增 `app/api/transport/event_stream_encoder.py`：

- 职责单一：把 `RuntimeEvent` 流转成 data-stream 协议的 SSE 帧
- 输入：事件流；输出：`text/event-stream` 分帧
- 映射规则按 §二 表格落地，逐事件类型一个分支
- 必须可单元测试（纯函数，无 IO）

### B2 · 发送端点（新增）

新增端点，接收 `SendCommandsRequestBody`：

```
commands: QueuedCommand[]     // add-message | add-tool-result | 自定义
state?: unknown
runId?: string
system / tools / callSettings / config / threadId / parentId
```

- 由 `prepareSendCommandsRequest` 在前端裁剪成我们自己的请求体（不必全盘接受它的 body 形状）
- **「新建任务 + 首 turn」作为一个复合 command 表达** —— 这一条直接消灭当前 P0 的载体（`useTask.createTask` 跨 await 调 `createTurn` 的闭包问题在结构上不再存在）

### B3 · 流式响应端点（新增）

- 在现有 `turn_stream_service` 之上叠加 B1 的编码器，输出协议流
- `run_finished` → `message-finish`；`run_failed` → `error`

### B4 · 恢复端点（可选，Phase 4）

`resumeStateApi` / `resumeApi`：返回 retained state 与 runId，无活跃 run 时返回 204。
现有 `useStartupTaskResume` 可退场或简化。

### 后端**不**改的部分（红线）

- `task` / `turn` / `delegation` 领域模型 —— 一个字不动
- 27 种 payload 定义 —— 不动
- 事件持久化与 replay —— 不动
- 工具权限 / path 边界 / 审批决策 —— 不动

---

## 五、分期与验收

### Phase 0 · Spike（阻断项，必须先做）

**目的**：在写任何生产代码前，验证三个不确定点。

1. **S1**：`human_input_requested` / `human_input_received`（工具审批）在协议中如何表达——是否有原生机制，还是必须走 data part 自建
2. **S2**：`tool_output_delta`（终端实时流式输出）能否映射进 `result` 的增量，还是必须走 data part
3. **S3**：`prepareSendCommandsRequest` 能否把 body 裁剪成我们现有的 `CreateTurnRequest` 形状（含 `provider_id` + `model_name` 配对、`reasoning_effort`、`attachments`）

**验收**：三个问题各有明确结论 + 一段可运行的最小验证（不必是生产代码）。任一结论为「无法映射且无扩展点」→ 回到选型讨论。

### Phase 1 · 后端协议层

新增 B1 + B2 + B3，与现有端点并行。

**AC1** 编码器为纯函数，27 种事件逐条单测覆盖
**AC2** `step-finish` / `message-finish` 携带的 usage 与后端 `turn_usage_stats` 一致
**AC3** `delegation_*` / `file_change_*` 均编码为带 `name` 的 data part，`name` 取值稳定且前端可枚举
**AC4** 现有 `/turns/{id}/stream` 行为零变化（回归测试通过）

### Phase 2 · 前端 runtime + Composer

接入 `useAssistantTransportRuntime`，替换 `InputBar` 与发送链路。

**AC5** 中文 IME 组合态下 Enter 不误发送（现自研三路判定退场，改由库的 composer 承担）
**AC6** **P0 验收用例通过**：新建任务时，后端收到的 `task_id` 是真实 ID，不是乐观占位 `-1-N`
**AC7** 发送失败时草稿不丢（store 与输入框两处都断言） —— 现有 `fixverify_send_rollback.test.ts` 只断言本地 `restore`，是假信心测试，须同步补强
**AC8** 首次发送失败后，第二次发送可正常走新建路径（当前会打 `createTaskTurn("-1")` → 404，见复审报告缺陷 7）

### Phase 3 · 消息渲染

替换 `ChatPanel` / `TurnTimeline`，`projector.ts` 退场。

**AC9** 长对话流式渲染不掉帧（以现有 `TurnTimeline` 的性能表现为基线，不得倒退）
**AC10** thinking / tool-call / result 三种 part 渲染与现有 `ThinkingBlock` / `ToolCallCard` 视觉等价
**AC11** `TaskHeaderBar`（Agent / Model 选择器）保持不动 —— 它属于项目差异化，不进 composer

### Phase 4 · 差异化能力走 data part

**AC12** `SubagentPanel` 与并发 delegation 流功能不倒退
**AC13** `ChangesDrawer` 与变更集撤销功能不倒退
**AC14** 上下文圆环**保持按 task 维度隔离** —— 迁移中不得退回全局单值（见 §六 R2）

### Phase 5 · 清理

移除已退场模块：`services/timeline/projector.ts`、`groupTools.ts`、`TurnTimeline.tsx`、旧 `InputBar` 自研状态机。

**AC15** 无死代码残留；`react-textarea-autosize` 的显式依赖可移除（assistant-ui 已依赖它）

---

## 六、风险登记

| 编号 | 风险 | 应对 |
|---|---|---|
| **R1** | 0.x API 不稳定：`unstable_*` 前缀若干，`external-store-adapter.d.ts` 内有 7 处 `@deprecated This API is still under active development` | 把所有 `unstable_*` 调用**集中到少数几个适配文件**，不让不稳定面扩散到业务组件；升级时改动面可控 |
| **R2** | 迁移期上下文圆环退回全局单值 | AC14 明列为验收项；`contextUsageStore` 的 task 维度隔离是刚修好的能力，迁移中不得回退 |
| **R3** | 工具审批的协议表达未定 | Phase 0 S1 必须先有结论 |
| **R4** | `unstable_enableToolInvocations` 误开启 | **必须保持默认 `false`**。源码注释明确：工具在服务端执行的 runtime 若开启，回调会**跑两遍**。我们是服务端执行，必须显式不开启并加注释说明理由 |
| **R5** | zod 双版本 | 顶层 `zod@3.25.76`，`@assistant-ui/react/node_modules/zod` 为 v4。跨边界传 schema 会类型不兼容。迁移中**不在 assistant-ui 边界上传递 zod schema**；长期需规划升级到 v4 |
| **R6** | `assistant-cloud@0.1.42` 是硬依赖（非 optional） | 本地桌面应用无云端需求，需确认它不会发起外网请求；如有，需在构建期剔除 |
| **R7** | 现有 100+ tsc 红色错误会干扰 | 只要求**本次相关文件零新增错误**，不要求清零存量（沿用既有约定） |

---

## 七、反验收：这些**不算**完成

- ❌ 「组件都换成 assistant-ui 了」—— 若 AC5（IME）/ AC6（P0）/ AC14（圆环隔离）任一不成立，仍判不合格
- ❌ 「converter 写完了」—— 若 `delegation` / 变更集 / 上下文三条链路功能倒退，不合格
- ❌ 「测试数量够了」—— 测试必须对旧实现可证伪
- ❌ 为迁 UI 而修改后端 `task` / `turn` / `delegation` 领域模型 —— 违反 §四红线
- ❌ 顺手重构 `SubagentPanel` / `ChangesDrawer` 的视觉表现 —— 不在范围

---

## 八、本次明确不做

| 项 | 说明 |
|---|---|
| 线程列表（Sidebar）迁到 `remote-thread-list` | 会动到刚修好的 task 维度隔离逻辑，Phase 5 之后再单独评估 |
| 消息编辑 / 分支切换 UI | 库有此能力，但需先定产品语义（编辑一条 turn 意味着重跑还是改文案） |
| 后端领域模型改造 | 红线，见 §四 |
| 现有 SSE 链路下线 | Phase 5 且全量灰度通过后再议 |
| zod 升级到 v4 | 独立任务 |

---

## 九、待用户拍板

1. **选型**：确认走路径 B（assistant-transport，后端改造）还是路径 A（external-store，后端零改造但自研面不缩小）。**推荐 B**，理由见 §三。
2. **`@assistant-ui/react` 是否保留**：若确认推进，此依赖留下并补提交；若暂缓，建议先卸掉——它当前 mock 了一个未安装的包，导致 `InputBar.attachment.test.tsx` 10/10 收集期失败，长期占据基线会掩盖真实回归。
3. **P0 与迁移的顺序**：推荐「迁移中一并消解」（Phase 2 的 AC6/AC7/AC8 即为 P0 用例），而非先单独修一遍再重写。前提是 Phase 2 周期可控。

---

## 十、审查 Agent 判定输出格式

```text
结论：通过 / 不通过

逐条判定：
  AC1 … AC15：通过 / 不通过 + 依据（文件:行）

风险登记复核：
  R1 … R7：已处置 / 未处置 + 依据

反验收检查：
  是否存在「以改动量替代结果态」的情形：是 / 否

不通过项清单（若有）：
  [文件名:行号] 问题描述｜违反 AC 编号｜修复建议
```

首行必须是「通过」或「不通过」。不通过时逐条列出文件名、行号、问题描述、违反条款与修复建议；不提修复建议的判定无效。

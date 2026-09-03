# 目标：Assistant Transport 链路打通 + 服务端权威 state（历史任务书）

> 本文是开发任务书与验收依据。开发 Agent 按本文实现；审查 Agent 与测试 Agent
> 本文记录的是 Assistant Transport 重构阶段的历史基线，不再描述当前桌面启动与前端代理架构；当前事实以代码和仓库根 `AGENTS.md` 为准。
> 文中旧的 Next 代理、固定端口和 `/api/backend` 引用均为历史问题快照，不是当前实现要求。
>
> **最高原则（第零铁律）**：尽量不改造后端；只有在「改造后端能更好适配前端」时才改造。
> 第 4 节逐项记录了每个改动点「改前端还是改后端」的判定与理由，那是本文的核心。
>
> 事实来源：① `apps/` 当前代码（行号为本文撰写时快照）；② assistant-ui **官方 MCP 文档**
> （`/docs/runtimes/custom/assistant-transport`、`/docs/runtimes/concepts/adapters`、
> `/docs/runtimes/concepts/threads`、`/docs/runtimes/custom/data-stream`）；
> ③ 已安装依赖的类型契约（`@assistant-ui/core/dist/**`）。

---

## 1. 目标（一句话）

**让 Assistant Transport 对话链路端到端可用，并把对话 state 的权威来源从「客户端回传」切换为「服务端 `turns` / `turn_messages` 事实」。**

- **G1（可用）**：用户在桌面端发一条消息，模型真实返回结果并渲染出来。当前**做不到**。
- **G2（权威）**：对话历史的唯一来源是服务端数据库；前端不持有、不回传、不伪造历史。

---

## 2. 现状代码事实（历史问题基线）

### 2.1 G1 的三个阻断级缺陷

| 编号 | 事实 | 位置 |
|---|---|---|
| B1 | 后端注册 `/api/tasks/{task_id}/assistant`，是**全后端唯一**带 `/api` 前缀的路由（其余 23 个均为 `/tasks`、`/workspaces`、`/models`、`/providers`…）；前端请求 `/api/backend/tasks/{id}/assistant`，Next 代理剥掉 `/api/backend` 后落到 `/tasks/{id}/assistant` → **404** | `apps/backend/app/api/assistant_api.py:26`；`apps/desktop/lib/api/client.ts:14`；`apps/desktop/app/api/backend/[...path]/route.ts:18` |
| B2 | 代理默认后端端口 `8002`；后端默认端口 `8000` 且由 `CODING_AGENT_PORT` 环境变量控制、README 有记载；全仓库无人设置 `COSIR_BACKEND_URL` → **连接被拒** | `apps/desktop/app/api/backend/[...path]/route.ts:5`；`apps/backend/app/__main__.py:72` |
| B3 | 模型选择只写 localStorage，从未下发；`main_agent` profile 无默认模型 → `resolve_chat_model` 取不到 model/provider → **每轮 RUN_FAILED**。而 `TurnService.create_turn` **已经支持** `provider_id`/`model_name`/`reasoning_effort` 三个入参，缺的只是「前端传 → schema 收 → api 透传」这一段 | `apps/desktop/components/assistant-ui/model-selector.tsx:112,143`；`apps/backend/app/api/assistant_api.py:59`；`apps/backend/app/core/agents/define_agents.py:27` |

### 2.2 G2 的核心缺陷

```135:139:apps/backend/app/api/assistant_api.py
    history = request_state.get("messages")
    messages = history if isinstance(history, list) else []
    user_id = f"user-{uuid4()}"
    assistant_id = f"assistant-{uuid4()}"
    messages = [item for item in messages if isinstance(item, dict)][:200]
```

历史**完全来自客户端回传**。已确认的连带后果：

- 前端 `initialState: { messages: [] }`（`apps/desktop/app/assistant.tsx:142`）→ 刷新后历史归零；
- 服务端每次 run 都以 root set 整体覆盖前端 state（`assistant_stream/create_run.py:215`
  发出 `{"type":"set","path":[],"value":...}`，前端 `GorpStreamAccumulator.updatePath` 对
  空 path 直接整体替换）→ **服务端本来就在重建 state，只是输入源选错了**；
- 消息 id 用 `uuid4()`，每次请求都变 → 无法 resume、无法编辑、无法与后端事实对齐；
- 违反 `AGENTS.md` §3：不得从前端投影反推对话事实。

### 2.3 状态语义缺陷（**前端**缺陷，非后端）

```120:123:apps/desktop/app/assistant.tsx
    status:
      message.status === "running"
        ? { type: "running" }
        : { type: "complete", reason: "stop" },
```

后端 `assistant_api.py:194,197` 已经在写 `assistant["status"] = "failed"` / `"cancelled"`，
是**前端 converter 丢弃了这两个值**。这是前端 3 行的事，不是后端问题。

### 2.4 时间戳丢失

当前 state 的 message **不含 `createdAt`**，前端 `toThreadMessage` 走
`new Date()` 兜底 → 历史消息全部显示「现在」。而 `TurnRecord.created_at` 是现成的事实。

### 2.5 已确认的死 UI（不在本次范围，见第 6 节）

官方 adapters 支持矩阵中，`AssistantTransport` 对 **Speech / Dictation / Feedback /
Suggestion 四者均为 `(no)`**，文档原文：*"Speech, dictation, feedback, and suggestions are
not currently exposed by AssistantTransport."* 当前前端却全部渲染了：
`thread.aui.tsx:286` `Dictate`、`:289` `StopDictation`、`:201` `ThreadFollowupSuggestions`、
`:243` `ThreadPrimitive.Suggestions`。

---

## 3. 官方契约依据（MCP 文档 + 已安装类型）

### 3.1 state 的所有权

- 官方：*"The UI is a stateless view on top of the agent state."*
- `create_run` docstring：*"You can set the root state directly by assigning to this property."*
  → **root set 灌入服务端权威 state 是协议内一等公民操作**，无需改动协议。
- 官方 payload 中 `state` 的语义是 *"previous state the frontend has"*，参考实现
  `create_run(run_callback, state=request.state)`。我们**不采用**这个默认模型。

### 3.2 历史适配器不可用（决定方案形态）

adapters 支持矩阵中 `AssistantTransport` 的 History 行为是 **`(use thread converter)`**，
即 `ThreadHistoryAdapter` 不适用于 transport runtime。
→ **首屏历史必须由 converter 从服务端 state 产出**，不能靠 history adapter。

### 3.3 「状态映射」是 converter 的职责（**推翻前一版方案的关键依据**）

官方对自定义 runtime 的定义明确包含：

> **Frontend state converter** that maps state snapshots to assistant-ui's data format.

即：**把后端状态快照翻译成 assistant-ui 数据格式，是前端的职责**。
后端只需说出事实，不必知道 `MessageStatus` 长什么样。

### 3.4 `MessageStatus` 契约（`@assistant-ui/core/dist/types/message.d.ts:251`）

```ts
type MessageStatus =
  | { type: "running" }
  | { type: "requires-action"; reason: "tool-calls" | "interrupt" }
  | { type: "complete"; reason: "stop" | "unknown" }
  | { type: "incomplete";
      reason: "cancelled" | "tool-calls" | "length" | "content-filter" | "other" | "error";
      error?: ReadonlyJSONValue }
```

**这是前端 converter 映射的唯一依据。该类型不得出现在后端代码中。**

### 3.5 `initialState` 的读取时机

`useAssistantTransportRuntime` 内部 `const agentStateRef = useRef(options.initialState)`，
**只在 runtime 首次创建时捕获一次**。因此首屏历史必须「先取后挂」，并用 `key={taskId}`
保证切换 task 时重建（与官方 `withKey(id, MyThread(...))` 同源思路）。

---

## 4. 第零铁律：改造边界判定（本文核心）

> 原则：**尽量不改造后端；只有改造后端能更好适配前端时才改造。**
> 补充准绳：后端的 `RuntimeEvent` 体系、领域模型、service 契约是**后端自己的事实**，
> transport 层的职责是「投影」，不是「改造」。绝不为了迁就前端而改变后端领域语义。

| # | 议题 | 判定 | 理由 |
|---|---|---|---|
| 1 | **路由前缀（B1）** | **改后端**：去掉 `/api`，改为 `/tasks/{task_id}/assistant` | 后端 23 个路由全部无 `/api` 前缀，这个 `/api` 是**后端自身的不一致**。修它是修正后端自身错误，不是迁就前端。反过来改前端会得到一个 `/api/backend/api/tasks/...` 的怪路径 |
| 2 | **端口（B2）** | **改前端**：代理默认端口由 `8002` 改为 `8000` | 后端 8000 有环境变量 `CODING_AGENT_PORT` 且 README 有记载；前端 8002 是**无文档的凭空硬编码**。改前端是修正前端自身的无据假设 |
| 3 | **模型下发（B3）** | **两端各加一小段**：前端注入 3 个字段，schema 补 3 个字段并透传 | 后端 `create_turn` **已支持**这三个入参，核心逻辑零改动。适配成本极低，不做则功能根本不可用 |
| 4 | **历史来源（G2）** | **必须改后端**：新增 `ConversationStateService` 从 `turns`/`turn_messages` 重建 | 这是「服务端要成为权威源」的必然要求。但**改动边界严格限定为新增一个 service 文件 + api 层调用**，不触碰任何既有领域逻辑 |
| 5 | **state wire 形状** | **不改**：后端继续输出中性形状 | 见下条 |
| 6 | **status 语义（2.3）** | **只改前端**：converter 补 `cancelled`/`failed` 映射 | 后端已经在写正确的领域字符串，是前端丢弃了它们。按 3.3，映射本就是 converter 职责。**改后端产出 assistant-ui 对象形状是错误的**——那会把框架类型泄漏进后端、让后端与 assistant-ui 版本强耦合，违反 `AGENTS.md` §3 精神 |
| 7 | **首屏历史端点** | **加一个薄 GET 端点**，复用同一个 service 方法 | 无它则刷新丢历史。端点只做「调 service → 返回」，无新逻辑 |
| 8 | **取消端点（T5）** | **加一个薄端点**，复用既有 `TurnService.cancel_turn_if_active` | 后端**已有**该 service 方法与运行时能力，只差 HTTP 暴露。不加则取消被记为 `failed/client_disconnected`，持续产生脏数据 |
| 9 | **`RuntimeEvent` 体系** | **绝对不动** | 23 种事件是后端领域事实。transport 只投影、不改造 |
| 10 | **既有 service / CRUD** | **不动** | 全部通过现有 `TurnService` / `TurnMessageCrud` 读取 |

### 4.1 前版方案的错误（明确记录，防止回退）

前版方案主张「后端直接产出 assistant-ui `ThreadMessage` 形状，converter 退化为透传，消除双协议」。**该决策已推翻**，理由：

1. **违反 3.3**：状态映射是 converter 的职责，官方文档明确定义；
2. **违反 `AGENTS.md` §3 精神**：该条款禁止 assistant-ui 类型进入 `core/tools/models/storage`，
   虽然 `api` 层不在字面禁止范围，但把框架 wire 格式当后端 state 输出，等于绕开条款意图；
3. **制造版本耦合**：升级 assistant-ui 就要改后端；
4. **前版方案内部自相矛盾**：4.1 节说「service 产出中性视图」，4.2 节又说「产出 assistant-ui 形状」，两条无法同时成立。

**修正后**：后端输出中性 wire 形状（与现状一致，仅补 `createdAt`），前端 converter 做映射。

---

## 5. 目标架构与任务分解

### 5.1 分层（只新增 **1 个**后端文件）

```
apps/desktop/app/assistant.tsx
  │  runtime 装配（瘦）+ converter（含 status 映射）+ 首屏拉取 + key 重建
  ▼
apps/backend/app/api/assistant_api.py
  │  端点编排：校验 → 建 turn → 调 service 取 state → 建流 → 返响应
  └─▶ apps/backend/app/service/task/conversation_state_service.py   【新增，唯一新文件】
        单一职责：turns + turn_messages → 中性对话视图
        └─▶ 复用既有 TurnService.list_turns_for_task / TurnMessageCrud.load_messages_full
```

- 依赖方向 `api → service`，符合 `AGENTS.md` §4；`api` 不直连 storage。
- **只新增 1 个文件**（前版为 2 个）。修正后只剩「领域事实 → 中性视图」**一个职责**，
  拆第二个文件属过度拆分。

### 5.2 后端中性 wire 形状（保持现状，仅补 `createdAt`）

```jsonc
{
  "messages": [
    {
      "id": "turn-12-user",              // 稳定 id，派生自 turn 主键
      "role": "user",
      "status": "complete",              // 领域字符串，不是 assistant-ui 对象
      "createdAt": "2026-08-30T10:00:00Z",
      "parts": [{ "type": "text", "text": "...", "status": "complete" }]
    },
    {
      "id": "turn-12-assistant",
      "role": "assistant",
      "status": "complete",
      "createdAt": "2026-08-30T10:00:05Z",
      "parts": [{ "type": "text", "text": "...", "status": "complete" }]
    }
  ],
  "run": { "runId": 12, "status": "completed" },   // runId = turn_id，供取消端点使用
  "revision": 7
}
```

- `status` 值域 = `TurnStatus` 枚举取值（开发时核实，预期为
  `pending` / `running` / `completed` / `failed` / `cancelled`）。
- **该形状不含任何 assistant-ui 专有类型**。

### 5.3 数据取值规则

| 字段 | 来源 |
|---|---|
| user 消息文本 | `turn.input_text` |
| assistant 消息文本 | 优先 `turn.response_text`；**为空时** fallback 到 `TurnMessageCrud.load_messages_full(turn.id)` 中最后一条 `role == "assistant"` 的 `content_text` |
| 消息 id | `turn-{turn.id}-user` / `turn-{turn.id}-assistant` |
| `createdAt` | `turn.created_at`（assistant 消息可用 `turn.updated_at`） |
| `role` 过滤 | 只投影 `user` / `assistant`；`tool` / `system` 属模型上下文口径，非 UI 口径，跳过 |

> `response_text` 为空的情况确实存在（如 max_steps 耗尽路径不写该字段），故 fallback 是必需的。

### 5.4 前端 converter 映射表（唯一依据，按 3.4）

| 后端 `message.status` | `turns.end_reason` | `MessageStatus` |
|---|---|---|
| `pending` / `running` | — | `{type:"running"}` |
| `completed` | — | `{type:"complete", reason:"stop"}` |
| `cancelled` | — | `{type:"incomplete", reason:"cancelled"}` |
| `failed` | `client_disconnected` | `{type:"incomplete", reason:"cancelled"}` |
| `failed` | 其它 / 空 | `{type:"incomplete", reason:"error", error:{turnId, endReason}}` |

### 5.5 任务分解

| 编号 | 任务 | 侧 | 关联 |
|---|---|---|---|
| **T1** | 打通链路：B1 后端去 `/api` 前缀；B2 前端代理默认端口改 `8000`；B3 模型选择下发（前端注入 `providerId`/`modelName`/`reasoningEffort` → schema 补字段 → 透传 `create_turn`） | 两端 | G1 |
| **T2** | 服务端权威 state：新增 `conversation_state_service.py`；删除 `_build_initial_state`；schema 删除 `state` 字段；前端 `prepareSendCommandsRequest` 剥离 `state` 不再回传 | 后端为主 | G2 |
| **T3** | 首屏历史：新增 `GET /tasks/{task_id}/assistant/state`（复用 T2 的 service）；前端 mount 拉取，`initialState` 就绪后再挂 runtime，`key={taskId}` 保证切换重建 | 后端薄端点 + 前端 | G2 |
| **T4** | 状态语义：**只改前端 converter**，按 5.4 映射；补 `createdAt` 消费 | **仅前端** | G2 |
| **T5** | 取消闭环：新增 `POST /turns/{turn_id}/cancel`，复用既有 `TurnService.cancel_turn_if_active`；前端停止按钮走真实 cancel | 后端薄端点 + 前端 | G1 |
| **T6** | 前后端各锁依赖版本（`assistant-stream` 等） | 工程 | — |
| **T7** | `_run_turn` 补异常日志（`log.exception`），不得只冒泡给 `add_error` | 后端 | 规范 |

> **执行顺序**：T1 → T2 → T3 → T4 → T7 → T5 → T6。
> T5 独立可切，若 T1–T4 验收通过后可单独交付。

---

## 6. 非目标（明确不做，避免过度扩张）

- **工具调用 / 思考过程投影**：`TOOL_CALL_*`、`MODEL_THINKING_DELTA` 暂不投影。
  `ThreadAssistantMessagePart` 已含 `tool-call` 与 `reasoning`，后续接入无需改协议。
- **HumanApprovalRequest → `interrupt` / `approval` part**：`ToolCallMessagePart` 已定义
  字段，但后端审批链路未接 transport，本次不做。
- **附件**：schema 仍只收文本 part。
- **死 UI 清理**（2.5）：Dictate / StopDictation / FollowupSuggestions / Suggestions
  本次**保留不动**。官方确认 AssistantTransport 不支持，属独立清理项。
- **canonical `ConversationMessage` / `MessagePart` 建表**：`AGENTS.md` §3 提到但当前不存在。
  本次直接投影 `turns` + `turn_messages`。投影收口在单一文件，将来替换数据源零成本。
- **取消级联到子 turn**：只取消指定 turn，子 turn 级联属 delegation 语义重构，独立议题。
- **修改 `RuntimeEvent` / payload / 领域模型 / 既有 service**：一律不动。

---

## 7. 验收标准（子 Agent 只按本节判定）

> Windows cmd 下执行。后端范式：`cd h:\coding-agent\apps\backend; uv run <cmd>`。

### 7.1 静态门禁（必须全绿，任一失败则不进入后续检查）

```
A1  cd h:\coding-agent\apps\backend; uv run ruff check app
    → 无 error（新增文件同样受检）

A2  cd h:\coding-agent\apps\backend; uv run mypy app/api/assistant_api.py app/service/task/conversation_state_service.py
    → 无 error。特别地：当前 `assistant_api.py:186` 的
      `"RuntimeEventPayload" has no attribute "text"` 必须消失。

A3  cd h:\coding-agent\apps\desktop; npx tsc --noEmit
    → 无 error

A4  cd h:\coding-agent\apps\backend; uv run pytest tests -q
    → 全绿（不得为通过而 skip 或删除既有用例）
```

### 7.2 G1 判定（链路可用）

```
B1  路由一致性：
      cd h:\coding-agent\apps\backend; uv run python -c "from app.app import app; [print(getattr(r,'path',r)) for r in app.routes]" | findstr assistant
    与前端实际请求经 Next 代理剥前缀后的路径逐字符比对。
    → 必须匹配（当前不匹配）

B2  端口一致性：代理默认后端端口 == 后端默认端口。
    → 必须一致（当前 8002 vs 8000）

B3  模型下发完整通路（人工代码审查三处）：
      - 前端 prepareSendCommandsRequest（或等价机制）注入 providerId / modelName
      - 后端 AssistantTransportRequest 声明并校验这两个字段
      - assistant_api.py 透传给 turn_service.create_turn
    → 三处必须全部存在（当前三处全无）

B4  契约测试：新增测试用**前端真实路径**发起请求（经代理前缀），断言 200 且
    响应首帧为 update-state。当前 tests/test_assistant_transport.py:66 使用
    /api/tasks/1/assistant，掩盖了 B1，必须改为真实路径。
```

### 7.3 G2 判定（服务端权威）

```
C1  apps/backend/app/api/ 下不得出现 request.state / request_state 的读取（注释除外）。
    _build_initial_state 必须已删除。→ 必须零命中

C2  消息 id 稳定：同一 turn 两次构建 state，user/assistant 消息 id 完全相同
    （形如 turn-{id}-user / turn-{id}-assistant）。
    → 新增单测断言两次结果相等；当前 uuid4 实现必然不等

C3  ConversationStateService 单测至少覆盖：
      - 空 task → messages 为空数组
      - 单个 completed turn → 恰好 2 条消息，status == "complete"
      - 多个 turn → 消息数 = 2 × turn 数，按 turn 顺序展开
      - cancelled turn → assistant status == "cancelled"
      - failed turn（end_reason 非 client_disconnected）→ status == "failed"
      - running turn → assistant status == "running"
      - response_text 为空但 turn_messages 有 assistant 轨迹 → 走 fallback 取到最后一条
      - 消息携带非空 createdAt，且与 turn.created_at 一致
    → 全部通过

C4  前端不回传：assistant.tsx 的 runtime 配置中必须存在剥离 state 的机制
    （prepareSendCommandsRequest 或等价）。→ 必须存在

C5  首屏历史：assistant.tsx 不得再硬编码 initialState: { messages: [] }；
    必须存在「拉取服务端 state → 就绪后挂载 runtime → key={taskId} 保证切换重建」。
    → 必须存在（依据 3.5）
```

### 7.4 T4 判定（状态语义，**只查前端**）

```
D1  前端 converter 不得出现 `running ? ... : complete` 的二分支映射。
    → 必须消失（当前 apps/desktop/app/assistant.tsx:120-123）

D2  前端 converter 必须覆盖 5.4 映射表全部五行，包括
    incomplete/cancelled 与 incomplete/error。
    → 必须存在

D3  apps/backend 全仓库不得出现 MessageStatus 的 TS 类型或其等价字面量结构
    （如 {"type":"incomplete"}）。这是 4.1 的回归防线。
    检查方式：在 apps/backend 下搜索 "incomplete" / "requires-action" / "content-filter"
    → 必须零命中

D4  TransportMessage.status 的类型必须是 assistant-ui 的 MessageStatus
    （或从 ThreadMessage 派生），不得是自定义 "running" | "complete"。
    → 必须对齐 3.4
```

### 7.5 T5 判定（取消闭环）

```
E1  存在 turn 取消端点，且复用既有 TurnService.cancel_turn_if_active，
    不得在 API 层重写状态机或绕过 service 直接调 CRUD。
    → 代码审查确认

E2  前端停止按钮（thread.aui.tsx:296 ComposerPrimitive.Cancel）触发真实
    cancel 请求，而非仅 abort 连接。→ 代码审查确认
```

### 7.6 规范与日志

```
F1  独立审查 Agent 对照 Agent 代码开发规范逐条审查本次改动文件，输出「符合」。
    重点：单一职责、无 Utils/Helper 模糊命名、函数 docstring 四段式且与实现同步、
    无空 catch、无跨层调用（api 不得直连 storage）。

F2  新增/修改的失败路径必须经 `from app.config.logging.logger import log` 落盘
    （含堆栈用 log.exception）。特别地：_run_turn 必须有异常日志路径。

F3  apps/backend/pyproject.toml 中 assistant-stream 必须锁定精确版本。
```

### 7.7 判定规则

- **A1–A4 任一失败 → 整体不通过**，不进入 B/C/D/E 检查。
- **B1–B3 任一失败 → G1 不通过**。**C1–C5 任一失败 → G2 不通过**。
- G1、G2、D、E、F 全部通过，方可判定目标达成。
- 修复后必须**重新执行完整验收**，不得只对失败项做局部回归。

---

## 8. 决策记录

| 项 | 决策 | 理由 |
|---|---|---|
| state wire 形状 | 后端输出**中性**形状，前端 converter 映射 | 3.3（converter 职责）+ `AGENTS.md` §3 精神 + 避免后端与 assistant-ui 版本耦合 |
| 新增后端文件数 | **1 个**（`conversation_state_service.py`） | 修正后只剩一个职责；拆两个属过度拆分 |
| 历史数据源 | 投影 `turns` + `turn_messages`，不建 canonical 表 | 投影收口单一文件，将来替换零成本；不为未验证需求提前建表 |
| assistant_text 取值 | `response_text` 优先，为空 fallback `turn_messages` | max_steps 等路径不写 `response_text`，fallback 必需 |
| 路由前缀 | 改后端去 `/api` | 后端自身不一致（其余 23 路由均无前缀），非迁就前端 |
| 端口 | 改前端为 8000 | 后端 8000 有环境变量 + 文档，前端 8002 是无据硬编码 |
| T5 优先级 | 排在 T1–T4、T7 之后 | 独立可切；但取消语义错误会持续产生脏数据，不可省略 |
| 死 UI | 不清理 | 官方确认不支持，属独立清理项，避免混淆验收 |

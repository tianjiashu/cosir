# 桌面客户端开发文档（第一版 MVP）

> 本文档是 `apps/desktop/` 桌面客户端的开发规格与实施清单。
> 后续开发严格以此文档为准；审查 Agent 依据本文档的 checklist 检查是否遗漏。
> 关联文档：`docs/ui-guidelines.md`、`docs/tech-stack.md`、`AGENTS.md`。
> 后端契约事实源：`apps/backend/app/api/app.py`、`apps/backend/app/api/sse.py`、`apps/backend/app/events/types.py`、`apps/backend/app/workflows/react_like.py`、`apps/backend/app/runtime/runner.py`、`apps/backend/app/runtime/operations.py`。

---

## 1. 目标与范围

### 1.1 目标

搭建 **Tauri 2 + React + TypeScript + Vite** 本地桌面客户端，参考 Codex 桌面客户端的三栏工作台形态（见 `docs/ui-guidelines.md`），实现第一版最小可用 UI 骨架，并预留与后端（FastAPI + LangGraph + SQLite）对接的契约与扩展口子。

### 1.2 第一版必须做（MVP）

- Tauri 2 项目脚手架与窗口配置。
- 三栏布局骨架（左侧导航 / 中央会话 / 右侧信息面板）+ 顶部任务栏 + 底部输入区。
- 左侧：项目列表、任务列表、历史会话入口、插件/能力入口（含选中态、截断、底部用户/设置入口占位）。
- 中央：用户消息气泡、Agent 输出流、代码块、文件链接、运行状态标签、可折叠工具调用卡片。
- 右侧：Outputs / Sources 两个 Tab 的视觉位置与静态只读列表；预留工具调用详情、checkpoint、上下文引用、MCP、Subagent、Context Compaction 区块锚点。
- 顶部：当前项目/任务标题、项目图标、更多操作入口（占位）、运行状态展示（接事件流或占位）。
- 底部：纯文本输入框、发送按钮、模型选择下拉（占位）、权限状态指示器（占位）、语音入口占位、"+" 添加入口（占位）。
- 前后端通信层：HTTP（`POST /tasks`、`GET /tasks/{id}`、`POST /tasks/{id}/cancel`）与 SSE（`GET /tasks/{id}/stream`）的 TypeScript 封装与事件映射（覆盖全部 14 个事件类型）。
- 审批事件 `tool_approval_required` 至少**只读渲染**（工具名 + 所需权限 + 原因），对齐 `AGENTS.md` 工具权限审批能力。
- 共享类型：在 `packages/shared/` 中沉淀前后端共享的 TypeScript 契约（事件类型枚举、Task 状态、API 路径常量）。
- shadcn/ui + Radix UI + Tailwind CSS + lucide-react 基础组件落地。

### 1.3 预留但不实现（留扩展口子）

- 附件上传：输入区保留 "+" 入口，点击提示"即将上线"，不实现发送能力。
- 审批交互按钮：权限状态/审批请求做**只读渲染**，不实现批准/拒绝按钮（后端第一版以 `tool_approval_required` + `run_failed` 终止任务，客户端需能解释该失败原因）。
- Monaco Editor：代码块先用 `<pre><code>` + 浅灰样式，后续替换为 Monaco。
- 虚拟滚动（TanStack Virtual）：列表数据结构支持后续接入，第一版不强制。
- 命令面板（cmdk）：布局保留快捷键入口，不实现面板。
- Checkpoint / Subagent / Context Compaction / MCP 的 UI 详情：右侧面板与顶栏预留**具体锚点**（见 4.2），第一版仅占位。
- Python 后端进程管理：Rust 侧写命令接口，第一版可手动启动后端（Tauri sidecar 不强制打通）。

### 1.4 非目标（第一版不做）

- 不做 CLI（见 `AGENTS.md` 不可变决议）。
- 不做像素级复刻 Codex。
- 不实现附件卡片、图片/文档输入。
- 不做多窗口、不做账号系统、不做云同步。

---

## 2. 技术栈

| 层面 | 选择 | 说明 |
|------|------|------|
| 桌面壳 | Tauri 2 | `>= 2.0`，Windows + Mac 兼容 |
| 前端框架 | React 18+ | |
| 语言 | TypeScript 5+ | |
| 构建 | Vite 5+ | |
| UI 组件 | shadcn/ui + Radix UI | 可复制、可定制基础组件 + 可访问交互原语 |
| 样式 | Tailwind CSS | 锁定 **v3**（降低 MVP 不确定性，避免 v4 配置漂移） |
| 图标 | lucide-react | 默认图标体系 |
| 状态管理 | Zustand | 极轻量，不用 Redux |
| HTTP | 原生 fetch | 封装于 `services/api.ts` |
| SSE | `fetch + ReadableStream` | 不用 `EventSource`（仅支持 GET，无法满足后续 POST/鉴权） |
| 表格/虚拟滚动 | TanStack Table / TanStack Virtual | 后续接入，第一版不强制 |
| 代码展示 | Monaco Editor（后续） | 第一版用 `<pre><code>` 占位 |

---

## 3. 目录结构

```text
apps/desktop/
  src-tauri/
    src/
      main.rs            # Tauri 入口
      commands.rs        # IPC 命令（启动/停止/探活 Python 后端，第一版占位）
      process.rs         # sidecar 进程管理（第一版占位）
    Cargo.toml
    tauri.conf.json      # 窗口、权限、bundle 配置
    icons/               # 已有图标资源
    build.rs
  src/
    main.tsx             # Vite 入口
    App.tsx              # 根组件：三栏 Grid 布局
    index.css            # Tailwind 入口 + 全局变量
    components/
      ui/                # shadcn/ui 基础组件（button/input/scroll-area/tooltip/tabs/
                         #   badge/collapsible/separator/dropdown-menu/avatar/dialog）
      layout/
        Sidebar.tsx      # 左侧导航栏
        TopBar.tsx       # 顶部任务栏
        ChatPanel.tsx    # 中央主会话区
        RightPanel.tsx   # 右侧信息面板
        InputBar.tsx     # 底部输入区
      chat/
        UserMessage.tsx  # 用户消息气泡
        AgentMessage.tsx # Agent 输出消息
        CodeBlock.tsx    # 代码块
        FileLink.tsx     # 文件链接
        ToolCallCard.tsx # 可折叠工具调用卡片（含审批请求只读渲染）
        StatusBadge.tsx  # 运行状态标签
      sidebar/
        ProjectList.tsx  # 项目列表
        TaskList.tsx     # 任务列表
        PluginList.tsx   # 插件/能力入口（占位）
        HistoryList.tsx  # 历史会话入口（占位）
      right-panel/
        OutputsTab.tsx   # Outputs 列表
        SourcesTab.tsx   # Sources 列表
        CheckpointBlock.tsx  # checkpoint 区块（占位）
        ContextBlock.tsx     # 上下文引用/Context Compaction 指标（占位）
        McpBlock.tsx         # MCP 入口（占位）
        SubagentBlock.tsx    # Subagent 分组（占位）
    hooks/
      useSSE.ts          # SSE 事件流连接（含回放去重/幂等）
      useTask.ts         # 任务 CRUD
      useBackend.ts      # 后端进程状态（占位）
    services/
      api.ts             # HTTP 封装
      sse.ts             # SSE 流管理
      types.ts           # API 请求/响应类型
    stores/
      taskStore.ts       # 任务/会话状态（Zustand）
      eventStore.ts      # 运行时事件流状态（Zustand）
    lib/
      utils.ts           # 仅放纯函数（cn() 等），组件级逻辑放 hooks
  public/
  package.json
  tsconfig.json
  tsconfig.node.json
  vite.config.ts
  tailwind.config.ts
  postcss.config.js
  components.json        # shadcn/ui 配置
  index.html

packages/shared/
  ts/
    events.ts            # 事件类型枚举、SSE 载荷类型（14 个）
    task.ts              # Task / Session 状态类型、状态枚举
    api.ts               # API 路径常量、请求/响应契约
    index.ts             # 统一导出
```

---

## 4. 布局设计（参照 Codex + ui-guidelines.md）

```
┌──────────────────────────────────────────────────────────────┐
│  TopBar: 当前项目/任务标题 · 运行状态 · 打开位置(占位) · 设置(占位)│
├──────────┬─────────────────────────────┬─────────────────────┤
│ Sidebar  │  ChatPanel（中央主会话区）   │  RightPanel          │
│ 项目列表  │   · 用户消息气泡            │   Tabs: Outputs/Sources│
│ 任务列表  │   · Agent 输出流            │   · Outputs 静态列表  │
│ 历史会话  │   · 代码块/文件链接         │   · Sources 静态列表  │
│ 插件入口  │   · 可折叠工具调用卡片       │   · Checkpoint 区块(占)│
│ (选中态)  │   · 审批请求只读渲染         │   · 上下文引用(占)    │
│ 底部入口  │   · 运行状态标签            │   · MCP 入口(占)      │
│ (占位)    │  ┌───────────────────────┐  │   · Subagent 分组(占) │
│          │  │ InputBar: 文本输入     │  │                      │
│          │  │ + 语音(占) 完全访问▼ ↗ │  │                      │
│          │  └───────────────────────┘  │                      │
└──────────┴─────────────────────────────┴─────────────────────┘
```

### 4.1 尺寸与视觉

- 默认窗口：1200×800；最小：960×640。
- 左侧固定宽 ~240px；右侧固定宽 ~280px（可折叠）；中央弹性。
- 视觉：浅色、克制、工程工具感；低饱和灰阶、细边框、轻阴影、少量强调色（对齐 `ui-guidelines.md` 视觉方向）。
- 信息密度偏高，适合长时间阅读日志/任务/代码。

### 4.2 各区域职责（对齐 ui-guidelines.md）

- **左侧导航**：项目列表、任务列表、历史会话入口、插件/能力入口；当前项目/任务明确选中态；标题截断；底部用户/设置入口占位。
- **顶部任务栏**：当前项目/任务标题、项目图标、运行状态展示（处理中/已完成/失败/已取消，接事件流或占位）、更多操作入口（占位）。
- **中央主会话区**：用户消息（靠右或明确气泡样式）、Agent 输出按步骤展示、长文本/方案/命令输出/代码片段用浅灰内容块、文件链接可点击（`docs/xxx.md` 样式）、运行状态可见（处理中/已处理时长/已取消/失败）、工具调用可折叠、审批请求只读渲染（工具名+权限+原因）。
- **右侧信息面板**：
  - Outputs（任务产生/修改的文件、文档、代码片段、工具产物）。
  - Sources（引用文档/上下文片段/规则文件）。
  - 第一版静态只读，预留以下具体锚点区块：工具调用详情、checkpoint 独立区块、上下文引用（Context Compaction 指标）、MCP 入口、Subagent 分组。
- **底部输入区**：纯文本输入、附件"+"入口（占位）、语音入口占位、权限状态指示器（占位）、模型/模式选择下拉（占位）、发送按钮。

---

## 5. 前后端通信契约

### 5.1 HTTP API（后端已实现）

| 方法 | 路径 | 说明 | 请求 | 响应 |
|------|------|------|------|------|
| POST | `/tasks` | 创建任务 | `{ text: string, session_id?: string }` | TaskRecord |
| GET | `/tasks/{task_id}` | 查询任务状态 | - | TaskRecord |
| GET | `/tasks/{task_id}/events` | 历史事件列表 | - | RuntimeEvent[] |
| GET | `/tasks/{task_id}/checkpoints` | checkpoint 列表 | - | CheckpointRecord[] |
| GET | `/tasks/{task_id}/stream` | SSE 事件流（同时触发运行） | - | text/event-stream |
| POST | `/tasks/{task_id}/cancel` | 取消任务 | - | TaskRecord |

> 注意 1：`/tasks/{id}/stream` 是 GET，但后续可能需要携带鉴权 header 或参数；因此 SSE 采用 `fetch + ReadableStream` 而非 `EventSource`（后者仅支持 GET 且无自定义 header）。
> 注意 2（回放语义）：后端 `run_task` 在任务状态非 `pending` 时会先回放已持久化事件（`runner.py:131-136`）。前端 `useSSE` 需做去重/幂等处理，避免重复渲染历史步骤。

### 5.2 SSE 格式

后端 `format_sse_event` 输出：

```
event: {event_type}
data: {json_payload}

```

`data` 为 `RuntimeEvent.to_dict()` 的 JSON：

```json
{
  "event_id": "uuid",
  "event_type": "run_started",
  "task_id": "uuid",
  "created_at": "2026-07-13T14:06:00+00:00",
  "payload": { "status": "running", "agent": { } }
}
```

> `payload` 结构因 `event_type` 而异（详见 5.3）。示例仅展示 `run_started` 形态。

### 5.3 事件类型枚举（后端实际发出）

后端通过 `record_event` / `_record` 实际发出的 SSE 事件类型共 **14 个**（详见 `workflows/react_like.py`、`runtime/runner.py`、`runtime/operations.py`）：

| event_type | 含义 | payload 关键字段 | 前端处理 |
|------------|------|------------------|----------|
| `run_started` | 任务开始运行 | `{status, agent}` | 置状态为"处理中" |
| `step_started` | 步骤开始 | `{step_type, step_index}` | 新增步骤分组 |
| `model_output_delta` | 模型增量输出 | `{delta}` | 追加到当前 Agent 消息 |
| `tool_call_requested` | 模型请求调用工具（待执行） | `{tool_name, arguments}` | 工具卡片置"运行中/待执行" |
| `tool_call_started` | 工具调用开始执行 | `{tool_name}` | 工具卡片置"执行中" |
| `tool_call_finished` | 工具调用执行完成 | `{tool_name, status, error}` | 工具卡片置"完成/失败" |
| `tool_approval_required` | 工具需用户授权（审批） | `{tool_name, permission, approval_status, reason}` | 工具卡片置"待审批"，**MVP 至少只读渲染审批请求（工具名+所需权限+原因）** |
| `observation_added` | 观察/结果回填 | `{tool_name, status}` | 追加到工具卡片结果 |
| `checkpoint_created` | checkpoint 创建 | `{stage, summary, ...}` | 右侧面板 checkpoint 区块 |
| `checkpoint_failed` | checkpoint 失败 | `{...}` | 日志/错误提示 |
| `final_response` | 模型最终响应产出 | `{status: "completed"}` | 标记输出收尾 |
| `run_finished` | 任务完成 | `{status: "completed"}` | 置状态"已完成" + 时长 |
| `run_failed` | 任务失败 | `{status, error, ...}` | 置状态"失败" + 错误；若 `error=="tool_approval_required"` 关联审批卡片 |
| `run_cancelled` | 任务取消 | `{status: "cancelled"}` | 置状态"已取消" |

> 注意：`tool_call_requested`（模型请求调用）与 `tool_approval_required`（工具返回需授权）语义不同，不可合并。当 `tool_approval_required` 发出后，后端会紧随 `run_failed`（`error="tool_approval_required"`）终止任务；MVP 必须能渲染审批请求上下文，否则用户无法理解失败原因（对齐 `AGENTS.md` 工具权限审批能力）。

### 5.4 共享类型（`packages/shared/ts`）

- `events.ts`：`RuntimeEventType` 联合类型（**14 个**，与 5.3 一致）、`RuntimeEvent` 接口、`CheckpointRecord` 接口。
- `task.ts`：`TaskStatus` 联合类型（`pending` / `running` / `completed` / `failed` / `cancelled`，需与后端 `TaskRecord.status` 对齐——后端完成态为 `completed`，非 `finished`）、`TaskRecord` 接口、`SessionRecord` 接口。
- `api.ts`：API 路径常量（`TASKS`、`TASK_DETAIL`、`TASK_STREAM`、`TASK_CANCEL`）、请求/响应类型。

> 约定：后端为 Python 单一事实源；TS 类型作为跨端契约，字段命名与后端 `to_dict()` 保持一致。

---

## 6. 状态管理（Zustand）

- `taskStore`：当前 session、任务列表、当前任务、任务状态、创建/取消动作。
- `eventStore`：当前任务的事件流数组、连接状态（idle/connecting/streaming/closed）、增量追加逻辑（含回放去重）。
- 派生 UI 状态（如运行状态标签）由 store 数据计算，不单独冗余存储。

---

## 7. 实施步骤

1. 初始化 Tauri 2 项目（Vite + React + TS 模板），配置 `tauri.conf.json`（窗口尺寸、权限、bundle 标识）。
2. 配置 Tailwind CSS v3 + shadcn/ui（`components.json`、基础组件 install）。
3. 搭建 `App.tsx` 三栏 Grid 布局骨架 + `index.css` 全局变量（浅色主题）。
4. 实现 `Sidebar`（ProjectList + TaskList + HistoryList + PluginList + 底部入口，mock 数据）。
5. 实现 `TopBar`（标题 + 运行状态 + 操作按钮占位）。
6. 实现 `ChatPanel`（消息列表 + ScrollArea 自动滚动）。
7. 实现 `InputBar`（输入框 + 发送 + 模型下拉占位 + 权限占位 + 语音占位 + "+" 占位）。
8. 实现 `RightPanel`（Tabs + OutputsTab + SourcesTab + 预留 CheckpointBlock/ContextBlock/McpBlock/SubagentBlock 锚点，静态 mock）。
9. 实现 chat 子组件（UserMessage / AgentMessage / CodeBlock / FileLink / ToolCallCard / StatusBadge）。
10. 编写 `services/`（api.ts + sse.ts + types.ts）。
11. 实现 `hooks/`（useSSE + useTask + useBackend 占位）。
12. 在 `packages/shared/ts/` 沉淀共享类型并导出。
13. 编写 Tauri Rust 侧命令（main.rs / commands.rs / process.rs 占位）。
14. 整体联调与样式微调（对齐 ui-guidelines 视觉方向）。

---

## 8. Checklist（审查 Agent 据此检查遗漏）

### A. 项目脚手架与配置
- [ ] Tauri 2 项目初始化（`src-tauri/Cargo.toml`、`tauri.conf.json`、`build.rs`、`main.rs` 存在）。
- [ ] `tauri.conf.json` 窗口默认 1200×800、最小 960×640。
- [ ] 前端 `package.json` / `vite.config.ts` / `tsconfig.json` / `index.html` 存在且可启动。
- [ ] Tailwind CSS v3 配置完成（config + postcss + `index.css` 指令）。
- [ ] shadcn/ui `components.json` 配置完成。

### B. UI 组件库
- [ ] 安装 shadcn/ui 基础组件：button, input, scroll-area, tooltip, tabs, badge, collapsible, separator, dropdown-menu, avatar（dialog 可选）。
- [ ] `lib/utils.ts` 提供 `cn()` 工具（clsx + tailwind-merge），仅放纯函数。
- [ ] lucide-react 作为默认图标体系接入。

### C. 布局骨架（对齐 ui-guidelines.md 基础布局）
- [ ] 三栏 Grid 布局（左固定 / 中弹性 / 右固定可折叠）。
- [ ] 顶部任务栏（TopBar）：当前项目/任务标题、项目图标、更多操作入口（占位）。
- [ ] 顶部任务栏展示运行状态（处理中/已完成/失败/已取消，接事件流或占位）。
- [ ] 底部输入区（InputBar）固定在主会话底部。

### D. 左侧导航（Sidebar）
- [ ] 项目列表展示，含选中态。
- [ ] 任务列表展示，含选中态。
- [ ] 项目/任务标题支持截断（不撑破布局）。
- [ ] 历史会话入口（占位）。
- [ ] 插件/能力入口（占位，对齐 ui-guidelines 左侧栏要求）。
- [ ] 底部保留用户/设置/帮助入口位置（占位）。

### E. 中央主会话区（ChatPanel）
- [ ] 用户消息气泡（靠右或明确样式区分）。
- [ ] Agent 输出按步骤展示。
- [ ] 长文本/方案/命令输出/代码片段使用浅灰独立内容块。
- [ ] 文件链接可点击样式（如 `docs/agent-v1-technical-plan.md`）。
- [ ] 运行状态可见：处理中 / 已处理时长 / 已取消 / 失败。
- [ ] 工具调用可折叠卡片（避免低价值日志淹没主会话）。
- [ ] 审批请求只读渲染：工具名 + 所需权限 + 原因（`tool_approval_required` 事件）。

### F. 右侧信息面板（RightPanel）
- [ ] Outputs Tab：展示任务产生/修改的文件、文档、代码片段、工具产物（第一版静态只读）。
- [ ] Sources Tab：展示引用的文档/上下文片段/规则文件（第一版静态只读）。
- [ ] 预留扩展区块锚点：工具调用详情、checkpoint 独立区块、上下文引用（Context Compaction 指标）、MCP 入口、Subagent 分组（每项指定具体 UI 锚点，第一版仅占位）。

### G. 底部输入区（InputBar）
- [ ] 纯文本输入框（第一版仅纯文本，见 ui-guidelines 第一版约束）。
- [ ] 发送按钮。
- [ ] 模型/模式选择下拉（占位）。
- [ ] 权限状态指示器（占位）。
- [ ] 语音入口占位（对齐 ui-guidelines 底部输入区要求）。
- [ ] 附件"+"添加入口（占位，点击提示"即将上线"，不实现发送）。

### H. 前后端通信
- [ ] `services/api.ts`：封装 `POST /tasks`、`GET /tasks/{id}`、`POST /tasks/{id}/cancel`。
- [ ] `services/sse.ts`：用 `fetch + ReadableStream` 连接 `GET /tasks/{id}/stream`，解析 `event:` + `data:` 格式。
- [ ] `hooks/useSSE.ts`：管理 SSE 连接生命周期、事件分发到 `eventStore`，并处理回放去重/幂等。
- [ ] `hooks/useTask.ts`：任务创建/查询/取消。
- [ ] 事件类型映射：14 个 `event_type` 全部映射到 UI 状态更新（含 `tool_approval_required` 只读渲染、`final_response` 收尾标记）。
- [ ] 取消任务按钮接入 `POST /tasks/{id}/cancel`。

### I. 共享类型（packages/shared）
- [ ] `events.ts`：`RuntimeEventType` 联合类型（14 个，与 5.3 一致）、`RuntimeEvent`、`CheckpointRecord`。
- [ ] `task.ts`：`TaskStatus`（`pending/running/completed/failed/cancelled`）、`TaskRecord`、`SessionRecord`。
- [ ] `api.ts`：API 路径常量、请求/响应类型。
- [ ] `index.ts` 统一导出，前端与共享类型解耦引用。

### J. Tauri Rust 侧
- [ ] `main.rs` Tauri 入口。
- [ ] `commands.rs`：启动/停止/探活 Python 后端的 IPC 命令（第一版占位）。
- [ ] `process.rs`：sidecar 进程管理（第一版占位，可手动启动后端）。

### K. 状态管理
- [ ] `taskStore.ts`（Zustand）：session/任务列表/当前任务/状态/动作。
- [ ] `eventStore.ts`（Zustand）：事件流数组/连接状态/增量追加（含回放去重）。

### L. 预留扩展口子（非实现，但需留位置）
- [ ] 审批交互按钮：MVP 至少只读渲染 `tool_approval_required` 审批请求；批准/拒绝按钮预留（不实现）。
- [ ] Checkpoint UI 位置（右侧面板独立区块）。
- [ ] Subagent UI 位置（右侧面板分组或任务列表分组）。
- [ ] Context Compaction UI 位置（右侧"上下文引用"区块/指标）。
- [ ] MCP UI 位置（右侧面板入口或左侧插件入口）。
- [ ] Monaco Editor 替换点（代码块组件预留）。
- [ ] TanStack Virtual 接入点（长列表数据结构支持）。
- [ ] cmdk 命令面板入口预留。

### M. 视觉与规范
- [ ] 浅色、克制、工程工具感（对齐 ui-guidelines 视觉方向）。
- [ ] 低饱和灰阶、细边框、轻阴影、少量强调色。
- [ ] 组件职责单一，目录结构清晰（对齐 AGENTS.md 代码开发规范）。
- [ ] `services/`、`hooks/`、`stores/` 每个导出函数含完整 docstring（目的/参数/返回值/异常/副作用，对齐 AGENTS.md 规范）。

### N. 联调与验收
- [ ] 前端可独立启动并展示 mock 数据效果。
- [ ] 后端手动启动后，前端可创建任务并接收 SSE 流、展示 Agent 输出与工具调用卡片。
- [ ] 审批请求（`tool_approval_required`）可只读渲染，且随之而来的 `run_failed` 能解释失败原因。
- [ ] 取消任务可生效并反映状态。
- [ ] 形成审查与测试闭环（开发 Agent 不兼当裁判）。

---

## 9. 审查要点（供审查 Agent 使用）

1. **需求覆盖**：对照 `docs/ui-guidelines.md` 的"基础布局""第一版客户端 UI 参考""左侧导航要求""中央主会话区要求""右侧信息面板要求""UI 组件实现决议"，确认本文档 checklist 是否逐项覆盖。
2. **技术栈一致性**：对照 `docs/tech-stack.md` 与 `AGENTS.md` 不可变决议，确认 Tauri 2 / React / TS / Vite / shadcn+Radix+Tailwind+lucide 选型一致，且未引入被禁止的体系（如 Ant Design / MUI 作为主体系）。
3. **后端契约准确性**：确认 5.1~5.3 节 API 路径、SSE 格式、事件类型（14 个）与后端 `app/api/app.py`、`api/sse.py`、`events/types.py`、runtime 实际发出的事件一致。
4. **范围边界**：确认"必须做 / 预留但不实现 / 非目标"三类边界清晰，无把预留项误标为必须做，也无把必须做遗漏。
5. **可验收性**：确认 checklist 每一项可验证、可执行，无含糊项。
6. **遗漏项**：指出任何未覆盖的 `AGENTS.md` 第一版能力（MCP、工具权限审批、checkpoint、subagent、context compaction、工具系统、任务执行闭环、审查与测试闭环）在客户端侧的 UI 预留是否完整。

# Workbench 子 Agent 只读标签页技术方案

> 状态：v1 已实现，独立验收通过
> 日期：2026-09-16  
> 范围：主 task 委派子 Agent 后，主对话只显示紧凑状态；用户点击后，在右侧 Workbench 的独立标签页查看子 Agent 的只读运行。

> 已确认的产品决策：父级 tool part 的可见投影只包含 `status`、`title` 和 `AgentProfile.role`；`role` 按当前 profile registry 解析；同时允许随 tool part 传递不渲染的 `child_task_id` 作为机器定位字段；只有当前激活的 Agent Tab 保持完整 SSE，其他 Agent Tab 在切回时重新同步；Workbench Tab 不在应用重启后恢复；workspace 删除时统一关闭相关 Workbench Tab。

## 1. 结论摘要

建议把 Workbench 建在 `WorkspaceShell` 层，而不是把它实现成某个 Assistant UI tool 的展开内容：

```text
WorkspaceShell
├─ 左侧：workspace / task 导航
├─ 中间：当前主 task 的 TaskPage + Assistant UI
└─ 右侧：Workbench
   ├─ Tab：子 Agent A（运行中）
   ├─ Tab：终端会话
   ├─ Tab：文件 Diff
   └─ Tab：网页 / 其他 surface
```

主对话中的 `delegate_task` 只负责渲染一个可点击的紧凑条目：图标、状态、title 和打开 Workbench 的动作；子 Agent 的消息、工具过程和长文本不再嵌入主消息流。

本版已落地：`delegation_ref` 严格运行期事件、canonical 冷读恢复、通用 `ToolActivityRow`、Agent surface registry、workspace 级 Workbench、active-only SSE、有限重同步和 stale 关闭态。终端输出、ChangeSet 和回退仍只保留协议预留，不属于本版。

Workbench 是一个前端展示编排层，不拥有 Agent、Run、终端或文件变更事实。标签页只保存“打开哪个 surface”的引用，内容通过既有领域 API / Transport 读取。关闭标签页只关闭视图，不取消后端运行。

当前实现已完成 v1 的 delegation ref、Workbench Agent Tab 和只读 child Assistant Transport；终端输出和文件变更通知仍只预留契约，不纳入第一版交付。

## 2. 当前项目基线

### 2.1 已有能力

| 能力 | 当前实现 | 对本方案的影响 |
| --- | --- | --- |
| Shell | [`WorkspaceShell`](../apps/desktop/components/workspace-shell.tsx) 负责 workspace/task 导航和当前 `TaskPage` | Workbench 应作为 Shell 的稳定兄弟区域，切换 task 时不应无故卸载 |
| Assistant runtime | [`AssistantRuntimeSession`](../apps/desktop/components/assistant/runtime/assistant-runtime-session.tsx) 按 `taskId` 装配 `AssistantRuntimeProvider` 和 Thread | 当前 runtime 是 task 级的；不能直接假设它是全局 Workbench runtime |
| Tool 路由 | [`tool-part.tsx`](../apps/desktop/components/assistant-ui/tools/tool-part.tsx) 已按 `display_data.kind` 和布局路由 | 新增 Workbench 打开动作应放在 delegation renderer，不按工具名扩展通用路由状态机 |
| 委派展示 | [`delegate_task.py`](../apps/backend/app/core/tools/tool_handler/child_task/child_agent_create.py) 当前声明 `expandable=False`、`expand_layout=none`、`show_result=False` | 主消息已经接近“只显示状态”的方向，但目前没有可用的 Workbench 引用和点击动作 |
| 委派 display data | [`delegation_display.py`](../apps/backend/app/core/tools/display/delegation_display.py) 当前可投影 title、child id、status 等字段，但尚未投影 `AgentProfile.role`；目标约束收窄为只向父 tool part 可见展示 `status`、`title`、`AgentProfile.role`，并允许附带不渲染的 `child_task_id` | 不会把子 Agent 正文带进主对话；实现必须补齐 role 的明确来源及 `child_task_id` 的协议投影，前端点击后可直接按 `child_task_id` 读取子 task，而不需要额外的 parent/tool-call locator 解析 |
| 委派生命周期 | [`DelegationService`](../apps/backend/app/service/delegation/delegation_service.py) 已有 pending/running/completed/failed/cancelled 和恢复收敛 | 可复用现有事实，不应新增一套 Workbench 状态机 |
| Assistant 状态读取 | `/tasks/{task_id}/assistant/state` 和 attach SSE 已存在 | 可作为只读 Agent viewer 的基础，但需要确认当前前端能否以只读方式复用 |
| 终端预览 | [`terminal_api.py`](../apps/backend/app/api/terminal_api.py) 已提供带 `after_seq` 的只读 WebSocket | Workbench 的 terminal surface 应复用该 cursor/replay 机制 |
| 文件变更 | [`changes_api.py`](../apps/backend/app/api/changes_api.py) 提供 changes、keep、revert | Workbench Diff 首版只读；保留/回退仍走现有明确操作，不由 Tab 生命周期触发 |
| 左侧 task 树 | 当前 [`task-tree-model.ts`](../apps/desktop/components/task-tree/task-tree-model.ts) 明确是 Fork-only | 子 Agent Tab 不应被误加到左侧 task 树；Workbench 是临时/工作区级视图 |

### 2.2 当前缺口

1. `delegate_task` 的前端展示目前没有专用的可点击 renderer，`users` 图标也没有在 [`tool-icons.tsx`](../apps/desktop/components/assistant-ui/tools/tool-icons.tsx) 中注册，会落到通用图标。
2. 当前 `delegation-result` 主要在子 Agent 完成后才有完整 display data；运行中需要在子 task 创建后，把不渲染的 `child_task_id` 动态补到父 tool part，才能让前端直接打开对应子 Agent。
3. 当前没有通用的 Workbench 容器、Tab 状态模型、surface registry 或 surface 生命周期协调层。
4. assistant-ui 的 tool UI 默认属于消息流，不能天然把内容移动到固定右侧面板。
5. `ToolExecutor.execute()` 和 `ToolHandlerRunner` 已经具备 `output_sink` 参数与跨进程队列，但 [`WorkflowOperations`](../apps/backend/app/core/workflows/workflow_operations.py) 尚未把它接到 Transport 事件；这作为后续 `terminal_output` 预留，不进入第一版。
6. 当前 `ConversationStateToolCallPart`、严格 snapshot 校验和前端 `TransportToolCallPart` 都没有 `child_task_id` 字段；第一版必须同步扩展三处契约，否则运行期 locator 会被校验层丢弃或拒绝。
7. 当前 `ConversationTaskStateRebuilder` 只读取 Task、Run 和 context，不读取 `DelegationRecord`；单靠一次进程内运行期事件不能保证“晚订阅、刷新或后端重启后”仍能打开子 Agent，必须补充可重建的 canonical 投影路径。
8. 当前 assistant-ui 的 `ReadonlyThreadProvider` 只负责渲染给定的 `ThreadMessage[]`，不负责 SSE attach；只读 surface 的订阅、取消、重连和竞态收束必须由项目自己的 adapter 明确实现，不能依赖未公开的 runtime 内部 API。

## 3. assistant-ui 官方能力调研结论

官方文档能提供底层组合能力，但没有直接提供“Codex 风格、可承载终端/子 Agent/网页/Diff 的通用 Workbench”。相关结论如下：

- [Assistant Sidebar](https://www.assistant-ui.com/elements/assistant-sidebar) 是“应用内容 + 一个 Thread”的可调整分栏，右侧并不是通用多标签容器。官方对于静态内容或其他类型面板建议直接组合 `ResizablePanelGroup`、`ResizablePanel` 和 `ResizableHandle`。
- [Tabs](https://www.assistant-ui.com/design/components/tabs) 提供受控 `value` / `onValueChange` 和 `line` 风格，可作为 Workbench 的标签栏基础。
- [Tool UI](https://www.assistant-ui.com/docs/tools/tool-ui) 支持注册 renderer、读取 `args` / `result` / `status`，也支持 `display: standalone`；但 renderer 仍然位于消息流中。
- [Part grouping](https://www.assistant-ui.com/docs/guides/part-grouping) 明确说明 grouping 不会把 tool UI pin、portal 或移动到固定 sidebar；脱离消息流的布局必须由应用自身负责。
- [Multi-agent](https://www.assistant-ui.com/docs/tools/multi-agent) 支持在 tool-call part 中携带 `messages`，再使用 `MessagePartPrimitive.Messages` 渲染嵌套的只读消息；也可用 `ReadonlyThreadProvider` 在 tool 外部渲染 `ThreadMessage[]`。当前项目的委派 display data 没有 `messages`，因此不能直接套用该方案。
- [Subagent List](https://www.assistant-ui.com/elements/subagent-list) 适合并行子 Agent 的列表和状态聚合；若要更平滑的实时进度，官方也提示需要额外的进度通道。
- [State outside the Thread](https://www.assistant-ui.com/docs/guides/state-outside-the-thread) 说明 sidebar/header 可以在 `AssistantRuntimeProvider` 下使用 runtime state。当前 provider 是 task 级的，所以 Workbench 不应依赖某个当前 Thread 才存在。
- [Interactables](https://www.assistant-ui.com/docs/tools/interactables) 用于模型和用户共享的可交互组件状态，不适合作为 Agent Run、PTY 输出或文件变更的事实来源。

因此，本方案采用 assistant-ui 做“子 Agent 消息的只读渲染”，采用项目自己的 Workbench 做“面板、标签和 surface 生命周期编排”。

## 4. 目标架构

### 4.1 进程和事实所有权

本功能不新增进程：

| 层 | 负责 | 不负责 |
| --- | --- | --- |
| React WebView | Workbench 布局、Tab 交互、surface renderer、只读显示、重连提示 | 不直接访问 SQLite、PTY、Agent Runtime 或工作区文件 |
| FastAPI 后端 | task/run/delegation、Assistant snapshot/SSE、终端输出、文件变更等事实 | 不保存“用户打开了哪些 Tab”这种纯 UI 偏好 |
| Tauri Rust | 后端子进程生命周期、动态 runtime config、崩溃恢复 | 不参与 Tab 内容编排 |

后端重启仍按现有约定处理：遗留 active Run 收敛为 cancelled，遗留 active delegation 标记失败；Workbench 重新读取 canonical state，不自动重放执行。

### 4.2 组件边界

建议新增以下前端模块，职责保持单一：

```text
apps/desktop/
├─ lib/workbench/
│  ├─ types.ts                 # Tab / SurfaceRef / 生命周期类型
│  ├─ store.ts                 # UI 状态：打开顺序、active、关闭、聚焦
│  ├─ surface-registry.ts      # surface kind -> renderer / capability
│  └─ surface-adapters/        # 各类 surface 的数据读取和订阅适配
├─ components/workbench/
│  ├─ workbench.tsx            # ResizablePanel + 空态 + Tab host
│  ├─ workbench-tabs.tsx       # Tab bar、关闭、状态徽标、键盘操作
│  ├─ workbench-content.tsx    # 按 registry 渲染 active surface
│  └─ surfaces/
│     ├─ agent-run-surface.tsx # 子 Agent 只读运行
│     ├─ terminal-surface.tsx  # 终端只读预览
│     ├─ diff-surface.tsx      # 文件变更只读审阅
│     └─ web-surface.tsx       # 后续网页 surface，占位接口
└─ components/assistant-ui/tools/
   ├─ tool-activity-row.tsx   # 通用低噪声运行行基础组件
   └─ delegation-tool.tsx     # delegation 数据适配器
```

后端运行期事件建议新增在：

```text
apps/backend/app/assistant_transport/event/
└─ tool_runtime_event.py      # 运行期工具 UI 事件（第一版仅 delegation_ref）
```

`Workbench` 不应反向依赖 `TaskPage`；`TaskPage` 只调用 `openWorkbenchSurface`。这样未来从任务树、通知、文件列表打开相同 surface 时，不需要复制 Tab 逻辑。

### 4.3 核心类型

以下是建议的前端 UI 类型。它们是引用和显示状态，不是后端领域模型的替代品：

```ts
type WorkbenchSurfaceKind =
  | "agent-run"
  | "terminal"
  | "diff"
  | "web";

type SurfaceRef = {
  kind: WorkbenchSurfaceKind;
  surfaceId: string;       // 稳定去重键，例如 agent-task:<childTaskId>
  title: string;
  workspaceId?: string;
  taskId?: string;
  runId?: string;
  delegationId?: string;
  sessionId?: string;
  changeSetId?: string;
  url?: string;
};

// 随父 tool part 传递、但不渲染到主消息中的机器定位字段。
// 协议字段名为 child_task_id；前端内部可转换为 SurfaceRef.taskId。
type AgentRunLocator = {
  childTaskId: number;
};

type ToolRuntimeUpdateKind =
  | "delegation_ref"       // v1
  | "terminal_output"      // reserved
  | "change_set_updated";  // reserved

type ToolRuntimeUpdate = {
  taskId: number;
  runId: number;
  toolCallId: string;
  seq: number;              // 每个 tool call 单调递增
  kind: ToolRuntimeUpdateKind;
  data: Record<string, unknown>;
};

type WorkbenchTab = SurfaceRef & {
  status: "idle" | "loading" | "running" | "completed" | "failed" | "stale";
  openedAt: number;
  lastSeenRevision?: string;
};
```

第一版只发送 `delegation_ref`：

```json
{
  "type": "tool_call_runtime_update",
  "kind": "delegation_ref",
  "data": {
    "child_task_id": 34,
    "role": "reviewer"
  }
}
```

`toolCallId` 只用于在父 task snapshot 中定位目标 tool part，不落库到
`DelegationRecord`。`terminal_output` 和 `change_set_updated` 先保留类型位置，避免以后
再次改造事件外壳；第一版不得发送这两类事件。

`store.ts` 只管理 `WorkbenchTab[]`、active tab、面板展开/收起和用户偏好。完整消息、终端 ring buffer、Diff 内容由各自 adapter 按需加载，并在组件内部处理订阅和释放；不把大段内容塞入全局 UI store。

### 4.4 Surface registry

registry 是长期可维护性的关键：

```ts
type WorkbenchSurfaceDefinition = {
  kind: WorkbenchSurfaceKind;
  createTab(ref: SurfaceRef): WorkbenchTab;
  render: React.ComponentType<{ tab: WorkbenchTab }>;
  closePolicy: "detach-only" | "confirm-if-dirty";
};
```

新增网页、日志、预览等类型时只增加一个 surface definition，不修改 Workbench host 的条件分支。未知类型应进入受控 fallback，并记录 `frontendLog("warning", "workbench_surface_unknown", ...)`，不能使主消息流崩溃。

## 5. 子 Agent 的交互和渲染设计

### 5.1 主消息中的紧凑条目

新增 `DelegationToolRow`，替代当前 delegation 走通用 `DetailsTool` 的路径。它应表现为一个低噪声、可打开的“运行对象”行，而不是把子 Agent 对话嵌套成第二个聊天框。父级 tool part 的可见投影严格限制为：

- `status`：来自后端 `ToolObservation.status` / Transport 状态；
- `title`：来自委派请求的 title；
- `role`：来自目标 `AgentProfile.role`。

不得向父 tool part 的可见 UI 增加 `delegation_id`、`child_run_id`、prompt 或 child transcript。`child_task_id` 允许作为不渲染的机器定位字段随 tool part 传递，但不得被当作可见文案或状态来源。

条目还需要携带一个仅供打开动作使用的 `AgentRunLocator`（`childTaskId`）。它不是展示字段；该字段由第一版的 `delegation_ref` 运行期事件补齐，Workbench 直接将其转换为 `SurfaceRef`，不通过父 task、父 run 和原始 `toolCallId` 做二次解析，也不根据 title、role 或数组顺序猜测目标。

推荐视觉结构：

```text
┌────────────────────────────────────────────────┐
│  [Agent 图标]  Review parser                    │
│                Reviewer                 ● 运行中 › │
└────────────────────────────────────────────────┘
```

条目本身应包含：

- 左侧：语义明确的子 Agent 图标（优先复用 `ToolIcon`，注册 `users` / `UsersRound`）；
- 中间第一行：`title`，单行截断，完整内容通过 tooltip 和无障碍 label 提供；
- 中间第二行：`AgentProfile.role`，使用弱化颜色，不显示 prompt 或 child task id；
- 右侧：复用 `ToolStatus` 的运行中 / 完成 / 失败 / 取消状态徽标；
- 最右侧：轻量 `ChevronRight` 或打开提示，仅在 hover / focus 时增强可见度；
- 整行使用原生 `<button>` 语义，提供明确的键盘焦点和 `Enter` / `Space` 行为；
- 点击只执行 `openWorkbenchSurface({ kind: "agent-run", taskId: child_task_id })`，不触发取消、不改变主 Thread；
- 不展示 prompt、child message 正文、原始异常、堆栈或工具大段输出。

状态样式建议如下：

| 状态 | 视觉表现 | 交互 |
| --- | --- | --- |
| `pending` | 灰色 Agent 图标，显示“准备中” | 尚无 `child_task_id` 时不可点击 |
| `running` | 蓝色图标或轻量状态点，显示“运行中” | 有 `child_task_id` 后可打开 Workbench |
| `completed` | 绿色完成图标，显示“已完成” | 可重新打开只读 Tab |
| `failed` | 红色警告图标，显示“失败” | 可打开查看失败上下文 |
| `cancelled` | 灰色取消图标，显示“已取消” | 可重新查看已产生的只读内容 |

样式应复用 [`DISCLOSURE_ROW_CLASS`](../apps/desktop/components/assistant-ui/elements/disclosure-tokens.ts)、
[`ToolStatus`](../apps/desktop/components/assistant-ui/tools/tool-status.tsx) 和
[`ToolIcon`](../apps/desktop/components/assistant-ui/tools/tool-icons.tsx) 的低噪声工具行几何，
但不直接复用带折叠语义的 `DisclosureRow`。必须抽出通用的 `ToolActivityRow` 作为主 Thread
运行对象行的基础组件，统一支持静态展示、点击打开、状态徽标、键盘访问以及 hover / active
状态；`DelegationToolRow` 只负责 delegation 字段读取、`child_task_id` 校验和打开动作适配。

建议的组件分层：

```text
ToolActivityRow
├─ DelegationToolRow   # v1：Agent 图标 + title + role + status + Workbench 打开
├─ TerminalToolRow     # 后续：终端命令 + 运行状态 + terminal surface
└─ ChangeSetToolRow    # 后续：变更统计 + diff surface + keep/revert 操作
```

`ToolActivityRow` 不知道 delegation、终端、ChangeSet 或 `child_task_id`；它只接收已经投影好的
`title`、`subtitle`、`leading`、`status`、`trailing`、`onOpen`、`disabled` 和 `active` 等 UI
属性。这样后续增加 surface 时不会把业务判断和 tool-specific 字段继续堆入通用行组件。

主 Thread 中不建议使用厚重卡片、持续旋转动画或嵌套完整 `Thread`。运行中可以使用轻量蓝色状态点
或短暂 pulse，但状态文本必须同时可见；Workbench Tab 被打开后，条目可显示淡蓝色 active 背景或
accent border，提示当前查看对象，不自动抢焦点或强制切换主 Thread。

`delegation_ref` 事件应在后端拿到 `child_task.id` 并完成 child run 创建后立即发出；前端收到
事件前，条目保持不可打开或显示“正在准备”；收到后才启用 Workbench 打开动作。这样不会把
尚未存在的 task 当作可用目标。

应为 `users` 注册语义明确的图标；图标只是视觉提示，状态仍以 `ToolObservation.status` / 后端投影为准，不由前端自行推断。

建议点击策略为“点击打开或聚焦”，不自动抢焦点、不自动切换 Workbench Tab。这样主 Agent 继续运行时，用户的阅读位置不会被打断。

### 5.2 只读 Agent surface

`AgentRunSurface` 不是主 Agent Thread 的一个特殊展开状态，而是一个以 `childTaskId` 为身份的独立只读 Assistant Transport runtime。这样每个子 Agent Tab 都可以像主 Agent 一样接收 snapshot/mutation 流，同时不会改变主 task 的 runtime、当前消息位置或 composer。

推荐的数据流如下：

```text
Workbench Agent Tab
        │
        ▼
AgentRunSurface(childTaskId)
        │
        ├─ GET /tasks/{childTaskId}/assistant/state  # 首屏 canonical snapshot
        ├─ active 时 attach SSE                    # 只订阅，不发送业务命令
        ├─ Assistant Transport snapshot/mutation
        ├─ child tool parts 继续接收其运行期 tool events
        └─ 复用 transport-view-converter / converter
                │
                ▼
        ReadonlyThreadProvider
                │
                ▼
        与主 Agent 相同的消息、reasoning、tool-call 流式渲染
```

运行时实现应从现有 [`useTaskAssistantTransportRuntime`](../apps/desktop/lib/assistant/use-task-assistant-transport-runtime.ts) 和 [`useRuntimeTransport`](../apps/desktop/components/assistant/runtime/use-runtime-transport.ts) 中抽取共享的 Transport 装配能力，但不要直接复用完整的 `AssistantRuntimeSession`。后者包含 composer、编辑恢复、取消、业务 resume 和主 task 状态桥接，会把只读 surface 与主 Agent 行为耦合在一起。

新增的只读 runtime 只允许：

   - 读取 `child_task_id` 对应的 child task state；state 返回当前 `current_run_id` 后，再决定是否 attach；
- 对仍为 `pending/running` 的 child run 发送一次空命令 attach/resume 请求；
- 接收 Assistant Transport 流并调用现有 converter；
- 在流断开后按 bounded retry 重新读取 snapshot 或重新 attach。

它不应配置新的 Agent 执行入口，也不应发送 `add-message`、edit、cancel 等业务命令。`ReadonlyAttachBridge` 应在 `AssistantRuntimeProvider` 下、`ReadonlyThreadProvider` 外部负责订阅；如果现有 runtime 没有公开的 attach-only 能力，则通过公开的外部 store/runtime 组合实现，禁止依赖未公开的 runtime 内部 API。这样只读 provider 不会阻断内部订阅，同时用户界面仍然没有任何可变操作。

`AgentRunSurface` 只提供：

- 子 Agent title、生命周期状态、开始/结束时间等元数据；
- 子 Agent 的只读消息和工具过程；
- 运行中、断线、后端重启、失败、过期等明确状态；
- “重新同步”或“关闭标签”这类视图动作。

明确不提供：

- composer、发送消息、编辑、分支、重试、取消；
- 直接改写 child context；
- 直接读本地文件或连接 PTY；
- 通过 assistant-ui runtime state 反向写回后端事实。

优先实现为只读 runtime + `ReadonlyThreadProvider` + 共享的 snapshot-to-`ThreadMessage[]` 转换器。`ReadonlyThreadProvider` 接收 child runtime 当前转换出的 `messages`，自身不负责 SSE；attach bridge 必须位于只读 provider 外部。不要把一个完整可交互 `Thread` 隐藏在 Tab 中，否则后续很容易出现“只读界面实际能发送/取消”的权限和状态问题。

当前项目的 Assistant Transport 是 state streaming：后端发送 snapshot mutation，前端 converter 将完整状态投影为 assistant-ui 消息。它不是只把模型 token 直接拼成一段文本。因此子 Agent 应复用同一套 `TransportState`、状态校验、tool renderer 和 message converter；不要为 child 另造 token/delta 协议。

如果现有 Assistant SSE 能被独立的只读 client 复用，则 surface 直接 attach child task 的已有流；如果现有 runtime transport 强绑定 composer 或当前 task，则应在前端增加 `agent-run` 专用 adapter，而不是复制一套 Assistant runtime。

### 5.3 标签页生命周期

- 去重键：子 Agent 使用 `agent-task:<childTaskId>`，不要使用 title 或短暂的 run 状态。
- Tab scope：建议按 workspace 保留，而不是按当前主 task 保留；切换主 task 时，已打开的子 Agent Tab 不消失。
- 初次打开：先渲染 metadata/loading，再读取 snapshot；不能因慢请求阻塞主 Thread。
- 运行中：只有当前激活的 Agent Tab 保持完整 SSE；以 revision / sequence 串行应用更新，乱序或重复事件被丢弃。
- 非激活 Agent Tab：释放完整 SSE；保留 Tab 引用和上次可见状态，切回时重新读取 snapshot，若仍 active 再重新 attach。
- 关闭：释放订阅和 surface 资源，但不取消 backend Run。
- 再打开：从 canonical snapshot 和增量游标恢复，不依赖组件之前是否挂载。
- 后端重启：重新读取状态；若后端已按架构收敛为 failed/cancelled，展示该事实，不自动 resume。
- child task 被删除：Tab 进入 stale/fallback，并给出关闭动作，不崩溃、不访问悬空引用。

## 6. Workbench 布局和多 Tab 设计

Workbench host 建议使用成熟的可调整分栏实现（assistant-ui 官方示例使用 `react-resizable-panels` 封装的 `ResizablePanelGroup`），标签栏使用受控 Tabs：

- panel 默认右侧展开，支持拖拽宽度、收起和重新打开；
- Tab 使用 `variant="line"` 风格，active tab 有明确指示；
- Tab 标题超长时截断，完整 title 通过 tooltip 和无障碍 label 提供；
- 运行中显示轻量状态点，失败显示错误状态，不能用动画代替具体状态文本；
- Tab close button 与 tab selection 分离，避免点击关闭误触发切换；
- 至少支持 `Ctrl/Cmd+W` 关闭当前 Tab、`Ctrl/Cmd+Tab` 切换 Tab、键盘遍历和焦点可见；
- 没有 Tab 时显示说明性空态，不显示伪造的运行内容；
- 多个 Tab 的内容不能共享可变的“当前 task”变量，所有 surface 都必须通过自己的 `SurfaceRef` 定位。

是否 keep-alive 由 surface capability 决定：Agent surface 以可重连为默认，不依赖 keep-alive；终端 surface 可以保留连接游标和 ring buffer；Diff surface 读取稳定 change set 后可卸载重建。不要为了视觉方便让所有 Tab 永久保留 SSE/WebSocket。

## 7. 前端数据流

```text
delegate_task tool part
        │
        │ 可见只投影 status / title / AgentProfile.role
        │ ToolCallRuntimeUpdateEvent(delegation_ref)
        │ data: child_task_id / role（不渲染）
        ▼
DelegationToolRow ── click ──▶ WorkbenchStore.open({
                                  kind: "agent-run",
                                  taskId: child_task_id
                                })
                                      │
                                      ▼
                              SurfaceRegistry
                                      │
                    ┌─────────────────┼─────────────────┐
                    ▼                 ▼                 ▼
             Agent adapter     Terminal adapter     Diff adapter
                    │                 │                 │
             state + SSE       replay + WebSocket   changes API
                    │                 │                 │
                    ▼                 ▼                 ▼
             ReadonlyThread     terminal preview     diff viewer
```

规则：

1. 主 tool row 的可见投影只包含 `status`、`title`、`AgentProfile.role`；`child_task_id` 仅作为不渲染的机器定位字段传递，点击后直接定位 child task，不进入可见文案，也不需要独立 resolver。
2. Workbench UI 状态和后端业务状态分离。`activeTabId` 不写入 SQLite，也不伪装成 Run 状态。
3. 所有 HTTP 请求经过 [`lib/http/client.ts`](../apps/desktop/lib/http/client.ts)，遵循动态 backend base URL、`no-store`、`X-Trace-Id` 和统一错误处理。
4. 前端订阅断开只是订阅中断，不取消后端 Run；只有 active Agent Tab 保持完整 SSE，非 active Tab 切回时执行 snapshot + attach/resync。
5. 第一版只消费 `ToolCallRuntimeUpdateEvent(kind="delegation_ref")`；终端输出和 ChangeSet 更新事件仅预留，不在本轮接线。
6. 关键打开、关闭、订阅、重连和未知 surface 事件使用 `frontendLog`，日志中不记录 prompt、token、凭据或大段模型输出。

## 8. 后端改动边界：第一版实施边界与后续预留

### 8.1 满足“运行中即可点击查看”所需的最小后端能力

当前代码事实表明，单靠现有前端不能可靠满足完整目标：委派运行开始时，父 tool part 没有可直接使用的 child 定位能力，而当前 `delegation_display` 主要在完成后生成结果。现在采用通用的 `ToolCallRuntimeUpdateEvent`，第一版只实现 `delegation_ref`。

**第一版：`delegation_ref` 运行期事件**

- `DelegationExecutor` 创建 child task 后，`child_task.id` 就是子 Agent 的 `taskId`；随后现有流程继续创建 child run，并把该 id 写入 `DelegationRecord.child_task_id`；
- child run 创建完成后，后端发送一条 `ToolCallRuntimeUpdateEvent(kind="delegation_ref")`，事件的严格 `data` 携带 `child_task_id`、`title` 和当前 `AgentProfile.role`；
- 父 tool part 的可见投影仍只包含 `status`、`title` 和 `AgentProfile.role`；`child_task_id` 只是随事件/投影传输的机器字段，不显示为文案，也不参与状态推断；
- 事件通过当前父 task 的 Assistant Transport snapshot/SSE 发给前端；前端收到后启用点击打开，并直接请求 `/tasks/{child_task_id}/assistant/state`；
- state 返回当前 `current_run_id` 后，若仍为 `pending/running`，再以 `commands: []`、目标 `runId` 请求 `/tasks/{child_task_id}/assistant/attach`；这是纯订阅已有 Run，不得调用 `/assistant`，也不属于业务 resume；子 Agent 内容继续复用现有 converter 和只读 runtime；
- 不新增 `DelegationRecord.tool_call_id`，不建立独立 resolver/table，不改变 delegation 状态机，也不把完整 child transcript 塞回父 tool part；冷读由现有 snapshot rebuilder 在同一父 Run 内按完整 `child_agent_id + prompt` 唯一匹配 canonical `DelegationRecord.child_task_id`，歧义时安全跳过。

事件中的 `tool_call_id` 仍然需要存在，但只用于父 task 当前 snapshot 内的事件定向；它可以通过当前工具调用的运行时上下文传入，不作为数据库字段持久化。

**预留：`terminal_output` 和 `change_set_updated`**

统一事件外壳预留两类后续 payload，但第一版不发送：

- `terminal_output`：接收 [`LocalExecutionBackend.output_sink`](../apps/backend/app/core/tools/tool_handler/terminal/local_backend.py) 已脱敏、已限额的输出片段；当前 `ToolHandlerRunner` 的跨进程 output queue 已存在，缺口是由 [`WorkflowOperations`](../apps/backend/app/core/workflows/workflow_operations.py) 把 `output_sink` 接到事件发布器；
- `change_set_updated`：只发送 ChangeSet 已更新的 task/run/path/revision 提示，前端再调用现有 changes API 查询；不把 `before` / `after` 或完整 patch 放进运行期事件；回退仍调用现有 `/tasks/{task_id}/changes/revert`，由后端 CAS 和冲突语义裁决。

这里的事件是后端 Assistant Transport 的 UI 投影旁路，不是新的业务事实源。高频终端输出必须有界、可丢弃、按 `seq` 去重；最终工具 observation、ChangeSet 和回退事实仍由现有 canonical owner 管理。

这里的 attach 是“订阅已有 child Run”，不是重新调用 Agent。前端打开 Tab 后先读取 child snapshot；若 snapshot 的当前 Run 为 `pending/running`，再发起空命令 attach，让已有的 `AssistantTransportStreamService` 持续发送状态变更。child 终态由后端 canonical Run status 决定，前端不自行结束流。

只有在现有 Assistant Transport 无法承载运行期事件时，才考虑增加独立只读查询/订阅接口。该备选仍复用现有 task/run/delegation 表和状态机，不新增 generic `workbench_surface` 表、不新增服务进程，也不引入隐式业务重放。

### 8.2 不建议的后端改法

- 不把 Workbench Tab 列表持久化成后端领域事实；这是前端 UI 状态。
- 不把完整 child transcript 塞进 parent 的 `delegation-result` display data；会放大 snapshot、SSE 和日志负担，也违反当前 display data 的投影边界。
- 不为了统一 UI 而新增一个跨 Agent/terminal/diff/web 的后端“surface”抽象；这些事实已有各自 owner。
- 不让前端绕过后端直接读取 SQLite、workspace 文件或 PTY。

## 9. 分阶段实施计划

### Phase 0：确认契约

- 确认 Workbench scope 为 workspace 级；
- 确认点击打开、不自动打开；
- 确认子 Agent Tab 是否展示完整只读 transcript；本方案默认展示；
- 已确认：父 tool part 可见只展示 `status`、`title`、`AgentProfile.role`，同时允许传递不渲染的 `child_task_id` 作为机器定位字段；
- 已确认：只有当前激活的 Agent Tab 保持完整 SSE，其他 Tab 切回时重新同步；
- 已确认：应用重启后不恢复已打开的 Workbench Tab；
- 先验证现有 Assistant attach/state 是否能作为只读 adapter；
- 已实现第 8 节所述的后端动态投影增量：子 task 创建后通过 `delegation_ref` 向父 tool part 补充不渲染的 `child_task_id`、运行期 role 和 title。

### Phase 1：前端 Workbench 基础设施（已完成）

- 建立 `lib/workbench` 类型、store、registry；
- 建立可调整右侧 panel、受控 Tabs、空态、关闭/聚焦/键盘交互；
- 用 fixture 或已有稳定引用验证多 Tab、去重、切换主 task、不误取消；
- 增加 store、registry、布局和可访问性测试。

### Phase 2：接入现有可读 surface（已完成 v1 Agent 部分）

- 建立 Diff/Terminal/Web surface 的 registry definition 和受控占位边界；本阶段不实现 terminal output/change-set 运行期事件，也不把这些 surface 计入 v1 功能交付；
- 将 `delegate_task` 改成专用紧凑 renderer；父 tool part 可见只展示 `status`、`title`、`AgentProfile.role`，点击目标直接使用不渲染的 `child_task_id`；
- 不修改后端状态机和数据库；终端输出和 ChangeSet 运行期事件仍只保留预留接口。

### Phase 3：接入运行中的子 Agent（已完成）

- 新增 `ToolCallRuntimeUpdateEvent` 及其 `conversation_event` 联合类型 / projector / 前端 transport converter 接线；第一版只允许 `kind="delegation_ref"`；
- 在 `DelegationExecutor` 创建 child task/run 后发布 `delegation_ref`，将 `child_task.id` 作为不渲染的 `child_task_id` 传给父 tool part；不增加 `DelegationRecord.tool_call_id` 或独立 resolver；
- 实现通用 `ToolActivityRow`，再实现 `DelegationToolRow` 作为 delegation 数据适配器：使用 Agent 图标、title、role、`ToolStatus` 和轻量打开 affordance；无 child id 时显示准备态并禁用打开，有 child id 后整行可聚焦、可点击；
- 实现 `AgentRunSurface` 的独立只读 runtime：首屏 state、仅 active Tab 的 attach SSE、非 active Tab 的切回同步、现有 converter、revision/sequence 串行更新、断线重连和后端重启状态收敛；
- 增加后端 contract/API 测试和前端 read-only 行为测试。

### Phase 4：终端与文件变更运行期事件（后续预留）

- 将 `LocalExecutionBackend.output_sink` 通过 `ToolExecutor` / `WorkflowOperations` 接到 `terminal_output` 事件；
- 接入 `change_set_updated` 事件，前端查询现有 ChangeSet API，并通过现有 revert API 提供回退按钮；
- 两项能力不属于第一版，不阻塞 `delegation_ref` 和子 Agent 只读流式查看。

### Phase 5：稳定性和体验收口（v1 已完成）

- 多并行 delegation、父 task 切换、Tab 重复打开、关闭后重开、后端重启、child 删除等场景；
- 日志和 trace 检查；
- Playwright 覆盖真实可见交互；对真实 Tauri IPC、动态端口和崩溃恢复仍保留桌面集成测试边界，不把现有 Vite E2E 当成完整桌面验证。

## 10. 验收标准

- 主 Thread 中的子 Agent 只显示图标、title、受控状态和打开动作，不显示 child 正文。
- 点击同一子 Agent 可打开或聚焦同一个 Workbench Tab，不产生重复 Tab。
- v1 至少可同时打开多个 Agent Tab，互不串数据；Workbench registry 必须为 terminal/diff/web 预留扩展位，但这些 surface 不属于 v1 功能验收。
- Agent Tab 完全只读：无 composer、发送、编辑、分支、取消和隐式重试。
- 主 Thread 的 delegation row 为低噪声横向运行行：左侧图标、中间 title/role、右侧状态和打开提示，不嵌套完整 child Thread。
- delegation row 在 `pending/running/completed/failed/cancelled` 下有稳定、可读且不依赖动画的状态表现，并具备键盘焦点与 `Enter` / `Space` 行为。
- 主 Thread 继续运行不受 Tab 打开、切换、关闭影响。
- 断线、刷新、切换 workspace/task 后，能够按稳定引用恢复、重连或清晰标记 stale。
- 只有当前激活的 Agent Tab 保持完整 SSE；其他 Agent Tab 切回时重新读取 snapshot，必要时再 attach。
- 父 tool part 的可见投影严格只有 `status`、`title`、`AgentProfile.role`；`child_task_id` 仅作为不渲染的机器定位字段，不能包含 prompt 或 child transcript。
- 第一版运行期事件只包含 `delegation_ref`；不得以第一版事件传递终端输出、完整 diff、文件 before/after 或回退指令。
- 后端崩溃恢复遵循现有状态收敛规则，不自动重放旧 Run。
- parent display data 不携带 prompt、完整 child transcript、原始异常、凭据或大段工具输出；允许携带不渲染的 `child_task_id`，仅用于打开 child Agent surface。
- 子 Agent 不进入左侧 Fork task tree；Workbench 与 task 导航职责清晰分离。
- 新增一种 surface 时只需注册 adapter/renderer，不修改 Workbench host 的大段条件分支。

### 10.1 本次审查补充的验收门槛

现有条目能够覆盖产品主路径，但还不足以证明实现可以在断线、刷新、并发委派和后端重启后稳定工作。以下门槛与上面的验收标准同等有效：

#### A. 协议、投影和数据完整性

- `delegation_ref` 必须使用严格的判别式协议：事件的 `task_id`、`run_id`、`tool_call_id`、`seq`、`kind` 和 `data.child_task_id` 类型及取值均经过后端和前端校验；`child_task_id` 必须是正整数，`role` 必须是来自目标 `AgentProfile.role` 的非空字符串。
- 必须明确并测试唯一的边界转换：后端内部 `ConversationEvent` 如何通过 projector 变成 snapshot mutation，mutation 如何进入前端 `TransportToolCallPart`；不得额外建立一条未定义的原始事件 SSE。`child_task_id` 的最终外部字段位置、snake_case/camelCase 形式、严格允许字段集和冷读形状必须在 backend schema、snapshot validator、TypeScript contract、converter 中保持一致。
- 事件只能更新同一父 `task_id + run_id + tool_call_id` 对应的 tool part。错误 task、run 或 tool call 的事件不得创建新 part、修改其他委派，也不得使父 Run 失败。
- 同一 `delegation_ref` 重复投递必须幂等；乱序、旧 `seq` 或旧 backend generation 的事件不得覆盖更新后的 locator 或状态。第一版虽然只有一次 locator 事件，也必须完成这项边界测试，为后续高频事件保留正确语义。
- 运行期事件未被订阅者收到、前端刷新、后端重启或 snapshot 首次冷读时，若 canonical `DelegationRecord` 已有 `child_task_id`，父 tool row 仍必须能够在不猜测 title/role/数组顺序的情况下打开对应 child task。不能把“事件曾经成功推送过”作为唯一可恢复条件。
- 父 tool part 的可见字段必须通过显式 allowlist 读取，只渲染 `status`、`title`、`AgentProfile.role`；不得把 `display_data` 或未知字段整体 spread 到 DOM。prompt、`delegation_id`、`child_run_id`、原始错误、凭据和 child transcript 不得进入可见 UI 或诊断日志。
- role 的来源必须可追溯：不得从 `child_agent_id`、title、prompt 或前端默认值推断。role 缺失或 profile 不可用时必须显示受控的“角色未知”状态，不得崩溃或展示内部标识。

#### B. 子 Agent 流式和订阅生命周期

- 在 child Run 仍为 `pending/running` 时打开 Tab，能够看到与主 Agent 相同的 text、reasoning、tool-call 状态和增量内容；同一消息在多次 snapshot/mutation 后不重复、不丢字，最终结果与 `GET /tasks/{child_task_id}/assistant/state` 一致。
- “只有 active Agent Tab 保持完整 SSE”必须可观测地成立：主 Thread 的 SSE 不计入该规则；任意时刻最多存在一个 active child Agent SSE。切到 terminal/diff、关闭 Tab、切换 workspace 或没有 Agent Tab 时，child SSE 必须释放；切回时才重新执行 snapshot + attach。
- 快速执行 `Agent A → Agent B → Agent A` 的切换测试时，旧订阅即使晚到事件，也不能覆盖当前 active Tab 的 snapshot、状态或消息；每个 surface 的订阅必须具备取消、清理和 generation/sequence 防护。
- 断线、页面刷新和切回 Tab 只能重新读取 snapshot 并 attach 已存在的 Run；不得调用 `/assistant` 创建新业务 Run，不得隐式 resume、重放或取消 child Run。重连必须有界，超过预算后展示可操作的“重新同步/连接失败”状态。
- child Run 完成、失败、取消、interrupted 或后端重启收敛后，Tab 能停止 SSE 并展示 canonical 终态；后端重启后不得自动重放旧执行。若 child task 被删除或 state 返回 404，Tab 进入可关闭的 stale/fallback 状态。

#### C. 只读和界面隔离

- `AgentRunSurface` 不仅不能显示 composer；其子消息中的所有 tool renderer 也必须处于只读模式，不得出现发送、编辑、分支、取消、重试、审批提交、文件 keep/revert 或终端输入等会改变后端状态的控件。
- 只读验收应通过网络断言验证：打开、切换、重连和关闭 Agent Tab 不产生 `POST /assistant`、`/runs/{id}/cancel`、编辑/分支或其他业务写请求；允许的请求仅为 state、attach 和只读资源读取。
- 主 Thread 在 child Tab 打开、切换、关闭和重连期间继续生成和渲染，不改变当前消息位置、composer 状态或主 Run；关闭视图不能取消后端 delegation。
- `ToolActivityRow` 必须可以脱离 delegation 单元测试：它只接受已投影的 UI props，不依赖 `child_task_id`、delegation service 或任何领域对象；`DelegationToolRow` 只负责字段适配和打开动作。

#### D. Workbench、多任务和进程边界

- Workbench Tab 的作用域必须明确为 workspace 级：切换同一 workspace 的主 task 不丢失已打开 Tab；切换到另一个 workspace 不得显示前一个 workspace 的 Tab、消息、状态或 surface 引用。
- 同一 child task 从多个父 task 或多个 row 打开时，只生成一个 `agent-task:<childTaskId>` Tab；不同 child task、不同 surface kind 之间不得共享可变的 current task/run 变量。
- Tab 关闭只释放前端订阅和 surface 资源；Workbench panel 的收起、展开、拖拽和空态不影响后端进程、Agent Run、终端 session 或文件事实。
- 应用重启后不恢复已打开的 Workbench Tab；但重新加载主 Thread 后，历史 delegation row 仍必须可从 canonical 数据获得 locator。不得通过 localStorage 恢复 Tab 内容或任务事实。
- backend 不可用、state/attach 返回可恢复错误、未知 surface kind 或异常 snapshot 时，Workbench 显示局部 fallback/错误，不得卸载或打崩主 Thread；所有请求仍经过动态 backend base URL 和统一 trace/logging 边界。

#### E. 测试和工程门禁

- 后端至少覆盖：事件判别式校验、错误 task/run/tool 定向、重复/乱序事件、snapshot 严格校验、冷读重建、`DelegationRecord` 已有/缺失 child id、父 Run 终态和 backend restart 收敛。
- 前端至少覆盖：`ToolActivityRow` 状态和键盘交互、delegation row allowlist、无 child id 禁用、同 child 去重、active-only SSE、切换竞态、重连不写业务、404/stale、跨 workspace 隔离和未知 surface fallback。
- 至少有一条真实可见的 Playwright 流程验证“主 Thread 委派 → row 出现 → 点击 → Workbench Agent Tab → 流式更新 → 切换/关闭 → 重新打开”；不能只用 fixture 证明 store 行为。
- 实现完成前通过项目现有的 TypeScript build、ESLint、前端 unit test，以及后端针对性 pytest/ruff/mypy 门禁；新增依赖必须进入锁文件并验证 Windows 桌面打包路径。
- 关键打开、关闭、attach、detach、resync、stale、未知事件和未知 surface 都有结构化日志；日志不包含 prompt、token、凭据、完整 transcript 或大段 terminal output。

本次验收结果：独立只读验收 Agent 判定 PASS。前端相关 Vitest 34 项、后端定向 pytest 44 项、变更文件 ESLint/Ruff、TypeScript/Vite build 通过；真实 Playwright 主路径已通过，测试 harness 的 plugin teardown 存在独立超时，但业务测试本身报告为 `1 passed`。

### 10.2 当前仍需明确的产品/实现决策

- “应用重启不恢复 Tab”已确认，但 Workbench panel 的宽度、收起/展开状态是否作为用户偏好持久化尚未定义。建议 v1 先恢复默认 panel 状态，不恢复 Tab 内容。

已确认：`AgentProfile.role` 使用当前 profile registry 解析；profile 改名后，历史 delegation row 在冷读重建时允许显示新角色。workspace 删除时统一关闭相关 Workbench Tab；单个 child task 删除时仍进入 stale/fallback。

## 11. 已确认决策与后续范围

已确认并已实现：Workbench 为 workspace 级临时 UI；父 tool part 可见只展示 `status`、`title`、`AgentProfile.role`，其中 role 使用当前 profile registry；并可携带不渲染的 `child_task_id`；第一版只实现 `ToolCallRuntimeUpdateEvent(kind="delegation_ref")`；只有 active Agent Tab 保持完整 SSE；应用重启不恢复 Workbench Tab；workspace 删除时统一关闭相关 Workbench Tab。

已确认暂缓：`terminal_output`（包括 `LocalExecutionBackend.output_sink` 接线）和
`change_set_updated`（包括 ChangeSet 刷新与回退按钮）只做协议预留，不进入第一版实现和验收。

# 状态模型收敛 + Checkpoint 利用 改造方案（已据代码核对修正）

> 基于 2026-07-20 ~ 07-21 两轮讨论 + 完整代码核对整理。供在文档上直接批注。
> 批注约定：请在行尾或段后用 `【批注：...】` 标注意见，或删除/改写任意章节。
> 状态：草案（已据真实实现修正，待确认后进 craft mode）。

---

## 0. 背景与问题

当前执行态被多处记录，且审批同步内联、多轮历史残缺。这是"逻辑乱乱的"根源。

### 0.1 三处重复的执行态（同一事实，三份权威）

一次执行里 `TaskRecord.status`、`TurnRecord.status`、`RunRecord.status`（外加
`wait_reason` / `active_step_id` / `active_wait_id` / `interruption_reason`）**三处都在写**。
Task 的状态纯粹是被 Run/Turn 的状态**带着跑的冗余副本**——这正是"任务为什么要有状态"疑问的来源。

【批注：认同 Task 状态是冗余】

### 0.2 审批同步内联，未持久化"等待审批"

`workflow.run` 遇 `interrupt()` 时当场 resolve 并 `Command(resume=)` 继续，整段循环卡在同一调用里。
结果：没有 `waiting_for_approval` 落库状态，"关掉应用→再打开→还停在审批界面继续批"当前不支持。

【批注：工具执行审批，暂时先不实现，暂缓】
> **本轮处理**：§6 审批异步持久化**移出本轮范围**。但 `interrupt()` 机制本身保留（当前 `approval_resolver=None` 即自动批准全部工具调用），后续做审批 UI 时再接 `waiting_for_approval` 状态与 `resume_turn`。

【批注：需要支持取消turn，用户点击取消，要中止turn以及工具运行】

### 0.3 多轮历史其实是坏的（只认第一轮）

`TurnService.get_turn_for_task` 实际取的是**第一轮**（`get_first_for_task`），
list_events / replay 也只覆盖第一轮。"一个 task 下多轮对话、关掉再打开显示全部历史"现在**未真正做到**。
继续对话应该是**追加新 turn**，而非重跑第一轮。

### 0.4 代码核对发现的关键偏差（决定方案必须修正）

1. **`nodes.py` 直接写 `task.status`**（6 处）：`_model_node` 写 `cancelled`/`failed`/`completed`，
   `_tools_node` 写 `failed`，均通过 `operations.update_task_status` / `has_task_status`。
   这是"Task 状态冗余"的核心来源——这些写操作必须改写到 **turn**。
2. **`workflow.run` 用错 turn**：`turn = operations.get_turn_for_task(task.task_id)` 取的是**第一轮**
   （见 `runtime_operations.get_turn_for_task` → `turn_crud.get_first_for_task`），而非当前要跑的 turn。
   正确做法是用 `operations.get_current_turn()`（基于 `current_turn_id`）。
3. **`runner.py:215` 引用未定义的 `self._task_store`**：`RuntimeOperations(..., task_store=self._task_store, ...)`
   中 `self._task_store` 在 `AgentRuntime.__init__` 从未赋值（只有 `_task_service` / `_turn_service`）。
   这是"未完成重构"遗留的 latent `AttributeError`，本轮必须修正 `RuntimeOperations` 的接线
   （明确传入 turn store 与 task store，或对应 service）。
4. **`builder.py` 依赖 `task.status` / `task.input_text` 兜底构造假 TurnRecord**：
   `_select_turns` 在无 turn 时拼一个 `status=task.status` 的假 turn。改造后 task 不再持有执行态，
   builder 必须完全基于 turn（及 TurnMessageStore）构建上下文，删除 task 兜底路径。
5. **`RunRecord` 删除的波及面比原清单大**（见 §9）：还牵扯 `workspace_service`、`dependencies`、
   `init_schema`、`models/__init__`、`durable_model`，以及测试 `test/refactor_service_facade_test.py`
   里的 `FakeRunStore` 与级联删除断言。

---

## 1. 目标与原则

- **单一事实来源**：执行态只由 `Turn` 持有，`Task` 状态派生，删除 `RunRecord` 的第三份冗余。
- **关掉再打开可续**：同一轮内崩溃→checkpoint 续跑；跨轮继续对话→持久化每轮轨迹（TurnMessageStore）；
  审批中断→**本轮不做**，后续接 `waiting_for_approval` + `resume_turn`。
- **历史可展示**：每轮结束把 `messages` 轨迹落库，replay 从轨迹重建 transcript，而非只取 `final_text`。
- **Task 只持有用户驱动的粗粒度生命周期**（open / archived），与执行态彻底分离。

---

## 2. 推荐架构

```
Workspace  ──容器/未来沙箱·无执行态──>  Task ──lifecycle(open/archived)·execution_status=派生──>  Turn#1 ──状态机权威──> Checkpoint(thread_id=turn_id·同轮 resume)
                                                                  │                         Turn#2 ──状态机权威──> Checkpoint
                                                                  │                         Turn#1 ──> TurnMessageStore(每轮轨迹·跨轮记忆+历史)
                                                                                              Turn#2 ──> TurnMessageStore
```

职责边界：
- **Checkpoint（LangGraph）**：只负责**同轮内**断点续跑（running 的 `pending_sends`、interrupt 值）。
- **TurnMessageStore（我们自己的 SQLite）**：负责**跨轮记忆**与**历史展示**。两者不重叠。
- **Task.lifecycle**：用户驱动的 open/archived，由用户操作（归档/打开）写入，runtime 不写。

【批注：架构边界认可】

---

## 3. 状态机（新建 `TurnStatus` 枚举）

复用 `EventType` 的"稳定字符串值"写法，杜绝状态字符串漂移。

```
[*] --> pending
pending --> running: claim/开始
running --> completed: 产出最终回答
running --> failed: 异常/terminal
running --> cancelled: 用户取消
completed --> [*]
failed --> [*]
cancelled --> [*]
```

```python
class TurnStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    # WAITING_FOR_APPROVAL = "waiting_for_approval"  # 本轮不接线，审批阶段再启用
```

> 原方案的 `waiting_for_approval` 状态本轮**不纳入枚举**（YAGNI，避免未接线的死状态）；
> 待 §6 审批落地时再加回。

---

## 4. 数据模型改造

### 4.1 `TurnRecord` 吸收运行时字段（仅保留真正需要的）

RunRecord 的 4 个字段里，`wait_reason` / `active_step_id` / `active_wait_id` 都是**审批/等待相关**，
本轮审批暂缓 → 按 YAGNI **不迁移**。只把 `interruption_reason` 收进 TurnRecord，重命名为
`end_reason: str | None`（承载 cancelled / failed 的原因，与 LangGraph `interrupt()` 无关）。

`TurnRecord` 最终字段：`turn_id, task_id, input_text, status, end_reason, created_at, updated_at`。

### 4.2 删除 `RunRecord`（整条链路）

`turn_id == thread_id` 已是 1:1，run 无独立身份价值。删除并清理全部引用（见 §9 完整清单）：
`run_record.py` / `durable_crud.py` / `durable_model.py` / `init_schema.APP_MODELS` /
`models/__init__.py` / `workspace_service.run_store` / `dependencies.run_store` /
`runner.run_store` + `_mark_run_for_turn` + `_sync_run_with_turn_status` / 测试 `FakeRunStore`。

【批注：RunRecord 直接删】

### 4.3 `Task` 状态改为「生命周期 + 派生执行态」

- **`TaskRecord.status` 语义变更**：由"执行态"改为"用户生命周期"——`"open"`（默认）/ `"archived"`。
  复用同一列，避免新增迁移列（最小改动）。
- **`TaskService` 移除执行态写入**：删 `update_status` 的执行用法；新增
  `set_lifecycle_status(task_id, "open"|"archived")`（仅用户归档/打开调用）。
- **新增 `task_display_status(task_id) -> str`**：从最新/活跃 turn 派生执行态
  （running→"active"；completed/failed/cancelled→对应；无 turn→"empty"）。列表/详情读派生值。
- **API 序列化**：`TaskRecord.to_dict()` 返回 `status`（生命周期）+ 新增 `execution_status`（派生）。
  `get_task` 端点同时返回两者。

【批注：Task 可以有粗粒度状态吧，打开、归档。】

---

## 5. 跨轮记忆（核心改动）

当前 `build_messages` 只注入每轮 `input_text`，导致"继续对话"失忆（模型看不到上一轮回答了什么、调了什么工具）。

改动：
1. **每轮结束落库轨迹**：turn 完成时，把 checkpoint `channel_values["messages"]` 完整轨迹写入
   `TurnMessageStore`（`turn_id` 维度，`sequence` 有序，`role` + `content_text` + 可选 tool 元数据）。
2. **`build_messages` 拼回历史**：下一轮构建时，加载该 task 下**所有前置 turn 的轨迹**
   （来自 TurnMessageStore）+ 当前 turn 的 user 输入，再 `runtime_to_langchain` 转换。
3. **`TextContextBuilder.build_messages` 签名调整**：去掉 `task` 兜底参数，改为
   `build_messages(agent_profile, current_turn, turn_history, message_store)`，完全基于 turn。

```
用户 -> AgentRuntime: 继续对话(新 turn)
AgentRuntime -> TurnMessageStore: 读前置 turns 的 messages 轨迹
TurnMessageStore -> AgentRuntime: [turn1轨迹, turn2轨迹]
AgentRuntime -> ContextBuilder: build_messages(前置轨迹 + 本轮输入)
ContextBuilder -> AgentRuntime: 完整上下文
AgentRuntime -> Checkpoint: astream(新 thread)
```

新增 `app/storage/model/turn_message_model.py` + `app/storage/crud/turn_message_crud.py`，
存 `RuntimeMessage` 列表（模型无关，复用 `runtime_to_langchain` 转换）。

---

## 6. 审批异步持久化（本轮不做 · 暂缓）

`workflow.run` 当前同步内联 resolve（遇 `interrupt()` 当场 `Command(resume=)`），
`interrupt()` 机制与自动批准（`approval_resolver=None`）**本轮保留不动**。
待后续做审批 UI 时再：落 `waiting_for_approval`、新增 `resume_turn`、重开恢复。
见 §0.2 批注。

---

## 7. 历史回放增强

`_replay_events_from_checkpoint` 当前只读 `final_text`，是阉切版。改为：
- 优先从 `TurnMessageStore` 读该 turn 的 `messages` 轨迹，重建 transcript（AI 回复 / 工具调用 / 工具结果）。
- 仅当轨迹缺失时退化为读 checkpoint `channel_values["messages"]`。
- 这样即使 checkpoint 被清理，历史仍可展示。

> 注意：`runner.list_events` / `run_task` 当前用 `get_turn_for_task`（第一轮），
> 须改为 `get_latest_turn`（见 §8 / §9）。

### 7.1 回放事件载荷契约（本轮重点补充）

回放 UI 不能只靠 `final_text` 和「工具名 + 返回」两件套。三类事实必须能从事件 `payload` 还原：
**(a) 工具不一定有名称/返回**——降级或解析失败时 `tool_name` / `result` 可能为空；
**(b) 工具需要"入参 + 工具特定明细"**——如读文件要展示「读了哪个文件、多少行」；
**(c) 预留 Human-in-Loop 与多元展示**——向用户提问、图表/组件渲染，本轮不实现但 schema 要留口。

为此统一 `RuntimeEvent.payload` 信封（所有事件类型共用，缺省字段可省略）：

```json
{
  "display_format": "text" | "component",   // 展示形态：纯文本 or 需渲染组件
  "component_type": "chart" | "diff" | "code" | "table" | "approval_prompt" | null,
  "title": "简短标题（可选，人读）",
  "summary": "一句话摘要（可选，人读，降级展示用）",
  "details": { "...": "..." },              // 结构化、机器可读明细（工具特定 / 组件数据）
  "_ext": {}                                 // 各事件类型自由扩展字段的兜底命名空间
}
```

**工具事件**（TOOL_CALL_REQUESTED / STARTED / FINISHED / OBSERVATION_ADDED）载荷约定：

```json
{
  "tool_name": "read_file" | null,   // 可能缺失（工具未解析/降级）——回放 UI 必须容忍
  "arguments": { "path": "...", "limit": 200, "offset": 0 },  // 入参展示（读文件：路径+行数）
  "result": "读成功，200 行" | null,  // 工具返回摘要，可能缺失
  "details": {                        // 工具特定明细，驱动富展示
    "path": "...", "start_line": 1, "end_line": 200, "line_count": 200
  },
  "display_format": "text",
  "summary": "读取文件 README.md（200 行）"
}
```

> 设计要点：`tool_name` / `result` 为 `Optional`，回放渲染层**不得假设其存在**；
> 真正"可读可展示"的内容来自 `summary` + `details`；`arguments` 承载入参明细。
> `details` 是开放结构，各工具 handler 自己定义其字段（如 `read_file` 的 path/line，
> `grep` 的 pattern/hit_count），回放 UI 按 `tool_name` 做差异化渲染即可，后端不耦合。

### 7.2 预留扩展点（Human-in-Loop / 多元展示）

本轮**只预留枚举成员与载荷字段，不接线、不实现 UI**（YAGNI 但按你的要求留口）：

- **Human-in-Loop 事件类型**（在 `EventType` 中新增，但 workflow/nodes 暂不 emit）：
  - `HUMAN_INPUT_REQUESTED = "human_input_requested"`：向用户提问/请求确认。
  - `HUMAN_INPUT_RECEIVED = "human_input_received"`：用户回复（续跑输入）。
  - 载荷：`{ "prompt": "...", "options": ["确认","取消"] | null, "input_type": "choice"|"free_text"|"form", "display_format": "component", "component_type": "approval_prompt", "details": {...} }`。
- **多元展示**：靠 `display_format` + `component_type` 两个字段驱动前端渲染决策
  （`text` 走纯文本；`component` 走图表/代码块/表格/审批卡等）。本轮所有事件仍 emit
  `display_format="text"`，但 schema 已支持组件渲染，后续工具（如数据分析）可直接 emit
  `component_type="chart"` 带上 `details` 里的图表数据，前端无需改协议。

> 这些预留点**不进入 §9 的运行时改动**（不 emit、不消费），仅作为 `EventType` 枚举成员
> + `payload` 信封约定存在，确保后续接 Human-in-Loop / 图表渲染时协议零改动。

---

## 8. API 层改动

- **`GET /tasks/{task_id}/turns`**：列出 task 下所有 turn（支撑多轮历史展示）。
- **取消端点改为作用于 turn**：`cancel_turn(turn_id)` 级联置该 turn 为 `cancelled`；
  task 级取消可级联到"活跃 turn"。`runner.cancel_task` 改为 `cancel_turn`，不再写 `task.status`。
- **`run_task` 语义改为**：取最新 turn；若已终态则**新建 turn** 继续对话（不再取第一轮）。
- **`get_task`**：返回 `status`（生命周期）+ `execution_status`（派生）。
- **`POST .../resume` 审批恢复端点：本轮不加**（见 §6）。

---

## 9. 文件改动清单（已据代码核对补全）

| 文件 | 改动 |
|------|------|
| `app/models/enums/turn_status.py` | **新增** `TurnStatus` 枚举 |
| `app/models/enums/event_type.py` | **预留** `HUMAN_INPUT_REQUESTED` / `HUMAN_INPUT_RECEIVED`（不 emit，仅留口） |
| `app/models/runtime_event.py` | `payload` 信封约定文档化（`display_format`/`component_type`/`summary`/`details`/`arguments`）；`to_dict` 不变 |
| `app/models/turn_record.py` | 加 `end_reason: str \| None` |
| `app/models/run_record.py` | **删除** |
| `app/models/__init__.py` | 移除 `RunRecord` 导出 |
| `app/storage/model/durable_model.py` | **删除** `DurableRunModel` |
| `app/storage/init_schema.py` | `APP_MODELS` 移除 `DurableRunModel`；新增一次性 `DROP TABLE IF EXISTS durable_runs` 清理孤儿表（§9.1） |
| `app/storage/crud/durable_crud.py` | **删除** `DurableRunStore` |
| `app/storage/model/turn_message_model.py` | **新增** 每轮消息轨迹表 |
| `app/storage/crud/turn_message_crud.py` | **新增** 每轮消息轨迹 CRUD |
| `app/storage/crud/turn_crud.py` | `get_first_for_task`→`get_latest_turn`（按 `created_at` 取最新） |
| `app/storage/crud/task_crud.py` | `update_status` 语义改为生命周期；保留 `has_status`（比生命周期） |
| `app/service/task/task_service.py` | 删执行态写入；加 `set_lifecycle_status` + `task_display_status` |
| `app/service/task/turn_service.py` | `get_turn_for_task`→`get_latest_turn`；`create_turn` 追加；加消息读写透传 |
| `app/service/task/workspace_service.py` | 移除 `run_store` 与 durable run 级联清理 |
| `app/core/context/builder.py` | `build_messages` 去掉 task 兜底，改基于 turn + TurnMessageStore |
| `app/core/runtime/runtime_operations.py` | 修正接线（明确传入 turn/task store）；`get_current_turn()`；
  `has_turn_status`/`update_turn_status`（替代 `has_task_status`/`update_task_status`）；`build_messages` 不依赖 task |
| `app/core/runtime/runner.py` | 删 `run_store`/`_mark_run_for_turn`/`_sync_run_with_turn_status`；
  `run_turn` 只写 turn；`run_task` 取最新/新建；`cancel_task`→`cancel_turn`；修正 `self._task_store` 未定义 bug |
| `app/core/workflows/react/workflow.py` | `run` 改用 `operations.get_current_turn()`（修复取第一轮 bug）；`build_messages` 基于 turn |
| `app/core/workflows/react/nodes.py` | 6 处 `update_task_status`/`has_task_status` 改为写 **turn** 状态 |
| `app/api/dependencies.py` | 移除 `DurableRunStore` 装配；`AgentRuntime`/`WorkspaceService` 不再传 `run_store` |
| `app/api/tasks_api.py` | 加 `GET .../turns`；`cancel_turn`；`get_task` 返回 `execution_status` |
| `test/refactor_service_facade_test.py` | 删 `FakeRunStore` 与 durable run 级联断言；改测 `set_lifecycle_status` / `task_display_status` / 最新 turn |

### 9.1 表级影响（删除 / 新增 / 改列 · 已据 `init_schema.APP_MODELS` 核对）

> 关键区分：`app/models/*_record.py` 是业务值对象（**不建表**）；真正建表的是
> `app/storage/model/*_model.py`（SQLModel，`__tablename__`）。故"删除 RunRecord"≠删表，
> 真正要删的表是 `DurableRunModel` 的 `durable_runs`。`RuntimeEvent` 是内存流式值对象（SSE 用），不落表。

**主库（`APP_MODELS`）：**

| 表名 | 来源 model | 本轮处理 |
|------|-----------|----------|
| `workspaces` | WorkspaceModel | 不变 |
| `tasks` | TaskModel | 不变（`status` 列**复用**，仅代码语义变 open/archived，**无 DDL**） |
| `turns` | TurnModel | **加列** `end_reason TEXT`（`ALTER TABLE ADD COLUMN`） |
| `turn_messages` | TurnMessageModel（新增） | **新增表**（加入 `APP_MODELS`） |
| `durable_runs` | DurableRunModel | **删除表**（移出 `APP_MODELS` + 一次性 DROP） |
| `trace_events` | TraceEventModel | 不变（trace 体系，非本轮范围） |
| `trace_spans` | TraceSpanModel | 不变（trace 体系，非本轮范围） |

**日志库（`LOG_MODELS`）：** `log_entries` 不变。

**新增表：** `turn_messages`（对应新增 `TurnMessageModel`，每轮消息轨迹，支撑跨轮记忆 + 回放）。

**迁移坑（已确认：直接删表）**：`init_schema` 采用"加列不删列"保守策略，本身不会 DROP 旧表。
- 全新库：`durable_runs` 不再创建，干净。
- 已有库（已存在 `durable_runs`）：该表不会被自动 DROP，会成为无人引用的孤儿表。
- **决策（2026-07-21 确认）**：在 `initialize_app_schema` 中新增一次性清理——对主库执行
  `DROP TABLE IF EXISTS durable_runs`（在 `APP_MODELS` 建表之后、列补齐之前或之后均可，
  因该表已不在 `APP_MODELS` 中，不会被重建）。这样存量库升级后 `durable_runs` 被干净移除，
  无需手动重置 `storage/*.sqlite`。

---

## 10. 执行顺序与验证（按开发规范）

1. 先落枚举与 model（`turn_status.py` / `turn_record.end_reason` / 删 `run_record` 及 durable 链路 / 新增 `turn_message` 表+crud）。最小改动、可单测。
2. 修 `RuntimeOperations` 接线（明确 turn/task store），新增 `get_current_turn` / `has_turn_status` / `update_turn_status`。
3. 改 `nodes.py` + `workflow.run`：状态写 turn、用当前 turn（修第一轮 bug）。
4. 改 `TaskService` / `turn_crud`：`set_lifecycle_status` / `task_display_status` / `get_latest_turn`。
5. 接 `TurnMessageStore` + `build_messages` 跨轮记忆；`runner` 落库轨迹。
6. 改 replay 增强 + API 端点（`turns` 列表 / `cancel_turn` / `execution_status`）。
7. 按规范走**独立审查 Agent + 独立测试 Agent** 闭环，开发 Agent 不自宣布完成。

---

## 11. 已确认决策汇总

1. Task 状态冗余 → 认同，执行态收敛到 Turn。
2. 审批异步持久化 → **暂缓**，本轮不做；`interrupt()` 机制保留。
3. 架构边界 → 认可。
4. RunRecord → **直接删除**（含 durable 全链路）；`durable_runs` 表一次性 `DROP` 清理（§9.1）。
5. Task → 保留用户驱动的粗粒度生命周期（open / archived），与执行态分离；执行态派生。
6. `waiting_for_approval` 状态本轮不纳入枚举（YAGNI）。
7. 回放事件载荷契约（§7.1）：`tool_name`/`result` 为 Optional，回放 UI 容忍缺失；
   入参放 `arguments`、工具特定明细放 `details`；`summary` 兜底人读。
8. 预留扩展点（§7.2）：`EventType` 预留 Human-in-Loop 两成员 + `payload` 信封预留
   `display_format`/`component_type`，支撑后续向用户提问与图表/组件渲染，本轮不接线。

---

## 12. Checkpoint 知识备忘（供参考，非改动项）

- LangGraph checkpoint 存的是**某个 thread（thread_id = turn_id）的完整图状态快照 + 控制元数据**，按"超级步"逐步落库。
- 不存事件流（SSE 那些 delta/step 事件），也**不跨 turn 携带历史**。
- 每张 checkpoint 含：`channel_values`（含 `messages` 整轮轨迹）、`channel_versions`、`versions_seen`、`pending_sends`（断点续跑核心）、`metadata` + `pending_writes`（interrupt 值缓存在这）。
- 物理位置：`AsyncSqliteSaver` 在 SQLite 建 3 张表：`checkpoints` / `checkpoint_writes` / `checkpoint_blobs`（长 `messages` 列表存这）。
- 关键事实 A：跨轮"继续对话"目前失忆——`build_messages` 只注入 `input_text`，没把上一轮 AI 回复/工具结果带进下一轮（§5 修复）。
- 关键事实 B：审批中断值在 checkpoint 的 `pending_writes` 里，但当前同步 resolve 没走通恢复路径（§6 后续做）。
- 关键事实 C（新增）：`runner.py:215` 的 `self._task_store` 未定义，是当前重构中间态的 latent bug，§10 第 2 步必须修正。

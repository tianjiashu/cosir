# 技术方案：状态模型收敛 + Checkpoint 利用（面向 Agent 实施）

> 阅读对象：执行本重构的 Agent（开发 Agent）。
> 本文是**实施蓝图**：只给目录落点、文件动作、职责契约、执行顺序与验证，**不含代码细节**。
> 设计背景、决策与批注见 `refactor-state-model-and-checkpoint.md`（批注稿），本文件是其落地分解。
> 状态：待执行（决策已全部闭合，可进 craft mode）。

---

## 0. 一句话目标与硬约束

**目标**：把执行态的"单一事实来源"收敛到 `Turn`；删除 `RunRecord` 及其 `durable_runs` 表；
`Task` 退化为用户驱动的粗粒度生命周期（open/archived）；每轮消息轨迹落库支撑跨轮记忆与回放；
取消作用于 turn 并中止工具运行。

**硬约束（不可违反）**：
- YAGNI：`waiting_for_approval` 状态与审批恢复（`resume_turn`）**本轮不接**，`interrupt()` 机制保留。
- 最小改动：改一行能解决的绝不改十行；不破坏现有 LangGraph checkpoint 机制；不动 `trace_*` 体系。
- 单一职责：每文件只承担其既有职责，状态收敛不引入新"上帝文件"。
- 回放事件载荷预留 Human-in-Loop 与多元展示字段，但**不接线、不实现 UI**。

---

## 1. 受影响目录总览

```
apps/backend/app/
├── models/                         # 业务层值对象（不建表）
│   ├── enums/
│   │   ├── turn_status.py          [新]  TurnStatus 枚举
│   │   └── event_type.py           [改]  预留 Human-in-Loop 两成员
│   ├── runtime_event.py            [改]  payload 信封约定文档化
│   ├── turn_record.py              [改]  加 end_reason 字段
│   ├── run_record.py               [删]
│   └── __init__.py                 [改]  移除 RunRecord 导出
├── storage/
│   ├── model/
│   │   ├── turn_model.py           [改]  turns 表加 end_reason 列
│   │   ├── turn_message_model.py   [新]  turn_messages 表（每轮轨迹）
│   │   └── durable_model.py        [删]
│   ├── crud/
│   │   ├── turn_crud.py            [改]  get_latest_turn；end_reason 映射
│   │   ├── turn_message_crud.py    [新]  轨迹 CRUD
│   │   ├── task_crud.py            [改]  update_status 语义改生命周期
│   │   └── durable_crud.py         [删]
│   └── init_schema.py              [改]  APP_MODELS 移除 + 一次性 DROP
├── service/task/
│   ├── task_service.py             [改]  删执行态写入；生命周期 + 派生
│   ├── turn_service.py             [改]  get_latest_turn；消息透传
│   └── workspace_service.py        [改]  移除 run_store 级联
├── core/
│   ├── context/builder.py          [改]  build_messages 去 task 兜底
│   ├── runtime/
│   │   ├── runtime_operations.py   [改]  修正接线；turn 状态 API
│   │   └── runner.py               [改]  删 run_store；cancel_turn 中止工具
│   └── workflows/react/
│       ├── workflow.py             [改]  run 用当前 turn
│       └── nodes.py                [改]  状态写 turn（6 处）
├── api/
│   ├── dependencies.py             [改]  移除 DurableRunStore 装配
│   ├── tasks_api.py                [改]  turns 列表；cancel_turn；execution_status
│   └── turns_api.py                [确认] SSE 订阅走 get_latest_turn
└── test/
    └── refactor_service_facade_test.py  [改]  删 FakeRunStore 与级联断言
```

> 图例：[新] 新增文件 / [改] 修改既有 / [删] 删除文件 / [确认] 仅核对无需改或极小改。

---

## 2. 分层实施规划（每文件：路径 · 动作 · 职责契约）

### 2.1 业务模型层 `app/models/`

| 路径 | 动作 | 职责契约（必须满足，不写代码） |
|------|------|-------------------------------|
| `app/models/enums/turn_status.py` | 新 | 定义 `TurnStatus(str, Enum)`：`pending/running/completed/failed/cancelled`；取值即落库字符串，杜绝拼写漂移。`waiting_for_approval` 本轮不加。 |
| `app/models/enums/event_type.py` | 改 | 新增 `HUMAN_INPUT_REQUESTED` / `HUMAN_INPUT_RECEIVED` 两个成员（值即 SSE 字符串）；本轮**只定义、不 emit**。 |
| `app/models/runtime_event.py` | 改 | 仅更新 docstring，固化 `payload` 信封约定：`display_format` / `component_type` / `title` / `summary` / `details` / `arguments` / `_ext`。`to_dict()` 行为不变。 |
| `app/models/turn_record.py` | 改 | `TurnRecord` 增加 `end_reason: str \| None`（承载 cancelled/failed 原因）。其余字段不变。 |
| `app/models/run_record.py` | 删 | 整体删除（业务值对象无引用方后移除）。 |
| `app/models/__init__.py` | 改 | 移除 `RunRecord` 导出；确认 `TurnStatus` / `EventType` 导出正确。 |

### 2.2 存储层 `app/storage/`

| 路径 | 动作 | 职责契约 |
|------|------|---------|
| `app/storage/model/turn_model.py` | 改 | `TurnModel`（`turns` 表）增加 `end_reason` 映射列（可空 Text），与 `TurnRecord.end_reason` 对应。 |
| `app/storage/model/turn_message_model.py` | 新 | 定义 `TurnMessageModel`（`turn_messages` 表）：`turn_id` / `sequence`（按 turn 有序）/ `role` / `content_text` / 可选工具元数据列（tool_call_id、tool_name 摘要等）。承载每轮消息轨迹。**必须将 `TurnMessageModel` 加入 `init_schema.APP_MODELS`**，否则 `initialize_app_schema` 不会建此表（见 §4 Phase 0）。 |
| `app/storage/model/durable_model.py` | 删 | 整体删除（`durable_runs` 表模型）。 |
| `app/storage/crud/turn_crud.py` | 改 | ① `get_first_for_task` → 新增/改为 `get_latest_turn`（按 `created_at` 取最新）；② `create_turn` 写入 `end_reason`（默认 None）；③ `_turn_from_model` 把 `end_reason` 映射回 `TurnRecord`。 |
| `app/storage/crud/turn_message_crud.py` | 新 | 按 `turn_id` 批量写入轨迹（带 sequence）、按 `turn_id` 有序读取；供 `build_messages` 与回放使用。**复用** `app/core/llm/langchain_bridge.py` 的 `runtime_to_langchain` 做 `RuntimeMessage → LangChain` 单向转换（全仓无反向函数，落库只需单向，不得凭空实现反向转换，避免重复造轮子）；工具元数据（`RuntimeMessage.metadata` 为 `dict[str, str]`）以 JSON 字符串落库、读取时反序列化。 |
| `app/storage/crud/task_crud.py` | 改 | `update_status` 语义改为"生命周期"（open/archived）；`has_status` 比生命周期；不承载执行态。 |
| `app/storage/crud/durable_crud.py` | 删 | 整体删除（`DurableRunStore`）。 |
| `app/storage/init_schema.py` | 改 | `APP_MODELS` 移除 `DurableRunModel`；在 `initialize_app_schema` 中、`APP_MODELS` 建表之后追加一次性 `DROP TABLE IF EXISTS durable_runs`（清理存量库孤儿表）。 |

### 2.3 服务层 `app/service/task/`

| 路径 | 动作 | 职责契约 |
|------|------|---------|
| `app/service/task/task_service.py` | 改 | 删除执行态写入；新增 `set_lifecycle_status(task_id, "open"\|"archived")`（仅用户归档/打开）；新增 `task_display_status(task_id) -> str` 从最新/活跃 turn 派生执行态（running→active；completed/failed/cancelled→对应；无 turn→empty）。 |
| `app/service/task/turn_service.py` | 改 | `get_turn_for_task` → `get_latest_turn`；`create_turn` 支持追加新轮；透传 `TurnMessageStore` 的读写（消息轨迹存取）。 |
| `app/service/task/workspace_service.py` | 改 | 移除 `run_store` 依赖与 durable run 的级联清理逻辑；workspace 删除级联改为 turn + task。 |

### 2.4 运行时 / 编排层 `app/core/`

| 路径 | 动作 | 职责契约 |
|------|------|---------|
| `app/core/runtime/runtime_operations.py` | 改 | 修正 `RuntimeOperations` 接线：明确传入 turn store 与 task store（修复 `self._task_store` 未定义 latent bug）；新增 `get_current_turn()`（基于 `current_turn_id`）；以 `has_turn_status` / `update_turn_status` 替代 `has_task_status` / `update_task_status`；`build_messages` 不再依赖 `task`。 |
| `app/core/runtime/runner.py` | 改 | 删除 `run_store` / `_mark_run_for_turn` / `_sync_run_with_turn_status`；`run_turn` 只写 turn 状态；`run_task` 取最新 turn、已终态则新建；`cancel_task` → `cancel_turn`，级联置 turn 为 `cancelled` 并**中止该 turn 的工具运行**（中断子进程/流，满足"取消要中止工具"批注）；修复 `self._task_store` 接线。 |
| `app/core/workflows/react/workflow.py` | 改 | `run` 改用 `operations.get_current_turn()`（修复取第一轮 bug）；`build_messages` 基于 turn + `TurnMessageStore`。 |
| `app/core/workflows/react/nodes.py` | 改 | 6 处经 `operations.update_task_status` / `has_task_status` 写 `task.status` 的逻辑，全部改写为写 **turn** 状态（经 `runtime_operations` 的 turn 状态 API）。 |
| `app/core/context/builder.py` | 改 | `build_messages` 去掉 `task.status` / `task.input_text` 兜底构造假 TurnRecord 的路径；改为完全基于 turn（及 `TurnMessageStore` 前置轨迹）+ 当前 turn 输入构建上下文。 |

### 2.5 API 层 `app/api/`

| 路径 | 动作 | 职责契约 |
|------|------|---------|
| `app/api/dependencies.py` | 改 | 移除 `DurableRunStore` 装配；`AgentRuntime` / `WorkspaceService` 不再接收 `run_store`。 |
| `app/api/tasks_api.py` | 改 | 新增 `GET /tasks/{task_id}/turns`（列出全部 turn）；`cancel_turn(turn_id)` 端点；`get_task` 返回 `status`（生命周期）+ `execution_status`（派生）。 |
| `app/api/turns_api.py` | 确认 | SSE 订阅 `/turns/{turn_id}/stream` 已基于 `RuntimeEvent`；确认 list/replay 走 `get_latest_turn` 而非第一轮，无逻辑则不改。 |

### 2.6 测试 `test/`

| 路径 | 动作 | 职责契约 |
|------|------|---------|
| `apps/backend/test/refactor_service_facade_test.py` | 改 | 删除 `FakeRunStore` 与 durable run 级联删除断言；改为验证 `set_lifecycle_status` / `task_display_status` 派生 / `get_latest_turn` 取最新轮 / 取消 turn 中止。 |

---

## 3. 关键契约（执行 Agent 必须满足，不写代码）

1. **`TurnStatus`**：`pending → running → completed|failed|cancelled`，终态不可再迁移。
2. **`turn_messages` 存储粒度**：以 `turn_id` 为维度、`sequence` 有序，存 `role` + `content_text` + 可选工具元数据；模型无关，复用 `app/core/llm/langchain_bridge.py` 的 `runtime_to_langchain` 做 `RuntimeMessage → LangChain` **单向**转换（无反向函数，不得凭空实现）；`metadata` 以 JSON 字符串落库、读取时反序列化，不重复造轮子。
3. **`Task` 派生 `execution_status` 规则**：最新/活跃 turn running→`active`；completed/failed/cancelled→对应；无 turn→`empty`。`TaskRecord.status` 仅表达 open/archived。
4. **事件 `payload` 信封**：所有事件共用；`tool_name` / `result` 为可选（回放 UI 容忍缺失）；可读内容来自 `summary` + `details`；`arguments` 承载入参；`display_format` + `component_type` 驱动前端渲染（本轮全 `text`）。
5. **`cancel_turn` 中止语义**：置 turn 为 `cancelled` 的同时，必须中断该 turn 正在运行的工具子进程 / 流式循环，避免"界面取消但后台工具仍在跑"。
6. **`durable_runs` 清理**：`init_schema` 一次性 DROP，存量库升级后无孤儿表。

---

## 4. 执行顺序（分阶段，依赖自上而下）

```
Phase 0  模型与表骨架（可单测，无运行时依赖）
  ├─ models/enums/turn_status.py            [新]
  ├─ models/turn_record.py                  [改] +end_reason
  ├─ storage/model/turn_model.py            [改] +end_reason 列
  ├─ storage/model/turn_message_model.py    [新] turn_messages 表
  ├─ storage/crud/turn_message_crud.py      [新]
  ├─ 删 run_record.py / durable_model.py / durable_crud.py
  └─ init_schema.py                         [改] APP_MODELS 移除 DurableRunModel + 加入 TurnMessageModel + 一次性 DROP durable_runs

Phase 1  接线修复与 turn 状态 API
  └─ core/runtime/runtime_operations.py     [改] 修正 store 接线；get_current_turn / has|update_turn_status

Phase 2  状态写 turn（修第一轮 bug + nodes 改写）
  ├─ core/workflows/react/nodes.py          [改] 6 处写 turn
  └─ core/workflows/react/workflow.py       [改] run 用 get_current_turn

Phase 3  Task/Workspace 收敛
  ├─ service/task/task_service.py           [改] 生命周期 + 派生
  ├─ service/task/turn_service.py           [改] get_latest_turn + 消息透传
  ├─ service/task/workspace_service.py      [改] 移除 run_store 级联
  └─ storage/crud/turn_crud.py / task_crud.py [改]

Phase 4  跨轮记忆接入
  ├─ core/context/builder.py                [改] 去 task 兜底
  └─ core/runtime/runner.py                 [改] 落库轨迹；run_turn 只写 turn

Phase 5  API 与回放增强
  ├─ api/dependencies.py                    [改]
  ├─ api/tasks_api.py                       [改] turns / cancel_turn / execution_status
  └─ api/turns_api.py                       [确认] get_latest_turn

Phase 6  测试与闭环
  └─ test/refactor_service_facade_test.py   [改]
  └─ 独立审查 Agent + 独立测试 Agent 闭环（开发 Agent 不自宣布完成）
```

依赖说明：Phase 1 必须在 Phase 2 前（nodes/workflow 依赖 turn 状态 API）；Phase 4 依赖
Phase 0 的 `turn_message` 表与 Phase 3 的 `get_latest_turn`；Phase 5 依赖前面所有 service 层改动。

---

## 5. 验证与闭环（按开发规范）

- **静态**：ruff + mypy 通过（Python 3.11，见 `MEMORY.md` 环境事实）。
- **单测/集成**：跑 `pytest`（重点 `test/refactor_service_facade_test.py` 及受影响模块），确认无回归。
  > 已知背景：仓库现存一批"未完成重构"遗留失败用例（非本方案引入）。本重构落地的模块，其相关用例应随重构转绿；若仍失败，属其他未完重构范围，不在本方案修复目标内，但需在交付说明中标注。
- **闭环**：开发完成后**必须**启独立审查 Agent + 独立测试 Agent；开发 Agent 不得自宣布完成；修复后重跑至全通过。

---

## 6. 风险与回滚

| 风险 | 缓解 / 回滚 |
|------|------------|
| 删 `durable_runs` 不可逆 | 该表是运行产物、不入库；开发期 `storage/*.sqlite` 可重置；一次性 DROP 仅清存量孤儿表，无业务数据损失。 |
| `end_reason` 加列 | 走 `init_schema` 保守 `ALTER TABLE ADD COLUMN`，可空，历史行零值，无破坏。 |
| `cancel_turn` 中止工具 | 需确保中止路径不破坏 checkpoint resume（同轮断点续跑）；中止只作用于活跃 turn 的工具子进程/流。 |
| `get_current_turn` 取错轮 | 必须基于 `current_turn_id` 而非 `get_first_for_task`，避免重演"只认第一轮"bug。 |
| 事件 `payload` 信封扩展 | 仅文档化约定 + 预留枚举，不改 `to_dict` 行为，前端兼容。 |

---

## 7. 与批注稿的对应关系

本文件是 `refactor-state-model-and-checkpoint.md` 的实施分解。批注稿中的决策编号（§11）一一映射到上文：
- 决策 1/3/5 → §2.3 Task 生命周期 + §2.4 状态写 turn
- 决策 2/6 → §0 硬约束（审批暂缓、waiting_for_approval 不接）
- 决策 4/9 → §2.2 删 durable + init_schema DROP
- 决策 7 → §3 关键契约 4（回放载荷信封）
- 决策 8 → §2.1 event_type 预留 + §3 关键契约 4（display_format/component_type）
- §0.2 批注（取消中止工具）→ §3 关键契约 5 + §2.4 runner.cancel_turn

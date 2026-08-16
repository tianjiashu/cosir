# 委派重构：父子 Task 模型实施规范

> 单一事实来源。开发/测试/审查三个独立子 Agent 共享此契约。
> 总指挥只做最终验收，不参与编码。

## 目标

将 `delegate_task` 从「父 task 下的 child turn」改造为「父子 task」：
每次委派创建 **一个子 task**（1 delegation ↔ 1 子 task ↔ 1 子 turn，1:1:1）。
子 task 拥有独立 `task_id`，天然隔离上下文（不再依赖 `context_excluded_turn_ids` hack）。

## 决策点（已拍板）

1. 引入 `tasks.task_type` 枚举：`"user"`（用户创建）/ `"delegation"`（委派子任务）。
2. 子 task **不出现在侧边栏对话列表**（`list_by_workspace` 过滤 `task_type='user'`）。
3. 子 task v1 **不允许用户续聊**（1 delegation = 1 turn）。
4. `context_excluded_turn_ids` 机制整体退役（task 边界已隔离）。
5. `delegations` 表新增 `child_task_id`（定位子 task + 前端跳转）。

## 数据模型改动

### `storage/model/task_model.py`（TaskModel）
新增字段（均 `Text`，可空）：
```python
task_type: Mapped[str] = mapped_column(Text, default="user", nullable=False)
parent_task_id: Mapped[str | None] = mapped_column(Text, ForeignKey("tasks.task_id"), nullable=True)
parent_turn_id: Mapped[str | None] = mapped_column(Text, ForeignKey("turns.turn_id"), nullable=True)
delegation_id: Mapped[str | None] = mapped_column(Text, nullable=True)  # 唯一索引兜底
```
索引（在 `__table_args__` 或 model 上声明）：
- `Index("idx_tasks_parent_task_id", "parent_task_id")`
- `Index("uq_tasks_delegation_id", "delegation_id", unique=True)`  # 并发重入兜底

> 迁移由 `init_schema.py` 的 `ALTER TABLE ADD COLUMN` 自动补齐，无需手写迁移 SQL。

### `models/task_record.py`（TaskRecord）
新增字段 `task_type: str`、`parent_task_id: str | None`、`parent_turn_id: str | None`、`delegation_id: str | None`。
新增便捷属性 `is_child: bool`（= `parent_task_id is not None`）。

### `storage/model/delegation_model.py`（DelegationModel）
新增 `child_task_id: Mapped[str | None] = mapped_column(Text, nullable=True)`。

### `models/delegation_record.py`（DelegationRecord）
新增 `child_task_id: str`（排在 `child_turn_id` 后）。

## storage/crud 改动

### `task_crud.py`
- `create(...)` 增加可选 `task_type: str = "user"`、`parent_task_id=None`、`parent_turn_id=None`、`delegation_id=None` 参数并写入。
- `list_by_workspace(workspace_id)` 增加 `WHERE task_type = "user"` 过滤（子 task 不进侧边栏）。
- 新增 `list_by_parent_task(parent_task_id: str) -> list[TaskRecord]`（展开子任务树）。
- `get(...)`、`update_latest_turn(...)`、`update_status(...)` 保持不变。
- `_task_from_model` 同步新字段。

### `delegation_crud.py`
- `update_status(...)` 增加 `child_task_id: str | None = None` 形参，传入时写入（与 `child_turn_id` 同款逻辑）。
- `_record_from_model` / `_model_values` / `_values_from_record` 同步 `child_task_id`。

## service 层改动

### `task_service.py`（TaskService）
- 新增方法 `create_child_task(*, parent_task_id, parent_turn_id, delegation_id, workspace_id, agent_id, input_text) -> TaskRecord`：
  - **只建 task，不建 turn**（turn 由委派执行器单独建）。
  - `task_type="delegation"`、`parent_task_id`/`parent_turn_id`/`delegation_id` 落库。
  - `latest_turn_id=None`、`status="open"`（生命周期，子 task 不参与 archived 交互）。
  - 校验 `agent_id` 注册（复用 `_registered_agent_ids`）。
  - **不写 title/last_message_preview**（子 task 无侧边栏展示，省去 preview 计算；或更简单：title 用 input_text preview，二者择一，须一致）。
  - 完整 docstring + 参数/返回/异常/副作用。
- `delete_task(task_id)` 改为**递归删子 task**：先 `list_by_parent_task(task_id)` 拿到子 task，对每个递归调用 `delete_task`，再删自身（保持原级联清理逻辑）。
- 提供新方法或让 `list_tasks_for_workspace` 透传 `list_by_workspace`（已过滤）。

### `turn_service.py`（TurnService）
- `create_child_turn` 的 `task_id` 改为传**子 task id**（调用方改，方法签名不变，但 docstring 同步：现在 child turn 挂在子 task 下而非父 task）。

## core/delegation 改动（主体）

### `delegation_executor.py`（DelegationExecutor.execute）
`execute` 内已有 `get_turn_service()`，需加 `get_task_service()`。
将创建 child turn 的段改为：
```python
task_service = get_task_service()
...
child_task = task_service.create_child_task(
    parent_task_id=self._parent_task.task_id,
    parent_turn_id=self._parent_turn.turn_id,
    delegation_id=acquire.delegation_id,
    workspace_id=self._parent_task.workspace_id,
    agent_id=args.child_agent_id,
    input_text=agent_input_text,
)
child_turn = turn_service.create_child_turn(
    task_id=child_task.task_id,          # ← 关键：挂在子 task 下
    input_text=agent_input_text,
    agent_id=args.child_agent_id,
    parent_turn_id=self._parent_turn.turn_id,
    delegation_id=acquire.delegation_id,
)
```
- 并发顺序必须保持：`try_create_pending`（原子 acquire 额度）→ 成功后才 `create_child_task` → `create_child_turn`（现状顺序正确，勿改）。
- `delegation_crud.update_status(..., child_turn_id=..., child_task_id=child_task.task_id)` 两处（running 与终态）都补 `child_task_id`。
- 唯一索引兜底：`create_child_task` 抛 `IntegrityError`（delegation_id 重复）时捕获，转 `tool_error`（reason 说明已存在该 delegation 的子 task，retryable=False），并记 error 日志含 `delegation_id`。

### `child_agent_profile_builder.py`（ChildAgentProfileBuilder.build）
- **删除** `context_excluded_turn_ids` 相关入参与赋值（child 现在走独立子 task，`load_history` 天然只看到自己的消息）。
- 方法签名去掉 `context_excluded_turn_ids` 参数；docstring 同步。

### `agent_profile.py`（AgentProfile）
- 删除 `context_excluded_turn_ids: tuple[str, ...] = ()` 字段及其 docstring 行。

### `workflows/react/workflow.py`
- `load_history(excluded_turn_ids=agent_profile.context_excluded_turn_ids)` 改为 `load_history()`（不传 excluded；或传 `()`）。
- 注意 188-190 行关于「子 Agent 不统计上下文圆环」的注释仍成立（判定靠 `parent_turn_id is not None`），无需改。

## API / shared / 前端

### `tasks_api.py`
- 新增 `GET /tasks/{task_id}/children` → `task_service.list_by_parent_task` 返回 `list[TaskResponse]`。
- 现有 `list_turns`、`delete_task` 无需过滤（child turn 已不在父 task 下）。

### `apps/shared/ts/task.ts`
- `TaskRecord`/`TaskResponse` 增加 `taskType`、`parentTaskId`、`parentTurnId`、`delegationId` 字段（与后端字段对齐；若由脚本生成则需同步生成脚本或手动加）。

### 前端（非阻塞，可后置）
- `ToolCallCard` 的 `tool_call_finished.data` 增加 `child_task_id`，支持从 delegate 卡片下钻查看子 task 轨迹。

## 不动的部分（红线）
- `init_schema.py` 迁移机制不动（加列自动完成）。
- `engine_cache.py` WAL/busy_timeout 已满足并发写，不动。
- 并发额度 `delegation_crud.try_create_pending` 原子 acquire 不动。
- 取消级联 `runner._cancel_active_child_turns`（见下，仅扩展 child_task_id 关联）。

## 取消级联扩展（runner.py）
- `_cancel_active_child_turns` 当前通过 `delegation_service.list_active_by_parent_turn` → `turn_service.cancel_turn_if_active(child_turn_id)`。
- 改造后 `delegation.child_task_id` 已存在，取消逻辑**逻辑不变**（仍按 child_turn_id 取消子 turn）；仅需在 `delegation_service` 的取消写入处确认 `child_task_id` 一并落库（已由 update_status 扩展覆盖）。

## 验收契约（测试 Agent 必做）
1. **上下文隔离**：父 task 下委派，验证 child 的 `load_history` 不含父 task 其他 turn 消息；父 task 历史不含 child 逐条轨迹（只回传 delegate 工具 observation）。
2. **列表过滤**：`list_tasks_for_workspace` 不含 delegation 子 task；`list_by_parent_task` 能拿到。
3. **删除级联**：删父 task 递归删子 task 及其 turns/messages/events/delegations，无孤儿。
4. **并发**：并行 N 个 delegate（N > `DELEGATION_MAX_CONCURRENCY`）时额度被拒的返回 error 且不产生孤儿 task；并行 N <= 额度时各自创建独立子 task。
5. **唯一索引**：同一 delegation_id 重入 `create_child_task` 抛 IntegrityError → tool_error（retryable=False）。
6. **取消**：取消父 turn 时子 task 的 child turn 被标记 cancelled。
7. **child_task_id 落库**：delegation 终态记录含正确 child_task_id。

## 交付约束（开发 Agent）
- 每个改动文件须 Ruff 通过、`mypy` 对改动文件无新增错误。
- 每个函数/方法 docstring 四段式（参数/返回/异常/副作用），签名变更同步更新。
- 删除 `context_excluded_turn_ids` 后须 grep 全仓确认无残留引用。
- 不得引入 `Utils/Helper/Common` 类命名。
- 提交前不得自行宣布完成（由审查+测试闭环判定）。

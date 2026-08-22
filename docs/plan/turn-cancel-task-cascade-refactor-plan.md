# 当前 Turn 取消改造方案：委派子 Agent 创建子 Task 后的级联取消

> 背景：委派子 Agent 已从「仅创建子 turn」改造为「创建子 **task**（`task_type="delegation"`），子 turn 挂在该子 task 下」（`delegation_executor.execute` 内 `create_child_task` + `turn_service.create_turn(task_id=child_task.task_id)`）。当前取消逻辑仍停留在「取消单个 child_turn」的旧模型，与第零铁律「以长期稳定迭代为尺」不一致。本文档基于代码事实，给出改造方案。

---

## 一、当前 Turn 取消的执行链路（基于代码事实）

入口与主链：

```
POST /turns/{turn_id}/cancel                     [api/turns_api.py:223 cancel_turn]
  → runtime.cancel_turn(turn_id)                 [core/runtime/runner.py:97]
      ├─ cancellation_registry.mark_cancelled(turn_id)      # 父 turn 进程内信号
      ├─ turn_service.cancel_turn_if_active(turn_id, "user_cancelled")
      ├─ self._cancel_active_child_turns(turn)              # 级联活动子 delegation
      │    └─ delegation_service.list_active_by_parent_turn(parent_turn.turn_id)
      │         （status in ACTIVE: pending/running 的 delegation）
      │    └─ for delegation: self._cancel_child_delegation(parent_turn, delegation)
      │         ├─ if delegation.child_turn_id:
      │         │    ├─ cancellation_registry.mark_cancelled(child_turn_id)   # 子 turn 信号
      │         │    ├─ turn_service.cancel_turn_if_active(child_turn_id, "parent_turn_cancelled")
      │         │    ├─ 保存+发布 child RUN_CANCELLED（task_id=child_turn.task_id）
      │         │    └─ _mark_stable_file_changes(child_turn_id)
      │         └─ delegation_service.mark_cancelled(delegation_id, "parent_turn_cancelled")
      └─ self._mark_stable_file_changes(turn_id)             # 父 turn 快照收口
```

子 Agent 运行线程侧的取消探测：

```
delegate_task 工具（thread 执行模式）→ DelegationExecutor.execute
  → ChildAgentRunner.run_child(child_profile)               [core/delegation/child_agent_runner.py:41]
       → asyncio.run(self._consume_child_events(...))        # 在工具线程跑
            → 每次迭代前 self._should_cancel(child_turn_id)
                 = cancellation_registry.is_cancelled(child_turn_id)
            → 命中 → DelegationResult(status="cancelled")
  → _finalize_result → delegation_service.mark_cancelled + tool_cancelled observation
```

**信号闭环事实**：`ChildAgentRunner` 的 `should_cancel` 绑定 `child_profile.turn.turn_id`；`_cancel_child_delegation` 标记的是 `delegation.child_turn_id`。两者指向同一 child turn（`create_turn(task_id=child_task.task_id)` 产出的 turn），因此线程侧信号能正确命中，父取消能中止当前正在跑的 child turn。

---

## 二、与第零铁律不一致的具体代码位置

| # | 位置 | 现状 | 与第零铁律的冲突 |
|---|------|------|------------------|
| 1 | `runner.py:214` `_cancel_child_delegation` | 仅对 `delegation.child_turn_id` 做取消（标记信号 + `cancel_turn_if_active` + RUN_CANCELLED + 快照收口）。**从不更新子 task 状态**。 | 取消单元错位：子 Agent 的可运行单元已是「子 task」，但取消仍只作用于单个 turn。子 task 记录残留 `pending`，语义不完整，且未来子 task 若承载多 turn 将无法整树回收。 |
| 2 | `runner.py:173` `_cancel_active_child_turns` | 只按 `parent_turn_id` 查 `list_active_by_parent_turn` 一层。 | 仅覆盖「当前 turn 直接派发的子 delegation」。若子 task 自己再派发（其 child delegation 的 `parent_turn_id` 是子 task 内的 turn，不是当前 turn），本层查不到，**嵌套不闭环**。 |
| 3 | `runner.py:230` `delegation_service.mark_cancelled(...)` | 只标记 delegation 记录终态。 | 子 task（`child_task_id`）状态不回收；delegation 已落 `child_task_id`（`mark_child_started` 写入），但取消路径没利用它去把子 task 一并置 cancelled。 |
| 4 | `runner.py:216` `turn_service.cancel_turn_if_active(child_turn_id, ...)` | 只取消单个 turn。 | 子 task 未来如果有多 turn（长生命周期），取消首个 turn 后其余 turn 可无阻碍启动，违背「整树回收」语义。 |
| 5 | `delegation_policy.py:39` | `effective_tools = child_allowed_tools - {"delegate_task"}` 已阻止子递归委派（depth 上限）。 | 当前已挡死嵌套派发，**所以 2 的缺陷当前不会触发**——但这是「靠策略挡，而非取消链路健壮」。策略一旦放开（第零铁律允许演进），取消链路就漏。应让取消链路本身具备整树级联能力。 |

> 结论：核心不一致是 **1/2/3** —— 取消单元从「turn」升级到「task」后，取消动作、状态回收、级联遍历三处都还没跟上。

---

## 三、改造方案

### 3.1 新增/修改的函数与方法

**A. `core/runtime/runner.py`（主改造点）**

新增一个 **task 级级联取消** 方法，替代当前 turn 级逻辑：

```python
def _cancel_task_tree(self, task_id: str, reason: str) -> None:
    """取消一个 task 及其整棵后代 task 树的全部活动 turn 与 delegation。

    以 task 为取消单元，双维度遍历：①取消该 task 下所有 active turn（标记进程内信号 +
    条件置 cancelled + 发 RUN_CANCELLED + 快照收口）；②对每个 turn 扫其 active
    delegation，有 child_task 者递归其子 task 树，无者仅落 delegation 终态，
    保证整棵委派树（含纯 pending delegation 窗口）被回收。

    参数:
        task_id: 待取消的 task 标识（用户 task 或 delegation 子 task 均可）。
        reason: 取消原因（"user_cancelled" / "parent_turn_cancelled"）。

    返回:
        无。

    异常:
        无。级联取消失败逐层记日志并继续，不中断上层流程。

    副作用:
        标记若干 turn 的进程内取消信号、更新若干 turn 为 cancelled、
        持久化 RUN_CANCELLED 事件、收口对应文件快照、更新 delegation 终态。
    """
```

内部实现要点（**两个遍历维度并行**，缺一不可）：

1. 先 `task_service.get_task(task_id)` 取该 task 记录，读取其 `delegation_id` / `parent_task_id`（该 task 若为 delegation 子 task，用于 `mark_cancelled` 对齐 delegation 记录）。
2. **turn 维度 + 该 turn 下的 active delegation 维度**：
   - 取该 task 全部 turn（`turn_service.list_turns_for_task(task_id)`，`turn_service.py:106` 已存在）。
   - 对每个 **active turn**（pending/running）：
     - `cancellation_registry.mark_cancelled(turn_id)` → `turn_service.cancel_turn_if_active(turn_id, reason)` → 发 RUN_CANCELLED → `_mark_stable_file_changes(turn_id)`。
     - 并扫该 turn 下的 **active delegation**（`delegation_service.list_active_by_parent_turn(turn_id)`，保留旧逻辑覆盖「已建 delegation 未建子 task」的纯 pending 窗口）：
       - 若 delegation 已有 `child_task_id`：递归 `_cancel_task_tree(child_task_id, reason)`。
       - 若 delegation 尚无 `child_task_id`（纯 pending，未执行 `create_child_task`）：仅 `delegation_service.mark_cancelled(delegation_id, reason)` 落终态，无子 task 可递归。
3. 该 task 若存在 `delegation_id`，一并 `delegation_service.mark_cancelled(delegation_id, reason, child_task_id=task_id)`，保持 delegation 记录终态一致（与点 2 的子树递归配合，避免漏掉当前 task 自身这条 delegation 边）。

> 用「task 树递归 + 每 turn 的 active delegation 扫描」双维度统一了 turn 级取消（父 turn 本身就是其 task 的 turn）与 task 级取消，避免两套逻辑；同时保留按 `parent_turn_id` 扫 delegation 的能力，覆盖纯 pending delegation 窗口。
>
> **语义声明**：级联到「整棵 task 树」= 同 task 全部 active turn + 该 task 全部子 task 树（递归）。对用户 task 可含多个 turn（重跑/历史轮次）的场景，取消当前 turn 会连带中止同 task 下其它**正在运行或 pending 的兄弟 turn**——这是「取消单元从 turn 升级到 task」的刻意设计，需在接受范围内；若后续要求「只取消当前 turn 派生的子树」，可在调用方限定遍历起点（见 §3.1-B 说明）。

**B. `runner.py` 修改 `cancel_turn` / `_cancel_active_child_turns` / `_cancel_child_delegation`**

- `cancel_turn(turn_id)` 取消当前 turn 后，把「级联子 delegation」改为「级联子 **task 树**」：

```python
# 旧：self._cancel_active_child_turns(turn)          # 按 parent_turn_id 查一层
# 新：self._cancel_task_tree(turn.task_id, "parent_turn_cancelled")
#     当前 turn 自身已取消（cancel_turn 主链已处理），此处只需级联其所属 task 下的
#     其它 turn + 子 task 树；为保证幂等，_cancel_task_tree 对已 cancelled turn 是空操作。
```

- **不删除 `_cancel_active_child_turns` 的 delegation 扫描能力**：其按 `parent_turn_id` 扫 active delegation 的逻辑被吸收进 `_cancel_task_tree` 点 2 的「每 turn active delegation 扫描」步骤（见 §3.1-A），用于覆盖「已建 delegation、但尚未执行 `create_child_task`」的纯 pending 窗口——该窗口在 task 树中无节点，若完全删除旧扫描将永远发现不了它，导致 delegation 永久滞留 `pending`。因此：
  - `_cancel_active_child_turns` 作为独立方法删除，但其**按 parent_turn 扫 active delegation** 的语义由 `_cancel_task_tree` 内联保留。
  - `_cancel_child_delegation` 按 child_turn_id 取消的逻辑并入 `_cancel_task_tree`（子 task 的取消即 delegation 的取消，二者合一）。
- **调用起点限制（避免取消范围过大的可选项）**：`_cancel_task_tree` 默认从 `turn.task_id` 出发遍历整棵 task 树（含同 task 兄弟 turn，见 §3.1-A 语义声明）。若后续需要「只取消当前 turn 派生的子树」，可在 `cancel_turn` 处限定遍历起点为该 turn 的 `delegation` 子 task（`delegation_service.list_active_by_parent_turn(turn_id)` 命中 `child_task_id` 的子树），而非整个 `turn.task_id` 树——本方案默认采用整棵 task 树（语义更完整），此限制作为演进预留。

**C. `service/task/turn_service.py`（复用，不新增）**

已确认 `TurnService.list_turns_for_task(task_id)`（`turn_service.py:106`）即为「按 task 列全部 turn」，runner 的 `_cancel_task_tree` 直接复用，避免重复造轮子，无需在 `task_service` 新增方法。

**C'. `storage/crud/delegation_crud.py`（修复语义，可选增强）**

已确认 `delegation_crud.update_status`（`delegation_crud.py:126`）是**无条件覆盖** status（无 `WHERE status IN (...)` 守卫）。这对「部分子 task 已完成」场景不安全：若某 delegation 已 `completed`，父取消时 `mark_cancelled` 会把已完成的 delegation 覆写为 `cancelled`，改写历史终态。改造时需二选一：
- **方案 1（推荐，对齐 `turn_crud.cancel_if_active`）**：给 `update_status` 增加可选 `expected_statuses` 参数（默认 None=无条件），`mark_cancelled` 传 `ACTIVE_DELEGATION_STATUSES`（pending/running），已完成/已失败 delegation 不覆写。
- **方案 2（最小改动）**：在 `runner._cancel_task_tree` 里先查 delegation 当前 status，仅当 active 才 `mark_cancelled`。

**D. `service/delegation/delegation_service.py`**

- `mark_cancelled(delegation_id, error, child_task_id=None, ...)`（`delegation_service.py:347`）第二位置参数名是 `error`（接收取消原因并写入 `delegations.error` 字段），方案各处写的 `mark_cancelled(delegation_id, reason)` 是位置传参、可运行；实现时**勿**把 `reason` 当关键字参数（不存在 `reason=` 形参），取消原因作为第二个位置实参传入即可。无需改签名。
- 确认其 `update_status` 内部支持把 delegation 终态与 child_task_id 一并落库（现状已支持，见 `delegation_crud.update_status`）。

### 3.2 各层调用关系（改造后）

```
POST /turns/{turn_id}/cancel  (api/turns_api.cancel_turn)
  → AgentRuntime.cancel_turn(turn_id)                     [api → core]
      ├─ cancellation_registry.mark_cancelled(turn_id)         [core 内部]
      ├─ turn_service.cancel_turn_if_active(turn_id, ...)      [core → service]
      ├─ self._cancel_task_tree(turn.task_id, "parent_turn_cancelled")   [新增，core 内部]
      │    ├─ 取本 task 全部 turn（turn_service.list_turns_for_task）    [core → service]
      │    ├─ 取消每个 active turn + 扫其 active delegation
      │    │     （delegation_service.list_active_by_parent_turn）
      │    │      └─ 有 child_task_id → 递归 _cancel_task_tree(child_task_id, ...)
      │    │      └─ 无 child_task_id（纯 pending）→ 仅 mark_cancelled
      │    └─ 本 task 有 delegation_id → mark_cancelled(delegation_id, ...)
      └─ self._mark_stable_file_changes(turn_id)
```

> 遍历统一由 **delegation 表驱动**（父 turn → delegation → child_task → 子 turn → 其 delegation → …），不使用 `task_service.list_child_tasks` 二次遍历，避免「全量子 task 列出」与「仅 active delegation 递归」两套机制不一致（子 task 由 `create_child_task` 创建，`parent_task_id` 与 delegation 的 `child_task_id` 指向同一子 task，delegation 驱动即可完整覆盖整棵树，且天然跳过已完成子树）。

子 Agent 线程侧探测不变（信号仍由 `cancellation_registry` 提供，`ChildAgentRunner.should_cancel` 命中即返回 cancelled），无需改动 `child_agent_runner.py` / `delegation_executor.py`。

### 3.3 状态传递与清理机制

- **进程内信号（runtime 控制面）**：`cancellation_registry.mark_cancelled(turn_id)` 对整棵树的每个 turn 打标，`ChildAgentRunner` 线程侧据此中断。任务完成后信号保留（不 clear），保证并发重入 / 后续轮次不会误放行（与现状一致）。
- **持久化事实（turn/task/delegation 三表）**：
  - `turns`：树内每个 active turn 经 `cancel_turn_if_active` 条件置 `cancelled`，终态只落一次。
  - `tasks`：**不改写 `tasks.status` 生命周期字段**。`tasks.status` 是用户驱动的生命周期状态（`set_lifecycle_status` 仅接受 `open`/`archived`，task_service.py:188），执行态由最新 turn 派生（`task_display_status`，task_service.py:210）。取消子 task 的 turn 后，其 `task_display_status` 自动派生为 `cancelled`，无需（也不应）把执行态值写入生命周期字段，以免污染前端 open/archived 展示与取值约束。
  - `delegations`：每个被级联的子 task 若有 `delegation_id`，`mark_cancelled(delegation_id, reason, child_task_id)` 落终态。
- **快照清理**：树内每个 turn 调 `_mark_stable_file_changes(turn_id)`，使运行中变更立即可见可撤销。

---

## 四、改造后如何保证「取消当前 turn → 已派发子 Agent 及子 task 正确取消回收」

1. **取消单元对齐**：以「task 树」为取消单元，而非单个 turn。当前 turn 取消后，其所属 task 下所有 turn 及全部后代 delegation 子 task（含孙 task 递归）被统一回收，杜绝「只取消首个 turn、其余残留」。
2. **信号直达线程**：`cancellation_registry` 对树内每个 active turn 打标；正在运行的 child turn 在工具线程被 `ChildAgentRunner.should_cancel` 命中，立即返回 `DelegationResult("cancelled")`，不再消费后续 `run_agent` 事件。
3. **终态一致**：turns（cancelled）+ delegations（cancelled + child_task_id）终态对齐；tasks 生命周期字段不改写（`task_display_status` 由最新 turn 自动派生为 cancelled），前端 timeline / 侧边栏子任务面板可正确展示「已取消」。
4. **失败安全**：任一环节异常（DB、事件持久化）逐层记日志并继续，不阻断整棵树回收（沿用现有 `_cancel_child_delegation` 的 try/except + `log.exception` 范式）。

---

## 五、边界情况覆盖

| 场景 | 处理方式 |
|------|----------|
| **无子 task** | 无任何 active turn 下挂 delegation，`_cancel_task_tree` 仅取消当前 task 自身 active turn，delegation 扫描自然空，递归终止；行为与现 `cancel_turn` 一致。 |
| **部分子 task 已完成** | 已完成子 task 的 turn 已是 `completed`，`cancel_turn_if_active` 条件更新返回 None（不改写历史终态）；其 delegation 已是 `completed` 非 active，不被 `list_active_by_parent_turn` 扫到，`_cancel_task_tree` 不会进入该子树。但 **`delegation_crud.update_status` 当前是无条件覆盖**，若对已 completed 的 delegation 调 `mark_cancelled` 会覆写历史终态——必须按 §3.1-C' 加 `expected_statuses` 条件更新，双保险保证已终态 delegation 不被改写。 |
| **子 task 嵌套派发** | delegation 驱动的递归经 `child_task_id` 逐层下钻整棵委派树，无论嵌套多深都回收。当前 `DelegationPolicy` 已挡死深度 >1，但取消链路本身不依赖策略，策略放开后依旧健壮。 |
| **delegation 已标记 child_task_id 但 turn 未启动（pending）** | `_cancel_task_tree` 对 pending turn 同样 `mark_cancelled` + `cancel_turn_if_active`，pending 未认领的 turn 被置 cancelled；子 task 树回收。 |
| **delegation 无 child_task_id（纯 pending 未建 task）** | `try_create_pending` 之后、`create_child_task` 之前取消：`_cancel_task_tree` 点 2 的「每 turn active delegation 扫描」按 `parent_turn_id` 扫到该 delegation，因无 `child_task_id` 无法递归子树，仅 `delegation_service.mark_cancelled(delegation_id, reason)` 落终态（该 delegation 无 child_turn、无子 task，无需更多动作）。此窗口被显式覆盖，不会滞留 pending。 |
| **取消竞态（取消与完成同时发生）** | `cancel_turn_if_active` 条件更新保证只落一个终态，不覆写 completed/failed。 |
| **并发多 task** | 取消仅作用于 `turn.task_id` 出发的整棵子树，不波及其他 task（不同 task 的 turn 并发，互不干扰）。 |

---

## 六、改造落地顺序

1. 在 `runner.py` 新增 `_cancel_task_tree`（delegation 驱动递归：取消本 task active turn + 扫每 turn active delegation 并沿 child_task_id 递归，不用 `list_child_tasks`），保留 `cancel_turn` 主链，调用点改为 `_cancel_task_tree(turn.task_id, "parent_turn_cancelled")`，吸收 `_cancel_active_child_turns` 的 delegation 扫描语义并删除/内联 `_cancel_child_delegation`。
2. 按 §3.1-C' 给 `delegation_crud.update_status` 加 `expected_statuses` 条件更新，`mark_cancelled` 传 `ACTIVE_DELEGATION_STATUSES`，保证第二次 mark（cancel 路径 + finalize 路径）幂等、不覆写已终态、不产生重复 `DELEGATION_CANCELLED` 事件。
3. **不改写 `tasks.status`**（沿用 `task_display_status` 由最新 turn 派生），不新增 task 生命周期写入方法。
4. 新增测试：无子 task / 单层子 task 部分完成 / 嵌套派发（mock 策略放开）/ 纯 pending delegation 窗口 / 并发取消竞态 / 重复 mark_cancelled 幂等。
5. 独立审查 Agent + 独立测试 Agent 闭环。

---

## 七、待确认事项

- **已解决**：`turn_service.list_turns_for_task(task_id)`（turn_service.py:106）即「按 task 列 turn」，runner 直接复用。
- **已解决**：`delegation_crud.update_status`（delegation_crud.py:126）为无条件覆盖，需按 §3.1-C' 补条件更新以保护已终态 delegation。
- **已解决**：不改写 `tasks.status` 生命周期字段，子 task 执行态由 `task_display_status` 从最新 turn 派生，无需新增 task 生命周期写入方法。
- **设计取舍确认**：`_cancel_task_tree` 从 `turn.task_id` 出发级联整棵 task 树（含同 task 兄弟 turn）。若期望「只取消当前 turn 派生的子树」，需改为从 `delegation_service.list_active_by_parent_turn(turn_id)` 命中 `child_task_id` 的子树作为遍历起点——本方案默认整棵 task 树，此限制作为演进预留，落地前需用户确认取舍。

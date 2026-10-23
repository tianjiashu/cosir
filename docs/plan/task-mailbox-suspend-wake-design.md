# Task 收件箱与挂起/唤醒统一设计

## 目标

把「等待」从 workflow 的一个节点，改为 Run 的一种静止状态：每个 Task 有一个持久化收件箱，任何来源的输入（用户、系统通知、用户决定、子 Agent 结果、后台任务结果）都以消息投递；Run 是收件箱的消费者，可以把自己挂起，由消息唤醒。

由此收敛三套并行机制：

- 等待审批：`wait_user` 节点 + Run 进度冻结待决调用 + 决定绑定 + 第二个恢复入口；
- 子 Agent / Team 节点等待：工具线程按秒轮询 `wait_idle`，最长占住父执行 300s，跨重启不可接续；
- 取消等待态：登记信号后「以强制拒绝恢复一个执行」，再由循环落定 cancelled。

统一后只剩一条唤醒路径、一个 workflow 入口，投递不含特殊逻辑，context 在静止时不存在未闭合的工具调用。

## 已确认的决策

| 问题 | 结论 |
|---|---|
| 消息在模型上下文中的角色 | 一律 `user` 消息，正文用后端生成的标签标识来源 |
| 后端如何区分消息 | 强类型 `kind`，后端逻辑只读 `kind`，不解析正文 |
| 何时并入 | 由消息属性 `delivery`（`immediate` / `next_run`）决定，投递方不做额外判断 |
| 是否投影到前端 | 由消息属性 `project_to_transport` 决定，默认值来自 `kind`，投递方可覆盖 |
| 运行中换模型 | 不允许：活动 Run 期间 `immediate` 输入必须与当前 Run 配置一致；换模型的输入只能是 `next_run` |
| 审批如何执行 | 工具调用当步闭合；用户决定以消息入队，由 service 确定性执行，不经模型转述 |
| 挂起的两种来源 | Agent 挂起（等审批、`wait_messages`）为 `suspended`；用户挂起即取消，为 `cancelled`；两者恢复规则不同，用状态区分 |
| Agent 挂起收到消息 | 任何可唤醒的 `immediate` 消息立即恢复当前 Run |
| 用户取消 | Agent、工具、子 Agent、后台任务全部中断；它们的结果不入父队列；取消落定时投递一条「用户已取消」通知；只有用户能恢复（发送新建 Run / 继续续跑） |
| 已完成 Task 收到子 Agent / 后台结果 | 唤醒主 Agent 继续处理（新建 Run） |
| 已失败 Task 收到子 Agent / 后台结果 | 只入队，不唤醒，随下一次用户输入并入 |
| 编辑 | 排队消息原地改；已消费消息运行中编辑 = 停止当前 Run 后重开；静止时编辑同现状。运行中编辑不投递「用户已取消」 |
| 一次委派多个子 Agent | 结果逐条到达、逐条唤醒，是否继续挂起由模型决定 |
| 取消的传递方式 | 保持队列外信号，不进 FIFO |
| 等待工具 | `wait_messages` |
| 等待超时 | 暂不考虑 |

## 核心概念

### 1. 消息

外部队列（`task_messages`）每行是一条消息：`kind`、正文载荷、`delivery`、`project_to_transport`、可选的发起配置（仅 `user_input`）。`TaskMessageQueue` 只负责**入队、按序读取、原地更新、在调用方事务内确认删除**，不承载投递策略、输入解析或交接规则。

| kind | 进模型的正文 | 默认 `delivery` | 能否唤醒 / 新建 Run | 发起配置 | 默认投影 |
|---|---|---|---|---|---|
| `user_input` | 用户原文（不加标签） | 前端按用户偏好传 | 新建 Run，或唤醒挂起的 Run | 有 | 是 |
| `notice` | `<system_notice>…</system_notice>` | `immediate` | 否，只随下一次模型请求并入 | 无 | 否 |
| `decision` | `<user_decision request="…">…</user_decision>` | `immediate` | 唤醒挂起的 Run | 无 | 否（审批卡片由 TeamRun 状态驱动） |
| `agent_result` | `<agent_result from="child_task:{id}">…</agent_result>` | `immediate` | 唤醒挂起的 Run；Task 已完成时新建 Run | 无 | 投递方决定 |
| `background_result` | `<background_result job="…">…</background_result>` | `immediate` | 唤醒挂起的 Run；Task 已完成时新建 Run | 无 | 投递方决定 |

「用户已取消」通知是 `notice` 的一种：取消落定时以 `next_run` 入队，不投影、不唤醒，随用户下一次发送新建的 Run 并入，让模型知道上一回合被用户中断。

约束：

- 标签由后端按 `kind` 生成；外部来源正文（子 Agent 输出、网页、终端）中出现的同名标签必须转义，防止伪造边界。
- 系统提示词声明：只有不带标签的 user 消息出自用户本人，带标签的内容一律视为数据。这是降低注入风险而非消除，与工具结果角色面临的风险同级。
- Run 内产生的提示（工具准入、续写、批次拦截）仍只进本次执行的 `RunMailbox`、不落外部队列，形态同 `notice`。
- 通知从「合成一条 system 消息」改为 user 消息：对话中途的 system 消息在部分供应商 / 兼容层不被接受。

### 2. 投递时机

投递只看 `delivery`，没有按模型过滤、批量改写投递方式等特殊逻辑：

- **有执行者时**：外部队列中全部 `immediate` 消息投进本次执行的 mailbox，`prepare_input` 在下一次模型请求前并入；`next_run` 消息不进入当前执行。
- **新 Run 开始时**：外部队列中剩余的全部消息（含 `next_run`）随初始投递并入。
- `next_run` 消息不唤醒挂起的 Run，等当前 Run 结束后交给新 Run。

### 3. 运行期配置锁定

一个 Run 一个模型配置（模型、Agent、选项），活动期间（`running` / `suspended`）不可更换。校验只在入队的 API 边界做一次，不合规直接 409：

- `immediate` 的 `user_input`：配置必须等于活动 Run 的配置；
- `next_run` 的 `user_input`：配置必须与队列中已有的 `next_run` 用户输入一致（下一个 Run 的配置由第一条决定）；
- 前端在运行中切换模型时，把该条消息强制为 `next_run`。

由此交接新 Run 时无需再过滤：队列中所有用户输入的配置要么等于当前 Run，要么彼此一致。

### 4. 投影

`project_to_transport` 随消息入队写入队列行，并入时写入 context entry；两个投影点都只看它：

- 排队列表：待并入且需投影的消息（取代现 `pending_users` 的「只投影用户输入」）；
- 消费后的消息流：`UserInputConsumedEvent` 只对需投影的消息发出。

前端按 `kind` 渲染：`user_input` 为用户气泡，其余可投影 kind 为对应卡片；未知 kind 走兜底渲染，不导致消息流崩溃。

### 5. Run 状态

```text
running ──SUSPEND──▶ suspended ──WAKE──▶ running
   │                     │
   ├─COMPLETE/FAIL       └─CANCEL──▶ cancelled
   └─CANCEL──▶ cancelled ──RESUME──▶ running   (failed 同理)
```

挂起分两种，用状态区分，因为恢复规则不同：

| 挂起 | 状态 | 谁能恢复 | 恢复后 |
|---|---|---|---|
| Agent 挂起（等审批、`wait_messages`） | `suspended` | 任何可唤醒的 `immediate` 消息 | 继续当前 Run |
| 用户挂起（取消） | `cancelled` | 只有用户 | 发送 → 新建 Run；「继续」→ 续跑当前 Run |

- `suspended` 取代 `waiting_for_input`：没有执行者，context 中没有未闭合调用，Run 进度中不冻结任何调用。
- `suspended` 属于活动态：不算静止（`wait_idle` 语义不变），配置锁定照常生效。
- 删除 `RESUME_WITH_DECISION`；新增 `WAKE`（`suspended → running`）；`CANCEL` 的源状态扩为 `running | suspended`。
- `completed` 与 `failed` 仍是终态，不重开：已完成 Task 被结果消息唤醒时新建 Run。

### 6. 挂起

挂起由工具声明：`ToolObservation` 上的 `suspend` 标记取代 `user_input_request` 在 workflow 侧的作用。

- 工具调用当步闭合，观察照常写入 context；
- 本批全部观察写入后，若有任一观察带 `suspend`，Run 迁移到 `suspended` 并结束本次驱动，不再请求模型；
- 使用方：`agent_team`（已提交审批）、`wait_messages`（取代 `child_agent_wait` 的阻塞等待）。

**防丢唤醒**：挂起落定后、释放执行前，调度方在 Task 闸门内检查外部队列；已有可唤醒的 `immediate` 消息时立即唤醒。

### 7. 唤醒与新建（统一入口）

`WorkflowSession` 只有一条推进规则，入队后与执行释放后各调用一次（持 Task 闸门）：

| Task 状态 | 队列中的消息 | 动作 |
|---|---|---|
| 有执行者 | 任意 | 投递全部 `immediate` 消息进 mailbox |
| 最新 Run `suspended` | 有可唤醒的 `immediate` 消息 | `WAKE` 并启动执行，从 `prepare_input` 进入 |
| 最新 Run `suspended` | 只有 `next_run` 消息或通知 | 不动 |
| 最新 Run `completed` | 有 `user_input` | 交接新 Run（配置见下） |
| 最新 Run `completed` | 只有 `agent_result` / `background_result`（及通知） | 新建 Run，配置继承最新 Run |
| 最新 Run `cancelled` / `failed`，或无 Run | 有 `user_input` | 交接新 Run（配置见下） |
| 最新 Run `cancelled` / `failed`，或无 Run | 只有非 `user_input` | 不动，等用户输入时一并并入 |

`decide`、`_advance`、等待态恢复三条路径合并于此。workflow 只剩一个入口 `prepare_input`。

新建 Run 的配置来源：队列中有 `user_input` 时取**最后一条**用户输入——全部输入都会并入这个 Run，最后一条是用户最新的选择（活动期间入队的输入受配置锁定约束、彼此一致；取消后残留的旧输入让位于新发送）；只有结果消息唤醒已完成 Task 时，继承最新 Run 的 Agent 身份、`model_config_id` 与选项。

### 8. 审批（Agent Team）

1. `agent_team(config)` 创建 pending TeamRun（现有 `create_pending_confirmation`），返回「已提交审批，team_run_id=…」并带 `suspend`；
2. 前端卡片按 `team_run_id` 绑定 TeamRun 状态，不再依附工具 part 的等待生命周期；
3. `/decisions` 在 API 边界按 pending TeamRun 校验后入队 `decision` 消息（校验只此一处，过期决定直接 409）；
4. 决定在 API 边界由 `AgentTeamDecisionService` 执行，不经调度方转手：
   - 批准：`AgentTeamRunService.confirm` 原子迁移 pending → running，Team 作为后台任务执行（Coordinator 放进工作线程），结束后投递 `background_result`；
   - 驳回：`reject_pending_confirmation`；
   - 两者都投递 `decision` 消息唤醒 Agent（批准时告知 Team 已在后台运行，不要重复启动）。
5. 等待审批期间的用户消息：`immediate` 时直接唤醒；TeamRun 保持 pending，用户仍可在卡片上决定（结果另行投递）。

审批门由后端强制：模型无法自行把 TeamRun 推到 running。

### 9. 子 Agent 与后台任务

- 子 Task 每次 Run 到终态（completed / failed / cancelled），在其执行收尾时向父 Task 投递一条 `agent_result`（含 `child_task_id`、状态、`final_output` / 失败原因）。多次 `child_agent_send` 产生多条结果，逐条到达。
- 父 Agent 用 `wait_messages` 挂起；结果到达即唤醒，由模型决定继续等待还是推进。父 Agent 没等就给出最终回答（父 Run 已完成）时，结果到达同样唤醒，新建 Run 继续处理。
- 后台任务（Team 执行，后续可扩展到长终端命令）结束时投递 `background_result`。
- **父 Run 被停止时不投递**，两条规则覆盖：
  - 因父取消传播而被取消的子 Run（`end_reason = parent_cancelled`）、因父停止而取消的后台任务，不投递结果；
  - 投递在父 Task 闸门内检查其最新 Run，为 `cancelled` 则丢弃并记录日志，覆盖「恰在取消后才正常结束」的子 Agent / 后台任务。
  - 父 Task 已失败时只入队。

### 10. 取消与恢复

取消即用户挂起，要求中断彻底：

- 运行中：发 Task 信号，Agent 与正在执行的工具在检查点中断，落定 `cancelled`（不变）。
- 挂起中：闸门内直接 `CANCEL`（`suspended → cancelled`），无执行需要启动、无调用需要补结果。
- 下游：向子 Task 传播取消，后台任务（含执行中的 TeamRun）一并取消，待确认的 TeamRun 驳回；它们的结果不进入父队列（见第 9 节）。
- 落定后：投递「用户已取消」通知（`next_run`）。
- 恢复：只能由用户发起——发送新建 Run（通知随之并入）；「继续」续跑当前 Run（`cancelled / failed → running`，从 `prepare_input` 进入，不变）。

### 11. 编辑

编辑资格按 `kind == user_input` 判断。现 `_exclude_edited_message` 以 `project_to_transport` 筛选，在子 Agent 结果也可投影后会误判。

| 情况 | 目标 | 处理 |
|---|---|---|
| A. 编辑排队消息 | 仍在外部队列、未并入 context 的 `user_input` | 原地更新队列行，保留身份与 `delivery`；编辑后的内容按第 3 节重新校验。不影响 Run |
| B. 运行中编辑已消费消息 | 最新一条已投影的 `user_input`，最新 Run 活动中 | 停止当前 Run（同取消：中断 Agent、工具与下游，`end_reason = edited`，**不**投递「用户已取消」）→ 闸门外等执行释放 → 进入情况 C |
| C. 静止时编辑 | 最新一条已投影的 `user_input`，最新 Run 已终态 | 目标及其后 context 移出模型上下文与投影，编辑后的消息以新身份入队，同一事务提交，随后推进出新 Run；可更换配置 |

- 情况 A 与 `prepare_input` 消费并发：原地更新是一次条件更新（行仍存在才更新），行已被确认删除时返回 409，不加其他前置防御。
- 情况 B、C 共用「移出 + 入队 + 推进」一条路径，B 只是前面多了停止。
- 外部工具副作用不回滚（不变）。

## 不变量

1. 一个 Run 一个模型配置，活动期间不可更换；发起配置来自队列中最后一条 `user_input`，只有结果消息唤醒已完成 Task 时继承最新 Run。
2. 并入时机只由 `delivery` 决定，投影只由 `project_to_transport` 决定；投递方不做额外判断。
3. 用户取消后，只有用户能让 Task 再次运行；下游结果不入父队列。
4. 静止的 context 中没有未闭合工具调用；工具调用在产生它的那一步闭合。
5. 后端规则只读 `kind`，不解析正文标签。
6. 同一 Task 至多一个执行者；新建与唤醒只在 Task 闸门内发生。
7. 外部队列是事实源；投递不删行，`prepare_input` 在 context 写入事务内确认。
8. 挂起与入队之间不丢唤醒：挂起落定、Run 完成后都在闸门内复查队列。

## 代码事实（设计须适配）

1. **Team 节点 Task 也挂在主 Task 下**：`coordinator.py` 建节点 Task 时 `parent_task_id = 主 Task`（`task_type = "agent_team_node"`）。子 Agent 结果投递必须限定 `task_type == "delegate_task"`，否则每个 Team 节点都会唤醒主 Agent。
2. **Coordinator 以父 Task 取消信号判定取消**（`_cancel_for_parent`）。Team 后台化后父 Run 挂起、无执行者、无信号，Coordinator 改为检查自身 TeamRun 状态；取消路径为 `cancel_for_parent_run_id` 置 TeamRun `cancelled`，Coordinator 下次检查退出。
3. **`confirm_and_start` 需要父 Agent 运行时配置**，现由工具执行期提供。决定改由 API 边界执行后，需从父 Run 重新解析：`run_driver._runtime_config` 公开为 `resolve_agent_config(run)` 供两处复用。

## 代码设计

### 消息类型（`models/task_message.py`）

```python
Delivery = Literal["immediate", "next_run"]

class RunLaunch(BaseModel):            # 交接建 Run 与配置锁定共用
    agent_id: str
    model_config_id: int
    options: ConversationRunOptions

class UserInputBody(BaseModel):  kind: Literal["user_input"]; message: ConversationInput; launch: RunLaunch
class NoticeBody(BaseModel):     kind: Literal["notice"];     content: str; notice_kind: str
# 第二期：DecisionBody、BackgroundResultBody（Team 后台结果）；第三期：AgentResultBody（有生产者时才引入）

MessageBody = Annotated[UserInputBody | NoticeBody | ..., Field(discriminator="kind")]

@dataclass(frozen=True)
class TaskMessage:
    id: int | None                     # 队列行 id；Run 内提示为 None
    body: MessageBody
    delivery: Delivery
    project_to_transport: bool
```

- 规则以类变量挂在 body 上（默认 `delivery`、默认投影、能否唤醒挂起 Run、能否唤醒已完成 Task），类型即规则，不另设映射表。
- 进模型的正文由 body 自身渲染（标签 + 转义）；`TaskMessage.to_context()` 只组装 user 消息与 metadata。context metadata 记录 `kind`，编辑资格与投影据此判断。
- 删除 `UserMessage` / `NoticeMessage` / `QueuedTaskInput`。

### 存储与队列

- `task_messages`：`id, task_id, kind, delivery, project_to_transport, payload(body JSON)`。
- CRUD：`append`、`list_pending`、`update_payload_if_exists`（编辑 A 的条件更新）、`delete_returning`。
- `TaskMessageQueue`：`put` / `put_to_tasks` / `pending` / `update` / `ack`；`update` 是一次条件更新，行已被并入时返回 `False`，由 `WorkflowSession` 映射为 409 `MESSAGE_CONSUMED`；`ack` 返回删除那一刻的行内容，`prepare_input` 以它为准写入 context（排队期间的原地编辑因此生效，无需撤回 mailbox 里的旧副本）。删除 `dispatchable`、`take_handoff`、`release_all_immediate`。
- 输入解析留在 `ConversationInputPreparationService`；编辑移出（目标及其后 context、复用附件）移到 `message_edit.py`。

### 调度规则（纯函数，`service/conversation_run/dispatch_policy.py`）

```python
Deliver(messages) | Wake() | StartRun(launch) | Stay()

def check_launch(body, delivery, latest, pending) -> None   # 第 3 节，违反抛 CONFIG_LOCKED
def admit(body, latest) -> bool                             # 第 9 节丢弃规则（第三期）
def next_action(latest, has_executor, pending) -> Deliver | Wake | StartRun | Stay   # 第 7 节规则表唯一实现
```

规则表可逐格单测，不依赖执行器与数据库。

### `WorkflowSession`

| 入口 | 职责 |
|---|---|
| `submit(task_id, body, delivery)` | 闸门内 `check_launch` → 入队 → 排队投影 → `_advance` |
| `post(task_id, message)` | 系统生产者（通知、决定、结果），任意线程；闸门内 `admit` → 入队 → `_advance`；取代 `post_notices` |
| `edit(task_id, message_id, body)` | A 队列原地更新；B `_stop(reason="edited")` → 等释放 → C；C 闸门内移出 + 入队同事务 → `_advance` |
| `cancel(task_id)` | 有执行者发信号；挂起中闸门内直接 `CANCEL` 并传播、投「用户已取消」 |
| `resume` / `cancel_tool_call` / `wait_idle` / `stop_tasks` / `close` | 保留；`decide` 删除 |

- `_advance(task_id)`：读最新 Run 与队列 → `next_action` → 执行（投递 / `_wake` / `create_run(launch)` + `_start` / 不动）。
- `_after_run(run)`：`completed` / `suspended` → `_advance`（防丢唤醒）；`cancelled` → 传播，用户取消时投通知；`failed` → 不动；`delegate_task` 子 Task 非父传播停止时向父 `post` 结果。
- `ConversationRunService.create_run(task_id, launch)`：配置由调用方给出，不读队列。
- 取消信号带原因：`request_cancel(task_id, reason)`，`reason ∈ {user, edited, parent}`，检查点据此写 `end_reason`，`_after_run` 据此决定通知与结果投递。

### Run 状态

`WAITING_FOR_INPUT` → `SUSPENDED`；`SUSPEND`、`WAKE`（不清空结果）、`CANCEL`（源 running | suspended）；删除 `RESUME_WITH_DECISION`；`end_reason` 新增 `user_cancelled` / `edited` / `parent_cancelled`。

### Workflow

- `ToolObservation.user_input_request` → `suspend: bool`。
- `tools_node` 删除 HIL 整批拦截与 `need_HIL`。
- `observe_node` 写入本批观察后，任一 `suspend` → `SUSPEND` 并返回 `END`（挂起的唯一决策点）。
- `prepare_input` 用 `message.to_context()` 并入；`workflow._drive` 单入口。
- 删除 `wait_user_node`、`WAIT_USER`、`tool_continuation`、`continuation`、`resume_decision`、`user_input_projection`、`user_input_decision_handlers`。

### Agent Team 与子 Agent

- `agent_team` 工具：建 pending TeamRun，返回带 `suspend` 的成功观察（含 `team_run_id`）。
- `/decisions` → `AgentTeamDecisionService.submit(task_id, team_run_id, decision, draft)`：校验与执行唯一处。批准 `confirm_and_start` + `start_in_background`；驳回 `reject_pending_confirmation`；两者都 `post` `DecisionBody` 唤醒。
- `start_in_background` 先作为 `AgentTeamRunService` 的具体实现（`asyncio.to_thread(coordinator.start)`，结束 `post` `BackgroundResultBody`），不建通用后台框架；出现第二个后台任务时再提取。
- `wait_messages`：无参数，返回带 `suspend` 的观察（没有待等的工作时不挂起）；删除 `child_agent_wait`，保留 `child_agent_status`。

### 前端

`lib/assistant/task-commands.ts` 运行中切模型强制 `next_run`；决定命令带 `team_run_id`；按 `kind` 渲染；Team 卡片按 `team_run_id` 读 TeamRun；排队消息与已消费消息的编辑入口。

### 分期对应

| 期 | 范围 |
|---|---|
| 一 | 消息类型（`user_input` / `notice`）、存储与队列、配置锁定、`submit` / `post` / 编辑 A、按属性投递、`prepare_input` |
| 二 | `next_action` 全表、挂起与编辑 B、Run 状态、Workflow 其余、Agent Team、前端 Team 卡片 |
| 三 | `admit`、子 Task 结果投递、`wait_messages` |

## 删除清单

- `core/workflow/nodes/wait_user_node.py`、`ReactRoute.WAIT_USER`、`workflow._drive` 的第二入口；
- `ConversationRunProgress.continuation` 与 `tool_continuation`（冻结 / 还原调用批次）；
- `bind_decision`、`DecisionMismatch` 在调度层的映射、`RunTransition.RESUME_WITH_DECISION`；
- `UserInputRequest` 在 workflow 与工具观察上的控制流用途（`user_input_decision_handlers`、`project_waiting_request`、HIL 批次拦截通知）；
- `WorkflowSession._cancel_waiting`、`_after_run` 的迟到取消分支、`submit` 的「等待中立即发送按驳回结算」分支、`_resume_or_conflict`；
- `ConversationRunService.recover_orphaned_runs` 中针对等待调用的 `invalidate_waiting_calls`；
- `child_agent_wait` 的轮询循环；Team 由工具内阻塞执行改为后台任务后，`agent_team` 工具内的 `_start_confirmed_run` 阻塞路径；
- `TaskMessageQueue.dispatchable` 的模型过滤、`release_all_immediate` 批量改写、`take_handoff`；`put_user` 的输入解析与编辑移出移到输入用例。

## 影响范围

| 区域 | 变化 |
|---|---|
| `models/task_message.py`、`task_message_model`、`task_message_crud` | 统一消息类型、`kind` 扩充、`delivery` 适用所有 kind、`project_to_transport` 落库、原地更新 |
| `service/conversation_run/task_message_queue.py` | 收窄为入队 / 读取 / 原地更新 / 确认 |
| `service/conversation_run/workflow_session.py` | 统一推进规则、按 `delivery` 投递、挂起后复查、挂起态取消、子 Task 结果投递、运行中编辑 |
| `core/workflow/`（workflow、prepare_input、tools、observation 节点） | 单入口；批次后按 `suspend` 迁移；通知改 user 角色 |
| `models/enums/conversation_run_status.py`、Run 状态服务 | `suspended`、`WAKE`、`CANCEL` 源状态 |
| `core/tools/tool_handler/agent_team/`、`service/agent_team/`、`agent_team/coordinator.py` | 提交审批即返回；批准后后台执行；结果投递 |
| `core/tools/tool_handler/child_task/` | `wait_messages` 取代阻塞等待 |
| `assistant_transport/`（api、projection、rebuilder） | 入队时的配置锁定校验；按 `project_to_transport` 投影；`/decisions` 入队；排队消息编辑；新 kind 的 wire 表达 |
| 前端 | 运行中切模型时强制 `next_run`；按 kind 渲染；Team 卡片绑定 `team_run_id`；排队消息与运行中消息的编辑入口 |
| `AGENTS.md` | 更新「wait node 续跑」「等待审批的发送按驳回结算」「两级队列」「编辑只允许终态」等描述 |

绿地项目：数据库与快照结构直接破坏性变更，不做迁移与兼容。

## 分期

1. **消息与投递**：统一消息类型与 `kind`，`delivery` / `project_to_transport` 落库，按属性投递，运行期配置锁定，通知改 user 角色，编辑资格按 `kind`，排队消息编辑（情况 A）。
2. **挂起与统一唤醒**：`suspended` 状态、`suspend` 标记、统一推进规则、挂起后复查、挂起态直接取消；删除 wait 节点与续跑冻结；Agent Team 改为审批消息 + 后台执行，前端 Team 卡片改绑；运行中编辑（情况 B）。
3. **子 Agent 与后台结果**：子 Task 结果投递与丢弃规则、`wait_messages`、删除轮询；Team coordinator 节点等待视情况改为消息驱动。

每期独立可测：Vitest / pytest 覆盖规则；Playwright 覆盖审批卡片、排队投影与编辑入口。

## 风险

- **注入**：带标签内容以 user 角色进入上下文。缓解见「消息」约束；不追求完全消除。
- **丢唤醒 / 重复执行**：靠不变量 6、8；需针对「挂起落定 / Run 完成与结果入队并发」「取消落定与结果投递并发」写竞态测试。
- **编辑与迟到结果**：运行中编辑后新 Run 已开始，恰在停止与传播之间正常结束的子 Agent 结果会进入新 Run。概率低、结果带 `child_task_id` 可辨识，接受，不加额外防御。
- **Team 后台化**：执行期取消、后端退出时的进程清理需从工具执行层迁到后台任务所有者；重启后遗留 running TeamRun 的收敛规则需与 `recover_orphaned_runs` 对齐。
- **前端改动**：Team 卡片从工具 part 迁出，涉及重连恢复与快照重建。

## 自主唤醒的边界

已完成 Task 会被子 Agent / 后台结果唤醒并新建 Run，意味着主 Agent 可能在用户未发话时继续消耗 token。边界由两条规则限定：结果只来自本 Task 委派的子 Agent 与后台任务；用户停止（取消或运行中编辑）后不再接收被停止下游的结果。

## 实施记录

- **第一期（已完成）**：消息类型与属性、按属性投递、运行期配置锁定（前端快照带 Run `launch`）、编辑 A、通知改 user 角色。
- **第二期（已完成）**：
  - `waiting_for_input` → `suspended`，`WAKE` / `CANCEL(running | suspended)`；删除 wait 节点、`continuation`、决定绑定、`UserInputRequest` / `UserDecision`、HIL 批次拦截与 `need_HIL`。
  - 挂起由 `ToolObservation.suspend` 声明，observe 节点是唯一决策点；挂起落定后 `_after_run` 在闸门内释放并推进，防丢唤醒；迟到的取消直接迁移为 `cancelled`。
  - 取消信号带原因（`user` / `edited` / `parent`），只有用户取消留「用户已取消」通知；挂起中取消在闸门内直接迁移；运行中编辑（编辑 B）先以 `edited` 停止再走移出 + 入队。
  - Agent Team：工具只建 pending 并挂起；`/decisions` 按 `teamRunId` 寻址；Coordinator 以 TeamRun 状态判定取消（`_stopped`），不再读父 Task 信号；`confirm` 只校验 TeamRun 归属，不再要求主 Run 未终态（主 Agent 可能已被唤醒并完成）。
  - 前端：Team 卡片按 TeamRun 状态渲染（`pending` 表单 / `user_rejected` 已驳回 / 其余执行卡），TeamRun 视图新增 `end_reason`。
- **第三期（已完成）**：
  - `AgentResultBody`（`agent_result`，不投影，唤醒挂起与已完成的父 Task）；`WorkflowSession._report_to_parent` 在子 Run 完成、失败（`_after_run`）与取消（`_after_cancelled`）时回投，只限 `delegate_task` 子 Task，取消原因为 `parent` 时不回投，父 Task 已被用户取消时由 `admit` 丢弃；回投在子 Task 闸门外进行，不嵌套闸门。
  - `wait_messages` 取代 `child_agent_wait`：无参数、调用当步闭合并带 `suspend`；没有活动中的委派子 Task 且没有待确认 / 运行中的 TeamRun 时不挂起，直接返回，避免只有用户消息能唤醒的无限期挂起。子 Agent 与 Team 节点禁用该工具。
  - 前端删除 `child-agent-wait-result` 的校验、解析与渲染；`wait_messages` 没有展示 `kind`，走通用卡片。
  - Team 节点结论仍由 Coordinator 经 `wait_idle` 等待（Coordinator 本身是后台阻塞循环），未改为消息驱动。

# 阶段 3 开发计划：派生规则表、派生深度与重启收尾

状态：已完成（第 1 步派生深度、第 2 步重启收尾、第 3 步规则表）。后续修订：派生规则表移到 `core/tool/derivation.py`、按工具名枚举；深度不再在派生入口检查，改为创建 Task 时按深度固化工具（`core/tool/task_tool_scope.py`），下文 `check_derivation_depth` 相关描述已不适用。需求与已定规则见 [agent-fork-task-design.md §3.1 / §3.3](agent-fork-task-design.md)。路径相对 `apps/backend/app/`，前端相对 `apps/desktop/`。

## 1. 目标

- **派生规则表**：哪类 Task 能派生什么、能用哪些「需要用户界面」的工具，集中声明在一处，并且方便扩展：
  - 新增一种 Task 类型，只加一行；
  - 新增一个派生工具，只在工具上声明它属于哪种派生，再在表里加一行；
  - 新增一个需要用户界面的工具，只给它打一个标记。
- **派生深度**：记在 Task 上，上限可配置，在派生入口检查。
- **重启收尾**：子 Task 获得 `wait_messages` 后也会挂起；重启后，父 Agent 的等待要能重新接上。

## 2. 调查结论与问题清单

| # | 现象 | 证据 | 根因层级 / 置信度 | 本次 |
|---|---|---|---|---|
| 1 | 「谁能用什么工具」靠两张手写的工具名清单，再加上按工具组剥离 | `config/constant.py:391-413`；`core/tool/handler/child_agent_create.py:46-58`；`core/tool/handler/agent_team/agent_team_preparation_service.py:72-103` | 结构：规则散在三个入口，规则的维度（能否派生、能否交互）没有显式表达 / 高 | 处理（§3.1） |
| 2 | 「侧栏对话 = {user, fork}」的字面量在后端重复了 3 处 | `service/terminal/terminal_workbench_service.py:21`、`core/workflow/branch/task_title.py:36`、`storage/crud/task_crud.py:353` | 同一事实有多个来源 / 高 | 处理：都从规则表取 |
| 3 | 系统提示词的工具目录按 profile 的 `allowed_tools` 生成，而不是按 Task 固化的 schema。委派子 Agent 的 schema 里已经没有某个工具，目录却照样列出它 | `core/agents/agent_runtime_config.py:107`、`core/context/system_prompt_builder.py:115-119`；目录更新广播 `task_runtime/broadcaster/agent_catalog_update_broadcaster.py:45` | 事实源错位 / 高。规则表一改，二者就会继续分叉 | 处理：`for_run` 取 profile 与固化工具名的交集 |
| 4 | 用户 fork 不校验源 Task 的类型。fork 一个子 Agent 或节点，会得到「侧栏身份 + 子 Agent 工具面 + 节点提示词」的混合体 | `api/tasks_api.py:84-110`、`task_runtime/service/task_service.py:332-345`；前端只读面不给入口（`components/assistant-ui/turn/turn-footer.tsx:39`） | 信任边界缺校验 / 高 | 处理：fork 入口一处拒绝非侧栏来源 |
| 5 | `child_agent_send` / `child_agent_status` 用 `is_child` 校验归属，Team 节点也满足，主 Agent 因此能给自己的节点发消息 | `core/tool/handler/child_agent_send.py:100`、`child_agent_status.py:93`；`models/task_record.py:35-51` | 契约不准 / 高。子 Agent 能再委派后，影响面更大 | 处理：改用「由我委派」的谓词 |
| 6 | 子 Task 挂起时重启，父 Agent 的等待随进程丢失，永远等不到结论；挂起的 Team 节点还可能被唤醒，在没有 Coordinator 的情况下继续跑 | `core/tool/background/restart_settlement.py:86-128`、`core/tool/handler/agent_team/coordinator.py:636-660` | 设计缺口：「谁在等谁」只在进程内 / 高。节点那一半今天就存在 | 处理（§3.3） |
| 7 | 重启回投用工具名常量 `TOOL_DELEGATE_TASK` 去比较 `task_type` | `restart_settlement.py:105` | 借用了无关常量 / 高 | 随 #6 处理 |
| 8 | 父 Run 已失败时不回投重启结论，而重启前同样情形的结论会开新 Run | `restart_settlement.py:52-54` | 规则不一致 / 中 | 随 #6 处理：纳入 `failed` |
| 9 | 决定到达后新开的 Run 如果关掉了对应工具组，HITL 重放会被门禁按「本轮禁用」拒绝 | `core/tool/execute/tool_access_gate.py:138-149`；重放走门禁 `core/workflow/nodes/tools_node.py:52-53` | 与本次正交 / 中 | **记下** |
| 10 | 配置中心能给 JSON 子 Agent 勾上 Team、配置提案等工具组，规则表会把它们过滤掉，界面上显示已勾选却不生效 | `api/schemas/AgentConfigurationDocument.py:85` | 展示与规则不一致 / 中 | **记下**：等规则表落地后再决定是否置灰 |
| 11 | 前端也写死了侧栏类型集合 | `components/assistant-ui/elements/agent-message.tsx:14` | 跨语言重复，无法共用 Python 表 / 高 | **记下**：只有一处，保留 |
| 12 | docstring 漂移：`workspace_service.py:282-284` 说提案工具不是主 Agent 的常驻工具；`is_child` 说只指委派子任务；`TaskResponse.py:18-19` 的取值缺 `agent_team_node`；`delegate_task` 描述写着 "cannot delegate further" | 见所列行 | 漂移 / 高 | 随所在改动修正 |
| 13 | 宽度上限、单个 Run 的派生调用次数上限 | — | 已定不做 | 已记入 AGENTS.md 待办 |

## 3. 方案

### 3.1 派生规则表（可扩展）

**规则表**放在 `models/task_kind.py`：它只是数据和纯规则，和 `models/agent_team/` 是同一类。core、service、storage 都可以导入它，不会违反依赖方向。

```python
DerivationKind = Literal["delegate", "fork", "team"]

@dataclass(frozen=True)
class Derivation:
    creates: str      # 派生出的 task_type
    depth_step: int   # 新 Task 深度 = 发起方深度 + depth_step

@dataclass(frozen=True)
class TaskKind:
    user_facing: bool                    # 侧栏可交互：能承接需要用户作答或操作界面的工具
    derives: frozenset[DerivationKind]   # 允许的派生方式

DERIVATIONS = {
    "delegate": Derivation(creates="delegate_task", depth_step=1),
    "fork":     Derivation(creates="fork", depth_step=1),
    "team":     Derivation(creates="agent_team_node", depth_step=0),  # Team 是多个主 Agent 协同，不是嵌套
}
TASK_KINDS = {
    "user":            TaskKind(user_facing=True,  derives=frozenset({"delegate", "fork", "team"})),
    "fork":            TaskKind(user_facing=True,  derives=frozenset({"delegate", "fork", "team"})),
    "delegate_task":   TaskKind(user_facing=False, derives=frozenset({"delegate"})),
    "agent_team_node": TaskKind(user_facing=False, derives=frozenset({"delegate"})),
}
USER_FACING_TASK_TYPES = frozenset(t for t, k in TASK_KINDS.items() if k.user_facing)
```

**工具侧的声明**：`ToolDefinition` 新增两个字段，沿用 `returns_images`、`user_decision` 这种「工具声明、消费方统一生效」的现有习惯：
- `derivation: DerivationKind | None`：标在 `delegate_task`、`agent_team` 上，之后的 `fork_task` 也标。
- `needs_user_surface: bool`：标在两个 `propose_*` 和交互终端组的 6 个工具上。

「需要用户作答」不再单独声明：`user_decision` 不为空，就推出需要用户界面。`browser_handoff`、`terminal_handoff`、`agent_team` 因此自动归入。

**唯一的消费点**是 `core/tool/task_tool_scope.py` 里的 `tools_for_task(task_type, definitions)`。一个工具在下面两条都满足时才保留：
- 它不需要用户界面，或者这类 Task 是侧栏对话；
- 它不是派生工具，或者这类 Task 允许这种派生。

调用方：
- **主 Agent**：`service/conversation_run/workspace_service.py:_main_agent_tool_schemas`；
- **委派**：替换 `strip_child_disallowed_tools`；
- **Team 确认**：替换 `materialize_node_tools`，按名字查注册表定义后再过滤；
- **Agent fork**：直接沿用父 Task 的 schema（同属侧栏对话，结果天然一致）。

**删除的**：
- `CHILD_DISALLOWED_TOOLS` 和 `TEAM_NODE_DISALLOWED_TOOLS` 两张表，以及守它们的不变量测试；
- `materialize_node_tools` 里「不要互相移植」的说明。

**为什么不复用工具组**：工具组是用户关组和配置授权用的 UI 分类，和这条规则的边界对不上。例如「子 Agent 工具」组里，`delegate_task` 要按派生规则过滤，`wait_messages` 却要保留。

**为什么不在运行期门禁再查一遍**：门禁的上限本来就是固化的 schema，再按 Task 类型判一次，就是同一个不变量查了两遍。

**扩展守卫**（新增测试）：
- 代码里创建 Task 用到的每个 `task_type` 都在 `TASK_KINDS` 里；
- 每个声明了 `derivation` 的工具，其派生方式都在 `DERIVATIONS` 里；
- 每个 `DERIVATIONS[k].creates` 都在 `TASK_KINDS` 里。

**顺带收拢**：问题 #2 的三处字面量改用 `USER_FACING_TASK_TYPES`。问题 #4：fork 入口校验源 Task 的类型属于 `USER_FACING_TASK_TYPES`，否则返回 409。

**提示词目录**（问题 #3）：`AgentRuntimeConfig.for_run` 把 `allowed_tools` 取为 profile 与 Task 固化工具名的交集。工具层、工作方式层和目录更新广播因此都只看到这个 Task 真正能用的工具。

**归属谓词**（问题 #5）：`TaskRecord.delegated_by(task_id)` 判定为 `task_type == "delegate_task"` 且 `parent_task_id == task_id`，替换 `is_child` 的两处用法。`is_child` 没有其他调用方，一并删除。

### 3.2 派生深度

- **事实**：
  - `tasks.derivation_depth INTEGER NOT NULL DEFAULT 0`，ORM 同时设 `default` 与 `server_default`；
  - `TaskRecord` 加字段，默认 0，同步 `to_dict` / `from_model` / `to_model`；
  - `TaskCrud.create` 与 `get_or_create_task` 加关键字参数，默认 0。
  - 现有约 70 处构造点不受影响。用户新建对话与用户 fork 不传，即为 0。
- **写入**：派生方按 `DERIVATIONS[kind].depth_step` 计算子 Task 深度，由 `child_depth(parent, kind)` 一处给出：
  - 委派：在建子 Task 时写入；
  - Team 节点：由 Coordinator 读主 Task 后写入，与主 Task 同层；
  - Agent fork：在阶段 4 的派生内核里写入。
- **配置**：`Settings.AGENT_MAX_DERIVATION_DEPTH`，默认 2，读取用现有的正整数函数（最小 1）；配置页限定 1–5，防止误填过大引发派生爆炸。在 `environment_field_catalog.py` 的 general 组登记（与 `MAIN_AGENT_MAX_STEPS` 同组，不新开分组）。保存后下一次工具调用即生效。
- **检查**：`check_derivation_depth(parent: TaskRecord) -> ToolObservation | None`，按调用时读到的配置判定 `parent.derivation_depth >= 上限`。超限时返回工具错误，正文写明实际上限，`status_hint` 为「派生超限」。调用方：
  - 委派入口：在建子 Task 之前；顺带把父 Task 的读取提前，原本建子 Task 之后才读；
  - `agent_team` 首次调用 `_prepare_pending_run`：在建 pending TeamRun 之前；
  - 阶段 4 的 fork 派生内核。
- **为什么放在入口、不放在门禁**：
  - 检查和写入深度在同一处，「建出来的深度不超过 N」只有一个负责方；
  - Team 节点由 Coordinator 创建，不经过门禁；
  - 审批后的 HITL 重放也经过门禁，门禁要是在重放时拒绝，handler 没机会收尾，会留下没人管的 pending TeamRun。
- **审批期间把配置调小**：不再拦。节点与主 Task 同层，节点之后的委派还会在各自入口按当时的配置再查一次。

### 3.3 重启收尾：按持久化占位重新挂等待

- **事实**：`BackgroundWork` 加 `target_task_id: int | None`。由 `run_background_work` 透传，`run_child` 传子 Task id。展示数据里的 `child_task_id` 不作为事实源。
- **登记表**：新增 `background_registry.reattach(tool_call_id, *, task_id, run_id, work, on_cancel)`，沿用持久化的 `work`，不重造 `started_at`，看板与卡片保持一致。从 `run_child` 抽出等待内核（建作业、登记、在 loop 上调度等待），进程内路径与重启路径共用。
- **重启收尾**：`reattach_child_waits` 取代 `settle_restarted_child_runs`。在 `RecoverService` 里先挂等待，再中断其余占位（`interrupt_pending_placeholders` 增加排除集）。占位要同时满足下面几条才重新挂：
  - `kind == child_agent`、`pending`、`include_in_context`：编辑重跑撤回的占位不挂；
  - 父 Task 当前 Run 处于 suspended、completed 或 failed（问题 #8，新增 `failed`）；
  - 父 Task 外部队列里没有同 `tool_call_id` 的 `background_result`：已入队的不重复回投；
  - 目标 Task 存在。目标缺失或旧行没有 `target_task_id` 时，按原 `tool_call_id` 投一条 `unavailable` 结论把父 Agent 唤醒，不物化目标的 space。
- **为什么不选「把挂起的子 Run 也收敛为 cancelled」**：它会破坏「suspended Run 原样保留」这条不变量，丢掉等待中的 HITL 决定与整棵子树的进度。子 Task 重启后自己开出新 Run 时，它还会错报 cancelled。
- **Team 节点**（问题 #6 的另一半）：Coordinator 收敛 TeamRun 之后，把这些 TeamRun 下挂起的节点 Run 直接迁移为 cancelled。只对「等待方不可能再出现」的节点这样做，不改一般规则。这样节点不会再被单独唤醒，它的占位按规则走中断。
- **语义**：重新挂上的等待在子 Task 无事可做时，按原 `tool_call_id` 回投，卡片照常落到 completed。子 Run 被重启收敛时，立即得到 `cancelled / runtime_restarted`；子 Task 挂起时，就等它真正落定。孙辈也被重新挂上时，逐级唤醒。
- **同步**：修正 `restart_settlement` 的模块说明（「子 Agent 不能挂起」不再成立），以及 `child_run_job.py:13`、`recover_service.py:42`、AGENTS.md 中重启收尾的描述。

## 4. 分阶段（每步可单独验证，顺序即安全顺序）

1. **派生深度**（§3.2）：加列、配置、`child_depth` / `check_derivation_depth`，在委派与 `agent_team` 首次调用两处接入。此时规则表还没切换，子 Agent 仍然不能委派，行为不变，只多了记录与检查。
2. **重启收尾**（§3.3）：子 Agent 获得 `wait_messages` 之前，先让等待能跨重启接上。
3. **规则表**（§3.1）：切换四个固化入口，删掉两张表，修提示词目录、fork 源校验与归属谓词。到这一步才放开能力：子 Agent 和节点的子 Agent 能再委派，子 Agent 能用 `wait_messages`、`child_agent_send`、`child_agent_status`。

## 5. 影响范围

- **数据**：
  - `tasks.derivation_depth` 新列；
  - `BackgroundWork.target_task_id`（JSON 字段，旧行缺省为空）；
  - 绿地项目，删本地库即可。
- **契约与行为**：
  - **工具面**：委派子 Agent 与 Team 节点的工具面改由规则表决定（见 §4 第 3 步列出的变化）。
  - **派生超限**：新增「派生超限」工具错误。
  - **fork**：fork 非侧栏来源返回 409。
  - **追加消息与状态查询**：`child_agent_send` / `child_agent_status` 不再能寻址 Team 节点。
  - **重启**：父 Run 失败时也会回投重启结论；挂起的孤儿节点 Run 在重启时被取消。
  - **对外 schema**：`TaskResponse` 不带深度，前端暂无消费方。
- **测试**：
  - 新增：规则表扩展守卫、`tools_for_task` 各类型的工具面、深度写入与超限（委派、Team、配置现读）、fork 源校验、归属谓词、重新挂等待（挂起、被收敛、已入队、目标缺失、编辑撤回、孤儿节点）。
  - 改写：`test_child_agent_close_removal_contract.py`、`test_terminal_tool_scope.py`、`test_agent_team_run_tool_contract.py`、`test_restart_settlement.py`、`test_recover_after_restart.py`、`test_background_placeholders.py`、`test_delegate_task_model_contract.py`（替身补 `get_task`）。
- **文档**：
  - AGENTS.md：调度、重启、交互终端「只开放给侧栏对话」的表述改为引用规则表，代码目录补 `models/task_kind.py`；
  - `tool_ui_display_contract.md`（派生超限短提示）；
  - 问题 #12 的 docstring；
  - 设计文档 §3.1 / §3.3 / §5。

## 6. 刻意不做

- 问题 #9、#10、#11 记下，不在本次处理。
- 宽度上限、调用次数上限：已在 AGENTS.md 待办。
- MCP / skill 是单一网关工具，只能按网关整体声明能力，无法按服务里的单个工具声明；现在没有动态注册单个 MCP 工具名的路径，等出现再议。
- 已存在的 pending / 运行中 TeamRun 冻结的是旧规则下的节点工具名；绿地项目不做迁移。

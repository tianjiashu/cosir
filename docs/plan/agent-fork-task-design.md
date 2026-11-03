# Agent fork_task 与派生统一方案

状态：已完成（阶段 1–4）。路径相对 `apps/backend/app/`（后端）与 `apps/desktop/`（前端）。

## 1. 目标与已定取舍

Agent 能把用户交给的任务拆成几份，各 fork 出一个**侧栏可见**、继承当前上下文的对话去执行，结论回投给自己。和委派子 Agent 是「一份实现、两份 tool」：

| | `delegate_task` | `fork_task` |
|---|---|---|
| 身份 | 子 Agent 配置 | 沿用父 Run 冻结的 `launch`（继承上下文必然继承身份：系统提示词是 context 第 0 行） |
| 上下文 | 空白 + 任务说明 | 复制到本次调用所在的 assistant 消息之前 + 任务说明 |
| 展示 | 工作台 | 侧栏，挂在来源对话下，与用户 fork 同一棵树 |
| 生命周期 | 所有权子树（`parent_task_id`） | 按用户 fork 规则删除（`extra.fork`，改挂或「一起删除」）；父 Task 停止时停下它在等的 fork |

已定：新增 `agent_message` 消息 kind；递归按派生深度统一限制；`send` / `status` 本次只扩到「我派生的 Task」，任意 Task 通信留到统一通信时做。

## 2. 调查结论与问题清单

| # | 现象 | 证据 | 根因层级 / 置信度 | 本次 |
|---|---|---|---|---|
| 1 | 子 Task 挂起时重启，父 Agent 永久挂起 | 挂起的 Run 重启后原样保留（`service/recover_service.py:73`）；`settle_restarted_child_runs` 只回投被收敛为 cancelled 的子 Run（`core/tool/background/restart_settlement.py:104`）；`execute_terminal` 到前台上限即挂起（`tools/tool_handler/execute_terminal.py:484`），general child 有它 | 设计缺口：「谁在等谁」只在进程内，重启兜底依赖「子 Agent 不能挂起」这一已失效假设 / 高。**今天就存在**：触发源是自动转后台挂起，阶段 1 删除后这个假设暂时重新成立；阶段 3 给子 Task 开放 `wait_messages`、fork 带 HITL 后会再次失效 | 阶段 1 删除触发源；阶段 3 随开放等待一起落地 §3.1 |
| 2 | `restart_settlement.py:5-8` 写「子 Agent 不能挂起」 | 同上 | docstring 漂移 / 高 | 处理（阶段 1 改准确，阶段 3 随规则改写） |
| 1a | `execute_terminal` 前台命令到 60 秒自动转后台并挂起 Agent，挂起来源因此有三种：显式等待、HITL、隐式超时 | `core/tool/background/run.py:74-92`、`tools/tool_models/execute_terminal_args.py:48`、`execute_terminal.py:466-484`；`foreground_limit_s` 只有这一个工具在用（委派传 `None`，`child_agent/child_run_job.py:78`） | 结构：前台语义被一条隐式规则改写 / 高 | 处理（阶段 1，见 §3.0） |
| 3 | `agent_message` 若只靠「继承当前 Run 配置」开 Run，委派首条消息会卡死 | 新子 Task 无 Run，`RunLaunch.from_run(None)` 为 `None` → `Stay`（`core/workflow/message_queue/dispatch_policy.py:94-96`） | 契约：「只有用户输入能交接新 Run」硬编码在 `dispatch_policy.py:94,105,123` / 高 | 处理：`agent_message` 自带 `launch` |
| 4 | 现有 fork 复制不能用于运行中的 Run | 要求前缀 Run 全部终态（`task_runtime/service/task_service.py:325`），切分点只认用户消息（`:712`） | 局部扩展 / 高 | 处理：抽出共用复制内核 |
| 5 | 静态剥离两类原因混在一张表里 | `CHILD_DISALLOWED_TOOLS`（`config/constant.py:391`）同时管「递归」与「子 Task 前端只读、HITL 无人作答」；节点清单另一张（`:406`） | 结构：维度不清 / 高 | 处理：拆成「可交互」与「递归」两个维度 |
| 6 | 侧栏在父 Run 活动期间不刷新，新 fork 看不到 | 列表只在标题变化或 activity 落定时刷新（`components/layout/app-layout.tsx:376`、`use-runtime-transport.ts:119`） | 设计缺口 / 高 | 处理 |
| 7 | 编辑、fork 切分、冷重建、投影只认 `user_input` | `latest_user_input`（`service/conversation_run/conversation_task_context_service.py:186`）、`starts_turn`（`task_service.py:728`）、rebuilder（`assistant_transport/service/conversation_task_state_rebuilder.py:265`）、前端 `conversation-actions.ts:11-23` | 新 kind 的消费方 / 高 | 处理（逐处定语义，见 §3.2） |
| 8 | 子 Agent 不等后台工作就结束，父拿到不完整结论；之后的结论无人回投 | `_quiescent` 不看 `background_registry`（`core/task_actor.py:324`） | 设计缺口 / 中。Team 节点今天已有 | **记下**：直接把未完成后台工作算进静止会让长驻后台命令（dev server）永远等不完，需单独设计 |
| 9 | 重启后挂起的 Team 节点可被唤醒并在没有 Coordinator 时继续跑 | `coordinator.py:635` 收敛 TeamRun，节点 Task 仍被物化 | 设计缺口 / 中，今天就存在 | **记下**，与本次目标无关 |
| 10 | 系统提示词的子 Agent 目录按 profile 的 `allowed_tools` 生成，不按固化 schema | `core/context/system_prompt_builder.py:116` | 既有漂移 / 高 | 深度化后最深一层看到目录、调用才报错，可接受；**记下** |
| 11 | `MessageKind.agent_result` 无生产者 | `models/message_metadata.py:22` | 死值 / 高 | 处理：替换为 `agent_message` |
| 12 | 前端树过滤 `"delegation"`，后端真实类型是 `delegate_task` / `agent_team_node` | `components/task-tree/task-tree-model.ts:84` | 死代码 / 高 | 处理（本次改这个文件，顺带删除） |
| 13 | 给挂起的 fork 发消息会唤醒它，若它在等 HITL 决定，模型可能不等决定就继续 | `child_agent_send.py:166` 现在就这样唤醒子 Run | 取舍 / 中 | 接受：等待中的决定在 Run 结束后仍可作出，卡片不丢 |

## 3. 方案

### 3.0 execute_terminal：前台就是前台，后台就是后台（阶段 1）

- **改后的不变量**：挂起只来自两种显式等待，一是 `wait_messages`，二是 HITL 等决定或交还；工具不会因为超时自己挂起。前台命令阻塞到结束、超时或被取消；后台命令立即返回占位，结论回投，不挂起。
- **删除**：
  - `BackgroundCapability.foreground_limit_s` 及参数描述里「超过 N 秒自动转后台」的文案（`core/tool/schemas/tool_background.py:24,39`）；
  - `run_background_work` 的期限分支和 `foreground_limit_s` 参数（`run.py:74-92`），以及 `PlaceholderReason` 里的 `"limit"`（`job.py:42`）；
  - `execute_terminal` 占位里 `limit` 那一支和 `suspend`（`execute_terminal.py:466-484`）；
  - 工具描述、`wait_messages` 描述里「long terminal waits」的说法；
  - `test_run_background.py` 中关于期限的用例。
- **保留的两条后台入口**（已确认）：
  - Agent 发起时用 `background=true` 直接放到后台（参数不变，默认仍是前台）；
  - 用户把前台等待手动「转到后台」：自动兜底删除后，命令最长可以前台跑 3600 秒（`execute_terminal_args.py:42`），模型选错前台时，用户除了取消还能把等待转走。
  - 两条都是显式选择，转到后台都不挂起 Agent，所以改写不了前台语义。
- **同步改写**：AGENTS.md「代码目录边界」里 `run_background_work` 的说明删掉「到上限自动转后台」。
- **行为变化**：长命令选了前台就会一直占着这一步，模型要靠工具描述自己判断用后台。`restart_settlement` 唤醒挂在终端上的 Run 这条规则仍然需要：在 `wait_messages` 里等后台命令、交接终端都还会挂起。只需把它 docstring 里「后台命令也会让 Run 挂起」改准确。

### 3.1 重启收尾：按持久化的后台占位重新挂等待（阶段 3，与开放 `wait_messages` 一起落地）

- `BackgroundWork`（`models/message_metadata.py:75`）加 `target_task_id: int | None`：委派、fork 的占位记下目标 Task。这正是「重启后还需要的事实」。
- 重启时，对还在等的子 Task 占位（`kind == child_agent`）：
  - 父 Task 当前 Run 是挂起或已完成：**重新挂等待**，在登记表补一个条目（用原 `tool_call_id`），在事件循环上等目标 Task 静止，再按原调用回投。被收敛为 cancelled 的子 Run 会立刻静止，得到 `cancelled / runtime_restarted`；挂起的子 Task 会等到它真正落定。
  - 其余情况（父 Run 自己被收敛）：照旧推到「已中断」，由用户决定是否继续。
- 删除被取代的 `settle_restarted_child_runs`，`RecoverService` 第 3 步改为调用新入口。修正 `restart_settlement.py` 的模块 docstring。
- 待验证：`run_background_work` 能否在没有工具执行上下文时补登记。实现时先核对，不行就在 `background_registry` 加一个显式的「重启补登记」入口。

### 3.2 `agent_message` 消息 kind（阶段 2，调研已核对）

**用途**：别的 Agent 发给某个 Task 的消息，不是用户输入。委派（`delegate_task` / `child_agent_send`）、Team 节点输入，以及之后的 fork，都复用它。

**消息体 `AgentMessageBody`**
- 字段：`text`、`launch`（必填）、`source`（`task_id` 与 `name`）。
  - `source.task_id`：委派时是父 Task，Team 节点是主 Task。
  - `source.name`：发送方的显示名快照。委派取父 Task 标题，Team 取 Team 配置名。
- 推进语义：能推进 Run（`WAKES=True`）、投影到界面（`DEFAULT_PROJECTED=True`）、`PRIORITY=1`。
- 渲染：`<agent_message source_task_id=... from=...>`，正文用 `escape_tag` 转义。
- `PROMPT_GUIDE`：说明这是委派方或 Team 交来的任务，最终回答交回给它。`user_input` 的读法同步去掉「唯一请求来源」一句。
- 入队前准备：
  - 从 `UserInputService` 抽出发起配置校验（模型配置存在、推理强度受支持），新增 `AgentMessageService.prepare` 共用。
  - Agent 文本是纯文本：不走附件 token 解析。Agent 正文里恰好出现形如 token 的文本，会让投影抛错。
- 登记：
  - 队列 kind 与 context `MessageKind` 各加一项。context `MessageKind` 里从未被使用的 `agent_result` 删掉。
  - `UserMessageMetadata` 规则：`presentation` 允许出现在 `user_input` / `agent_message` 上，`source` 只允许出现在 `agent_message` 上。

**调度**
- 「带发起配置的输入」（用户输入、Agent 消息）一轮一条，配置取被消费那条（`dispatch_policy` 三处判定）。
- 入队一律 `immediate`：运行中并入，挂起时唤醒，空闲时开新 Run。
- `child_agent_send` 只按「子 Task 当前是否活动」判定是并入还是新开 Run。`matches_run` 只有它在用，一并删除。

**写入与投影**
- **写入**：两类输入都写 `message_id`。
- **消费事件**：`UserInputConsumedEvent` 泛化为 `InputConsumedEvent`（带可空 `source`）。去重、原位替换空占位、移出排队这套规划逻辑只有一份，不复制。
- **冷重建**：两类都投影。Agent 消息的 parts 只是一段纯文本。
- **一轮的起点**：fork 切分的 `starts_turn`、消息 id 与窗口对齐都把两类都算作一轮的起点。
- **编辑重跑**：最新的输入是 Agent 消息时返回 409 `EDIT_TARGET_STALE`，前后端口径一致。

**wire 与前端**（前后端快照校验都是严格键，须同时发布）
- 契约：snapshot 消息加 `source: {taskId, name} | null`，只有 user 消息可以带。
- 校验与转换：后端 `_MESSAGE_KEYS` / 校验、前端 `contract.ts` / `snapshot-validation.ts` / `converter.ts`（`custom.source`）同步改。
- 渲染：`thread.aui.tsx` 按 role 分支时，`source` 非空的消息直接分流到 `AgentInboundMessage`，不进入用户消息的编辑逻辑。
  - 「来自 X」可点击：来源在侧栏目录里就跳转路由，否则在工作台打开。
  - 子 Agent 与 Team 节点阅读器用的是同一个 thread 组件，改一处即可。
- 判定修正：
  - `conversation-actions.ts`：可编辑要求 `source === null`。
  - `transport-frame-store.ts` 的 `userMessageIds` 过滤掉 Agent 消息，否则它到达时会把用户的乐观发送收回。
  - `message-height-estimate.ts` 补一个分支。
- 测试：
  - 后端：快照 fixture 补 `source: None`。
  - 前端：E2E 内存服务与 Vitest 的公共消息构造补 `source: null`。
  - 新增：带 `source` 的 fixture 与一条 E2E。

**不做**
- 排队中的 Agent 消息不投影到队列区：一律 `immediate`，入队到并入只有一瞬间。
- 标题支线不触发：子 Task 和节点本来就不改写标题。

### 3.3 派生深度与派生规则（阶段 3）

实施计划（含规则表的可扩展设计与重启收尾细节）见 [agent-derivation-plan.md](agent-derivation-plan.md)。

两件事分开定：**谁能派生什么**是按 Task 类型固定的规则表；**能派生几层**是可配置的深度。

**派生规则表**（发起方类型 → 允许的派生方式；在一处声明，Task 创建、固化工具列表时生效，Task 类型不变，不会与配置漂移）

| 发起方 \ 派生方式 | 委派 | fork | 建 Team |
|---|---|---|---|
| 侧栏对话（`user`，含用户 fork、Agent fork 的 `fork`） | ✓ | ✓ | ✓（用户审批） |
| 委派子 Agent（`delegate_task`） | ✓（受深度限制） | ✗ | ✗ |
| Team 节点（`agent_team_node`） | ✓（受深度限制） | ✗ | ✗ |

- fork 与建 Team 只给侧栏对话：Team 要用户审批，fork 产物是侧栏里可交互的对话；委派子 Agent 在前端只读，审批无人作答。
- Team 节点只开放委派（已定）：节点在前端只读，建 Team 的审批无人作答，Team 里嵌套 Team 也不再是固定流程；节点 fork 出的侧栏对话会与 Team 流程脱钩（结论交给谁、Team 停止时停不停都说不清）。
- 不涉及派生、只因「前端只读」而剥离的工具（交接、提案配置、交互终端）另成一条「仅侧栏对话可用」规则，不混进派生表。
- `child_agent_send` / `child_agent_status` / `wait_messages` 不派生，不再剥离；`wait_messages` 还要等自己的后台命令。
- 取代 `CHILD_DISALLOWED_TOOLS` 与 `TEAM_NODE_DISALLOWED_TOOLS` 两张表，删掉 `materialize_node_tools` 里「不要互相移植」的说明和 `delegate_task` 描述里的「cannot delegate further」。

**深度**（已定）

- **事实**：`tasks` 表加列 `derivation_depth INTEGER NOT NULL DEFAULT 0`。
- **计数**：
  - 用户新建对话与用户手动 fork 同属一层，都是 0：用户 fork 是用户自己开的新起点。
  - 委派、Agent fork：发起方深度 +1。
  - Team 节点：与主 Task 同层（不 +1）。Team 是多个主 Agent 按固定流程协同，不是嵌套；节点再委派出去的子 Agent 才 +1。
- **上限**：深度达到 N 的 Task 不能再派生。`Settings.AGENT_MAX_DERIVATION_DEPTH`，默认 2，最小 1；在 `environment_field_catalog.py` 登记，前端配置页按目录自动渲染。只有一个数，不按派生方式分别设。
- **检查**：（后续修订）不在派生入口拦，改为 Task 创建时按深度固化工具：已到上限的 Task 不给派生工具，模型看不见就不会调；改上限只影响之后新建的 Task。见 `core/tool/task_tool_scope.py`。
- **默认上限 2 下的链路**：主对话(0) → 子 Agent(1) → 孙 Agent(2) 止；主对话(0) → Team 节点(0) → 节点的子 Agent(1) → 孙 Agent(2) 止；主对话(0) → Agent fork(1) → 再 fork(2) 止。

**有意的行为变化**：委派子 Agent 可以再委派；Team 节点的子 Agent 也可以再委派一层（现状不行）；两者都能用 `wait_messages`、`child_agent_send`、`child_agent_status`。

### 3.4 fork_task（阶段 4）

**后端**

- **复制内核**：把 `_copy_fork_history` 拆出「按切分序号复制前缀」的共用内核，用户 fork 与 Agent fork 各自只负责定位切分点：
  - 用户 fork：按消息定位（现有规则）。
  - Agent fork：按 `tool_call_id` 找到含本次调用的 assistant 行，切在它之前（同批其他调用尚未闭合，带进去会违反工具协议）。前缀里正在运行的那个 Run 在目标 Task 里克隆为 `completed`、`end_reason="forked"`，不带 `final_output`，因为新 Task 需要终态的当前 Run。其余 Run 本来就是终态。在 workspace 闸门与 `BEGIN IMMEDIATE` 内完成。
- **共用部分**：委派和 fork 共用 `child_depth`、`prepare_agent_message` 与 `run_child`，不另抽「派生内核」（只有这三步相同，再包一层只多一跳）。委派解析 profile；fork 用父 Run 的 launch、复制上下文（`TaskService.fork_for_agent`）。
- **Task 事实**：`task_type="fork"`，`extra.fork = {source_task_id, source_message_id: null, created_by: "agent", tool_call_id}`；不设 `parent_task_id`。`reparent_forks` 只改 `source_task_id`，其余键保留。标题取工具参数 `title`；标题支线只在用户输入时触发，用户在 fork 里开口后才会改写。
- **工具**：`fork_task(title, message)` 一律后台，`parallel_mode="parallel"`，与委派一样经 `wait_messages` 等结论。描述讲清与 `delegate_task` 的分工：需要对话上下文用 fork，需要专门角色用委派；fork 看不到父对话之后的新消息。
- **send / status**：`TaskRecord.derived_by(task_id)` 一处判定：委派看 `parent_task_id`，Agent fork 看 `extra.fork.created_by == "agent"` 加 `source_task_id`。`child_agent_send` 的 launch：委派子 Task 按 profile 重算（profile 修改能生效），fork 取目标当前 Run 的 launch。工具名暂不改，描述改为「你派生的 Task」。
- **停止**：父 Task 停止时，登记表撤销条目，经 `run_child` 的撤销回调以 `parent` 原因停下它在等的 fork（已有机制）。结论已回投、不再被等待的 fork 不受影响。
- **展示**：新 kind `fork-task-result`（`child_task_id`、`title`），后端 `_CHILD_TASK_KINDS`（`assistant_transport/projection/tool_result.py:15`）加一项，part 上的 `child_task_id` 照常提升。
- **注册清单**：`tool_names.py` 三处、`schemas/__init__.py`、`tool_registration.py`、`tool_error.py` 短提示、`define_agents.py` 主 Agent 默认工具、`tool_ui_display_contract.md`。

**前端**

- `tool-catalog.ts` 加条目（标题取 `args.title`），`tool-icons.tsx` 加 `git-fork`。
- `call-view-registry.ts` 登记 `fork-task-result` 视图，复用 `ChildTaskStatus` 与停止逻辑；「打开」改为跳转 `/tasks/{id}`。
- 侧栏：`workspaces.ts` 类型加 `created_by`；`workspace-tree-row.tsx` 的标记列给 Agent fork 显示 Bot 图标。
- 侧栏刷新：目录里没有该 Task 时，`fork-task-result` 视图触发一次刷新，由 catalog hook 提供 `ensureTaskListed(id)`、经 `TaskListingContext` 交给视图，只在那一处实现。（备选：新增一路工作区任务推送。代价更高，暂不做。）
- 删除 `task-tree-model.ts:84` 的失效过滤。

## 4. 影响范围

- **契约变化**：
  - inbox 新 kind 与调度规则（AGENTS「Task inbox」「调度」两段要改写）。
  - snapshot 消息新增 `source` 字段，前后端须同时发布。
  - 新工具 `fork_task` 与新展示 kind。
  - 工具剥离规则。
  - 重启收尾规则。
- **数据变化**：`tasks.derivation_depth` 列、`task_messages` 的 CHECK 约束、`BackgroundWork.target_task_id`。绿地项目，删本地库即可。
- **行为变化**：
  - 委派子 Agent 能多委派一层。
  - 委派子 Agent 和 Team 节点能用 `wait_messages`、`send` / `status`。
  - 子 Task 里的任务说明不再显示成用户气泡。
  - 给子 Task 发消息一律立即并入或唤醒，不再因配置不同而排到下一个 Run。
- **测试**：
  - 后端新增：调度（带 launch 的输入一轮一条、Agent 消息唤醒挂起）、Agent fork 切分（运行中 Run、并行批次）、深度检查（三个入口、配置现读）、重启重新挂等待（子 Task 挂起与被收敛两种）、send / status 对 fork 的归属校验、契约守卫。
  - 后端需改：`test_child_agent_close_removal_contract.py`、`test_terminal_tool_scope.py`、`test_agent_team_run_tool_contract.py`、`test_configuration_services.py`。
  - 前端 Vitest：snapshot 校验与 converter 的 `source`、`AgentMessageView`、latest 判定排除、fork 视图打开与刷新。
  - Playwright E2E（内存测试服务）：fork 卡片跳转与侧栏出现新 fork。
- **文档**：AGENTS.md（inbox、调度、删除、重启、工具 kind 清单、目录边界里的深度配置）、`tool_ui_display_contract.md`、上述各处 docstring。

## 5. 分阶段（每步可单独验证）

1. `execute_terminal` 删除超时自动转后台（§3.0）：消除今天的漏洞触发源，不依赖后续阶段。
2. `agent_message` kind、调度、投影、前端消息视图；委派、send、Team 改投新 kind。
3. 派生深度列与配置、两维剥离规则，同时落地重启重新挂等待（§3.1）。子 Task 能 `wait_messages` 的那一刻就需要它，二者不拆开。
4. `fork_task`：复制内核与派生内核重构、工具、展示、侧栏。
5. AGENTS 与契约文档收尾。

## 6. 刻意不做

- 向任意 Task 发消息（含发出即返回、防来回发消息的死循环、跨工作区）以及 send / status 改名，留到统一通信时做。
- 宽度上限（同一个根下同时活动的派生 Task 数量）与单个 Run 的派生调用次数上限：已记入 AGENTS.md 待办。
- 问题清单第 8、9、10 条。
- 分身之间互相通信。

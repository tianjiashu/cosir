# Task → Session 改名方案

> 状态：已执行（2026-11-13，阶段 2、3 合并为一次换词，见交付说明）。数字来自 2026-11-13 的 `rg` 统计，基线是 `main`（abb67863）加上正在进行的 Team 删除（工作树里约 241 个文件）。Team 删除提交后再开工，见「分阶段」的前置条件。

## 【任务类型】

功能改造，属于只改结构、不改行为的重构。改的是词汇与命名，不改任何调度、状态机或数据流。

## 〔功能改造〕

### 现状 → 终态

**动机**：Task 实际指「一段对话、一个 Agent 实例」：拥有上下文、队列、终端与浏览器标签、派生树，会长期存在、跑多个 Run。task 这个词却读成「一件做完就结束的事」。同时它还跟另外三种「任务」混用：后台任务（`background_tasks`）、委派交出去的任务说明、asyncio Task。界面里同一个东西有时叫「任务」（最近任务、暂无任务），有时叫「对话」（新对话、分叉对话）。

**session 在仓里已有的用法**。规则是：「会话 / session」默认指 Agent 会话，别的会话带上限定词就行（终端会话、CDP 会话）。只有在同一段代码里会和 Agent 会话撞名的，才需要改：

| 现有用法 | 规模 | 处理 |
|---|---|---|
| SQLAlchemy `session` / `Session` / `*_session_factory` | 后端约 644 行，43 个文件 | **要改**。裸的 `session` 参数会和 Agent 会话的变量直接撞名：现在叫 `task` 的记录变量（约 355 处）改名后也叫 `session`，写出来就是 `SessionService.delete(session, session)` |
| 终端代码里裸的 `session_id` / `sessionId` | 后端约 96 处、前端约 95 处；其中后端 6 个、前端 10 个文件里同时有 task id | **要改**。改名后这些文件里会同时出现两种 `session_id`，路由也会变成 `/sessions/{session_id}/terminal/sessions/{session_id}`（同名路径参数）。只把终端的 id 改成 `terminal_session_id` / `terminalSessionId` |
| 带限定词的终端用法：`TerminalSession*`、snapshot 的 `terminal_sessions`、`terminal_runtime/session/` 目录、Worker 的 `session.rs`、中文「终端会话」「交互会话」 | 约 500 行 | 不动，限定词或所在目录已经说明是终端 |
| 前端编辑状态 `AgentEditorSession`、`WorkbenchSession`、`FileSession` | 约 39 处 | 不动，类型名本身带着限定词 |
| 「会话侧栏」（ConversationAside） | 文档与界面 | 不动。它展示的正是当前会话的状态卡片，叫会话侧栏没有问题 |
| MCP `ClientSession`、`CdpSession`、Electron `session` 分区、POSIX 进程会话（setsid） | 约 30 处 | 不动，第三方或协议术语 |

**终态词汇表**：

| 词 | 含义 | 说明 |
|---|---|---|
| **Session / 会话** | 一段对话、一个 Agent 实例（原 Task） | 同一个词的英中两种写法：代码写 Session；界面、文档、docstring、日志 `msg` 等中文文本写「会话」，也可以直接写 Session。中文里不再用「任务」或「对话」指它。不带限定词的「会话」默认就是它 |
| **Run** | Agent 的一次运行 | 不改；`ConversationRun*` 去掉前缀成 `Run*`。这个前缀原本用来和 TeamRun 区分，Team 删除后只剩一种 Run，前缀没有用了 |
| 驱动、回合 | 不变 | — |
| **conversation / 对话** | Session 的消息流和对话区（Transport snapshot、`components/conversation/`、`service/conversation/` 的上下文用例） | 保留：它说的是内容，不是实体。例如「添加到对话」「对话界面渲染失败」「更早的对话历史」不改 |
| **task / 任务** | 只指一件工作：后台任务、委派交出去的任务说明（`delegate_task`）、asyncio Task | 改名后剩下的 task 和「任务」都必须属于这三类 |
| **terminal session / 终端会话** | 交互终端，名字不变 | 只有和 Agent 会话 id 同处一处的裸 `session_id` 改成 `terminal_session_id` |
| **db** | SQLAlchemy 会话：参数 `db`，类型 `DbSession`（`from sqlalchemy.orm import Session as DbSession`），工厂 `db_factory` | — |

### 行为账

**行为不变**：调度、Run 状态机、HITL、派生与汇报、终端与浏览器的生命周期、重启恢复、snapshot 的投影规则与时机。

**有意改变**（都只是名字，不改语义）：

1. HTTP 路由：`/tasks/...` → `/sessions/...`，`/workspaces/{id}/tasks` → `/workspaces/{id}/sessions`，`/task-feed/stream` → `/session-feed/stream`，终端路由 → `/sessions/{session_id}/terminal/sessions/{terminal_session_id}(/stream)`。
2. wire 字段：`taskId` / `task_id` → `sessionId` / `session_id`，`child_task_id` → `child_session_id`，`sourceTaskId` → `sourceSessionId`，`taskType` → `sessionType`；终端条目上的 `sessionId` → `terminalSessionId`，`terminal_sessions` 不变。错误码 `TASK_NOT_FOUND` / `TASK_STATE_CONFLICT` → `SESSION_*`。
3. 枚举值：`TaskType` → `SessionType`，值 `user`、`user_fork`、`agent_fork` 不变，`agent_task` → `delegated`。
4. 数据库：表 `tasks` / `task_messages` / `conversation_task_contexts` / `conversation_runs` → `sessions` / `session_messages` / `session_contexts` / `runs`；列 `task_id` / `parent_task_id` / `task_type` → `session_*`。没有迁移（`store_engines.py` 用 `create_all`），旧库直接删。
5. 模型看到的名字：`fork_task` → `fork_session`，`task_status` → `session_status`，`send_message` / `session_status` 的参数 `task_id` → `session_id`，Agent 消息的 `source_task_id` → `source_session_id`，提示词与工具描述里指实体的 task → session。**`delegate_task` 不改**：委派的确实是一件任务，它的结果是一个子 Session。
6. 日志 event 名：约 81 个 `task_*` / `*_task_*` → `session_*`（只把指实体的那部分改掉；`background_task_*` 不改）。
7. localStorage：`cosir:pinned-tasks:v1` → `cosir:pinned-sessions:v1`，模型选择的 `kind: "task"` → `"session"`，草稿按 sessionId 存。旧键作废，偏好重置一次。
8. 界面文案：指实体的「任务」和「对话」统一改成「会话」。
   - 任务 → 会话：最近任务、暂无任务、搜索任务、正在加载任务、无法打开任务、这个任务已不存在、任务已删除、任务 ID、打开来源任务、子 Agent 任务已不存在、工作区和任务列表、Fork 任务等。
   - 对话 → 会话：新对话、开始对话、返回对话、分叉对话、创建对话失败等。
   - 终端：「终端会话」「会话侧栏」都不改。只有一处例外：对话时间线里终端工具卡片的状态「会话已结束」（`call-views/terminal-session-view.tsx:16`）。它出现在 Agent 会话的消息流里，容易被读成「这个会话结束了」，改成「终端已结束」。
   - 后端给用户看的中文：默认标题「新对话」→「新会话」，「分叉对话时发生异常」→「分叉会话时发生异常」，设置项「每个 / 全部任务的标签上限」→「会话」。「已取消本轮对话」里的「本轮」是 AGENTS.md 禁用的词，改成「已停止这次运行」。
   - 不改：「后台任务」「委派任务」「委托任务时发生异常」都是一件工作；「添加到对话」这类指内容的也不改。拿不准的一条按「指实体 → 会话，指内容 → 对话，指一件工作 → 任务」判定。

### 消费方

| 消费方 | 怎么改 |
|---|---|
| 后端 `app/` | 类、模块、包整体改名，见下面的改名账本 |
| Electron 宿主（`host-contract.ts`、`browser/` 下 `params.ts`、`wire.ts`、`browser-tab.ts`、`downloads.ts`） | 标签归属 `taskId` → `sessionId`；`contract/tool-args.schema.json` 由 `scripts/export_browser_tool_schema.py` 重新导出，`wire-samples.json` 一起更新，`test_browser_contract_guard.py` 守着 |
| 前端 React | 路由 `tasks/:taskId` → `sessions/:sessionId`，lib 与 components 改名，见下 |
| 模型 | 工具名、参数、提示词，见行为账第 5 条 |
| 测试 | 后端 201 个、前端单测 129 个、E2E 文件名与夹具同步改名，断言里的 wire 字段同步 |
| 文档 | AGENTS.md（约 48 行，以 Team 删除改完之后的版本为准）、`tool_ui_display_contract.md`、`interaction_contract.md` |
| 本机数据 | 第二阶段完成后删掉 `~/.cosir/storage/app.sqlite3`（删之前先问你） |

### 分阶段

**前置条件**：Team 删除先提交。两件事都会改 `task_actor.py`、`task_recall.py`、`TaskResponse.py`、AGENTS.md 等同一批文件，并行做会互相冲突；改名脚本也要跑在 Team 已删干净的代码上，不必再为 Team 写映射。

每个阶段做完都要全绿：`pytest`、`mypy --strict`、`vitest`、`tsc`、隐藏窗口的 Playwright E2E。docstring 跟着代码在同一阶段改，不单独留一阶段。

**阶段 1：让位**（只改会撞名的两处）
- 数据库会话：`session` → `db`，`Session` → `DbSession`，`*_session_factory` → `*_db_factory`，`owned_session` → `owned_db`，`*_in_session` → `*_in_db`。行为与 wire 都不变。
- 终端 id：终端代码里裸的 `session_id` / `sessionId` 改成 `terminal_session_id` / `terminalSessionId`，路由路径参数和终端条目的 wire 字段一起改，前后端同时改。`TerminalSession*`、`terminal_sessions`、目录名和 Worker 都不动。
- 验证：全套测试；`rg -w session apps/backend` 剩下的只能是第三方用法；终端代码里不再有裸的 `session_id` / `sessionId`。

**阶段 2：后端内部 Task → Session**（HTTP 路由与 wire 字段暂不改）
- 存储、模型、service、core、task_runtime、assistant_transport 内部全部改名；`ConversationRun*` → `Run*`，`ConversationTaskContext*` → `SessionContext*`，`ConversationTaskStateService` → `ConversationStateService`。
- API 层的 schema 字段仍是 `task_id`，从 `session_id` 赋值。这是两个阶段之间的过渡态，不是兼容层，阶段 3 删掉。
- 删本机旧库（先问你）。
- 验证：后端全套测试与 mypy；前端不需要改动也能跑通 E2E，证明内部改名没有动到契约。

**阶段 3：wire、前端、宿主、模型可见名字一起改**
- 行为账第 1–8 条全部落地；重新导出浏览器工具 schema。
- 界面文案按行为账第 8 条改；E2E 与单测里断言的中文文案同步（「新对话」24 处、「分叉对话」6 处、「开始对话」6 处、「暂无任务」2 处、「搜索任务」1 处）。
- 验证：全套测试与 E2E；`test_browser_contract_guard.py`；隐藏窗口 E2E 跑委派、分叉、`send_message`、`session_status`，确认模型能正常调用改名后的工具。

**阶段 4：文档与残留核对**
- AGENTS.md 全文换词，篇幅不增加（四层事实改成 Session / Run / 驱动 / 回合，补一句「task 只指一件工作」，抵消掉别处的重复）。
- AGENTS.md 里指实体的「任务」「对话」（如「侧栏对话 Task」）改成「会话」。
- 残留核对：`rg -i '\btask'` 与 `rg 任务` 剩下的每一处，都要属于后台任务、委派任务说明、asyncio 三类之一。

## 【链路复用】

- 直接复用：浏览器工具 schema 导出脚本 `apps/backend/scripts/export_browser_tool_schema.py` 与跨语言守卫 `test_browser_contract_guard.py`（宿主契约改名后用它们证明两端一致）；core 导入边界守卫 `tests/test_core_import_boundary.py`（包搬家后它守依赖方向）。
- 改造后复用：无。
- 无可复用：改名本身没有现成工具。查过 IDE 重构（只覆盖 Python 或 TS 单边，改不到字符串里的 wire 字段和路由）和一次性 sed（会误伤数据库 `session` 和终端的 `session_id`）。做法是每阶段用一份显式映射表加脚本，逐个文件过 diff。

## 【问题清单】

1. **`test_task2_*` 到 `test_task5_*` 这批测试文件名里的 task 指旧计划的步骤号，不是实体**。证据：`apps/backend/tests/test_task2_lifecycle_cleanup.py` 等 6 个文件；根因是命名；置信度高。改名脚本必须排除它们，否则会被误改成 `test_session2_*`。是否重新命名见【刻意不做】。
2. **`AssistantRuntimeSession` / `RuntimeSessionContext` 里的 session 本来就指「一个 Task 的会话」**（`components/conversation-runtime/runtime/runtime-types.ts:24`，持有 `taskId`）。改名后正好和 Session 一致，只把字段 `taskId` → `sessionId`，类型名不动；置信度高。
3. **`delegate_task` 的错误文案里有「任务标题」**（`delegate_task_args.py:59`），实际指子 Session 的名字，要随阶段 3 改成「名称」；置信度中，阶段 3 逐条核对工具描述时再定。

## 【选定路线】

**结论**：按上面四个阶段整体改名，先让位、再改内部、再改契约、最后改文档。

**被否的方案**：
- **一次 sed 全仓替换**：会把数据库 `session`、终端的 `session_id` 和新 Session 搅在一起，`test_task2_*` 这类也会被误改。否决。
- **改叫 Conversation**：仓里已有 33 个 `conversation_*` 文件，而且 conversation 已经用来指消息流和对话区；再让它指实体，一个词就有两个意思。实体还拥有终端、浏览器标签和派生树，叫 conversation 不贴切。否决。
- **只改界面文案、代码保留 Task**：代码和界面的词对不上，task 与后台任务的混用也还在。否决。
- **`delegate_task` 一起改成 `delegate_session`**：委派出去的就是一件任务，原名更准确。否决。

## 【改动账本】

- **精简**：基本没有，因为这是改名，不删逻辑。只有 `ConversationRun*` / `ConversationTaskContext*` 去掉多余前缀，标识符变短。约 −0 行。
- **新增**：无新逻辑。只有阶段 1 每个用到 `DbSession` 的文件改一行 import 别名，约 +0 行。
- **净变化**：约 ±0 行。修改的行数估计如下（按「含该词的行」统计，同一行可能同时命中多类）：

  | 范围 | 约修改行数 |
  |---|---|
  | 后端 `app/` 的 task | 3350 |
  | 后端测试的 task | 3050 |
  | 后端 Conversation（只算 Run / Context） | 1000 |
  | 后端数据库 session | 640 |
  | 终端裸 `session_id` / `sessionId`（前后端） | 190 |
  | 前端 components / lib / electron / src | 1880 |
  | 前端测试 | 1790 |
  | AGENTS.md 与两份契约文档 | 100 |
  | 中文措辞（指实体的「任务」「对话」，前后端合计，其中一部分和上面几项是同一行） | 300 |

  合计约 12,300 行修改。终端和「会话」相关的中文不再整体改名后，比上一版少了约 500 行。交付后用 `git diff --stat -M` 对账；文件改名必须被识别成 rename，不能变成删一个、加一个。

- **目录与文件改名**（主要部分；同一规则下的其余文件照此处理）：

  | 原路径 | 新路径 |
  |---|---|
  | `app/task_runtime/`（`task_runtime_space*.py`） | `app/session_runtime/`（`session_runtime_space*.py`） |
  | `core/task_actor.py`、`task_execution.py`、`task_recall.py` | `core/session_actor.py`、`session_execution.py`、`session_recall.py` |
  | `core/events/task_event.py` | `core/events/session_event.py` |
  | `core/workflow/message_queue/task_message*.py` | `session_message*.py` |
  | `core/workflow/branch/task_title.py` | `session_title.py` |
  | `core/tool/multi_agent/fork_task.py`、`task_status.py`、`task_tool_scope.py`（及 `args/` 同名文件） | `fork_session.py`、`session_status.py`、`session_tool_scope.py` |
  | `models/task_record.py`、`task_extra.py`、`task_kind.py`、`conversation_task_context.py`、`conversation_run_*.py`、`enums/conversation_run_status.py` | `session_record.py`、`session_extra.py`、`session_kind.py`、`session_context.py`、`run_*.py`、`enums/run_status.py` |
  | `storage/model/task_model.py`、`task_message_model.py`、`conversation_task_context_model.py`、`conversation_run_model.py`（crud 同理） | `session_model.py`、`session_message_model.py`、`session_context_model.py`、`run_model.py` |
  | `storage/queries/task_status_queries.py` | `session_status_queries.py` |
  | `service/task/`、`service/task_feed/` | `service/session/`、`service/session_feed/` |
  | `service/conversation/conversation_task_context_service.py` | `service/conversation/session_context_service.py` |
  | `core/workflow/conversation_run_ledger.py`、`cancel/conversation_run_cancellation_registry.py` | `run_ledger.py`、`cancel/run_cancellation_registry.py` |
  | `api/tasks_api.py`、`task_feed_api.py`、`schemas/*Task*` | `sessions_api.py`、`session_feed_api.py`、`schemas/*Session*` |
  | `assistant_transport/service/conversation_task_state_service.py` | `conversation_state_service.py` |
  | `assistant_transport/{projection,request,stream}/task_*.py` | `session_*.py` |
  | `tools/tool_handler/terminal/task_terminals.py` | `session_terminals.py` |
  | desktop `components/task/`、`task-tree/`、`task-status/` | `components/session/`、`session-tree/`、`session-status/` |
  | desktop `lib/task-status/`、`task-feed/`、`pinned-task-storage.ts`、`assistant/task-commands.ts`、`assistant/transport/*task-*` | `lib/session-status/`、`session-feed/`、`pinned-session-storage.ts`、`assistant/session-commands.ts`、`assistant/transport/*session-*` |
  | desktop `components/links/use-open-task.ts`、`call-views/child-task-views.tsx` | `use-open-session.ts`、`child-session-views.tsx` |
  | 测试 `test_task_*.py`、`*task*.test.ts`、`*task*.spec.ts`（不含 `test_task2_*`–`test_task5_*`） | 同规则换成 session |

  **正向检索**：需求里说「会话 / Session 的执行者、闸门、删除」，看目录树就能落到 `session_runtime/`、`core/session_actor.py`、`service/session/`；说「终端会话」，仍落到 `terminal_runtime/session/`（目录在 `terminal_runtime/` 下，不会和 Agent 会话混淆）。
  **反向读出**：`core/session_actor.py` 读得出是「每个 Session 的调度 actor」；`storage/model/run_model.py` 读得出是「Run 表」；`assistant_transport/service/conversation_state_service.py` 读得出是「对话区 snapshot 的状态服务」。

  **保持不动**：`background-task*`、`ConversationBackgroundTask`、`background_tasks`、`delegate_task*`（都是「一件工作」）；`assistant_transport/state/conversation_state_*`、`ConversationEventProjector`、`components/conversation*/`、`service/conversation/`（都是对话内容）。

- **同步**：上面各阶段对应的测试；AGENTS.md、`core/tool/tool_ui_display_contract.md`、`terminal_runtime/session/interaction_contract.md`。

## 【刻意不做】

- **Team 删除本身**：另一项工作在做，本方案只假设它已完成。删除后测试里剩下的 Team 引用（约 25 个测试文件）也由那项工作清理。

- **`docs/plan/` 下的历史设计稿**（约 1500 行提到 task，包括 5 个文件名带 task 的）：它们是当时决定的记录，改词会让「当时怎么想的」失真。新读者以 AGENTS.md 为准。
- **`test_task2_*` 到 `test_task5_*` 的文件名**：里面的 task 指计划步骤号，名字本来就没信息。但重新命名要先读懂每个文件守的是什么，和本次换词无关。单独做。
- **`ConversationState*` 与 `ConversationEventProjector` 这批 wire 类型**：conversation 在这里指对话内容，命名没错，去掉前缀不属于这次的根因。
- **带限定词的各种「会话」**：终端会话（`TerminalSession*`、`terminal_sessions`、`terminal_runtime/session/`、Worker 的 `session.rs`）、编辑器与界面状态（`AgentEditorSession`、`WorkbenchSession`、`FileSession`）、会话侧栏，以及第三方与协议术语（MCP `ClientSession`、`CdpSession`、Electron `session` 分区、POSIX 进程会话）。限定词已经说清是哪种会话，不会和 Agent 会话撞名。
- **旧 localStorage 键的清理**：键作废后留在浏览器存储里，不写清理代码（绿地项目，重置一次偏好可以接受）。

## 已定事项

- **界面文案用「会话」**（2026-11-13 定）：Session 与「会话」是同一个词的英中两种写法，界面、文档和代码完全对上。「新对话」等用户已经熟悉的文案要跟着改。
- **「会话」默认指 Agent 会话，其他会话带限定词**（2026-11-13 定）：终端会话、会话侧栏、CDP 会话等都不改名。只有同一段代码里会和 Agent 会话撞名的才改：数据库的裸 `session`，以及终端代码里的裸 `session_id`。

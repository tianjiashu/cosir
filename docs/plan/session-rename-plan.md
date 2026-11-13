# Task → Session 改名方案

> 状态：待确认。数字来自 2026-11-13 在 `main`（c9767675）上的 `rg` 统计。

## 【任务类型】

功能改造，属于只改结构、不改行为的重构。改的是词汇与命名，不改任何调度、状态机或数据流。

## 〔功能改造〕

### 现状 → 终态

**动机**：Task 实际指「一段对话、一个 Agent 实例」：拥有上下文、队列、终端与浏览器标签、派生树，会长期存在、跑多个 Run。task 这个词却读成「一件做完就结束的事」。同时它还跟另外三种「任务」混用：后台任务（`background_tasks`）、委派交出去的任务说明、asyncio Task。界面里同一个东西有时叫「任务」（最近任务、暂无任务），有时叫「对话」（新对话、分叉对话）。

**session 在仓里已有的用法**，改名前必须先让开：

| 现有用法 | 规模 | 冲突 |
|---|---|---|
| SQLAlchemy `session` / `Session` / `*_session_factory` | 后端约 777 行，45 个文件 | `add_message(session=...)` 分不清是数据库事务还是对话 Session |
| 交互终端 `TerminalSession*`、snapshot 的 `terminal_sessions`、`/tasks/{id}/terminal/sessions/{session_id}` | 约 506 行 | 改完会变成 `/sessions/{id}/terminal/sessions/{session_id}` |
| Terminal Worker 的「会话模式」（`session.rs`） | 约 46 处 | 同上 |
| 前端编辑状态 `AgentEditorSession`、`TeamEditorSession`、`WorkbenchSession`、`FileSession` | 约 60 处 | 它们是编辑或界面状态，不是对话 |
| 中文「会话侧栏」（ConversationAside） | 文档与注释 | 和 Session 的中文重名 |
| MCP `ClientSession`、`CdpSession`、Electron `session` 分区、POSIX 进程会话（setsid） | 约 30 处 | 第三方或协议术语，都带限定词，**不动** |

**终态词汇表**（全仓一个词只指一件事）：

| 词 | 含义 | 说明 |
|---|---|---|
| **Session** | 一段对话、一个 Agent 实例（原 Task） | 代码、中文文档都写 Session；界面文案见「需要你决定的一件事」 |
| **Run** | Agent 的一次运行 | 不改；`ConversationRun*` 去掉前缀成 `Run*`，与 `TeamRun` 并列 |
| 驱动、回合 | 不变 | — |
| **conversation** | Session 的消息流和对话区（Transport snapshot、`components/conversation/`、`service/conversation/` 的上下文用例） | 保留：它说的是内容，不是实体 |
| **task** | 只指一件工作：后台任务、委派交出去的任务说明（`delegate_task`）、asyncio Task | 改名后剩下的 task 都必须属于这三类 |
| **terminal** | 交互终端（原 terminal session），id 叫 `terminal_id` | 一条命令仍叫 command（`terminal_runtime/command/`） |
| **db** | SQLAlchemy 会话：参数 `db`，类型 `DbSession`（`from sqlalchemy.orm import Session as DbSession`），工厂 `db_factory` | — |

### 行为账

**行为不变**：调度、Run 状态机、HITL、派生与汇报、终端与浏览器的生命周期、重启恢复、snapshot 的投影规则与时机。

**有意改变**（都只是名字，不改语义）：

1. HTTP 路由：`/tasks/...` → `/sessions/...`，`/workspaces/{id}/tasks` → `/workspaces/{id}/sessions`，`/task-feed/stream` → `/session-feed/stream`，终端路由 → `/sessions/{id}/terminals/{terminal_id}(/stream)`。
2. wire 字段：`taskId` / `task_id` → `sessionId` / `session_id`，`child_task_id` → `child_session_id`，`sourceTaskId` → `sourceSessionId`，`taskType` → `sessionType`，`terminal_sessions` → `terminals`。错误码 `TASK_NOT_FOUND` / `TASK_STATE_CONFLICT` → `SESSION_*`。
3. 枚举值：`TaskType` → `SessionType`，值 `user`、`user_fork`、`agent_fork` 不变，`agent_task` → `delegated`，`team_node_task` → `team_node`。
4. 数据库：表 `tasks` / `task_messages` / `conversation_task_contexts` / `conversation_runs` → `sessions` / `session_messages` / `session_contexts` / `runs`；列 `task_id` / `parent_task_id` / `task_type` → `session_*`。没有迁移（`store_engines.py` 用 `create_all`），旧库直接删。
5. 模型看到的名字：`fork_task` → `fork_session`，`task_status` → `session_status`，`send_message` / `session_status` 的参数 `task_id` → `session_id`，Agent 消息的 `source_task_id` → `source_session_id`，提示词与工具描述里指实体的 task → session。**`delegate_task` 不改**：委派的确实是一件任务，它的结果是一个子 Session。
6. 日志 event 名：约 82 个 `task_*` / `*_task_*` → `session_*`（只把指实体的那部分改掉；`background_task_*` 不改）。
7. localStorage：`cosir:pinned-tasks:v1` → `cosir:pinned-sessions:v1`，模型选择的 `kind: "task"` → `"session"`，草稿按 sessionId 存。旧键作废，偏好重置一次。
8. 界面文案：指实体的「任务」改成统一的叫法（最近任务、暂无任务、搜索任务、打开来源任务、子 Agent 任务已不存在等）；「后台任务」「委派任务」不改。

### 消费方

| 消费方 | 怎么改 |
|---|---|
| 后端 `app/` | 类、模块、包整体改名，见下面的改名账本 |
| Electron 宿主（`host-contract.ts`、`browser/` 下 `params.ts`、`wire.ts`、`browser-tab.ts`、`downloads.ts`） | 标签归属 `taskId` → `sessionId`；`contract/tool-args.schema.json` 由 `scripts/export_browser_tool_schema.py` 重新导出，`wire-samples.json` 一起更新，`test_browser_contract_guard.py` 守着 |
| Terminal Worker（Rust） | 会话模式改叫 interactive：`session.rs` → `interactive.rs`，协议帧里的模式名一起改；`containment.rs` 的 POSIX 会话号保留 |
| 前端 React | 路由 `tasks/:taskId` → `sessions/:sessionId`，lib 与 components 改名，见下 |
| 模型 | 工具名、参数、提示词，见行为账第 5 条 |
| 测试 | 后端 210 个、前端单测 141 个、E2E 文件名与夹具同步改名，断言里的 wire 字段同步 |
| 文档 | AGENTS.md（49 行）、`tool_ui_display_contract.md`、`interaction_contract.md` |
| 本机数据 | 第二阶段完成后删掉 `~/.cosir/storage/app.sqlite3`（删之前先问你） |

### 分阶段

每个阶段做完都要全绿：`pytest`、`mypy --strict`、`vitest`、`tsc`、`cargo test`、隐藏窗口的 Playwright E2E。docstring 跟着代码在同一阶段改，不单独留一阶段。

**阶段 1：让位**（行为与 wire 契约对前端之外不变）
- 数据库会话：`session` → `db`，`Session` → `DbSession`，`*_session_factory` → `*_db_factory`，`owned_session` → `owned_db`，`*_in_session` → `*_in_db`。
- 交互终端：`terminal_runtime/session/` → `terminal_runtime/interactive/`，`TerminalSession*` → `InteractiveTerminal*`（对外的数据对象直接叫 `Terminal*`），`session_id` → `terminal_id`，snapshot 的 `terminal_sessions` → `terminals`，路由改成 `/tasks/{id}/terminals/{terminal_id}`，Worker 会话模式改叫 interactive。这一步要求前后端同时改终端的 wire。
- 前端编辑状态：`AgentEditorSession` → `AgentEditorStore`，`TeamEditorSession` → `TeamEditorStore`，`WorkbenchSession` → `WorkbenchState`，`FileSession` → `WorkspaceFilesState`；文件 `*-editor-session.ts` → `*-editor-store.ts`。
- 中文「会话侧栏」→「对话附注栏」（代码名 ConversationAside 不变）。
- 验证：全套测试；`rg -w session apps` 剩下的只能是第三方用法（MCP、CDP、Electron）。

**阶段 2：后端内部 Task → Session**（HTTP 路由与 wire 字段暂不改）
- 存储、模型、service、core、task_runtime、assistant_transport 内部全部改名；`ConversationRun*` → `Run*`，`ConversationTaskContext*` → `SessionContext*`，`ConversationTaskStateService` → `ConversationStateService`。
- API 层的 schema 字段仍是 `task_id`，从 `session_id` 赋值。这是两个阶段之间的过渡态，不是兼容层，阶段 3 删掉。
- 删本机旧库（先问你）。
- 验证：后端全套测试与 mypy；前端不需要改动也能跑通 E2E，证明内部改名没有动到契约。

**阶段 3：wire、前端、宿主、模型可见名字一起改**
- 行为账第 1–8 条全部落地；重新导出浏览器工具 schema。
- 验证：全套测试与 E2E；`test_browser_contract_guard.py`；隐藏窗口 E2E 跑委派、分叉、`send_message`、`session_status`，确认模型能正常调用改名后的工具。

**阶段 4：文档与残留核对**
- AGENTS.md 全文换词，篇幅不增加（四层事实改成 Session / Run / 驱动 / 回合，补一句「task 只指一件工作」，抵消掉别处的重复）。
- 残留核对：`rg -i '\btask'` 剩下的每一处都要属于后台任务、委派任务说明、asyncio 三类之一；`rg -i 'session'` 剩下的都要是 Session 实体或第三方用法。

## 【链路复用】

- 直接复用：浏览器工具 schema 导出脚本 `apps/backend/scripts/export_browser_tool_schema.py` 与跨语言守卫 `test_browser_contract_guard.py`（宿主契约改名后用它们证明两端一致）；core 导入边界守卫 `tests/test_core_import_boundary.py`（包搬家后它守依赖方向）。
- 改造后复用：无。
- 无可复用：改名本身没有现成工具。查过 IDE 重构（只覆盖 Python 或 TS 单边，改不到字符串里的 wire 字段和路由）和一次性 sed（会误伤阶段 1 要让开的那几种 session）。做法是每阶段用一份显式映射表加脚本，逐个文件过 diff。

## 【问题清单】

1. **`test_task2_*` 到 `test_task5_*` 这批测试文件名里的 task 指旧计划的步骤号，不是实体**。证据：`apps/backend/tests/test_task2_lifecycle_cleanup.py` 等 6 个文件；根因是命名；置信度高。改名脚本必须排除它们，否则会被误改成 `test_session2_*`。是否重新命名见【刻意不做】。
2. **`AssistantRuntimeSession` / `RuntimeSessionContext` 里的 session 本来就指「一个 Task 的会话」**（`components/conversation-runtime/runtime/runtime-types.ts:24`，持有 `taskId`）。改名后正好和 Session 一致，只把字段 `taskId` → `sessionId`，类型名不动；置信度高。
3. **`delegate_task` 的错误文案里有「任务标题」**（`delegate_task_args.py:59`），实际指子 Session 的名字，要随阶段 3 改成「名称」；置信度中，阶段 3 逐条核对工具描述时再定。

## 【选定路线】

**结论**：按上面四个阶段整体改名，先让位、再改内部、再改契约、最后改文档。

**被否的方案**：
- **一次 sed 全仓替换**：会把数据库 `session`、终端 session 和新 Session 搅在一起，`test_task2_*` 这类也会被误改。否决。
- **改叫 Conversation**：仓里已有 33 个 `conversation_*` 文件，而且 conversation 已经用来指消息流和对话区；再让它指实体，一个词就有两个意思。实体还拥有终端、浏览器标签和派生树，叫 conversation 不贴切。否决。
- **只改界面文案、代码保留 Task**：代码和界面的词对不上，task 与后台任务的混用也还在。否决。
- **`delegate_task` 一起改成 `delegate_session`**：委派出去的就是一件任务，原名更准确。否决。

## 【改动账本】

- **精简**：基本没有，因为这是改名，不删逻辑。只有 `ConversationRun*` / `ConversationTaskContext*` 去掉多余前缀，标识符变短。约 −0 行。
- **新增**：无新逻辑。只有阶段 1 每个用到 `DbSession` 的文件改一行 import 别名，约 +0 行。
- **净变化**：约 ±0 行。修改的行数估计如下（按「含该词的行」统计，同一行可能同时命中多类）：

  | 范围 | 约修改行数 |
  |---|---|
  | 后端 `app/` 的 task | 3550 |
  | 后端测试的 task | 3270 |
  | 后端 Conversation（只算 Run / Context） | 900 |
  | 后端数据库 session | 780 |
  | 终端 session（前后端与 worker） | 550 |
  | 前端 components / lib / electron / src | 1900 |
  | 前端测试 | 1800 |
  | AGENTS.md 与两份契约文档 | 100 |

  合计约 12,000 行修改。交付后用 `git diff --stat -M` 对账；文件改名必须被识别成 rename，不能变成删一个、加一个。

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
  | `api/tasks_api.py`、`task_feed_api.py`、`terminal_sessions_api.py`、`schemas/*Task*` | `sessions_api.py`、`session_feed_api.py`、`terminals_api.py`、`schemas/*Session*` |
  | `assistant_transport/service/conversation_task_state_service.py` | `conversation_state_service.py` |
  | `assistant_transport/{projection,request,stream}/task_*.py` | `session_*.py` |
  | `assistant_transport/state/conversation_state_terminal_session.py` | `conversation_state_terminal.py` |
  | `tools/terminal_runtime/session/` | `tools/terminal_runtime/interactive/` |
  | `tools/tool_handler/terminal/task_terminals.py` | `session_terminals.py` |
  | `terminal-worker/src/session.rs` | `interactive.rs` |
  | desktop `components/task/`、`task-tree/`、`task-status/` | `components/session/`、`session-tree/`、`session-status/` |
  | desktop `lib/task-status/`、`task-feed/`、`pinned-task-storage.ts`、`assistant/task-commands.ts`、`assistant/transport/*task-*` | `lib/session-status/`、`session-feed/`、`pinned-session-storage.ts`、`assistant/session-commands.ts`、`assistant/transport/*session-*` |
  | desktop `components/links/use-open-task.ts`、`call-views/child-task-views.tsx` | `use-open-session.ts`、`child-session-views.tsx` |
  | desktop `lib/agent-editor/agent-editor-session.ts`、`agent-team-editor/agent-team-editor-session.ts` | `agent-editor-store.ts`、`agent-team-editor-store.ts` |
  | desktop `components/terminal/terminal-session-pane.tsx`、`use-terminal-session-actions.ts`、`lib/terminal/terminal-sessions.ts`、`call-views/terminal-session-view.tsx` | `terminal-pane.tsx`、`use-terminal-actions.ts`、`terminals.ts`、`terminal-view.tsx` |
  | 测试 `test_task_*.py`、`*task*.test.ts`、`*task*.spec.ts`（不含 `test_task2_*`–`test_task5_*`） | 同规则换成 session |

  **正向检索**：需求里说「会话 / Session 的执行者、闸门、删除」，看目录树就能落到 `session_runtime/`、`core/session_actor.py`、`service/session/`；说「终端」，落到 `terminal_runtime/interactive/`。
  **反向读出**：`core/session_actor.py` 读得出是「每个 Session 的调度 actor」；`storage/model/run_model.py` 读得出是「Run 表」；`assistant_transport/service/conversation_state_service.py` 读得出是「对话区 snapshot 的状态服务」。

  **保持不动**：`background-task*`、`ConversationBackgroundTask`、`background_tasks`、`delegate_task*`（都是「一件工作」）；`assistant_transport/state/conversation_state_*`、`ConversationEventProjector`、`components/conversation*/`、`service/conversation/`（都是对话内容）。

- **同步**：上面各阶段对应的测试；AGENTS.md、`core/tool/tool_ui_display_contract.md`、`terminal_runtime/interactive/interaction_contract.md`。

## 【刻意不做】

- **`docs/plan/` 下的历史设计稿**（约 1500 行提到 task，包括 5 个文件名带 task 的）：它们是当时决定的记录，改词会让「当时怎么想的」失真。新读者以 AGENTS.md 为准。
- **`test_task2_*` 到 `test_task5_*` 的文件名**：里面的 task 指计划步骤号，名字本来就没信息。但重新命名要先读懂每个文件守的是什么，和本次换词无关。单独做。
- **`ConversationState*` 与 `ConversationEventProjector` 这批 wire 类型**：conversation 在这里指对话内容，命名没错，去掉前缀不属于这次的根因。
- **第三方与协议术语**：MCP `ClientSession`、`CdpSession`、Electron `session` 分区、POSIX 进程会话。
- **旧 localStorage 键的清理**：键作废后留在浏览器存储里，不写清理代码（绿地项目，重置一次偏好可以接受）。

## 需要你决定的一件事

**界面文案用「对话」还是「会话」**：指实体的那些「任务」要统一改成哪个词。

推荐用**「对话」**：界面里已经大量使用（新对话、分叉对话、开始对话、返回对话），用户不需要学新词。代码与文档写 Session，界面写「对话」，这和现在代码写 Task、界面写「对话」的分工一样，只是去掉了界面里混用的「任务」。

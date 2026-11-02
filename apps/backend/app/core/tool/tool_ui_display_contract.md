# Tool UI 展示契约

状态：现行契约，适用于全部内置工具（34 个）。`tests/test_tool_registry_display_guard.py` 守卫两件事：每个注册工具都在前端工具目录（`apps/desktop/lib/assistant/tool-catalog.ts`）与第 3 节表格里有条目，代码里产出的每个展示 `kind` 都必须在表里登记。

本文定义工具调用在桌面端如何展示：后端给出哪些事实、前端据此做什么决定。

## 1. 运行边界

本项目是单用户、本机运行的桌面 Agent。工具 UI 展示不新增进程或服务：

```text
Electron main 进程（桌面宿主）
└─ 渲染进程 / React 前端
   └─ localhost Assistant Transport
      └─ FastAPI 后端进程
         └─ 工具 handler + 展示数据纯函数
```

- 工具在 FastAPI 后端进程执行。
- 展示外壳（图标、动词、调用行标题）归前端：工具名是封闭集合，前端工具目录按 `toolName` + `args` 查表得出；后端不声明、不下发任何展示文案。
- 展示数据纯函数与工具同侧：外部工具在 `apps/backend/app/tools/display/`，内部工具（子 Agent、Agent Team、web）在 `apps/backend/app/core/tool/handler/` 各自目录。它们不执行 IO。
- `ToolObservation.display_data` 承载工具终态的 UI 结构化数据；交互终端的运行期原始 bytes 只经每会话一条可写 `terminal-v2` WebSocket 呈现，不进 display_data。一次性终端（`execute_terminal`）的输出按 `terminal-result` 契约进入 display_data，原样、不剥 ANSI、不截断，也没有 `truncated` / `stream_truncated` 字段。模型 `content` 的统一输出预算是另一条边界，不改变 UI 展示数据。
- Assistant Transport 把展示数据带入事件与 snapshot，服务前端渲染与重连恢复；展示数据不是任务、Run、Agent context、文件变更或子 Task 状态的事实源。
- 一次性终端的输出增量只存在于当前进程的运行期 snapshot；交互终端增量只存在于会话的订阅队列与环形缓冲；会话列表与实时状态由 snapshot 顶层 `terminal_sessions` 投影，工具卡片 display_data 只是调用当时的冻结快照。
- `ToolObservation.artifact_data` 只承载内部工具产物，不进入 UI Transport。
- 后端重启后由已有 snapshot 恢复 UI，不隐式重放旧工具执行。

## 2. 契约分层

工具 part（`ConversationStateToolCallPart`）上与展示有关的字段：

| 字段 | 来源 | 何时写 |
| --- | --- | --- |
| `toolName` / `args` | 模型的工具调用 | 创建时写工具名；`args` 随 `status=running` 一次给全，冷重建从持久化参数读 |
| `awaitingUser` | `project_tool_result`（按占位行 metadata） | 每次结果投影都显式写；创建时为 `false` |
| `display_data` | `ToolObservation.display_data`（工具展示纯函数） | 终态（或后台 / HITL 占位）写入 |

### 2.1 展示外壳：前端工具目录

位置：`apps/desktop/lib/assistant/tool-catalog.ts`。每个工具一条：

- `verb`：动词，如「读取文件」。区头按它计数（命令的标题每条各异，按标题计数会把「执行命令 4」拆成四个 1），也是没有自定义意图时的标题。
- `icon`：lucide 图标名，取值表在 `components/assistant-ui/tools/tool-icons.tsx`。
- `target(args)`：目标，路径、模式、会话、地址、子 Agent 等；没有目标的工具不给。多目标只写首个、其余 `+N`（`read_image`、`web_extract`、`apply_patch`、`browser_act`）；多行输入只取首个非空行；组合目标（`"pattern" in path`、`a → b`）各段齐全才拼。
- `title(args)`：自定义意图，只有 `execute_terminal`（模型写的 `description`）、`delegate_task`（`agent_name`）与 `fork_task`（`title`）需要，取不到时回落到 `verb`。

为什么归前端：工具名是封闭集合，标签、图标、i18n 都是客户端的事；哪个参数是目标虽然是工具的知识，但投影的输入 `args` 前端本来就有。按工具名**查表**是声明式的，允许；按工具名写**渲染分支**仍然禁止，展开体与元信息只按 `display_data.kind` 选视图。

参数不可信：`args` 是模型原样参数，排队时为空对象，目录里每个取值都容忍缺字段；目录里没有的工具退回工具名与扳手图标。`execute_terminal.description` 只用于展示：执行、安全策略、日志与终态展示只认 `command`，需要用户批准命令的界面展示原命令。

后端唯一还要的「名字」是后台看板条目与 `wait_messages` 提示里的 `label`：由工具从模型给的事实里取（命令的 `description`、子 Agent 的名字），经 `run_background_work(label=...)` 登记，不含动词文案。

后台占位的 part 带 `background` 工作记录 `{kind, label, startedAt, endedAt}`（投影自占位行 metadata 的 `BackgroundWork`）：卡片只看它在不在；看板 `background_tasks` 由后端从工作副本里这些记录与登记表活条目投影，状态为 `running`（登记表里还活着）、`settling`（工作已结束、占位还没落定）、`completed`、`cancelled`。

### 2.3 `awaitingUser`：等你操作

`project_tool_result`（`assistant_transport/projection/tool_result.py`）一处计算：占位行 `status == "pending"` 且不是后台占位（没有 `background` 工作记录）即为真——HITL 等决定、交接等交还。每次结果投影都显式写，决定到达、重放后清回 `false`；`ToolCallsSettledEvent` 收束时也写 `false`。实时与冷重建同口径。前端据此把调用放进「等待用户」区，不再从 `pending` 自行推断。

### 2.4 `display_data`：动态展示数据

位置：`apps/backend/app/core/tool/schemas/tool_observation.py`

成功态非空 `display_data` 是面向客户端的结构化 JSON，必须有稳定的 `kind`：

```json
{ "kind": "<stable-display-kind>", "...": "UI-safe structured fields" }
```

约束：

- 只放客户端渲染所需的事实：目标、列表、统计、diff、分页、有限状态信息。
- 不放 `content`、堆栈、原始异常、完整 prompt、原始 provider 响应或凭据。
- 不由客户端从 `result`、`args` 或模型正文推导展示结果。
- 不把 `display_data` 当作后端业务事实源：派生 Task 的运行状态不在 `delegation-result` / `fork-task-result` / `agent-task-result` 里镜像，前端按 part 的 `child_task_id` 读应用级任务状态推送（`GET /task-status/stream`）。
- 字段投影和敏感字段过滤必须先于 Transport；安全 allowlist 不能由大小预算替代。
- `display_data` 只服务前端展示，不按模型 `content` 预算截断；工具自身业务语义需要截断时，用明确的 `truncated` 标记表达。
- 外层工具状态使用 Transport 的 `pending`、`running`、`completed`、`failed`、`cancelled`，不在 payload 里建第二套状态机。
- 错误态 `display_data` 按 2.5 节的短提示规则处理，不带成功态 `kind`。

### 2.5 错误展示策略

工具侧的重试提示只是写进 `tool_error` 的 `content` 的自然语言建议，不是观察字段；它既不进 Transport tool-call part，也不进 Run 级错误契约（`ConversationRunError` / `ConversationStateError` 只有 `code` / `message`）。

错误信息分三个通道，不能混用：

```text
ToolObservation.content（失败态）
  └─ tool_error 拼好的诊断与修正建议，只面向模型，不自动展示给用户

ToolObservation.display_data.status_hint
  └─ tool_error 错误态唯一允许的 UI 数据，约 5 个字、最长 8 个字符的受控短提示

Transport tool-call status
  └─ pending / running / completed / failed / cancelled 生命周期状态
```

规则：

- 状态必须从 `ToolObservation.status` 得到；`display_data` 不得定义或覆盖状态。
- `tool_error` 的 `display_data` 只能是 `{"status_hint": "..."}`；`status_hint` 来自后端的分类映射（`normalize_status_hint`：省略或超长时退到该工具的默认提示，再退到「执行失败」），不得复制失败文本、异常字符串或 provider 原始响应。
- 取消态显示「已取消」，不显示为失败；部分成功用「部分成功」等受控提示。
- Agent Team 的持久化运行 ID 走 part 的 `team_run_id`，成功与失败都可携带；它不是错误文案或第二份运行状态。
- UI 不展示堆栈、完整网络错误、模型 prompt、凭据或大段模型正文。

Transport 投影的固定映射：

```text
ToolObservation.status == "success"   → tool-call status "completed"，error 为空
ToolObservation.status == "error"     → tool-call status "failed"，error 为 status_hint
ToolObservation.status == "cancelled" → tool-call status "cancelled"，error 为「已取消」
```

Transport 不把失败态 `content` 写入 `ToolCallStatusChangedEvent.error`；进程恢复时的 `ToolCallsSettledEvent` 也只写「执行异常」「已取消」。错误态 `display_data` 不再包含对应工具成功态的 `kind`、path、URL、结果列表、diff 或终端输出。

### 2.6 各工具错误短提示（与代码对齐）

| 工具 | 默认提示 | 具体提示 |
| --- | --- | --- |
| `read_file` | `读取失败` | `无法读取`、`路径无效` |
| `read_image` | `执行失败` | `无法读取图片`（全部失败才是错误态；部分失败是成功态的 `failures`） |
| `write_file` | `写入失败` | — |
| `patch_write` | `替换失败` | — |
| `apply_patch` | `补丁失败` | — |
| `delete_file` | `删除失败` | — |
| `move_file` | `移动失败` | — |
| `search_content` | `搜索失败` | `搜索超时`、`正则无效` |
| `find_files` | `搜索失败` | `搜索超时` |
| `list_directory` | `列举失败` | — |
| `execute_terminal` | `命令失败` | `命令超时` |
| `terminal_*` | `执行失败` | `用户输入中`、`用户命令中`、`缺少集成`、`终端失败`、`已在交接中` |
| `web_search` | `搜索失败` | — |
| `web_extract` | `提取失败` | `部分成功`（成功态） |
| `browser_*` | `执行失败` | `标签不存在`、`标签已满`、`地址被拦截`、`页面加载失败`、`页面崩溃`、`页面无响应`、`用户正在操作`、`页面有弹窗`、`已在交接中`、`截图保存失败`、`元素已失效`、`元素不可见`（元素截图）、`路径无效`（上传的文件不可用，整次不执行）、`宿主不可用`、`宿主超时`（`browser_act` 中途某一步失败不是工具失败，结论写在正文，短提示放在 `steps` 里停下的那一步） |
| `delegate_task` | `委派失败` | — |
| `fork_task` | `分叉失败` | — |
| `wait_messages` | `执行失败` | `无可等待` |
| `agent_team` | `执行失败` | — |
| 任一声明 `returns_images` 的工具 | — | `模型不支持图片`（准入门禁） |

文件写入后的语法检查不属于 UI：即使检查失败也不生成短提示，按文件写入本身的结果展示。

## 3. 内置工具契约

每行：`kind` 列是该工具成功态会产出的展示 `kind`（「无」= 空 `display_data`，走通用调用行）；`heading` 列是前端目录算出的 `title · target`。

| 工具 | `kind` | `heading` | 动态展示字段 |
| --- | --- | --- | --- |
| `read_file` | `read-file-meta` | 读取文件 · `path` | `path`、实际展示的 `line_range`、`file_size`；因字符预算截断时附 `truncated` 与固定 `status_hint` |
| `read_image` | `image-read` | 查看图片 · 首个 `path` +N | `images`（每项 `path`、`asset_id`、`width`、`height`）、`failures`（每项 `path`、`hint`） |
| `write_file` | `file-changes` | 写入文件 · `path` | `changes`、`diff_stats` |
| `patch_write` | `file-changes` | 替换文本 · `path` | 与 `write_file` 相同 |
| `apply_patch` | `file-changes` | 应用补丁 · 补丁首个文件 +N | 只含既有文件 `modified` 的多文件 `changes`、`diff_stats` |
| `delete_file` | `file-changes` | 删除文件 · `path` | 一条 `deleted` 变更，只含 `path` 与 `status`（`patch` 为 `null`，不携带被删内容） |
| `move_file` | `file-changes` | 移动文件 · `source → destination` | 一条 `moved` 变更，含源路径与目标路径 |
| `search_content` | `content-search-results` | 搜索内容 · `"pattern" in path` | `pattern`、`path`、`matches`、`page`、`total_rows`、`match_count`、扫描统计 |
| `find_files` | `file-list` | 查找文件 · `pattern in path` | `pattern`、`path`、`files`、`page`、`match_count` |
| `list_directory` | `directory-list` | 查看目录 · `path` | `path`、`entries`、`page`、`total_entries` |
| `execute_terminal` | `terminal-result` | `description` · 命令首行 | 终态 `command`、`workdir`、`output`、`exit_code`、`timed_out`；后台占位只有 `command`、`workdir`，输出照常实时追加（见 3.4） |
| `terminal_list` | `terminal-session` | 列出终端 · 无 | `session_count` |
| `terminal_start` | `terminal-session` | 启动终端 · `label` 或 `shell` | `session_id`、`status`、`exit_code`、`initial_cwd`、`shell_kind`、`label`；实时状态读 snapshot `terminal_sessions` |
| `terminal_read` | `terminal-session` | 读取终端 · `session_id` | `session_id`、`status`、`lines`、`screen`、`wait_result` |
| `terminal_write` | `terminal-session` | 输入终端 · `session_id · 输入摘要` | 同上，另有 `input_summary` |
| `terminal_handoff` | `terminal-session` | 交给用户输入 · `session_id` | 同 `terminal_write`，`input_summary` 为交接原因；等待占位 `pending` 且 `awaitingUser` 时卡片提供「交还」；进行中的交接另见 snapshot 顶层 `handoffs` |
| `terminal_close` | `terminal-session` | 关闭终端 · `session_id` | `session_id`、`status`、`exit_code` |
| `web_search` | `web-search-results` | 网页搜索 · `query` | `query`、`results`（每项只有 `title`、`url`） |
| `web_extract` | `web-extract-urls` | 网页正文提取 · 首个 `url` +N | `urls`（每项只有 `url`）；部分成功带 `status_hint` |
| `browser_navigate` | `browser-page` | 打开网页 · `url` 或 `action` | `tab_id`、`url`、`title` |
| `browser_snapshot` | `browser-page` | 读取网页 · `tab` | 同上 |
| `browser_act` | `browser-page` | 操作网页 · 首个动作类型 +N | 同上，另有 `steps`（每一步的 `action`、`target`、`status`：`done` / `failed` / `skipped`，失败带 `hint`） |
| `browser_screenshot` | `browser-page` | 网页截图 · `full page` 或 `tab` | 同上，另有 `screenshot`（附件 asset id） |
| `browser_tabs` | 无 | 管理标签页 · `action [tab]` | — |
| `browser_logs` | `browser-page` | 读取网页日志 · `kind` | 同 `browser_navigate` |
| `browser_evaluate` | `browser-page` | 执行网页脚本 · 表达式开头一段 | 同 `browser_navigate`；脚本抛错是脚本的结果，按成功返回、正文写明报错 |
| `browser_handoff` | `browser-page` | 交给你操作网页 · `reason` | 同 `browser-page`，另有 `handoff_reason`；等待占位 `awaitingUser` |
| `delegate_task` | `delegation-result` | `agent_name` · `child_agent_id` | `title`、`child_agent_id`、`role`、`delegation_id`、`child_task_id`；无状态字段（见 3.7） |
| `fork_task` | `fork-task-result` | 分叉对话 · `title` | `title`、`child_task_id`；无状态字段（见 3.7） |
| `send_message` | `agent-task-result` | 发送消息 · `#task_id` | `operation=send`、`direction`（`down` 发给自己派生的 Task / `up` 发给派生自己的一方）、`task_id`、`agent_name`、`final_output`、`end_reason` |
| `task_status` | `agent-task-result` | 读取任务状态 · `#task_id` | `operation=status`、`direction=down`，其余同上（查询时刻的快照） |
| `wait_messages` | 无 | 等待结果 · 无 | — |
| `propose_agent_configuration` | `agent-configuration-draft` | 生成子 Agent 配置草稿 · `agent_id` | `status=draft`、`agent_id`、`role`、`description`、`system_prompt` |
| `propose_agent_team_configuration` | `agent-team-configuration-draft` | 生成 Agent Team 配置草稿 · `name` | `status=draft`、`team_id`、`name`、配置预览（不含 system prompt 与密钥） |
| `agent_team` | `agent-team-approval` | 运行已有 Agent Team · `team_id` | 目标、节点、边、配置与 `team_run_id`；是否待确认由前端按 `team_run_id` 读 TeamRun 状态 |
| `mcp` | `mcp-tool-result` | 调用 MCP · `server/tool`（list 为服务名或无） | `action=call`：`server_name`、`tool_name`；`action=list`：`servers` |
| `skill` | `skill-content` | 加载 Skill · `skill_name[/resource_path]`（list 无） | `action=load`：`skill_name`、`resource_path`；`action=list`：`skills` |

### 3.1 文件读取

`read_file` 只展示读取目标、实际展示的起止行号和文件大小；不展示正文、总行数或下一页 offset。读到行尾时范围渲染为 `L<start>-END`，否则 `L<start>-L<end>`。只有因字符预算截断时额外带 `truncated: true` 与固定 `status_hint: "文件过大，已截断"`；分页到达 `limit` 不算截断。

```json
{ "kind": "read-file-meta", "path": "app/main.py", "line_range": { "start": 1, "end": 200 }, "file_size": 18320 }
```

### 3.2 文件修改

`write_file`、`patch_write`、`apply_patch`、`delete_file`、`move_file` 共用 `file-changes`。`changes` 只携带客户端可直接解析的 Git 风格 `patch`、`status`、路径和增删行数，不携带完整正文；patch 不因模型输出预算截断。`delete_file` 不携带 `patch`（恒为 `null`）、增删计数恒为 0；`move_file` 返回 `moved` 与 `new_path`。

```json
{
  "kind": "file-changes",
  "changes": [
    { "path": "src/example.py", "new_path": null, "status": "modified",
      "patch": "diff --git a/src/example.py b/src/example.py\n--- a/src/example.py\n+++ b/src/example.py\n@@ -1,2 +1,3 @@\n ...",
      "insertions": 2, "deletions": 1 }
  ],
  "diff_stats": { "total_files": 1, "total_insertions": 2, "total_deletions": 1 }
}
```

非删除变更的 `patch` 以 `diff --git` 开头并含标准 `---` / `+++` 与 hunk 头；客户端不得按 `before` / `after` 重拼。语法检查诊断只走模型通道，不进 `display_data`。

### 3.3 搜索和目录

`search_content`、`find_files`、`list_directory` 各用自己的 `kind`。`search_content` 的命中行经有界的 `matches` 展示，不解析模型正文；目录条目至少含 `name`、`type`、`path`。

### 3.4 终端

终端展示原始命令、工作目录和完整输出，输出原样（保留 ANSI 与敏感文本）、不设 UI 字符预算、不插截断标记。运行期间后端以有界批次更新 snapshot，批次只用于分帧，不丢弃或改写输出；实时通道随命令而不随调用结束，转到后台后照常推送。是否追加只看 part 自己的状态（前台 `running`，或带 `background` 的 `pending` 占位），不看所属 Run；命令结束、取消或撤销后迟到的块丢弃。终态 `display_data` 替换临时内容（已实时流出的 part 以实时累计输出为准）。

命令转到后台（模型选后台或用户转后台）时卡片是后台占位：状态 `pending`、`background: true`，展示数据只有命令与工作目录。结论分两份：给模型的载荷过输出预算、经 `background_result` 回投；给卡片的完整 `terminal-result` 随同一条消息持久化，并入时与 `completed` 一起写回。撤销时卡片推到 `cancelled`、不写终态展示。「在后台」不写进 `display_data`：所有后台结果的占位都由 part 的 `background: true` 表达（投影自占位行 metadata，结算后保留）。

超时属于 `tool_error`：只展示失败与「命令超时」，不展示输出或退出码。

### 3.5 网页

`web_search` 的结果项只有 `title`、`url`，不带摘要或排名。`web_extract` 只展示 URL 列表（每项只有 `url`，保持后端顺序），不展示正文、metadata、原始响应或逐站点错误；网站名称和图标由前端从 URL 处理。整体成功、失败、部分成功由 Transport 状态与受控短提示表达。

### 3.6 内置浏览器

除 `browser_tabs`（没有展示数据）外的 `browser_*` 工具共用 `browser-page`：标签、地址与标题，截图带附件 asset id，交接带要用户做的事，act 带每一步（目标说明来自宿主给的元素角色与名称，只用于显示；结局按宿主报的停在哪一步定）。快照、日志条目与脚本返回值只给模型。卡片据 `tab_id` 在浏览器面板打开实时页面。

### 3.7 委派与子 Agent

`delegate_task` 的卡片展示委派身份，不展示完整 prompt，也不混入子 Agent 工具的 diff、终端输出或正文：

```json
{ "kind": "delegation-result", "title": "Review the parser", "child_agent_id": "reviewer", "role": "Reviewer", "child_task_id": 34 }
```

`fork_task` 的卡片同样只带分叉对话的 `title` 与 `child_task_id`（`{ "kind": "fork-task-result", "title": "拆出前端部分", "child_task_id": 41 }`），状态读应用级任务状态推送；分叉是侧栏对话，「打开」跳到它的路由而不是工作台，卡片出现时侧栏里还没有它就补刷一次目录。

派生 Task 按 Task 寻址：展示数据只携带 Task id，不携带 run_id；`delegation-result` / `fork-task-result` 的 `child_task_id` 与向下发送（`direction=down`）的 `agent-task-result.task_id` 都提升到 part 的 `child_task_id`，行据此读应用级任务状态推送显示对方的运行状态。展示数据里没有状态字段，后端也不再回写它。`send_message` 与 `task_status` 用同一形状，以 `operation` 区分；`direction=up` 的卡片只能打开对方、不能停止它（对方是上级）。打开按对方 Task 的类型：侧栏对话跳路由，委派子 Agent 在工作台打开。派生 Task 的结论与上级的回复都不经工具展示，而是以 `background_result` 回投（`tool_name` 为 `child_agent` / `agent_reply`）；`wait_messages` 只让 Agent 挂起，没有展示数据。

### 3.8 Agent Team

`agent_team` 首次调用建 pending TeamRun 并返回 `agent-team-approval` 预览卡；是否待确认由前端按 part 的 `team_run_id` 读 TeamRun 状态，不在 `display_data` 里放审批声明。`propose_agent_team_configuration` 返回不含 system prompt 与密钥的配置草稿。

## 4. 展示纯函数的位置

每个展示模块持有该工具族的 `display_data` 构造函数；handler 只引用，不内联字典：

```text
app/tools/display/
├─ filesystem_display.py   read_file / search_content / find_files / list_directory
├─ file_change_display.py  write_file / patch_write / apply_patch / delete_file / move_file
├─ image_display.py        read_image
├─ terminal_display.py     execute_terminal / terminal_*
├─ browser_display.py      browser_*
├─ mcp_display.py          mcp
└─ skill_display.py        skill
app/core/tool/handler/
├─ child_agent/{delegation_display.py, child_agent_display.py, agent_configuration_display.py}
├─ agent_team/agent_team_display.py
└─ web/web_display.py
```

函数接收领域结果，返回新的普通字典；不执行工具、网络请求、数据库写入或前端渲染。

## 5. 验收要求

### 后端

- 全部注册工具在前端工具目录里都有条目；守卫测试按注册表遍历。
- 每条成功、失败、取消路径都能在安全范围内构建对应的 `display_data`；失败只给 `status_hint`。
- `display_data` 在 allowlist 投影后再进入统一大小预算：`delete_file` 不携带被删内容、`move_file` 只携带 rename 元数据，体积不随目标文件增长。
- `web_search` 不出现 description / position；`web_extract` 不出现 content / metadata / 逐站点状态 / provider 原始 payload。
- `artifact_data` 不进入 Assistant Transport；`ToolObservation.content` 不被当作通用 UI 展示数据来源。

### 前端

- 调用行的图标、标题与目标按工具目录从 `toolName` + `args` 算出；分区按 `display_data.kind` 与 `awaitingUser`，不按工具名写渲染分支，状态与结果也不从 `args` / `result` 推断。
- 未知 `kind` 走安全兜底，不让消息流崩溃。
- `file-changes` 里 `status === "deleted"` 的变更只渲染路径与「已删除」，不解析 `patch`。
- 子 Task 状态只读应用级任务状态推送，不读展示数据。

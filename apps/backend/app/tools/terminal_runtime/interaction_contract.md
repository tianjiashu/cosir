# 交互终端契约

终端归 Task 所有，Agent 与用户以对等身份使用同一个 PTY：都能新建、输入、读取；输入仲裁只约束 Agent，用户输入从不因 Agent 被拒。设计与参数见 `docs/plan/terminal-workbench-design.md`；本文只记稳定契约与职责边界。会话、输入仲裁、等待与交接都是进程内协调状态，不持久化、不跨后端重启。

## Agent 工具（6 个）

- `terminal_start(shell, label, cwd, integration)`：新建会话；`integration=auto` 时等集成就绪（超时记为 unavailable）再返回一行会话摘要。Agent 新建每 Task 最多 `MAX_AGENT_SESSIONS_PER_TASK` 个，用户只受全局上限。
- `terminal_write(session_id, operation_id, input, wait)`：`input` 为 `line{text}`（一行 + 一次 Enter，拒绝控制字符）、`text{text}`（可打印字符、不带 Enter，用于全屏程序单键命令与单键确认）、`keys{keys[1..32]}`、`signal{interrupt|suspend|eof}`。返回输入之后的新增行。`operation_id` 在最近接受窗口内去重，重试不重发输入。
- `terminal_read(session_id, start_line, limit, tail, pattern, wait)`：按绝对行号读取；默认尾部 40 行，`limit ≤ 400`。`pattern`（Python 正则，不与 `tail` 同用）把读取变成搜索：从 `start_line`（缺省最早保留行）扫到末行，只返回命中行，`limit` 限制命中数，超出时提示续搜的 `start_line`；搜索总按逻辑行。无变化、无等待且涉及可变区的重复读取只返回一行提示。
- `terminal_list()`：纯文本，每个会话一行（创建者、当前状态与最近命令结果，状态措辞与 read / write header 同源），活跃会话在前、随后是最近结束的会话；末行是宿主可用 shell（`ShellResolver.available()`，只标注默认项与缺集成的例外）。
- `terminal_handoff(session_id, reason)`：把输入交给用户（HITL，见「交接」）。
- `terminal_close(session_id)`。

## 结束通知

Agent 创建的会话在它不知情时结束（shell 退出、worker 故障、用户关闭记为 `closed_by_user`），且当时没有条件等待或交接会把 `session_ended` 带回给它，runtime 经副作用执行器投一条 `system_notice`（`notice_kind=terminal_session_ended`，正文含会话摘要与尾部若干行）：不唤醒 Run，随下一次推进并入。Agent 自己关闭、启动失败、Task 删除与后端关闭不通知。

非交互的多行脚本走 `execute_terminal`；人工输入与交互会话输入都不经 denylist。

## 逻辑行缓冲（`line_buffer.py`）

worker 交出物理行：滚出的 `history`（软换行行不带 `\n`）与当前屏幕 `lines`（带 `wrapped`）。Python 按 `\n` 封口成逻辑行，行号 1 基、单调、永不复用；未封口段与屏幕顶部续接。可变区 = 当前屏幕按 `wrapped` 合并的逻辑行，从 `live_from_line` 起编号，重绘 / `clear` 只影响可变区。超过 10 000 行或 2 MiB 字符淘汰最旧行并推进 `first_available_line`。备用屏幕（全屏程序）不进入逻辑行编号：进入时冻结主屏可变区（行号与光标保持进入前的值），屏幕物理行另存为屏幕视图，退出后 worker 重发主屏、可变区照常重建；否则全屏重绘会让同一行号先后指向不同内容。

## 观察文本（`render.py`）

头部一两行：会话、shell、状态、正在执行的命令、集成状态、cwd、用户是否在打字 / 交接（用户启动的命令标 `started by user`）；`result:` 行给输入摘要、等待结束原因、最近命令的退出码与 `output lines a-b`；范围行给 `lines a-b of first-last (live from L) · cursor line C · user typed lines x-y`。正文为 `行号│文本`。write / 条件等待的新增行超预算保留头 20 + 尾 60 并标出 `--- omitted lines a-b ---`；尾部读取从前裁，按 start_line 读取从后截并提示续读行号。总量受 `Tools.MAX_OUTPUT_CHARS` 约束。不变元数据只在 start / list / close 返回。全屏程序期间 write、不带 start_line 的 read 与后台结论（`screen` 字段代替 `tail`）给屏幕视图：整屏以 `s<行>│文本` 返回，屏幕行号与逻辑行号分属两套编号；带 start_line 的 read 仍读冻结的主屏逻辑行；`match` 等待扫描整屏，命中记 `matched_screen_row`，输入后的等待须等屏幕在输入后至少更新一次（输入前已可见的内容不算命中），读取时的等待以当前屏幕为准；用户在全屏程序里的输入只标「有输入」，不标行号。

## Shell 集成与命令区间（`observation.py`）

命令只由集成事件创建。worker 保证语义事件前的输出与屏幕快照先到，因此 `command_started` 时光标所在逻辑行为输出起点；`command_returned` 时光标在行首取上一行为终点，否则取光标行；PowerShell / pwsh 在下一次读取输入时才报告返回，终点恒为光标行 − 1。zsh、bash、PowerShell、pwsh 有集成；cmd、fish、自定义路径降级。

## 条件等待（`wait.py`）

`wait{until: command_returned|output_quiet|match|elapsed, pattern, quiet_ms, timeout_ms}`。判定在会话锁内由 worker 事件 `notify_all` 唤醒；`WAIT_CANCEL_CHECK_SECONDS` 时间片只复查取消（Task 停止或用户取消这次调用），不轮询条件。结束原因：条件名（`matched` 带 `matched_line`）、`timeout`、`elapsed`、`cancelled`、`session_ended`；read 的 `command_returned` 在无执行中命令时返回 `idle`。无集成时 `command_returned` 在参数边界报 `TERMINAL_INTEGRATION_REQUIRED`。write 默认 `output_quiet 500ms / 3s`。

等待只在前台、在工具线程里持会话锁进行，不登记延迟结果，总超时上限 30s（`WAIT_MAX_TIMEOUT_MS`）：交互终端只服务短交互，长构建与测试交给 `execute_terminal` 的后台执行。后端关闭时 lifespan 先 `deferred_registry.close()`，关闭期间结束会话唤醒的交接不再写 `task_messages`。`match` 条件在行输入时从命令回显行的下一行开始扫描，读取与按键从光标行开始。

## 输入仲裁

没有租约，也没有持有方：仲裁只在 Agent 的 write（line / text / keys / signal）送达前判定，用户输入从不被拒（只在会话已结束或背压时未送达）。两条规则：

- 前台是用户启动的命令（集成记录的命令发起方 `started_by=user`，取命令开始时最近一次被接受输入的来源）时立即拒绝，`TERMINAL_USER_COMMAND_RUNNING`（不可重试）：命令可能长驻（dev server、编辑器、ssh），等待会让 Agent 无声卡住，往里打字又会落进用户的程序；Agent 应换会话或询问用户。没有 shell 集成的会话看不到命令边界，不做这条判定。
- 用户最近 3 秒内按过键时，Agent 最多等 10 秒让其停手，超时 `TERMINAL_USER_TYPING`（retryable）：只防 Agent 的输入拼进用户的半行，按按键时间判定，不需要用户释放。

Agent 不持锁：一次输入是 worker 输入队列里的一帧，用户按键切不开它；终端工具串行执行，同一会话不会有两个 Agent 输入并发。Agent 的条件等待期间用户照常输入，用户输入时记录光标所在逻辑行，Agent 下一次观察报告 `user typed lines` 后清空。

## 交接

`terminal_handoff` 是 `execute` 型 HITL 工具（`user_decision` 只能按工具声明，所以不是 `terminal_write` 的一种输入）。首次调用在会话上登记交接（`_Handoff`：调用 id、原因、交接时的末行），返回等待占位挂起 Agent；交接不改变输入仲裁、不登记延迟结果，snapshot 条目的 `handoff` 给出 `{reason, toolCallId}`。用户「交还」（终端面板提示或聊天卡片上的按钮）即经通用决定入口对这次调用作出决定（`ToolDecisionService.submit`），决定到达后 `prepare_input` 绕过模型重放本调用：`end_handoff` 清除交接，报告交接以来的输出（会话已结束时报告尾部，会话已不在时报告不存在）。用户停止打字不结束交接（用户输密码等操作时会停顿）。交接中的会话意外结束（shell 退出、worker 故障、用户关闭）时由运行时代为提交决定（`reject`，`source=runtime`），Agent 不会一直挂着；后端关闭与 Task 删除不代为落定，Run 停止也不清除交接——等待中的决定按 HITL 契约保留，之后仍可交还。

## service 与线程边界（`session_service.py` / `session_runtime.py` / `session_effects.py`）

- service 拥有会话集合、Task 归属、容量与终态历史（每 Task 最多 10 个已结束会话）；runtime 持单会话锁、输入仲裁、观察、等待与唯一终态迁移。
- worker 读线程只做内存更新；终态收尾（`worker.close`）在独立线程。worker 握手不持会话锁（启动期间快照投影、订阅不被阻塞），握手与关闭由每会话启动互斥串行化；`starting` 期间输入被拒，resize 记下、握手后补发。用户输入先入队，成功后才记录按键时间、来源与标注。会话变化的 snapshot 重投影（`TerminalSessionRuntime.notify_changed`）、结束通知与交接代为落定经单线程副作用执行器在锁外串行执行。
- 会话列表 snapshot 条目：`TerminalSessionService.task_runtimes` 顺序（活跃在前按创建时间、已结束最多 10 个）下各会话的 `snapshot()`，由 `assistant_transport` 的 `to_terminal_session` 映射成 camelCase；`label` 缺省为空串。

## 交互通道（`stream.py`，`api/terminal_stream_api.py`，`terminal-v2`）

每会话一条 WS。订阅绑定事件循环与 `asyncio.Queue`，发布经 `loop.call_soon_threadsafe`；背压按帧数与字节数计量，溢出投递一次 `subscriber_overflow`，WS 转为 `resync_required` 并以 1013 关闭；事件循环已关闭时在订阅边界吞掉并记日志。

- 客户端帧：`attach{after_seq}`（首帧）、`input{data_base64}`（≤ 64 KiB）、`resize{rows 2..500, cols 10..1000}`（交还交接走 HTTP 决定入口，不经 WS）。
- 服务端帧（带 `protocol`、`session_id`、`generation`）：`attached{rows, columns, status, first_available_seq, next_seq}`、`output{seq, data_base64}`、`geometry{rows, columns}`、`input_mark{by:"agent"}`、`input_rejected{reason: session_ended|backpressure}`、`exit{}`（只标记流结束）、`resync_required`、`protocol_error{code, message}`（`code` 与 HTTP 错误体同源：领域错误的稳定码，控制帧非法为 `TERMINAL_STREAM_PROTOCOL_ERROR`；客户端按 `code` 分支，`message` 只供排查）。
- 交接、终态与退出码只经 snapshot `terminal_sessions` 投影，WS 不重复推送（两条通道无顺序保证，双推会合并出过期值）。
- 连接时按 Task 归属取到会话 runtime，之后输入直接调用它（内存更新 + 非阻塞入队），不经线程池。断开只移除订阅，不关闭会话、不取消 Run。

## Worker 协议

4 字节大端长度 + JSON。`write{input: line|text|keys|raw}`、`resize{rows, cols}`、`signal`（与 write / resize 走同一有序输入队列，不越过排队中的输入；等待 `signal_result` 回执，必须锁外发送）；内容校验失败为非致命 `error{input_rejected|resize_rejected}`。能力来自握手：`input_keys`、`key_<name>`、`signal_*`；runtime 投递 keys / signal 前复查（line、raw、resize 不需要能力声明）。

## 验证入口

- `test_terminal_line_buffer.py`：行号、软换行、可变区、淘汰、备用屏幕、clear、命令区间、渲染预算。
- `test_terminal_session_service.py`：条件等待（事件唤醒、取消、超时）、输入仲裁、交接、snapshot 投影、容量。
- `test_terminal_stream.py`：asyncio 桥接、背压、WS v2 帧。
- `test_terminal_session_tools.py`：工具面、按平台 schema、展示数据、错误短提示。
- `test_terminal_shell_integration.py` / `test_terminal_worker_integration.py`：真实 worker（zsh、/bin/bash 3.2、Homebrew bash 5.x；Windows 上 PowerShell / pwsh）。

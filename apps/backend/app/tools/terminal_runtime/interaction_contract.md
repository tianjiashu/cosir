# 交互终端契约

终端归 Task 所有，Agent 与用户以对等身份使用同一个 PTY：都能新建、输入、读取，输入以租约互斥。设计与参数见 `docs/plan/terminal-workbench-design.md`；本文只记稳定契约与职责边界。会话、租约、等待与交接都是进程内协调状态，不持久化、不跨后端重启。

## Agent 工具（5 个）

- `terminal_start(shell, label, cwd, integration)`：新建会话，返回元数据（geometry、集成状态、本会话可用按键与信号）。Agent 新建每 Task 最多 `MAX_AGENT_SESSIONS_PER_TASK` 个，用户只受全局上限。
- `terminal_write(session_id, operation_id, input, wait)`：`input` 为 `line{text}`（一行 + 一次 Enter，拒绝控制字符）、`keys{keys[1..32]}`、`signal{interrupt|suspend|eof}`、`handoff{reason}`。返回输入之后的新增行。`operation_id` 在最近接受窗口内去重，重试不重发输入。
- `terminal_read(session_id, start_line, limit, tail, wait)`：按绝对行号读取；默认尾部 40 行，`limit ≤ 400`。无变化、无等待且涉及可变区的重复读取只返回一行提示。
- `terminal_list()`：活跃会话 + `available_shells`（`ShellResolver.available()`）。
- `terminal_close(session_id)`。

非交互的多行脚本走 `execute_terminal`；人工输入与交互会话输入都不经 denylist。

## 逻辑行缓冲（`line_buffer.py`）

worker 交出物理行：滚出的 `history`（软换行行不带 `\n`）与当前屏幕 `lines`（带 `wrapped`）。Python 按 `\n` 封口成逻辑行，行号 1 基、单调、永不复用；未封口段与屏幕顶部续接。可变区 = 当前屏幕按 `wrapped` 合并的逻辑行，从 `live_from_line` 起编号，重绘 / `clear` 只影响可变区。超过 10 000 行或 2 MiB 字符淘汰最旧行并推进 `first_available_line`。备用屏幕按物理行返回并在头部标出。

## 观察文本（`observation_render.py`）

头部一两行：会话、shell、状态、正在执行的命令、集成状态、cwd、租约 / 交接；`result:` 行给输入摘要、等待结束原因、最近命令的退出码与 `output lines a-b`；范围行给 `lines a-b of first-last (live from L) · cursor line C · user typed lines x-y`。正文为 `行号│文本`。write / 条件等待的新增行超预算保留头 20 + 尾 60 并标出 `--- omitted lines a-b ---`；尾部读取从前裁，按 start_line 读取从后截并提示续读行号。总量受 `Tools.MAX_OUTPUT_CHARS` 约束。不变元数据只在 start / list / close 返回。

## Shell 集成与命令区间（`terminal_observation.py`）

命令只由集成事件创建。worker 保证语义事件前的输出与屏幕快照先到，因此 `command_started` 时光标所在逻辑行为输出起点；`command_returned` 时光标在行首取上一行为终点，否则取光标行；PowerShell / pwsh 在下一次读取输入时才报告返回，终点恒为光标行 − 1。zsh、bash、PowerShell、pwsh 有集成；cmd、fish、自定义路径降级。

## 条件等待（`terminal_wait.py`）

`wait{until: command_returned|output_quiet|match|elapsed, pattern, quiet_ms, timeout_ms}`。判定在会话锁内由 worker 事件 `notify_all` 唤醒；`WAIT_CANCEL_CHECK_SECONDS` 时间片只复查 Run 取消，不轮询条件。结束原因：条件名（`matched` 带 `matched_line`）、`timeout`、`elapsed`、`cancelled`、`session_ended`；read 的 `command_returned` 在无执行中命令时返回 `idle`。无集成时 `command_returned` 在参数边界报 `TERMINAL_INTEGRATION_REQUIRED`。write 默认 `output_quiet 500ms / 3s`。

`timeout_ms > 30000`：以本次工具调用的 `tool_call_id` 在 `deferred_results`（`core/workflow/deferred/`，设计见 `docs/plan/deferred-tool-result-design.md`）登记 `terminal_wait` 条目，立即返回当前观察并 `suspend`；独立等待线程在条件满足或超时后经 `deferred_results.complete(tool_call_id, ...)` 投递带 `tool_call_id` 的 `background_result`（出队成功才写库）。Run 停止时撤销该 Task 的全部条目、撤销回调唤醒线程，不投递；后端关闭时 lifespan 先 `deferred_results.close()`，关闭期间结束会话唤醒的等待与交接都不再写 `task_messages`。`match` 条件在行输入时从命令回显行的下一行开始扫描，读取与按键从光标行开始。

## 输入租约

每会话一个持有方（`agent` / `user` / 空闲）。Agent 的 write / keys / signal 取得租约，投递 + 等待窗口结束释放（后台等待在投递后即释放）；用户持有时 Agent 最多等 10 秒，超时 `TERMINAL_INPUT_HELD_BY_USER`（retryable）。用户输入取得租约，空闲 3 秒释放；shell 处于 executing 时保持到命令返回与空闲超时中更晚者；`release_user_input` 手动释放。Agent 持有时用户输入被拒（`rejected_agent_input`），单字节 Ctrl-C 例外：照常送达并把租约转给用户。用户输入时记录光标所在逻辑行，Agent 下一次观察报告 `user typed lines` 后清空。

## 交接

`input.handoff` 以本次调用的 `tool_call_id` 登记 `terminal_handoff` 条目并 `suspend`，snapshot 与 WS 标出交接。只有用户「交还」（`complete_handoff`）或会话结束时经 `deferred_results.complete` 投递 `background_result`（含尾部行）；输入租约的空闲或手动释放不结束交接（用户输密码等操作时会停顿）。Run 取消撤销交接，只清提示，终端不受影响。

## service 与线程边界（`terminal_session_service.py` / `terminal_session_runtime.py`）

- service 拥有会话集合、Task 归属、容量与终态历史（每 Task 最多 10 个已结束会话）；runtime 持单会话锁、租约、观察、等待与唯一终态迁移。
- worker 读线程只做内存更新；终态收尾（`worker.close`）在独立线程。worker 握手不持会话锁（启动期间快照投影、订阅不被阻塞），握手与关闭由每会话启动互斥串行化；`starting` 期间输入被拒，resize 记下、握手后补发。用户输入先入队，成功后才改租约与标注。`on_task_sessions_changed(task_id)` 与交接结论投递经单线程副作用执行器在锁外串行执行。
- `project_task_sessions` 输出 snapshot 条目（camelCase）：活跃在前按创建时间、已结束最多 10 个；`label` 缺省为空串。

## 交互通道（`terminal_stream.py`，`api/terminal_stream_api.py`，`terminal-v2`）

每会话一条 WS。订阅绑定事件循环与 `asyncio.Queue`，发布经 `loop.call_soon_threadsafe`；背压按帧数与字节数计量，溢出投递一次 `subscriber_overflow`，WS 转为 `resync_required` 并以 1013 关闭；事件循环已关闭时在订阅边界吞掉并记日志。

- 客户端帧：`attach{after_seq}`（首帧）、`input{data_base64}`（≤ 64 KiB）、`resize{rows 2..500, cols 10..1000}`、`release_input{}`、`handoff_done{}`。
- 服务端帧（带 `protocol`、`session_id`、`generation`）：`attached{rows, columns, status, first_available_seq, next_seq}`、`output{seq, data_base64}`、`geometry{rows, columns}`、`input_mark{by:"agent"}`、`input_rejected{reason: agent_input|session_ended|backpressure}`、`exit{}`（只标记流结束）、`resync_required`、`protocol_error{code, message}`（`code` 与 HTTP 错误体同源：领域错误的稳定码，控制帧非法为 `TERMINAL_STREAM_PROTOCOL_ERROR`；客户端按 `code` 分支，`message` 只供排查）。
- 输入租约、交接、终态与退出码只经 snapshot `terminal_sessions` 投影，WS 不重复推送（两条通道无顺序保证，双推会合并出过期值）。
- 输入直接调用 service（内存更新 + 非阻塞入队），不经线程池。断开只移除订阅，不关闭会话、不取消 Run。

## Worker 协议

4 字节大端长度 + JSON。`write{input: line|keys|raw}`、`resize{rows, cols}`、`signal`（与 write / resize 走同一有序输入队列，不越过排队中的输入；等待 `signal_result` 回执，必须锁外发送）；内容校验失败为非致命 `error{input_rejected|resize_rejected}`。能力来自握手：`input_keys`、`key_<name>`、`signal_*`；runtime 投递 keys / signal 前复查（line、raw、resize 不需要能力声明）。

## 验证入口

- `test_terminal_line_buffer.py`：行号、软换行、可变区、淘汰、备用屏幕、clear、命令区间、渲染预算。
- `test_terminal_session_service.py`：条件等待（事件唤醒、取消、超时）、租约、后台等待与交接、snapshot 投影、容量。
- `test_terminal_stream.py`：asyncio 桥接、背压、WS v2 帧。
- `test_terminal_session_tools.py`：工具面、按平台 schema、展示数据、错误短提示。
- `test_terminal_shell_integration.py` / `test_terminal_worker_integration.py`：真实 worker（zsh、/bin/bash 3.2、Homebrew bash 5.x；Windows 上 PowerShell / pwsh）。

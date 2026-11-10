# 交互终端契约

终端归 Task 所有：会话只由用户在终端面板创建和关闭，Agent 只能列出、读取，以及把一行命令填进空提示符（不执行，回车只能由用户按）。本文只记稳定契约与职责边界，具体参数以代码为准。会话、填入准入与等待都是进程内协调状态，不持久化、不跨后端重启。

## Agent 工具（3 个）

- `terminal_list()`：纯文本，每个会话一行（当前状态与最近命令结果，状态措辞与 read / write header 同源），活跃会话在前、随后是最近结束的会话；末行是宿主可用 shell（`ShellResolver.available()`，只标注默认项与缺集成的例外）。
- `terminal_read(session_id, start_line, limit, tail, pattern, wait)`：按绝对行号读取；默认尾部 40 行，`limit ≤ 400`。`pattern`（Python 正则，不与 `tail` 同用）把读取变成搜索：从 `start_line`（缺省最早保留行）扫到末行，只返回命中行，`limit` 限制命中数，超出时提示续搜的 `start_line`；搜索总按逻辑行。无变化、无等待且涉及可变区的重复读取只返回一行提示。
- `terminal_write(session_id, command)`：把**一行**命令填进用户终端的提示符，**不执行、不等待**；参数模型与 worker 两侧都拒绝空串、回车与控制字符。只有 shell 集成就绪、停在提示符、且这个提示符出现后还没有任何输入时才允许（见「Agent 填入」）。返回填入后的尾部观察；用户是否按回车、之后发生了什么，由 `terminal_read` 观察。

## Agent 填入

- 只把一行文本写进 PTY，绝不发送回车：命令仍在提示符上，由用户核对后自己按 Enter。同一提示符至多填一次，这替代了旧的 `operation_id` 幂等缓存。
- 准入只读 shell 集成给出的既有事实，任一不满足即 `TERMINAL_PREFILL_REJECTED`（advice：把命令写进回复，或等用户清掉提示符后再试）：
  - 没有 shell 集成：看不到提示符，无法确认行里是否为空；
  - 有命令在执行，或尚未回到提示符（`phase != editing`）：填入会落进正在运行的程序；
  - `prompt_has_input` 为真：这个提示符出现之后，用户已经敲过字（或已经填过一次），再填会拼进已有内容。
- `prompt_has_input` 在用户原始输入被接受与 Agent 填入被接受时置真，在 `shell_prompt` 事件到达时复位；它是「这一行是否还空着」的唯一事实，与屏幕内容无关。
- 填入成功后发布 `input_mark{by:"agent"}`（终端里的 Agent 输入色条），不改变输入权：用户随后按键照常送达。
- 非交互的多行脚本走 `execute_terminal`；多步操作用户可先写脚本文件，再让 Agent 填运行它的一行命令。

## 逻辑行缓冲（`line_buffer.py`）

worker 交出物理行：滚出的 `history`（软换行行不带 `\n`）与当前屏幕 `lines`（带 `wrapped`）。Python 按 `\n` 封口成逻辑行，行号 1 基、单调、永不复用；未封口段与屏幕顶部续接。可变区 = 当前屏幕按 `wrapped` 合并的逻辑行，从 `live_from_line` 起编号，重绘 / `clear` 只影响可变区。超过 10 000 行或 2 MiB 字符淘汰最旧行并推进 `first_available_line`。备用屏幕（全屏程序）不进入逻辑行编号：进入时冻结主屏可变区（行号与光标保持进入前的值），屏幕物理行另存为屏幕视图，退出后 worker 重发主屏、可变区照常重建；否则全屏重绘会让同一行号先后指向不同内容。

## 观察文本（`render.py`）

头部一两行：会话、shell、状态、正在执行的命令、集成状态、cwd、用户是否在打字（用户启动的命令标 `started by user`）；`result:` 行给填入摘要、等待结束原因、最近命令的退出码与 `output lines a-b`；范围行给 `lines a-b of first-last (live from L) · cursor line C · user typed lines x-y`。正文为 `行号│文本`。条件等待返回的新增行超预算保留头 20 + 尾 60 并标出 `--- omitted lines a-b ---`；尾部读取从前裁，按 start_line 读取从后截并提示续读行号。总量受 `Tools.MAX_OUTPUT_CHARS` 约束；`terminal_list` 返回的一行摘要由同一份状态措辞投影。全屏程序期间 read 与后台结论（`screen` 字段代替 `tail`）给屏幕视图：整屏以 `s<行>│文本` 返回，屏幕行号与逻辑行号分属两套编号；带 start_line 的 read 仍读冻结的主屏逻辑行；`match` 等待扫描整屏，命中记 `matched_screen_row`；用户在全屏程序里的输入只标「有输入」，不标行号。

## Shell 集成与命令区间（`observation.py`）

命令只由集成事件创建。worker 保证语义事件前的输出与屏幕快照先到，因此 `command_started` 时光标所在逻辑行为输出起点；`command_returned` 时光标在行首取上一行为终点，否则取光标行；PowerShell / pwsh 在下一次读取输入时才报告返回，终点恒为光标行 − 1。zsh、bash、PowerShell、pwsh 有集成；cmd、fish、自定义路径降级。

## 条件等待（`wait.py`）

`wait{until: command_returned|output_quiet|match|elapsed, pattern, quiet_ms, timeout_ms}`。判定在会话锁内由 worker 事件 `notify_all` 唤醒；`WAIT_CANCEL_CHECK_SECONDS` 时间片只复查取消（Task 停止或用户取消这次调用），不轮询条件。结束原因：条件名（`matched` 带 `matched_line`）、`timeout`、`elapsed`、`cancelled`、`session_ended`；read 的 `command_returned` 在无执行中命令时返回 `idle`。无集成时 `command_returned` 在参数边界报 `TERMINAL_INTEGRATION_REQUIRED`。

等待只在前台、在工具线程里持会话锁进行，不登记后台结果，总超时上限 30s（`WAIT_MAX_TIMEOUT_MS`）：交互终端只服务短交互，长构建与测试交给 `execute_terminal` 的后台执行。后端关闭前 lifespan 先结束会话，唤醒的等待以 `session_ended` 返回。`match` 从光标行开始扫描（全屏程序扫描整屏）。

## service 与线程边界（`session_service.py` / `session_runtime.py`）

- service 拥有会话集合、Task 归属、容量与终态历史（每 Task 最多 10 个已结束会话）；runtime 持单会话锁、观察、填入准入、等待与唯一终态迁移。
- worker 读线程只做内存更新；终态收尾（`worker.close`）在独立线程。worker 握手不持会话锁（启动期间快照投影、订阅不被阻塞），握手与关闭由每会话启动互斥串行化；`starting` 期间用户输入与 resize 照常入队、握手后按序补发，Agent 填入只在 `running` 且集成 ready 后接受。用户输入先入队，成功后才记录按键时间、来源与标注。会话变化的 snapshot 重投影经单线程副作用执行器在锁外串行执行。
- 会话列表 snapshot 条目：`TerminalSessionService.task_runtimes` 顺序（活跃在前按创建时间、已结束最多 10 个）下各会话的 `snapshot()`，由 `assistant_transport` 的 `to_terminal_session` 映射成 camelCase；`label` 缺省为空串。

## 交互通道（`stream.py`，`api/terminal_stream_api.py`，`terminal-v2`）

每会话一条 WS。订阅绑定事件循环与 `asyncio.Queue`，发布经 `loop.call_soon_threadsafe`；背压按帧数与字节数计量，溢出投递一次 `subscriber_overflow`，WS 转为 `resync_required` 并以 1013 关闭；事件循环已关闭时在订阅边界吞掉并记日志。

- 客户端帧：`attach{after_seq}`（首帧）、`input{data_base64}`（用户原始按键，≤ 64 KiB；客户端把大段输入切成多帧按序发送，一次粘贴总量上限 256 KiB，超过整段拒绝而不截断）、`resize{rows 2..500, cols 10..1000}`。
- 服务端帧（带 `protocol`、`session_id`、`generation`）：`attached{rows, columns, status, first_available_seq, next_seq}`、`output{seq, data_base64}`、`geometry{rows, columns}`、`input_mark{by:"agent"}`（只在 Agent 填入时出现）、`input_rejected{reason: session_ended|backpressure}`、`exit{}`（只标记流结束）、`resync_required`、`protocol_error{code, message}`（`code` 与 HTTP 错误体同源：领域错误的稳定码，控制帧非法为 `TERMINAL_STREAM_PROTOCOL_ERROR`；客户端按 `code` 分支，`message` 只供排查）。
- 终态与退出码只经 snapshot `terminal_sessions` 投影，WS 不重复推送（两条通道无顺序保证，双推会合并出过期值）。
- 连接时按 Task 归属取到会话 runtime，之后对它的调用（输入、resize、订阅）一律经 `asyncio.to_thread`：它们要拿会话锁，而工具线程在同一把锁里执行 Agent 给的正则，事件循环不能碰这把锁。断开只移除订阅，不关闭会话、不取消 Run。
- 终端查询（设备属性、状态与光标位置、模式查询等）只由 worker 应答：有没有面板都要有人答，前端 xterm 在解析器处吞掉这些查询、不应答，否则程序收到两份应答，多余的一份还会被当成用户输入。

## Worker 协议

用户命令进程只由 Terminal Worker 启动与清理：本文的交互会话，以及 `execute_terminal` 的一次性命令（`tool_handler/terminal/local_backend.py`）。4 字节大端长度 + JSON。首帧定模式、之后不换：`start_session{shell, shell_kind, cwd, integration}` 或 `start_exec{program, args, raw_args, cwd}`（管道、无 stdin、无 PTY；先握手再启动）。握手带 `mode`，只有会话模式带 `pty_kind` 与尺寸，客户端按模式校验。`exit{reason, exit_code?, signal?, message?}` 是握手之后唯一的终态事件，每条结束路径恰好一次（只有 worker 的 stdout 已断开时发不出）：`reason` 为 `exited`（命令或 shell 自己结束，带退出码或信号）、`stopped`（后端要求停止且先于命令自己结束生效）、`heartbeat_timeout`、`start_failed`（执行模式命令没能启动）或 `failed`（worker 自身故障）；停止请求与子进程退出谁先到只由 worker 的 `LifecycleArbiter` 裁决，后端不按自己的标志推断。`error` 帧从不代表终态，只是非致命拒绝或诊断（如 `cleanup_failed`）。worker 没发 `exit` 就断开时，后端读线程补发 `disconnected`。`shutdown` 要求结束；后端关闭 stdin 等同 `shutdown`，15 秒收不到心跳时 worker 自行停止（`heartbeat_timeout`）。worker 由后端以独立会话拉起（POSIX），宿主强杀后端整组时不被波及，读到 stdin EOF 后自行清理再退出。

进程清理只在 worker 一处（`containment.rs`）：会话模式的归属是与 shell 同一个会话（shell 先退出时控制终端已被内核摘掉，会话号仍在，所以照样找得到它留下的进程），执行模式是命令的进程组，Windows 两种都挂 Job Object。顺序 HUP → TERM → 宽限 1 秒 → KILL，没有残留立即返回。归属按首进程 PID（即组号、会话号）认成员：POSIX 上首进程退出后只被观察（`waitid(WNOWAIT)`）、清理之后才回收，清理期间 PID 不会被复用。会话模式在会话结束（关闭、shell 自己退出、后端断开）时清理，会话里一条命令结束不清理；执行模式在命令结束、超时或取消时清理。主动 `setsid` 脱离会话的进程不在范围内。

会话模式的输入只有两种：`write{input: prefill{text}}`（Agent 填入，只写不执行）与 `write{input: raw{data_base64}}`（用户原始按键，含回车、控制键与 Ctrl-C 等字节）；另有 `resize{rows, cols}`。三者走同一有序输入队列（与 worker 的写线程顺序一致）。`prefill` 在 worker 侧再次拒绝空串与控制字符，按会话当前的 bracketed paste 模式位决定是否用 `ESC[200~`/`ESC[201~` 包裹（包裹只是让 shell 不触发用户自定义按键绑定；不包裹也不含控制字符、不会执行）；模式位反映读线程已解析到的输出，可能滞后，只影响包不包裹，不影响「不执行」。`raw` 原样写入。内容校验失败为非致命 `error{input_rejected|resize_rejected}`。写入 PTY 只经 worker 的专用写线程（有界队列）：前台程序不读输入时 PTY 只缓冲约 1 KB，控制循环与读线程都不能阻塞在写上；队列满时这一次写入被丢弃并报非致命 `error{input_backpressure}`，后端转成 `input_rejected{reason: backpressure}` 推给面板。

## 验证入口

- `test_terminal_line_buffer.py`：行号、软换行、可变区、淘汰、备用屏幕、clear、命令区间、渲染预算。
- `test_terminal_session_service.py`：条件等待（事件唤醒、取消、超时）、Agent 填入准入、snapshot 投影、容量。
- `test_terminal_stream.py`：asyncio 桥接、背压、WS v2 帧。
- `test_terminal_session_tools.py`：工具面（list / read / write）、参数边界、展示数据、错误短提示。
- `test_terminal_shell_integration.py` / `test_terminal_worker_integration.py`：真实 worker（zsh、/bin/bash 3.2、Homebrew bash 5.x；Windows 上 PowerShell / pwsh），含填入不执行、shell 自己退出与后端被强杀后的清理。
- `test_local_backend_process_cleanup.py`：执行模式的残留清理、超时温和终止、信号退出码、后端被强杀。
- Rust：`containment` / `exec` 单测与 `apps/terminal-worker/tests/exec_mode.rs`（经 stdio 帧驱动真实二进制）。

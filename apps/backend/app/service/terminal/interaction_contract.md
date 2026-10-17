# 交互终端契约

终端归 Task 所有。输入操作、shell 命令观察、会话生命周期是三个独立概念；`command_returned` 只表示顶层 shell 执行返回，不保证后台或挂起进程终止。输入、等待和事件历史均不进入 checkpoint 或业务 Run 状态机。

## 工具入口

- `terminal_start(shell, cwd, label, integration="auto")`：启动持久交互 shell。`off` 禁用集成；`auto` 按实际 shell 接入。worker 握手与集成就绪不同；初始化超时返回 `unavailable`，迟到的真实 ready 事件可以恢复能力。
- `terminal_write(session_id, operation_id, input, wait_ms=500)`：`input` 是单个 `{"type":"line","text":"..."}` 或 `{"type":"key","key":"ctrl_c"}` 对象，不接收数组、裸 text 或混排批次。完整行由 worker 保留原文 UTF-8 并追加一次 Enter；接受后等待 wait_ms 再返回当前快照，取消或会话结束可提前返回。
- `terminal_read(session_id, wait_ms=3000)`：只读当前快照。工具参数边界限制 3000～30000 毫秒，到期才返回；输出活动、shell_prompt、command_returned 不缩短等待。取消或会话终态可提前返回。工具总超时 35 秒。同会话只允许一个 read 等待，并发读取报可恢复错误，不排队占用额外超时；输入、信号和 worker 事件不被阻塞。
- `terminal_signal`：能力来自 worker 握手。Windows 支持 interrupt；Unix 支持 interrupt/suspend 和有条件的 canonical EOF。拒绝时保留受控原因码。投递成功不等于程序退出。
- `terminal_list/close`：发现跨 Run 会话、显式清理；Task 删除与后端退出继续负责兜底。

`operation_id` 只在最近接受的输入窗口内去重，指纹覆盖输入项，不包含等待条件。接受仅保证进入本机队列。取消只结束等待；重试已接受操作不重复输入，即使 shell 已退出。重试返回当前快照，不重放原调用的输出。

write 与 read 使用同一快照结构，每次返回当前 screen 与有界最近 history，允许重合和旧内容丢失。不接收 cursor 或 wait 条件，不返回 next_cursor、observed_cursor、events、has_more、gap 或 condition，不保留旧契约。长驻服务、静默命令和程序提问均可通过 read 观察，无须额外输入。需要完整输出时，应将命令结果写入文件，再用文件工具读取。


## 完整行与控制键

- line 的 text 必填但允许空字符串（只按 Enter），保留前后空格与缩进。拒绝真实 CR/LF、Tab、Escape 和其他 C0/C1 控制字符，不解码字面量反斜杠转义。包含最终 Enter 的输入最多 64 KiB。
- 多行结构逐次提交完整行；不接受 lines 数组，不自动拆分文本、不补括号或终止空行。zsh 的 `for ...; do` / `done`、PowerShell 的 `{` / `}` 由 shell 继续解析；传统 Python REPL 的复合语句由调用方显式提交空行结束。
- 同一个 line 可以是 shell 命令、REPL 表达式、密码或交互回答。工具不猜接收程序；输入接受不代表语法完整或开始执行。REPL 内部求值不产生根 shell 的 command_returned。
- 输入行是向当前终端发送文字和 Enter，不替换行编辑器已有内容；自动缩进、自动补全、用户键位绑定等由前台程序决定。高级 REPL 与全屏菜单不在通用兼容承诺内。
- Unix 控制键只声明 ctrl_c / ctrl_d；Windows 只声明 ctrl_c / ctrl_z。参数模型按同一宿主键集合生成 schema 并校验；不属于宿主的键在参数边界报错。session 与 read/write 返回的 capabilities 中 `input_ctrl_*` 来自真实 worker 握手，service 在接受前另检查实际会话能力，worker 在独立进程边界校验。
- key 只发送对应控制字符，不追加 Enter、不自动映射 EOF、不保证程序退出。需要后续 Enter 时显式发送空 line。terminal_signal 仍表示运行时信号操作，独立于按键投递。
- 没有 enter/tab/backspace/escape 或光标移动键；空行替代单独 Enter。Worker 输入格式随 backend/worker 源码同步演进，不承诺兼容旧二进制。

例如传统 Python REPL：依次提交 `for n in range(2):`、`    print(n)`、空字符串；程序提问则提交答案或空字符串接受默认值。两者使用同一 line 契约，是否完成始终由实际程序行为与 shell 事件决定。

## 当前观察与重复读取

返回包含 session_id、generation、state、integration、capabilities、latest_command、screen、history、history_truncated、return_reason；write 另含 input.accepted/duplicate。return_reason 为 wait_elapsed、cancelled 或 session_ended，不表示命令结果。read 的 run_id 由工具执行上下文提供，Agent 不传入。

- state 表示当前会话和 shell 阶段。shell_phase 为 editing/executing/unknown；editing 不保证空输入行，executing 不区分计算、休眠、REPL 或等待输入。integration 不可用时不通过普通输出推断命令状态。
- latest_command 为最近一个集成报告的顶层命令（尚无命令时为 null），含 command_id、裁剪到 1024 字符的 command、command_text_truncated 和 status。status 为 executing、returned 或 tracking_ended；returned 带 result，tracking_ended 带 reason 且没有伪造 result。新命令替换旧命令结果，不保存可按 ID 查询的命令结果表。
- command_id 只由真实 shell 开始事件创建。REPL、Read-Host 的输入不创建新命令；语法错误或空行可能不产生新命令，因此 latest_command 不一定属于最近一次 write。必须比较 command_id，不能将旧命令成功套到新输入上。
- returned 只表示根 shell 执行返回，不保证后台或挂起进程结束。result.success/code 允许 null；zsh 为 shell_status，PowerShell 使用捕获的成功状态，不复用旧 LASTEXITCODE。
- screen 每次返回，不做增量、省略未变版本或行级 patch。text 保留物理换行和内部空行，省略底部空行；行末缺省列为空格。wrapped_rows 使用原屏幕零基行号标记软换行；cursor 的 row/column/visible 同样保留原屏幕坐标，可能落在省略的空行。alternate 表示备用屏幕。尺寸 geometry（rows/columns）在 start/list/signal/close 会话元数据中提供。
- 按紧凑 JSON 字符数限制整份快照，沿用统一 ToolOutputBudget，预留提示与 handler 包装空间。预算先保留元数据和当前屏幕，再填入最近 history。屏幕超预算先去掉顶部旧行，再对单条超长行保留后缀；truncated、row_offset（第一条返回物理行号）、first_row_omitted_chars（第一行省略前缀的 Unicode 字符数）明确裁剪，不通过裁剪后字符数反推终端坐标。未裁剪时两个偏移为 0。
- history 为真正滚入主屏幕历史的最近文本字符串，超出内存或返回预算时淘汰最旧前缀，history_truncated 明示有损。快照不消费 history，允许重叠；同样的真实滚动行不去重。擦除、覆盖的中间文字和备用屏幕历史不承诺保留。需要完整输出必须另存文件。
- 有新信息时清零重复计数，不返回 warning。无新信息的提醒含 code=TERMINAL_READ_UNCHANGED、unchanged_reads、message，显示连续重复次数，要求先检查是否需要输入，否则建议增加 wait_ms。不据此宣告命令结束。只使用下述信息版本，不比较文本相似度或缓存上一份屏幕文本；截断范围外的信息仍参与进展判定。
- 无新信息由独立信息版本判定：实际画面文字/软换行、光标位置、备用屏幕变化、真实滚动历史、shell 事件或会话终态均算进展；纯光标可见性闪烁和原始字节回显但无上述变化不算进展。相同内容的新日志仍算进展，不能因为最终画面相同而阻断。
- 重复计数按当前 Run + session 隔离。首次 read 仅建立基线，不计重复；后续连续无新信息第 1～4 次返回画面和提醒，第 5 次起抛 TERMINAL_READ_UNCHANGED（retryable=true）。每次仍先完成等待；有新信息或换 Run 后恢复正常。取消的 read 不建立基线、不累计，write 和前端预览不参与计数。会话每次只保存当前 Run 的基线，利用 Task 单活 Run 约束，不引入跨 Run registry。
- TERMINAL_READ_UNCHANGED 与 TERMINAL_READ_IN_PROGRESS 是观察策略错误，不触发会话失败、进程清理、信号或命令失败结果。重复读取错误明确声明不停止、不失败、不影响命令或会话；reason 说明增加 wait_ms 不重置次数，也不保证恢复，只有观察到新输出或状态变化才能恢复当前 Run 的正常读取。Agent 应检查已有提示，再决定延长等待或提供输入。该错误单独投影为 UI 短提示「重复读取」，不再套用 terminal operation failed 或通用瞬时故障建议；其他终端错误保持原投影。并发读取错误要求等待现有 read。提醒和阻断保留结构化 terminal_read_unchanged / terminal_read_repeated 日志。
- 预览 WebSocket 继续使用原始 output seq，与 Agent 快照无消费位置共享。Agent content 不返回 first_available_seq / next_seq；service 原始快照和预览仍保留各自水位。
- worker 使用 Alacritty terminal 内核维护唯一屏幕、Unicode 宽度和光标；DSR 来自同一内核。每字节导出已滚出历史，避免同一块内 clear/备用屏幕切换抹掉历史。无界面投影即时应用 synchronized-update 内容，不等待闭合标记。
- 固定 80×24 由 worker 定义，经握手和预览 attached 传递；PTY、引擎、前端使用同一尺寸。没有 resize 工具。颜色/字体不进入 Agent 文本快照，原始预览保留控制序列。

## 接入边界

Python 侧按事实所有权拆分，不另建会话状态机：

- `terminal_session_service.py`：Task/工作区归属、会话集合与容量、批量生命周期、worker 资源释放和外部状态通知。
- `terminal_session_runtime.py`：单会话锁、输入去重、观察等待、worker 事件应用和唯一终态迁移。迁移赢家交出 worker 句柄，service 在会话锁外清理；Task 删除和 shutdown 等待已开始的清理尝试结束。显式 close 报告清理异常，后台清理记录日志并继续。
- `terminal_preview.py`：原始输出环形缓冲、重放到实时订阅的衔接和慢订阅者背压。字节预算按原始输出大小计费，元数据固定估算；并发出队和溢出清空时不保证瞬时精确计量，溢出后停止接受事件。它是队列容量控制，不是 WebSocket JSON 大小的硬上限。
- `terminal_observation.py`：shell 语义、最近命令和有界历史的当前快照；`terminal_screen_projection.py`：屏幕文本预算投影；`terminal_read_guard.py`：Run 级重复观察判定；`interaction/input.py`：完整行和控制键的输入边界。删除事件分页、交付版本、命令结果查询表和旧 wait 条件模型。

service 在创建会话时直接构造 `ProcessTerminalWorker`，runtime 直接使用该具体类，不设置 Worker Protocol 或 Factory。可执行文件路径由 worker 构造时解析（显式配置优先，否则读取宿主环境变量）；未配置时保留诊断日志并失败，构造本身不启动进程。单元测试在具体构造调用处替换 worker，真实集成测试使用显式路径。

会话状态直接由 runtime 持有，不再使用 `TerminalSessionRecord` 或其同义包装。身份和用途创建后不变；生命周期在会话锁内直接更新，snapshot 同锁读取并显式投影。`TerminalSessionStatusChange` 和 `SessionTermination` 仍冻结一次迁移的结果，锁外通知不持有可变 runtime 的状态引用。预览只接收生成事件所需的标量字段。

start/list/signal/close 的会话快照保留 session_id、task_id、label、initial_cwd、shell_kind、status、end_reason、exit_code，以及 generation、geometry、预览水位、观察状态和集成能力。尚未成功握手的会话 geometry 为 null；成功建立后保留到终态。workspace_id 与 created_by_run_id 仅用于内部归属检查和日志；不再返回 last_activity_at、ended_at、worker_instance_id、worker_pid 或 shell_executable，也不保留这些字段的会话副本。worker 实例身份仍用于握手；启动时的结构化日志关联 session_id、worker 实例、PID 和 shell 路径。没有空闲回收，不再逐帧维护活动时间。

ShellResolver 接收 integration 并一次生成启动规格；Windows 启动标志在显式平台分支内直接读取，不用反射回退掩盖配置或能力缺失。

read/write 直接返回观察字典，不再套内部结果包装。已接受的 write 在同一 runtime 上完成观察，即使期间 Task 删除也保留输入接受事实。状态通知按会话串行投递并丢弃过期 running，外部观察者不占用会话状态锁。

Rust worker 随二进制嵌入脚本并释放到临时目录，正常退出时清理。zsh bootstrap 保留用户启动文件链路；PowerShell bootstrap 保留 profile，使用 NoExit 装配交互脚本，不修改永久配置或执行策略。

脚本发送 OSC 633 的 Cosir 扩展：`633;Cosir;<session-token>;<kind>;...`。token 用于区分本次启动与其他输出，不构成安全隔离。vte 负责分帧，worker 将标记前原始字节、语义事件、标记后字节按顺序交付；Python service 独占 shell 状态投影。退出码语义和根会话身份是扩展的项目契约，不冒充通用 OSC 633 D 事件的原生进程退出码。

zsh 使用 preexec/precmd；语法错误在 preexec 前被拒绝时不创建命令。PowerShell 在 Prompt 入口捕获状态，在下一次顶层 PSConsoleHostReadLine 入口确认返回，避免手动调用 prompt 提前结束命令。嵌套提示符不结束根命令。输入读取返回后报告执行尝试，不声称已经成功执行。

脚本加载失败、配置禁用 hooks、受限语言模式等情形不通过普通提示符文字猜测恢复。无事件的长时间静默本身也不能证明集成失效。不跟踪子 shell 内部命令。备用屏幕可以观察，但完整行/有限控制键不承诺能操作所有全屏程序。

## 验证入口

- `test_terminal_interaction_contract.py`：输入严格边界、read 三秒下限、旧参数拒绝、跟踪终止、最新结果替换、软换行/空行/Unicode 和预算保留尾部。
- `test_terminal_read_guard.py`：首次基线不计数、第 1～4 次提醒、第 5 次起可恢复错误、Run 隔离、新信息不提醒并清零、真实重复日志和光标闪烁。
- Rust `terminal_screen` 测试：覆盖重绘、宽字符/组合字符、跨块、DSR、重复历史、滚动区域、备用屏幕；PowerShell 样式控制序列用合成数据验证，不能替代 Windows 实测。
- `test_terminal_session_service.py`：幂等、取消、背压、预览、Task 生命周期与日志；实际三秒等待、并发 read 拒绝且不阻塞 write、取消/关闭唤醒、重复错误不失败命令和输出有损尾部保留。
- `test_terminal_preview.py`：重放/实时交界、慢订阅者隔离、帧数与字节预算溢出。
- `test_terminal_session_lifecycle.py`：收尾只执行一次、清理失败、关闭唤醒、接受输入后的 Task 删除、状态通知排序和批量清理等待。
- `test_terminal_shell_integration.py`：真实 zsh / Windows PowerShell / pwsh，覆盖 REPL、多行、程序提问、静默命令、中断、变量保持、语法错误、后台任务和嵌套 shell。未安装 shell 或宿主平台不符时显式 skip。
- `test_terminal_worker_integration.py`：worker 协议、进程树和跨 Run 会话复用。
- Rust parser/protocol 测试：跨字节分块、事件身份、结果语义和输入编码。

Windows ConPTY 必须在 Windows 主机执行；macOS 上通过脚本检查或 Rust 编译不代表 Windows 行为已通过。

## 屏幕引擎适配决策

采用 `alacritty_terminal` 0.25.1 的纯内核（关闭默认 serde feature），不使用其窗口或 PTY 管理；现有 portable-pty 继续拥有进程。验证阶段排除了 vt100 的滚动历史导出限制，以及 avt 的组合字符存储限制，避免为库接口缺口重新手写去重或光标算法。删除旧 Python 文本过滤器和 Rust TerminalQueryResponder，不保留双实现。

适配先在临时工程验证真实 zsh 长行原始输出回放，再接入 worker 进行实际 PTY 验证。Windows PowerShell/PSReadLine/ConPTY 必须由 Windows 集成测试覆盖；macOS 上的合成控制序列及脚本检查只证明协议适配，不证明该平台已经实测。

# 交互终端契约

终端归 Task 所有。输入操作、shell 命令观察、会话生命周期是三个独立概念；`command_returned` 只表示顶层 shell 执行返回，不保证后台或挂起进程终止。输入、等待和事件历史均不进入 checkpoint 或业务 Run 状态机。

## 工具入口

- `terminal_start(shell, cwd, label, integration="auto")`：启动持久交互 shell。`off` 禁用集成；`auto` 按实际 shell 接入。worker 握手与集成就绪不同；初始化超时返回 `unavailable`，迟到的真实 ready 事件可以恢复能力。
- `terminal_write(session_id, operation_id, input, wait_ms=500)`：`input` 是单个 `{"type":"line","text":"..."}` 或 `{"type":"key","key":"ctrl_c"}` 对象，不接收数组、裸 text 或混排批次。完整行由 worker 保留原文 UTF-8 并追加一次 Enter；不接收 cursor，从接受输入前的水位开始观察。
- `terminal_read(session_id, cursor=null, wait={until:"activity", timeout_ms:3000})`：只读观察。`wait.until` 为 activity、shell_prompt 或 command_returned；后者必须提供 command_id。最长等待 30 秒，工具总超时 35 秒。
- `terminal_signal`：能力来自 worker 握手。Windows 支持 interrupt；Unix 支持 interrupt/suspend 和有条件的 canonical EOF。拒绝时保留受控原因码。投递成功不等于程序退出。
- `terminal_list/close`：发现跨 Run 会话、显式清理；Task 删除与后端退出继续负责兜底。

`operation_id` 只在最近接受的输入窗口内去重，指纹覆盖输入项，不包含等待条件。接受仅保证进入本机队列。取消只结束等待；重试已接受操作不重复输入，即使 shell 已退出。重试从本次调用的水位观察，不重放原调用的输出。read 的 cursor 为空时读取保留窗口。

write 与 read 都返回 next_cursor，可传给下一次 read 续读。write 不负责补读此前输出；它观察到的活动可能包含后台日志，不保证由本次输入产生。长驻服务日志、静默命令等待、程序交互提示、输出分页及跨 Run 接续均通过 read 观察，无须发送额外输入。

## 完整行与控制键

- line 的 text 必填但允许空字符串（只按 Enter），保留前后空格与缩进。拒绝真实 CR/LF、Tab、Escape 和其他 C0/C1 控制字符，不解码字面量反斜杠转义。包含最终 Enter 的输入最多 64 KiB。
- 多行结构逐次提交完整行；不接受 lines 数组，不自动拆分文本、不补括号或终止空行。zsh 的 `for ...; do` / `done`、PowerShell 的 `{` / `}` 由 shell 继续解析；传统 Python REPL 的复合语句由调用方显式提交空行结束。
- 同一个 line 可以是 shell 命令、REPL 表达式、密码或交互回答。工具不猜接收程序；输入接受不代表语法完整或开始执行。REPL 内部求值不产生根 shell 的 command_returned。
- 输入行是向当前终端发送文字和 Enter，不替换行编辑器已有内容；自动缩进、自动补全、用户键位绑定等由前台程序决定。高级 REPL 与全屏菜单不在通用兼容承诺内。
- Unix 控制键只声明 ctrl_c / ctrl_d；Windows 只声明 ctrl_c / ctrl_z。工具 schema 按宿主收窄，session 与 read/write 返回的 capabilities 中 `input_ctrl_*` 来自真实 worker 握手。service 在接受前拒绝不支持的键，worker 也在独立进程边界校验。
- key 只发送对应控制字符，不追加 Enter、不自动映射 EOF、不保证程序退出。需要后续 Enter 时显式发送空 line。terminal_signal 仍表示运行时信号操作，独立于按键投递。
- 没有 enter/tab/backspace/escape 或光标移动键；空行替代单独 Enter。协议为 terminal-worker-v4，不兼容旧输入格式。

例如传统 Python REPL：依次提交 `for n in range(2):`、`    print(n)`、空字符串；程序提问则提交答案或空字符串接受默认值。两者使用同一 line 契约，是否完成始终由实际程序行为与 shell 事件决定。

## 观察与分页

返回包含 session_id、generation、events、state、integration、capabilities、next_cursor、has_more、gap、return_reason；write 另含 input.accepted/duplicate。命令返回条件已经满足时，condition 保留命中的命令结果，即使该事件已经被游标消费。

- events：output、integration_ready/unavailable、command_started、command_returned、shell_prompt、tracking_ended。
- command_id 只由 shell 报告的顶层输入边界创建。Python REPL、Read-Host 等程序内部的输入不创建 shell 命令。
- state.shell_phase：editing/executing/unknown。editing 不承诺空输入行；executing 不区分忙碌、休眠或等待交互。
- state.observed_cursor 是最新快照的水位；next_cursor 是本页实际交付水位。快照可以领先于分页输出。
- result.success 与 result.code 均允许 null。zsh 数值标记为 shell_status；PowerShell 使用捕获的成功状态，不复用可能属于旧程序的 LASTEXITCODE。
- shell 退出或观察边界异常时，未返回的命令以 tracking_ended 收敛，不伪造退出码。
- 相邻 output 在模型返回时合并。文本可按字符分页，语义事件完整交付。JSON 总大小沿用统一 ToolOutputBudget；不靠事后截断 JSON。
- has_more 表示仍有可读内容；gap 表示缓冲已淘汰内容。游标绑定 generation，包含文本内偏移，调用方只原样传递。
- activity 短暂聚合输出；等待到期不结束命令。输出积压达到页预算时提前返回 page_available，调用方继续分页。
- 预览 WebSocket 继续使用原始 output seq；Agent 观察游标绝不能传给预览。原始字节与清理文本是同一有序输入流的两个投影，不共享消费位置。

## 接入边界

Python 侧按事实所有权拆分，不另建会话状态机：

- `terminal_session_service.py`：Task/工作区归属、会话集合与容量、批量生命周期、worker 资源释放和外部状态通知。
- `session_runtime.py`：单会话锁、输入去重、观察等待、worker 事件应用和唯一终态迁移。迁移赢家交出 worker 句柄，service 在会话锁外清理；Task 删除和 shutdown 等待已开始的清理尝试结束。显式 close 报告清理异常，后台清理记录日志并继续。
- `preview.py`：原始输出环形缓冲、重放到实时订阅的衔接和慢订阅者背压。字节预算按原始输出大小计费，元数据固定估算；并发出队和溢出清空时不保证瞬时精确计量，溢出后停止接受事件。它是队列容量控制，不是 WebSocket JSON 大小的硬上限。
- `observation.py`：shell 语义状态、Agent 事件窗口与分页游标；`interaction.py`：工具输入和等待条件的边界契约。

service 在创建会话时直接构造 `ProcessTerminalWorker`，runtime 直接使用该具体类，不设置 Worker Protocol 或 Factory。可执行文件路径由 worker 构造时解析（显式配置优先，否则读取宿主环境变量）；未配置时保留诊断日志并失败，构造本身不启动进程。单元测试在具体构造调用处替换 worker，真实集成测试使用显式路径。

read/write 直接返回观察字典，不再套内部结果包装。已接受的 write 在同一 runtime 上完成观察，即使期间 Task 删除也保留输入接受事实。状态通知按会话串行投递并丢弃过期 running，外部观察者不占用会话状态锁。

Rust worker 随二进制嵌入脚本并释放到临时目录，正常退出时清理。zsh bootstrap 保留用户启动文件链路；PowerShell bootstrap 保留 profile，使用 NoExit 装配交互脚本，不修改永久配置或执行策略。

脚本发送 OSC 633 的 Cosir 扩展：`633;Cosir;<session-token>;<kind>;...`。token 用于区分本次启动与其他输出，不构成安全隔离。vte 负责分帧，worker 将标记前原始字节、语义事件、标记后字节按顺序交付；Python service 独占 shell 状态投影。退出码语义和根会话身份是扩展的项目契约，不冒充通用 OSC 633 D 事件的原生进程退出码。

zsh 使用 preexec/precmd；语法错误在 preexec 前被拒绝时不创建命令。PowerShell 在 Prompt 入口捕获状态，在下一次顶层 PSConsoleHostReadLine 入口确认返回，避免手动调用 prompt 提前结束命令。嵌套提示符不结束根命令。输入读取返回后报告执行尝试，不声称已经成功执行。

脚本加载失败、配置禁用 hooks、受限语言模式等情形不通过普通提示符文字猜测恢复。无事件的长时间静默本身也不能证明集成失效。首版不跟踪子 shell 内部命令，不提供全屏程序屏幕快照。

## 验证入口

- `test_terminal_interaction_contract.py`：输入严格边界、分页、游标归属、跟踪终止。
- `test_terminal_session_service.py`：幂等、取消、背压、预览、Task 生命周期与日志。
- `test_terminal_preview.py`：重放/实时交界、慢订阅者隔离、帧数与字节预算溢出。
- `test_terminal_session_lifecycle.py`：收尾只执行一次、清理失败、关闭唤醒、接受输入后的 Task 删除、状态通知排序和批量清理等待。
- `test_terminal_shell_integration.py`：真实 zsh / Windows PowerShell / pwsh，覆盖 REPL、多行、程序提问、静默命令、中断、变量保持、语法错误、后台任务和嵌套 shell。未安装 shell 或宿主平台不符时显式 skip。
- `test_terminal_worker_integration.py`：worker 协议、进程树和跨 Run 会话复用。
- Rust parser/protocol 测试：跨字节分块、事件身份、结果语义和输入编码。

Windows ConPTY 必须在 Windows 主机执行；macOS 上通过脚本检查或 Rust 编译不代表 Windows 行为已通过。

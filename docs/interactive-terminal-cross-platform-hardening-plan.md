# Interactive Terminal 跨平台稳定性修复方案

状态：已实现；第三轮子 Agent 按本文档验收通过。Linux/macOS 原生运行时 smoke 仍需在对应主机执行

本文针对 `apps/terminal-worker` 与
`apps/backend/app/service/terminal` 的跨平台终端稳定性问题，目标是让
Windows、macOS 和 Linux 共用稳定的 session/worker 协议，同时把平台差异收敛在
Rust worker 的平台适配层。

本文不改变产品拓扑：Tauri 是桌面宿主，Python backend 是 Terminal Worker 的
生命周期 owner，Rust worker 是 backend 的本机子进程，PTY/ConPTY 和 shell 子进程
只由 Rust worker 管理。React、SQLite、Assistant Transport 和 terminal session
业务状态机不直接接触平台 PTY API。

## 1. 总体设计原则

当前代码把三类不同语义混在了一起：

1. 向终端输入一个字节，例如普通用户输入或 EOF 控制字符；
2. 向当前前台作业发送 OS 信号，例如 interrupt、suspend；
3. 关闭并清理整个 shell 进程树。

修复后的边界如下：

```text
worker.rs
  ├─ 控制帧与生命周期状态机
  ├─ 输出读取和事件发布
  └─ 调用 PtyRuntime 的平台无关操作

pty.rs
  ├─ PTY I/O
  ├─ TerminalSignal 的统一语义
  └─ PlatformTerminalControl

platform/
  ├─ unix.rs       前台进程组、termios、Unix signal
  └─ windows.rs    ConPTY 输入/控制事件、Job Object

process_tree.rs
  └─ shell containment、后代发现、优雅终止、强制终止

terminal_emulator.rs
  └─ 有状态 VT 解析和受控 DSR 响应
```

worker 内增加一个唯一的 `TerminalLifecycleArbiter`。所有 child watcher、PTY
reader、control reader、heartbeat timeout 和 shutdown 事件都先进入该仲裁器；只有
仲裁器可以发布最终 Exit 和清理结果。平台适配器不能直接改变 worker/session 的
最终状态。

Rust 层新增内部结果类型，不再让调用方只能看到 `anyhow::Result<()>`：

```text
TerminalControlResult
  ├─ Applied
  ├─ Unsupported { operation, platform }
  └─ Failed { operation, retryable }
```

`Unsupported` 必须变成显式 worker error，不能静默把不支持的 Windows EOF 或
Unix raw-mode suspend 当成成功。`Failed` 只携带稳定错误码；原始 OS 错误写入
worker/backend 的结构化诊断日志，并限制长度、过滤控制字符。

`Applied` 的语义是“控制请求已投递到平台边界”，不表示前台程序一定退出；程序
可以捕获、忽略或延迟处理 signal。backend 不得把这种结果误投影为 worker failure。

## 2. Windows interrupt 与 EOF

### 2.0 Windows shell 支持范围

Windows 的目标支持范围是以下 shell 的基础 ConPTY 交互；实际能力以 shell 已安装、
版本探测成功且当前 Windows/ConPTY 可用为前提：

| Shell | 启动形式 | 基础能力 |
| --- | --- | --- |
| `cmd.exe` | `cmd.exe /Q /D` | start、read、write、close |
| Windows PowerShell 5.1 | `powershell.exe -NoLogo` | start、read、write、close |
| PowerShell 7+ | `pwsh.exe -NoLogo` | start、read、write、close |

这里的“基础能力”不依赖 interrupt/EOF 是否已经在该 Windows build 和 shell 组合
上验证通过。ConPTY 能正常创建、shell executable 已安装且版本探测成功时，三类
shell 都必须能够启动、读写 PTY 输出并关闭。当前实现已提供显式 `cmd` 分支，
`pwsh` 和三类 shell 的交互式 resolver 验收仍以
真实 Windows 环境中的“已安装/未安装”矩阵为准。

Shell resolver 将 `cmd` 作为显式 Windows 选项，而不是要求调用方把
`cmd.exe` 当作 custom executable 传入：

* `auto`：保持当前默认 PowerShell 行为；
* `cmd`：解析 `cmd.exe`，固定参数 `/Q /D`；
* `powershell`：解析 Windows PowerShell 5.1，固定参数 `-NoLogo`；
* `pwsh`：解析 PowerShell 7+，固定参数 `-NoLogo`；
* 自定义 shell 仍走现有 custom 路径，但不自动获得 Windows signal 能力声明。

修复后的协议目标是控制能力单独协商，不把基础 shell 支持与控制信号支持绑成一个
总开关。worker handshake 通过 `capabilities` 声明静态能力；依赖当前 console/input
mode 的结果仍在 signal 请求时动态检查：

| 能力 | `cmd.exe` | PowerShell 5.1 | `pwsh` |
| --- | --- | --- | --- |
| ConPTY start/read/write/close | 必须支持 | 必须支持 | 必须支持 |
| interrupt | 以 capability probe 结果为准 | 以 capability probe 结果为准 | 以 capability probe 结果为准 |
| EOF | 以 input-mode/shell probe 结果为准 | 以 input-mode/shell probe 结果为准 | 以 input-mode/shell probe 结果为准 |
| suspend | 默认 `Unsupported`，验证后再开放 | 默认 `Unsupported`，验证后再开放 | 默认 `Unsupported`，验证后再开放 |

静态 capability 可以在 handshake 中声明，例如 PTY 类型、shell kind、支持的操作
集合；依赖当前 console/input mode 的 EOF 结果必须在 signal 请求时再次检查，不能
只依赖启动时的静态字段。`ShellResolver` 单测必须覆盖：Windows `cmd`、已安装和
未安装的 PowerShell 5.1、`pwsh`、版本不可识别、以及 custom executable。

backend 在发送 signal 前消费静态 capability；worker 仍负责动态 termios/平台检查，
因此 unsupported 不会再被静默当作普通输入字节。

因此，即使某个组合暂时返回 `PTY_SIGNAL_UNSUPPORTED`，`cmd` 或 PowerShell 的
普通交互 session 仍然是受支持的；只有该控制操作失败，不能把整个 shell 标记为
无法启动。

### 2.1 不再把平台控制语义实现为裸字节

`interrupt`、`eof`、`suspend` 仍然保留在 backend 协议中，但 worker 内部必须由
`WindowsTerminalControl` 负责映射。Windows 分支禁止继续复用：

```rust
self.write(&[0x03])
self.write(&[0x04])
```

这两个字节可以作为普通输入数据进入 ConPTY，不能作为 Windows 控制事件的可靠
实现。

### 2.2 先固定 Windows 控制机制，再实现适配器

Windows 适配器分为两个能力：

* `interrupt`：发送真正的 console control event，或者使用 ConPTY 支持的
  Win32 input/control-event 编码；
* `eof`：发送经过验证的 Windows console EOF/input 事件，不能假设 POSIX 的
  `VEOF` 在 Windows 上存在。

实现顺序必须是：

1. 建立一个最小 Windows probe，分别验证 `cmd.exe`、Windows PowerShell 5.1 和
   `pwsh` 中的 interrupt/EOF 行为；
2. 建立 capability matrix，不预设存在覆盖上述 shell 的单一机制；
3. 将该机制封装到 `platform/windows.rs`，业务层只调用统一接口；
4. 将 capability 作为可选 handshake 字段暴露给 backend，避免只能在调用失败后
   才知道能力不可用；
5. 如果某个 shell 或 Windows 版本无法安全支持某个操作，返回
   `Unsupported`，绝不伪装成写入成功。

Windows EOF 必须用能力表描述，而不是抽象成无条件的“Windows EOF 事件”。至少
记录以下维度：

| 维度 | 内容 |
| --- | --- |
| shell | `cmd.exe`、PowerShell 5.1、`pwsh`、版本 |
| Windows/ConPTY | Windows build、ConPTY 模式、是否存在 console/helper |
| input mode | processed、canonical、raw 或其他可观察模式 |
| operation | interrupt、EOF、suspend |
| result | 可验证行为、`Unsupported` 原因、测试证据 |

`GenerateConsoleCtrlEvent` 不能直接作为无条件修复。它要求目标进程与调用方
共享 console；`CTRL_C_EVENT` 也不能按指定 process group 精确限定。若采用
console control event，必须同时解决：

* shell 创建时的 process-group 建立；
* worker 或专用控制 helper 如何进入目标 console；
* `CTRL_BREAK_EVENT` 与 `CTRL_C_EVENT` 的差异；
* helper/worker 不得继承或污染 backend 的 stdio 管道。

若 ConPTY 的正式输入路径更适合发送 Win32 input event，则应在
`portable-pty` 的可维护扩展点上实现，而不是在本项目重复复制 ConPTY 的
`CreateProcessW`/attribute-list 代码。优先顺序是：

1. 使用 upstream 已支持的 portable-pty API；
2. 若缺少必要能力，提交或维护一个最小、版本锁定、带测试的 portable-pty
   扩展；
3. 只有 upstream API 无法承载时，才在 `platform/windows.rs` 集中维护一套
   Windows FFI，并把 unsafe 代码限制在该文件。

### 2.3 Windows 进程控制元数据

启动 shell 时在平台控制对象中保存：

* worker PID；
* shell PID；
* shell process handle 的 RAII owner；
* process group/control helper 的标识；
* Job Object 的 RAII owner。

Handshake 可以增加可选 `capabilities` 字段；旧 backend 忽略未知字段，新 backend
根据 capability 选择或拒绝操作。capability 只描述 worker 已验证的能力，不代表
shell 当前一定会响应 signal。

Handshake 中现有 `pid` 继续表示 worker PID；如平台控制层需要 shell PID，新增
内部字段或单独的非业务事件，不复用现有字段。

### 2.4 Windows 验收标准

必须在真实 Windows runner 上验证：

* `cmd.exe`、Windows PowerShell 5.1 和 `pwsh` 均能启动并完成基础
  start/read/write/close smoke；
* `cmd.exe` 中运行长时间命令，`interrupt` 后命令停止且 shell 仍可继续输入；
* PowerShell 5.1 与 `pwsh` 中运行前台子进程；只有 capability probe 证明控制路径
  能按前台作业作用时才要求 interrupt 验证通过，否则必须返回 `Unsupported`；
* `eof` 在等待输入的命令中产生预期结束输入语义；
* 不支持的 signal/eof 组合返回稳定的 `PTY_SIGNAL_UNSUPPORTED`，不会把 signal/eof
  操作退化为普通 write；普通 `terminal_write` 仍可合法发送任意输入字节，包括
  `0x03` 或 `0x04`；
* backend 控制管道、shell stdin 和 ConPTY 输出不会被 control helper 混用。

## 3. Unix/macOS 子进程树清理

### 3.1 从“杀一个进程组”升级为 containment tracker

`ProcessTreeGuard` 不再只保存一个启动时的 `process_group`，而是保存一个
`ProcessContainment`：

* shell PID 和启动时间，用于防止 PID reuse；
* shell session/process-group 信息；
* 已发现的 descendant PID、父 PID、进程组 ID 和启动时间；
* 最近一次扫描时间与关闭阶段；
* 平台实现句柄/FD（如果需要）。

进程发现和终止逻辑必须与 worker 生命周期解耦，避免把“关闭 shell”误认为
“关闭整个终端 session”。

### 3.2 关闭流程

统一使用 deadline 驱动的两阶段关闭：

1. 读取当前 PTY 前台进程组作为观测信息，并确认 shell 身份仍匹配启动时的
   PID/start-time；
2. 扫描 shell 的后代进程，重复扫描直到达到明确的稳定条件：两次扫描间隔达到
   `scan_interval` 且 PID/start-time 集合不再增加，或达到 `scan_deadline`；
3. 只有在 PGID 成员和 leader 身份均重新验证通过时才允许 group kill；当前实现
   采用更保守的经过 PID/start-time 校验的逐进程路径，避免 PGID reuse 误杀；
4. 按“叶子进程优先、shell 最后”的顺序补发 `SIGTERM`；
5. 等待短暂 grace period；
6. 对仍存活的已发现 PID/进程组发送 `SIGKILL`；
7. 最后 wait/reap shell，并发布唯一的关闭结果。

`ESRCH`、已经退出的 PID 和重复发送信号必须视为正常竞态，不得在第一处错误
返回，导致后续 `SIGKILL` 和 wait 被跳过。所有其他错误要记录结构化诊断，但仍
继续清理剩余目标。

任何 `kill(-pgid, signal)` 前都要重新验证：

* PGID 属于目标 session；
* 组内至少有一个 PID 的 start time 与 tracker 记录一致；
* PGID 没有被复用为不相关进程组。

无法完成验证时，不得使用 group kill，改为对经过身份验证的 PID 逐个发送信号。
每个目标最终记录为 `signalled`、`already_exited`、`identity_unknown` 或
`signal_failed`，避免把“无法再观察到”误报为“已清理”。

### 3.3 平台发现实现

* Linux：读取 `/proc/<pid>/task/<tid>/children`，必要时结合 `/proc/<pid>/stat`
  的父 PID、进程组 ID 和启动时间；
* macOS：使用 `proc_pidinfo`/`PROC_PIDLISTCHILDREN` 获取后代，并用进程启动时间
  防止 PID reuse；
* 两个平台都记录 process-group 信息；在无法完成 PGID reuse 验证时使用逐 PID
  路径，不能把 process group 当作完整树清理保证。

`tcgetpgrp()` 得到的前台 PGID 也必须通过 session/进程身份检查；只检查 shell PID
的 start time 不足以证明 PGID 本身没有复用。

扫描存在竞态：进程可能在扫描后 fork、exec、setsid 或退出。因此实现必须：

* 在 grace period 内重复扫描；
* 对每次目标校验 PID start time；
* 叶子优先发送信号；
* 明确记录“已发现并清理”和“无法再观察到”的范围。

对于主动 daemonize、脱离 session 并成为 init 子进程的进程，普通 POSIX 进程
树扫描无法提供绝对 containment。产品契约应明确：关闭保证覆盖 shell 及其在
关闭期间可观察到的后代，不承诺杀死主动 daemonize 的系统级服务。若未来需要
绝对 containment，应另行评估 Linux cgroup/macOS sandbox，而不是继续堆叠扫描补丁。

### 3.4 Unix signal 语义

Unix `interrupt` 和 `suspend` 不再写控制字节作为主要实现：

* 每次操作实时读取 PTY 当前 foreground process group；
* `interrupt` 发送 `SIGINT` 到该进程组；
* `suspend` 发送 `SIGTSTP` 到该进程组；
* `eof` 才使用 termios 中实际读取的 `VEOF` 字符，并要求 `ICANON` 已启用且
  `VEOF` 不是 `_POSIX_VDISABLE`；
* raw mode 下无法提供 EOF 的 line-discipline 语义时返回 `Unsupported` 或采用
  明确记录的 input-byte fallback，不能宣称已经发送 EOF。

这样可以覆盖 shell 切换前台作业以及程序进入 raw mode 的情况，并避免缓存启动
时 process group 导致信号打到错误目标。

## 4. DSR 响应与 VT 状态

### 4.1 把 substring probe 替换为状态化组件

`respond_to_terminal_queries` 已替换为独立的 `TerminalQueryResponder`，
由 PTY reader 单线程拥有：

* 输入可以跨 output chunk；
* 解析 CSI、OSC、DCS、字符串终止符和取消序列；
* 维护 DSR 所需的最小 cursor 状态；
* 对 DSR 查询生成基于当前状态的响应，而不是固定写死 `ESC[1;1R`；
* 未识别序列只原样继续输出，不阻塞 reader。

parser 必须有资源上限：CSI 参数数量、OSC/DCS 字符串长度、未闭合字符串的保留
时间和总 pending bytes 都要有固定上限；超限时取消当前控制序列并继续转发输出，
不能让异常 shell 输出无限占用 worker 内存。

当前实现额外限制未闭合控制序列的最长保留时间；超时会重置本地 parser，随后
继续把后续 PTY 输出作为新的字节流处理。

优先复用成熟的 Rust VT/terminal-emulator crate；不要在 worker 中继续扩展一个
只匹配 4 个字节的自研解析器。该 emulator 是输出旁路，不是新的事实源，也不把
终端屏幕快照写入 SQLite 或 Assistant snapshot。

当前 worker 使用成熟 `vte` parser 维护跨 chunk 的 CSI 状态和有限 cursor 状态，
只在 cmd/Windows PowerShell/pwsh 的 terminal-emulation policy 下自动回复 DSR；
未知/custom shell 默认不启用 responder。

### 4.2 响应策略

DSR 响应是终端仿真语义，不应被当成普通工具输入。目标是降低误判、提供安全策略，
而不是声称完整 parser 能消除单向 PTY 的根本歧义。策略分三层：

1. 默认只响应完整、合法、当前能力声明允许的 DSR；
2. shell 启动阶段可以启用兼容性响应，覆盖 PowerShell/PSReadLine 的启动查询；
3. 对未知 shell、原始二进制模式或策略关闭时，不自动向 PTY 写响应，返回诊断
   但不结束 session。

需要注意：单向 PTY 输出无法在协议层绝对区分“程序发出的 DSR”和“`cat` 的二进制
内容恰好包含 DSR”。因此真正的安全边界不是再增加一个 substring 上下文判断，
而是：

* 使用完整 VT parser；
* 将自动响应做成明确能力和策略；
* 对不需要 PSReadLine 兼容性的 shell 默认关闭自动响应；
* 若产品必须同时支持任意二进制输出和完整终端仿真，改用可配置的终端仿真层，
  并接受“终端控制序列会产生终端行为”这一协议语义。

验收必须同时覆盖两种产品选择：

* 字节透明模式：自动响应关闭，`cat` 或任意程序输出绝不触发 worker 写入；
* 终端仿真模式：合法 DSR 会产生响应，包含 DSR 的二进制输出也按终端控制序列
  处理，并在文档中明确这是预期语义。

### 4.3 DSR 验收标准

增加单元测试覆盖：

* 查询跨 chunk 分割；
* 查询前后带普通输出；
* CSI/OSC/DCS 交错；
* cursor 移动后返回正确位置；
* 不完整序列不会写入 PTY；
* DSR 写失败不会直接杀死 session；
* 策略关闭时不会自动注入任何响应。

## 5. Job Object RAII 与双重 CloseHandle

Windows Job Object 采用单一所有权，不允许 `terminate(self)` 中显式关闭后再由
`Drop` 关闭。

推荐改为：

* `WindowsJob` 内部持有 `std::os::windows::io::OwnedHandle`，或等价的
  `Option<HANDLE>` RAII wrapper；
* `terminate(&mut self)` 只调用 `TerminateJobObject`，不调用 `CloseHandle`；
* handle 的关闭只发生在 RAII owner 的 Drop/take 路径；
* `ProcessTreeGuard` 通过 `Option<WindowsJob>` 管理终止后生命周期；
* `AssignProcessToJobObject`、`SetInformationJobObject` 失败时统一走 owner 的
  清理路径，避免每个错误分支手动 close。

为消除 shell 创建后到 Job assignment 之间的窗口，Windows 平台适配器应优先让
shell 以 suspended 状态创建：`CreateProcess` → 设置 Job →
`AssignProcessToJobObject` → `ResumeThread`。如果 portable-pty 当前 API 无法提供
这个顺序，应先扩展 upstream/受控 fork 的创建参数，而不是接受一个未受保护的
assignment race。

Job setup 失败时必须 kill/wait 已创建的 shell，再释放 job handle。还要显式处理：

* worker/backend 已经处于 Job 的情况；
* nested job 不被当前 Windows 版本允许的情况；
* `CREATE_BREAKAWAY_FROM_JOB` 导致的逃逸；
* `TerminateJobObject` 失败后的 fallback、日志和最终 wait。

产品契约只能保证仍属于该 Job、且没有显式 breakaway 的后代被清理；不能把 Job
Object 宣称为所有 Windows 后代的绝对 containment。

同时加入 Windows handle 生命周期测试/代码审查规则：每个 raw handle 必须在同一
函数中明确标注“borrowed”或“owned”，从 raw handle 转成 OwnedHandle 后禁止再手工
`CloseHandle`。

## 6. F5 错误策略与 stderr 诊断

### 6.1 分离输出错误类型

worker 已将 PTY read、DSR response write 和 backend stdout pipe 分成不同事件：

* `PtyOutputEof`：正常或子进程退出后的 EOF；
* `PtyOutputReadFailed`：PTY reader 真实错误；
* `PtyQueryResponseFailed`：DSR 响应写失败；
* `BackendOutputFailed`：worker 到 backend 的事件管道失败。

处理规则：

* `PtyQueryResponseFailed`：禁用本次 query responder，记录一次限频 warning，
  让 child watcher 决定 shell 是否已经退出；不因一次响应失败立即终止整个 session；
* `PtyOutputReadFailed`：终止 runtime，发送稳定错误码和 Exit；
* `BackendOutputFailed`：停止 worker，清理 shell containment；
* child 已经退出时的 broken pipe/EOF 按退出竞态处理，不覆盖更准确的 exit code。

生命周期仲裁器至少维护：

```text
Starting -> Running -> Closing -> Exited
                         \------> Failed
```

仲裁器定义事件优先级：

1. 已观察到 child exit 时，保留真实 exit code；
2. child exit + PTY EOF/EIO 属于同一退出竞态时，收敛为 Exited；
3. backend pipe 断开时停止继续发布事件，但仍执行 containment cleanup；
4. heartbeat timeout、真实 PTY read failure 和 cleanup failure 分别记录，不互相
   覆盖已有的更准确终态；
5. 只有仲裁器可以发布一次 Exit；进入 Closing/Exited/Failed 后控制帧统一拒绝或
   丢弃，并记录稳定原因。

backend 必须同步扩展 `_worker_error_reason()` 的映射：

| worker error | session 影响 |
| --- | --- |
| `PTY_SIGNAL_UNSUPPORTED` | 当前操作失败，session 保持 running |
| `PTY_QUERY_RESPONSE_FAILED` | 诊断/关闭 responder，session 通常保持 running |
| `PTY_OUTPUT_READ_FAILED` | fatal，进入 failed |
| `BACKEND_OUTPUT_FAILED` | worker 清理后结束，不能再发送事件 |
| `HEARTBEAT_TIMEOUT` | fatal，进入 failed/interrupted 的既定映射 |

Unix PTY 的 `EIO` 是否等价于 EOF 必须由平台 reader 明确判定，不能在通用层把所有
`read` error 都静默当作 EOF。

### 6.2 stderr 诊断链路

stderr 不能继续被 backend 静默丢弃：

* `_drain_stderr` 逐行读取并写入 backend 的结构化日志；
* 每行限制最大长度，移除控制字符，不记录 shell 输入、凭据或大段 PTY 输出；
* 日志字段包含 `worker_instance_id`、worker PID、平台、错误类别；
* 日志失败不得阻断 worker 或 session 主流程；
* worker 的最终 fatal error 仍通过协议 error/exit 事件传递，stderr 只作为诊断旁路。

Rust `eprintln!` 可以保留作为无 logger 时的最后兜底，但生产路径必须保证
backend 日志中能看到可关联的结构化记录。

## 7. 实施顺序

按以下顺序实现，避免同时修改所有错误路径；当前第 1、2、3、4、6、7、8、9、10
项已进入代码。Windows control mechanism 因 `portable-pty 0.9` 没有暴露安全的
ConPTY 控制事件 API，本阶段不宣称 interrupt/EOF 已支持，而是通过 capability
缺失和稳定 `Unsupported` 错误显式拒绝；Job assignment race 同样记录为该依赖
边界，后续若要消除必须升级/维护带 suspended-create 扩展的 PTY 实现：

1. 新增 capability/error contract、`TerminalControlResult` 和稳定错误码；
2. 新增 `TerminalLifecycleArbiter`，先固定终态、Exit 唯一性和错误优先级；
3. 先把 Windows `cmd`、PowerShell 5.1、`pwsh` 纳入显式 shell resolver，并完成
   三者基础 start/read/write/close smoke；
4. 完成 Unix foreground signal 与 containment tracker，补 macOS/Linux 测试；
5. 在可维护的 portable-pty 扩展可用后完成 Windows control mechanism probe，
   再实现 `platform/windows.rs`；
6. 在同一扩展提供 suspended-create 后消除 Job assignment race；当前先保证
   RAII、失败清理和显式能力边界；
7. 引入成熟 VT parser，替换 DSR substring probe；
8. 拆分 worker internal event 和 stderr 结构化诊断；
9. 更新 backend error mapping、session 状态投影和文档；
10. 最后运行跨平台 release build、真实 shell smoke 和 crash/EOF 测试。

每一步都应保持现有 `terminal_start/read/write/signal/close` 业务 API 不变；只有
当平台确实不支持某个信号时，才通过稳定错误码暴露能力差异。

## 8. 验收矩阵

### Windows

* `cargo check`、`cargo build --release`；
* cmd、PowerShell 5.1、pwsh 的 start/read/write/close；interrupt/eof/suspend 在
  当前 worker 中必须返回稳定 `PTY_SIGNAL_UNSUPPORTED`，不得静默写入普通字节；
* Windows capability handshake 与未支持操作的稳定错误码；
* 前台子进程 interrupt；
* Job Object RAII、assignment 失败清理、already-in-job/nested-job/breakaway 的
  能力边界、shell 提前退出、TerminateJobObject 失败和重复终止；assignment race
  在当前 `portable-pty 0.9` API 下记录为已知边界，不宣称已经消除；
* Job Object 清理仍属于 Job 且未显式 breakaway 的 shell 后代；
* backend EOF、heartbeat timeout、worker stdout/stderr 断开；
* DSR 开启/关闭、`cat` 二进制伪造 DSR 和 query writer 失败。

### macOS

* zsh、bash、用户配置的 `$SHELL`；
* 前台 `sleep`/Python 子进程；
* interrupt、suspend、canonical/raw mode EOF；
* shell job-control 切换后 close 不遗留可观察后代；
* PGID reuse、session 校验、停止/忽略 SIGTERM 的进程和 fork-after-final-scan；
* backend 崩溃后的 worker 自清理。

### Linux

除 macOS 项目外，增加 `/proc` 扫描、PID reuse 防护、不同 shell 和 daemonize
边界测试；增加未闭合 OSC/DCS、超长 VT 字符串、PTY `EIO` 与 child exit 错误优先级、
多事件并发下唯一 Exit、stderr 日志限长脱敏和关联字段测试。测试必须断言稳定错误码、
唯一 Exit 和无未回收的 worker，而不能只断言终端输出字符串。

# 终端统一：用户命令进程收进 Terminal Worker

> 状态：已完成（2026-11-03），四个阶段都已落地；Windows 部分未实测，见 AGENTS.md 的 Windows 待办。实施中把会话模式的归属从「控制终端」改为「会话号」（U9），执行模式改为先握手再启动命令（§5.2）。由 `terminal-gap-assessment.md` 的 T5 与「残留进程清理」讨论演化而来，取代其中 T5 的方案。
> 范围：一次性命令 `execute_terminal` 与交互终端 `terminal_*` 的进程启动、归属与清理；两个工具对 Agent 的边界。不含沙箱（只定接入点）与 T3 的读输出 / 停止工具（下一步，基于本设计）。

## 1. 要解决什么

需求（已确认）：**命令正常退出后，清理它留下的残留进程**；两个终端用**一套实现**管理，便于以后统一接沙箱。

现状（已核实）：

| 情形 | 一次性命令 `execute_terminal` | 交互终端 |
| --- | --- | --- |
| 命令正常退出，留下后台进程 | **不清理**。实测 `cmd &`、`nohup cmd &` 全部存活；残留握着输出管道时调用多卡 5 秒、输出可能被截断（`local_backend.py:105-125`） | 不清理——**这是对的**：交互 shell 里用 `&` 起后台任务是有意为之 |
| 超时 / 取消 | POSIX 直接 `SIGKILL` 进程组；Windows `taskkill /T` 只遍历当时的树，已被收养的进程漏掉 | — |
| 会话结束（关闭、shell 自己退出） | — | 关闭且 shell 还活着：从 shell 往下找子孙，HUP → TERM → KILL。**shell 先退出：macOS / Linux 一个都清不到**（`process_tree.rs:142-170`）。Windows 用 Job Object，两种都清得到 |
| 后端被强杀 | 命令在独立会话里，宿主打不到，成为孤儿 | worker 与后端同组被一起杀掉，只靠 PTY 关闭时内核的 SIGHUP 侥幸兜底 |

## 2. 已定的取舍

- **统一到 Rust worker，不改用 Python**：Windows 的 PTY 在 Python 侧仍需原生扩展（pywinpty 本身是 Rust 写的）；屏幕模型只能退回 pyte（慢，对全屏程序支持较弱）；解析大量输出会和后端主流程抢 GIL；已验证的 shell 集成、查询应答、写入线程、进程清理都要重写。worker 约 2700 行，规模可控。
- **沙箱不在本设计里**：安全与权限后续整体设计（AGENTS.md 待办「安全与权限」，终端部分的草案见 [`terminal-sandbox-design.md`](terminal-sandbox-design.md)，srt 与 Codex 沙箱都在候选里）。无论选哪个，对沙箱要紧的是**所有用户命令只从一处启动**——这正是本设计。
- **两个工具保持分开**：统一的是进程层，不是工具。见第 3 节。

## 3. 两个工具的边界（从 Agent 角度）

判断规则：**命令会自己结束、只要它的结果 → `execute_terminal`；需要它一直开着、需要和它一来一回、或需要用户看到并参与 → 交互终端。**

| | `execute_terminal`：任务型命令 | `terminal_*`：会话型工作台 |
| --- | --- | --- |
| Agent 要什么 | 一个结果（退出码与输出） | 一个持续存在的东西（服务、REPL、正在操作的程序） |
| 生命周期 | 跟随这次调用，结束即收尾、不留进程 | 跟随会话、归 Task，后续 Run 复用 |
| 状态 | 调用之间无状态；要串起来就写成一段脚本（`&&`、管道、多行、heredoc 都支持） | 有状态：cwd、环境、前台程序跨调用保留 |
| 交互 | 不交互：没有 stdin、没有 TTY | 交互：行、按键、信号、读屏幕、条件等待 |
| 用户 | 在对话卡片里看结果 | 底部面板里实时看、能打字、可交接 |
| 谁能用 | 所有 Agent | 侧栏对话的主 Agent（需要用户界面） |

灰色地带：

| 场景 | 归属 |
| --- | --- |
| 长构建 / 测试 | `execute_terminal` 后台，结束时收到结果 |
| 起服务、自己验证就停 | `execute_terminal` 后台 + T3 的读输出与停止（子 Agent 也要能做） |
| 给用户长期开着的服务、`tail -f`、REPL / TUI | 交互终端 |
| 可能会问问题的命令 | 先加非交互参数用 `execute_terminal`；做不到才用交互终端；涉及密码用交接 |

## 4. 调查结论（已核实）

| # | 发现 | 证据 | 影响 |
| --- | --- | --- | --- |
| U1 | worker 与后端同处一个进程组：宿主强杀兜底时对后端整组 `SIGKILL`，worker 一起被杀，来不及清理 | `worker_client.py:143-151`（无 `start_new_session`）；`electron/main/backend/process.ts:31,93` | 交互会话的后台作业逃逸 |
| U2 | 后端被强杀时，一次性命令在独立会话里（`start_new_session`），宿主打不到 | `local_backend.py:87` | 不写输出的命令永久存活 |
| U3 | 后端正常关闭时，后台命令只置撤销标记，靠 0.25 秒时间片发现；作业线程是 daemon，解释器先退出时命令可能没被杀 | `core/tool/background/registry.py:374-386`；`job.py:120`；`execute_terminal.py:341`（`wake` 为空） | 关停竞态 |
| U4 | worker 的进程归属只在清理那一刻、从仍活着的 shell 往下找；shell 先退出则一个都找不到；归属标识里没有控制终端，也不能枚举全部进程 | `process_tree.rs:127-170` | 会话 shell 自己退出后残留存活 |
| U5 | worker 协议与客户端握手都写死了 PTY 字段 | `protocol.rs:16-24`；`worker_client.py:450-462` | 加执行模式要改协议与客户端 |
| U6 | 退出帧只有 `exit_code: Option<u32>`，被信号结束统一映射为 1；一次性命令现在把被信号结束报成负的信号值 | `protocol.rs:216-252`；`local_backend.py:128` | 执行模式的退出帧要带信号 |
| U7 | Windows：cmd 必须整串交给 `COMSPEC /c`（按参数数组会把引号转坏），PowerShell 必须按参数数组；Rust 侧同样要区分（`CommandExt::raw_arg`）；Job 在启动后才挂入，有短竞态 | `local_backend.py:169-213`；`process_tree.rs:403-440` | 回归风险最高的一处，Windows 未实测 |
| U8 | 执行模式必须显式把 stdin 置空，否则命令会继承 worker 的 stdin——那是控制帧管道 | 现状由 `test_execute_terminal_shell.py:167-180` 守着 | 硬约束 |
| U9 | （第 1 阶段实测）会话首进程（shell）退出时，内核把控制终端从会话上摘掉：留下的进程 `ps` 显示 `??`（Linux `tty_nr` 归零），所以「控制终端 = 本 PTY」在 shell 先退出时同样找不到残留；会话号则一直保留到最后一个成员退出。另外，没有作业控制的 shell 里普通 `&` 进程与 shell 同在前台进程组，shell 退出或 PTY 关闭时会被内核的 SIGHUP 顺带结束，真正残留的是 `nohup`、`trap '' HUP` 与交互 shell 里 `disown` 的作业 | 用 `pty.fork` 实测：shell 退出前后 `ps -o tt` 由 `s011` 变 `??`，`getsid` 不变 | 会话模式的归属改用会话号 |

可复用的事实：macOS `proc_listallpids` 枚举全部进程、`proc_bsdinfo` 给启动时间与僵尸状态、`getsid` 给会话号，Linux `/proc/<pid>/stat` 一处给齐；`std::io::pipe` 可把 stdout 与 stderr 接到同一根管道；worker 的帧编解码、控制读线程、心跳、`LifecycleArbiter`（子进程退出 + 输出 EOF 或排空时限）与具体介质无关，可直接复用。

## 5. 终态结构

**所有用户命令进程只从 worker 启动，由 worker 里的一个进程归属模块收拾；后端只决定跑什么、怎么解释输出。**这也是以后接沙箱的唯一接入点。

```text
execute_terminal ── LocalExecutionBackend ──┐                          ┌─ 执行模式：管道、无 stdin、无 PTY，命令结束即退出
（命令、cwd、超时、取消、解码、实时推送）     ├─ worker 客户端 ══ worker ──┤
terminal_* ── SessionRuntime ───────────────┘   （每次一个进程）          └─ 会话模式：PTY + 集成 + 屏幕模型，会话结束才退出
                                                                             └─ 两种模式共用：进程归属 + 清理顺序 + Windows Job
```

### 5.1 worker 的进程归属模块（`containment.rs`，取代 `process_tree.rs`）

- **归属判断按启动方式选**：
  - 执行模式 = 命令自己的**进程组**。非交互 shell 没有作业控制，`&`、`nohup` 起的进程都留在组里；POSIX 保证组还有成员时组号不被复用，命令退出后再按组发信号是安全的。
  - 会话模式 = **与 shell 同一个会话**的全部进程（已实现）。PTY 启动时 shell 经 `setsid` 成为会话首进程；作业控制把后台任务分到不同的组，但会话号不变。清理时枚举全部进程（macOS `proc_listallpids`，Linux 列 `/proc`）按会话号筛出，排除僵尸，shell 先退出也找得到（U9）。会话号只在最后一个成员退出后才可能被复用：若 shell 的 pid 已换成别的进程，说明会话早已结束，不再清理。
  - 被取代的「从 shell 往下遍历」与按深度排序已删除。
- **清理顺序只有一份**：HUP → TERM → 宽限 1 秒 → KILL；没有残留时立即返回，不白等宽限。Windows 两种模式都挂 Job Object（`KILL_ON_JOB_CLOSE`），结束时终止作业。
- **清理时机**：执行模式在**命令结束时**（正常结束、超时、取消一致）；会话模式在**会话结束时**（关闭或 shell 自己退出），一条命令结束不清理。

### 5.2 worker 执行模式（已实现，`exec.rs`）

- 协议：首帧 `start_exec`（`program`、`args`、`raw_args`、`cwd`）或 `start_session`（原 `start`，绿地项目不保留旧名），之后不能换模式；入口 `worker.rs` 只读首帧并分派，会话循环在 `session.rs`。握手带 `mode`，执行模式不带 PTY 字段；会话客户端拒收执行模式的握手。
- 启动：**先握手再启动命令**，启动失败（找不到程序、cwd 不存在）以 `EXEC_START_FAILED` 错误帧经已确认身份的连接报回，不会被当成 worker 起不来；执行模式因此没有「握手前命令已开始执行」的问题（§9）。stdin 置空；stdout 与 stderr 接到同一根管道，保留交错顺序；POSIX 放进新进程组；Windows 挂 Job、`CREATE_NO_WINDOW`，`raw_args` 时参数按原文拼进命令行（其他平台传 true 是协议错误）。不设 `TERM`。
- 结束：命令退出 → 清理进程组残留 → 读到管道 EOF（残留被结束后 EOF 立即到来；有进程脱离进程组还握着管道时，等到排空时限 2 秒）→ 上报 `exit`（`exit_code`，被信号结束时另带 `signal`）→ worker 退出。`shutdown`、后端关闭 stdin、心跳超时都先清理整组（连命令本身），再走同一顺序。
- 输出帧沿用 `output`；不发 `screen`、`shell` 事件；输入、尺寸帧回非致命拒绝，信号帧回 `unsupported`。
- 命令进程只交给退出观察线程回收；结束命令一律经归属清理（按 pid 发信号或结束作业）。

### 5.3 worker 自成进程组

后端以 `start_new_session` 拉起 worker（U1）。宿主强杀后端时 worker 不被波及，读到 stdin EOF 后自行清理再退出；两种模式都受益。Windows 由 Job 的 `KILL_ON_JOB_CLOSE` 兜底。

### 5.4 后端（已实现）

- `LocalExecutionBackend` 保留名字，内部改走 worker 执行模式；删除 `Popen`、`_kill_tree`、`taskkill` 与 0.25 秒轮询。一个实例只跑一条命令，取消改为 `stop()`：`ThreadJob` 的 `wake` 接到它，撤销与 Run 取消立即让 worker 结束整组（消除 U3），命令启动前调用也生效。
- `_resolve_command` 留在 Python（「`auto` 指什么、怎样启动」归工具，见 AGENTS.md 对 `shell/` 的约定），产出启动参数：POSIX `auto` 为 `/bin/sh -c <命令>`；Windows `auto` / `cmd` 为 `<COMSPEC>` + 原文参数 `/c "<命令>"`（与 `subprocess` 的 `shell=True` 同一形态）；显式 shell 把命令作为单个参数。
- 输出收集器改为接收 worker 推来的字节块（`feed` / `finish`），沿用同一个增量解码器（UTF-8 / GBK / 本机编码）；实时推送 `TerminalOutputStream` 不变。
- worker 客户端按模式提供 `start` / `start_exec` 两个入口，握手按模式校验；`shutdown()` 只请求结束、不断开，退出仍经 `exit` 事件报回。
- 退出码语义对齐现状：被信号结束报负的信号值，超时 / 取消为 -1，启动失败为 -1 且输出写明原因；worker 在命令结束前断开时为 -1 并说明结果未知。
- 测试：真跑命令的用例统一用 `tests/terminal_test_support.py` 的 `requires_terminal_worker`（worker 路径由 `conftest` 缺省指向本仓构建产物）。

## 6. 分阶段（每步可单独验证）

1. **worker 进程归属模块重构 + worker 自成进程组**（已完成）：会话模式按会话号归属。单独这一步就修好「shell 自己退出后残留清不掉」。验证：shell 自己退出后它留下的 `nohup` / `trap '' HUP` 进程被清理（Rust 单测 + `test_terminal_worker_integration.py`）；会话外的进程不受影响；后端整组被强杀后 worker 自行清理会话进程。两个集成用例都在旧实现上复现了残留，在新实现上通过；交互 zsh 里 `nohup`、`disown`、`trap '' HUP` 三种作业在 `exit` 后全部被清理。
2. **worker 执行模式**（已完成）：协议、握手、管道合并、stdin 置空、进程组、Windows Job 与原文传参、带信号的退出帧。Rust 单测覆盖协议、合并输出、空 stdin、残留清理与放开管道、信号退出、启动失败；`tests/exec_mode.rs` 经 stdio 帧驱动真实二进制，覆盖握手、输出、退出码、`shutdown`、后端断开、拒收输入帧与启动失败。Windows 原文传参与 Job 未在本机编译运行，随 CI 与 Windows 待办验证。
3. **`execute_terminal` 改走执行模式**（已完成）：后端执行后端、输出收集、`wake` 接 `shutdown`、删除旧杀树代码；真跑命令的后端测试改为依赖已构建的 worker（与现有 worker 集成测试同一套定位与跳过规则）；Windows 引号规则仍由 Python 侧的 cmd / PowerShell 用例锁住，它们改为经 worker 执行，只在 Windows 上运行（未实测，见 Windows 待办）。验证：`cmd &` / `nohup cmd &` 在命令退出后被清理且不多等（旧实现同一命令 5 秒、两个进程存活）；超时先 HUP（命令的 trap 能收尾）；停止在命令启动前也生效；后端整组被强杀后命令被清理。每条命令多一个 worker 进程，实测 `true` 由 6.8 毫秒变为 13.3 毫秒。
4. **描述与文档**（已完成）：两个工具的描述按第 3 节重写（子 Agent 只有 `execute_terminal`，它的描述不点名交互终端，只写「需要交互或长期开着时换别的方式或交回上级」）；`interaction_contract.md` 的 worker 协议扩充为两种模式；AGENTS.md 中「工具子进程由工具执行层负责进程树清理」「`shell/` 怎样启动仍归各工具」两句按新结构改写；README 补「直接跑后端需要设置 `CODING_AGENT_TERMINAL_WORKER`」；Windows 待办补执行模式的引号与 Job 验证。

## 7. 行为变化

- **有意改变**：
  - 命令结束时它留下的进程被清理；`npm run dev &` 不再能偷留服务，长驻服务改用 `background=true` 或交互终端。
  - 不再有 5 秒等待与输出截断。
  - 超时 / 取消改为先 HUP、TERM 再 KILL，最多慢 1 秒。
  - 后端被强杀时，一次性命令与交互会话都被清理。
  - Windows 改用 Job Object。
  - 交互会话 shell 自己退出后，残留被清理。
- **代价**：
  - `execute_terminal` 依赖 worker 二进制：直接跑后端要设环境变量，真跑命令的测试要先构建 worker。
  - 每条命令多起一个 worker 进程（十几到几十毫秒）。
  - 输出多经一跳 JSON 帧，大输出吞吐略降。
- **不变**：工具参数；`auto` 的语义（一次性命令仍是 `/bin/sh -c` / `COMSPEC /c`）；环境变量（`user_command_environment`）；没有 TTY；输出解码；前后台与回投；输出预算；卡片展示。

## 8. 刻意不做

- 用 `setsid` 或守护化主动脱离进程组 / 控制终端的进程：已知盲区，留给沙箱。
- Windows 上挂入 Job 的启动竞态（需要挂起启动再恢复）：两种模式一致接受，记入 Windows 待办。
- Windows 上的温和终止阶段：Job 直接结束。
- T3 的读输出与停止工具：下一步，基于执行模式做（命令运行时持有 worker 句柄）。
- worker 进程池：每次一个进程，简单且隔离清楚。
- 沙箱接入：接入点就是 worker 的启动入口，方案见 [`terminal-sandbox-design.md`](terminal-sandbox-design.md)。

## 9. 风险

- **Windows 引号与 Job**（U7）：Rust 侧要重做 cmd / PowerShell 的传参规则；Windows 从未实测。缓解：把现有规则写成两边共享的测试用例，Windows 真机验证列入待办。
- **心跳误杀长命令**：worker 15 秒收不到心跳即结束；后端长时间停顿（调试断点、GIL 饥饿）会误杀最长可达 3600 秒的一次性命令。与交互会话同一限制，本次不改，出现案例再调。
- **握手前命令已开始执行**：只剩会话模式（shell 先启动再握手；握手被拒或超时时 shell 随后被清理，接受）；执行模式先握手再启动（§5.2）。

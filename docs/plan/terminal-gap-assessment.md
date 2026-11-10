# 终端能力差距评估（对照 Codex / Claude Code）

> 状态：调查结论（2026-11-02）；P0（§7.1）与 P1 交互终端稳健性（§7.3）已实施未提交、P2 界面第一阶段（§7.4）已实施未提交；T5 已由 `terminal-process-unification-design.md` 实施；T3 / T4 由 [`terminal-unified-read-plan.md`](terminal-unified-read-plan.md) 实施（待执行）；P2 第二、三阶段待实施。
> 范围：一次性命令 `execute_terminal`、交互终端（Rust worker + `terminal_*` 工具 + `terminal-v2` 通道）、前端终端工作台与对话内终端卡片。**沙箱不在本稿范围**；安全与权限按「先功能后安全」只记待办（§6）。

## 1. 参照系

Codex CLI 与 Claude Code 的「终端」是**给 Agent 的命令执行工具**，人和 Agent 不共用同一个 PTY。本项目额外有「人机共用的交互终端 + 交接」，这一层更接近 VS Code / Warp / Cursor 的 Agent 终端。因此分两层对照：

- **执行工具层**（命令怎么跑、环境、输出、前后台）：对照 Codex / Claude Code。
- **共享终端层**（PTY、屏幕、人机仲裁、重连）：对照 VS Code 终端与 shell integration。

外部产品的做法依据公开文档与使用经验，细节以各自官方文档为准；表中标「约」的为中等置信度。

## 2. 现状概览

```text
execute_terminal ── ThreadJob ── LocalExecutionBackend ── subprocess(/bin/sh -c | <shell> -c)
                                 （无 stdin，前台 / 后台，结束回投 background_result）

terminal_* 工具 ──┐
                  ├─ TerminalSessionService ── SessionRuntime ── worker_client ══ Rust terminal-worker
前端 xterm ── WS ─┘   （逻辑行、条件等待、人机仲裁）          （长度前缀 JSON）  （portable-pty + alacritty + OSC 633）
```

**做得比参照对象好的部分**（保留，不在本稿改动）：

- Rust worker 内置无界面屏幕模型（alacritty），给 Agent 的是渲染后的逻辑行（绝对行号、全屏程序单独成屏幕视图），而不是原始字节流。
- 私有 OSC 633 shell integration（带一次性 token）判定命令边界与每条命令的退出码。
- 人机仲裁（用户输入从不被拒；用户打字时 Agent 等待；用户启动的前台命令拒绝 Agent 输入）与 `terminal_handoff` 交接。
- 进程树清理：Unix 从 shell 遍历子孙，HUP → TERM → KILL；Windows 用 Job Object `KILL_ON_JOB_CLOSE`。
- 宿主存活管道：worker 读到 stdin EOF 或 15 秒无心跳自行退出，不留孤儿。

## 3. 问题清单

每条写明现象、证据、根因层级、置信度。「已核对」表示本稿作者抽查过所引代码；其余来自子 Agent 调查，引用行号已复核存在，但结论未逐条实测。

### P0：执行环境（决定 Agent 在用户机器上能不能用）

#### T1. 两类终端都拿不到用户登录 shell 的环境（已核对，置信度高）

- **现象**：nvm / pyenv / Homebrew 加进 PATH 的工具在 Agent 命令里可能找不到；打包后从 Dock 启动时，Electron 只拿到 launchd 的最小 PATH（`/usr/bin:/bin:/usr/sbin:/sbin`），问题更明显。
- **证据**：
  - 一次性命令 `auto` 走 `/bin/sh -c`，显式 zsh / bash 也只是 `-c`，都不是 login shell：`apps/backend/app/tools/tool_handler/terminal/local_backend.py:192-211`；参数描述也承认 rc 与 PATH 增补不生效：`apps/backend/app/tools/tool_models/execute_terminal_args.py:102-109`。
  - 交互终端 zsh 只追加 `-i`：`apps/terminal-worker/src/shell_integration/mod.rs:83`；bash 在 darwin 上却在 rcfile 里手动补跑 login 链：`apps/terminal-worker/resources/shell_integration/bash/bashrc:9-23`。两种 shell 口径不一致。
  - 宿主拉起后端时只透传 `process.env`，没有解析用户 shell 环境：`apps/desktop/electron/main/backend/process.ts:29-48`。
- **根因层级**：结构——「Agent 命令的基础环境」没有事实源，各执行路径各自继承后端进程环境。
- **同类出现点**：`execute_terminal`、交互终端 worker，两处同一根因，应一处解决。
- **参照**：Codex 用登录 shell（`-lc`）执行；Claude Code 先对用户 shell 拍环境快照（PATH、alias、函数）再执行每条命令；VS Code 启动时用 login shell 解析一次环境（`resolveShellEnv`）。

#### T2. 子进程环境没有构造策略，整份继承后端进程（已核对，置信度高）

- **现象**：Agent 命令与交互 shell 能看到并继承后端私有变量。
- **证据**：
  - 一次性命令 `Popen` 不传 `env`：`apps/backend/app/tools/tool_handler/terminal/local_backend.py:77-86`；worker 同样不传：`apps/backend/app/tools/terminal_runtime/worker_client.py:143-151`；worker 再原样交给 shell，只加 `TERM` 与集成变量：`apps/terminal-worker/src/pty.rs:45-51`。
  - 继承来源：宿主注入的 `CODING_AGENT_HOST_TOKEN` / `PORT` / `DATA_DIR` / `TERMINAL_WORKER`（`apps/desktop/electron/main/backend/process.ts:36-46`）；`.cosir/.env` 写入 `os.environ` 的键（`apps/backend/app/config/settings.py:86-100`）；开发模式下后端 venv 的 `bin` 前置 PATH 并设 `VIRTUAL_ENV`（`apps/desktop/electron/main/backend/process.ts:58-64`）。
- **功能影响**（不谈安全也成立）：
  - Agent 在别的项目里跑 `python` / `pytest`，开发模式下会落到 cosir 后端的 venv。
  - **待验证假设**：Agent 开发 cosir 自身时，继承的 `CODING_AGENT_*` 可能让它拉起的后端或测试指向正在运行的实例与数据目录。
- **根因层级**：同 T1，缺少「Agent 命令环境」这一层。
- **参照**：Codex `shell_environment_policy`：一处策略作用于它启动的全部子进程，按「自动排除 → 自定义排除 → `set` → 白名单」应用；按名字排除 KEY / SECRET / TOKEN 需用户关掉默认为 `true` 的 `ignore_default_excludes` 才生效。

### P1：Agent 执行模型

#### T3. 一次性命令与交互终端各管一半，中间缺能力（已核对，置信度高）

- **现象**：
  - `execute_terminal` 没有 stdin；转后台后只在结束时回投结果，运行中 Agent 读不到输出；不适合服务与 watcher（`apps/backend/app/tools/tool_handler/execute_terminal.py:76-88`）。
  - `terminal_*` 只开放给侧栏主 Agent；单次条件等待上限 30 秒（`apps/backend/app/config/constant.py:79`），命令结束不通知。
  - 结果：**子 Agent 与 Team 节点跑不了开发服务器 / watcher**；一次性命令中途要输入只能杀掉换交互终端重来；交互终端里的长命令只能轮询。
- **根因层级**：设计不足——两套工具按「有无交互」切分，但「长时间运行 + 需要观察」落在两者之间。
- **参照**：
  - Claude Code：`run_in_background` + 读取后台增量输出 + 停止后台命令，结束时通知。
  - Codex：unified exec——命令先当一次性跑，超过 `yield_time` 未结束就返回会话 id，之后用 `write_stdin` 输入或轮询（约）。
- **可选路线**（待决，见 §7）：A. 给后台命令补「读取增量输出」，复用 `background_registry`；B. 向 Codex 式统一模型收敛。

#### T4. 模型看到的输出保留 ANSI 控制序列（已核对，置信度高）

- **证据**：`_render_content` 把原始输出直接拼进模型正文：`apps/backend/app/tools/tool_handler/execute_terminal.py:565-586`；后端其余位置没有 ANSI 剥离。
- **影响**：浪费 token，干扰模型解析（进度条、颜色码）。
- **建议**：只在模型正文剥离，展示数据保留颜色。交互终端已经过屏幕渲染，不受影响。

#### T5. 一次性命令取消 / 超时直接 SIGKILL（已核对，置信度高）

- **证据**：`apps/backend/app/tools/tool_handler/terminal/local_backend.py:150-164`。
- **影响**：测试、构建没有机会清理临时文件或子服务；与交互终端的 HUP → TERM → KILL（`apps/terminal-worker/src/process_tree.rs:60-98`）口径不一致。
- **同类出现点**：两条路径都只按进程组 / 从 shell 遍历，`setsid` 或二次 fork 后脱离的子孙会逃逸（`process_tree.rs:143-185`）；非 Linux / macOS 的 Unix 枚举不到子进程、只杀 shell（`process_tree.rs:382-385`）。

### P1：交互终端稳健性

#### T6. 粘贴超过 64 KiB 会断开通道（已核对，置信度高）

- **证据**：前端整段粘贴只发一帧（`apps/desktop/components/terminal/terminal-view.ts:329-343` → `:113`）；后端校验 1 byte..64 KiB，超出回协议错误并结束连接（`apps/backend/app/api/terminal_stream_api.py:193-203`、`:89-97`，上限在 `apps/backend/app/config/constant.py:61`）；前端随即进入 `failed`，需手动重连。
- **建议**：前端按上限分块发送。

#### T7. 「前台命令是谁发起的」靠最近一次输入来源推断（链路已核对，后果待验证）

- **证据**：
  - `command_started` 到达时取 `_last_input_by` 作为发起方：`apps/backend/app/tools/terminal_runtime/observation.py:31-32`、`:93-103`。
  - 用户任意字节都会记为 `user`：`apps/backend/app/tools/terminal_runtime/session_runtime.py:786-795`。
  - xterm 对终端查询（DA / DSR）的自动应答也走 `onData`，被当成用户输入发出：`apps/desktop/components/terminal/terminal-view.ts:113-115`；而 worker 也已自行应答这些查询：`apps/terminal-worker/src/worker.rs:274-282`。
- **待验证后果**：查询被回答两次，多余字节落进程序；Agent 被无故延迟（`TERMINAL_USER_TYPING`）；Agent 自己的命令被记成用户发起，之后输入一律被拒（`TERMINAL_USER_COMMAND_RUNNING`）。仲裁在锁内、投递在锁外，也存在竞争窗口（`session_runtime.py:483-490`）。
- **设计不足**：没有集成的 shell（fish / cmd / custom / 集成失败）完全没有这层保护，也没有 `tcgetpgrp` / 进程名兜底（`session_runtime.py:731-733`）。

#### T8. 重连重放原始字节，不是屏幕状态（置信度中高）

- **证据**：attach / resync 从 1 MiB 原始字节窗口重放（`apps/backend/app/tools/terminal_runtime/stream.py:154-192`，`apps/desktop/lib/terminal/terminal-connection.ts:219-226`）。
- **影响**：窗口之前建立的模式（备用屏幕、滚动区、DECCKM、鼠标模式、SGR）丢失，长会话或全屏程序画面错乱。切换 Task 时 `TaskPage` 以 `key` 重建（`apps/desktop/components/layout/app-layout.tsx:368-369`），所有终端都会走一遍这种重放，1 MiB 之前的历史丢失。
- **参照**：VS Code 用序列化的屏幕状态恢复终端；worker 已有 alacritty 屏幕模型可复用。

#### T9. 阻塞隐患（代码事实已确认，实际影响置信度中）

- 模型给的正则在会话锁内执行（`apps/backend/app/tools/terminal_runtime/wait.py:104-113`，`render.py:190`），事件循环里的 WS 用户输入同步获取同一把锁（`session_runtime.py:787`）。灾难性回溯会卡住该会话的输出处理，并阻塞整个事件循环。
- worker 单控制循环里同步 `write_all` 写 PTY（`apps/terminal-worker/src/pty.rs:78-86`，`worker.rs:110`）。前台程序 raw 模式且不读输入时写入阻塞，心跳、shutdown 与 Ctrl-C 排在后面，恢复后可能被心跳超时误杀（`worker.rs:87-94`）。
- 单帧 4 MiB 不分片，超限终止会话（`apps/terminal-worker/src/protocol.rs:7`，`worker.rs:299-311`），只在极端控制序列下可能触发。

### P2：界面体验

| 位置 | 缺口 | 证据 |
| --- | --- | --- |
| 人机协作 | 「Agent 正在操作」只有一条 2px 左侧色条；仲裁结果（等待 / 拒绝）用户看不到；标签上的 Bot 图标表示创建者而非当前操作者；交接中的标签在标签栏无标记 | `terminal-view.ts:215-217`、`:242-246`；`terminal-tab-bar.tsx:75-90` |
| 一次性命令卡片 | 无耗时；无复制输出 / 复制命令 / 重跑；前端 scrollback 5000 行，丢行无提示；输出里链接不可点 | `terminal-output-view.ts:53`、`:58-61`；`terminal-result-view.tsx` |
| 工作台 | 无右键菜单、清屏、字号缩放、重命名、拆分窗格、搜索选项（大小写 / 正则）、「选区加入对话」 | `components/terminal/` 全目录检索 |
| 渲染性能 | DOM 渲染器，未用 WebGL addon；工作台对大量输出直接 `write`，无写入流控 | `apps/desktop/package.json:48-52`；`terminal-view.ts:126` |
| 粘贴 | 2-10 行的多行粘贴不确认直接执行（阈值 >1000 字符或 >10 行） | `apps/desktop/lib/terminal/terminal-keyboard.ts:42-48` |
| 键位 | `macOptionIsMeta: false`，Option+B / F 按词移动不可用 | `terminal-view.ts:98` |

以上 `components/terminal/`、`components/assistant-ui/tools/` 下文件省略了 `apps/desktop/` 前缀。

### P2：生命周期与可观测

- 无空闲回收；已结束会话仍持有缓冲（每个约 1 MiB 原始字节 + 2M 字符，全局最多 64 个）：`apps/backend/app/config/constant.py:42-53`。
- 后端退出时串行关闭会话（`apps/backend/app/tools/terminal_runtime/session_service.py:146-157` → `_close_matching` `:204-217`），每个至少约 1 秒宽限，32 个会话可能超出 15 秒硬退出窗口（`apps/backend/app/parent_watch.py:25`）；worker 读到 EOF 会自行清理兜底，属依赖兜底而非泄漏。
- 无会话录制；用户在交互终端里的输入完全不留记录。
- worker 崩溃后不能自动重启 shell。

## 4. 对照表

| 维度 | 本项目 | Codex | Claude Code |
| --- | --- | --- | --- |
| 执行 shell | `/bin/sh -c` 或 `<shell> -c`，非 login | 登录 shell `-lc` | 用户 shell + 环境快照 |
| 子进程环境 | 整份继承后端进程 | `shell_environment_policy` | 继承 + 快照 + 设置项注入 |
| 跨调用 cwd | 不保留 | 不保留 | 保留 |
| 默认 / 最大超时 | 300 s / 3600 s | 可设 `timeout_ms`（约） | 2 min / 10 min |
| 后台命令 | 支持，结束回投；运行中不可读输出 | unified exec 会话可轮询（约） | 支持，可读增量输出、可停止、结束通知 |
| 命令要输入时 | 一次性命令无 stdin；交互终端仅主 Agent | `write_stdin`（约） | 不支持交互命令 |
| 给模型的输出 | 预算内头尾保留，全文落盘；**保留 ANSI** | 头尾截断 | 截断并剥离 ANSI（约） |
| 取消 | 直接 SIGKILL 进程组 | — | — |
| 人机共享终端 | 有：PTY + 屏幕模型 + 仲裁 + 交接 | 无 | 无（用户另用 `!` 执行命令） |

## 5. 不算作差距

- **一次性命令之间不保留 cwd**：Codex 同样无状态，Claude Code 保留，属取舍；本项目的工具描述已明确告知模型。
- **终端不跨后端重启恢复**：两者也都不恢复；重启后已有「旧终端失效」提醒（`apps/backend/app/core/tool/background/restart_settlement.py`）。

## 6. 记入安全待办（不在本轮处理）

- 终端 WebSocket 没有 Origin / token 校验，只靠 session_id 随机性（`apps/backend/app/api/terminal_stream_api.py`）。
- 子进程环境里的密钥脱敏：P0 只去掉本应用自己的变量（见 §7），用户 shell 里本来就有的密钥照常继承。
- 命令级防线：原 deny-list 已删除，改由沙箱接管（见 AGENTS.md 待办「安全与权限」）。

## 7. 建议路线与待决问题

### 7.1 P0 定稿方案（T1 + T2，2026-11-02 确认）

**根因与出现点**：T1 的根不在终端而在后端进程本身——从 Dock 启动时后端继承 launchd 的最小 PATH，它派生的一切子进程跟着错。受影响的有四处：`execute_terminal`、交互终端 worker 的 shell、MCP stdio 服务（SDK 只从 `os.environ` 白名单继承 PATH 等，`npx` / `uvx` 类服务起不来）、后端自己的 shell 探测（`host_shells.find_shell` 依赖 PATH）。所以在进程树的根上修一次，不在每个工具上各补 login 参数。

T2 的三个来源各有进入后端环境的理由，处理不同：

| 来源 | 为什么在后端环境里 | 处理 |
| --- | --- | --- |
| `CODING_AGENT_*` | 宿主与后端的契约 | 留在后端，不交给用户命令（按前缀） |
| `.cosir/.env` 注入的键 | 配置加载机制：`os.environ` 是配置读取来源 | 复用 `Settings.is_file_loaded_value` 的注入记录过滤，不另立清单 |
| 开发模式的 venv（`interpreterEnvironment`） | 原意是让 Agent 的裸 `python` 用后端解释器，层级放错：任何项目里都会用到 cosir 的 venv | 删除 |

**改动**：

1. **后端启动时解析一次用户登录 shell 环境**（`app/tools/shell/login_environment.py`）：在 `app/__main__.py` 日志就绪之后、`Settings.load()` 之前执行，合并进 `os.environ`，之后的工具注册、shell 探测、MCP 连接池与 reload 子进程都自动拿到正确环境。
   - 归属依据：AGENTS.md「能力归哪个进程，按它离不开的运行时依赖与事实所有者定」——这份环境的消费者全在后端子进程树里，解析也不依赖 Electron；与 VS Code 在主进程解析同理（在消费者进程树的根上解析一次），且后端无论经宿主、直跑还是打包启动都成立。
   - 做法参照 VS Code `shellEnv.ts` 与 npm `shell-env`：`$SHELL -i -l -c`（csh / tcsh 用 `-ic`）执行 `/usr/bin/printf 标记; /usr/bin/env -0; /usr/bin/printf 标记`，随机标记隔开 rc 的输出，NUL 分隔保住多行值；设 `COSIR_RESOLVING_ENVIRONMENT=1` 供用户 rc 跳过 tmux 等；stdin 为 `/dev/null`，10 秒超时杀进程组；Windows 跳过。不用 `sys.executable -c` 打印：打包后的冻结可执行文件不能执行 `-c`。
   - 合并规则：`CODING_AGENT_*` 以宿主注入为准；放在 `Settings.load()` 之前，`.env`「不覆盖已存在变量」的语义不变（用户 shell 导出的同名变量优先，配置中心照常显示来源为 process）。
   - 失败：回退原环境、写 warning 日志；bootstate 记一步 `resolve_shell_env` 便于排查卡住。
2. **用户命令环境的唯一构造入口**（`app/tools/shell/command_environment.py`）：`os.environ` 去掉 `CODING_AGENT_*` 与 `.env` 注入的键；`execute_terminal` 与 worker 的 `Popen` 显式传入。MCP 不改：SDK 只继承白名单，不会泄漏，PATH 随第 1 步修正。
3. **删除宿主的 `interpreterEnvironment`**：开发 cosir 本身时 Agent 用 `uv run` 等写法，与对待其他项目一致。
4. **交互终端 macOS 上 zsh 以 `-l -i` 启动**：与 bash 在 darwin 上已补跑的 login 链、Terminal.app / VS Code 的默认一致，`.zprofile` 包装因此生效；`.zlogin` 包装删除——用户 `.zshrc` 之后 `ZDOTDIR` 已指回用户目录，zsh 直接读用户的 `.zlogin`，包装永远用不到。Linux 不变。
5. **一次性命令保持 `-c`**：基底环境已含 login 环境（解析时带 `-i`，写在 `.zshrc` 的 nvm PATH 也在内），每条命令再跑 login 只增加耗时与 rc 噪音；同步修改 `MacExecuteTerminalArgs.shell` 描述。

**刻意不做**：`PAGER=cat` 等非交互约定（stdin 为 `/dev/null`、stdout 是管道，程序不会分页）；重构配置让 `.env` 不再写进 `os.environ`；alias / 函数快照；可配置的环境策略（不预留扩展点）；解析失败的界面提示（先记待办）。

**行为变化**：从 Dock 启动时后端与全部子进程拿到用户 PATH；Agent 命令里不再出现 `CODING_AGENT_*`、`.env` 的键与 cosir 的 venv；后端就绪多等通常不到 1 秒，最坏 10 秒（宿主就绪时限 90 秒）。

### 7.2 T3 / T4 / T5 深度调研与方案（2026-11-02，待确认）

#### 调研结论：三个问题同一个根

三条都指向同一处结构缺口：**一次性命令在运行期间没有所有者**。命令只活在 `ThreadJob` 的一个局部函数里（`execute_terminal.py:310-337`），进程句柄、输出历史、生命周期都没有可寻址的事实源；`background_registry` 按契约只管结论回投、不持有执行状态（`core/tool/background/registry.py:1-11`）。由此：

| 现象 | 直接原因 | 证据 |
| --- | --- | --- |
| T3：Agent 读不到运行中的输出、停不掉自己的命令 | 输出只在作业局部的 `_OutputCollector` 里（无上限），停止只有 HTTP 入口 | `output_collector.py:93,115`；`transport_assistant_service.py:236-272` |
| T3：子 Agent / Team 节点的后台命令比子 Task 的结论活得更久 | `wait_quiescent` 不看登记表，子 Task 带着运行中的命令交回结论；之后命令结束会给子 Task 开一个没人等的 Run | `core/task_actor.py:325-336`（已核对） |
| T4：模型看到 ANSI 与 `\r` 进度帧 | 只有工具知道这是终端字节流，`_render_content` 原样拼接；预算层契约写明「终端输出在模型通道保持原文」 | `execute_terminal.py:509`；`tool_observation_budget.py:1-9`（已核对） |
| T5：取消直接 SIGKILL；正常退出不清残留进程；宿主兜底杀不到命令；Windows 无 Job | 进程归属与清理只是执行函数里的几行，命令在新会话里，宿主按后端进程组兜底打不到 | `local_backend.py:152-166`；`process.ts` 的 `process.kill(-pid)` |

附带发现（同一根因下的实现缺陷，随方案一并处理）：

- 前台命令也会构造一次后台结论并跑一次输出预算（`execute_terminal.py:337`），超限时多写一份没人引用的 artifact。
- 后台结论先过预算再 `json.dumps`（`registry.py:452`），ANSI 被转义成 `\u001b` 后实际长度超出预算。
- 取消时部分输出整段丢弃，超时却保留；超时正文没有 "timed out" 字样；退出码一律 -1。
- 工具描述「进程树会在超时时被杀」、`RESTART_WAKE_NOTICE`「后台命令已停止」与真实覆盖范围不符。

记下、不在本次处理：工具执行管线的进程隔离路径（`tool_handler_runner.py` 与 `windows_job_object.py`）在生产中没有任何工具使用，属于闲置代码，单独评估删除；后台结论的预算顺序是否在其他作业上也有同样问题，待查。

#### 成熟方案对照（来源：Codex `6c4fa868`、VS Code `ebd62d89` 源码，Claude Code CHANGELOG）

| 能力 | Codex（unified exec，默认开启） | Claude Code | VS Code Copilot | 本项目现状 |
| --- | --- | --- | --- | --- |
| 长命令 | `exec_command` 等到 `yield_time_ms` 未结束即返回会话 id | `run_in_background`；前台超时自动转后台、不杀 | `mode: async`；超时或长时间无输出转后台 | 可转后台；超时即杀 |
| 运行中读输出 | `write_stdin` 空写轮询，每次只给新增 | Read 输出文件 | `get_terminal_output` | **无** |
| 结束通知模型 | 无（靠轮询） | 有 | 有 | 有（`background_result`） |
| 停止 | 会话管理 | `TaskStop`（也停后台子 Agent） | `kill_terminal` | **仅用户** |
| stdin | `tty=true` 时可写 | 无 | `send_to_terminal` | 无（交互终端仅主 Agent） |
| 给模型的文本 | 头尾截断，未见 ANSI 剥离 | 超限存文件 | 读 xterm 缓冲区（已渲染，无 ANSI） | 原始字节流 |
| 取消清理 | 进程组 TERM → 50ms → KILL；Linux `PDEATHSIG`；Windows Job Object | TERM 再 KILL，杀整棵树 | 销毁终端 | 进程组直接 KILL；Windows `taskkill` |
| 会话上限 | 64，LRU 淘汰 | — | — | 无（一次性命令） |

共同点：**后台运行 + 可读增量输出 + 可停止**是三家都有的基线；stdin 不是（Claude Code 没有）。给模型的文本，最可靠的是 VS Code 的「先过终端模拟再给模型」。

#### 路线比较

- **A. 补齐基线（推荐）**：给一次性命令建运行时所有者，提供读输出与停止，三家基线都覆盖；契约改动集中在终端族内，现有 `background_result` + `wait_messages` 范式不变。
- **B. 把交互终端开放给子 Agent**：解决的是 stdin，不是基线缺口；带来全局 32 个会话名额被子 Task 占满、父 Agent 不能寻址子 Task 会话、子 Task 前端只读看不到等问题。**本次不做**，有真实需求再定。
- **C. 收敛到 Codex 式统一模型**：终态最干净，但要推翻「前台没有时限、不自动转后台」这条不变量（重启收尾依赖它，`core/tool/schemas/tool_background.py:25-26`），并合并两种展示契约。**本次不做**；A 的运行时按「将来能演进到 C」设计：按 `tool_call_id` 寻址、有界逻辑行、限时条件等待，与交互终端同一套读取契约。

#### 方案 A：一次性命令运行时

**新事实的所有者**：`app/tools/tool_handler/terminal/` 下新增命令运行时（`CommandRuntime` 管单条、`CommandService` 管集合，形状对齐交互终端的 `TerminalSessionService`），按 Task + `tool_call_id` 持有运行中的命令：进程树句柄、两路输出、生命周期。`background_registry` 仍只管结论回投与可等待判定，不变。

**T5 进程树**（已由 [`terminal-process-unification-design.md`](terminal-process-unification-design.md) 取代）

- POSIX：命令在新会话里（保持现状）；停止时对进程组 TERM，宽限 1 秒（与交互终端 worker 一致）后 KILL。
- **shell 正常退出后也清理进程组残留**：一次性命令拥有自己的进程树。`cmd &`、`nohup` 留下的进程今天会握住 stdout，让读线程泄漏、输出在 5 秒处被截断。需要长驻服务就用后台模式，进程归命令所有、可读可停。
- Windows：每条命令挂进带 `KILL_ON_JOB_CLOSE` 的 Job Object，`TerminateJobObject` 结束；后端崩溃时随句柄关闭由内核兜底。复用 `windows_job_object.py` 的 ctypes 实现，推广成按子进程句柄挂入。Windows 未实测，记入 AGENTS.md 的 Windows 待办。
- 后端退出、Task 删除时由 `CommandService` 统一终止，不再只置撤销标记就返回。macOS 上后端被强杀时命令仍会成孤儿：Linux 可用 `PDEATHSIG`，macOS 没有对应机制，记下不做。
- 结果表达对齐事实：取消时附带尾部输出；超时写明 "timed out after Ns"；被信号结束时报出信号；修正工具描述与 `RESTART_WAKE_NOTICE`。

**T4 给模型的文本（放 `app/tools/tool_handler/terminal/`，工具层纯逻辑）**

- 采集时分两路：原始字节照旧交给展示（卡片 xterm 需要颜色，展示契约不变）；另一路经流式终端规整进**有界逻辑行缓冲**，给模型用。
- 规整规则：区分 `\r\n` 与裸 `\r`（后者是回到行首覆盖）；`\b` 退格；`CSI K` 擦除；有限的光标上移（多行进度条）；丢弃 SGR、DCS 等；OSC 8 超链接只留可见文本；序列被分块边界切断时跨块处理。
- 不引依赖：pyte 是固定网格，管道没有终端宽度，会插入人为折行；worker 的 alacritty 绑在 PTY 上。
- 有界：保留开头若干行与最近 1 万行（与交互终端一致），中间丢弃并报告丢弃范围，顺带给今天无上限的 `_OutputCollector` 设上限。
- 前台结论、后台结论、读取工具都从这份规整文本取；只在需要回投时才构造后台结论（去掉多写的 artifact）；同步修改 `tool_observation_budget.py` 的 docstring 与固化原文契约的测试。

**T3 Agent 可用的读取与停止**

- 两个新工具（名称待定）：读运行中的输出，按绝对行号续读（无服务端游标，可重放）、可按正则搜索、可限时等待到「输出匹配 / 进程退出 / 安静」（上限 30 秒，用来等服务就绪）；停止命令。两者都不声明 `needs_user_surface`，子 Agent 与 Team 节点可用，profile 同步加入。
- 停止与用户取消同一语义：把今天只在 HTTP 路径里的取消编排（`transport_assistant_service.py:254` `_cancel_background_work`）下沉到 service 层，HTTP 入口与工具共用，占位结算、通知一致。
- **非侧栏 Task 交回结论时，撤销它仍在运行的后台工作**，结论里写明停掉了哪些；落点是委派与 Team 共用的「等子 Task 落定」处，不放进 actor。理由：子 Task 交回结论就是它的工作结束；需要长驻服务的应由上级自己运行。
- `execute_terminal` 描述改写：服务与 watcher 用后台模式，用读取工具等它就绪，不要对它们用 `wait_messages`（它们不会结束）。

**刻意不做**：子 Agent 的 stdin（路线 B）；前台超时自动转后台（Claude Code 的做法，但违反现有不变量）；统一模型（路线 C）；后台命令会话上限（一次性命令受超时上限约束）；macOS 下后端被强杀后的孤儿清理。

**分阶段实施（每步可单独验证）**

1. T5 + 运行时骨架：进程树模块、`CommandService` 接管运行中的命令与关闭和删除时的清理、结果文案。
2. T4：流式规整与有界逻辑行；模型通道改用规整文本；去掉多写的 artifact。
3. T3：读取与停止工具、取消编排下沉、非侧栏 Task 的结论规则、工具描述、前端工具目录、注册表守卫。

**待确认**

1. 非侧栏 Task 交回结论时撤销其后台工作（推荐），还是让它继续跑？
2. shell 正常退出后清理进程组残留（推荐），这会杀掉 `cmd &` 留下的进程。
3. 停止的宽限期 1 秒（与交互终端一致）是否合适？Codex 用的是 50 毫秒。

### 7.3 P1 交互终端稳健性：核实结论与方案（2026-11-03，T6 / T7 / T9a / T9b 已实施）

#### 核实结论

| 编号 | 结论 | 关键证据 | 处理 |
| --- | --- | --- | --- |
| T6 | **属实**。一次粘贴只发一帧；后端单帧上限 64 KiB，超出按协议错误断开，前端进入 failed | `terminal-view.ts:329-343`；`terminal_stream_api.py:195-203`；`constant.py` `MAX_RAW_INPUT_BYTES` | 修 |
| T7 | **属实（根因已定位）**。xterm 对终端查询的自动应答经 `CoreService.triggerDataEvent` 触发 `onData`，前端把 `onData` 全部当用户输入发出；worker 的 alacritty 也会应答同一批查询（DA1/DA2、DSR/CPR、DECRQM、窗口尺寸）。面板连着时程序会收到两份应答，多余的一份落成乱码（如 `^[[12;1R`）；这些字节还被记成「用户在打字」与命令发起方 | `@xterm/xterm/src/common/InputHandler.ts:1672-1719,2657-2678`；`alacritty_terminal-0.25.1/src/term/mod.rs:1257-1342,2046-2270`；`session_runtime.py:786-795` | 修 |
| T8 | **属实，但只影响画面**。重连 / 首次打开 / 背压重同步时从 1 MiB 原始字节窗口中间重放，窗口之前建立的模式丢失；Agent 读的是 worker 渲染后的逻辑行，不受影响 | `stream.py:154-192` | 记下不做 |
| T9a | **属实**。WS 处理在事件循环里同步调用 `runtime.user_input`，要拿会话的线程锁；工具线程在同一把锁里跑 Agent 给的正则。正则一旦卡住（灾难性回溯），整个后端的事件循环跟着卡住 | `terminal_stream_api.py:161`；`wait.py:104-113`；`session_runtime.py:786-787` | 修 |
| T9b | **属实，且比预期严重**。实测 macOS 的 PTY 在前台程序不读输入时只能缓冲 **约 1 KB**（canonical 与 raw 模式都是 1022 字节）。worker 在控制循环里同步 `write_all`，用户在命令运行期间粘贴超过 1 KB 就会卡住整个循环：Agent 的中断信号、resize、关闭都排在后面；卡住超过 15 秒后一旦解开，循环开头的心跳检查还会把会话杀掉 | `worker.rs:86-118`；`pty.rs:78-86`；实验脚本见下 | 修 |
| T9c | 单帧 4 MiB 不分片，只在极端控制序列下触发 | `protocol.rs:7` | 记下不做 |

T9b 的实验：在 PTY 从端跑一个不读输入的 `sleep`，以非阻塞方式往主端写，1022 字节后返回 `EAGAIN`。真实终端（iTerm 等）遇到同样的情况是把输入异步缓冲住，界面照常响应。

#### 方案（只改根因处，不加新机制）

1. **T7：终端查询只由 worker 应答。** worker 在没有面板时也必须应答，所以它是唯一应答方，前端 xterm 不再应答。用 xterm 的解析器钩子（`registerCsiHandler` / `registerDcsHandler`）吞掉这些查询，在源头不产生应答。吞掉的范围是 alacritty 会答的那批（DA1、DA2、DSR、DECRQM），再加上 xterm 独有的 DECRQSS 和 `CSI ? 6 n`。理由：程序拿到的应答不该随「面板开没开」而变化。在 `onData` 里按正则过滤不可取：那是在症状处打补丁，还可能误伤用户输入。
2. **T6：前端按 64 KiB 分块发送，并给总量设上限。** 后端的单帧上限是协议契约，保持不变。总量上限取后端输入队列一定装得下的大小（256 KiB）；超过时不发送，并提示「内容过大」，避免队列中途背压把一段粘贴截成半截执行。
3. **T9a：事件循环不碰会话锁。** WS 处理里对 runtime 的调用（输入、resize、订阅）改用 `asyncio.to_thread`。同一连接内按顺序 `await`，输入顺序不变。正则即使卡住也只影响这一个会话，不再拖垮整个后端。不另外给正则加超时：标准库 `re` 不支持超时，为一个低概率事件引入依赖不值得。
4. **T9b：worker 的 PTY 写入移到专用写线程。** 控制循环只负责把写入交给一个有界通道（`try_send`），自己不再阻塞，心跳、信号、关闭、resize 始终能及时处理。通道满了就拒绝这次写入，并发出非致命的背压事件；Python 侧把它转成前端已有的「输入被拒（背压）」提示，今天这类事件只写日志。Windows 的中断本来就是写 `0x03`，照样走写线程，保持顺序。

**刻意不做**

- T8 屏幕快照：成熟做法是 VS Code 那样，重连时把屏幕序列化下发。这需要给 worker 的 alacritty 网格写一个带属性和模式的序列化器，代价中等偏大，而影响只在画面上。等真正遇到再做。
- T7 的「仲裁在锁内、投递在锁外」竞争窗口：去掉自动应答之后，只剩用户恰好在那几毫秒里打字一种情况，记下不做。
- 没有 shell 集成的 shell 没有「前台命令发起方」保护：这是交互契约里写明的取舍，不改。
- T9c 大帧分片。

**影响范围**：前端 `components/terminal/terminal-view.ts`（解析器钩子、分块与上限）；后端 `api/terminal_stream_api.py`（`to_thread`）、`terminal_runtime/session_runtime.py`（worker 背压事件转成提示）；worker `src/worker.rs`、`src/pty.rs`（写线程）。需要同步：`interaction_contract.md` 的输入一节、worker 与后端的相关测试、前端终端视图的单测。

### 7.4 P2 界面：核实结论与方案（2026-11-03，三项决定已确认；第一阶段已实施）

前提：界面保持克制。从参照对象（Claude Code、Codex CLI 源码、VS Code 与 Copilot、Grok Build 源码、Warp）归纳出四条共同做法，本节逐项以它们为准：**默认折叠、成功不提示失败才着色、低频操作进二级入口（右键 / 悬停）、一个事实只在一个位置显示**。

#### 核实结论：§3 的 P2 清单有几条不准

| 原记录 | 核实 | 证据 |
| --- | --- | --- |
| 无清屏 | shell 的 Ctrl+L / `clear` 已可用，且只有它是真实清屏：xterm 只是后端缓冲的可重放视图，本地 `clear()` 在切换 Task 或 resync 时会被重放回来 | `terminal-keyboard.ts:28`；`shortcuts.ts:61-65`；`terminal-connection.ts:230-237` |
| 无写入流控 | 服务端已有按订阅者的有界队列，超限转 resync；只剩客户端写缓冲无界，积压超过 50 MB 时 xterm 的 `write` 直接抛错，而 `onOutput` 没有接住 | `stream.py:55-56`；`@xterm/xterm/src/common/input/WriteBuffer.ts:104-105`；`terminal-view.ts:128` |
| 2-10 行粘贴直接执行 | zsh 与 bash ≥ 5.1 默认开启 bracketed paste，多行只进编辑缓冲；真正会执行的是 macOS 自带 bash 3.2、cmd 与未开该模式的前台程序。确认文案「会被逐行执行」多数情况下不对 | `@xterm/xterm/src/browser/Clipboard.ts:53`；`terminal-view.ts:344` |
| 无重命名 | Agent 新建时可命名，标签显示 label；label 也进 Agent 的会话摘要 | `session_runtime.py:164`；`render.py:272` |
| `macOptionIsMeta: false` 导致不能按词移动 | 打开它也修不好：xterm 6 把带修饰的方向键一律编码成 `ESC[1;3D`，zsh 默认没绑；Cmd+方向键则什么都不发。Option+Backspace 删词已可用 | `@xterm/xterm/src/common/input/Keyboard.ts:84-88,113-135` |
| 卡片缺耗时 / 复制 / 重跑 / 丢行提示 / 链接 | 属实；另有两处**结构空洞**（下文 C1、C2）比这些更要紧 | 见下 |
| Agent 操作不可见 | 属实；但「拒绝」已显示在对话的工具卡片上，看不到的是终端面板一侧 | `terminal_session/common.py:24-28`；`call-row.tsx:60` |

#### A. 人机协作可见性（结构：缺「Agent 正在使用这个会话」的事实）

根因：「Agent 此刻在用、在等你停手」只存在于 runtime 的局部代码路径里（`session_runtime.py:760-790` 的等待循环、`_agent_waits` 计数），没有投影；现有唯一信号 `input_mark` 是 WS 瞬时帧，不进缓冲、不重放，未挂载的会话收不到，没有「结束」。标签上的 Bot 图标表示创建者（`createdBy`，创建后不变），被当成了操作者。

浏览器面板已有同构先例：宿主按标签计数 Agent 的进行中调用（`browser-host.ts:82,221` 的 `agentBusy`），标签图标按「弹窗 > Agent 操作中 > 加载 > 交接」取一个，面板上方一条 `AgentActivityBar`。终端照搬，不另起机制：

1. **runtime 产生事实**：会话持有「Agent 进行中的调用」计数与「正在等用户停手」标记，在 Agent 的输入与条件等待入口用 try/finally 维护，变化时 `notify_changed`。投影进 snapshot `terminal_sessions` 一个字段 `agentActivity: null | "using" | "waiting_for_user"`（命名实施时定）。不走 WS：`stream.py:144-149` 规定会话事实只经 snapshot，标签栏也需要全部会话的状态。
2. **标签图标只放一个**，优先级：交接（手形）> Agent 使用中（与浏览器同一图标）> 状态点；创建者退到 tooltip。交接标记读已有的 `handoffs`，纯前端。
3. **活动条只在当前会话**：`AgentActivityBar` 抽到共用位置，终端与浏览器同用；文案只有两种：「Agent 正在使用这个终端」「Agent 在等你停止输入」。不显示「拒绝」：那是事件，已在工具卡片上。
4. **顺带修正**：Agent 输入的来源记录与 `input_mark` 在投递之前就设置（`session_runtime.py:496,501` 早于 `:502`），投递失败也会留下标记，改到投递成功之后；同步修正与代码不符的文档：`interaction_contract.md:49,63`、`stream.py:147`、`terminal-connection.ts:30`（仍写交接经 `terminal_sessions` 投影）。

不做：`commandStartedBy` 投影（只在 Agent 被拒时有意义，拒绝已在卡片上）；Warp 式的接管快捷键（交接已有显式入口）。

#### B. 底部终端工作台（补一个二级入口，修四处错，其余不做）

1. **右键菜单**：低频操作唯一合适的落点。项目已有 radix `ContextMenu` 与 `menu-tokens.ts`（文件树、选区操作在用）。菜单项只有：复制（无选区时禁用）、粘贴（走现有确认）、查找。关闭后焦点交还终端；开了鼠标上报的程序也会收到这次右键，属已知行为。
2. **粘贴确认改成按模式判断**：对齐 VS Code `enableMultiLinePasteWarning: auto`——含换行且 `terminal.modes.bracketedPasteMode` 为假时才确认；字数阈值去掉，256 KiB 总量上限保留；文案改成实话。
3. **macOS 编辑键翻译**：在纯函数 `resolveTerminalKeyAction` 里加「发送序列」：Option+←/→ → `ESC b` / `ESC f`，Cmd+←/→ → `^A` / `^E`，Cmd+Backspace → `^U`（与 VS Code 在 macOS 上的默认键位同一做法，置信度中高）。`macOptionIsMeta` 保持 false：打开会让欧洲键盘打不出 `@ { [`。
4. **查找条焦点**：终端的 `FindBar` 没传 `focusKey`（对比 `text-find.tsx:84`），查找条已开时再按 Cmd/Ctrl+F 拉不回焦点。
5. **客户端写入积压兜底**：用 `write` 回调统计待写字节，超过阈值走现成的 resync（清屏后从缓冲重放），与服务端落后时同一语义，不改协议。

不做及理由：

| 项 | 理由 |
| --- | --- |
| 清屏按钮 / 快捷键 | 本地清屏是假象（会被重放回来）；Ctrl+L 已是真实清屏；Cmd+K 是应用命令面板 |
| 字号缩放 | 打包版有意去掉了缩放（`app-menu.ts:4-8`，缩放会改变 Agent 看到的布局）；终端字号变化还会改 PTY 行列数；Claude Code / Codex 都没有 |
| 重命名 | 做成前端偏好会让用户与 Agent 看到的名字分叉；做成后端事实代价中等、收益低 |
| 拆分窗格 | 工作台全程假设单一活动会话（`terminal-workbench-store.ts:24-31` 等），等于重写状态模型；参照对象中只有 VS Code 有 |
| 搜索的大小写 / 正则 | `FindBar` 是全应用共用的一个组件，只给终端加开关会不一致 |
| WebGL 渲染 | 每个常驻窗格一个 WebGL context，易碰 Chromium 上限，需另做降级；等实测卡顿再定 |
| 「选区加入对话」 | 引用模型以工作区路径为前提（`prepare.ts:3-14`），终端选区没有路径；Agent 本来就能读终端。等引用模型扩展时再做 |

#### C. 对话内命令卡片（先补两处结构空洞，再加三个克制的小能力）

1. **C1 命令一启动就有视图**：`terminal-result` 展示数据要等第一块输出到达才由投影创建（`tool_runtime_event_projection.py:71-79`），之前 `resolveCallView` 返回 null（`call-view-registry.ts:33-39`）。后果：一条不出声的前台命令（如 `sleep 300`）**卡片上没有停止按钮**。改法：命令启动时实时通道先发一帧空输出，展示数据随之建立；不在投影里按工具名特判。
2. **C2 超时与取消可以展开看输出**：投影本来就刻意保留了已流出的输出（`tool_call_event_projection.py:150-154`），但调用行对失败与取消一律不解析视图（`call-row.tsx:46`），展示契约也写着错误态不含终端输出（`tool_ui_display_contract.md:113`）。三处互相矛盾，结果是用户最需要看输出的时候看不到。改法（通用规则，不按工具特判）：调用行对失败与取消也解析视图，有 `kind` 就能展开；错误态的成功字段禁令不变——普通工具的错误展示本来就没有 `kind`，只有运行期流式累积的终端输出会带。契约同步写明这条。
3. **耗时**：后端在工具里量出 `duration_ms` 写进展示数据（前端计时在刷新、冷重建时会失效）；只放展开体底部、与 workdir 同一行灰字，折叠行不显示（Codex 单元格不显示，VS Code 只在悬停时显示）；运行中不走表（回合级已有计时）。格式统一用回合级的 `formatDuration`。
4. **复制**：展开体里两个悬停才显示的图标——命令行旁「复制命令」（取 `data.command`，多行命令只能靠它拿全），输出面板右上角「复制输出」；文本取卡片 xterm 已渲染的缓冲（所见即所得，没有 ANSI 与 `\r` 帧），成功后图标变对勾，不弹 toast。沿用用户消息复制按钮与回合脚注的现成写法。
5. **丢行提示**：只在卡片 xterm 确实裁剪了滚动缓冲时，在视口上方加一行灰字「仅显示最近 5000 行」；不加底色、不加边框。
6. **链接**：把底部终端的链接识别（网页 + 工作区绝对路径）抽成两种视图共用的函数，卡片接入同一套 `useOpenLink`；仍只认绝对路径（命令可能 `cd`）。

不做：卡片上的「重跑」——一次性命令不在 PTY 里，原地重跑既没有对应的模型调用，也违反「同一调用形态不变」；Codex、Claude Code、Grok 都没有。等方案 A 的命令运行时落地后，再评估「在终端中运行」（只粘贴不回车）。

#### 分阶段

1. **纯前端、不改契约**：B1-B5；A 的交接标签标记；C4-C6。
2. **后端事实 + snapshot 契约**：A1-A4（含活动条抽成共用组件、文档漂移修正）。
3. **卡片契约**：C1-C3（含展示契约修订）。

#### 已确认（2026-11-03）

1. C2：允许失败 / 取消的调用行在带运行期输出时展开（改调用行通用规则与展示契约）。
2. A2：标签上的 Bot 图标改为表示「Agent 正在使用」，创建者退到 tooltip。
3. C3：耗时只放展开体，折叠行不显示。

#### 第一阶段实施方案（纯前端、不改契约；2026-11-03 深读后定稿并实施）

前提：**macOS 与 Windows 都要可用**。Windows 下交互终端是 ConPTY，默认 shell 是 PowerShell（PSReadLine），也可选 pwsh / cmd；仓内 Windows 链路尚无实测（AGENTS.md 的 Windows 待办），下文凡涉及 Windows 的结论都标「待真机验证」。

##### 深读后的新发现（改变了原方案的几处）

| # | 发现 | 证据 | 对方案的影响 |
| --- | --- | --- | --- |
| F1 | **Windows 上工作区路径在终端里基本点不开**：识别层用根目录字面拼正则，大小写敏感、根内分隔符按原样；解析层两者都不敏感。`C:/repo/...`、`c:\repo\...` 都识别不到。另外只读单行（长路径折行即失效）、按字符串下标定范围（行内有中文时偏移） | `terminal-view.ts:357-386`；`resolve-link.ts:113-121` | C6 从「抽共用」升级为「重写识别规则」 |
| F2 | xterm `reset()` 不清写缓冲：积压时 resync，旧字节会先画在清屏后的画面上再接重放。今天服务端触发的 resync 就有这个竞态 | `@xterm/xterm/src/common/CoreTerminal.ts:244-250` | B5 的根因是「reset 时机」，不只是「积压上限」 |
| F3 | 去掉粘贴的字数阈值后，原生粘贴路径会绕过 256 KiB 上限（今天靠「超限必超 1000 字符」顺带拦下） | `terminal-view.ts:323-330` | B2 必须把上限检查放到所有粘贴路径上 |
| F4 | 断线期间粘贴被 xterm 静默丢弃，没有提示 | `@xterm/xterm/src/common/services/CoreService.ts:61-65` | B1/B2 一并处理 |
| F5 | 平台判断有两个来源：`isMac` 读 `navigator.platform`，`titlebarMode` 读宿主 `desktopHost.platform`；没有 `isWindows` | `lib/platform.ts:11-19` | 新增 Windows 行为前先收成一个来源 |
| F6 | 视图没设 xterm 的 `windowsPty` 选项，ConPTY 的换行与重排启发式不启用 | xterm `typings/xterm.d.ts:303,421`；全仓无使用 | 新增 W1 |
| F7 | portable-pty 打开了 ConPTY 的 win32-input-mode，xterm 6 不支持（`?9001h` 被忽略），输入退回有歧义的 VT 序列 | `portable-pty-0.9.0/src/win/psuedocon.rs:29,89`；xterm `InputHandler.ts` 无 9001 | 记下不做（ConPTY 会回退，影响是少数组合键有歧义） |
| F8 | `copyText` 成功必弹 toast；复制按钮没有可复用组件，回合脚注是内联写法 | `lib/clipboard.ts:21-31`；`turn-footer.tsx:32-58` | C4 先抽组件 |
| F9 | 卡片若用 `useOpenLink`，`call-row` / 后台看板的静态渲染测试没有 `QueryClientProvider` 会直接抛错 | `call-row.test.tsx:121-135`；`background-tasks.test.tsx:196-205` | 测试随改动补包装 |

##### 改造项（按根因归并）

**P0 平台判断收成一个来源（W0）**
- `lib/platform.ts` 以宿主 `desktopHost.platform` 为准、浏览器开发时退回 `navigator`，导出 `isMac` / `isWindows`；`titlebarMode` 也从它派生。理由：后面右键、键位、`windowsPty` 都要按平台分支，两个来源会在「浏览器开发 / E2E」与「桌面」之间给出不同答案。
- 纯函数（键位、粘贴判定）继续以参数接收平台，单测在任何主机上都能同时覆盖 macOS 与 Windows 分支（现有 `resolveTerminalKeyAction` 已是这种写法）。
- 修正文件头注释：规则是「只经本模块读平台」，不是「业务组件不读平台」（终端与快捷键已在合理地使用）。

**C6 终端链接识别：识别只找候选，归属交给解析层（两个 xterm 封装共用）**
- 根因：识别层自己拼了一份「工作区根」规则，与 `resolve-link.ts` 的规则不一致（F1）。改为：识别层只按通用形态找**绝对路径候选**（POSIX `/…`、Windows `X:\…` 与 `X:/…`，可带 `:行[:列]`），是否属于工作区、大小写与分隔符怎么比，**全部交给 `resolveLink`**——AGENTS.md 规定的「解析规则只在 `lib/links/resolve-link.ts`」由此落实，识别层不再知道根目录长什么样。
- 折行与宽字符：按 `isWrapped` 拼接相邻行再匹配；匹配下标经 `IBufferLine.getCell(x).getWidth()` 换算成单元格列。均为 xterm 公开 API（`WebLinkProvider` 的同类实现是私有的，不能直接用）。
- 放置：`components/terminal/terminal-links.ts`（需要 `Terminal` 实例，归 components；不放 `lib/links/`，那里只放解析）。底部终端与卡片都调用它注册网页链接与路径链接，`TerminalView` 里原有的私有实现与 `escapeRegExp` 删除。
- 卡片接入：`TerminalResultBody` 用 `useOpenLink("tool", context.taskId)` 拿打开回调与工作区根，经 `TerminalViewport` 以 ref 交给视图（视图只按挂载创建一次，与 `terminal-session-pane.tsx:49-64` 同一写法）。卡片同样只认绝对路径（命令可能 `cd`）。
- 顺带修正 `resolve-link.ts:118`（注释说只有盘符大小写不敏感，代码整条小写比较）与 `:156`（注释说终端文本不解码，实际会解码）两处注释。

**C4 复制：先抽组件，文本复用 xterm 自己的选区机制**
- 抽出 `components/ui/copy-icon-button.tsx`：图标按钮，成功变对勾 1.5 秒，不弹 toast，失败记 WARNING。回合脚注改用它（删掉内联实现，三处同一写法）。
- 复制命令：取 `data.command`，回退 `args.command`。
- 复制输出：卡片视图新增 `copyText()`，内部用 `selectAll()` + `getSelection()` + `clearSelection()`，去掉末尾空行。理由：xterm 自己的选区拼接已经处理折行、NBSP，并在 Windows 上用 `\r\n`（`SelectionService.ts:227-259`），不再手写第二份拼接与换行规则；拿到的是渲染后的文本，没有 ANSI 与 `\r` 进度帧。
- 呈现：展开体加具名组 `group/terminal-body`，两个按钮默认透明，悬停或键盘聚焦时显示；后台看板（`interactive=false`）照常显示，复制是只读操作。

**C5 丢行提示：用 marker 判断，不靠估算**
- 卡片视图在缓冲第 0 行注册 marker，行被裁掉时 marker 被销毁，即确实丢了输出（公开 API，`xterm.d.ts:483-509`）；`reset()` 后重新注册。视图经回调告诉 React，`TerminalResultBody` 在 `$ command` 行与视口之间显示一行灰字「较早的输出未显示」（不写「5000 行」：xterm 按显示行计，含折行，数字不准）。

**A 交接标签标记（第一阶段只做这一项）**
- 标签栏读 `useHandoffStore` 的整组交接（照浏览器面板 `browser-panel.tsx:53-54` 的写法），交接中的会话把状态点换成手形图标。第二阶段做「Agent 使用中」时，同一个位置按「交接 > Agent 使用中 > 状态点」取一个，创建者图标退到 tooltip——第一阶段先不动创建者图标，避免两次改动同一处。

**B1 右键菜单：两个平台一致**
- 用 radix `ContextMenu` 与 `menu-tokens.ts`，菜单项：复制（无选区时禁用）、粘贴（断线时禁用）、查找。`TerminalView` 公开 `hasSelection` / `copySelection` / `paste`（今天是私有方法），查找沿用 `onOpenSearch`。
- 平台：macOS 保留 xterm 默认的「右键先选中词」再弹菜单（与 VS Code 一致）；Windows 也弹菜单，不采用 Windows Terminal / VS Code 的「右键即复制 / 粘贴」——命令来自模型时误粘贴即执行的风险更高，且两个平台行为一致更好学。若你更想对齐 Windows 惯例，这是唯一需要改的取舍。
- 不做键盘唤起（Shift+F10 / 菜单键在终端里属于 shell）。开了鼠标上报的程序（vim、tmux）也会收到这次右键，属已知行为，不处理。

**B2 粘贴确认：按模式判断，上限覆盖所有路径**
- 判定：含换行，且 `terminal.modes.bracketedPasteMode` 为假，才确认（对齐 VS Code `auto`）；去掉字数阈值。文案改为实话：「将粘贴 N 行，每行都会被执行」。
- 256 KiB 上限对**所有**粘贴路径生效：原生 paste 事件里先查上限，超限一律拦截提示（F3）。
- 断线时粘贴给出提示，不再静默丢弃（F4）。
- 跨平台效果：macOS 的 zsh / 新版 bash 开启 bracketed paste，多行粘贴不再打扰；macOS 自带 bash 3.2、Windows 的 PSReadLine 与 cmd 大概率不开，多行粘贴照常确认——这正是会逐行执行的情形，与 Windows Terminal 默认的多行警告一致（Windows 待真机验证 `bracketedPasteMode` 的实际值）。
- 顺带修正 `terminal-keyboard.ts:41-46` 那段放错位置的阈值注释。

**B3 编辑键：只补 macOS 缺的，Windows 已原生可用**
- `resolveTerminalKeyAction` 新增「发送序列」动作，仅 macOS：Option+←/→ → `ESC b` / `ESC f`，Cmd+←/→ → `^A` / `^E`，Cmd+Backspace → `^U`。`handleKey` 对 keyup / keypress 同样返回 false。
- Windows 不加映射：xterm 已把 Ctrl+←/→、Home/End、Ctrl+Delete 编成标准 VT 序列，ConPTY 翻成按键后 PSReadLine 与 cmd 都按词移动、到行首行尾（待真机验证）。为 Windows 再造一层映射反而会和 PSReadLine 自身的键表冲突。
- `macOptionIsMeta` 保持 false（打开会让欧洲键盘打不出 `@ { [`）。

**B4 查找条**
- 工作台每次打开查找递增 `focusKey` 传给 `FindBar`（与 `text-find.tsx` 同一写法）；重新打开时不再把结果清成「无结果」而保留高亮的错位状态。

**B5 写入积压与 reset 时机：一处规则覆盖两种 resync**
- 视图用 `write(data, callback)` 统计待写字节；超过上限（数 MB，远低于 xterm 的 50 MB 抛错线）时停止写入新输出，等写缓冲排空后再 `reset()` 并请求 resync。
- **服务端触发的 resync 也走同一条「排空后再 reset」**，消除 F2 的竞态。`TerminalConnection` 把私有的 `resync` 公开为请求入口。
- 不再给 `write` 包 try/catch：上限远低于抛错线，抛错已不可能发生（不层层防御）。

**W1 Windows 的 ConPTY 选项**
- 宿主是 Windows 时，底部终端传 `windowsPty: { backend: "conpty" }`，让 xterm 启用 ConPTY 的换行与重排处理（F6）。只在底部终端：卡片的输出不经过 PTY。待真机验证。

##### 刻意不做（第一阶段）

- F7 win32-input-mode：需要 xterm 支持或关掉 ConPTY 的这个标志，影响面在 worker，单独评估。
- Windows 上 Ctrl+Alt+B/R 应用快捷键在终端里失效：Ctrl+Alt 在很多 Windows 键盘布局上就是 AltGr，让给终端是对的，不改。
- 查找条在焦点回到终端后按 Esc 不关闭：Esc 属于 shell。

##### 影响范围与测试

- 涉及文件：`lib/platform.ts`、`lib/terminal/terminal-keyboard.ts`、`lib/terminal/terminal-connection.ts`、`components/terminal/`（`terminal-view.ts`、`terminal-workbench.tsx`、`terminal-tab-bar.tsx`、`terminal-menus.tsx`、新增 `terminal-links.ts`）、`components/assistant-ui/tools/`（`terminal-output-view.ts`、`terminal-viewport.tsx`、`views/terminal-result-view.tsx`）、`components/ui/copy-icon-button.tsx`（新增）、`components/assistant-ui/turn/turn-footer.tsx`、`lib/links/resolve-link.ts`（只改注释）。不涉及后端与契约。
- 单测：键位（两个平台的新增序列）、粘贴判定（模式 × 换行 × 上限）、链接识别（POSIX / `C:\` / `C:/` / 小写盘符 / 折行 / 宽字符 / 工作区外不识别，用真实 `@xterm/xterm` 不 open 即可解析，同 `terminal-query-replies.test.ts`）、连接的 resync 入口、卡片视图 mock 扩充、`call-row` 与后台看板测试补 `QueryClientProvider`。
- E2E：查找条重复按 Ctrl/Cmd+F 重新聚焦；右键菜单出现与禁用态；卡片复制按钮悬停可见。剪贴板内容不断言（Playwright 未授予剪贴板权限）。
- Windows 真机验证清单（已记入 AGENTS.md 的 Windows 待办）：PSReadLine / cmd 的 `bracketedPasteMode`、Ctrl+←/→ 按词移动、`windowsPty` 生效后的换行、`C:\` 路径可点击、复制输出的换行。

##### 实施记录（2026-11-03，未提交）

已按上述方案实施，与方案的出入：

- W1：xterm 只有在同时给出 `buildNumber` 且低于 21376 时才启用 ConPTY 换行启发式；只给 `backend: "conpty"` 时生效的是「面板变高时不从滚动区拉回旧行」（ConPTY 自己重绘屏幕，否则同一段输出会出现两次），重排照常启用——对 Windows 10 21H2 之后的系统正是所需行为，所以不向宿主要版本号。
- C5：提示文案为「较早的输出未显示」，不写行数。
- B5：`reset()` 排进 xterm 写队列执行（`write("", cb)`），`tests/unit/xterm-write-order.test.ts` 守住这个依赖，并复现了直接调用时的竞态。
- A：交接标签标记改为读整组交接（`handoffOn`），只剩它一个调用方的 `useHandoffOn` 已删除。
- 测试：前端单测 123 个文件全部通过；新增键位（两个平台）、粘贴判定、链接识别（POSIX / `C:\` / `C:/` / 小写盘符 / 折行 / 宽字符）、写队列顺序、连接 resync、卡片丢行与复制；E2E 新增右键菜单与查找重新聚焦、卡片复制按钮悬停可见。

##### 复核结论与修复方案（2026-11-03，已实施未提交）

独立复核加自查共 13 条，按根因归并为 5 组。原则：每组只在根因处改一次；不为低概率情形加防御；能删的机制先删。

**R1 积压后的重新同步不该在旧积压之后排队（复核 1、10；中，原理推演）**
- 根因：连接收到 resync 后**立刻**重新 attach，重放排到视图写队列里还没解析的几 MB 旧输出后面；xterm 没有丢弃写队列的 API，于是「积压 → resync → 更多积压」。不是视图计数写错了，是「清屏」与「重新 attach」的先后没有约定。
- 改法：`onReset` 改为 `onReset(resume)`——视图把清屏排进写队列，执行时调用 `resume`，连接这时才 attach。写队列因此最多是「积压上限 + 一份重放（≤ 1 MiB）」。`resume` 时若已经有连接（期间用户点了重新连接）就不再连，一个判断即可，不加代次计数。
- 顺带删：`resync()` 里 `socket === null` 的分支走不到，删掉。
- 不做：给 `write` 包 try/catch——上限远低于 xterm 的 50 MB 抛错线，抛错已不可能。

**R2 终端路径识别以工作区根目录为锚（复核 2、5、7、8；中，已复现）**
- 根因：第一阶段让识别层按「通用绝对路径形态」找候选，形态里不允许空格；而能点的只有工作区内的路径。根目录本身带空格（Windows 用户名常见）时一个都认不出，相对旧实现是回退。
- 改法：识别层在文本里找**工作区根目录**再向后延伸。「根目录在文本里长什么样」（大小写不敏感、`/` 与 `\` 等价）放进 `lib/links/resolve-link.ts`，与 `toWorkspacePath` 的比较口径写在一起，导出一个按根目录生成的匹配式；识别层只用它锚定，最终归属仍由 `resolveLink` 判定——比较规则只有一处，不在识别层另写一份。
- 同一处顺带：路径里允许 `[ ]` `( )`（Next.js 的 `[slug]`、`(group)`），末尾不成对的右括号与句点剥掉；交给 `resolveLink` 前把 `%`、`#` 转义（终端文本不是 URL，否则 `F#/x.cs` 被当成片段）；折行拼接限在当前行附近约 2048 个字符（与 `WebLinksAddon` 相同），超长压缩行悬停不卡。
- 补测：根目录带空格、`[slug]` / `(group)`、`#`、括号收尾。

**R3 丢行提示改用容量判断，删掉 marker（复核 3；低到中，已复现）**
- 根因：第一阶段用「第 0 行 marker 被销毁」当丢行信号，但清屏序列（`clear`、`cls`）也会销毁它，信号本身不成立。补救（销毁时再判断、重挂 marker）要多维护一套生命周期。
- 改法：删掉 marker；`layout()` 本来就在每次写入后执行，在那里看缓冲是否已到容量（`buffer.normal.length >= rows + scrollback`）。唯一的偏差是「恰好写满还没裁」那一行，可以接受。清屏重放后行数回落，提示自然消失。

**R4 改发的按键与 Agent 输入标记走 xterm 自己的路径（复核 4、6；中 / 低）**
- 同一个根因：绕开了 xterm 的顺序或输入路径。
- Agent 输入标记排进写队列再标（与 R1 的清屏同一写法），否则会标在还没解析完的旧光标行上。
- macOS 编辑键改用 `terminal.input(序列, true)`：与普通按键同走 `onData`，会话结束时同样被 `disableStdin` 拦下、翻看回滚区时回到底部；删掉视图里另一条直接发送的路径。

**R5 小修（复核 9、11-13；低）**
- 前台程序开了鼠标上报（htop、tmux、`mouse=a` 的 vim）时右键归它，不弹菜单：在右键事件里读 `terminal.modes.mouseTrackingMode`。
- 没有可见会话时右键菜单不接管。
- 复制按钮取到空文本时不写剪贴板、不显示「已复制」（否则会清空用户的剪贴板）。
- 粘贴确认在内容全是换行时不显示「共 0 行」；Cmd+Backspace 的注释按实际行为写（bash 删到行首、zsh 默认删整行）。

**刻意不做**
- Windows 10 上 `windowsPty` 缺版本号（复核 5）：要让宿主上报系统构建号，改宿主契约；Windows 尚未实测，记入 AGENTS.md 的 Windows 待办「终端交互」一条。
- geometry 帧的顺序：真实终端的 resize 同样是立即生效，不改。
- 备用屏幕（vim、nano）下不翻译编辑键：与 VS Code 一致，不改。
- 为 `TerminalView` 搭 DOM 单测框架：代价与收益不相称，行为由 E2E 与纯函数单测覆盖。

**影响范围**：`lib/terminal/terminal-connection.ts`（`onReset` 签名，唯一调用方是 `TerminalView`）、`components/terminal/terminal-view.ts`、`components/terminal/terminal-links.ts`、`lib/links/resolve-link.ts`（新增导出）、`components/assistant-ui/tools/terminal-output-view.ts`（删 marker）、`components/terminal/terminal-menus.tsx`、`components/terminal/terminal-workbench.tsx`、`components/ui/copy-icon-button.tsx`、`lib/terminal/terminal-keyboard.ts`（注释）；不涉及后端与契约。测试：连接（`resume` 时机）、链接识别（R2 的新用例）、卡片视图（容量判断替代 marker 的用例）。

**实施结果**：R1-R5 按上述方案实施；R2 的根目录匹配式落在 `resolve-link.ts` 的 `workspaceRootPattern`。前端单测 123 个文件、829 个用例与终端相关 E2E 29 个全部通过；新增用例：连接在 `resume` 前不重连、根目录带空格、`[slug]` / `(group)`、括号收尾、`#` 与 `%`、卡片按容量判断丢行。

### 7.5 两个终端统一：用户命令进程收进 worker（2026-11-03，已完成）

已独立成文：[`terminal-process-unification-design.md`](terminal-process-unification-design.md)。内容包括：命令结束后清理残留进程、两个工具从 Agent 角度的边界、统一到 Rust worker（不改用 Python）、沙箱接入点与 sandbox-runtime 的关系、分阶段实施。它取代 §7.2 方案 A 里 T5 的部分。

### 7.6 其余待决问题

（无）

## 8. 附：调查中发现的注释漂移（已修正，2026-11-02）

- `apps/desktop/components/terminal/xterm-appearance.ts:6` 原写「在 canvas 上绘制」，实际为 DOM 渲染器；改为「只经构造选项以 JS 值传入」。
- `apps/desktop/components/terminal/terminal-view.ts:24` 的「输入租约」、`apps/desktop/components/terminal/use-terminal-session-actions.ts:15` 的「接管 Agent 输入」、`apps/desktop/lib/terminal/terminal-keyboard.ts:6-7` 的「后端租约的 Ctrl-C 例外」与交互契约「没有租约」矛盾，也没有对应功能；已按契约改写或删除。
- `apps/desktop/lib/terminal/terminal-connection.ts:99`、`apps/desktop/lib/terminal/terminal-sessions.ts:100` 提到的「⋮ 菜单」已不存在；改为状态栏「重新连接」与页头「已结束的终端」菜单。

`apps/terminal-worker/src/shell_integration/mod.rs:75-77` 的 `.zprofile` / `.zlogin` 包装不是注释而是代码：zsh 非 login 启动时不会执行。删掉还是让它生效取决于 T1 的 login 语义决定，随 P0 处理。

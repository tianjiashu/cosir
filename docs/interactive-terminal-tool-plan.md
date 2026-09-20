# 可交互终端 Tool 设计方案

状态：绿地项目设计基线，供后续实现和验收使用。

本方案只针对单用户、本机运行的桌面 Agent。它解决两个不同问题：

1. Agent 如何使用一个真实的可交互终端；
2. 用户如何在前端打开一个只读面板，观察 Agent 的终端操作。

本方案明确不允许用户通过该面板输入、发送信号或关闭终端，也不允许打开本地可见的 CMD、PowerShell、Terminal.app、xterm 等窗口。

本次是绿地实现，不做旧协议兼容层，不保留两套交互终端实现，不新增数据库表。

## 1. 设计结论

保留现有一次性命令工具 `execute_terminal`，新增正式的交互式终端 session 工具族。两者复用终端执行底座，但拥有不同的生命周期和 UI 契约。

```text
execute_terminal
  一条命令 → 等待进程退出 → 返回最终输出和 exit code

terminal_start
  创建隐藏 PTY/ConPTY shell session
terminal_write/read/signal/close
  Agent 驱动同一个 session 完成交互
```

总体进程边界：

```text
Tauri Rust 主进程
  └─ 管理 FastAPI backend 生命周期
       └─ FastAPI backend
            ├─ Agent Runtime / ToolExecutor
            ├─ TerminalSessionService
            └─ 隐藏的 terminal-worker 子进程
                 └─ PTY / Windows ConPTY
                      └─ shell 子进程

React WebView
  ├─ AssistantRuntimeProvider
  ├─ Assistant UI Tool renderer
  └─ 只读 TerminalPanel
```

核心原则：

- backend 拥有终端 session、权限、生命周期、进程树和输出游标；
- terminal-worker 只拥有 PTY/ConPTY 和 shell 进程，不拥有 Task、Run、SQLite 或 Assistant snapshot；
- React 只负责展示，不创建、写入、停止或重启终端；
- 交互式 session 的完整 PTY 原始字节流不进入 Assistant Transport；现有一次性 `execute_terminal` 的既有运行中 output 投影仍保持原契约，不在本次方案中删除或改义；
- TerminalPanel 使用独立的只读预览数据面，通过 `session_id + output_seq` 重新连接；
- 不把终端输出复制成大量 Assistant message，也不把终端流写入 conversation snapshot。

## 2. 当前代码事实

### 2.1 后端已有能力

当前仓库已经具备交互终端的主要基础：

- `apps/backend/app/service/terminal/terminal_session_service.py`：session 用例、归属校验、生命周期、输出 cursor 和 subscriber 管理；
- `apps/backend/app/service/terminal/worker.py`：backend 与本机 terminal-worker 的控制边界；
- `apps/terminal-worker/`：Rust `portable-pty` worker，Windows 使用 ConPTY，POSIX 使用 PTY；
- `apps/backend/app/api/terminal_api.py`：终端 session API/只读预览边界；
- `apps/backend/app/core/tools/tool_handler/terminal_session/`：`terminal_start`、`terminal_read`、`terminal_write`、`terminal_signal`、`terminal_close` handler；
- `apps/backend/app/core/tools/tool_models/terminal_session_args.py`：交互终端参数模型；
- `apps/backend/app/storage/model/terminal_session_model.py` 与对应 CRUD：现有 `terminal_sessions` 元数据表；
- `apps/backend/app/core/tools/tool_ui_display_contract.md`：已约定交互 session 使用 `kind: terminal-session`，并复用 terminal layout；
- `apps/desktop/components/assistant-ui/tools/terminal-session.ts`、`terminal-write-scheduler.ts`、`terminal-output-reconciler.ts`、`terminal-viewport.tsx`：一次性终端的 xterm 渲染和控制字符处理基础。

当前限制是：交互式 handler 仍处于 hidden 状态，没有加入 `ToolSystem`，因此 Agent 当前不可见；前端已有一次性终端 renderer，但还没有 session attach 的任务级只读面板。

当前一次性 `TerminalSession` 的输入是后端累计文本 snapshot，并依赖 snapshot 前缀比较；交互式 preview 则是 `Uint8Array` 原始 output frame。两者应复用 xterm 初始化、尺寸、生命周期和错误处理，但不能把 raw byte frame 直接塞进一次性 snapshot reconciler。交互式面板需要单独的 append-bytes consumer；一次性终端继续使用累计 snapshot consumer。

### 2.2 一次性终端不能直接改造成 session

现有 `execute_terminal` 使用“启动命令、读取输出、等待退出”的一次性模型。它适合 `git status`、`git diff`、`npm install`、`pytest`、`git fetch`、编译、测试和查询类命令。

交互式 session 需要持续存在的 shell、多次 input/output 往返、PTY/ConPTY 的光标/颜色/回车/全屏/REPL 语义，以及独立的 session owner、generation、output sequence 和清理流程。

因此不能在 `execute_terminal` 中继续堆叠 `interactive=true`、`session_id`、`stdin` 等分支。正确做法是保留一次性工具，复用底层的 workspace、权限、取消、日志和 terminal worker 边界。

## 3. 数据和持久化边界

### 3.1 不新增数据库表

本方案不新增数据库表，不增加 session output 表，不增加 operation 表，不增加 terminal history 表。

直接复用当前已有的 `terminal_sessions` 元数据表，用于：

- `session_id`、`task_id`、`workspace_id`；
- shell 类型和 executable；
- 初始 cwd；
- status、exit code、end reason；
- worker instance/pid 诊断信息；
- cols/rows 和时间字段。

表中不保存 PTY handle、worker connection、完整终端输出、subscriber、UI 的 xterm 屏幕状态或 Agent 的终端输入历史。

输出 ring buffer、subscriber、generation、cursor 和活跃 worker handle 只存在当前 backend 进程内。backend 重启后，遗留 active session 收敛为 `interrupted`，不重放旧 shell、不恢复旧 Agent 操作。

`worker_instance_id` 是现有元数据表中的诊断 identity；`generation` 是当前 backend 进程内用于拒绝旧 callback/event 的运行时 fencing 值。两者相关但不等价：持久化 identity 不能替代每次 worker attach 的 generation 校验。

### 3.2 事实所有权

```text
terminal_sessions 表
  session 元数据和最终生命周期状态

TerminalSessionService / process registry
  当前 worker、ring buffer、subscriber、generation、cursor

Assistant Transport snapshot
  工具调用状态和有限 terminal-session identity

Terminal preview stream
  原始 PTY bytes、output seq、status、resync 事件

React state / xterm
  当前窗口的渲染缓存，不是事实源
```

前端关闭面板只是取消预览订阅，不修改 session 状态。只有 Agent 的 `terminal_close`、backend 生命周期、worker 失败或明确的 session 生命周期策略可以关闭终端。

## 4. Agent 如何使用

### 4.1 工具族

内部保持五个职责单一的 handler，并在完成真实 worker 和跨平台验收后注册到 Agent：

| Tool | Agent 用途 | 是否改变 session |
| --- | --- | --- |
| `terminal_start` | 创建隐藏 shell session | 是 |
| `terminal_write` | 写入命令、回答交互提示 | 是 |
| `terminal_read` | 按 cursor 读取新增输出 | 否 |
| `terminal_signal` | 发送 interrupt、EOF、suspend | 是 |
| `terminal_close` | 关闭 shell 和进程树 | 是 |

前端不会调用这些 Tool。它只消费已经创建的 session 预览流。

### 4.2 典型调用流程

```text
terminal_start(shell="auto", cwd=".")
  → 返回 session_id、platform、shell、cwd、status、next_seq

terminal_write(session_id, data="npm install\n", after_seq=0, wait_ms=500)
  → 返回新增输出、next_seq、status

terminal_read(session_id, after_seq=next_seq, wait_ms=1000)
  → 等待后续输出或状态变化

terminal_write(session_id, data="y\n", ...)
  → 仅在输出明确要求输入时使用

terminal_signal(session_id, signal="interrupt")
  → 需要中断长时间命令时使用

terminal_close(session_id)
  → Agent 完成交互后显式关闭
```

`terminal_write` 可以等待一个短窗口并返回新增输出，避免 Agent 每个字符都调用 `terminal_read`。长时间命令通过多次 `terminal_read` 观察；`wait_ms` 只表示本次工具调用等待新增输出的上限，不表示等待 shell 退出。

### 4.3 参数和可靠性要求

所有操作都必须携带 `session_id`，由 backend 校验 Task、Workspace 和当前 session 的归属。

`terminal_write` 应增加进程内的 `operation_id`/`command_id` 去重语义。该字段必须进入工具参数或统一执行 envelope：同一个 session 内同一个 id 搭配相同 payload 只允许写入一次并返回既有结果；相同 id 搭配不同 payload 必须拒绝。去重状态只保存在当前 session actor 内，不新增数据库表；backend 重启后 session 已经 interrupted，不自动重试旧写入；前端没有写入权限，因此不需要为 UI 设计写入幂等协议。

输出读取使用从 `1` 开始的单调递增 `output_seq`：`after_seq=0` 或 `null` 表示尚未应用任何 frame；`after_seq` 表示调用方已经完整应用的最后一个序号；返回 `seq > after_seq` 的增量和 `next_seq`；ring buffer 无法覆盖 cursor 时返回 `resync_required`，禁止静默拼接不完整输出；不根据文本内容猜测重复或增量。

## 5. 根据操作系统生成 Tool 描述

Tool 名称和参数结构保持统一，不拆成 `execute_cmd`、`execute_bash`、`execute_powershell` 等多个工具。backend 启动时根据 `platform.system()` 和现有 `ShellResolver` 生成描述，运行时把可用 shell 和路径语法告诉 Agent。

### Windows 描述

```text
Start a hidden interactive terminal session on Windows.
The session uses ConPTY and does not open CMD, PowerShell, Windows Terminal,
or any visible console window. The default shell is the configured Windows
shell, usually PowerShell under the current resolver. Use Windows paths and
command syntax. Use terminal_write for input. Use terminal_signal only when
the worker capabilities explicitly advertise the requested signal; the
current Windows worker does not advertise signal control. The frontend only
previews output.
```

### macOS 描述

```text
Start a hidden interactive terminal session on macOS.
The session uses a local PTY and does not open Terminal.app, iTerm, or any
visible terminal window. The default shell is usually zsh. Use POSIX paths
and shell syntax. Use terminal_write for input and terminal_signal with
interrupt for Ctrl+C-like interruption. The frontend only previews output.
```

### Linux 描述

```text
Start a hidden interactive terminal session on Linux.
The session uses a local PTY and does not open gnome-terminal, xterm,
Konsole, or any visible terminal window. The default shell is usually bash
or sh. Use POSIX paths and shell syntax. Use terminal_write for input and
terminal_signal with interrupt for Ctrl+C-like interruption. The frontend
only previews output.
```

描述的来源应该是 `ShellResolver` 的实际解析结果和 worker handshake capabilities，而不是硬编码一个假设。当前代码事实是：Windows 的 `auto` 解析为 `powershell.exe`，不会自动优先 `pwsh`；POSIX 的 `auto` 优先 `$SHELL`，缺失时 macOS 回退 zsh、Linux 回退 bash。若后续改变 resolver 优先级，必须同步更新描述测试。显式 shell 只能选择已解析且允许的 executable。

Tool 描述还必须按 worker capabilities 声明可用信号。当前 worker handshake 在 Unix 提供 `signal_interrupt`、`signal_suspend`、`signal_eof_canonical`，Windows 当前只报告 `pty_io` 和 `terminal_query_dsr`；因此第一版 Windows 描述不能承诺 Agent 可用 `terminal_signal` 的所有信号。未支持的 signal 必须返回稳定的 unsupported 错误，不能伪装成成功。

## 6. 不打开本地终端窗口

“交互终端”指后台 PTY/ConPTY，不指 GUI 终端应用。

### Windows

- terminal-worker 使用 ConPTY；
- worker 以无控制台方式启动；
- shell 连接到 pseudo console，不创建新的可见 console window；
- 禁止通过 `start`、`wt` 或 GUI shell 命令启动外部终端窗口；
- Windows Job Object/进程树清理由 worker/backend 负责。

### macOS/Linux

- worker 使用 Unix PTY；
- shell 是 worker 的子进程，不启动 Terminal.app、iTerm、gnome-terminal、xterm 或 Konsole；
- shell 进程组和 worker owner pipe 用于异常退出清理。

当前 `apps/terminal-worker/` 和 `apps/backend/app/service/terminal/worker.py` 已经是这个方向：PTY/ConPTY 在本机隐藏 worker 中运行，React/Tauri 不直接创建 shell。

## 7. Backend 改造范围

### 7.1 需要改造的模块

1. **Tool 注册**：在真实 worker、跨平台和生命周期测试通过后，把现有 hidden handler 注册进 `ToolSystem`。不要新建第二套交互工具执行器。
2. **Tool 描述**：把静态描述拆成稳定公共部分和平台运行时部分；描述由 shell resolver 的实际结果生成。
3. **session service**：继续复用 `TerminalSessionService`、已有 `terminal_sessions` 表、进程内 registry、ring buffer 和 Task/Workspace/session 归属校验。Tool handler 已经有 `workspace_id` 上下文但后续 service API 当前主要以 `task_id` 校验；正式注册前必须在 service 边界交叉验证 `context.workspace_id` 与 session 的 `workspace_id`，不需要把 workspace_id 暴露给模型参数。不要新增表。
4. **worker 生命周期**：backend 是 terminal-worker 的唯一 owner。启动、heartbeat、EOF、正常 close、强制清理和 backend crash recovery 都必须由 backend/worker 边界完成。
5. **只读预览 API**：保持独立的本机 preview stream。浏览器连接只允许 attach 和 cursor，不接受 input、signal、close、resize 消息。
6. **Assistant Transport 投影**：`terminal_start` 的 `display_data` 使用有限的 `kind: terminal-session`，不把完整 PTY 输出放进 display data、conversation snapshot 或数据库。

建议的 display data：

```json
{
  "kind": "terminal-session",
  "session_id": "term_xxx",
  "platform": "win32",
  "shell_kind": "powershell",
  "initial_cwd": "C:\\project",
  "status": "running",
  "generation": "gen_7",
  "first_available_seq": 1,
  "next_seq": 12,
  "read_only": true
}
```

`next_seq` 是下一个将分配的序号，不是最后一个已应用序号；`first_available_seq` 是当前 ring buffer 可重放的最小序号；前端自己的 `last_applied_seq` 只存在 terminal store，不写回工具 payload。`platform`、`shell_kind`、`initial_cwd` 是后端稳定投影字段，不能让前端根据工具名或命令字符串猜测。

### 7.2 Backend 不负责的事情

- 不把 assistant-ui 类型带入 service、worker、storage；
- 不让 React 通过 Tauri command 直接控制 shell；
- 不让 preview WebSocket 成为第二套 Agent 控制 API；
- 不因 WebSocket 断开而取消 session；
- 不因前端重新打开面板而重放 Agent 操作；
- 不把日志中的完整命令输出、凭据或大段模型正文写入结构化日志。

## 8. Frontend 改造范围

### 8.1 Tool 卡片

交互式 Tool renderer 继续由现有 `tool-part.tsx` 按 `data.kind` 和 `expand_layout` 路由，不按工具名增加散落的专用分支。实现时必须把 `terminal-session` 加入 `KNOWN_DISPLAY_KINDS`，并明确路由到 terminal renderer；在 allowlist 未更新前不能宣称 renderer 已接通，未知 kind 只能走 fallback。

Tool 卡片只展示摘要：

```text
终端会话 · PowerShell · C:\project · 运行中
[打开终端]
```

完成后展示：

```text
终端会话 · PowerShell · C:\project · exit 0 · 已完成
[打开终端]
```

“打开终端”只打开或聚焦 Task 级 TerminalPanel，不创建 session，不执行命令。

### 8.2 只读 TerminalPanel

建议新增清晰的前端职责目录：

```text
apps/desktop/components/terminal/
  terminal-panel.tsx          # 面板布局和只读状态
  terminal-connection.ts      # preview WebSocket / attach / reconnect
  terminal-session-store.ts   # Task 级 session 订阅和当前面板状态
  terminal-types.ts           # preview-v1 协议类型
```

它们复用当前 xterm 初始化、尺寸适配、生命周期清理和 `TerminalViewport` 的视觉容器；一次性输出的 `TerminalWriteScheduler`、`TerminalOutputReconciler` 不能直接处理 raw byte frame。交互式面板需要一个按 `seq` 排序、去重并调用 `xterm.write(Uint8Array, callback)` 的 append consumer；两种 consumer 共用底层 session/dispose 逻辑，但保持输入契约分离。

面板功能：shell、平台、cwd、运行状态、运行时长、exit code；ANSI/VT 原始输出由 xterm 解释；自动滚动；用户滚动离开底部时暂停跟随；新输出 unread 提示和“回到底部”；选择、复制、搜索；可选的输出下载；`resync_required` 时清空并按协议允许的 replay 重建；`read_only` 明确标识。复制和下载都是用户主动操作，仅保留在本机 UI，不写日志、Assistant snapshot 或新增历史表。

面板禁止：

- xterm `onData`；
- 键盘输入或粘贴写入；
- Ctrl+C、Ctrl+D、signal、kill、close session 按钮；
- 通过 ResizeObserver 反向修改 PTY cols/rows；
- 直接调用 terminal Tool；
- 直接创建或停止进程。

面板关闭只取消 WebSocket 订阅。Agent 仍可继续操作 session，用户重新打开时按照最后 cursor attach。一个 Task 可以存在多个 session，terminal store 的 key 必须是 `task_id + session_id`；第一版可以只显示当前选中的 session，不能把多个 session 的 output 混到同一 xterm。

### 8.3 Assistant UI 的使用边界

assistant-ui 的职责是：在现有 `AssistantRuntimeProvider` 下渲染工具调用，读取工具 renderer 的 `args`、`result` 和 `status`，显示 running、completed、failed、cancelled 等状态，并由工具 renderer 产生“打开预览”的 UI 事件。

assistant-ui 不负责创建 backend session、管理 PTY/ConPTY、保存完整终端输出、处理 terminal preview WebSocket，也不替代项目已有的 session service 或 transport。

官方文档依据：

- [Tool UI](https://www.assistant-ui.com/docs/tools/tool-ui)：backend tool 可以只注册 UI renderer，renderer 应根据工具调用状态显示运行中、结果和错误；assistant-ui 的 `status.type` 使用 `running`、`complete`、`incomplete` 等值；
- [Assistant Transport](https://www.assistant-ui.com/docs/runtimes/custom/assistant-transport)：它是 Agent state snapshot 和 command 的传输协议，适合对话/Run 状态，不适合承载无限增长的 PTY 字节流；
- [Runtime architecture](https://www.assistant-ui.com/docs/runtimes/concepts/architecture)：`AssistantTransport` 建立在 `ExternalStoreRuntime` 上，前端是 Agent state 的视图；
- [AssistantRuntimeProvider](https://www.assistant-ui.com/docs/api-reference/context-providers/assistant-runtime-provider)：继续使用现有根 Provider，不为终端建立第二个 runtime。

assistant-ui 状态和项目 Transport 状态必须显式映射，不能混用名称：

```text
项目 backendStatus=pending/running  → assistant-ui status.type=running
项目 backendStatus=completed       → assistant-ui status.type=complete
项目 backendStatus=failed          → assistant-ui status.type=incomplete, reason=error
项目 backendStatus=cancelled       → assistant-ui status.type=incomplete, reason=cancelled
```

项目的 `backendStatus`/`ToolObservation.status` 仍是生命周期唯一事实源；assistant-ui `status` 只是 renderer 适配层。`display_data.status_hint` 只提供受控短提示，不能反向建立第二套状态机。

`pending`/`unknown` 等项目内部状态在 renderer 中按非终态 loading/fallback 处理；交互终端不使用 assistant-ui 的 `requires-action`，因为用户没有输入或审批控制面。若未来工具权限审批接入，应沿项目现有 approval/permission UI 单独处理，不能把 TerminalPanel 变成审批或控制器。

## 9. Terminal preview stream

Assistant Transport 保留对话和工具状态；终端预览使用独立的本机只读 WebSocket/stream。不要把终端输出改造成 Assistant message。

建议协议版本：`terminal-preview-v1`。

```text
WebSocket URL:
/tasks/{task_id}/terminal/sessions/{session_id}/stream?trace_id=...
```

首帧只能是：

```json
{"type":"attach","after_seq":123}
```

服务端消息：

```json
{"protocol":"terminal-preview-v1","type":"attached","generation":"gen_7","first_available_seq":120,"next_seq":124,"status":"running"}
{"protocol":"terminal-preview-v1","type":"output","generation":"gen_7","seq":124,"data_base64":"..."}
{"protocol":"terminal-preview-v1","type":"status","generation":"gen_7","status":"running"}
{"protocol":"terminal-preview-v1","type":"exit","generation":"gen_7","status":"exited","exit_code":0}
{"protocol":"terminal-preview-v1","type":"resync_required","generation":"gen_7","after_seq":10,"first_available_seq":120,"next_seq":124}
```

输出数据在连接层按 bytes/base64 传输，前端解码为 `Uint8Array` 后直接交给 xterm；不要把每个 chunk 当成普通文本行。这样可以保留 `\r`、ANSI 光标移动、擦除、颜色和全屏程序语义，避免 `<pre>` 平铺控制字符导致重复和乱序。

连接要求：

- attach 前校验 Task/session 归属，并通过 session 元数据交叉确认对应 workspace；Tool 控制面和 preview 面使用同一 Task/Workspace/session 归属规则；
- 注册 subscriber 后建立 replay 边界，保证 replay 期间不漏 output；
- output 必须按 seq 升序发送；
- subscriber 队列有界，慢客户端出现 gap 时发送 `resync_required` 并断开；
- WebSocket 断开不关闭 session；
- 不接受 input、signal、close、resize 消息；
- 使用运行时 backend 地址，不硬编码端口；
- 复用项目 trace/logging 规则，不记录完整原始 output。

所有 `resync_required` 事件都必须携带当前 session generation；当前 API 在 stale cursor 错误路径中可能返回 `generation: null`，正式实现时应改为当前 generation。`null` 只能表示 session 尚未成功启动、没有可用 generation 的启动失败，不得用于已经运行过的 session。

当前代码的 ring buffer 只保存原始 output frame，不保存 VT 屏幕快照或 parser state。因此 `resync_required` 的第一版语义必须诚实限定为“从仍可用的完整 frame 起点重新 replay”；如果 gap 落在半截 escape sequence，或程序是全屏/光标密集型程序，不能宣称能够恢复精确屏幕。UI 应重建 xterm 并显示“终端输出需要重新同步”的受控提示。若产品以后要求全屏程序在 gap 后精确恢复，应增加 backend 进程内的 VT/screen snapshot（不新增数据库表），并将 snapshot 与 generation 一起纳入 resync 协议后再提高验收承诺。

generation 不能只停留在文档字段：当前 worker event 协议没有 generation，service 主要按 `session_id` 分发。正式实现前必须把 worker instance/generation 绑定到 worker callback 或事件 envelope；service 丢弃不匹配的旧事件，前端丢弃旧 generation 的 output/status，重连时以新的 `attached.generation` 建立边界。

WebSocket 的 `trace_id` 使用 query 是因为浏览器原生 WebSocket 不能设置任意请求 header；backend 应把该 query 值绑定到与 HTTP `X-Trace-Id` 相同的日志上下文语义，并限制长度、格式和敏感内容。它只用于 tracing，不是认证凭据。

## 10. 生命周期、取消和恢复

### Agent Run

- `terminal_start` 记录 `created_by_run_id`，但 session 生命周期由 session service 管理；
- `terminal_read/write` 的等待支持当前 Run cancel；
- Run cancel 默认只取消当前 Tool 等待，不自动关闭 Task 级 session；
- Agent 必须显式调用 `terminal_close`，或由 session idle/max lifetime 策略清理；
- 同一 session 的 write/signal/close 通过 session actor/mailbox 串行化。

### 面板

- 打开面板：attach；
- 关闭面板：detach；
- 页面切换：保存 cursor，返回时 attach；
- backend 断线：显示“后端不可用/正在重连”，不发送任何控制动作；
- `resync_required`：清空 xterm，重新取得可用输出；
- session 终态：保留只读内容和退出状态。

### backend/worker

- Tauri 是 backend 生命周期唯一 owner；
- backend 是 terminal-worker 生命周期唯一 owner；
- worker 监视 owner pipe/heartbeat，backend 崩溃后清理 shell 进程树；
- backend 重启不重放旧 session；
- 遗留 active session 收敛为 `interrupted`；
- 旧 generation 事件不得覆盖新状态；
- worker、shell、subscriber 的清理失败必须记录结构化日志，但不能阻塞其余 session 的关闭。

## 11. 实施顺序

### 后端改造

1. 以现有 `TerminalSessionService`、`terminal-worker`、`terminal_api` 和 `terminal_sessions` 表为基础，不增加表；
2. 补齐 `operation_id/command_id` 的进程内 write 去重；
3. 统一 session generation、output seq、resync 和 subscriber 背压；
4. 完成 Windows/macOS/Linux shell resolver 和 Tool 描述；
5. 完成 worker 无窗口启动和进程树清理验收；
6. 将现有 hidden handlers 注册到 `ToolSystem`；
7. 投影 `terminal-session` display data，不把完整 output 放进 Assistant Transport。

### 前端改造

1. 复用现有 `TerminalViewport` 及 xterm 输出调度/重建逻辑；
2. 新增 Task 级 terminal store 和只读 preview connection，并为 raw `Uint8Array` frame 增加独立的 append consumer；
3. 新增 TerminalPanel 和打开/聚焦事件；
4. 接入 attach、增量 output、status、exit、resync 和重连；
5. 在现有 `tool-part.tsx` 的 `kind + expand_layout` 路由下把 `terminal-session` 加入 allowlist 并接入 renderer；
6. 明确禁止 input、signal、close、resize；
7. 保证普通 workspace 刷新不会无故卸载正在运行的 TerminalPanel。

### 不做兼容改造

- 不保留旧交互终端协议；
- 不同时支持两套前端 TerminalPanel；
- 不为旧终端 payload 做多版本解析；
- 不新增 compatibility migration；
- 不新增数据库表；
- 不修改一次性 `execute_terminal` 的工具语义。

## 12. 开发完成后的验收

### 12.1 主 Agent 开发验收

开发 Agent 负责完成实现和第一轮验证，至少覆盖：

后端：

- handler 注册后 Agent 能看到五个交互终端 Tool；
- Windows、macOS、Linux 的 shell 描述与实际 resolver 结果和 worker capabilities 一致；
- Windows 不出现可见控制台窗口；
- `terminal_start → write → read → signal/close` 链路可用；
- Task/session 归属校验有效；
- output seq、ring buffer、resync、generation 和背压有效；
- `terminal-session` display schema 的 `first_available_seq`、`next_seq`、`generation` 语义经过测试；
- backend/worker/shell 关闭和 crash recovery 不残留进程；
- 未新增数据库表；
- 一次性 `execute_terminal` 测试不回归。

前端：

- Tool 卡片可打开任务级只读 TerminalPanel；
- `terminal-session` 已加入 renderer allowlist，不会错误落入 fallback；
- 面板只能发送 attach，不能发送 input、signal、close、resize；
- ANSI、`\r`、光标移动、擦除、颜色和进度刷新显示正确；
- 面板关闭后 session 继续运行；
- 无 gap 时断线重连使用 cursor 且按 seq 精确恢复；出现 gap 时清空 xterm、提示重新同步并按可用 frame replay，不把 gap 后的全屏/光标状态恢复宣称为精确；
- session 完成后输出和 exit code 可见；
- xterm 实例和 WebSocket 在卸载时正确释放；
- Assistant Transport 不被大量终端 output 拖慢或污染。

### 12.2 子 Agent 文档/代码复查

子 Agent 只负责复查，不负责开发，不得编辑代码或文档。复查应在主 Agent 宣布开发和测试完成后进行。

子 Agent 的输入范围：本设计文档、当前 git diff、与终端相关的前后端源码和测试、assistant-ui 官方文档对应页面、测试命令输出和验收截图（如有）。

子 Agent 的复查重点：

1. 是否清楚区分前端、backend、Tauri、terminal-worker 的进程职责；
2. 是否错误地让前端创建/控制本地终端；
3. 是否把只读面板误做成了用户可输入终端；
4. 是否把 PTY 原始字节流放入 Assistant Transport 或 React 大状态；
5. 是否有重复消费、乱序、gap、旧 generation 覆盖新状态的问题；
6. 是否存在可见终端窗口启动路径；
7. Windows/macOS/Linux Tool 描述是否与实际 shell resolver 一致；
8. 是否违反“不新增数据库表”；
9. 是否将一次性 `execute_terminal` 和交互 session 混成一个难以维护的工具；
10. 是否处理取消、backend 崩溃、worker 退出、页面卸载和 WebSocket 重连；
11. 是否复用了现有 `TerminalViewport`、`ToolDisplayHints`、`display_data.kind` 和 assistant-ui renderer 边界；
12. 测试是否覆盖了真实失败路径，而不仅是 happy path。

复查结果必须按以下格式返回：

```text
结论：通过 / 有条件通过 / 不通过

P0：阻断发布的问题（没有则写“无”）
P1：应在本轮修复的问题（没有则写“无”）
P2：后续优化建议（没有则写“无”）

每条问题：文件路径、行号或模块、事实证据、风险、建议。
明确列出已检查但未发现问题的验收项。
```

子 Agent 不得修改实现以“顺手修复”问题，不得修改验收结果来降低问题等级，不得把推测当成代码事实，也不得代替主 Agent 运行需要用户授权的外部操作。

## 13. 测试矩阵

后端单元测试：shell resolver 的 Windows/POSIX 分支、Tool 描述生成和 shell 可用性、session 归属和状态机、operation 去重和 session 级串行化、output seq/ring buffer/resync、generation 防旧事件覆盖、worker EOF/异常退出/超时/进程树清理、cancel/close/write 竞态，以及不新增表的 schema 检查。

平台集成测试：Windows PowerShell 5.1，存在时验证 PowerShell 7；macOS zsh；Linux bash/sh；REPL 多轮输入；中文输出；ANSI/VT、光标、擦除、回车刷新；Ctrl+C/EOF；子进程树清理；worker locator、Tauri resources、无窗口启动。

前端 Vitest：preview protocol encode/decode、attach/reconnect/cursor/resync、output seq 去重和顺序、read-only 约束、ToolPart 对 `terminal-session` 的路由和 fallback、xterm 不把完整输出复制进 React render state。

前端 Playwright：工具卡片打开/聚焦 TerminalPanel、running/exited/interrupted/failed 状态、ANSI 和回车刷新视觉效果、断线重连和 resync、多只读 preview 同时 attach。当前 E2E 不代替真实 Tauri/ConPTY 集成测试。

## 14. 明确不采用的方案

1. 不把 `shell=True + PIPE` 扩展成伪交互终端；
2. 不在 React 中使用 `child_process`、Tauri command 或直接 SQLite；
3. 不打开系统终端 GUI；
4. 不让 TerminalPanel 发送用户输入或控制信号；
5. 不把终端 output 全量写入 conversation snapshot、LangGraph checkpoint 或新数据库表；
6. 不通过 Assistant SSE 模拟无限终端输出；
7. 不引入公网 WebSocket、认证、多租户、Redis、Postgres、容器沙箱或远程队列；
8. 不按操作系统拆出重复 Tool 名称；
9. 不把 `execute_terminal` 和交互式 session 合并成一个超级工具；
10. 不让工具 renderer 直接管理 session service 或 WebSocket。

## 15. 前端面板成熟实现细化

本节是前端 TerminalPanel 的实现基线。推荐采用成熟的 `xterm.js` 终端模拟器、独立只读 preview stream、Task 级 session store 和 assistant-ui Tool renderer；不使用 `<pre>`、Markdown 文本或自定义 TerminalBlock 解释 ANSI/VT。

### 15.1 前端组件结构

```text
TaskPage
├─ Thread
├─ Assistant UI ToolPart
│   └─ TerminalToolCard
│       └─ 打开终端
└─ TerminalPanelHost
    └─ TerminalPanel
        ├─ TerminalPanelHeader
        ├─ TerminalViewport
        └─ TerminalPanelStatus
```

TerminalPanel 应作为 Task 级底部面板或右侧抽屉存在，不嵌入每一个 Tool 卡片。Tool 卡片只展示 session 摘要并提供“打开终端”入口；点击入口只聚焦或打开已有 session，不创建 session、不执行命令。

面板关闭只取消 preview 订阅，不关闭 backend session。一个 Task 可以存在多个 session，store 必须使用 `task_id + session_id` 作为 key，不能把不同 session 的输出混入同一个 xterm 实例。

### 15.2 复用边界和两种输出模式

当前一次性终端使用累计字符串 snapshot：

```text
累计 output snapshot
  → TerminalOutputReconciler
  → xterm.write(string)
```

交互式终端使用原始字节 frame：

```text
WebSocket output frame
  → base64 解码为 Uint8Array
  → 按 seq 排序和去重
  → xterm.write(Uint8Array, callback)
```

两种模式可以复用 xterm 初始化、尺寸适配、生命周期销毁和错误处理，但不能让交互式 raw byte frame 直接进入一次性 snapshot reconciler。前端应保持两个明确的 consumer：

```text
SnapshotTerminalConsumer  → execute_terminal
StreamTerminalConsumer    → terminal session
```

现有 `TerminalSession`、`TerminalWriteScheduler`、`TerminalOutputReconciler` 和 `TerminalViewport` 可以复用其 xterm 生命周期和串行写入思想；交互式 session 需要独立的 append-bytes consumer。

### 15.3 xterm 配置

交互式 TerminalPanel 建议使用：

```ts
new Terminal({
  disableStdin: true,
  cursorBlink: false,
  convertEol: false,
  scrollback: 5000,
  allowTransparency: false,
});
```

必须满足：

- `disableStdin: true`；
- 不注册 xterm `onData`；
- 不转发键盘输入或粘贴内容；
- 不发送 Ctrl+C、EOF、signal 或 close；
- 不通过 ResizeObserver 修改后端 PTY 的 cols/rows；
- 只用 fit addon 做本地视觉适配；
- 保持 PTY 的 `\r`、`\n`、ANSI 光标、擦除、颜色和全屏语义。

一次性终端可以继续使用自身的换行配置；交互式 raw PTY 输出不能套用一次性终端的文本归一化逻辑。

### 15.4 TerminalSessionStore

Store 只保存连接、状态和游标，不保存完整终端 output：

```ts
type TerminalSessionState = {
  taskId: number;
  sessionId: string;
  generation: string | null;
  status: "connecting" | "running" | "exited" | "interrupted" | "failed";
  firstAvailableSeq: number;
  nextSeq: number;
  lastAppliedSeq: number;
  connection: "idle" | "connecting" | "attached" | "reconnecting" | "closed";
  unreadCount: number;
  followOutput: boolean;
};
```

`lastAppliedSeq` 只有在 xterm 的写入 callback 成功完成后才能更新。React state 不保存完整输出文本；TerminalPanel 通过外部 store 订阅状态变化，output frame 直接进入有界写入队列。

### 15.5 Preview connection 状态机

```text
idle
  → connecting
  → attached
  → streaming
       ├─ 网络断开 → reconnecting
       ├─ resync_required → resetting
       ├─ exit → exited
       └─ backend crash → interrupted
```

打开面板时：

1. 使用运行时 backend 地址创建 WebSocket；
2. 发送 `attach(lastAppliedSeq)`；
3. 接收 `attached`；
4. 按 seq 升序 replay output；
5. 进入 streaming 状态。

处理 output frame 时：

1. generation 不匹配时丢弃旧事件；
2. `seq <= lastAppliedSeq` 时丢弃重复事件；
3. `seq > lastAppliedSeq + 1` 时触发 resync；
4. 连续 frame 进入串行 xterm 写入队列；
5. xterm 写入 callback 完成后更新 `lastAppliedSeq`。

不能仅因为 WebSocket 收到 message 就推进游标，必须以 xterm 实际完成写入作为“已应用”依据。

### 15.6 输出背压

前端必须使用有界 frame queue：

```text
WebSocket 接收
  → 有界 frame queue
  → 串行 xterm.write(Uint8Array)
```

当连续 frame 可以安全合并时，可以批量写入，但必须保留 seq 语义。不能丢弃中间 frame、无限堆积内存或阻塞 React 渲染线程。当无法继续保证连续性时，应主动断开并重新 attach/resync，而不是显示一段不完整终端内容。

### 15.7 面板交互

面板头部展示：

```text
PowerShell · C:\project · 运行中 · 只读预览
```

面板可以提供：

- 自动滚动；
- 用户滚动离开底部后暂停跟随；
- unread 数量和“回到底部”；
- 选择、复制和搜索；
- 用户主动触发的输出下载；
- running、exited、interrupted、failed 状态；
- exit code 和重连提示。

复制和下载只保留在本机 UI，不写日志、Assistant snapshot 或新增历史表。

面板不得提供：

- 输入框；
- Ctrl+C、Ctrl+D、kill、signal、close session 按钮；
- 键盘输入、粘贴写入；
- 远程 resize；
- 直接调用 terminal Tool；
- 直接创建或停止进程。

### 15.8 assistant-ui 接入

现有 `tool-part.tsx` 继续按 `display_data.kind` 和 `expand_layout` 路由，不按工具名散落增加分支。实现时必须将 `terminal-session` 加入 `KNOWN_DISPLAY_KINDS` 并路由到 terminal renderer，否则会进入 `ToolFallback`。

Tool renderer 只负责：

- 显示工具调用状态；
- 显示 shell、cwd、session 摘要；
- 发出“打开预览”的 UI 事件。

Tool renderer 不负责创建 WebSocket、维护 xterm、调用 terminal service 或控制进程。建议通过 Task 级 terminal context/store 连接 `TerminalToolCard` 与 `TerminalPanelHost`。

assistant-ui 的 `status.type` 与项目状态显式映射：

```text
backendStatus=pending/running  → assistant-ui running
backendStatus=completed       → assistant-ui complete
backendStatus=failed          → assistant-ui incomplete(error)
backendStatus=cancelled       → assistant-ui incomplete(cancelled)
```

`backendStatus`/`ToolObservation.status` 是生命周期唯一事实源，assistant-ui status 只是 renderer 适配层。交互终端不使用 `requires-action`，因为用户没有输入或审批控制面；`unknown` 按 fallback 处理。

### 15.9 resync 的现实边界

当前 ring buffer 保存原始 output frame，不保存 VT 屏幕快照或 parser state。因此：

- 无 gap 时，可以按 cursor 精确恢复；
- 有 gap 时，清空 xterm 后 replay 仍可用的完整 frame；
- 如果 gap 截断了 escape sequence，不能保证画面完全正确；
- 全屏程序和光标密集型程序不能宣称支持 gap 后的精确恢复。

UI 应显示受控提示：

```text
终端输出需要重新同步
```

如果未来必须支持全屏终端的精确恢复，再增加 backend 进程内 VT/screen snapshot；该 snapshot 不进入数据库，也不新增数据库表。

### 15.10 实施顺序

1. 从现有 `TerminalViewport` 抽取 raw-byte append 模式；
2. 增加 `StreamTerminalConsumer`；
3. 实现 `TerminalSessionStore`；
4. 实现 `terminal-preview-v1` WebSocket attach/reconnect；
5. 实现 Task 级 `TerminalPanelHost`；
6. 将 `terminal-session` 加入 Tool renderer allowlist；
7. 完成重复 seq、gap、generation、只读约束和组件卸载测试；
8. 完成 `npm install`、ANSI 进度条、`git fetch`、REPL 和异常退出的视觉验收。

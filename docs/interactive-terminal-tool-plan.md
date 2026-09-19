# 可交互、跨平台终端工具方案

> 状态：后端 session/service/API、Rust `portable-pty` Worker 与 Tauri resource staging 已实现并通过 Windows 定向验收；前端预览面板和 Agent tool 注册仍按“充分测试后再开放”执行
>
> 范围：第一阶段只解决本机单用户桌面 Agent 中由 Agent 驱动的可交互终端，以及 Windows/macOS/Linux 的 shell 兼容；前端只提供 WebSocket 只读预览。
>
> 明确暂不处理：用户通过终端面板输入或控制终端、危险命令审批、输出脱敏、远程执行、多租户、容器沙箱、终端录制回放。

当前额外约束：终端 Worker 和其 shell 必须无可见窗口启动。终端 UI 只接收 Worker 输出，不提供用户输入通道。

## 1. 结论

当前的 `execute_terminal` 是“一次性前台命令”工具，不应直接改造成长期交互终端。建议保留它，并增加一个独立的终端会话能力：

```text
execute_terminal
  一条命令 → 等待进程结束 → 返回输出和 exit code

terminal_session
  启动 shell → Agent 通过工具读写 → resize / signal → 显式关闭
```

可交互能力的核心不是 `cmd`、PowerShell、bash 等命令名称，而是使用操作系统的伪终端：

```text
Windows       → ConPTY
macOS/Linux   → Unix PTY
前端          → xterm.js 终端模拟器（只读预览）
后端          → TerminalSessionService
```

普通 `stdin=PIPE/stdout=PIPE` 只能提供管道，不会提供真实 TTY 语义。很多 REPL、全屏程序、颜色控制、光标控制和密码提示会因此失效。

## 2. 基于当前代码的事实

### 2.1 当前终端执行实现

当前实现位于：

- `apps/backend/app/core/tools/tool_handler/execute_terminal.py`
- `apps/backend/app/core/tools/tool_handler/terminal/execution_backend.py`
- `apps/backend/app/core/tools/tool_handler/terminal/local_backend.py`
- `apps/backend/app/core/tools/tool_execute/tool_handler_runner.py`

现有行为：

1. `ExecuteTerminalTool.execute()` 接收 `command`、`timeout`、`workdir` 和 `ToolExecutionContext`。
2. `LocalExecutionBackend` 使用 `subprocess.Popen`，当前调用 `shell=True`。
3. Windows 依赖默认 shell `cmd.exe`，POSIX 依赖 `/bin/sh`；这与“Windows PowerShell”这个目标还不一致。
4. stdout 和 stderr 合并为一个输出流。
5. 读取线程支持增量 `OutputSink`，并有最终输出和流式输出预算。
6. 超时在 POSIX 使用进程组，在 Windows 使用 `taskkill /F /T` 清理进程树。
7. `ToolDefinition.execution_mode="process"` 可让工具进入隔离子进程，并由 `ToolHandlerRunner` 做硬超时和取消清理。
8. `ToolExecutionContext.for_process_execution()` 已经提供了跨进程安全的任务、工作区和路径上下文。

因此，现有代码可以复用“工具注册、工作区上下文、进程隔离、取消和执行日志”这些边界，但不能复用 `execute()` 的“启动后等待退出”模型作为交互会话本体。

### 2.2 当前桌面和 Assistant UI 边界

当前桌面边界事实：

- Tauri Rust 主进程启动、停止和重启本机 FastAPI 后端。
- 后端绑定 `127.0.0.1`，前端通过运行时配置获取动态端口。
- React 通过 `apps/desktop/lib/http/client.ts` 使用动态 base URL、`no-store` 和 `X-Trace-Id`。
- Assistant UI 由 `AssistantRuntimeProvider` 提供 runtime，具体装配位于 `apps/desktop/components/assistant/runtime/assistant-runtime-session.tsx`。
- 当前 `useAssistantTransportRuntime` 通过 Assistant Transport 传输 Task 的 canonical conversation state。
- 当前工具 UI 由 `components/assistant-ui/tools/tool-part.tsx` 根据工具 display contract 路由，已有 `TerminalTool` 用于渲染一次性终端工具结果。
- 后端的 Assistant Transport 入口位于 `apps/backend/app/assistant_transport/assistant_api.py`；当前前端使用 `protocol="assistant-transport"`，通过 `text/event-stream` 传输对话 Run 和 snapshot，不应承担终端原始字节流。

Assistant UI 官方文档对应的结论：

- [Assistant Transport](https://www.assistant-ui.com/docs/runtimes/custom/assistant-transport) 是基于 `ExternalStoreRuntime` 的 Agent state streaming 协议，适合传输完整 Agent state 和自定义 commands。
- [Runtime architecture](https://www.assistant-ui.com/docs/runtimes/concepts/architecture) 将 runtime、protocol 和 framework adapter 分层；终端字节流不属于对话 message state。
- [Tool UI](https://www.assistant-ui.com/docs/tools/tool-ui) 要求工具 renderer 处理工具调用的 running、complete、error 等状态。
- [AssistantRuntimeProvider](https://www.assistant-ui.com/docs/api-reference/context-providers/assistant-runtime-provider) 应继续作为 UI runtime 的根 Provider。

方案不把 assistant-ui 类型泄漏到 backend domain、终端 backend 或 storage。Assistant UI 只显示终端工具调用及其可控摘要；Agent 的终端输入和终端预览输出走独立的 terminal service/transport，前端 WebSocket 只接收输出。

## 3. 进程、数据和生命周期边界

### 3.1 进程归属

```text
Tauri Rust 主进程
  └─ 启动/停止/重启 FastAPI backend

FastAPI backend
  ├─ TerminalSessionService
  ├─ Agent Runtime / ToolExecutor
  └─ session registry
       └─ Terminal Worker 子进程
            └─ PTY / ConPTY
                 └─ PowerShell/bash/zsh/fish 等 shell

React WebView
  ├─ AssistantRuntimeProvider / Thread
  └─ xterm.js 终端面板
```

终端业务行为由 backend 拥有。React 不创建 shell，不直接操作 SQLite，不直接管理子进程。每个 Terminal Worker 是由 `TerminalSessionService` 长期持有的独立子进程，只拥有 PTY 和操作系统子进程，不拥有 Task、Run 或 Assistant snapshot 等业务事实。

### 3.2 进程边界变化

这是一个新的本机 backend ↔ Terminal Worker 边界，但不是新的服务层，也不是远程服务：

- backend 通过本机进程通信或 worker 管理 API 控制终端；
- worker 只返回 PTY 字节、退出状态和运行态事件；
- backend 负责把这些事件映射为终端会话状态和工具结果；
- Tauri 仍然只负责 backend 的生命周期，不直接管理每个终端会话。

第一版固定使用独立 Rust `portable-pty` Terminal Worker，由 `TerminalSessionService` 直接创建并长期持有。Worker 已落在 `apps/terminal-worker/`，通过长度前缀 JSON stdio 协议与 Python backend 通信。它不能复用现有 `ToolHandlerRunner`：`ToolHandlerRunner` 是“一次工具调用一个子进程”，工具 handler 返回后会在 `finally` 中清理仍存活的子进程，因此无法承载跨 `terminal_start`、`terminal_write`、`terminal_read` 调用的 session。若未来替换 PTY 底层实现，只替换 worker，不改变 backend 的 session/service 契约。

worker 与 backend 之间使用有界、长度前缀的本机控制帧；输入和输出 payload 按 bytes 处理，控制字段使用 JSON，二进制 payload 使用 base64。worker 启动后必须先发送包含协议版本、pid、PTY 类型的 handshake；backend 每 5 秒发送 heartbeat，worker 连续 15 秒未收到 heartbeat 或读到 owner pipe EOF 时终止 shell 进程组并退出。正常关闭先发送显式 `shutdown` 控制帧再关闭 pipe，异常关闭则只表现为 EOF/heartbeat timeout。owner pipe 只保留 backend 写端和 worker 读端，创建 worker 时禁止 shell/孙进程继承该 pipe 句柄。worker 不直接读写 SQLite。

### 3.3 权威状态存储

持久事实和进程内运行态必须区分：

```text
SQLite app.sqlite3
  保存 terminal session 元数据和最终状态

backend 进程内 registry
  保存活跃 session、worker handle、订阅者、取消事件、输出 seq

有界 ring buffer
  保存用于当前 UI/Agent 重连的最近输出，不作为长期事实

LangGraph checkpoint
  仍只服务 Agent workflow，不记录 PTY 状态
```

建议的最小元数据：

```text
session_id
task_id
workspace_id
created_by_run_id nullable
initial_cwd
shell_kind
shell_executable
worker_instance_id
worker_pid nullable
status: starting | running | exited | interrupted | failed | closed
end_reason nullable
exit_code nullable
cols
rows
created_at
updated_at
last_activity_at
ended_at nullable
```

`created_by_run_id` 只记录创建来源，不是 session 所有权；Task 级 session 可以跨多个 Run 使用。`initial_cwd` 是启动目录，不承诺反映 shell 内部通过 `cd` 改变后的当前目录。`worker_instance_id` 用于避免 PID 复用，`worker_pid` 只用于诊断和当前进程定位。`end_reason` 应使用稳定枚举，例如 `shell_exited`、`user_closed`、`backend_shutdown`、`worker_failed` 或 `idle_timeout`。`cols/rows` 保存最近一次尺寸，`last_activity_at` 用于生命周期清理。

不把每个输出 chunk 无限写入 SQLite。默认只保留有界 ring buffer；输出 seq、subscriber 和 cursor 属于 backend 进程内运行态。`last_output_seq` 即使将来作为诊断 watermark 持久化，也不能作为重连输出的事实源；如果将来需要审计或录制，应另行设计存储策略，而不是让 conversation snapshot 变成终端日志。

session 采用 Task 级所有权，而不是 Run 级所有权：表中的 `created_by_run_id` 只记录创建来源，可为空，不决定 session 是否存活。这样同一个终端可以跨多个 Agent Run 和页面 attach 使用。Run 取消只取消当前 `terminal_write/read` 工具调用；除非显式 `terminal_close`、session idle/max lifetime 到期或 backend shutdown，不自动关闭 session。

### 3.4 启动、停止、崩溃和重启

Task 内 Run 串行只适用于现有 Agent Run 的业务操作，不自动覆盖 terminal WebSocket：

- `TaskRuntimeSpace.async_operation()` 保护 Run/context 等 Task 级业务互斥；不能被 attach、output 发送或长时间等待的 `terminal_read` 持有。
- 多个只读预览可以并发 attach 同一 session；不同 session 也可以并行。
- 同一 session 的 Agent `write/signal/close` 由 session actor/mailbox 串行；`terminal_read` 和 WebSocket preview 都是基于 cursor 的并发非破坏性读取。
- WebSocket 连接不会进入 Task 锁；它只经过 session 归属校验和 subscriber registry。

启动：

1. Agent 调用 `terminal_start` 创建会话；前端只通过已知的 `task_id + session_id` attach 预览，不负责创建会话。
2. backend 校验 task/workspace 上下文，解析 shell，创建 PTY worker。
3. worker 启动 shell，并返回 `session_id`、初始输出和状态。
4. UI attach 到 session；Agent 通过工具读写同一 session，UI 只接收输出和状态。

正常关闭：

1. Agent 或 backend 生命周期发送 `terminal_close`。
2. backend 先停止接收新的 input，再向 shell 发送正常结束信号。
3. 等待短暂 grace period；仍未退出时清理进程树。
4. 写入 `exited` 或 `closed` 终态元数据，广播终态事件。

取消和超时：

- `terminal_signal("interrupt")` 对应 Ctrl+C/中断语义；
- session 自身 idle/max lifetime 到期时，backend 终止 worker；
- 所属 Agent Run 被取消时，只中断当前工具操作并释放等待者；Task 级 session 继续存活，后续操作仍需显式使用同一个 `session_id`；
- worker 退出后不能被旧的异步读取线程重新标记为 running；
- 所有事件带 `session_id` 和 generation，旧 worker 事件不得覆盖新 session。

backend 崩溃或桌面退出：

- Tauri 是 backend 生命周期的唯一 owner，并按现有平台实现发起 backend 停止；但它不直接拥有 Terminal Worker，当前 POSIX `child.kill()` 也不保证 backend 的所有孙进程被清理；
- backend 正常 shutdown 时先关闭所有 session worker；
- Terminal Worker 必须监视 backend 控制管道的 EOF/heartbeat 超时，发现 backend 崩溃后主动终止自己的 shell 进程组；
- Windows worker 使用 Job Object 管理 shell 树；POSIX worker 使用独立进程组清理 shell 树；
- 启动恢复时，在单个数据库事务中把遗留的 `starting/running` session 原子标记为 `interrupted`；
- 不隐式重放旧 shell 输入，也不恢复旧 Agent Run；
- Agent 必须显式创建新 session；前端预览不能恢复或创建旧 session。

这与现有 Conversation Run 的恢复规则一致：传输断开可以重新 attach，但业务执行不会因为前端断开而自动取消或重放。

backend lifespan 的接入顺序固定为：storage ready 后初始化 `TerminalSessionService`，在标记 boot ready 前完成 session registry 和 orphan recovery；shutdown 时先停止接受新的 terminal operation，再关闭并等待 `ConversationRunExecutor`，随后关闭全部 Terminal Worker，最后关闭 service dependencies/SQLite。`close_all()` 必须对重复调用安全；worker 关闭失败或超时必须记录结构化错误，并继续清理其余 session。

## 4. Shell 和 PTY 适配设计

### 4.1 ShellResolver

新增独立的 `ShellResolver`，不要在 handler 中散落平台判断。

输入：

```text
shell: auto | powershell | pwsh | bash | zsh | fish | custom
```

输出：

```python
ShellSpec(
    executable: str,
    argv: tuple[str, ...],
    env: Mapping[str, str],
    platform: Literal["windows", "posix"],
)
```

默认解析：

Windows：

```text
powershell → powershell.exe -NoLogo -NoProfile
pwsh       → pwsh.exe -NoLogo -NoProfile
auto       → 优先 pwsh.exe，找不到时使用 powershell.exe
```

macOS/Linux：

```text
auto       → $SHELL，缺失时使用 /bin/sh
bash       → bash -i
zsh        → zsh -i
fish       → fish -i
custom     → 仅允许已解析的可执行文件路径
```

是否附加 `-l` 应是显式配置，不要默认改变用户 shell 初始化行为。交互 shell 不使用 `-NonInteractive`；一次性 `execute_terminal` 可以继续采用非交互参数，但应逐步从 `shell=True` 迁移到显式 shell argv。

### 4.2 InteractiveTerminalBackend

终端执行 backend 与一次性 `ExecutionBackend` 分开：

```python
class InteractiveTerminalBackend(Protocol):
    def start(self, spec: ShellSpec, cwd: str, cols: int, rows: int) -> None: ...
    def write(self, data: bytes) -> None: ...
    def read(self, timeout: float) -> bytes: ...
    def send_signal(self, signal: TerminalSignal) -> None: ...
    def terminate(self, force: bool = False) -> None: ...
```

平台实现：

- POSIX：Unix PTY，使用非阻塞读和 `selectors`/async bridge。
- Windows：ConPTY，处理 pseudo console handle 和输入输出。
- 不在业务 service 中直接调用 `pty`, `CreatePseudoConsole` 或平台 API。

实现选型固定为 Rust `portable-pty` Terminal Worker：

- worker 通过项目桌面构建/分发流程作为本地 sidecar 提供；
- 开发环境从明确的 runtime 路径解析，生产环境从应用资源目录解析；
- FastAPI backend 启动 worker，不由 React 或 Tauri 单独创建 worker；
- worker 内部统一封装 Windows ConPTY 和 macOS/Linux Unix PTY；
- Python backend 只依赖稳定的 session/worker 协议，不直接 import 平台 PTY API。

不手写一套 Python 伪终端兼容层，也不在第一阶段同时维护 `pywinpty`、`ptyprocess` 和 Rust 三条实现路线。

sidecar 路径由桌面运行时解析：开发模式优先使用 `CODING_AGENT_TERMINAL_WORKER`，否则查找仓库统一的 `target/debug/terminal-worker[.exe]`；Tauri 启动 backend 时将路径注入同名环境变量。生产模式通过 `bundle.resources` 将 `target/resources/terminal-worker/` 映射到应用 resource 目录，`build:bundle` 会先按当前平台构建 release binary 并完成 staging；不采用 Tauri 直接拥有进程的 `externalBin` 语义，因为 Terminal Worker 的生命周期 owner 是 backend。macOS/Linux 发布前验证可执行权限，Windows 验证 `.exe` 路径和签名/杀毒软件兼容性。

无窗口约束由两层共同保证：Rust Worker 使用 `windows_subsystem = "windows"`，不创建可见控制台；Windows backend 使用 `CREATE_NO_WINDOW` 创建 Worker。PTY shell 仍运行在 ConPTY/Unix PTY 中，因此“无窗口”不等于退化为普通 pipe，也不影响交互程序接收 PTY 输入。

当前真实验收覆盖：Rust `cargo check/build`、Windows `cmd.exe` PTY 输入/输出/退出、Windows PowerShell 命令启动/输出/退出、backend session 定向测试、Tauri debug resource staging 与实际构建 resource 目录启动 smoke test。staging 与 packaged 验证使用不同模式，packaged 模式不会回退到 staging 目录；跨平台 release/installer 验收由 `.github/workflows/terminal-worker-bundle.yml` 矩阵执行。PowerShell 5.1 的 PSReadLine 依赖真实终端模拟器的设备查询，Worker 对 `CSI 6 n` 自动返回光标位置，满足只读预览阶段的启动兼容；用户输入通道开放前仍需单独验证更多 VT 查询和 xterm.js 交互。

### 4.3 输入输出语义

PTY 数据在 transport 层按 bytes 处理，在 UI 层按终端编码处理；不要把每个 chunk 强行当作一行文本。

需要支持：

- `\r`、`\n` 和 ANSI/VT 控制序列；
- UTF-8；Windows PowerShell 旧版本的系统编码兼容；
- Ctrl+C、Ctrl+D、Ctrl+Z 等控制输入；
- `cols/rows` 调整；
- shell 退出、worker 退出、EOF 和强杀；
- 输出读取背压。

UI 应将 PTY 原始输出传给 xterm.js。Agent 的 `terminal_read` 可以返回文本视图，但这一视图必须有明确的最大长度和 seq 边界。本阶段不设计新的终端专用脱敏策略；这不改变现有 `execute_terminal` 已有的命令拦截/输出处理，也不允许结构化日志写入完整原始输出。终端交互本身属于本机信任边界内的原始输出能力。

## 5. Backend session/service 结构

建议目录：

```text
apps/backend/app/core/tools/tool_handler/terminal/
  execution_backend.py       # 现有一次性命令契约
  local_backend.py            # 现有一次性命令实现
  shell_resolver.py           # 新增：跨平台 shell 解析
  interactive_backend.py      # 新增：PTY backend 契约
  interactive_worker.py       # 新增或抽取：worker 进程入口
  session_protocol.py         # 新增：worker/session 消息契约

apps/backend/app/service/terminal/
  terminal_session_service.py # 新增：会话用例和生命周期
  terminal_session_registry.py# 新增：进程内活跃会话协调

apps/backend/app/api/
  terminal_api.py              # 新增：session HTTP/WS 边界

apps/backend/app/storage/
  terminal_session_*           # 新增：元数据模型、CRUD、migration
```

如果现有项目的 storage 命名约定要求不同，可以按现有目录规范调整，但职责不要合并回 `execute_terminal.py`。

`TerminalSessionService` 负责：

- 校验 task/workspace 归属；
- 创建和关闭 session；
- 转发 input、signal；
- 管理 output seq 和订阅者；
- 处理 attach/reconnect；
- 处理 worker 退出和终态落库；
- 在 backend lifespan 关闭时清理全部 session。

它必须通过现有 `ToolRuntimeDependencies` 接入 Agent Runtime，不能依赖 `ToolHandlerRunner` 的 thread 路径自动传递取消：

```python
ToolRuntimeDependencies(
    delegate_task_executor=...,             # existing
    terminal_session_service=...,           # thread-only
    is_run_cancelled=lambda run_id: ...,   # thread-only
)
```

`for_process_execution()` 必须清空 `terminal_session_service` 和 `is_run_cancelled`。`terminal_read` 在 service 内以不超过 50ms 的条件等待/轮询检查 `is_run_cancelled(run_id)`；取消后返回 cancelled observation。现有 thread runner 不承诺 OS 级强杀，PTY 清理由 Terminal Worker 负责。

`terminal_start/write/read/signal/close` 这些 handler 应声明为 `execution_mode="thread"`：它们只在当前 backend 线程中快速调用 `TerminalSessionService`，由 service 持有长期 worker。不能声明为 `execution_mode="process"`，也不能把长期 session 放进现有 `ToolHandlerRunner` 的一次性 worker。运行时通过现有 `ToolRuntimeDependencies` 注入 service；需要扩展该值对象时，只加入同进程可用的 service 引用，并继续由 `for_process_execution()` 清空。

它不负责：

- Assistant UI message 转换；
- Agent workflow 状态机；
- shell 字符串危险性分析；
- 长期保存全部终端输出；
- Tauri 窗口或 backend 进程启动。

## 6. 工具契约

### 6.1 `terminal_start`

```json
{
  "shell": "auto",
  "cwd": ".",
  "cols": 120,
  "rows": 32
}
```

返回：

```json
{
  "session_id": "term_123",
  "shell": "powershell.exe",
  "cwd": "C:/workspace",
  "status": "running",
  "output": "...",
  "next_seq": 4
}
```

### 6.2 `terminal_write`

```json
{
  "session_id": "term_123",
  "data": "python\n",
  "after_seq": 4,
  "wait_ms": 500
}
```

`data` 在 backend tool 边界按 UTF-8 编码为 bytes 后写入 worker；当前不做 terminal operation 幂等保护，重复请求可能重复输入。`after_seq` 明确表示调用方已经应用的最后一个 output frame；`wait_ms` 只表示本次工具调用最多等待新增输出的时间，不表示等待命令结束。返回 `seq > after_seq` 的 output delta、`next_seq` 和 session status。Agent `terminal_read` 返回 UTF-8 文本视图，非法字节使用 replacement character；WebSocket 仍传输原始 bytes 的 base64。调用方不得对状态改变操作自动重试。

### 6.3 `terminal_read`

```json
{
  "session_id": "term_123",
  "after_seq": 4,
  "wait_ms": 1000
}
```

如果 ring buffer 已经丢弃了 `after_seq` 之前的输出，应返回明确的 `resync_required`，而不是静默拼接不完整文本。

### 6.4 `terminal_signal`、`terminal_close`

```json
{"session_id":"term_123","signal":"interrupt"}
{"session_id":"term_123"}
```

第一阶段不做 terminal operation idempotency。操作仍必须经过 session 级串行化，并校验 Task/session 归属；HTTP 客户端和 Agent 调用方不得自动重试 `terminal_start/write/read/signal/close`。PTY 尺寸只在 `terminal_start` 时确定，不提供运行时 resize 操作。如果未来需要网络重试安全，再新增独立 operation record，而不是把多个 operation id 塞进 `terminal_sessions`。

## 7. Transport 设计

### 7.1 终端数据面单独走 WebSocket

当前 Assistant Transport wire protocol 是基于 `text/event-stream` 的 `assistant-transport` 协议，用于对话状态和 Run snapshot。终端预览需要低延迟、可持续的服务端输出流，建议新增本机只读 WebSocket；终端不接入 Assistant UI 的 data-stream 协议：

```text
GET /tasks/{task_id}/terminal/sessions/{session_id}/stream?trace_id=<trace_id>
```

浏览器原生 WebSocket API 不能像 `requestJson` 一样任意设置 `X-Trace-Id` header，因此 WebSocket 使用 query 中的随机 `trace_id`，backend 将其绑定到本次连接日志上下文。该值只是本次请求的 trace 标识，不承载凭据。backend 必须同时校验 URL 中的 `task_id` 与 session 的 `task_id`；`session_id` 使用不可预测的随机值，但不把它当作绕过 Task 归属校验的永久 capability。前端把运行时的 `http://127.0.0.1:port` 转成 `ws://127.0.0.1:port`（HTTPS 将对应转换为 WSS），不得硬编码端口。

连接建立后，客户端只发送 attach 控制帧；该帧仅用于声明重连 cursor，不是终端输入。新连接没有 cursor 时将 `after_seq` 设为 `null`：

```json
{"type":"attach","after_seq":123}
```

服务端先返回：

```json
{"protocol":"terminal-preview-v1","type":"attached","generation":"gen_7","first_available_seq":120,"next_seq":124,"status":"running","cols":120,"rows":32}
```

客户端除 attach 外不发送终端操作消息。以下消息不属于第一阶段 WebSocket 协议：

```text
input / signal / close
```

第一阶段 WebSocket 不承载状态改变操作，因此不定义 input frame、WebSocket operation_id 或 WebSocket 输入背压。Agent 工具仍通过 backend service 调用 `write/signal/close`；这些调用的 backend→worker 队列保持有界，队列满时返回 `TERMINAL_INPUT_BACKPRESSURE`，不得无限排队，也不得阻塞 PTY reader。

attach 和协议错误：客户端首帧必须是合法的 `attach`，`after_seq` 只能是非负整数或 `null`；未知消息、重复 attach、非法 cursor 或超过 8 KiB 的控制帧返回结构化 `protocol_error` 后关闭连接。session 不属于 URL 中的 Task 时返回 404/403；不向 WebSocket 推送其他 Task 的状态。服务端每 30 秒发送 WebSocket ping，连续 90 秒未收到 pong 则关闭连接；这只关闭预览连接，不关闭 session。

服务端控制消息：

```json
{"protocol":"terminal-preview-v1","type":"status","generation":"gen_7","status":"running","cols":120,"rows":32}
{"protocol":"terminal-preview-v1","type":"exit","generation":"gen_7","status":"exited","exit_code":0}
{"protocol":"terminal-preview-v1","type":"resync_required","generation":"gen_7","after_seq":10,"first_available_seq":120,"next_seq":124}
```

输出消息是 JSON text frame，PTY bytes 以 base64 传输，避免把任意控制字节误当作 JSON/UTF-8：

```json
{"protocol":"terminal-preview-v1","type":"output","generation":"gen_7","seq":124,"data_base64":"bHMgLWxhDQo="}
```

`seq` 是每个 output frame 的单调递增编号；`next_seq` 是下一个将要分配的编号；`first_available_seq` 是 ring buffer 当前能重放的最小编号，ring buffer 为空时等于 `next_seq`。`after_seq` 表示客户端已经完整应用的最后一个编号，服务端只发送 `seq > after_seq` 的 frame。如果 `after_seq < first_available_seq - 1`，服务端必须发送 `resync_required`，不得静默返回不完整输出。PTY screen state 无法仅由被截断的输出恢复，UI 应清空/重建终端并提示存在 output gap；Agent `terminal_read` 应返回可识别的 resync 错误。首次 attach 不带 cursor 时从 `first_available_seq` 开始；若 session 已退出，仍先返回可用 ring buffer，再返回 exit/status。

attach 的输出顺序必须是：`attached` → 按 seq 升序 replay 的 output → 当前终态 `status/exit`（如果 session 已终止）。backend 必须先注册 subscriber，再建立 replay 边界；replay 期间到达的新 output 进入该 subscriber 的待发送队列，避免漏帧。所有 output/status/exit/resync 消息携带同一 `generation`；旧 generation 事件不得覆盖新状态。`exited` 表示 shell 自然退出或返回 exit code，`closed` 表示 backend/Agent 显式关闭，`interrupted` 表示 backend 重启或异常恢复收敛，`failed` 表示 worker/PTY 启动或运行失败；终态只允许第一次有效转换，重复 close 不重新广播终态。

PTY reader 只负责把 output 写入 session ring buffer，然后以非阻塞方式复制给每个 subscriber 的独立有界发送队列。每个 subscriber 队列满时，标记该 subscriber 有 gap，尽力发送一次 `resync_required` 后关闭该 WebSocket（关闭码 1013）；不能因为一个慢 UI 阻塞 PTY reader、其他 subscriber 或 Agent `terminal_read`。重新连接必须带 `after_seq`，由服务端根据 ring buffer 判断能否补发。

Worker/backend 内部同样按 bytes 传输。Agent 工具返回文本时使用统一 UTF-8 解码并对非法字节采用 replacement；UI 连接层解码 base64 为 `Uint8Array` 后直接交给 xterm.js。第一阶段不定义更高层的终端编码协商。

断开 WebSocket 不等于关闭 session。客户端可以通过 session id 重新 attach；预览面板没有关闭 session 的权限。只有 Agent/backend 显式调用 `terminal_close`、session 生命周期超时、backend shutdown 或 worker 异常才关闭 PTY。Run cancel 不关闭 Task 级 session。

### 7.2 与 AssistantTransport 的关系

二者边界如下：

```text
AssistantTransport
  → conversation state、Run 状态、工具调用状态

Terminal WebSocket
  → PTY output、session status、重连 cursor
```

不要把每个终端 output chunk 写成 Assistant Transport 的 message 或 snapshot mutation。否则会造成对话 snapshot 膨胀、stream 终态耦合和重连语义混乱。

`terminal_start` 等 Agent 工具调用仍然可以通过现有 ToolDefinition 注册；工具结果只返回 session identity、有限的文本摘要和状态。终端面板通过 WebSocket 显示完整交互内容。

## 8. Frontend 设计

### 8.1 终端面板

新增独立的 React 终端组件，例如：

```text
apps/desktop/components/terminal/
  terminal-panel.tsx
  terminal-connection.ts
  terminal-session-store.ts
  terminal-types.ts
```

职责：

- 仅 attach 已由 Agent 创建的 session；session 的创建和关闭由 Agent/backend 负责；
- 初始化 xterm.js；
- 将 output 写入 xterm.js；
- 断线重连和 seq 恢复；
- 展示 starting/running/exited/interrupted/failed。

第一阶段终端面板是只读预览：不注册 xterm.js `onData` 输入处理，不把键盘事件发送到 backend，不通过 `ResizeObserver` 改变 PTY，也不提供 Ctrl+C、signal 或 close 控件。面板尺寸只影响本地渲染；PTY 使用 `terminal_start` 的固定初始尺寸，并通过 status 消息同步给预览端。

前端预览不得执行 `terminal_start/write/read/signal/close` 这些状态改变或 Agent 观察操作；它只消费 `terminal-preview-v1` 的 `attached/output/status/exit/resync_required` 消息。工具 renderer 可以发出“打开预览”的 UI 事件，但实际 WebSocket 连接由 Task 级 terminal store 统一管理。

终端组件可以放在 Task 页面旁边，但不应因为普通 workspace 刷新而无故卸载。session id 是 backend 返回的事实，不放入 Assistant UI 的 message content 作为唯一来源。

### 8.2 Assistant UI 工具 renderer

现有 `tool-part.tsx` 已经按 `display_data.kind` 和 presentation 语义路由工具。交互终端不需要改变这个通用路由原则：

- `terminal_start`：显示 session 创建状态、shell、cwd、打开/聚焦只读预览按钮；
- `terminal_write`：显示发送状态、输出增量或“已发送”；
- `terminal_close`：显示终态和 exit code；
- 未知或不完整的 terminal display data 走 `ToolFallback`。

如果需要从 tool renderer 打开终端面板，使用 Task 级 terminal store/事件桥；不要让 renderer 直接创建 WebSocket 或读取 backend 状态。

Assistant UI 继续使用现有 `AssistantRuntimeProvider`、Thread 和工具 part renderer。不要为终端单独创建第二个 AssistantRuntimeProvider，也不要把终端会话伪装成一个新的 conversation thread。

这里的工具 renderer 是项目现有 `ToolPart`/`display_data.kind` 路由的自定义实现，不把 assistant-ui 的 toolkit executor 当作 backend session manager。assistant-ui 只消费工具调用的状态和结果；终端 session 的创建、WebSocket 连接和进程控制仍由项目 backend/frontend terminal boundary 负责。

## 9. 取消、并发和背压

### 9.1 同一个 session 的操作串行化

同一 session 的 Agent `write`、`resize`、`signal`、`close` 必须经过 session 级操作锁或 actor mailbox 串行执行。前端 WebSocket 不执行状态改变操作，因此第一阶段不存在“用户输入与 Agent 输入”的竞争；仍需避免 close 与 Agent write 竞争、旧 output 覆盖新状态等问题。

`TaskRuntimeSpace` 不保护 WebSocket attach、subscriber 投递、PTY output 读取，也不应包住长时间的 `terminal_read`。多个 preview 可以同时 attach 同一个 session；不同 session 可以并行。`terminal_read` 与 preview 都从同一个有界 ring buffer 按各自 cursor 读取，不能使用会互相吞数据的共享消费队列。

不同 session 可以并行，但应设置 backend 级最大 session 数。第一阶段建议将以下值作为可配置默认上限，而不是散落在 handler 中：最大 active session 数 32；每个 session ring buffer 1 MiB；每个 preview subscriber 256 帧且不超过 1 MiB；单次 Agent input 64 KiB；单个 worker output chunk 16 KiB；preview 控制帧 8 KiB；session max lifetime 8 小时；无 Agent/worker 活动 idle timeout 30 分钟。

### 9.2 输出背压

PTY 输出可能远快于 UI 或 Agent 的消费速度。必须采用有界策略：

- 每个 session 使用有限 ring buffer；
- PTY reader → session ring buffer → 每个 subscriber 独立有界发送队列；
- WebSocket 写入阻塞时，不阻塞 PTY reader；
- subscriber 队列满时标记 gap、尽力发送一次 `resync_required` 并关闭慢连接，要求带 cursor 重连；
- 旧数据被丢弃时递增 seq，并发送 `resync_required`；
- Agent `terminal_read` 使用 `after_seq`，不能根据文本内容猜测增量；
- UI 可直接消费流，不把所有内容复制进 React render state。

### 9.3 Agent 工具调用不等待 shell 终止

交互工具不能沿用现有 `timeout_seconds=120` 的单次调用模型。建议：

- `terminal_start` 有较短启动超时；
- `terminal_write/read` 有单次等待上限；
- session 自身有 idle timeout 和 max lifetime；
- Agent 通过多次 `read` 继续观察；
- `ToolRuntimeDependencies` 注入当前 Run 的 `is_cancelled` callback；`terminal_read` 使用不超过 50ms 的条件等待/轮询检查它，取消后立即返回 cancelled observation，不依赖现有 thread runner 的硬杀能力；
- Run 取消时只取消当前 `terminal_write/read` 操作的等待，不自动关闭 Task 级 session；关闭必须显式调用 `terminal_close`，或由 session 生命周期策略触发；
- `execution_mode="thread"` 不承诺终端工具 handler 的 OS 级强杀，OS 级清理始终由独立 Terminal Worker 的 close/owner-pipe 机制负责。

## 10. 实施顺序

### Phase 1：Rust PTY worker 和独立 session 核心

1. 增加 `ShellSpec` 和 `ShellResolver`。
2. 增加 `InteractiveTerminalBackend` 契约。
3. 使用 Rust `portable-pty` 实现同一个 Windows ConPTY/macOS/Linux Unix PTY worker。
4. 定义 worker handshake、长度前缀控制帧、heartbeat、EOF、退出和强制终止协议；owner pipe 只保留 backend 写端、worker 读端，确保 backend 崩溃时 EOF 可达。
5. 增加 session registry、operation lock、subscriber queue 和 backend→worker input backpressure。
6. 增加 session 元数据 migration/CRUD。
7. 将 `TerminalSessionService` 装配到 backend lifespan 的初始化和关闭路径，并把 service/Run cancel callback 注入 `ToolRuntimeDependencies`。
8. 增加 backend shutdown 清理和 orphan session recovery；验证 backend 崩溃时 worker 通过 owner pipe EOF 自清理。

### Phase 2：Backend API 和 Agent tools

1. 增加 `TerminalSessionService`。
2. 增加 `terminal_start/write/read/signal/close` 工具定义。
3. 增加本机 WebSocket attach endpoint。
4. 将 WebSocket 断连与 session close 分离，并拒绝除 attach 外的终端操作 frame。
5. 为每个 output chunk 引入 seq、ring buffer 和 resync 语义。

### Phase 3：React/xterm.js

1. 引入并锁定 `@xterm/xterm` 及必要 addon。
2. 实现 terminal connection/store/panel。
3. 接入动态 backend base URL、WebSocket query trace_id 和日志边界。
4. 接入只读 output、status、重连和终态展示；不接入键盘输入、远程 resize、Ctrl+C 或面板 close。
5. 在现有 `ToolPart` 路由下增加交互终端工具 renderer。

### Phase 4：验证和兼容收口

1. 保证现有 `execute_terminal` 行为和测试不回归。
2. Windows/macOS/Linux CI 分别验证 shell resolver、Rust worker 和 PTY 行为。
3. 验证 Tauri backend restart 后没有残留 worker，并验证 `bundle.resources` 的 target-triple 路径解析。
4. 验证 Assistant Transport 不被终端输出拖慢或污染。
5. 验证 worker sidecar 在 Windows/macOS/Linux 开发和生产打包中的路径解析与启动。

## 11. 验收标准

### 功能验收

- Windows 默认可以打开 PowerShell；如果存在 `pwsh.exe`，可以显式选择 PowerShell Core。
- macOS 默认可以打开用户 `$SHELL`，至少验证 zsh。
- Linux 默认可以打开用户 `$SHELL`，至少验证 bash。
- 可以执行 `python`/Node REPL，并发送下一次输入。
- 可以运行需要 TTY 的程序并看到 ANSI/光标行为；至少验证 `python -i` 和一个颜色输出命令。
- Agent 可以通过 `terminal_signal("interrupt")` 中断正在运行的命令。
- shell 正常退出后 session 进入终态并返回 exit code。
- WebSocket 断开后 session 仍存活，重连可以从 seq 继续观察。
- 前端 WebSocket 只发送 attach，不发送 input/resize/signal/close；预览面板不会改变终端状态。
- backend 重启后旧 session 不会被错误恢复或重放，数据库状态为 interrupted。

### 架构验收

- `execute_terminal` 仍然是一次性命令，不承担 session registry。
- React 不启动或杀死 shell 进程。
- Assistant UI 不保存完整 PTY output，也不把终端 chunk 写入 conversation snapshot。
- Terminal Worker 不直接访问 Task/Run/SQLite 业务事实。
- 所有终端 API 使用动态 backend 地址和本机 loopback 边界。
- session 的任务归属、并发、关闭和旧 generation 事件都有测试。
- Task 锁不被 WebSocket attach、output 投递或 `terminal_read` 持有；多个 preview 可同时 attach 同一 session，且 `terminal_read` 与 preview 不互相消费输出。
- 关键启动、退出、异常、重连和清理路径使用结构化日志。

### 测试矩阵

后端单元测试：

- shell resolver 的 Windows/POSIX 分支；
- session 状态机；
- operation 串行化；
- output seq/ring buffer/resync；
- worker EOF、异常退出和超时；
- cancel/close 与并发 write 的竞态；
- Task lock 与 WebSocket attach/发送的隔离；多个 preview 和不同 session 并发；
- backend shutdown orphan recovery。

平台集成测试：

- Windows PowerShell 5.1 / 可用时 PowerShell 7；
- macOS zsh；
- Linux bash；
- 中文输入输出；
- Ctrl+C；
- cols/rows；
- REPL 多轮输入；
- 子进程树清理。
- worker locator、Tauri resources、环境变量注入和真实 shutdown ordering。

前端 Vitest：

- terminal attach/output/status protocol encode/decode；
- reconnect 和 seq 恢复；
- xterm store 不把全部输出写进 React state；
- ToolPart 对新 terminal display data 的路由和 fallback。

前端 Playwright：

- 使用现有独立内存测试服务验证连接、断连、重连、面板状态和工具 renderer；
- 验证 preview 面板不发送 input/resize/signal/close，多个 preview 可同时订阅同一 session；
- 不把当前 Playwright E2E 当作真实 Tauri/ConPTY 集成测试。

## 12. 明确不采用的方案

1. 不把 `shell=True + PIPE` 扩展成所谓交互终端。
2. 不在 React 中用 `child_process`、Tauri command 或直接 SQLite 绕过 backend。
3. 不在第一阶段实现终端面板键盘输入、WebSocket input、signal 或 close。
4. 不把每次终端输入编码成 Assistant UI 普通用户消息。
5. 不把 PTY output 全量存到 conversation snapshot 或 LangGraph checkpoint。
6. 不通过 Assistant SSE 模拟终端输出流。
7. 不新增公网 WebSocket 服务、认证、多租户、Redis、Postgres 或远程队列。
8. 不让终端 worker 拥有业务状态；worker 只是 PTY/子进程执行底座。

## 13. 后续实现任务拆分

建议后续按以下互不重叠的写入范围实施：

1. Rust PTY worker 和 shell resolver；
2. backend session service、storage、API/WebSocket；
3. frontend terminal connection/store/panel；
4. Assistant UI terminal tool renderer；
5. 跨平台集成测试和生命周期验收。

当前已完成 backend 侧的持久化 metadata、session registry、cursor/ring buffer、只读 WebSocket attach、Task 删除清理、Run 取消等待和隐藏 tool handler。上述 handler 已实现但没有加入 `ToolSystem`，因此当前 Agent 不可见；每个 handler 的 docstring 都明确记录了该门禁，待 worker/跨平台集成测试通过后再单独提交注册变更。

剩余实施顺序：

1. Rust `portable-pty` worker sidecar 与 Tauri resource/env 装配；
2. 用真实 Windows/macOS/Linux worker 做启动、输入、VT/ANSI、signal、退出和进程树清理验收；
3. 前端 terminal store/panel 只连接本 WebSocket preview；
4. 充分集成测试后，再把隐藏 handler 显式注册进 Agent tool registry。

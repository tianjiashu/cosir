# 交互终端一期：Agent 只读与填入

> 状态：已实施（2026-11-10，提交 `68399308`）。
> 上游：[`terminal-ownership-redesign.md`](terminal-ownership-redesign.md)。本文是它的一期，只做「收窄 Agent 对交互终端的能力」，不做「请你执行」，也不做统一读取（二期，见 [`terminal-unified-read-plan.md`](terminal-unified-read-plan.md)）。
> 不在范围：`execute_terminal`、沙箱、权限、浏览器交接。

---

## 第一部分：方案（按 AGENTS.md 交付模板）

【任务类型】功能改造（删除 Agent 的终端创建、关闭、交接能力；把输入收窄为「填入不执行」）。

〔功能改造〕

- **现状 → 终态**
  - 现状：交互终端同时是 Agent 的工作台和人的终端。Agent 能新建（`terminal_start`）、关闭（`terminal_close`）、交给用户（`terminal_handoff`），能往任何会话输入整行、文本、按键、信号（`terminal_write`）。为此有一整套人机仲裁：等用户停手、拒绝写进用户的程序、幂等缓存、交接登记、交接中 shell 退出时代为驳回、Agent 会话结束通知。
  - 终态：
    - 终端只由用户在终端面板里创建和关闭。
    - Agent 只有三个工具：
      - `terminal_list`：列出用户的终端。
      - `terminal_read`：读取，保留全部现有能力。
      - `terminal_write`：**只能把一行命令填进用户终端的提示符，不能执行**。只在 shell 集成报告「停在提示符」、且这个提示符出现后还没有任何输入时允许。不带等待。
    - 回车只能由用户按。
  - 动机：一个 shell 的沙箱在启动时定死，「Agent 操作要沙箱、人操作不要沙箱」在共用的 shell 上无法同时成立。所以 Agent 不再在交互终端里执行任何东西，Agent 的执行只走 `execute_terminal`（以后进沙箱）。顺带删掉为人机共用而存在的仲裁与交接机制。
- **行为账**
  - 不变：
    - 用户创建、关闭、输入（`Raw`，从不被拒）、缩放、重连。
    - 屏幕模型与 shell 集成。
    - `terminal_read` 的参数、等待条件与输出格式。
    - `terminal_list` 的输出（除「谁创建的」）。
    - worker 两种模式、进程归属与清理。
    - 浏览器交接（`browser_handoff`）的全部行为。
    - snapshot 顶层 `terminal_sessions`（除 `createdBy`）与 `handoffs`（只剩浏览器条目）。
  - 有意改变：
    - Agent 不能新建、关闭、交接终端。
    - Agent 不能回车、不能发按键与信号。
    - `terminal_write` 只填一行到空提示符，并立即返回，不等输出。
    - 删除 Agent 会话结束通知（`terminal_session_ended`）。
    - 删除 `terminal_sessions[].createdBy` 与标签栏的创建者图标、交接标记。
    - 删除 worker 握手里的 `capabilities`。
- **消费方**：见第二部分各步骤，三个方向（Rust worker、后端、前端）的全部消费方都已列出。
- **分阶段**：一次交付，因为工具、协议、前端契约必须同时变。内部按第二部分的顺序实施，每步有可单独运行的测试。被取代的路径全部在本次删除。

【链路复用】

- 直接复用：
  - 用户终端的创建与关闭（`service/terminal/terminal_workbench_service.py:32-70`、`api/terminal_sessions_api.py:92-123`）。
  - 读取与等待（`session_runtime.read` 550-629、`_wait` 633-661、`wait.py`）。
  - shell 集成维护的提示符状态 `observation.phase`（`observation.py:97,111,115,136`）。
  - 前端 `input_mark{by:"agent"}` 帧与终端里的 Agent 输入色条（`session_runtime.py:496-497`、`apps/desktop/components/terminal/terminal-view.ts:259-260,308-314`），帧名与前端都不变。
  - Rust 的 `printable_bytes`（`apps/terminal-worker/src/protocol.rs:172-180`）。
- 改造后复用：
  - `_admit_agent_input`（`session_runtime.py:724-781`）收窄为「填入准入」。
  - Rust `TerminalInput::Text` 改名为 `Prefill`，加 bracketed paste 包裹。
  - `TerminalScreen` 里现成的「把模式位写进原子量」做法（`apps/terminal-worker/src/terminal_screen.rs:130-133`，现在写的是 `APP_CURSOR`），改为写 `BRACKETED_PASTE`。
- 无可复用：「这个提示符出现后有没有输入过」这一事实，现在没有，要新增（见第二部分步骤 4）。

【问题清单】

| # | 现象 | 证据 | 层级 | 置信度 / 处理 |
|---|---|---|---|---|
| Q1 | 删除交接后，`handoff_registry.settle` 与 `on_resource` 不再有调用方 | `settle` 只被 `session_runtime.py:321` 调用；`on_resource` 只被 `session_runtime.py:292,879` 调用 | 死代码 | 高。实施时 grep 确认浏览器不用后删除 |
| Q2 | `HandoffSurface` 只剩 `"browser"` 一个取值，`surface` 字段成了单值字段 | `core/tool/handoff/handoff_registry.py:34`、前端 `lib/assistant/contract.ts:313` | 单实现抽象 | 高。本次只把类型收窄到 `"browser"`，不删字段（见【刻意不做】） |
| Q3 | 现有幂等缓存（`operation_id` / `_accepted_writes`）防的是重试导致重复输入 | `session_runtime.py:470-511` | 契约 | 高。新规则「提示符上已有输入就拒绝」天然防住重复填入，缓存与 `operation_id` 参数一并删除 |
| Q4 | 大量会话测试靠 Agent `write` 整行来驱动 shell | `tests/test_terminal_session_service.py` 等 | 测试依赖 | 高。改用用户输入（`runtime.user_input(...)`）或 worker 的 `raw` 帧驱动，见步骤 10 |
| Q5 | Windows 的 PSReadLine 与 ConPTY 下 bracketed paste 模式位能否读到 | 未实测 | 平台 | 低。读不到时退回直接写入可打印文本，仍不会执行。记入 AGENTS.md Windows 待办 |

【选定路线】

- 结论：删掉三个工具与仲裁、交接、结束通知；`terminal_write` 收窄为填入。准入由「提示符状态 + 提示符上是否已有输入」两个事实判定：前者已有，后者新增一个布尔值。
- 被否方案：
  - **保留 `terminal_write` 全部输入种类，只禁止回车**：按键与信号在程序前台照样能触发动作（比如一个等单键确认的程序），守不住「不执行」。
  - **保留幂等缓存**：被「已有输入即拒绝」覆盖，保留就是两套机制防同一件事。
  - **保留打字等待**：「已有输入即拒绝」同样覆盖用户敲了半行的情况；用户只是刚敲完回车、提示符为空时，填入不会和用户的输入混在一起，不需要等。删除。
  - **删掉 `surface` 字段**：要改浏览器交接的后端登记、投影、前端 store 与面板唤起，本次不碰浏览器，见【刻意不做】。

【改动账本】（粗估，按文件）

- 精简：
  - 后端工具入口：删除 `terminal_session/start.py`（159）、`close.py`（68）、`handoff.py`（107），约 −334。
  - `session_runtime.py`：删 Agent 执行类输入、signal、幂等缓存、Agent 等待计数、交接、结束通知、`wait_for_integration`、打字等待，约 −260。
  - `inputs.py`：删 `InputLine` / `InputKeys` / `InputSignal`、平台键与信号、`require_input_capability`、`DEFAULT_WRITE_WAIT`，约 −130。
  - `wait.py`：删只为写入存在的 `after_*` 参数，约 −15。
  - `errors.py`：删 `Signal*` 两个错误与过时的 advice，约 −25。
  - `render.py`：删 `handed_off`、「by {created_by}」、`render_end_notice`，约 −40。
  - `session_effects.py`：整个文件删除（30）。
  - `session_service.py`：删 Agent 会话上限与 `close(by=)` 分支，约 −15。
  - `handoff_registry.py`：删 `settle`、`on_resource`（Q1），约 −40。
  - `worker_client.py`：删 signal 与 capabilities，约 −70。
  - `notice.py`：删 `terminal_session_ended`，约 −5。
  - `constant.py`：删 5 个常量，约 −10。
  - `terminal_display.py`：删只给 start / close 的 `build_terminal_session_display_data`，约 −30。
  - Rust：`protocol.rs` 删 Line / Keys / KeyName / COMMON_KEYS / Signal / SignalResult 及测试，约 −260；`session.rs` 删 Signal 处理与 capabilities，约 −45；`pty.rs` 删 signal 相关，约 −80；`exec.rs` 删 Signal 分支，约 −8。
  - 前端：工具目录三条、交接 terminal 分支、标签栏创建者与交接标记、attention-card 终端分支、`createdBy` 契约与校验，约 −120。
  - 测试：约 −1100（以实施时为准）。
  - 合计约 −2600。
- 新增：
  - `observation.py`：加一个布尔值，约 +10。
  - `session_runtime.py`：填入准入与填入路径，约 +50。
  - `inputs.py` / 参数模型：`TerminalWriteArgs` 改为 `session_id` + `command`，约 +15。
  - `errors.py`：新增填入被拒错误，约 +15。
  - Rust：`Prefill` 与包裹、bracketed paste 原子量及测试，约 +70。
  - 测试辅助（用用户输入驱动 shell）：约 +30。
  - 新增测试：约 +250。
  - 合计约 +440。
- 净变化：约 −2150。交付后以 `git diff --stat` 对账。
- 命名：
  - 本文 `docs/plan/terminal-agent-read-prefill-plan.md`：正向检索「终端 Agent 读取 填入」能落到它；反向读出「终端里 Agent 只读与填入的实施方案」。
  - Rust 输入种类 `Prefill`：名字即「填入、不执行」。
  - 工具名 `terminal_write` 保留（用户决定），描述写清只填入。
  - 不新增目录或代码文件，删除 4 个文件（三个工具入口 + `session_effects.py`）。
- 同步：
  - AGENTS.md 三处（步骤 9）。
  - `tool_ui_display_contract.md`、`rt/interaction_contract.md`。
  - `terminal-process-unification-design.md` 的状态标注（另两份设计稿的状态已由主设计会话改好）。

【刻意不做】

- 「请你执行」：用户决定先不做。Agent 需要用户做事时，在回复里写出命令，或在用户要求时用 `terminal_write` 填进用户的终端。
- 统一读取（运行中的后台命令也能用 `terminal_read` 读）与 `terminal_list` / `terminal_read` 开放给子 Agent：二期。
- 删掉 `HandoffSurface` / `surface` 字段（Q2）：会动到浏览器交接的五处代码，本次不碰浏览器，留作单独评估。
- 多行填入：一行限制在 Rust 与参数模型两侧都守住；多步操作让 Agent 先写脚本再填一行命令。
- 沙箱、权限、`execute_terminal` 的任何改动。

---

## 第二部分：实施手册（给执行者）

### 0. 开工前必读

1. 读仓库根目录 `AGENTS.md`，至少读「开发约定」整节，以及「进程生命周期边界」里交互终端那一段、「工具 UI 渲染边界」里交接那一段。本仓规则要点：
   - **docstring 与注释一律中文**，只写不明显的信息（为什么、上下游契约），不复述代码。
   - **日志**用 `log.info("snake_case_event", extra={"msg": "中文说明", "data": {...}})`。
   - **禁止**用 `getattr(obj, "方法名", None)` 探测本仓对象的方法。
   - 绿地项目，不做任何兼容：旧帧、旧字段、旧工具名直接删掉，不留别名。
   - 删除功能时同步删掉只为它存在的代码、常量、注释与测试，**不留死代码**。
2. **不要提交 git**，不要改本文没提到的文件。遇到本文没覆盖、或与代码事实冲突的情况，**停下来问**，不要自己发明方案。
3. 开工前先通读这些文件：
   - `apps/backend/app/tools/terminal_runtime/session_runtime.py`（全文）、`observation.py`、`inputs.py`、`wait.py`、`errors.py`、`render.py`、`session_service.py`、`worker_client.py`、`session_effects.py`。
   - `apps/backend/app/tools/tool_handler/terminal_session/` 全部文件。
   - `apps/backend/app/core/tool/handoff/handoff_registry.py`。
   - `apps/terminal-worker/src/protocol.rs`、`session.rs`、`terminal_screen.rs`、`exec.rs`、`pty.rs`。
4. 每删除一个符号，都用 `grep -rn <符号> apps/` 确认没有遗留引用，包括测试与注释。

### 1. Rust worker

**`apps/terminal-worker/src/protocol.rs`**

- `TerminalInput`：删除 `Line`、`Keys` 两个变体；`Text { text }` 改名为 `Prefill { text }`（serde 的 `type` 取值随之变成 `"prefill"`）；`Raw` 不变。更新枚举上方的文档注释：`Prefill` 是 Agent 唯一的输入，只填进提示符、绝不执行；`Raw` 是用户输入。
- 删除 `KeyName` 整个枚举及其 `supported` / `capability` / `encode` 方法、`COMMON_KEYS`、`MAX_KEYS_PER_INPUT`。
- 删除 `ControlFrame::Signal` 与 `WorkerEvent::SignalResult`，同步更新 `WorkerEvent::Error` 注释里列出的错误码。
- `printable_bytes(text, reserved)`：去掉 `reserved` 参数（只有 `Line` 的回车用它）。
- `encode_input(input, app_cursor)` 改为 `encode_input(input, bracketed_paste: bool)`：
  - `Prefill { text }`：
    - 空串拒绝。
    - 经 `printable_bytes` 拒绝任何控制字符。这条规则保证文本里不可能出现 `ESC`，所以包裹不会被提前结束。
    - `bracketed_paste` 为真时，写入 `b"\x1b[200~" + text + b"\x1b[201~"`；为假时，直接写入 text。
    - 文档注释写明：为什么包裹（粘贴模式下 shell 不触发用户自定义的按键绑定），以及为什么不包裹也安全（不含控制字符，写入后不会执行）。
  - `Raw` 不变。
- 测试（同文件 `#[cfg(test)]`）：
  - 删除 `reads_signal_frame_with_request_id`、`encodes_signal_result_event`、`complete_line_*`、`cursor_keys_follow_app_cursor_*`、`keys_count_and_platform_*`。
  - `text_sends_printable_*` 改为 `prefill_*`：断言不包裹与包裹两种字节；断言含 `\n`、`\r`、`\x1b`、`\x03` 的文本被拒；断言空串被拒。
  - `rejects_old_inputs_*` 改写：断言 `{"type":"line"}`、`{"type":"keys"}`、`{"type":"text"}` 都是未知类型、反序列化失败。
  - `handshake_carries_pty_fields_only_in_session_mode`：去掉对 `"input_keys"` 的断言（capabilities 删除，见下）。

**`apps/terminal-worker/src/terminal_screen.rs`**

- 把 `app_cursor` 这一套（字段约第 90 行、构造约第 113 行、`app_cursor_flag()` 约第 119-121 行、`advance()` 里约第 130-133 行的 store）整体换成 `bracketed_paste`：同样是 `Arc<AtomicBool>`，在 `advance()` 同一处写入 `self.terminal.mode().contains(TermMode::BRACKETED_PASTE)`。
- 测试 `app_cursor_flag_follows_decckm` 改为 `bracketed_paste_flag_follows_mode`：喂 `\x1b[?2004h` 后为真，`\x1b[?2004l` 后为假。
- 注释写明：模式位反映的是读线程**已解析**到的输出，有固有延迟；即使读到旧值，也只影响包不包裹，不影响「不执行」。

**`apps/terminal-worker/src/session.rs`**

- import 去掉 `KeyName`。
- 约第 64 行取 flag 改为取 `bracketed_paste_flag()`；约第 109 行 `encode_input(input, bracketed_paste.load(Ordering::Relaxed))`。
- 删除 `ControlFrame::Signal` 的处理分支（约第 160-182 行）。
- 删除握手里的 `capabilities`（约第 462-483 行的 `capabilities()` 与 `platform_signals()`），以及握手帧里对应的字段与测试 `handshake_advertises_input_keys_and_platform_keys`（约第 490 行）。握手帧的字段定义在 `protocol.rs`，一起删。

**`apps/terminal-worker/src/exec.rs`**：删除 `ControlFrame::Signal` 分支（约第 215-221 行）。`Write` 分支照旧回 `input_rejected`。

**`apps/terminal-worker/src/pty.rs`**：删除只服务信号的 `TerminalControlResult`（约第 53-57 行）与 `signal` / `send_foreground_signal` / `send_eof`（约第 128-199 行）。

**`apps/terminal-worker/tests/exec_mode.rs`**：`input_frames_are_rejected_without_stopping_the_command`（约第 153 行）改为发送一个 `prefill` 写入帧，断言被拒且命令照常运行；删除发送 signal 帧的部分。

### 2. Python worker 客户端

**`apps/backend/app/tools/terminal_runtime/worker_client.py`**

- 删除 signal 全部代码：`SIGNAL_ACK_TIMEOUT_SECONDS`、`_PendingSignal`、`_signal_lock` / `_pending_signals`、`signal()`、`close` 里对未决信号的收敛、读线程的 `signal_result` 分支、相关错误类 import。
- 删除 `capabilities` 字段、属性与握手时的保存（约第 101、114-118、463-467 行）。
- `write` 的 docstring 改为只列 `prefill` 与 `raw`；`_enqueue` 注释去掉 signal。

**`apps/backend/app/tools/terminal_runtime/errors.py`**：删除 `TerminalWorkerSignalFailedError` 等 `Signal*` 错误（约第 134-148 行）。

### 3. 输入与参数模型

**`apps/backend/app/tools/terminal_runtime/inputs.py`**

- 删除 `InputLine`、`InputText`、`InputKeys`、`InputSignal`、`keys_for_platform`、`signals_for_platform`、`_host_*_schema`、`DEFAULT_WRITE_WAIT`、`require_input_capability`。
- 保留 `TerminalWait`（read 使用）与 `_reject_control_characters`（参数模型复用）。
- `summarize_input` 改为只摘要一条填入命令，或直接删除、在调用处用命令前若干字符。按你读完代码后最简单的方式定，在报告里说明。
- 模块 docstring 去掉 `terminal_handoff` 与多种输入的说明。

**`apps/backend/app/tools/tool_models/terminal_session_args.py`**

- 删除 `TerminalStartArgs`、`TerminalHandoffArgs`、`TerminalCloseArgs`，以及 `tool_models/__init__.py` 里的导出。
- `_SESSION_ID_DESCRIPTION` 改为 `"Session ID from terminal_list."`。
- 模块 docstring 改为「交互终端工具的输入契约（list / read / write）」。
- `TerminalWriteArgs` 改为只有两个字段（删除 `operation_id`、`input`、`wait`）：

  ```python
  class TerminalWriteArgs(BaseModel):
      model_config = ConfigDict(strict=True, extra="forbid")

      session_id: str = Field(min_length=1, max_length=128, description=_SESSION_ID_DESCRIPTION)
      command: str = Field(
          min_length=1,
          max_length=Constant.Terminal.MAX_AGENT_INPUT_BYTES,
          description=(
              "One command line to place at the prompt, without a line break or control "
              "characters. It is not run: the user decides whether to press Enter."
          ),
      )
      # 用 inputs._reject_control_characters 校验 command（field_validator）
  ```

### 4. 「提示符上已有输入」事实 + 会话运行时

**`apps/backend/app/tools/terminal_runtime/observation.py`**

- 在观察对象上新增一个布尔值 `prompt_has_input`，初始 `False`。
- `shell_event` 的 `shell_prompt` 分支（约第 112-115 行）把它复位为 `False`。
- 加一个方法 `note_input()` 把它置为 `True`，由会话运行时在输入被接受后调用。
- docstring 写明语义：这个提示符出现之后，用户或 Agent 是否已经往这一行写过东西。它是「填入」准入的依据，也替代了原来的幂等缓存：同一个提示符至多填一次。

**`apps/backend/app/tools/terminal_runtime/session_runtime.py`**

1. `user_input`（约第 783-803 行）：在写入成功后、与 `_user_last_input` 同一把锁内，调用 `self._observation.note_input()`。
2. 改写 `write` 为填入（建议改名为 `prefill(self, command: str, *, run_id, tool_call_id) -> TerminalObservationResult`，调用方同步改）：
   - 持锁：先调用收窄后的准入（下一条），再 `self._observation.note_input()`、`self._last_input_by = "agent"`、`self._stream.publish("input_mark", by="agent")`。
   - 锁外：`worker.write({"type": "prefill", "text": command})`。
   - 再持锁：清 `self._last_read = None`，记日志 `terminal_agent_prefill_accepted`（`task_id`、`run_id`、`session_id`、`tool_call_id`、`command_preview`：前 200 字符）。返回 `self._tail_result(note="filled into the prompt; it runs only if the user presses Enter", input_type="prefill", input_summary=<命令摘要>)`。
   - **不等待**：删除 `wait` 参数、`WaitBaseline` / `WaitEvaluator` 的调用、`_require_wait_supported` 在写入中的调用。
   - 删除 `operation_id`、`_accepted_writes` 及 `MAX_WRITE_OPERATION_CACHE` 相关代码（Q3）。
3. `_admit_agent_input` 收窄（建议改名 `_admit_prefill`），持锁调用，任一条件不满足就抛新错误 `TerminalPrefillRejectedError`（步骤 5），消息写明原因：
   - `self._observation.integration_status != "ready"`：「this shell has no shell integration, so its prompt cannot be detected」。
   - `self._observation.active_command is not None` 或 `phase != "editing"`：「a command is running in this terminal (or it is not at its prompt)」。
   - `self._observation.prompt_has_input`：「the prompt already has input (typed by the user or filled earlier)」。
   - 删除打字等待循环、`AGENT_TYPING_WAIT_SECONDS`，以及「前台是用户命令」的专门分支（已被上面第二条覆盖）。
4. 删除：
   - `_deliver_agent_input`（填入直接 `worker.write`）。
   - `begin_handoff` / `end_handoff`（约第 665-712 行）。
   - `finish` 里的交接查询、结束通知与 `settle` 调度（约第 291-326 行）：保留状态迁移与 `terminal_session_ended` **日志**（日志不是通知），去掉 `announced` 字段。
   - `_agent_waits` 计数。
   - `_facts` 里的 `handed_off`。
   - `wait_for_integration`（只有被删的 `terminal_start` 与测试在用；测试需要时在 `tests/terminal_test_support.py` 里写轮询辅助）。
   - `finish` / `close` 的 `announce` 参数：删完结束通知与 `settle` 后，如果已无用途就一并删除。
   - 相关 import（`handoff_registry`、`session_effects`、`TOOL_TERMINAL_HANDOFF` 等）。
5. 保留 `_user_typing` 与 `USER_TYPING_IDLE_SECONDS`：`terminal_list` / `terminal_read` 的「user typing」状态还在用（`_facts` 约第 878 行）。
6. `created_by` 字段与构造参数删除：所有会话都由用户创建。
7. 更新模块与方法的 docstring：Agent 只读与填入；交接、Agent 输入仲裁的描述全部删除。

**`apps/backend/app/tools/terminal_runtime/wait.py`**：`WaitBaseline.of` 删除只为写入存在的 `after_line_input` / `after_input` 参数及相关字段（read 调用时恒为 `False`），按 `False` 的行为简化。

### 5. 其他运行时文件

- **`errors.py`**：
  - 新增 `TerminalPrefillRejectedError(TerminalSessionError)`，`code = "TERMINAL_PREFILL_REJECTED"`，`advice = "Give the command in your reply instead, or ask the user to clear the prompt and try again later."`。
  - 删除不再使用的 `TerminalUserTypingError`、`TerminalUserCommandRunningError`（逐个 grep 确认）。
  - `TerminalSessionNotFoundError.advice` 改为 `"It ended earlier or the backend restarted; call terminal_list to see the user's terminals."`。
  - `TerminalSessionCapacityError`：去掉「Agent 会话上限」的说明，`advice` 改为 `"Close a terminal in the terminal panel, then retry."`。
- **`tool_handler/terminal_session/common.py`**：`_STATUS_HINTS`（约第 25-29 行）去掉被删错误，加上 `TerminalPrefillRejectedError: "无法填入"`。
- **`render.py`**：删除 `SessionFacts.handed_off` 与「handed off to user」、`render_session_summary` 的「by {created_by}」、`render_end_notice`。
- **`session_effects.py`**：整个文件删除。
- **`core/workflow/message_queue/message_bodies/notice.py`**：删除 `terminal_session_ended`（约第 16、35 行）。
- **`session_service.py`**：
  - `start` 去掉 `created_by` 参数与 Agent 会话上限分支（约第 77-83 行），全局上限 `MAX_ACTIVE_SESSIONS` 保留。
  - `close` 去掉 `by` 参数，原因恒为 `closed_by_user`。
  - 更新 `shutdown` / `delete_task_sessions` 的 docstring（去掉交接）。
  - 同步 `terminal_workbench_service.py` 的调用。
- **`app/tools/display/terminal_display.py`**：删除只被 start / close 使用的 `build_terminal_session_display_data`（约第 54 行）；动作行（约第 78 行）的 docstring 去掉交接。
- **`config/constant.py`**：删除 `MAX_AGENT_SESSIONS_PER_TASK`、`AGENT_TYPING_WAIT_SECONDS`、`MAX_WRITE_OPERATION_CACHE`、`END_NOTICE_TAIL_LINES`、`INTEGRATION_READY_TIMEOUT_SECONDS`。删之前逐个 grep 确认只有被删代码在用；有其他引用就停下来问。

### 6. 交接登记表与 snapshot

- **`core/tool/handoff/handoff_registry.py`**：
  - `HandoffSurface = Literal["browser"]`。
  - 删除 `settle`、`on_resource`（先 grep 确认浏览器与其他代码不调用）。
  - 模块 docstring 去掉终端。
  - `core/tool/handoff/` 下若有只服务终端的辅助代码，一并删除。
- **`assistant_transport/state/conversation_state_handoff.py`**：随类型收窄，校验只接受 `"browser"`。
- **`assistant_transport/state/conversation_state_terminal_session.py`**：删除 `TerminalSessionCreatedBy`、`createdBy` 字段、`_CREATED_BY` 与校验（约第 29、40、50、65、92 行）。
- **`app/tools/tool_handler/browser/handoff.py:38`**：docstring 里「同 terminal_handoff」改为直接描述浏览器交接本身。

### 7. 工具入口与登记

- 删除 `apps/backend/app/tools/tool_handler/terminal_session/start.py`、`close.py`、`handoff.py`。
- **`terminal_session/__init__.py`**：`build_terminal_definitions` 只建 list / read / write 三个。
- **`terminal_session/write.py`**：
  - `execute(self, session_id: str, command: str, *, execution_context)`，调用 `runtime.prefill(...)`，经 `observation_result` / `with_terminal_errors` 返回。
  - `timeout_seconds` 改为 `10.0`（不再等待）。
  - `needs_user_surface=True` 保留。
  - 描述（同时写中文译文注释）：

    ```text
    Fill one command line into the prompt of a terminal the user opened, without running it: the user reviews it and presses Enter themselves, and you are not told whether they did (read the terminal later to see what happened). Use it when the user asks you to put a command in their terminal. It works only while the shell is idle at its prompt with nothing typed yet (shell integration required); otherwise it fails and you can give the command in your reply instead. One line only: for several steps, write a script file first and fill in the command that runs it.
    ```

- **`terminal_session/list.py`** 描述（同步中文译文注释）：

  ```text
  List the terminals the user opened for this task, one line each: what it is doing now (at prompt, running a command, full-screen app, user typing, ended), its cwd and last command result. Also lists the installed shells and which lack shell integration. Only the user opens and closes terminals; call this before reading or filling a terminal, and when picking up work in a new run.
  ```

  并去掉输出里「谁创建的」。

- **`terminal_session/read.py`**：描述不变；`_SESSION_ID_DESCRIPTION` 已在步骤 3 改。
- **`core/tool/schemas/tool_names.py`**：删除 `TOOL_TERMINAL_START`、`TOOL_TERMINAL_HANDOFF`、`TOOL_TERMINAL_CLOSE`，三处都要删（常量、`ToolName` Literal、`ALL_TOOL_NAMES`）；`core/tool/schemas/__init__.py` 的再导出同步。
- **`core/agents/define_agents.py`**：从主 Agent 的 `allowed_tools` 删除这三个工具（约第 142、145、146 行，以及第 33-38 行附近的 import）。
- **`app/core/tool/results/tool_error.py`**：如果按工具名配了默认短提示，删掉三个工具的条目。
- **`app/tools/tool_handler/execute_terminal.py`**：`_DESCRIPTION` 最后一句「Programs that must keep running (servers, watchers) or need interaction do not fit this tool: they are stopped at the timeout.」改为「Programs that must keep running (servers, watchers) or need interaction do not fit this tool: they are stopped at the timeout, so leave them to the user.」并同步中文译文注释。

### 8. 前端（`apps/desktop`）

- **`lib/assistant/tools/tool-catalog.ts`**：
  - 删除 `terminal_start`、`terminal_handoff`、`terminal_close` 三条（约第 130、133、134 行）。
  - `terminal_write` 的 `verb` 改为 `"填入终端"`。
  - `terminalInput` 函数（约第 75-83 行）只保留 `session_id · command` 的展示（参数名从 `input.text` 改为 `command`）。
  - 测试 `tests/unit/tool-catalog.test.ts:36-38` 同步。
- **`components/conversation/tools/call-views/terminal-session-view.tsx`**：删除只服务 start / close 的分支（shell_kind、exit_code、「继承的会话记录」「已失效」，约第 68-74 行）与 handoff 注释（约第 87-89 行）；保留 list、read、write 的分支与「在终端中打开」按钮。
- **交接**：
  - `lib/assistant/contract.ts:313`：`surface: "browser"`，同步第 304-309 行注释。
  - `lib/handoff/reveal.ts:11-13`：删除 terminal 分支与对应 import。
  - `lib/assistant/transport/snapshot-validation.ts:423`：只接受 `"browser"`。
  - `components/terminal/terminal-workbench.tsx`：删除交接相关 import（约第 5、12 行）、第 88-89 行、`handedOff` 属性（约第 171 行）与提示条（约第 204 行）。
  - `components/terminal/terminal-tab-bar.tsx`：删除 `handedOff`、`HandIcon`、「交给你操作」文案（约第 14-15、30-31、48、72、79、95、97-99 行）。
  - `components/conversation/turn/attention-card.tsx`：`HANDOFF_KINDS` 只留 `"browser-page"`（约第 33 行）；删除读 `input_summary` 的终端分支（约第 27 行）；同步第 22 行注释。
- **createdBy**：
  - `terminal-tab-bar.tsx`：删除 `CreatorIcon`、Bot / User 图标与「Agent 创建 / 用户创建」title（约第 1、86、95、100 行）。
  - `lib/assistant/contract.ts:266`：删除字段，第 261 行注释「label 为 Agent 指定」改为用户终端的说法。
  - `lib/terminal/terminal-sessions.ts:35`：同上。
  - `snapshot-validation.ts`：第 267 行精确键列表去掉 `createdBy`，删除第 275 行。
  - **不要动**浏览器标签的 `createdBy`（`browser-panel.tsx:90` 与 electron 侧是另一套契约）。
- **Agent 输入标记**：帧名 `input_mark{by:"agent"}` 不变；只更新 `terminal-view.ts`、`terminal-connection.ts`、`styles/globals.css` 里的注释，说明现在只在 Agent 填入时出现。
- **注释同步**：`components/terminal/use-terminal-session-actions.ts:15-16`、`terminal-view.ts:25`、`terminal-connection.ts:30`、`lib/terminal/terminal-workbench-store.ts:15-16` 中提到交接或 Agent 新开会话的注释。
- **图标**：`tool-icons.tsx` 里的 `keyboard` 若已无引用，删除映射（`x` 仍被其他工具使用时保留；以 `npm run knip` 结果为准）。

### 9. 文档

- **`AGENTS.md`**，只改下面三处，不扩写：
  - 「进程生命周期边界」交互终端一段：
    - 「Agent 显式关闭、shell 自然退出…」改为「用户关闭、shell 自然退出…」。
    - 「人和 Agent 都可新建会话与输入，用户输入从不被拒，Agent 输入在用户打字时等其停手、前台是用户启动的命令时立即被拒」改为「会话只由用户新建；Agent 只能列出、读取，以及把一行命令填进处于提示符且尚无输入的会话（不执行，回车只能由用户按）；用户输入从不被拒」。
  - 「工具 UI 渲染边界」交接段：「交接（`terminal_handoff` / `browser_handoff`）」改为「交接（`browser_handoff`）」，删除「资源在交接中意外结束时代用户驳回（`settle`，如 shell 退出）」这一条结束路径。
  - 「Windows 待办 · 终端交互」末尾加一句：「Agent 填入的 bracketed paste 包裹在 PSReadLine / ConPTY 下未验证」。
- **`apps/backend/app/core/tool/tool_ui_display_contract.md`**：第 3 节表格删除 start / handoff / close 三行（约第 39、42、43 行）；第 78 行 `terminal-session` 的字段说明去掉只属于 start / close / handoff 的字段。
- **`apps/backend/app/tools/terminal_runtime/interaction_contract.md`**：
  - 工具清单只留 list / read / write。
  - 删除结束通知、人机仲裁、交接三节，换成一节「Agent 填入」：条件、不执行、`prompt_has_input`。
  - worker 协议部分：输入只有 `prefill` 与 `raw`，删除 signal 与 capabilities。
- **`docs/plan/terminal-process-unification-design.md`** 顶部状态行：注明「§3 的工具边界已被 `terminal-agent-read-prefill-plan.md` 取代」。
- `terminal-ownership-redesign.md` 与二期方案的状态行主设计会话已改好，不要动。

### 10. 测试

**测试辅助**（`apps/backend/tests/terminal_test_support.py`）：

- 删除 `start` 辅助的 `created_by="agent"` 默认值（约第 185-195 行）。
- 新增「以用户身份敲一行」的辅助：会话级调用 `runtime.user_input((text + "\r").encode())`；worker 级发送 `{"type": "raw", "data_base64": base64(text + "\r")}`。原来靠 Agent `write` 整行驱动 shell 的测试都改用它。
- 原来依赖 `wait_for_integration` 的测试，改用这里新增的轮询辅助（轮询 `integration_status == "ready"`，带超时）。

**删除**：

- `tests/test_terminal_session_tools.py`：start、close、handoff 的用例，以及 write 的旧 schema 与旧输入种类用例。保留并改写 list / read / write 的用例。
- `tests/test_terminal_session_service.py`：
  - Agent 会话上限。
  - Agent 会话结束通知（含「Agent 等待期间会话结束」）。
  - keys / signal / 重复 `operation_id`。
  - 打字等待的三个用例（「agent_waits_for_user_to_pause」「agent_times_out…typing」「user_input_is_never_rejected_while_agent_waits」）。
  - 「agent_rejected_at_once…」「agent_waiting…rejected…」「agent_may_type_into_its_own…」。
  - 五个交接用例（约第 557-627 行）。
  - 「match_after_line_input_skips_echo」「match_after_input_ignores_screen…」「text_input_rejects_control…」。
- `tests/test_terminal_worker_integration.py`：`test_real_worker_signal_failure_is_reported_to_caller`。
- Windows Ctrl-C 用例 `test_windows_ctrl_c_is_delivered_and_isolated_per_conpty_session`：改为用 `raw` 发送 `\x03`（用户的 Ctrl-C 本来就走原始字节），保留隔离断言。

**修改**：

- `tests/test_terminal_session_service.py` 中靠 `write` 推进命令的用例，改用用户输入辅助：`write_command_returned…`、`output_quiet_elapsed…`、`cancel_and_session_end…`、`full_screen_app…`、`resize…`、`worker_unavailable_on_commands…`。`start_list_close…` 去掉 `by=agent`。
- `tests/test_terminal_shell_integration.py`：第 66-72 行的 write 封装改为用户输入；`match_and_keys_and_interrupt` 删除 keys / interrupt 部分；`full_screen…`、`quiet_wait…` 同步改。
- `tests/test_terminal_worker_integration.py` 中发 `line` 帧的地方（约第 101、117、341 行）改用 `raw` 辅助；`test_task_shell_state_survives…`（约第 414-505 行）不再用 `TerminalStartTool`，改为经 `TerminalSessionService.start` 创建会话、用用户输入驱动。
- `tests/test_terminal_worker_input_order.py`：去掉 signal；`line` 改为 `raw`。
- `tests/test_strict_tool_contract.py`（约第 25、115、241-250 行）：按新的 `TerminalWriteArgs` 更新。
- `tests/test_task_tool_scope.py`（约第 29、83 行）、`tests/test_task_workspace_deletion.py`（约第 557、580、594 行）、`tests/test_terminal_worker_handshake_diagnostics.py:197`：去掉对被删工具名与 `created_by` 的依赖。
- 用 `terminal_session_ended` 或 `terminal_handoff` 当样本数据的测试，换成仍然存在的种类：`test_runtime_notice.py:271,311` 换成其他 `NoticeKind`；`test_turn_withdrawal.py:139,175,225`、`test_hitl_decision.py:70`、`test_dispatch_policy.py:53`、`test_tool_call_pipeline.py:328`、`test_tool_call_identity.py:196`、`test_background_registry.py:253`、`test_background_placeholders.py:92` 换成 `browser_handoff` 或其他仍存在的工具名。
- `tests/test_handoff_registry.py`：删除 `settle` / `on_resource` 用例，保留浏览器用例。

**新增**（放进 `tests/test_terminal_session_service.py`，用 fake worker，不需要真 worker）：

1. 提示符为空时填入成功：worker 收到 `{"type": "prefill", "text": ...}`；发布了 `input_mark{by:"agent"}`；返回结果含 note。
2. 同一个提示符第二次填入被拒（`prompt_has_input`）。
3. 用户在提示符上输入过字符后，填入被拒；新提示符出现（`shell_prompt` 事件）后再次允许。
4. 有命令在执行时填入被拒；没有 shell 集成时填入被拒。
5. 填入后用户回车，随后开始的命令 `started_by == "user"`。
6. 参数模型拒绝含 `\n`、`\r`、`\x1b` 的 `command`。

真 worker 集成用例（`requires_terminal_worker`，POSIX）：

- 在 zsh 或 bash 会话里，等集成 ready、停在提示符后填入 `echo PREFILLED`。
- 等待 1 秒后，断言屏幕上出现 `echo PREFILLED` 但**没有**出现单独一行的输出 `PREFILLED`（没有执行）。
- 再以用户身份发送 `\r`，断言输出出现 `PREFILLED`。

**前端测试**：

- `tests/unit/handoff-store.test.ts`：删除终端条目与「新出现的终端交接打开终端面板…」用例，保留浏览器用例。
- `tests/unit/attention-card.test.tsx`：默认 artifact 与终端相关用例换成 `browser_handoff`。
- `tests/unit/terminal-workbench-store.test.ts`：删除「Agent 新开会话不弹出面板」；修改「…Agent label 优先」（去掉 `createdBy:"agent"`）。
- `tests/unit/snapshot-validation.test.ts`：去掉 `createdBy`；终端 surface 改为预期被拒。
- `tests/unit/transport-fixtures.ts:101-107`：删除 `createdBy`。
- `tests/e2e/test-server.mjs`：删除第 706 行 `createdBy`；第 270-273 行的 `terminal_handoff` 换成 `browser_handoff`（带 `handoff_reason`）。
- 联动修改 `tests/e2e/frontend-regressions.spec.ts:180-182,196-198` 与 `tests/e2e/timeline-disclosure.spec.ts:153-154` 的断言。

### 11. 验收命令

从仓库根目录运行，全部通过才算完成：

```bash
cargo build -p cosir-terminal-worker
```

```bash
cargo test -p cosir-terminal-worker
```

```bash
cargo clippy --package cosir-terminal-worker --all-targets -- -D warnings
```

```bash
uv run --project apps/backend pytest
```

```bash
uv run --project apps/backend ruff check apps/backend/app apps/backend/tests
```

改动过的 `app/` 文件里，凡是已在严格类型检查范围内的，必须通过（在 `apps/backend` 下运行，列出你改过的文件）：

```bash
cd apps/backend && uv run mypy --config-file mypy.strict.ini <改过的 app 文件>
```

```bash
cd apps/desktop && npm run test:unit && npm run lint && npm run knip && npx tsc --noEmit
```

```bash
cd apps/desktop && npm run test:e2e
```

（E2E 用无头浏览器；不要运行 `test:e2e:desktop`，它会弹出真实窗口。）

最后做一次全仓残留检查，除了本文与其他 `docs/plan/` 的历史描述外不应有命中：

```bash
grep -rn "terminal_start\|terminal_close\|terminal_handoff\|created_by=\"agent\"\|createdBy\b.*agent\|InputKeys\|InputSignal\|KeyName\|signal_result" apps/ AGENTS.md
```

### 12. 交付时报告

1. 每一步做了什么，偏离本文的地方和理由；`summarize_input` 的处理方式。
2. `git diff --stat`，与第一部分的改动账本对账，偏差大要说明。
3. 第 11 节全部命令的真实输出（失败要贴输出，不要只说「通过」）。
4. 发现的 docstring 与代码不一致之处（只报告，不在范围外擅自改）。

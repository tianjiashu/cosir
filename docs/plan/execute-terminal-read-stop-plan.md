# execute_terminal 补齐：运行中读取、停止、给模型的输出整理

> 状态：**暂停，不要执行**。待改写为终端二期：运行中的后台命令并入 `terminal_list` / `terminal_read` 统一读取，不新增 `read_command_output`；停止改名为 `terminal_stop`。一期见 [`terminal-agent-read-prefill-plan.md`](terminal-agent-read-prefill-plan.md)。下文的输出整理器、运行中命令登记表与问题清单仍有效，改写时沿用。
> 上游：[`terminal-gap-assessment.md`](terminal-gap-assessment.md) §3 T3 / T4、§7.2 方案 A（本文是它的定稿与收窄）；[`terminal-ownership-redesign.md`](terminal-ownership-redesign.md) T1。
> 范围：只动 `execute_terminal` 一族。交互终端（`terminal_*`）、沙箱、权限都不在本次范围。

---

## 第一部分：方案（按 AGENTS.md 交付模板）

【任务类型】功能改造（给已有工具补能力 + 改模型通道的文本）。

〔功能改造〕

- **现状 → 终态**
  - 现状：后台命令只在结束时以 `<background_result>` 回投一次结果。运行中的输出只在 `_OutputCollector._chunks` 里（`apps/backend/app/tools/tool_handler/terminal/output_collector.py:85-115`），没有可寻址的持有者，Agent 读不到，也停不了自己的后台命令。给模型的正文把原始输出直接拼进去，带着颜色码和进度条控制字符（`execute_terminal.py:580-602` 的 `_render_content`）。
  - 终态：
    - 运行中的命令由进程内登记表 `running_commands` 按 `(task_id, command_id)` 持有，`command_id` 就是那次 `execute_terminal` 调用的 `tool_call_id`。
    - 新增两个工具：`read_command_output`（读运行中的输出，可等待某行出现）、`stop_command`（停止）。
    - 所有给模型的命令输出（前台结果、后台结论、读取结果）都经同一个整理器去掉控制序列、按 `\r` 覆盖。卡片展示仍是原始输出。
  - 动机：Agent 跑开发服务器、watcher 时可以等它就绪、看日志、用完关掉；子 Agent 与 Team 节点也能做到（它们没有交互终端）。
- **行为账**
  - 不变：
    - `execute_terminal` 的参数、前台 / 后台 / 用户转后台 / 结束推结果的语义。
    - 超时（默认 300 秒，最长 3600 秒，后台命令同样会在超时被结束）。
    - 结局分类与标记（`[exit_code=N]` 等）。
    - 卡片展示数据（原始输出、带颜色）。
    - 输出预算（头尾截断 + 全文落盘）。
    - 后台登记表的投递、撤销、关闭语义。
  - 有意改变：
    - 模型看到的命令输出不再含 ANSI 控制序列，进度条只留最终状态。
    - 后台占位文案多一个 `command_id`。
    - `execute_terminal` 与 `terminal_start` 的工具描述改写（服务 / watcher 的分工）。
    - `background_registry.request_user_cancel` 改名为 `request_cancel`（Agent 也会用它）。
- **消费方**：见第二部分各步骤的「改哪里」。全部调用方都已列出。
- **分阶段**：一次交付，内部按第二部分的步骤顺序实施；每步有可单独运行的测试。没有被取代的旧路径需要删除（只有 `_render_content` 里的结局标记表搬到 `ExecutionResult`）。

【链路复用】

- 直接复用：
  - 后台登记表的单项取消（`background_registry.request_user_cancel`，`apps/backend/app/core/tool/background/background_registry.py:177-220`，改名后复用）。停止走这条路，卡片结算、结论回投与用户点「停止」完全一致。
  - 取消检查 `should_stop_call`（`apps/backend/app/core/workflow/cancel/tool_call_cancellation_registry.py:74`），读取等待期间轮询它。
  - 输出预算由执行管线对所有工具统一应用，新工具不需要自己截断（`app/core/tool/execute/tool_output_budget.py`）。
  - 工具调用意图 `intent` 由框架自动注入，新工具不用管（`app/core/tool/call_intent.py:21-29`）。
  - `execute_terminal` 的实时输出回调 `output_sink`（`local_backend.py:85-100`），新登记表在这里分一路。
- 改造后复用：`ExecuteTerminalTool._render_content` 改为调用整理器和 `ExecutionResult.end_marker()`，原调用方（前台结果、后台结论）行为只差「去掉控制序列」。
- 无可复用：输出整理。查过 `terminal_runtime` 的屏幕模型，它绑在 PTY 和 Rust worker 上，管道输出用不上；仓内没有别的 ANSI 处理（`grep x1b` 只有注释）。

【问题清单】

| # | 现象 | 证据 | 层级 | 置信度 / 处理 |
|---|---|---|---|---|
| Q1 | Run 还有没结束的后台调用时，模型的最终回复会被丢弃、Run 挂起等待。Agent 忘了停服务时，要等到超时（最长 1 小时）才能结束 | `core/workflow/nodes/model_node.py:186-200` 的 `run_awaits_background` | 契约（有意设计） | 高。本次只在工具描述里明确「结束前先停掉自己起的服务」，用户也能在看板上停。是否在挂起时提醒模型，另议（见【刻意不做】） |
| Q2 | 正因为 Q1，gap 稿方案 A 里「子 Task 交回结论时撤销它的后台工作」这条规则用不上：子 Task 有后台命令时根本不会正常结束 | 同上 | — | 高。从本次范围删除 |
| Q3 | 输出预算的 docstring 说落盘的是「完整原文」；改造后模型通道落盘的是整理后的全文（仍完整，不再含控制序列） | `tool_output_budget.py:1-12` | 注释漂移 | 高。随本次改 docstring |

【选定路线】

- 结论：新增一个只持有「仍在运行的命令」的进程内登记表，加两个薄工具；停止复用后台登记表的单项取消；输出整理做成一个纯函数式的增量整理器，三个出口共用。
- 被否方案：
  - **把输出放进 `background_registry`**：它的契约是「只记录回投给谁、不持有执行状态」（模块 docstring 第 3-5 行），塞进去会让它变成第二套执行状态。否决。
  - **Codex 式「自动让出 + 轮询」**：用户已定沿用显式后台。
  - **命令结束后仍保留条目供读取**：结论会以 `<background_result>` 送达，保留条目就要管清理时机（Task 删除、后端关闭）。只持有运行中的命令，生命周期自然闭合。否决。
  - **读取支持正则搜索、等待「安静」**：先不做，等真实需求（见【刻意不做】）。
  - **处理光标上移等多行进度条**：只处理 `\r`、`\b`、行内擦除；其余控制序列丢弃。多行进度条最坏情况是多出几行旧进度，可接受。

【改动账本】（粗估）

- 精简：
  - `execute_terminal.py`：`_render_content` 的结局标记表搬到 `ExecutionResult.end_marker()`（约 −10）。
  - `background_registry.py`：`request_user_cancel` 改名，不增删逻辑（±0）。
- 新增：
  - `apps/backend/app/tools/tool_handler/terminal/output_normalizer.py`：约 +160。
  - `apps/backend/app/tools/tool_handler/terminal/running_commands.py`：约 +220。
  - `apps/backend/app/tools/tool_handler/read_command_output.py`：约 +170。
  - `apps/backend/app/tools/tool_handler/stop_command.py`：约 +110。
  - `apps/backend/app/tools/tool_models/running_command_args.py`：约 +60。
  - `execute_terminal.py`：约 +30。
  - `execution_result.py`：约 +15。
  - 注册、工具名、Agent 白名单、常量：约 +25。
  - 前端工具目录与展示契约表：约 +4。
  - 测试：约 +600。
- 净变化：约 +1400（其中测试约 600）。交付后以 `git diff --stat` 对账。
- 命名：
  - `tools/tool_handler/terminal/output_normalizer.py`：正向「终端输出整理」能落到 terminal 目录；反向读出「终端输出的整理器，属于工具层」。
  - `tools/tool_handler/terminal/running_commands.py`：反向读出「运行中命令的登记表」。
  - 工具名 `read_command_output` / `stop_command`：动词在前，与 `read_file`、`write_file` 一致；不用 `terminal_` 前缀，避免和交互终端的 `terminal_read` 混淆。
  - 参数模型 `tool_models/running_command_args.py`：两个工具同族，参数模型放一个文件。
- 同步：
  - AGENTS.md 两处（第二部分步骤 9）。
  - `tool_ui_display_contract.md` 第 3 节表格。
  - gap 稿状态行、终端改造稿 T1 指向本文。

【刻意不做】

- 读取时按正则搜索、等待「输出安静」：先不做，`wait_for` 已覆盖「等服务就绪」。
- Agent 往运行中的命令输入（stdin）、分配 TTY。
- 给 `_OutputCollector` 设上限：卡片展示契约是完整输出，另议。
- Run 因后台命令挂起时提醒模型去停（Q1）：会改变 `model_node` 的等待语义，需要单独设计。
- 停止子 Agent、Team 等其他种类的后台工作：本次只停终端命令。
- 子 Task 交回结论时撤销后台工作（Q2）。
- 交互终端、沙箱、权限。

---

## 第二部分：实施手册（给执行者）

### 0. 开工前必读

1. 读仓库根目录 `AGENTS.md`，至少读「开发约定」整节、「工具 UI 渲染边界」「代码目录边界」。本仓规则要点：
   - **docstring 与注释一律中文**，只写不明显的信息（为什么、上下游契约），不复述代码。
   - **日志**用 `log.info("snake_case_event", extra={"msg": "中文说明", "data": {...}})`。
   - **禁止**用 `getattr(obj, "方法名", None)` 探测本仓对象的方法。
   - 「未找到」用 `KeyError` 表达，由边界（这里是工具 handler）一处捕获。
   - 绿地项目，不做兼容。
2. **不要提交 git**，不要改本文没提到的文件。遇到本文没覆盖、或与代码事实冲突的情况，**停下来问**，不要自己发明方案。
3. 先读这些文件，搞清现状再动手：
   - `apps/backend/app/tools/tool_handler/execute_terminal.py`（全文，重点 `_execute_command` 325-430 行、`_placeholder_observation`、`_render_content`）。
   - `apps/backend/app/tools/tool_handler/terminal/execution_result.py`、`local_backend.py`（`execute` 的 `output_sink`）、`output_stream.py`。
   - `apps/backend/app/core/tool/background/background_registry.py`（模块 docstring、`request_user_cancel`）。
   - `apps/backend/app/tools/tool_models/execute_terminal_args.py`（strict 参数模型的写法：可选字段写成 `X | None = Field(default=None, description=...)`，描述用英文）。
   - `apps/backend/tests/test_execute_terminal_background.py`（真跑命令的测试写法与 fixture）。

### 1. `ExecutionResult.end_marker()`

**改哪里**：`apps/backend/app/tools/tool_handler/terminal/execution_result.py`。

- 给 `ExecutionResult` 加方法 `end_marker(self) -> str`，把 `execute_terminal.py` 里 `_render_content` 的 `marker = {...}[result.end]` 整张表原样搬过来。
- `ExecuteTerminalTool._render_content` 改为调用 `result.end_marker()`。
- 理由：读取工具在命令结束时也要显示同样的结局标记，标记表只能有一份。

### 2. 输出整理器 `output_normalizer.py`（新文件）

**位置**：`apps/backend/app/tools/tool_handler/terminal/output_normalizer.py`。

**职责**：把命令的原始解码文本（含 ANSI 控制序列、`\r` 进度条）整理成给模型读的纯文本行。**只服务模型通道**，卡片展示不经过它。

**接口**：

```python
class OutputNormalizer:
    """增量整理：按块喂入，跨块的半截控制序列与半行留在内部状态里。"""

    def feed(self, text: str) -> list[str]:
        """喂入一块文本，返回这块里**已完成**（遇到 \\n）的行，不含换行符。"""

    def pending_line(self) -> str:
        """当前还没换行的那一行（可能是 "Password:" 之类的提示），只读，不消费。"""

    def flush(self) -> list[str]:
        """输入结束：把未完成的行（非空时）作为最后一行返回，并清空状态。"""


def normalize_output(text: str) -> str:
    """一次性整理整段输出：OutputNormalizer().feed(text) + flush()，按 \\n 拼接。"""
```

**规则**（逐字符状态机，必须全部实现并有测试）：

| 输入 | 处理 |
|---|---|
| 可打印字符、`\t` | 写到当前行的光标位置：光标在行尾就追加，否则**覆盖**该位置的字符；光标右移一格 |
| `\n` | 当前行作为完成行输出（去掉行尾空格），清空当前行，光标归零 |
| `\r` | 光标归零（不清空当前行，后续字符从行首覆盖）。所以 `"abc\rX"` 得到 `"Xbc"`，`"10%\r20%\r100%\n"` 得到 `"100%"`，`"abc\r\n"` 得到 `"abc"` |
| `\b` | 光标左移一格（最小为 0），不删除字符 |
| CSI：`ESC [` + 参数字节 `0x30-0x3F`* + 中间字节 `0x20-0x2F`* + 结束字节 `0x40-0x7E` | 整段丢弃。**例外**：结束字节是 `K`（行内擦除）时，参数为空或 `0` 截断光标之后的内容，`1` 把光标之前的位置清成空格，`2` 清空整行（光标不动） |
| OSC：`ESC ]` … 以 `BEL`（`\x07`）或 `ESC \` 结束 | 整段丢弃。OSC 8 超链接 `ESC]8;;url ESC\ 文字 ESC]8;; ESC\` 因此只剩中间的文字 |
| DCS / SOS / PM / APC：`ESC P` / `ESC X` / `ESC ^` / `ESC _` … 以 `ESC \` 结束 | 整段丢弃 |
| `ESC` + 中间字节（`0x20-0x2F`，如 `ESC (`）+ 一个字节 | 丢弃（字符集切换等） |
| `ESC` + 其他单个字节 | 丢弃这两个字节 |
| 其余 C0 控制字符（`0x00-0x1F` 中除 `\t` `\n` `\r` `\b`、`ESC`）与 `DEL`（`0x7F`） | 丢弃 |
| 8 位 CSI `\x9b` | 当作 `ESC [` 处理 |

- **跨块**：半截控制序列（比如一块以 `"\x1b["` 结尾）留在状态里，和下一块拼起来再判断。
- **单行上限**：当前行超过 `Constant.Terminal.COMMAND_OUTPUT_LINE_MAX_CHARS`（新增，2000）后，多出的可打印字符丢弃，这一行完成时末尾加 `" …[line truncated]"`。防止没有换行的超长输出撑爆内存。
- **不做**：光标上下移动、颜色保留、Unicode 宽度计算。

### 3. 运行中命令登记表 `running_commands.py`（新文件）

**位置**：`apps/backend/app/tools/tool_handler/terminal/running_commands.py`。

**事实边界**（写进模块 docstring）：

- 只持有**仍在运行**的 `execute_terminal` 命令。命令结束（任何结局）即移除。
- 进程内状态，不持久化、不跨重启。
- 不负责结论回投（归 `background_registry`），不负责停止进程（停止经 `background_registry` 的撤销回调落到 `LocalExecutionBackend.stop`）。
- 命令一定会结束：超时、取消、Run 停止、Task 删除、后端关闭都会让作业收尾，所以登记表不需要另外清理。

**新增常量**（`apps/backend/app/config/constant.py` 的 `class Terminal` 里，带中文注释）：

```python
COMMAND_OUTPUT_HEAD_LINES = 100        # 保留最开头的行数
COMMAND_OUTPUT_TAIL_LINES = 10_000     # 保留最近的行数；中间丢弃并记录丢弃范围
COMMAND_OUTPUT_LINE_MAX_CHARS = 2_000  # 单行上限（整理器用）
COMMAND_OUTPUT_READ_DEFAULT_LINES = 100  # 不给 since_line 时返回最后多少行
COMMAND_OUTPUT_READ_MAX_LINES = 200    # 一次读取最多返回多少行
COMMAND_OUTPUT_WAIT_MAX_SECONDS = 30   # 读取时最多等多久
COMMAND_STOP_WAIT_SECONDS = 10         # 停止后最多等命令收尾多久
```

**接口**：

```python
@dataclass(frozen=True)
class OutputWindow:
    first_line: int                 # lines[0] 的绝对行号（从 1 开始）；没有行时为下一行的行号
    lines: list[str]
    pending: str                    # 还没换行的当前行，可能为空
    total_lines: int                # 已完成的总行数（含被丢弃的）
    dropped: tuple[int, int] | None # 被丢弃的中间行范围（含两端），没有则为 None
    result: ExecutionResult | None  # 命令已结束时的结局，运行中为 None


class RunningCommand:
    """一条运行中命令的输出与结束状态。feed / finish 在 worker 读线程与作业线程里调用，
    read / wait_* 在工具线程里调用：全部经同一个 threading.Condition 保护。"""

    def feed(self, text: str) -> None: ...      # 整理后存入有界行缓冲，notify_all
    def read(self, since_line: int | None) -> OutputWindow: ...
    def wait_for_match(
        self, pattern: re.Pattern[str], start_line: int, timeout_s: float,
        should_stop: Callable[[], bool],
    ) -> int | None: ...
    def wait_finished(self, timeout_s: float, should_stop: Callable[[], bool]) -> bool: ...


class RunningCommandRegistry:
    def start(self, task_id: int, command_id: str, command: str) -> RunningCommand: ...
    def get(self, task_id: int, command_id: str) -> RunningCommand: ...  # 不存在抛 KeyError
    def finish(self, task_id: int, command_id: str, result: ExecutionResult | None) -> None: ...


running_commands = RunningCommandRegistry()   # 模块级单例，与 background_registry 同样的用法
```

**语义细节**：

- **有界缓冲**：
  - 前 `HEAD_LINES` 行永久保留。
  - 之后最多保留最近 `TAIL_LINES` 行（用 `collections.deque(maxlen=...)`）。
  - 记录总行数，用来算绝对行号和被丢弃的范围。
- **`read(since_line)`**：
  - `since_line is None`：返回最后 `READ_DEFAULT_LINES` 行。
  - 否则返回从 `since_line` 起最多 `READ_MAX_LINES` 行。
  - `since_line` 落在被丢弃的范围里时，从仍保留的第一行开始，并在 `dropped` 里如实报告。
  - `since_line` 大于总行数时返回空列表（不报错）。
- **`wait_for_match(pattern, start_line, ...)`**：
  - 在「绝对行号 ≥ `start_line` 的已完成行 + 当前未完成行」里找第一个 `pattern.search` 命中的行。已经存在的行也算（服务可能在读取前就打印了就绪行）。
  - 命中返回行号（未完成行命中时返回 `total_lines + 1`）。
  - 命令结束、超时或 `should_stop()` 为真时返回 `None`。
  - 等待用 `condition.wait(0.25)` 循环，每轮复查 `should_stop()`。
- **`wait_finished`**：同样的循环，命令结束返回 `True`。
- **`finish`**：
  - `RunningCommand` 内部先 flush 整理器、记下 `result`、`notify_all`，再从登记表移除。
  - `result is None` 表示执行抛了异常、没有结果；读取时按「结局未知」显示。
- **`start` 重复登记**：同一个 `(task_id, command_id)` 已存在时抛 `RuntimeError`。`tool_call_id` 在 Task 内唯一，出现即代码缺陷，不要静默覆盖。

### 4. `execute_terminal` 接入

**改哪里**：`apps/backend/app/tools/tool_handler/execute_terminal.py`。

1. 在 `_execute_command` 里、`BackgroundJob.start_in_thread` 之前，登记：`running = running_commands.start(execution_context.task_id, tool_call_id, command)`。
2. 实时输出分两路：传给 `backend.execute` 的 `output_sink` 改为一个同时调用 `channel.emit`（`channel` 可能为 `None`）和 `running.feed` 的小函数。回调在 worker 读线程里执行，**不得抛异常**（见 `local_backend.py` 的 docstring）。
3. `work` 里改成：

   ```python
   result: ExecutionResult | None = None
   try:
       result = backend.execute(...)
   finally:
       if channel is not None:
           channel.finish()
       running_commands.finish(execution_context.task_id, tool_call_id, result)
   ```

   `finish` 必须在结论交付（`return self._background_conclusion(...)`）之前发生，这样读取方先看到结束，`<background_result>` 后到。
4. `_render_content`：输出部分改为 `normalize_output(result.output)`，标记改为 `result.end_marker()`。`_display_data` **不变**，仍用原始 `result.output`。
5. `_placeholder_observation` 的正文里加上 `command_id`，两种情形（`background`、`detached`）都要加。目标文案：

   ```text
   the command is running in the background (timeout 300s); keep working. command_id: <tool_call_id>. Its result will arrive as a <background_result> message for this tool call (use wait_messages if you need to pause for it).
   ```

   `_placeholder_observation` 要多接收一个 `command_id` 参数，用 `execution_context.tool_call_id`（这是改写冲突后的真实 id，不要自己从别处取）。
6. **工具描述**：把 `_DESCRIPTION` 最后一句（`Programs that must keep running ... stopped at the timeout.`）替换为下面这段，并同步上方的中文译文注释：

   ```text
   A server or watcher you only need while working (to check a page, wait for a build) can run with background=true: read its output with read_command_output (wait_for a ready line) and end it with stop_command when done. Background commands still stop at their timeout, and you cannot finish your answer while one of yours is still running, so stop what you started; do not use wait_messages on a server, since it never finishes on its own. Programs that need interaction do not fit this tool.
   ```

   模块顶部注释说「描述不点名其他工具」，那条针对的是 `terminal_*`。本次新增的两个工具与 `execute_terminal` 在所有内置 Agent 里同时出现，可以点名。把那条注释改成「不点名交互终端工具」。
7. `apps/backend/app/tools/tool_models/execute_terminal_args.py` 的 `_BACKGROUND_DESCRIPTION` 改为：

   ```text
   true returns immediately with a command_id and delivers the result later as a <background_result> message; read_command_output reads its output meanwhile. false or null waits here until the command ends. Use wait_messages to wait for background work that will finish on its own.
   ```

### 5. 停止入口改名

- `apps/backend/app/core/tool/background/background_registry.py`：`request_user_cancel` 改名为 `request_cancel`，docstring 补一句「用户点卡片停止与 Agent 的 `stop_command` 共用」。逻辑不改。
- 调用方同步改名：`apps/backend/app/assistant_transport/service/transport_assistant_service.py:204`，以及 `tests/test_background_registry.py`、`tests/test_execute_terminal_background.py`、`tests/test_execute_terminal_execution.py`。用 `grep -rn request_user_cancel apps/backend` 确认没有遗漏。

### 6. 两个新工具

参数模型放在 `apps/backend/app/tools/tool_models/running_command_args.py`（新文件），并在 `tool_models/__init__.py` 里照现有方式导出。strict 约束：

- `model_config = ConfigDict(strict=True, extra="forbid")`。
- 每个字段都有**英文** `description`。
- 可选字段写成 `X | None = Field(default=None, ...)`。
- 不能有叫 `intent` 的字段。

#### 6.1 `read_command_output`

**文件**：`apps/backend/app/tools/tool_handler/read_command_output.py`。照 `execute_terminal.py` 的结构写：继承 `HandlerBase`，`group = TOOL_GROUP_TERMINAL`，实现 `execute(...)` 与 `to_definition()`，加模块级 `build_read_command_output_definition()`。

**参数**：

| 字段 | 类型 | 说明 |
|---|---|---|
| `command_id` | `str`（`min_length=1`） | `execute_terminal` 后台占位里给出的 command_id |
| `since_line` | `int \| None`（`ge=1`） | 从第几行开始读；不给时返回最后 100 行。结果里会给出 `next_line`，下次传它就能续读 |
| `wait_for` | `str \| None`（`min_length=1`） | Python 正则；给出时等到某一行（含读取前已打印的行）命中，或命令结束 |
| `wait_seconds` | `float \| None`（`ge=0`，`le=30`） | 最多等多久；缺省时有 `wait_for` 就是 30 秒，否则 0（不等） |

**模型可见描述**（英文，同时在代码注释里写中文译文）：

```text
Read the output of a command you started with execute_terminal that is still running (a background or user-detached command), by its command_id. Returns numbered lines plus next_line for the next read; with wait_for, first waits up to wait_seconds for a line matching the regex (lines printed earlier count too) or for the command to end, e.g. wait_for "ready|listening" after starting a dev server. A command that has finished is no longer readable: its full result arrives as a <background_result> message.
```

**执行流程**：

1. `wait_for` 不为空时先 `re.compile`；失败返回 `tool_error(name, f"invalid wait_for regex: {exc}")`。
2. `running_commands.get(task_id, command_id)`，捕获 `KeyError`，返回：

   ```python
   tool_error(name, f"no running command with command_id {command_id} in this task. "
              "A finished command's result arrives as a <background_result> message; "
              "only commands still running can be read.")
   ```

   注意按 `execution_context.task_id` 查，**不能读别的 Task 的命令**。
3. 有 `wait_for` 时调用 `wait_for_match(pattern, since_line or 1, wait_seconds, should_stop)`；只有 `wait_seconds > 0`、没有 `wait_for` 时调用 `wait_finished`。`should_stop = lambda: should_stop_call(task_id, tool_call_id)`（本次读取调用自己的 `tool_call_id`）。被取消时返回 `tool_cancelled(name, reason="the read was cancelled")`。
4. 调用 `read(since_line)` 取窗口，组装正文：

   ```text
   [running] command_id=<id> lines 136-150 of 150, next_line=151
   (lines 101-2000 were dropped to bound memory)        ← 只在 dropped 时出现
   136: ...
   ...
   150: ...
   (incomplete line) Password:                            ← 只在 pending 非空时出现
   [wait_for matched at line 148]                         ← 或 [wait_for did not match within 30s] / [command ended before wait_for matched]
   ```

   - 命令已结束时（窗口的 `result` 不为空），首行的 `[running]` 换成 `result.end_marker()`；`result` 为 `None` 时用 `[outcome_unknown]`。
   - 没有新行时写 `(no new lines)`。
5. 返回 `tool_success(name, content)`，**不带** `display_data`（前端退化成纯行）。

**定义字段**：`execution_mode="thread"`（必须，登记表在进程内）、`parallel_mode="parallel"`、`timeout_seconds=COMMAND_OUTPUT_WAIT_MAX_SECONDS + 5`、不设 `resource_keys`。

**日志**：`command_output_read`，`data` 带 `task_id`、`command_id`、`since_line`、`returned_lines`、`waited`、`matched_line`、`running`。

#### 6.2 `stop_command`

**文件**：`apps/backend/app/tools/tool_handler/stop_command.py`。结构同上。

**参数**：`command_id: str`（`min_length=1`），说明同上。

**模型可见描述**：

```text
Stop a command you started with execute_terminal that is still running (a background or user-detached command), by its command_id, together with every process it started. Use it to end a dev server or watcher once you are done with it. Its final output still arrives as a <background_result> message.
```

**执行流程**：

1. `running_commands.get(task_id, command_id)`。`KeyError` 时返回 `tool_error(name, f"no running command with command_id {command_id} in this task; it may have already finished.")`。
2. 调用 `background_registry.request_cancel(task_id, command_id, driving=False)`。
   - 返回 `False` 表示已经在停止中，或条目已不在。这时不报错，继续第 3 步。
   - 为什么 `driving=False`：目标是另一次调用登记的后台条目，不是「当前正在执行的工具调用」，不需要打调用级取消信号。
3. `running.wait_finished(COMMAND_STOP_WAIT_SECONDS, should_stop)`：
   - 结束了：返回 `tool_success(name, f"stopped command {command_id}. Its final output arrives as a <background_result> message.")`。
   - 超时：返回 `tool_error(name, f"asked command {command_id} to stop, but it has not exited after {N}s; its result will still arrive as a <background_result> message.")`。
4. 日志：`command_stop_requested`、`command_stopped`（`data` 带 `task_id`、`command_id`、`stopped`）。

**定义字段**：`execution_mode="thread"`，其余默认。

### 7. 登记新工具

按以下清单逐项改（子 Agent 已核对过行号，以实际文件为准）：

1. `apps/backend/app/core/tool/schemas/tool_names.py`：新增常量 `TOOL_READ_COMMAND_OUTPUT = "read_command_output"`、`TOOL_STOP_COMMAND = "stop_command"`，放在 `TOOL_EXECUTE_TERMINAL` 旁边；同时加进 `ToolName` Literal 和 `ALL_TOOL_NAMES`（三处）。
2. `apps/backend/app/core/tool/schemas/__init__.py`：再导出两个常量。
3. `apps/backend/app/tools/tool_registration.py`：在 `build_execute_terminal_definition()` 的注册之后注册两个新工具。
4. `apps/backend/app/core/agents/define_agents.py`：主 Agent、通用子 Agent、只读子 Agent 三个 profile 里，凡是有 `TOOL_EXECUTE_TERMINAL` 的地方，紧跟着加上两个新工具。
5. **不要**改 `task_tool_scope.py`、`task_kind.py`：新工具保持 `needs_user_surface=False`（默认值），会自动给子 Agent 与 Team 节点。
6. `apps/desktop/lib/assistant/tools/tool-catalog.ts`：在 `execute_terminal` 那一行旁边加两条（两空格缩进，形状必须一致，守卫按这个形状提取）：

   ```ts
     read_command_output: { verb: "读取命令输出", icon: "terminal" },
     stop_command: { verb: "停止命令", icon: "x" },
   ```

7. `apps/backend/app/core/tool/tool_ui_display_contract.md` 第 3 节表格：在 `execute_terminal` 那一行后加两行，第二列 kind 写「无」，第三列照抄表里 `browser_tabs` 那一行（约第 53 行）的写法。

### 8. 交互终端描述的分工

`apps/backend/app/tools/tool_handler/terminal_session/start.py` 的 `description`：把「Use it for what must keep running (dev servers, watchers, tail -f), ...」改为：

```text
Use it for what the user should keep running after you finish or watch and join (a dev server they will use, tail -f), or that needs back-and-forth input (REPLs, TUIs, programs that ask questions); a server you only need while checking something runs better with execute_terminal background=true. For a command that finishes on its own and whose result is all you need, use execute_terminal.
```

其余句子保留，同步上方中文译文注释。

### 9. 文档同步

- `apps/backend/app/core/tool/execute/tool_output_budget.py` 模块 docstring：「落盘 artifact 与 display_data 都保持完整原文」改为「模型通道落盘的是整理后的完整文本（不含控制序列），display_data 保持完整原文」。
- `AGENTS.md`，只改两处，不扩写：
  - 「代码目录边界」里描述 `ExecuteTerminalTool` 的那句（「终端独有的前台等待、用户转后台与观察组装归 `ExecuteTerminalTool`」）后面加：「运行中命令的输出与读取等待归 `tools/tool_handler/terminal/running_commands`（只持有仍在运行的命令，Agent 经 `read_command_output` / `stop_command` 读取与停止；停止复用后台登记表的单项取消）；给模型的命令输出统一经 `output_normalizer` 去掉控制序列」。
  - 「工具 UI 渲染边界」的已注册 kind 列表里，在 `execute_terminal→terminal-result` 后加「`read_command_output` / `stop_command` 无展示 `kind`」。
- `docs/plan/terminal-gap-assessment.md` 顶部状态行：注明「T3 / T4 由 `execute-terminal-read-stop-plan.md` 实施」。
- `docs/plan/terminal-ownership-redesign.md` §3.1「补齐运行中读取与停止」一条后加「方案见 [`execute-terminal-read-stop-plan.md`](execute-terminal-read-stop-plan.md)」。

### 10. 测试

新增测试文件，用例名用英文描述行为，docstring 中文。

**`apps/backend/tests/test_terminal_output_normalizer.py`**（纯单测，不需要 worker）：

1. 颜色码被去掉：`"\x1b[31mred\x1b[0m\n"` → `["red"]`。
2. `\r` 覆盖：`"10%\r20%\r100%\n"` → `["100%"]`；`"abc\rX\n"` → `["Xbc"]`；`"abc\r\n"` → `["abc"]`。
3. `\b`：`"ab\bc\n"` → `["ac"]`。
4. 行内擦除：`"abcdef\r\x1b[K12\n"` → `["12"]`；`"abcdef\x1b[3D\x1b[K\n"` → `["abcdef"]`（`D` 光标左移不支持，被丢弃，`K` 在行尾截断不影响）。按你的实现如实断言，并在测试 docstring 里写明「光标移动不支持」。
5. OSC 8 超链接只留文字：`"\x1b]8;;https://x.test\x1b\\link\x1b]8;;\x1b\\\n"` → `["link"]`；以 `BEL` 结束的 OSC 同样。
6. 跨块：`feed("a\x1b[")` 返回 `[]`，再 `feed("31mb\n")` 返回 `["ab"]`。
7. `flush` 返回未完成行；`pending_line` 不消费。
8. 制表符保留、`BEL` 与其他 C0 被丢弃、`\x9b31m` 当 CSI 丢弃。
9. 超长行被截断并带 `…[line truncated]`。
10. `normalize_output` 与逐块 `feed` 的结果一致（同一段文本随机切块，结果相同）。

**`apps/backend/tests/test_running_commands.py`**（纯单测）：

1. 有界缓冲：喂入 `HEAD + TAIL + 50` 行后，`read(None)` 返回最后 100 行，行号正确；`read(1)` 返回前 200 行里实际保留的部分；`dropped` 范围正确。测试里用 monkeypatch 把常量调小，不要真喂一万行。
2. `read(since_line)` 续读与 `next_line` 计算；`since_line` 超过总行数返回空列表。
3. `wait_for_match` 能命中读取前已存在的行。
4. `wait_for_match` 在另一个线程稍后 `feed` 时被唤醒并命中。
5. 超时返回 `None`；`should_stop` 为真时返回 `None`；命令 `finish` 时返回 `None`。
6. `wait_finished` 在 `finish` 后返回 `True`。
7. `finish` 之后 `get` 抛 `KeyError`；重复 `start` 抛 `RuntimeError`。
8. 不同 `task_id` 互相看不到。

**`apps/backend/tests/test_command_output_tools.py`**（真跑命令，照 `test_execute_terminal_background.py` 的写法：`requires_terminal_worker`、POSIX 限定、替换 `deliver_background_result` 记录投递）：

1. 后台跑 `printf 'booting\nready\n'; sleep 30` → `read_command_output(command_id, wait_for="ready")` 返回 `[running]`、包含 `ready`、`[wait_for matched at line 2]`。
2. 接着 `stop_command(command_id)` 返回成功；随后收到一条 `end_reason == "cancelled"` 的后台结论；再读返回「no running command」错误。
3. 读不存在的 `command_id` 返回错误；用另一个 `task_id` 读同一个 `command_id` 也返回错误。
4. 前台跑 `printf '\033[31mred\033[0m\n'`：观察 `content` 里只有 `red` 没有 `\x1b`，`display_data` 里的输出仍含 `\x1b[31m`。
5. 后台占位的 `content` 里含 `command_id: <tool_call_id>`。
6. `wait_for` 正则非法时返回错误。
7. 命令在等待期间自己结束：`printf 'x\n'; exit 3` 后台跑，`read_command_output(wait_for="never", wait_seconds=10)` 在命令结束时立即返回（耗时远小于 10 秒），首行是 `[exit_code=3]`。注意：命令结束后条目会移除，这个用例要在命令结束前发起读取，可以在命令里先 `sleep 1`。

**要同步修改的现有测试**：

- 改名 `request_user_cancel` → `request_cancel` 的三个测试文件。
- `tests/test_task_tool_scope.py`：在断言侧栏对话、子 Agent 拿到 `execute_terminal` 的地方，补上两个新工具的断言。
- 如果有测试固定了后台占位文案或 `_DESCRIPTION` 原文，按新文案更新（`grep -rn "keep working\|must keep running" apps/backend/tests`）。

### 11. 验收命令

从仓库根目录运行，全部通过才算完成：

```bash
cargo build -p cosir-terminal-worker
```

```bash
uv run --project apps/backend pytest apps/backend/tests/test_terminal_output_normalizer.py apps/backend/tests/test_running_commands.py apps/backend/tests/test_command_output_tools.py apps/backend/tests/test_execute_terminal_background.py apps/backend/tests/test_execute_terminal_execution.py apps/backend/tests/test_background_registry.py apps/backend/tests/test_task_tool_scope.py apps/backend/tests/test_strict_tool_contract.py apps/backend/tests/test_tool_registry_display_guard.py apps/backend/tests/test_child_agent_close_removal_contract.py apps/backend/tests/test_tool_names.py
```

```bash
uv run --project apps/backend pytest
```

```bash
uv run --project apps/backend ruff check apps/backend/app apps/backend/tests
```

新增的 `app/` 文件必须过严格类型检查（在 `apps/backend` 目录下运行）：

```bash
cd apps/backend && uv run mypy --config-file mypy.strict.ini app/tools/tool_handler/terminal/output_normalizer.py app/tools/tool_handler/terminal/running_commands.py app/tools/tool_handler/read_command_output.py app/tools/tool_handler/stop_command.py app/tools/tool_models/running_command_args.py
```

```bash
cd apps/desktop && npm run test:unit && npm run lint
```

### 12. 交付时报告

1. 每一步做了什么，偏离本文的地方和理由。
2. `git diff --stat`，与第一部分的改动账本对账。
3. 上面全部验收命令的结果（贴失败输出，不要只说「通过」）。
4. 发现的 docstring 与代码不一致之处（只报告，不在本次范围外擅自改）。

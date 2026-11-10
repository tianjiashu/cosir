# 终端二期：后台命令并入终端读取

> 状态：2026-11-10 实施方案，待执行（同日补入命令输出封顶，见 S1 与 Q6）。主设计会话调研定稿，交给执行者（没有讨论上下文）按本文实施。
> 修订经过：11-10 上午的初稿（新增 `read_command_output` / `stop_command`）已作废，本稿取代它，原文件 `execute-terminal-read-stop-plan.md` 改名为本文。
> 上游：[`terminal-ownership-redesign.md`](terminal-ownership-redesign.md) 二期；一期已落地（[`terminal-agent-read-prefill-plan.md`](terminal-agent-read-prefill-plan.md)，提交 `68399308`）。也是 [`terminal-gap-assessment.md`](terminal-gap-assessment.md) §3 T3 / T4 的定稿。
> 不在范围：沙箱、权限、「请你执行」、Agent 往运行中的命令输入（stdin / TTY）。

---

## 第一部分：方案（按 AGENTS.md 交付模板）

【任务类型】功能改造（给已有读取能力接入第二种来源，补停止，统一给模型的命令输出文本）。

〔功能改造〕

**现状 → 终态**

- 现状：
  - `execute_terminal` 的后台命令只在结束时以 `<background_result>` 回投一次结果。运行中的输出只存在局部收集器里（`apps/backend/app/tools/tool_handler/terminal/output_collector.py`），没有可寻址的持有者，Agent 读不到，也停不了自己的后台命令。
  - 给模型的正文把原始输出直接拼进去，带着颜色码和进度条控制字符（`execute_terminal.py` 的 `_render_content`）。
  - `terminal_read` 已有成熟的读取契约：按行号读、读尾部、按正则搜索、四种等待条件（`command_returned` / `output_quiet` / `match` / `elapsed`）。但它只能读用户的交互终端，而且只给侧栏对话（`needs_user_surface=True`）。
- 终态：
  - 「终端」指本 Task 里产生终端输出的东西：**用户的交互会话**（id 形如 `term_…`），以及 **Agent 还在运行的命令**（id 是那次 `execute_terminal` 调用的 `tool_call_id`）。
  - `terminal_list` 两者都列，`terminal_read` 两者都能读，读取参数、等待条件、输出格式完全一样。新增 `terminal_stop`，只能停 Agent 自己运行中的命令。
  - 这三个工具开放给所有 Agent，子 Agent 与 Team 节点也能读、停自己的命令。`terminal_write` 仍只给侧栏对话、只能填用户终端。
  - 所有给模型的命令输出（前台结果、后台结论、读取结果）都经同一个整理器：去掉控制序列、按 `\r` 覆盖进度条。卡片展示仍是原始输出。
  - 命令输出只在一处收集（`CommandOutput`），内存、snapshot 与数据库里都有上限：卡片只留最近 256 KiB，前面标「省略 N 行」；超过模型预算时，完整输出边跑边写进工作区日志文件，模型正文与卡片都指向它。
- 动机：
  - 「一串带行号、可读可搜可等的终端输出」是同一种事实，只是来源不同（PTY 屏幕 / 管道），不该有两套读取实现，也不该让模型学两套参数。
  - Agent（包括子 Agent）跑开发服务器、watcher 时，可以等它就绪、看日志、用完关掉。
  - 本次请 Agent 把服务挂在后台，一挂可能一小时；输出不封顶会撑大内存、重连首帧与数据库行（Q6）。

**行为账**

- 不变：
  - `execute_terminal` 的参数，以及前台 / 后台 / 用户转后台 / 结束推结果的语义。超时默认 300 秒、最长 3600 秒，后台命令同样会在超时被结束。
  - 结局分类与标记（`[exit_code=N]` 等）；卡片输出仍是原始文本（带颜色）；模型正文仍是头尾截断 + 全文落盘、预算不变；短命令（输出不超过窗口与预算）看起来与现在完全一样；后台登记表的投递、撤销、关闭语义。
  - 用户终端的读取结果与现在逐字一致（只有 id 参数改名，见下）。
  - `terminal_write` 的全部行为（只多了参数改名）。
  - 用户终端的创建、输入、面板、snapshot `terminal_sessions`：运行中的命令**不**进 snapshot，前端看命令仍靠 `execute_terminal` 卡片与后台看板。
- 有意改变：
  - 模型看到的命令输出不再含 ANSI 控制序列，进度条只留最终状态。
  - 卡片（实时与终态）只保留最近 `DISPLAY_OUTPUT_WINDOW_CHARS` 的输出，展示数据新增 `output_omitted_lines` / `output_log_path`，卡片显示「前面省略 N 行 · 打开完整日志」。展示契约从「完整输出」改为「最近一段 + 日志路径」。
  - 超长命令输出的全文落盘改为边跑边写、内容是整理后的文本；`ExecutionResult` 不再携带输出。
  - `terminal_read` / `terminal_write` 的参数 `session_id` 改名为 `terminal_id`，展示数据的 `session_id` 同步改名 `terminal_id`。
  - `terminal_list` / `terminal_read` 开放给所有 Agent，并从「交互终端工具」组移到「终端工具」组（和 `execute_terminal` 同组）。
  - 新增工具 `terminal_stop`。
  - 后台占位文案多一个 `terminal_id`。
  - `execute_terminal`、`terminal_list`、`terminal_read` 的工具描述改写（服务 / watcher 的用法）。
  - `background_registry.request_user_cancel` 改名为 `request_cancel`（Agent 也会用它）。
  - `output_collector.py` 由 `command_output.py` 取代，解码器搬到 `output_decoder.py`。
  - 目录 `tools/tool_handler/terminal_session/` 改名为 `tools/tool_handler/terminal_tools/`，参数模型 `terminal_session_args.py` 改名为 `terminal_tools_args.py`（见改动账本）。
- 消费方：见第二部分各步骤的「改哪里」。
- 分阶段：见第二部分，按 S1 → S6 顺序实施，每步有可单独运行的测试。被取代的旧路径（`output_collector.py`、`ExecutionResult.output`、`session_id` 参数名、`terminal_session` 目录名、`request_user_cancel`）在所在步骤直接删除，不留别名。

【链路复用】

- **直接复用**（以下全部沿用，不另写一套）：
  - 有界、行号永不复用的逻辑行缓冲 `TerminalLineBuffer`（`tools/terminal_runtime/line_buffer.py`），含按 `MAX_BUFFER_LINES` / `MAX_BUFFER_CHARS` 淘汰。
  - 观察对象 `TerminalObservation` 的命令跟踪（`observation.py` 的 `shell_event`：`ready` / `command_started` / `command_returned`）与 `end_tracking`。
  - 等待判定 `WaitEvaluator` / `WaitBaseline`（`wait.py`），四种等待条件原样适用。
  - 渲染 `render_observation` / `render_session_summary` / `session_state_parts`（`render.py`）：行号格式、尾部、搜索、区间说明、预算。
  - 后台登记表的单项取消（`core/tool/background/background_registry.py` 的 `request_user_cancel`，改名后复用）：停止走这条路，卡片结算、结论回投与用户点「停止」完全一致。
  - 调用级取消检查 `should_stop_call`（`core/workflow/cancel/tool_call_cancellation_registry.py`）。
  - `execute_terminal` 的实时输出通道 `TerminalOutputStream`（`tools/tool_handler/terminal/output_stream.py`）与解码器 `_IncrementalOutputDecoder`（搬家后原样复用）。
  - 超长输出落盘目录 `workspace_tool_artifact_dir` 与命名方式；卡片打开工作区文件的链路（`lib/links/resolve-link.ts` + `use-open-link.ts`）。
  - snapshot 变更协议的 `set` / `append-text`：卡片裁剪用 `set` 表达，不新增变更种类，前端不写截断逻辑。
- **改造后复用**：
  - `TerminalLineBuffer` 增加管道输出的写入方法（第二个生产者），屏幕事件路径不变。
  - `TerminalSessionRuntime` 里的读取算法（`read` / `_wait` / `_build_result`）提到一个共同基类 `ObservedTerminal`，会话与运行中命令各自继承。会话的读取结果逐字不变。
  - `SessionFacts` 改名 `TerminalFacts`，增加 `kind`（`"session"` / `"command"`），表头写 `session <id>` 或 `command <id>`。
  - `_OutputCollector` → `CommandOutput`：从「存全文 + 旁路原始片段」改为唯一收集点（卡片窗口、模型头尾、日志文件、运行中读取）。原调用方只有 `LocalExecutionBackend`，改为由 `execute_terminal` 创建后传入。
  - `ToolOutputBudget._truncate_content` 的头尾算法提成 `fit_head_tail`，对原调用方逐字不变，`CommandOutput` 溢出后复用。
- **无可复用**：管道输出的整理（去控制序列、`\r` 覆盖）。查过：交互终端靠 Rust worker 的屏幕模型（alacritty，绑在 PTY 上），管道用不上；仓内没有别的 ANSI 处理。

【问题清单】

| # | 现象 | 证据 | 层级 | 置信度 / 处理 |
|---|---|---|---|---|
| Q1 | Run 还有没结束的后台调用时，模型的最终回复会被丢弃、Run 挂起等待。Agent 忘了停服务时，要等到超时（最长 1 小时）才能结束 | `core/workflow/nodes/model_node.py` 中 `run_awaits_background` 的分支 | 契约（有意设计） | 高。本次只在工具描述里写明「结束前先用 terminal_stop 停掉自己起的服务」；用户也能在看板上停。挂起时提醒模型另议（见【刻意不做】） |
| Q2 | 因为 Q1，「子 Task 交回结论时撤销它的后台工作」这条规则用不上：子 Task 有后台命令时根本不会正常结束 | 同上 | — | 高。不做 |
| Q3 | 会话 id（`term_` + 12 位十六进制）与命令 id（`tool_call_id`）理论上可能同名 | `session_runtime.py` 中 `session_id` 的生成；`tool_call_id` 由模型厂商给出，冲突时由 `save_model_response` 改写 | 契约 | 高。查找顺序固定为「先会话、后命令」，写进查找函数的 docstring；厂商 id 以 `term_` 开头且恰好撞上 12 位十六进制的概率可以忽略，不另加前缀 |
| Q4 | 输出预算 docstring 说落盘的是「完整原文」；改造后模型通道落盘的是整理后的全文（仍完整，不再含控制序列） | `core/tool/execute/tool_output_budget.py` 模块 docstring | 注释漂移 | 高。随本次改 |
| Q5 | 读取工具的名字、展示 kind 还叫 `terminal-session` | `tool_ui_display_contract.md` 第 3 节 | 命名 | 中。kind 表示「终端类工具的调用行视图」，不是来源；本次不改 kind，见【刻意不做】 |
| Q6 | 命令输出在三处完整累积、没有上限：收集器、snapshot 卡片（实时追加）、终态展示数据（落库）。今天服务被描述挡在外面，本次请服务进来后会被放大 | `tool_handler/terminal/output_collector.py` 的 `_accept`；`tool_runtime_event_projection.py` 的 `_plan_terminal_output`；`execute_terminal.py` 的 `_display_data` | 设计不足（缺长时运行这一维度） | 高。本次处理，见 S1 |

【选定路线】

**结论**：把运行中的命令建模成「一个带观察的终端」，复用交互终端整条观察链路（行缓冲 → 观察 → 等待 → 渲染）。读取算法提到共同基类，会话与命令各自实现「事实快照」与「是否活跃」两个钩子。工具层只多一个按 id 查找终端的函数和一个停止工具。命令输出改为唯一收集点 `CommandOutput`：一次解码、一个整理器，卡片、模型、运行中读取各取有上限的一份，全文只在磁盘。

**被否方案**：

- **新增独立的 `read_command_output` / `stop_command`**（本稿取代的初稿）：重造一个比 `terminal_read` 弱的读取契约，模型要学两套参数，读取算法两份。否决。
- **把运行中的命令放进 `TerminalSessionService` 的会话集合**：会话集合承载的是用户的 PTY 会话（生命周期、终态保留、snapshot 投影、WebSocket 交互），命令只需要「运行期间可读可停」，混进去会让会话集合里出现一类不进 snapshot、不能输入、结束即消失的特例。否决，命令另设登记表。
- **给观察喂合成的屏幕事件**（伪造 `screen` 字典）来复用 `TerminalLineBuffer.apply`：要伪造行列几何与光标，是 worker 线格式的冒用。否决，改为给行缓冲加一个管道写入方法。
- **命令结束后仍保留在登记表里供读取**：结论已以 `<background_result>` 送达，保留就要管清理时机（Task 删除、后端关闭）。只持有运行中的命令，生命周期自然闭合。否决。
- **Codex 式「自动让出 + 轮询」**：用户已定沿用显式后台。
- **只给后台命令封顶**：用户随时能把前台命令转到后台，两套规则就是特例。否决，所有命令同一规则。
- **前端自己截断卡片输出**：后端 snapshot 与数据库仍是无上限的，问题没解决，且规则写两份。否决。
- **卡片继续存完整输出、只给收集器封顶**：重连首帧与数据库行仍无上限。否决。

【改动账本】（粗估）

- **精简**：
  - `session_runtime.py`：`read` / `_wait` / `_build_result` 搬进基类（约 −110）。
  - `execute_terminal.py`：`_render_content` 的结局标记表搬到 `ExecutionResult.end_marker()`（约 −10）。
  - `background_registry.py`：`request_user_cancel` 改名，逻辑不变（±0）。
  - `output_collector.py` 删除（约 −120，解码器约 60 行搬走）；`ExecutionResult.output` 及 `local_backend.py` 里的传递（约 −15）。
  - `tool_output_budget.py`：`_truncate_content` 的算法搬进 `fit_head_tail`（±0）。
- **新增**：
  - `tools/terminal_runtime/output_normalizer.py`：约 +160。
  - `tools/tool_handler/terminal/command_output.py`：约 +170；`output_decoder.py`：约 +65（搬来）。
  - `core/tool/terminal_output_window.py`：约 +35；投影裁剪约 +20；展示数据两个字段约 +10。
  - 前端卡片与阅读器的省略提示：约 +40。
  - `tools/terminal_runtime/observed_terminal.py`（共同基类）：约 +150（大部分从会话搬来）。
  - `tools/terminal_runtime/running_command.py`：约 +130。
  - `tools/terminal_runtime/running_command_registry.py`：约 +70。
  - `line_buffer.py` / `observation.py` 的管道写入：约 +25。
  - `render.py`：`TerminalFacts` 改名与 `kind`，约 +10。
  - `execute_terminal.py` 接入：约 +35；`execution_result.py`：约 +15。
  - 工具层：`terminal_stop` 约 +100；按 id 查找约 +30；`terminal_list` / `terminal_read` 改造约 +30。
  - 注册、工具名、白名单、常量、分组：约 +25。
  - 前端工具目录、调用行视图、展示契约：约 +15。
  - 测试：约 +700。
- **净变化**：约 +1550（其中测试约 700）。交付后以 `git diff --stat` 对账。
- **目录与文件命名变更**：
  - `tools/terminal_runtime/output_normalizer.py`（新）。正向检索：「终端输出整理」能落到 terminal_runtime；反向读出：终端运行时的输出整理器。
  - `tools/tool_handler/terminal/command_output.py`（新，取代 `output_collector.py`）。正向检索：「命令输出怎么收集、截断、落盘」落到 `execute_terminal` 的执行后端目录；反向读出：一条命令的输出。
  - `tools/tool_handler/terminal/output_decoder.py`（从 `output_collector.py` 搬出）。反向读出：命令输出的字节解码。
  - `core/tool/terminal_output_window.py`（新）。反向读出：终端卡片输出窗口规则；放 `core/tool` 是因为 `app/tools` 与 `assistant_transport` 都要用。
  - `tools/terminal_runtime/observed_terminal.py`（新）。反向读出：「可观察的终端」——会话与运行中命令的共同读取底座。
  - `tools/terminal_runtime/running_command.py`、`running_command_registry.py`（新）。与 `session_runtime.py` / `session_service.py` 对称：单个对象与它的集合。
  - `tools/tool_handler/terminal_session/` → `tools/tool_handler/terminal_tools/`（改名）。理由：这个目录装的是按 `terminal_id` 操作终端的 `terminal_*` 工具，读取与停止的对象已不止会话。正向检索：找 `terminal_read` 能落到 `terminal_tools/read.py`。
  - `tools/tool_models/terminal_session_args.py` → `terminal_tools_args.py`（改名），理由同上。
  - 本文由 `execute-terminal-read-stop-plan.md` 改名而来。
  - **不改名**：`tools/tool_handler/terminal/`（`execute_terminal` 的执行后端：`local_backend`、`command_output` 等），职责没变。
- **同步**：
  - AGENTS.md（步骤 S6）。
  - `tool_ui_display_contract.md`（工具表、`terminal-session` 字段、`terminal-result` 新增字段）。
  - `tools/terminal_runtime/interaction_contract.md`（工具清单）。
  - 其他设计稿里指向本文的链接与状态行：主设计会话已改好。

【刻意不做】

- Agent 往运行中的命令输入（stdin）、分配 TTY。
- Run 因后台命令挂起时提醒模型去停（Q1）：会改变 `model_node` 的等待语义，需要单独设计。
- 子 Task 交回结论时撤销后台工作（Q2）。
- 停止子 Agent、Team 等其他种类的后台工作：`terminal_stop` 只停终端命令。
- 工作区 `.cosir/tool-artifacts` 里日志文件的清理：与现有超长输出落盘是同一个问题，以后统一处理。
- 文件面板打开超大日志文件的体验：沿用文件面板现有能力。
- 卡片里的完整日志查看器（虚拟滚动、搜索）：文件面板够用。
- 展示 kind `terminal-session` 改名（Q5）：它命名的是调用行视图，改名要动展示契约守卫与前端注册表，收益小。
- 运行中的命令进 snapshot、在终端面板里显示：前端已有命令卡片与后台看板，不需要第二个入口。
- 沙箱、权限、「请你执行」。

---

## 第二部分：实施手册（给执行者）

### 0. 开工前必读

1. 读仓库根目录 `AGENTS.md`，至少读「开发约定」整节、「进程生命周期边界」里交互终端那一段、「工具 UI 渲染边界」「代码目录边界」。本仓规则要点：
   - **docstring 与注释一律中文**，只写不明显的信息（为什么、上下游契约），不复述代码。
   - **日志**用 `log.info("snake_case_event", extra={"msg": "中文说明", "data": {...}})`。
   - **禁止**用 `getattr(obj, "方法名", None)` 探测本仓对象的方法。
   - 「未找到」用 `KeyError` 表达，由边界（工具 handler）一处捕获。
   - 绿地项目，不做兼容：改名就直接改，不留旧名别名。
   - 删掉被取代的旧路径与死代码，不让新旧并存。
2. **不要提交 git**，不要改本文没提到的文件。遇到本文没覆盖、或与代码事实冲突的情况，**停下来问**，不要自己发明方案。
3. 开工前先通读：
   - `apps/backend/app/tools/terminal_runtime/` 下 `session_runtime.py`、`observation.py`、`line_buffer.py`、`wait.py`、`render.py`、`session_service.py`、`errors.py`。
   - `apps/backend/app/tools/tool_handler/terminal_session/` 全部文件。
   - `apps/backend/app/tools/tool_handler/execute_terminal.py`（全文，重点 `_execute_command`、`_placeholder_observation`、`_render_content`）。
   - `apps/backend/app/tools/tool_handler/terminal/` 全部文件（`local_backend.py`、`output_collector.py`、`output_stream.py`、`execution_result.py`）。
   - `apps/backend/app/core/tool/execute/tool_output_budget.py`、`apps/backend/app/assistant_transport/projection/tool_runtime_event_projection.py`、`apps/backend/app/tools/display/terminal_display.py`。
   - 前端卡片：`apps/desktop/components/conversation/tools/call-views/terminal-result-view.tsx`、`components/conversation/tools/terminal-output/reader.tsx`。
   - `apps/backend/app/core/tool/background/background_registry.py`（模块 docstring、`request_user_cancel`）。
   - 测试写法：`apps/backend/tests/test_terminal_session_service.py`（fake worker）、`tests/test_execute_terminal_background.py`（真跑命令）。
4. 每删除或改名一个符号，都用 `grep -rn <符号> apps/` 确认没有遗留引用，包括测试与注释。

### S1. 命令输出：整理、封顶与落盘

本步之后，一条命令的输出只在一处收集（`CommandOutput`），解码一次，分给三个读者：

| 读者 | 拿到什么 | 上限 |
|---|---|---|
| 卡片（实时与终态） | 原始文本（带颜色码）的**最近一段** | `DISPLAY_OUTPUT_WINDOW_CHARS`（256 KiB），前面省略的行数另记 |
| 模型（前台结果、后台结论） | 整理后的纯文本：没超预算时是全文，超了是开头一段 + 结尾一段 | 各 `MAX_OUTPUT_CHARS` |
| 运行中读取（S3 的 `RunningCommand`） | 整理后的完成行 + 当前未完成行 | 行缓冲自己的上限 |

完整输出只有一份：整理后的全文超过 `MAX_OUTPUT_CHARS` 时，边跑边写进工作区的日志文件（与现有超长输出落盘同一目录），模型正文和卡片都带这个路径。内存里不再保留全文。

**1.1 新文件 `apps/backend/app/tools/terminal_runtime/output_normalizer.py`**

职责：把命令的原始解码文本（含 ANSI 控制序列、`\r` 进度条）整理成给模型读的纯文本行。**只服务模型通道与运行中读取**，卡片展示不经过它。

```python
class OutputNormalizer:
    """增量整理：按块喂入，跨块的半截控制序列与半行留在内部状态里。"""

    def feed(self, text: str) -> list[str]:
        """喂入一块文本，返回这块里已完成（遇到 \\n）的行，不含换行符。"""

    def pending_line(self) -> str:
        """当前还没换行的那一行（如服务打印的 "Listening on :3000"），只读，不消费。"""

    def flush(self) -> list[str]:
        """输入结束：把未完成的行（非空时）作为最后一行返回，并清空状态。"""
```

不提供「一次性整理整段文本」的函数：唯一的调用方是 `CommandOutput`，它按块喂入。

规则（逐字符状态机，全部要实现并有测试）：

| 输入 | 处理 |
|---|---|
| 可打印字符、`\t` | 写到当前行的光标位置：光标在行尾就追加，否则**覆盖**该位置的字符；光标右移一格 |
| `\n` | 当前行作为完成行输出（去掉行尾空格），清空当前行，光标归零 |
| `\r` | 光标归零（不清空当前行）。所以 `"abc\rX"` → `"Xbc"`，`"10%\r20%\r100%\n"` → `"100%"`，`"abc\r\n"` → `"abc"` |
| `\b` | 光标左移一格（最小为 0），不删除字符 |
| CSI：`ESC [` + 参数字节 `0x30-0x3F`* + 中间字节 `0x20-0x2F`* + 结束字节 `0x40-0x7E` | 整段丢弃。**例外** `K`（行内擦除）：参数为空或 `0` 截断光标之后的内容；`1` 把光标之前的位置清成空格；`2` 清空整行（光标不动） |
| OSC：`ESC ]` … 以 `BEL`（`\x07`）或 `ESC \` 结束 | 整段丢弃（OSC 8 超链接因此只剩中间的文字） |
| DCS / SOS / PM / APC：`ESC P` / `ESC X` / `ESC ^` / `ESC _` … 以 `ESC \` 结束 | 整段丢弃 |
| `ESC` + 中间字节（`0x20-0x2F`）+ 一个字节 | 丢弃（字符集切换等） |
| `ESC` + 其他单个字节 | 丢弃这两个字节 |
| 其余 C0 控制字符（`0x00-0x1F` 中除 `\t` `\n` `\r` `\b` `ESC`）与 `DEL`（`0x7F`） | 丢弃 |
| 8 位 CSI `\x9b` | 当作 `ESC [` |

- **跨块**：半截控制序列（如一块以 `"\x1b["` 结尾）留在状态里，和下一块拼起来再判断。
- **单行上限**：当前行超过 `Constant.Terminal.OBSERVATION_LINE_MAX_CHARS`（现有常量，读取渲染时也按它截）的 **4 倍**后，多出的可打印字符丢弃，这一行完成时末尾加 `" …[line truncated]"`。防止没有换行的超长输出撑爆内存。
- **不做**：光标上下移动、颜色保留、Unicode 宽度。

**1.2 `ExecutionResult`**（`apps/backend/app/tools/tool_handler/terminal/execution_result.py`）

- 加方法 `end_marker(self) -> str`，把 `execute_terminal.py` 中 `_render_content` 的 `marker = {...}[result.end]` 整张表原样搬过来。理由：读取运行中命令时，命令结束后要显示同样的结局标记，标记表只能有一份。
- **删除字段 `output`**。输出归 `CommandOutput`，`ExecutionResult` 只表达结局（`end` / `exit_code` / `signal` / `failure`）。同步改类 docstring 和模块顶部 `CommandEnd` 的注释里提到输出的部分。

**1.3 解码器搬家**：`tool_handler/terminal/output_collector.py` 里的 `_IncrementalOutputDecoder` 原样搬到新文件 `tool_handler/terminal/output_decoder.py`，改名 `IncrementalOutputDecoder`（去掉下划线，它被另一个模块使用）。然后**删除 `output_collector.py`**，它的职责由 1.5 的 `CommandOutput` 取代。

**1.4 新文件 `apps/backend/app/core/tool/terminal_output_window.py`**：卡片输出窗口的唯一规则。后端收集器（`app/tools`）和 snapshot 投影（`assistant_transport`）都用它，所以放在两者都能依赖的 `core/tool`。

```python
def trim_output_window(text: str) -> tuple[str, int]:
    """把卡片输出裁到最近一段，返回 (保留的文本, 这次裁掉的行数)。"""
```

- 窗口 `window = Constant.Terminal.DISPLAY_OUTPUT_WINDOW_CHARS`。在 `config/constant.py` 的 `class Terminal` 里新增 `DISPLAY_OUTPUT_WINDOW_CHARS: int = 256 * 1024`，带中文注释：「卡片里命令输出保留的最近字符数；完整输出在日志文件里。每次重新订阅，首帧都要带上全部卡片，窗口越大重连越慢」。
- 规则：`len(text) <= window + window // 4` 时原样返回 `(text, 0)`。多留四分之一是为了不在每个输出块都触发裁剪，每次裁剪都要整段重发卡片。否则从 `cut = len(text) - window` 起找下一个 `\n`：
  - 找到、且不是最后一个字符：保留它之后的文本，在整行边界上切；
  - 找不到（最后一段是一整行超长输出）：保留 `text[cut:]`，硬切。
  - 裁掉的行数 = 被丢弃部分里 `\n` 的个数。
- 硬切可能切在控制序列中间，终端模拟器能容忍。docstring 写明这一点。

**1.5 新文件 `apps/backend/app/tools/tool_handler/terminal/command_output.py`**

```python
@dataclass(frozen=True)
class CommandOutputSummary:
    """命令结束后交给工具层的输出：卡片窗口、模型用的头尾与日志位置。"""

    display: str                # 原始文本的最近一段（经 trim_output_window）
    display_omitted_lines: int  # 卡片窗口前面省略的行数
    model_head: str             # 整理后的文本：没溢出时是全文，溢出时是前 MAX_OUTPUT_CHARS 个字符
    model_tail: str             # 溢出时是最后约 MAX_OUTPUT_CHARS 个字符（整行）；没溢出时为 ""
    model_total_chars: int      # 整理后全文的字符数（含换行）
    model_total_lines: int      # 整理后全文的行数
    log_path: str               # 日志文件的工作区相对路径（POSIX）；没写或写失败时为 ""
    overflowed: bool            # 整理后全文是否超过 MAX_OUTPUT_CHARS


class CommandOutput:
    """一条命令输出的唯一收集点：解码一次，分给卡片、模型与运行中读取；内存有上限，全文只在磁盘。"""

    def __init__(
        self,
        *,
        workspace_root: Path,
        on_text: Callable[[str], None] | None = None,
        on_lines: Callable[[list[str], str], None] | None = None,
    ) -> None: ...
    def feed(self, raw_chunk: bytes) -> None: ...
    def finish(self) -> None: ...
    def summary(self) -> CommandOutputSummary: ...
```

- 线程：`feed` 在 worker 读线程调用，`finish` 与 `summary` 在作业线程调用，用一把 `threading.Lock` 保护。`feed` **不得抛异常**，它在读线程的回调里，抛了会打断后续输出。
- `feed(raw_chunk)`：已结束就丢弃。否则 `text = decoder.decode(raw_chunk)`，然后：
  1. **卡片**：`on_text(text)`（实时通道，原样）；`self._display += text`，再 `self._display, dropped = trim_output_window(self._display)`，`self._display_omitted_lines += dropped`。
  2. **模型与读取**：`lines = normalizer.feed(text)`，交给 `_accept_lines(lines)`（见下），然后 `on_lines(lines, normalizer.pending_line())`。即使 `lines` 为空也调用，因为未完成行可能变了。
- `_accept_lines(lines)`，每行按 `line + "\n"` 计入：
  - **没溢出时**：追加到 `self._head`（字符串），累计 `total_chars` / `total_lines`。一旦 `len(self._head) > MAX_OUTPUT_CHARS`，就进入溢出：
    - 打开日志文件：路径 `workspace_tool_artifact_dir(workspace_root) / f"{uuid4().hex}.txt"`（与 `ToolOutputBudget._write_artifact` 同目录、同命名），`mkdir(parents=True, exist_ok=True)`，`open(..., "w", encoding="utf-8", newline="")`；
    - 把 `self._head` 全部写进去；
    - 把 `self._head` 截成前 `MAX_OUTPUT_CHARS` 个字符；
    - 尾部 `self._tail`（`collections.deque[str]`，按行存）放进本次溢出时的整行，从左边弹出，直到总字符数不超过 `MAX_OUTPUT_CHARS`。
  - **溢出之后**：每行写进日志文件，追加到 `self._tail` 并从左边弹出到不超过 `MAX_OUTPUT_CHARS`，累计总数。`self._head` 不再变。
  - 写文件遇到 `OSError`：记一次 `log.warning("terminal_output_log_write_failed", extra={"msg": "命令输出日志写入失败，之后只保留头尾", "data": {"path": ..., "error": str(exc)}})`，关闭文件，`log_path` 置空，之后不再写。内存仍然有上限，主流程不受影响。
- `finish()`：只执行一次，后续调用直接返回。依次执行：
  - 冲出解码器尾部 `decoder.decode(b"", final=True)`，按 `feed` 同样处理；
  - `normalizer.flush()` 出来的行交给 `_accept_lines`，并 `on_lines(flushed, "")`；
  - 关闭日志文件；
  - 记 `log.info("terminal_output_collected", extra={"msg": "命令输出收集完成", "data": {"total_chars": ..., "total_lines": ..., "overflowed": ..., "log_path": ..., "display_omitted_lines": ...}})`。
- `summary()`：没 `finish` 就调用时抛 `RuntimeError`（调用顺序错误属于代码缺陷）。`log_path` 用 `target.relative_to(workspace_root.resolve()).as_posix()`，与 `_write_artifact` 的返回格式一致。
- 模块 docstring 写清：为什么是唯一收集点（一次解码、一个整理器，三个读者各取所需）；为什么不在内存保留全文（后台服务可以运行一小时）；为什么只在超过模型预算时才写文件（短命令不产生文件，与现有超长输出落盘的时机一致）。

**1.6 `ToolOutputBudget` 头尾截断提成可复用函数**（`core/tool/execute/tool_output_budget.py`）

现在的 `_truncate_content(content, artifact_path)` 从一整段文本里取头和尾。`CommandOutput` 溢出后手里只有头尾两段，算法应当只有一份：

```python
def fit_head_tail(
    head: str, tail: str, *, total_chars: int, total_lines: int, artifact_path: str, max_chars: int,
) -> str:
    """从全文的前缀 head 与后缀 tail 里取「头 + 截断标记 + 尾」，总长不超过 max_chars。"""
```

- 函数体就是 `_truncate_content` 现在的算法，只改两处：头从 `head` 的开头取，尾从 `tail` 的末尾取；`total` 与 `total_lines` 改用参数，不再从文本里数。前提：调用方保证 `head` 至少有 `max_chars` 个字符或就是全文，`tail` 同理。写进 docstring。
- `_truncate_content` 改为调用 `fit_head_tail(content, content, total_chars=len(content), total_lines=content.count("\n"), artifact_path=..., max_chars=self._max_chars)`，行为逐字不变。现有 `ToolOutputBudget` 的测试必须原样通过。
- **Q4**：模块 docstring 中「落盘 artifact 与 display_data 都保持完整原文」改为：「模型通道落盘的是完整正文（终端命令是整理后、不含控制序列的全文，由 `CommandOutput` 边跑边写）；`display_data` 不归这里截断（终端卡片只保留最近一段，见 `core/tool/terminal_output_window.py`）」。

**1.7 `LocalExecutionBackend.execute`**（`tool_handler/terminal/local_backend.py`）

- 参数 `output_sink: Callable[[str], None] | None` 改为 `output: CommandOutput`（必填）。调用方创建并持有它，执行后端只负责喂入和封存。
- `_observe` 里的 `collector.feed(...)` 改为 `output.feed(...)`。原来 `output = collector.finish()` 的位置改为 `output.finish()`。**所有提前返回的分支**（`_resolve_command` 失败、启动前已被要求停止、`TerminalWorkerUnavailableError`）也要先 `output.finish()`，最简单的写法是整个方法体包一层 `try: ... finally: output.finish()`。
- `_result`、`_start_failed` 去掉 `output` 参数，`ExecutionResult(...)` 不再传 `output=`。
- 同步 docstring：「回收完整输出」改为「输出交给调用方的 `CommandOutput`」；`output` 参数说明写清「在 worker 读线程里喂入，不得抛异常」。

**1.8 `execute_terminal`**（`tools/tool_handler/execute_terminal.py`）

- `_execute_command` 里创建 `output = CommandOutput(workspace_root=Path(execution_context.workspace_root), on_text=channel.emit if channel is not None else None)`，传给 `backend.execute(..., output=output)`。S3 会再给它加 `on_lines`。
- `finished` 列表改为保存 `(result, output.summary())`，或者用一个小的 frozen dataclass 保存二者，`work` 返回前写入。`_background_conclusion`、`_render_content`、`_display_data` 都改为接收 `(result, summary)`。
- `_render_content(result, summary)`：
  - `prefix` 行与现在相同：`result.end_marker()`，有 `failure` 时再加一行。
  - 输出部分：
    - 没溢出时用 `summary.model_head`（就是整理后的全文），交给后面统一的 `ToolOutputBudget`。
    - 溢出时用 `fit_head_tail(summary.model_head, summary.model_tail, total_chars=..., total_lines=..., artifact_path=summary.log_path, max_chars=Constant.Tools.MAX_OUTPUT_CHARS - len(prefix_text) - 1)`，这样整段正文不超过预算，通用预算不会再截一次、也不会再落一份盘。
  - docstring 写明这两条路径。
- `_display_data(command, cwd, result, summary)`：`output=summary.display`，加 `omitted_lines=summary.display_omitted_lines`、`log_path=summary.log_path`。
- `terminal_command_finished` 日志的 `data` 里加 `output_overflowed`、`output_log_path`。

**1.9 卡片展示数据**（`tools/display/terminal_display.py` 的 `build_terminal_display_data`）

- 新增参数 `omitted_lines: int` 与 `log_path: str`。`omitted_lines > 0` 时写 `data["output_omitted_lines"]`，`log_path` 非空时写 `data["output_log_path"]`，两者都是 0 / 空时不写键。
- docstring 中「不截断输出」改为「输出是调用方给的最近一段（窗口规则见 `core/tool/terminal_output_window.py`），完整输出在 `output_log_path`」。

**1.10 实时卡片投影封顶**（`assistant_transport/projection/tool_runtime_event_projection.py` 的 `_plan_terminal_output`）

- 在已有 `current_output`（字符串）上追加 `data.text` 之前，先算 `kept, dropped = trim_output_window(current_output + data.text)`：
  - `dropped == 0` 且 `kept` 就是拼接结果：照旧发 `append-text`。
  - 否则发 `set` `(*base, "display_data", "output")` 为 `kept`，再发 `set` `(*base, "display_data", "output_omitted_lines")` 为「原值（没有时按 0）+ `dropped`」。
- `display_data is None` 时的首帧（`set` 整个 `display_data`）也先过一次 `trim_output_window`，有裁剪就带上 `output_omitted_lines`。
- 函数 docstring 补一句：「卡片输出只保留最近一段，规则与终态展示数据同源（`trim_output_window`）；终态展示数据到达后整体覆盖实时预览」。
- 前端不需要任何截断逻辑：它只照着 `set` / `append-text` 更新。

### S2. 观察链路接入管道来源 + 读取算法提到基类

**2.1 行缓冲的管道写入**（`line_buffer.py`）

给 `TerminalLineBuffer` 加方法：

```python
def append_lines(self, lines: Sequence[str], pending: str) -> None:
    """管道来源（运行中的命令）的写入：完成行逐条封口，未换行的那一行作为唯一的可变行。

    屏幕来源走 :meth:`apply`；两条路径共用封口、淘汰与行号规则。管道没有屏幕几何，rows /
    columns 保持 0，也没有备用屏幕。
    """
```

- 实现：每个完成行调用 `self._append_history(line + "\n")`。
- 然后 `self._live = LiveRegion((pending,) if pending else (), 0, len(pending))`。

**2.2 观察的管道写入**（`observation.py`）

给 `TerminalObservation` 加方法：

```python
def pipe_output(self, lines: Sequence[str], pending: str) -> None:
    """管道来源的输出：写进行缓冲、推进信息版本、记下最近输出时刻（output_quiet 等待用）。"""
    self.buffer.append_lines(lines, pending)
    self.output_arrived()
    self.information_version += 1
```

运行中命令的开始与结束**沿用 `shell_event` 的事件词表**，不新增方法：

- 开始：`ready`、`command_started`（带命令文本）。
- 正常退出：`command_returned`（`result={"code": exit_code}`）。
- 其他结局：`end_tracking(<结局>)`。

在 `shell_event` 的 docstring 里补一句：执行模式没有 shell，运行中命令的开始与结束按同一词表喂进来，由 `RunningCommand` 负责。

**2.3 `SessionFacts` → `TerminalFacts`**（`render.py`）

- 改名 `TerminalFacts`；字段 `session_id` 改名 `terminal_id`；新增 `kind: Literal["session", "command"]`（不给默认值，两处构造都显式传）。
- 表头与摘要里写死的 `f"session {facts.session_id}"` 改为 `f"{facts.kind} {facts.terminal_id}"`。共 2 处：`_header_lines` 与 `render_session_summary`。
- `TerminalObservationResult.session_id` 改名 `terminal_id`。
- 全仓 grep `SessionFacts`、`facts.session_id`、`result.session_id` 同步改。

**2.4 共同基类 `ObservedTerminal`**（新文件 `apps/backend/app/tools/terminal_runtime/observed_terminal.py`）

把 `TerminalSessionRuntime` 里的读取算法原样搬过来，会话改为继承它：

```python
class ObservedTerminal(ABC):
    """可观察的终端：会话（PTY）与运行中命令（管道）的共同读取底座。

    读取、条件等待与结果渲染只此一份；两种来源只在「事实快照」「是否活跃」「交付后的清理」三处不同，
    由子类实现。子类在构造时设置 ``terminal_id``、``_condition``（可重入锁上的 Condition）与
    ``_observation``。等待都在 ``_condition`` 上进行，来源写入后 ``notify_all`` 唤醒。
    """

    terminal_id: str
    _condition: threading.Condition
    _observation: TerminalObservation
    _last_read: tuple[object, ...] | None

    def read(self, *, start_line, limit, tail, pattern, wait, is_cancelled) -> TerminalObservationResult: ...
    def _wait(self, evaluator, is_cancelled) -> dict[str, object]: ...
    def _build_result(self, first, last, mode, *, wait_result=None, prefilled=None, logical=False, search=None) -> TerminalObservationResult: ...

    @abstractmethod
    def _facts(self) -> TerminalFacts: """持锁调用：渲染所需的事实快照。"""
    @abstractmethod
    def _is_active(self) -> bool: """持锁调用：来源还在产出（会话 starting / running，命令尚未结束）。"""
    @abstractmethod
    def _on_delivered(self) -> None: """持锁调用：一次观察交付之后的清理（会话清空用户输入标注；命令无事可做）。"""
```

搬移要点（逐条照做，**会话的读取结果必须逐字不变**）：

- `read` / `_wait` / `_build_result` 的代码从 `session_runtime.py` 原样搬，只做以下替换：
  - `self._status not in _ACTIVE` → `not self._is_active()`。
  - 「无变化的重复读取」判定里的 `self._user_marks.empty` → `facts.user_marks is None and not facts.user_typed_in_screen`（先 `facts = self._facts()`；两者等价）。
  - 判定签名里的 `self._status` → `facts.status`。
  - 「no change since your last read」那句里的 `session {self.session_id}` → `{facts.kind} {self.terminal_id}`。
  - `_build_result` 末尾的 `self._user_marks = _UserInputMarks()` → `self._on_delivered()`。
- `TerminalSessionRuntime(ObservedTerminal)`：
  - 设 `self.terminal_id = self.session_id`（`session_id` 属性保留，会话服务、WebSocket、snapshot 都在用）。
  - 实现三个钩子：`_facts` 用现有实现并传 `kind="session"`；`_is_active` 返回 `self._status in _ACTIVE`；`_on_delivered` 清空 `_user_marks`。
  - 删掉被搬走的方法。
  - `prefill` 调用基类 `_build_result`，不变。
- 抽象基类的依据：同一契约当前就有两个生产实现（会话、运行中命令），符合 AGENTS「抽象按需引入」。

**S2 验证**：现有全部终端测试（`tests/test_terminal_*.py`）不改断言照样通过，这一步对会话是纯重构。

### S3. 运行中的命令

**3.1 新文件 `apps/backend/app/tools/terminal_runtime/running_command.py`**

```python
class RunningCommand(ObservedTerminal):
    """一条 execute_terminal 命令运行期间的观察：CommandOutput 交来的整理后行进行缓冲，可读、可等、可停。

    事实边界：只在命令运行期间存在，结束即从登记表移除（结论由 background_registry 回投，不归这里）。
    停止不经这里：经 background_registry 的撤销回调落到 LocalExecutionBackend.stop。
    feed 在 worker 读线程、finish 在作业线程、read 在工具线程调用，全部经 _condition 保护。
    不自己整理输出：整理器只有一个，在 CommandOutput 里（见 S1）。
    """

    def __init__(self, *, task_id: int, command_id: str, command: str, shell_kind: str, cwd: str) -> None: ...
    def feed(self, lines: list[str], pending: str) -> None: ...
    def finish(self, result: ExecutionResult | None) -> None: ...
    def wait_finished(self, timeout_s: float, should_stop: Callable[[], bool]) -> bool: ...
```

- 构造：
  - `terminal_id = command_id`；`_condition = threading.Condition(threading.RLock())`。
  - `_observation = TerminalObservation(shell_kind, "auto")`，然后依次：`shell_event({"kind": "ready"})`、设 `_observation.cwd = cwd`、`shell_event({"kind": "command_started", "command": command})`。
  - `_result: ExecutionResult | None = None`；`_ended = False`；`_last_read = None`。
- `feed(lines, pending)`：持锁，`self._observation.pipe_output(lines, pending)`，`notify_all`。**不得抛异常**：它在 worker 读线程的回调里执行（见 `local_backend.py` 的 `execute` docstring）。
- `finish(result)`：持锁，并且只执行一次（重复调用直接返回）：
  - 按结局喂观察（剩下的行此前已由 `CommandOutput.finish` 经 `feed` 交来）：`result` 为 `None` → `end_tracking("worker_failed")`；`result.end == "exited"` → `shell_event({"kind": "command_returned", "result": {"code": result.exit_code}})`；其他结局 → `end_tracking(result.end)`。
  - 记下 `self._result = result`、`self._ended = True`，`notify_all`。
- `_facts()`：

  ```python
  TerminalFacts(
      terminal_id=self.terminal_id, kind="command",
      status="running" if not self._ended else "ended",
      ended=self._ended,
      end_reason=None if self._result is None else self._result.end,   # 结束后为 CommandEnd
      exit_code=None if self._result is None else self._result.exit_code,
      user_typing=False, user_marks=None, user_typed_in_screen=False,
  )
  ```

  `result` 为 `None` 且已结束时，`end_reason` 写 `"worker_failed"`。
- `_is_active()`：`not self._ended`。`_on_delivered()`：什么都不做（docstring 写明：命令没有用户输入标注）。
- `wait_finished`：`condition.wait(0.25)` 循环，每轮复查 `should_stop()`；结束返回 `True`，超时或 `should_stop()` 为真返回 `False`。

**3.2 新文件 `apps/backend/app/tools/terminal_runtime/running_command_registry.py`**

```python
class RunningCommandRegistry:
    """按 (task_id, command_id) 持有运行中的命令；进程内状态，不持久化、不跨重启。

    命令一定会结束（超时、取消、Run 停止、Task 删除、后端关闭都会让作业收尾），所以登记表不需要另外清理。
    """

    def start(self, *, task_id: int, command_id: str, command: str, shell_kind: str, cwd: str) -> RunningCommand: ...
    def get(self, task_id: int, command_id: str) -> RunningCommand: ...          # 不存在抛 KeyError
    def task_commands(self, task_id: int) -> list[RunningCommand]: ...           # 按开始先后
    def finish(self, task_id: int, command_id: str, result: ExecutionResult | None) -> None: ...


running_commands = RunningCommandRegistry()
```

- `start` 在同一个 `(task_id, command_id)` 已存在时抛 `RuntimeError`：`tool_call_id` 在 Task 内唯一，出现即代码缺陷，不要静默覆盖。
- `finish`：先调用 `RunningCommand.finish(result)`（唤醒正在等待的读取方），再从登记表移除。
- 用 `threading.Lock` 保护字典。

**3.3 接入 `execute_terminal`**（`tools/tool_handler/execute_terminal.py` 的 `_execute_command`）

1. `BackgroundJob.start_in_thread` 之前登记：`running = running_commands.start(task_id=..., command_id=tool_call_id, command=command, shell_kind=<本次实际使用的 shell 名>, cwd=str(cwd))`。shell 名用参数 `shell`；`auto` 时 POSIX 写 `sh`，Windows 写 `cmd`，与 `_resolve_command` 的含义一致。
2. S1 创建 `CommandOutput` 的那一行挪到登记之后，加上 `on_lines=running.feed`。`finish` 时 `CommandOutput` 先把剩下的行交给 `running.feed`，`running_commands.finish` 再落结局，所以读取方能看到最后几行。
3. `work` 改为：

   ```python
   result: ExecutionResult | None = None
   try:
       result = backend.execute(...)
   finally:
       if channel is not None:
           channel.finish()
       running_commands.finish(execution_context.task_id, tool_call_id, result)
   ```

   `finish` 必须在结论交付（`return self._background_conclusion(...)`）之前：读取方先看到结束，`<background_result>` 后到。
4. `_placeholder_observation` 多接收一个 `terminal_id` 参数（用 `execution_context.tool_call_id`，这是冲突改写后的真实 id），两种情形（`background` / `detached`）的正文都加上。目标文案：

   ```text
   the command is running in the background (timeout 300s); keep working. terminal_id: <tool_call_id>. Read its output with terminal_read and stop it with terminal_stop; its result will arrive as a <background_result> message for this tool call (use wait_messages if you need to pause for it).
   ```

5. `_DESCRIPTION` 最后一句（以 `Programs that must keep running (servers, watchers) or need interaction do not fit this tool` 开头、以 `so leave them to the user.` 结尾）替换为下面这段，并同步上方中文译文注释：

   ```text
   A server or watcher you only need while working (to check a page, wait for a build) can run with background=true: read its output with terminal_read (wait until=match a ready line) and end it with terminal_stop when done. Background commands still stop at their timeout, and you cannot finish your answer while one of yours is still running, so stop what you started; do not use wait_messages on a server, since it never finishes on its own. Programs that need interaction do not fit this tool.
   ```

   模块顶部注释说「描述不点名其他工具」，那条针对的是交互终端工具；本次的 `terminal_read` / `terminal_stop` 与 `execute_terminal` 在所有内置 Agent 里同时出现，可以点名。把那条注释改为「不点名 terminal_write（它只给侧栏对话）」。
6. `apps/backend/app/tools/tool_models/execute_terminal_args.py` 的 `_BACKGROUND_DESCRIPTION` 改为：

   ```text
   true returns immediately with a terminal_id and delivers the result later as a <background_result> message; terminal_read reads its output meanwhile. false or null waits here until the command ends. Use wait_messages to wait for background work that will finish on its own.
   ```

### S4. 工具层

**4.1 目录与文件改名**

- `git mv apps/backend/app/tools/tool_handler/terminal_session apps/backend/app/tools/tool_handler/terminal_tools`。
- `git mv apps/backend/app/tools/tool_models/terminal_session_args.py apps/backend/app/tools/tool_models/terminal_tools_args.py`。
- 全仓改 import（`grep -rn "terminal_session\b\|terminal_session_args" apps/backend`）。测试文件名不改。

**4.2 参数模型**（`terminal_tools_args.py`）

- `_SESSION_ID_DESCRIPTION` 改为：

  ```python
  _TERMINAL_ID_DESCRIPTION = (
      "Terminal ID from terminal_list: a terminal the user opened (term_…), or the terminal_id of "
      "your command still running from execute_terminal."
  )
  ```

- `TerminalReadArgs.session_id` → `terminal_id`（描述用上面的常量）。
- `TerminalWriteArgs.session_id` → `terminal_id`，描述单独写：`"Terminal ID of a terminal the user opened, from terminal_list."`（填入只针对用户终端）。
- 新增 `TerminalStopArgs`：只有 `terminal_id: str = Field(min_length=1, max_length=128, description="terminal_id of your command still running from execute_terminal.")`。
- 模块 docstring 改为「终端工具的输入契约（list / read / write / stop）」。

**4.3 按 id 查找终端**（`terminal_tools/common.py`）

```python
def find_terminal(task_id: int, terminal_id: str) -> ObservedTerminal:
    """按 id 找本 Task 的终端：先查用户会话，再查运行中的命令（顺序固定，见设计稿 Q3）。

    异常:
        TerminalSessionNotFoundError: 两处都没有。
        TerminalSessionOwnershipError: 会话存在但不属于该 Task。
    """
```

- 先 `service_depends.get_terminal_session_service().require(terminal_id, task_id)`。捕获 `TerminalSessionNotFoundError`，再查 `running_commands.get(task_id, terminal_id)`。捕获 `KeyError`，抛：

  ```python
  TerminalSessionNotFoundError(
      f"no terminal {terminal_id} in this task: neither a terminal the user opened nor a command of "
      "yours that is still running (a finished command's result arrives as a <background_result> message)"
  )
  ```

- `errors.py` 中 `TerminalSessionNotFoundError.advice` 改为 `"Call terminal_list to see the user's terminals and your running commands."`。

**4.4 `terminal_read`**（`terminal_tools/read.py`）

- 参数 `session_id` → `terminal_id`，`runtime = find_terminal(execution_context.task_id, terminal_id)`，其余调用不变（`runtime.read(...)`）。
- `needs_user_surface` 删除（取默认 `False`），`group = TOOL_GROUP_TERMINAL`。
- 描述改为（同步中文译文注释）：

  ```text
  Read a terminal's output like a text file: a terminal the user opened, or your command still running from execute_terminal (by its terminal_id). Logical lines (soft wraps merged) come as 'line_number│text', with absolute line numbers that are never reused. Lines from 'live from' onward may still be redrawn; earlier lines are final. While a full-screen app (vim, less, top) is open in a user terminal, a read without start_line returns its screen as 's<row>│text' rows instead. Use wait to block for a condition instead of polling; for your running command, until=command_returned means the command ended.
  ```

**4.5 `terminal_list`**（`terminal_tools/list.py`）

- 输出先列用户会话（现有），再列 `running_commands.task_commands(task_id)` 的每一条摘要：在命令的锁内用 `render_session_summary(command._facts(), command._observation)` 生成。为此给 `RunningCommand` 加一个公开方法 `summary(self) -> str`（持锁调用 `render_session_summary`），列表调用它，不要在工具里碰私有属性。
- 没有任何会话和命令时输出 `no terminals`。
- 展示数据 `build_terminal_session_list_display_data(len(sessions) + len(commands))`；函数名与参数不变。
- `needs_user_surface` 删除，`group = TOOL_GROUP_TERMINAL`。
- 描述改为（同步中文译文注释）：

  ```text
  List this task's terminals, one line each: the terminals the user opened (what each is doing now: at prompt, running a command, full-screen app, user typing, no shell integration, ended; its cwd and last command result), then your commands still running from execute_terminal. Only the user opens and closes their terminals. Call it before reading a terminal, and when picking up work in a new run.
  ```

**4.6 `terminal_write`**：参数 `session_id` → `terminal_id`；仍用 `service_depends.get_terminal_session_service().require(...)`（只能填用户终端，不经 `find_terminal`）；`needs_user_surface=True` 与分组不变。

**4.7 新工具 `terminal_stop`**（新文件 `terminal_tools/stop.py`，照 `read.py` 的结构写）

- 流程：
  1. 用 `get_terminal_session_service()` 的查找方式判断 `terminal_id` 是不是本 Task 的用户会话（调用 `require`，成功即是）。是 → 返回受控错误：`"terminal {id} is a terminal the user opened; you can stop only your own commands still running from execute_terminal."`（抛 `TerminalSessionStateError`，经 `with_terminal_errors` 归一化）。
  2. `command = running_commands.get(task_id, terminal_id)`。`KeyError` → `TerminalSessionNotFoundError(f"no running command {terminal_id} in this task; it may have already finished.")`。
  3. `background_registry.request_cancel(task_id, terminal_id, driving=False)`。返回 `False` 表示已在停止中或条目已不在，不报错，继续下一步。为什么 `driving=False`：目标是另一次调用登记的后台条目，不是当前正在执行的工具调用。
  4. `command.wait_finished(Constant.Terminal.COMMAND_STOP_WAIT_SECONDS, should_stop)`（`should_stop` 用 `cancelled(execution_context)`）。
     - 结束了 → `tool_success(name, f"stopped command {id}. Its final output arrives as a <background_result> message.")`。
     - 超时 → `tool_error(name, f"asked command {id} to stop, but it has not exited after {N}s; its result will still arrive as a <background_result> message.")`。
  5. 日志 `terminal_command_stop_requested` / `terminal_command_stopped`（`task_id`、`terminal_id`、`stopped`）。
- 定义：`group = TOOL_GROUP_TERMINAL`，`execution_mode="thread"`（必须：登记表在进程内），不设 `needs_user_surface`，`timeout_seconds = COMMAND_STOP_WAIT_SECONDS + 5`。
- 描述（同步中文译文注释）：

  ```text
  Stop your command still running from execute_terminal (by its terminal_id), together with every process it started. Use it to end a dev server or watcher once you are done with it. Its final output still arrives as a <background_result> message. Terminals the user opened cannot be stopped.
  ```

- 常量：在 `config/constant.py` 的 `class Terminal` 里加 `COMMAND_STOP_WAIT_SECONDS: float = 10.0`（带中文注释）。

**4.8 停止入口改名**

- `core/tool/background/background_registry.py`：`request_user_cancel` → `request_cancel`，docstring 补一句「用户点卡片停止与 Agent 的 `terminal_stop` 共用」。逻辑不改。
- 调用方：`assistant_transport/service/transport_assistant_service.py` 与测试（`grep -rn request_user_cancel apps/backend`）。

**4.9 登记**

- `core/tool/schemas/tool_names.py`：新增 `TOOL_TERMINAL_STOP = "terminal_stop"`，三处都要加（常量、`ToolName` Literal、`ALL_TOOL_NAMES`）；`core/tool/schemas/__init__.py` 再导出。
- `terminal_tools/__init__.py`：`build_terminal_definitions` 加上 `TerminalStopTool`，docstring 的工具数改为 4。
- `core/agents/define_agents.py`：
  - 主 Agent 加 `TOOL_TERMINAL_STOP`。
  - 通用子 Agent、只读子 Agent：凡有 `TOOL_EXECUTE_TERMINAL` 的地方，紧跟着加 `TOOL_TERMINAL_LIST`、`TOOL_TERMINAL_READ`、`TOOL_TERMINAL_STOP`。
- **不要**改 `task_tool_scope.py`、`task_kind.py`：去掉 `needs_user_surface` 后会自动给子 Agent 与 Team 节点。

### S5. 前端（`apps/desktop`）

- `lib/assistant/tools/tool-catalog.ts`：
  - `sessionId` 函数改名 `terminalId`，读 `args.terminal_id`。
  - `terminal_read`、`terminal_write` 的 target 改用它；`terminalInput` 里的 `sessionId(args)` 同步。
  - 新增 `terminal_stop: { verb: "停止命令", icon: "x", target: terminalId },`（两空格缩进，形状与其他条目一致）。
  - 同步 `tests/unit/tool-catalog.test.ts`。
- `components/conversation/tools/call-views/terminal-session-view.tsx`：
  - 读 `data.terminal_id`（原 `data.session_id`）。
  - `RevealSessionAction` 的「会话是否还在」判断不变：运行中的命令不在会话列表里，自然不显示「在终端中打开」。
  - 列表的「N 个会话」改为「N 个终端」。
- 命令卡片的省略提示（S1 的展示数据字段）：
  - 新文件 `components/conversation/tools/terminal-output/omitted-notice.tsx`，导出 `OmittedOutputNotice({ omittedLines, logPath, links })`。`omittedLines` 不大于 0 时不渲染。否则一行灰字「前面省略 {omittedLines} 行」；`logPath` 有值时后面接一个文字按钮「打开完整日志」，点击用 `resolvePathText(logPath, { workspaceRoot: links.workspaceRoot })` 解析后交 `links.open`（与卡片「启动目录」按钮同一条链路）。解析结果为空时只显示文字。
  - `terminal-result-view.tsx`：读 `data.output_omitted_lines`（数字）与 `data.output_log_path`（字符串），在 `TerminalResultBody` 输出预览上方渲染 `OmittedOutputNotice`；`TerminalReaderOverlay` 把同样两个值传给阅读器。
  - `terminal-output/reader.tsx`：`TerminalOutputReader` 新增可选 props `omittedLines?: number`、`logPath?: string`，在输出区域顶部渲染同一个 `OmittedOutputNotice`（`links` 用已有的 `links` prop：`{ workspaceRoot: links.getWorkspaceRoot(), open: links.onOpenLink }`，按现有类型适配）。
  - 「复制输出」只复制卡片里的这一段，不改。
  - 单测：`omittedLines` 为 0 时不渲染；有 `logPath` 时点击按钮调用打开链接；没有 `logPath` 时只有文字。
- 同步测试：`tests/unit/call-presentation.test.tsx` 等引用 `session_id` 展示字段的单测，以及 `tests/e2e/test-server.mjs`（`grep -rn "session_id" apps/desktop/tests apps/desktop/lib apps/desktop/components`，只改终端相关的）。

### S6. 文档与展示契约

- 后端 `tools/display/terminal_display.py`：`build_terminal_observation_display_data` 输出的 `session_id` 键改为 `terminal_id`（读 `result.terminal_id`）。
- `core/tool/tool_ui_display_contract.md` 的 `terminal-result`：
  - `output` 的说明改为「原始文本的最近一段（窗口见 `core/tool/terminal_output_window.py`）」；
  - 新增可选字段 `output_omitted_lines`（前面省略的行数）与 `output_log_path`（完整输出日志的工作区相对路径，只在输出超过模型预算时存在）；
  - 写明实时预览与终态同一窗口规则、终态整体覆盖实时预览。
- `core/tool/tool_ui_display_contract.md`：
  - 第 3 节表格加一行 `terminal_stop`，kind 写「无」，第三列照抄 `browser_tabs` 那一行的写法。
  - `terminal-session` 的字段说明里 `session_id` 改为 `terminal_id`。
- `tools/terminal_runtime/interaction_contract.md`：工具清单补 `terminal_stop`，并说明 `terminal_list` / `terminal_read` 也覆盖运行中的命令、开放给所有 Agent。
- `AGENTS.md`，只改三处，不扩写：
  - 「进程生命周期边界」交互终端一段，「Agent 只能列出、读取，以及把一行命令填进……」之后加：「`terminal_list` / `terminal_read` 也覆盖 Agent 自己运行中的 `execute_terminal` 命令（`terminal_runtime/running_command*`，命令结束即移除），`terminal_stop` 只停这类命令；这三个工具给所有 Agent，`terminal_write` 只给侧栏对话」。同时把同一段末尾「终端工具按 Task 类型规则只给侧栏对话」改为「`terminal_write` 按 Task 类型规则只给侧栏对话」。
  - 「代码目录边界」中描述 `ExecuteTerminalTool` 的那句后加：「运行中命令的观察归 `tools/terminal_runtime/running_command*`（与交互会话共用读取底座 `observed_terminal`）；给模型的命令输出统一经 `output_normalizer` 去掉控制序列」。
  - 「工具 UI 渲染边界」已注册 kind 列表里，`terminal_*→terminal-session` 后加「（`terminal_stop` 无展示 `kind`）」。
  - 「代码目录边界」`core/tool/background` 那一段里「（作业带卡片终态时一并写入展示数据，如后台命令的完整输出）」改为「（作业带卡片终态时一并写入展示数据，如后台命令的最近一段输出）」。上面「代码目录边界」新增的那句末尾再接：「命令输出只在 `tool_handler/terminal/command_output` 收集一次，卡片只留最近一段（`core/tool/terminal_output_window`），全文超过模型预算时边跑边写工作区日志」。
- 其他设计稿（`terminal-gap-assessment.md`、`terminal-agent-read-prefill-plan.md`、`terminal-ownership-redesign.md`）指向本文的链接与状态行，主设计会话已改好，不要动。

### 测试

新增测试文件，用例名用英文描述行为，docstring 中文。

**`apps/backend/tests/test_terminal_output_normalizer.py`**（纯单测，不需要 worker）：

1. 颜色码被去掉：`"\x1b[31mred\x1b[0m\n"` → `["red"]`。
2. `\r` 覆盖：`"10%\r20%\r100%\n"` → `["100%"]`；`"abc\rX\n"` → `["Xbc"]`；`"abc\r\n"` → `["abc"]`。
3. `\b`：`"ab\bc\n"` → `["ac"]`。
4. 行内擦除：`"abcdef\r\x1b[K12\n"` → `["12"]`。
5. OSC 8 超链接只留文字（`ESC \` 与 `BEL` 两种结束都测）。
6. 跨块：`feed("a\x1b[")` 返回 `[]`，再 `feed("31mb\n")` 返回 `["ab"]`。
7. `flush` 返回未完成行；`pending_line` 不消费。
8. 制表符保留；`BEL` 与其他 C0 被丢弃；`\x9b31m` 当 CSI 丢弃。
9. 超长行被截断并带 `…[line truncated]`（测试里 monkeypatch 常量调小）。
10. 随机切块逐块 `feed` 的结果与整段一次 `feed` 再 `flush` 的结果一致。

**`apps/backend/tests/test_command_output.py`**（纯单测，直接喂字节，`workspace_root` 用 `tmp_path`；常量用 monkeypatch 调小）：

1. 短输出：`summary()` 的 `display` 是原始文本（含 `\x1b[31m`），`model_head` 是整理后全文，`overflowed` 为假，`log_path` 为空，工作区里没有产生文件。
2. 超过 `MAX_OUTPUT_CHARS`：`overflowed` 为真；日志文件存在，内容等于整理后的全文；`model_head` 是全文前缀、`model_tail` 是全文后缀；`log_path` 是 `.cosir/tool-artifacts/` 下的相对路径。
3. 超过 `DISPLAY_OUTPUT_WINDOW_CHARS`：`display` 长度不超过窗口的 1.25 倍、以整行开头；`display_omitted_lines` 等于被裁掉的行数。
4. `on_text` 收到全部原始片段；`on_lines` 收到的完成行拼起来等于整理后全文，最后一次调用的 `pending` 为 `""`。
5. `finish` 之后的 `feed` 被丢弃；`finish` 重复调用无副作用；没 `finish` 就 `summary()` 抛 `RuntimeError`。
6. 日志写入失败（monkeypatch `open` 抛 `OSError`）：`feed` 不抛异常，`log_path` 为空，`model_head` / `model_tail` 仍正确。
7. 多字节字符跨块（UTF-8 汉字拆在两块里）解码正确（原 `output_collector` 测试的同类用例搬过来）。

**`apps/backend/tests/test_terminal_output_window.py`**：未超过 1.25 倍窗口时原样返回；超过时按整行切、行数正确；没有换行时硬切到窗口长度。

**`fit_head_tail`**：在现有 `ToolOutputBudget` 测试文件里补一条，`fit_head_tail(content, content, ...)` 与原截断结果逐字相同；头尾分开传入时，结果的头来自 `head`、尾来自 `tail`、长度不超过 `max_chars`。

**投影裁剪**（在现有实时输出投影测试里补）：连续追加超过窗口后，snapshot 里 `display_data.output` 不超过窗口的 1.25 倍，`output_omitted_lines` 累计正确；未超过时仍只发 `append-text`。

**`apps/backend/tests/test_running_command.py`**（纯单测，直接构造 `RunningCommand` / 登记表，不需要 worker）：

1. 喂入多行后 `read()` 返回 `command <id> · sh · running · executing \`<命令>\` (cmd 1)` 开头的表头和带行号的行。
2. 未换行的提示行出现在可变区，`wait=match` 能命中它（如服务打印的 `Listening on :3000`，不带换行）。
3. 另一个线程稍后 `feed` 就绪行时，`wait=match` 被唤醒并命中；`wait=command_returned` 在 `finish` 后返回 `command_returned`；`wait=output_quiet` 在静默后返回。
4. `finish(ExecutionResult(end="exited", exit_code=3, ...))` 后读取的表头含 `exited` 与 `exit 3`；`end="cancelled"` 时含 `cancelled`。
5. 登记表：`finish` 后 `get` 抛 `KeyError`；重复 `start` 抛 `RuntimeError`；不同 `task_id` 互相看不到；`task_commands` 按开始先后。
6. 行缓冲淘汰：monkeypatch `MAX_BUFFER_LINES` 调小，超出后最早的行被淘汰、行号不复用。

**`apps/backend/tests/test_terminal_unified_read_tools.py`**（真跑命令，照 `test_execute_terminal_background.py` 的写法：`requires_terminal_worker`、POSIX 限定、替换 `deliver_background_result` 记录投递）：

1. 后台跑 `printf 'booting\nready\n'; sleep 30` → `terminal_read(terminal_id=<call id>, wait={"until": "match", "pattern": "ready", ...})` 命中，正文含 `ready`。
2. `terminal_list` 列出这条命令（含 `command <call id>`）。
3. `terminal_stop(terminal_id=<call id>)` 返回成功；随后收到 `end_reason == "cancelled"` 的后台结论；再 `terminal_read` 返回「no terminal」错误。
4. 对用户会话 id 调 `terminal_stop` 返回受控错误；对不存在的 id 返回「no running command」。
5. 读不存在的 id 返回错误；用另一个 `task_id` 读同一个命令 id 也返回错误。
6. 前台跑 `printf '\033[31mred\033[0m\n'`：观察 `content` 只有 `red`、没有 `\x1b`；`display_data` 的输出仍含 `\x1b[31m`。
7. 后台占位 `content` 含 `terminal_id: <call id>`。
8. 等待期间命令自己结束：`sleep 1; printf 'x\n'; exit 3` 后台跑，`terminal_read(wait={"until": "match", "pattern": "never", "timeout_ms": 10000})` 在命令结束时就返回（耗时远小于 10 秒），表头含 `exit 3`。

**现有测试要同步的**：

- 会话读取相关测试：`SessionFacts` / `session_id` 改名后的引用；读取结果文本应逐字不变（S2 的验证点）。
- `session_id` 参数改名 `terminal_id`：`tests/test_terminal_session_tools.py`、`tests/test_strict_tool_contract.py` 等（`grep -rn "session_id=" apps/backend/tests` 里调用 `terminal_read` / `terminal_write` 的地方）。
- `request_user_cancel` 改名：`tests/test_background_registry.py`、`tests/test_execute_terminal_background.py`、`tests/test_execute_terminal_execution.py`。
- `tests/test_task_tool_scope.py`：子 Agent 与 Team 节点现在拿到 `terminal_list` / `terminal_read` / `terminal_stop`，`terminal_write` 仍只给侧栏对话，补断言。
- 展示数据 `terminal_id` 键：`tests/test_terminal_session_tools.py` 中对 `display_data` 的整体断言。
- `_OutputCollector` 相关测试（`tests/test_terminal_output_streaming.py`）改为测 `CommandOutput`，或并入 `test_command_output.py`，不留指向已删模块的用例；构造 `ExecutionResult(output=...)` 的测试与替身（`tests/live_cold_support.py`、`test_background_run_completion.py`、`test_execute_terminal_execution.py`、`test_terminal_output_streaming.py`）去掉 `output`，替身的 `execute` 改为接收 `output: CommandOutput` 并往里喂字节。
- 固定了后台占位文案或 `_DESCRIPTION` 原文的测试（`grep -rn "keep working\|leave them to the user" apps/backend/tests`）。

### 验收命令

从仓库根目录运行（后端 pytest 要在 `apps/backend` 下运行，展示目录守卫用相对路径找文件），全部通过才算完成：

```bash
cargo build -p cosir-terminal-worker
```

```bash
cd apps/backend && uv run pytest
```

已知 3 个与本次无关的原有失败：`test_live_cold_parity.py::test_a_failure_before_the_response_is_committed_settles_its_calls`，以及 `test_model_request_cancellation.py` 的两个 `timeout-*` 用例。除此之外不得有失败。

```bash
cd apps/backend && uv run ruff check app tests
```

新增的 `app/` 文件必须过严格类型检查：

```bash
cd apps/backend && uv run mypy --config-file mypy.strict.ini app/tools/terminal_runtime/output_normalizer.py app/tools/tool_handler/terminal/command_output.py app/tools/tool_handler/terminal/output_decoder.py app/core/tool/terminal_output_window.py app/tools/terminal_runtime/observed_terminal.py app/tools/terminal_runtime/running_command.py app/tools/terminal_runtime/running_command_registry.py app/tools/tool_handler/terminal_tools/stop.py
```

```bash
cd apps/desktop && npm run test:unit && npx tsc --noEmit && npm run knip
```

```bash
cd apps/desktop && npm run test:e2e
```

（E2E 用无头浏览器；不要运行 `test:e2e:desktop`，它会弹出真实窗口。）

lint 只检查改动过的前端文件（全仓有与本次无关的历史问题）：

```bash
cd apps/desktop && git diff --name-only --relative -- . | grep -E '\.(ts|tsx|mjs)$' | xargs npx eslint
```

### 交付时报告

1. 每一步做了什么，偏离本文的地方和理由。
2. `git diff --stat`，与第一部分的改动账本对账，偏差大要说明。
3. 上面全部验收命令的真实输出（失败要贴输出，不要只说「通过」）。
4. 发现的 docstring 与代码不一致之处（只报告，不在范围外擅自改）。

# 终端归属改造：Agent 执行，人在人的终端

> 实施顺序（11-10 定）：一期只收窄 Agent 对交互终端的能力（保留 list / read，write 只填入，不含「请你执行」），实施方案见 [`terminal-agent-read-prefill-plan.md`](terminal-agent-read-prefill-plan.md)；二期把运行中的后台命令并入 `terminal_read` 统一读取；「请你执行」与沙箱在其后另议。
> 状态：2026-11-10 设计稿，未实施。修订经过：
> - 11-09 初稿：「请人执行」用卡片 + 运行按钮。
> - 11-10 上午：改为「填入提示符 + 等待交还」。
> - 11-10 下午：按场景拆开——Agent 请你执行用回复里的命令块，你请 Agent 帮忙用 `terminal_write` 填入；两者都不阻塞，终端交接整体删除。
>
> 任务类型：功能改造（删除 Agent 常驻 shell 与终端交接、收窄 `terminal_write`、补齐后台命令）。
> 后续：[`agent-permission-sandbox-design.md`](agent-permission-sandbox-design.md) 的命令沙箱（P2）以本改造为前提。
> 取代：[`terminal-process-unification-design.md`](terminal-process-unification-design.md) §3「两个工具的边界」、[`terminal-gap-assessment.md`](terminal-gap-assessment.md) 中依赖 Agent 执行输入的人机仲裁（§3 T7、§7.4 A）。进程层（worker 两种模式、进程归属与清理）不变。

## 1. 为什么改

交互终端今天同时承担两件事：

- **Agent 的工作台**：Agent 开 shell、敲命令、读屏幕（`terminal_start` / `terminal_write` / `terminal_close`）。
- **人的终端**：用户自己开的终端，以及 Agent 把终端交给用户的交接（`terminal_handoff`）。

权限档位要求「Agent 操作必须在沙箱里，人操作不能有沙箱」。一个 shell 进程启动后，沙箱就定死了，没法按「谁在打字」切换。于是两种用途在同一个终端上冲突，交接最尴尬：Agent 把沙箱里的 shell 递给人，人在里面登录、写 `~/.npmrc` 同样会被拦。命令级包装也试过，在 ssh、REPL、已激活环境里失效（权限稿 §15）。

根因在结构上：**执行面和人机共享面放在了同一个东西上**。拆开以后，沙箱自然落位，交接也不再需要。

## 2. 终态：两个表面、四个动作

| | Agent 的执行 | 人的终端 |
|---|---|---|
| 是什么 | Agent 发起的进程 | 用户开的 shell |
| 沙箱 | 按档位（权限稿） | 永远没有 |
| Agent 能做 | 执行、读输出、停止 | 读；应你的请求把命令填进提示符（不能执行） |
| 人能做 | 在卡片里看、转后台、停止 | 一切；执行只能由人触发（按回车或点「运行」） |

| 动作 | 谁发起 | 形式 | 阻塞 | 谁能用 |
|---|---|---|---|---|
| ① 执行 | Agent | `execute_terminal`，补齐后台命令的读取与停止 | 前台等待 / 后台 | 所有 Agent |
| ② 读人的终端 | Agent | `terminal_list`、`terminal_read`（保留，只剩用户会话） | 否 | 侧栏对话 |
| ③ 请你执行 | Agent 需要你做 | 回复里的命令块，带「运行」「复制」 | 否：这一回合结束，你做完回来说一声 | 侧栏对话（前端能力，无后端工具） |
| ④ 帮你填入 | 你请 Agent 帮忙 | `terminal_write`：把命令填进你终端的提示符，你按回车 | 否 | 侧栏对话 |

**删除**：`terminal_start`、`terminal_close`、`terminal_handoff`，以及终端交接整套机制（交接租约的终端部分、`settle`）。Agent 不再有自己的常驻 shell，对任何终端都不能回车、不能发按键或信号。

**一条不变式**：终端里的每一次执行都由人触发。Agent 发起的执行只有 ① 一条路，并且按档位进沙箱。

参照 Claude Code：Agent 自己执行（一次性、可后台、可读输出），读用户终端，在回复里给出带「运行」按钮的命令块请用户执行；没有 Agent 常驻 shell，也没有挂起等待的终端交接。

## 3. 四个动作

### 3.1 执行：`execute_terminal`

- 语义不变：一次性命令、无 stdin、无 TTY，可前台等待或后台运行。调用之间不保留状态；需要串起来就写进同一段脚本，`workdir` 指定目录。
- **补齐运行中读取与停止**（原 gap 稿 T3，方案沿用 [`terminal-gap-assessment.md`](terminal-gap-assessment.md) §7.2 方案 A）：
  - 命令运行时由 `CommandService` 按 Task + `tool_call_id` 持有。现在它没有可寻址的持有者，输出只在 `_OutputCollector._chunks` 里（`apps/backend/app/tools/tool_handler/terminal/output_collector.py:85-115`）。
  - 新增两个工具：读运行中的输出（按行号续读、可等待匹配或退出，上限 30 秒）和停止命令。
    - 停止复用用户取消入口 `background_registry.request_user_cancel`（`assistant_transport/service/transport_assistant_service.py:192-205`）。
    - 两者都不需要用户界面，子 Agent 与 Team 节点也能用。
  - 读取通道给模型的文本要去掉控制序列并设上限（原 gap 稿 T4；现在原样拼进正文，`execute_terminal.py:578`）。
  - 删掉 Agent 常驻 shell 后，开发服务器、watcher 全靠它，**必须先于删除上线**（§6）。
- 需要交互的命令：提示词引导用非交互写法（`-y`、`--yes`、`CI=1`、`python -c`、`ssh host 'cmd'`、事务写进一个 SQL 文件、`expect` 脚本）。真正需要人的（登录、密码、二次验证、sudo），用 ③。
- Agent 的交互输入（Codex `write_stdin` 形态）本期不做。结构上留了口子：以后给后台命令加「带 TTY 运行 + 写入」，每个进程仍各自进沙箱，不会回到常驻 shell。

### 3.2 读人的终端：`terminal_list`、`terminal_read`

- 现在就可行：会话只按 Task 归属校验（`apps/backend/app/tools/terminal_runtime/session_service.py:170-182`），`terminal_read` 直接复用屏幕模型与命令边界。改造后会话只剩用户创建的。
- 保留读取时的条件等待（等输出匹配、等命令结束），用来看用户开着的服务，也用来在你说「好了」之后看 ③ 的结果。
- 只读，任何档位都可用、不需要审批（权限稿 §4）。
- 描述与报错里点名 `terminal_start` 的地方要改写（`terminal_session/list.py:39`、`terminal_runtime/errors.py:23,82`）。

### 3.3 请你执行：回复里的命令块

用来做 Agent 做不了、或不该由 Agent 做的事：登录、输密码、二次验证、sudo、需要沙箱外身份的一次操作。

- **形式**：Agent 在回复里写一个标了 shell 语言（`bash` / `sh` / `zsh` / `powershell` 等）的代码块，并结束这一回合。
  - **单行**命令块带「运行」和「复制」两个按钮。
  - 多行的只给「复制」：多行脚本一键执行风险高，也容易被 shell 逐行解释成意料之外的样子。
- **点「运行」就是你执行**：前端把命令作为**用户输入**（命令 + 回车）发进你的终端，不进沙箱。走现有用户输入通道（WebSocket `terminal-v2` 的 `Raw`，`apps/backend/app/api/terminal_stream_api.py:157-171`），后端不认识「命令块」这个概念。
- **目标终端**：
  - 优先用当前 Task 终端面板里正在看的那个用户终端，前提是它满足三个条件：
    - `status == "running"`；
    - 集成为 `ready`；
    - 不在执行命令（`commandRunning == false`）。
    这些字段都已在 snapshot 里（`assistant_transport/state/conversation_state_terminal_session.py:34-46`）。
  - 否则在工作区根新开一个。这样不会把命令敲进正在跑的程序里。
  - 点击后终端面板弹出并聚焦。
- **结果怎么回到 Agent**：你做完在对话里说一声，Agent 用 `terminal_read` 看结果。不挂起、不登记、不自动交还。
- **提示词**：需要你做事时给命令块、说明为什么、结束这一回合，不要试图用 ① 绕过（越权走权限稿的审批）。
- **位置**：这是 Markdown 渲染层的能力。代码块现由 Streamdown 的代码插件渲染（`apps/desktop/components/markdown/markdown-rendering-config.ts:1,17`），「运行」按钮加在这里；打开终端、发送输入经 `lib/terminal/`。它和链接一样只在对话正文里出现，文件面板的 Markdown 预览不带「运行」。

### 3.4 帮你填入：`terminal_write`

用于**你请 Agent 帮忙**的场景：「帮我写个命令查谁占了 3000 端口，放到终端里」。Agent 把命令填进你终端的提示符，你检查、修改后按回车。

**参数**：

- `session_id`：目标终端，必须是已有的用户终端。Agent 不能开终端。
- `command`：单行命令。

**「只能填、不能执行」靠机制保证**，分两层：

| 层 | 位置 | 规则 |
|---|---|---|
| 内容 | Rust worker 的输入种类 `Prefill { text }`，由现在的 `Text` 改名而来（`apps/terminal-worker/src/protocol.rs:55-63,185-214`），同时删除 `Line`、`Keys` | 只收可打印的单行文本，任何控制字符一律拒绝（回车、换行、Ctrl+M / J / O 等），沿用 `printable_bytes`（`protocol.rs:172-180`）。shell 开了 bracketed paste 时，把文本包成粘贴序列送入（屏幕模型能读到 `TermMode::BRACKETED_PASTE`，`terminal_screen.rs:131` 已在读同类模式位），用户自定义的快捷键不会被触发。没开时照常送入可打印文本，因为不含控制字符，仍不会执行。这是唯一的信任边界 |
| 状态 | 后端 `session_runtime`，由现在的 `_admit_agent_input`（`session_runtime.py:721-781`）收窄而来 | 只在 shell 集成报告「在提示符」时接受（`observation.phase == "editing"`，`observation.py:112-115`）。前台是任何程序都拒绝，含 ssh、REPL、`read -n1` 之类等单键的程序，因为一个普通字符在那里也可能就是执行。提示符出现后用户已经输入过字符（有半行）也拒绝，绝不发 Ctrl+U 清掉用户的输入。用户正在打字时，沿用现有的打字等待 |

- **多行命令不填**：Agent 先用文件工具写成脚本，再填 `bash script.sh`。不靠 bracketed paste 放宽多行，因为 macOS 自带的 `/bin/bash` 是 3.2，没有这个机制，换行会直接执行。
- **不阻塞**：填完返回「已填入终端 2」，Agent 照常结束回复。你按不按回车、改没改，Agent 都不等。
- **界面**：填入的文字在终端里标为「Agent 填入」（复用现有的 Agent 输入标记，`apps/desktop/components/terminal/terminal-view.ts:259,312`）。终端面板弹出并聚焦这个会话。
- **工具描述**：只在你要它把命令放进你的终端时用。Agent 自己需要你执行时，用 ③ 的命令块。
- **拒绝时**：返回原因（前台有程序 / 你正在输入 / 该 shell 没有集成 / 终端已结束），Agent 改为在回复里给命令块。

## 4. 行为账

**不变**

- 用户终端的创建、输入（用户输入走 `Raw`、从不被拒）、屏幕、重连、退出确认。
- worker 两种模式、进程归属与清理。
- `execute_terminal` 的参数与前后台语义。
- 浏览器交接（`browser_handoff` 与交接登记表的浏览器部分）。
- snapshot 顶层 `terminal_sessions` 与 `handoffs`（`handoffs` 只剩浏览器条目）。

**有意改变**

- Agent 不能开终端、不能执行、不能发按键与信号；原来的交互用途改为 ① 的后台读取 / 停止，或 ③。
- `terminal_write` 只剩「填入提示符」，而且只在提示符为空时可用。
- **终端交接删除**：
  - Agent 需要你做事时，不再挂起等待，而是在回复里给命令块、结束这一回合。
  - 长任务中途要你登录时，你做完要回一句，它才继续。Claude Code 也是这样。
- `terminal_sessions[].createdBy` 只剩一个取值，删掉这个字段；标签栏的创建者图标（`apps/desktop/components/terminal/terminal-tab-bar.tsx:86-100`）一并删除。
- 「终端结束」通知（`terminal_session_ended`，`core/workflow/message_queue/message_bodies/notice.py:16,35`）只为 Agent 会话而设，删除。
- 子 Agent 与 Team 节点获得后台命令的读取与停止能力（原来完全跑不了服务）。

## 5. 消费方与改法

| 区域 | 位置 | 改法 |
|---|---|---|
| 工具入口 | `tools/tool_handler/terminal_session/{start,close,handoff}.py`；`write.py` | 删前三个；`write.py` 改为填入 |
| 参数模型 | `tools/tool_models/terminal_session_args.py`（Start:28、Write:50、Handoff:130、Close:144） | 删 Start / Handoff / Close；Write 改为 `session_id` + `command` |
| Agent 输入 | `tools/terminal_runtime/inputs.py`（line/text/keys/signal 66-221） | 只留填入与读取等待 |
| 会话运行时 | `session_runtime.py`：`write` 448-530、`_deliver_agent_input` 531、等待 633-660、`begin/end_handoff` 665-712、`_admit_agent_input` 721-781、结束通知与 `settle` 调用 292-326 | 删执行类输入、signal、交接与结束通知；`_admit_agent_input` 收窄为 §3.4 的状态规则 |
| 会话服务 | `session_service.py:77-83`（Agent 数量上限）、`:129-137`（`close(by=)`） | 删除 |
| 常量 | `config/constant.py`：Agent 会话上限、打字等待 | 删会话上限；打字等待保留；Agent 输入上限按单行重定 |
| 工具名 / 分组 / 白名单 | `core/tool/schemas/tool_names.py:21-26,60-65,100-105`、`core/agents/define_agents.py:33-38,141-146` | 删 start / close / handoff |
| 交接登记表 | `core/tool/handoff/handoff_registry.py`（`HandoffSurface` :34、`settle` 101-130） | 删 `settle` 与 terminal surface；`surface` 只剩 `browser`（是否收掉这个字段见 §8 P3） |
| 结束通知 | `notice.py:16,35`、`session_effects.py:20-29` | 删除 |
| snapshot | `conversation_state_terminal_session.py:29,40,65` | 删 `createdBy` |
| 命令运行时（T1） | `tools/tool_handler/terminal/`、`execute_terminal.py:325-423` | 按 gap 稿方案 A 新增 `CommandService` 与读取 / 停止工具 |
| worker 协议 | `apps/terminal-worker/src/protocol.rs:44,55-170`（Signal、Line/Text/Keys、KeyName）、`session.rs:160-180`、`worker_client.py:323-336` | 删 Signal、Line、Keys、KeyName；`Text` 改为 `Prefill`（按模式位包粘贴序列）；用户输入照旧 `Raw` |
| 前端工具目录 | `apps/desktop/lib/assistant/tools/tool-catalog.ts:75,129-134` | 删 start / close / handoff；`terminal_write` 的动词改为「填入终端」 |
| 前端代码块 | `components/markdown/markdown-rendering-config.ts` | 单行 shell 代码块加「运行」，对话正文专用 |
| 前端终端 | `terminal-view.ts:259,312`、`terminal-workbench.tsx`、`lib/terminal/` | Agent 输入标记只标「Agent 填入」；删交接提示条；新增「运行」所需的选终端 / 新开并发送 |
| 前端交接 | `lib/handoff/reveal.ts:11`、`attention-card.tsx:22,33`、`lib/assistant/contract.ts:266,313` | 删 terminal 分支与 `terminal-session` 交接类型 |
| 契约文档 | `core/tool/tool_ui_display_contract.md:38-43,78`、`terminal_runtime/interaction_contract.md` | 改写 |

测试：

- **整体删除**：`test_terminal_session_tools.py` 中 start / close / handoff 用例，以及 write 的执行类用例。
- **大改**：`test_terminal_session_service.py`（约 15 个 Agent 输入、等待、交接、上限用例；输入仲裁的用例改写为填入规则）。
- **小改**：
  - `test_turn_withdrawal.py:130-145`：交接数据改用 browser。
  - `test_task_tool_scope.py`、`test_tool_call_identity.py`、`test_task_workspace_deletion.py:557`、`test_strict_tool_contract.py`、`terminal_test_support.py`。
- **保留**：`test_handoff_registry.py`（只用 browser，删 `settle` 用例）。
- **Rust**：`protocol.rs` 内置测试 508-590 改为 `Prefill` 的拒绝控制字符与粘贴包裹。
- **前端**：
  - 单测：`tool-catalog`、`attention-card`、`handoff-store`、`terminal-connection`、`snapshot-validation`。
  - E2E：`e2e/terminal-workbench.spec.ts`、`e2e/test-server.mjs`。
- **新增**：
  - 读取 / 停止工具。
  - `terminal_write` 填入：提示符外拒绝、半行拒绝、控制字符拒绝、多行拒绝。
  - 命令块：单行有「运行」、多行只有「复制」；目标终端忙时新开。

## 6. 分阶段

| 期 | 内容 | 验证 | 旧路径删除 |
|---|---|---|---|
| T1 | 后台命令的读取与停止（`CommandService` + 两个工具 + 模型通道文本整理 + 工具描述），即 gap 稿方案 A | 子 Agent 起开发服务器、等就绪、读日志、停止；后端单测 | 无 |
| T2 | 命令块「运行」；worker `Prefill` 与 `terminal_write` 收窄；**同版本**删除 Agent 常驻 shell、执行类输入、终端交接 | Rust 单测：控制字符拒绝、粘贴包裹；后端：§5 新增用例；E2E（无头）：点「运行」后终端出现并执行命令；`terminal_write` 填入后提示符上出现命令且未执行 | 本期删除 start / close / handoff、`settle`、Signal / Line / Keys、Agent 会话上限、结束通知、`createdBy` |
| T3 | 文档与守则：AGENTS.md 终端与交接段落、展示契约、交互契约，两份旧稿加「已被取代」标注 | 文档对账 | — |

T2 必须同版本：删了交接却没有命令块，Agent 就没法请你做事；只加不删，模型会在两种方式里选。

## 7. 链路复用

- **直接复用**：
  - 用户终端的新建（`terminal_workbench_service.py:32-70`）与用户输入通道（`Raw`）。
  - 屏幕模型与 shell 集成的命令边界和提示符状态（`observation.py`、`render.py`）。
  - snapshot 里的会话状态（`commandRunning`、`integration`），供「运行」选终端。
  - 前端终端面板的弹出与聚焦。
  - 用户取消入口（停止工具）。
- **改造后复用**：
  - worker `Text` 输入改为 `Prefill`：内容校验不变，加粘贴包裹。
  - `_admit_agent_input` 从「执行类输入的仲裁」收窄为「填入的状态规则」，打字等待照用。
- **沿用已有方案**：gap 稿 §7.2 方案 A（`CommandService`、读取与停止、子 Task 交回结论时撤销后台工作）。
- **无可复用**：命令块的「运行」按钮。查过链接分层（`lib/links/`，只做识别、解析、打开）与 Streamdown 代码插件自带的复制按钮，要在代码插件上加一个动作。

## 8. 问题清单

| # | 问题 | 证据 | 层级 | 置信度 / 待验证 |
|---|---|---|---|---|
| P1 | Streamdown 代码插件能否加自定义动作按钮 | `markdown-rendering-config.ts:1,17` 用 `@streamdown/code` | 依赖能力 | 中；不行就在渲染层替换代码块组件 |
| P2 | PowerShell / PSReadLine 对 bracketed paste 的支持，以及 ConPTY 下模式位能否被屏幕模型读到 | 未实测 | 平台 | 低；不支持时退回只送可打印文本（仍不会执行）；随 Windows 待办验证 |
| P3 | 交接登记表只剩浏览器一种资源，`surface` 字段与前端按 `surface` 分支是否收掉 | `handoff_registry.py:34`、`lib/handoff/reveal.ts:10` | 结构 | 高；按「抽象按需引入」倾向收掉，实施时按第零铁律定 |
| P4 | 用户把某个可打印键自定义绑成「执行」，且 shell 没开 bracketed paste | 极少见 | 边界 | 中；接受 |
| P5 | `child-agent-interactive-session-design.md` 设想子 Agent 用终端工具，且依赖已不存在的 `close_run_terminals` | 该稿 :52-53、:545-557 | 过时文档 | 高；T3 标注「已被取代」 |

## 9. 选定路线与被否方案

**选定**：执行面和人机共享面分开。Agent 只经 `execute_terminal` 执行；人的事在人的终端里做。Agent 请你做事用回复里的命令块，你请 Agent 帮忙用 `terminal_write` 填入，执行永远由人触发，两者都不阻塞。

| 被否方案 | 否决理由 |
|---|---|
| 保留 Agent 常驻 shell，整个进沙箱 | 交接时人拿到的是沙箱 shell，登录类操作做不了；放宽档位后旧 shell 仍按旧策略运行 |
| 保留共享 shell，Agent 的命令逐条包进沙箱 | 在 ssh、REPL、已激活环境里失效；需要暂存位、辅助程序、按键规则，复杂度高 |
| Agent 经授权「接管」用户终端 | 等于让 Agent 在没有沙箱的地方执行 |
| 「卡片 + 运行按钮 + 挂起等待交还」（11-09 版） | 要专用接口、交接租约、命令绑定与自动交还；Claude Code 证明非阻塞够用 |
| 「填入提示符 + 挂起等待交还」（11-10 上午版） | 把「Agent 请你做」和「你请 Agent 帮忙」两个场景混成一个；同样要整套交接机制 |
| 保留 `terminal_write` 的全部输入种类，只禁回车 | 按键、信号在程序前台照样能触发动作 |
| 现在就做 Codex 式交互进程（`write_stdin`） | 实际需要它的主要是测试交互式程序，先不做；结构上留口子（§3.1） |

## 10. 改动账本（粗估）

- **精简**（来自实测行数）：
  - 删除工具入口 start / close / handoff（159 + 68 + 107 ≈ −334）；`write.py` 收窄（约 −30）。
  - `session_runtime.py` 删执行类输入、signal、Agent 等待、交接与结束通知，收窄输入仲裁（约 −300）。
  - `inputs.py` 只留填入与等待（约 −130）。
  - `session_service.py` 删 Agent 上限与关闭来源（约 −30）。
  - `handoff_registry.py` 删 `settle` 与 terminal surface（约 −40）。
  - 结束通知与 `session_effects`（约 −50）。
  - 常量（约 −10）。
  - Rust Line / Keys / KeyName / Signal 及测试（约 −300）。
  - `worker_client` 的 signal（约 −15）。
  - 前端：创建者图标、交接 terminal 分支与提示条、三个工具目录条目（约 −120）。
  - 测试（约 −950，以实施时为准）。
  - 合计约 −2300。
- **新增**：
  - 填入状态规则（约 +60）。
  - Rust `Prefill` 与粘贴包裹（约 +60）。
  - T1：`CommandService` 与读取 / 停止工具（约 +600，按 gap 稿方案 A 再细化）。
  - 前端命令块「运行」与选终端 / 新开（约 +150）。
  - 测试（约 +550）。
  - 合计约 +1400。
- **净变化**：约 −900；实施时按阶段以 `git diff --stat` 对账。
- **命名**：
  - `docs/plan/terminal-ownership-redesign.md`：找「终端改造、交接、请用户执行」能落到它；从路径读出「终端按归属分工」。
  - `terminal_write` 保留原名：写进终端的输入区；描述写明只填入、不执行。
  - worker 输入种类 `Prefill`：名字即「填入、不执行」。
- **同步**：
  - AGENTS.md 三处：「进程生命周期边界」交互终端一段（人机都可输入 → Agent 只能填入）；「工具 UI 渲染边界」交接段（只剩浏览器）与已注册 kind 列表；「前端架构」链接与代码块一段（命令块「运行」）。
  - `tool_ui_display_contract.md`、`interaction_contract.md`。
  - 两份旧稿加标注。

## 11. 刻意不做

- **Agent 交互输入**（`write_stdin` / 带 TTY 的后台命令）：等测试交互式程序的真实需求出现再做（§3.1）。没有 TTY 就拒绝运行的程序（如 `docker run -it`），以后可给 `execute_terminal` 加「分配 TTY、不接受输入」的选项，出现真实案例再加。
- **命令块执行结果自动回传**：你点「运行」执行的命令，在下一条消息里自动附上退出码与输出摘要。v1 由你说一声、Agent 读终端。
- **多行命令的「运行」与多行填入**：先写脚本，再运行或填入一行。
- **一次性命令保留 cwd**：`workdir` 已够用，多一份隐藏状态得不偿失。
- **用户终端的任何沙箱化**。
- **子 Agent 与 Team 节点请你执行或帮你填入**：它们没有用户界面，需要人时报回上级。

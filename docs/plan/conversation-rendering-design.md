# 对话渲染重构设计：回合结构、统一调用行与 Agent 协作

> 状态：已实施（阶段 1–4）· 2026-11-01
> 样式原型：[assets/conversation-rendering/mock.html](assets/conversation-rendering/mock.html)（文中截图由它渲染）；实现截图 `assets/conversation-rendering/impl-*.png` 由 e2e 交替序列用例生成。
>
> 实施与本文的差异：命令输出上限沿用既有常量 16 行（文中 12）；运行中区头显示「当前动作」而不显示本地计时（耗时只在结束后显示）；同一文件多次写入时产物卡点开取最后一次的 diff；区头计数按 `presentation.verb` 聚合（命令的 `description` 各异，按标题计数会把「执行命令 4」拆成四个 1）；`read_file` 的页预算取 `MAX_OUTPUT_CHARS - 200`，结果头 `[lines a-b of N]`、尾 `[end of file]` / `[continue with offset=…]`；并行批次只改返回顺序（按调用顺序），执行仍先串行后并行。

## 0. 一页结论

- **问题不在某个组件，是三层叠加**：Transport 契约里回合没有「过程 / 结论」之分；前端分组只合并「相邻同类 part」，思考与正文必然把工具切碎；后端用逐工具手写的 `surface` 决定能否分组，34 个工具里 22 个声明为独立卡片。Agent 一侧又被工具设计与提示词推着用 shell 复合命令做探索，界面只能原样显示命令。
- **目标形态**：一个回合 = 过程（叙述、思考、调用，运行中展开、结束收成一行） + 等待用户（常显） + 本轮产物（改动、草稿，常显） + 结论（常显）。所有工具调用共用一种「意图 · 目标 · 结果 · 状态」单行，专用卡片只出现在产物区与等待区。
- **划分规则精确、无需新事实**：模型一次响应内正文必在工具调用之前，带工具调用的响应不是最终回答，所以「最后一个非正文 part 之后的正文」就是结论（§6.1 论证）。前端用一个纯函数投影回合，不需要后端标注步骤。
- **契约做减法**：`ToolDisplayHints` 从 8 个字段收敛为 `verb` + `icon`；新增后端统一投影的调用标题 `heading`（意图 + 目标）与显式的 `awaitingUser`；删除委派状态写回展示数据的镜像（改读已有的任务状态推送）。
- **视觉克制**：分隔按「留白 → 字号与明度 → 底色 → 边框」的顺序取用。对话流里只有「等待用户」卡带边框，其余靠缩进、明度与浅底面区分；命令输出不再是黑块，跟随应用主题（§5.8）。
- **Agent 侧同时改**：`execute_terminal` 加必填意图参数 `description`；系统提示词新增「工作方式」层（工具选择、并行、简短叙述）；修正把模型推向 shell 的工具缺陷（`read_file` 的预算说明与实际不符、只读工具串行执行等）。

## 1. 背景与目标

现象（截图见对话讨论）：Agent 调研代码时交替「思考 → 命令 → 思考 → 读文件」，界面每一步一行同等分量——十几张黑底命令卡、每隔一行一个「思考过程」，标题是 `echo '=== query options ==='; grep -n …` 这类原始复合命令。用户既看不出 Agent 在做什么，也找不到结论在哪。

目标：

1. **用户视角**：一眼分清「Agent 正在做什么 / 做了什么量级的事 / 改了什么 / 结论是什么 / 需要我做什么」；运行中有进度感但不刷屏；结束后紧凑、可回溯。
2. **Agent 视角**：让「正确的做法」也是「最省事的做法」——专用工具对模型确实比 shell 好用；结构信息（意图、叙述）由模型以低成本声明，界面原样采用。
3. **架构**：每类展示事实只有一个来源，渲染器形态在调用生命周期内不突变，新增工具不需要写前端分支。

非目标：不改 Run 状态机与事件模型；不做命令解析；不做显示密度设置；不做块级虚拟化（见 §10）。

## 2. 调查结论

调查分四个方向并行：前端渲染链路、后端展示契约与工具定义、Agent 视角（提示词、模型可见结果、本机库里的真实调用）、业界做法。关键论断均已抽查代码。本机库样本（Task 40–42，deepseek-flash，两次「调研代码架构」）：`execute_terminal` 35 次、`read_file` 22 次、`list_directory` 3 次、`search_content` / `find_files` 0 次。

### 2.1 问题清单

| # | 现象 | 证据 | 根因层级 / 置信度 | 本次 |
|---|---|---|---|---|
| F1 | 回合内没有「过程 / 结论」之分，叙述与最终答复同等分量；复制会带上中间叙述 | text part 只有 `text`/`status`（`apps/desktop/lib/assistant/contract.ts:56`）；所有 text 走同一个 `MarkdownText`（`apps/desktop/components/assistant-ui/elements/thread.aui.tsx:986`） | 前端回合模型缺失 / 高 | 处理 |
| F2 | 交替序列下工具永不成组 | 分组只合并相邻同路径 part（`apps/desktop/node_modules/@assistant-ui/core/dist/react/utils/groupParts.d.ts`）；reasoning 与工具是两个键（`thread.aui.tsx:185`）；单调用不折叠（`thread.aui.tsx:148`）。e2e fixture 恰好是理想形态，没覆盖交替（`apps/desktop/tests/e2e/test-server.mjs:199`） | 前端分组模型 / 高 | 处理 |
| F3 | 组从 1 个调用变成 2 个时子树重挂并收起，流式时视觉跳动 | `thread.aui.tsx:148` 返回根从 Fragment 变为 `ToolGroupRoot` | 前端组件结构 / 高 | 处理 |
| F4 | 同一调用的渲染器随生命周期切换（运行中单行、完成后变卡片） | 无 kind 时按 presentation 路由，兜底到单行（`apps/desktop/components/assistant-ui/tools/tool-renderer-registry.ts:129`）；只给 terminal-session 打过补丁（`:118`），浏览器、委派、文件删改、草稿卡仍会突变 | 路由契约 / 高 | 处理 |
| F5 | 外壳与状态视觉 7 套以上：状态 7 种写法、失败态 4 种色调、折叠状态 6 套实现 | `tool-card-shell.tsx`、`terminal-tool.tsx`、`file-operation-card.tsx:45`、`delegation-tool-row.tsx:80` 等 6 处手写卡片；`tool-group.aui.tsx:71` 与 `tool-status.tsx:20` 取消态颜色不同 | 前端缺统一原语 / 高 | 处理 |
| F6 | 折叠状态是组件局部 state，虚拟列表卸载即丢；收起的组会卸载里面的交互控件，「定位到工具卡片」在卡片未挂载时静默失败 | `tool-group.aui.tsx:27`；`use-thread-history-paging.ts:85` | 状态归属 / 高 | 处理 |
| F7 | 冷重建产生空正文 part，刷新后分组被切开 | 重建时 `content is not None` 就追加 text（`apps/backend/app/assistant_transport/service/conversation_task_state_rebuilder.py:323`），实时链路只在有 delta 时建 part；本机库 50 条 assistant 记录中 37 条 `content=""` | 后端投影缺陷 / 高 | 处理 |
| B1 | 22/34 个工具声明 `standalone`，包括 `terminal_read`、`wait_messages`、`child_agent_status` 这类低信息调用；`surface` 文档说「需要强调」，docstring 说「能否折叠」，实际唯一用途是分组 | `apps/backend/app/core/tool/tool_ui_display_contract.md:41`；`apps/backend/app/core/tool/schemas/tool_display.py:36`；前端唯一消费点 `thread.aui.tsx:190` | 展示契约语义 / 高 | 处理 |
| B2 | `ToolDisplayHints` 字段膨胀：`show_result` 无消费点也无结果通道；`default_open` 恒为 False；`expand_layout` 有 kind 时不起作用、`write` 取值无人声明；`variant` 只为 terminal-session 充当工具身份 | `tool_display.py:56`；`contract.ts:37`；`terminal-session-tool.tsx:30` | 展示契约 / 高 | 处理 |
| B3 | 没有调用意图或统一标题：终端标题取 `args.command`，委派取 `display.title`，details 取 verb + 按 kind 拼的摘要，组标题取 verb，看板取后端拼的 `terminal_command:{command[:80]}`；运行中（未有 display）只能显示 verb | `terminal-tool.tsx:70`；`delegation-tool-row.tsx:33`；`apps/backend/app/tools/tool_handler/execute_terminal.py:371` | 契约缺「标题」这一事实 / 高 | 处理 |
| B4 | 「等待用户决定」没有显式字段，`pending` 同时表示「准备执行」与「等你操作」，各卡片自行推断 | `browser-page-tool.tsx:37` | Transport 契约 / 中高 | 处理 |
| B5 | 委派卡片的 `display_data.status` 被当作子 Run 状态镜像，由 state service 写回并用 SQL `json_set` 改写，与「展示数据不是事实源」相抵；而应用级任务状态推送已覆盖子任务 | `apps/backend/app/storage/crud/conversation_task_context_crud.py:296`；`apps/backend/app/storage/queries/task_status_queries.py:35` | 事实所有权 / 高 | 处理 |
| B6 | 参数只在 running 时下发、不流式；排队与生成参数期间卡片只有 verb | `apps/backend/app/core/tool/execute/tool_executor.py:84` | 事件契约 / 高 | 记下（§10） |
| B7 | 契约文档多处漂移：`deferred` 应为 `background`、`web_fetch` 声明不符、漏列 10 个工具、variant 数量、过时章节；前端读后端从不产出的 `truncated` / `stream_truncated` | 契约 `:173`、`:180`、`:188`、`:408`、`:452`；`terminal-tool.tsx:75` | 文档治理 / 高 | 契约重写时一并修正 |
| A1 | 主 Agent 提示词没有工具选择、叙述、工作方式指令；本机预设与全局层文件为空，实际只有三层约 4.5K 字符 | `apps/backend/app/core/context/system_prompt_builder.py:110`、`:421`；`apps/backend/app/core/agents/define_agents.py:101` | 提示词 / 高 | 处理 |
| A2 | `read_file` 描述写「约 100K 字符」，实际全局预算 20K、超出从中间截掉；不报总行数、模型看不到 END——模型因此先 `wc -l` 再 `sed -n` | `apps/backend/app/tools/tool_handler/read_file.py:43`；`apps/backend/app/config/constant.py:378`；`core/tool/execute/tool_output_budget.py:61`；Task 42 一次读取被删 56 行 | 工具契约 / 高 | 处理 |
| A3 | 并行发出的只读调用实际串行执行，每个一张卡；shell 一条命令能做 N 件事 | `apps/backend/app/core/workflow/node_helper/tool_batch_runner.py:56`；`parallel_mode` 默认串行，只有 `delegate_task` 并行 | 工具定义 / 高 | 处理 |
| A4 | `execute_terminal` 没有意图字段，描述还鼓励「把相关步骤串在一条命令里」 | `execute_terminal.py:73`；`apps/backend/app/tools/tool_models/execute_terminal_args.py:90` | 工具契约 / 高 | 处理 |
| A5 | 运行时上下文说「文件工具拒绝工作区外路径」，而读工具实际允许；按字面理解，读工作区外只能用 shell | `system_prompt_builder.py:140`；`read_file_args.py:11` | 提示词与代码不符 / 中 | 处理 |
| A6 | `terminal_start` 描述误抄了 `terminal_read` 的 | `apps/backend/app/tools/tool_handler/terminal_session/start.py:46` | 工具描述缺陷 / 高 | 处理 |
| A7 | `list_directory` 每行带绝对路径、只列一层 | `list_directory.py:188` | 工具实现 / 高 | 处理（相对路径）；深度记下 |
| A8 | strict schema 下可选字段也必须显式写出，`search_content` 每次 6 个参数 | `apps/backend/app/core/tool/schemas/model_tool_schema.py:8` | 平台约定 / 中 | 记下 |
| A9 | 历史 Run 的推理全部回送模型，没有上下文压缩 | `core/context/runtime_message_context_manager.py:445` | 上下文 / 中 | 记下 |
| A10 | 没有计划 / 待办机制，模型无法声明结构化进展 | `core/tool/schemas/tool_names.py:10` | 能力缺失 / 高 | 待确认（§11） |

### 2.2 前端已在做对的事（保留）

- 工具 renderer 不按工具名分支，未知 kind 走兜底（`tool-renderer-registry.ts:171`）。
- 一次 Run 内 assistant 消息只在并入用户输入时切分（`conversation_task_state_rebuilder.py:267`），一段用户输入对应一条 assistant 消息——回合投影可以在单条消息内完成。
- 链接、Markdown、终端视窗、diff 解析等内核成熟，直接复用。

## 3. 业界做法与取舍

| | 过程折叠 | 命令标题 | 只读探索 | 思考 | 改动 |
|---|---|---|---|---|---|
| Codex CLI | 分步：探索合并为 `Exploring → Explored`，推理被吸收进探索组 | 命令解析（白名单，复合命令有一段认不出就退化为 Unknown，源码自称有损） | 合并，`Read a, b, c` | 主界面不显示，标题只刷新状态行 | `Edited path (+a −r)` + diff |
| Codex App / Cursor | 整轮折叠 `Worked for …` | — | 折进 `Explored N` | 折叠 | **被折叠——这是两家用户投诉最集中的点**（openai/codex #19891、#20090、#22334、#23868；Cursor 论坛） |
| Claude Code | 分步合并：`Read N files / Searched for` → 后来连续 shell 也合并为 `Ran N shell commands`；`/focus` 只留提示词、一行工具摘要（含 diffstat）与答复 | **Bash 必填 `description`**：「用户读的是它，常常看不到命令」 | 合并，活跃时露出当前文件 | 默认不生成摘要 | 单行 + diffstat |
| Cline / VS Code Copilot | 只读工具按类型合并；工具详情默认折叠 | 专用工具，命令原文 | `read 3 files, performed 2 searches` | 可折叠行 | 审批时展开 |

共同原则：只读探索合并成计数；会改状态的东西（改动）留在顶层；动词时态表达状态；失败不并入合并行；思考默认收起。

本方案的取舍：

- **意图参数，而不是命令解析**。复合命令正是本仓 Agent 的主要写法（`for f in …; do head …; done`、`echo '==='; sed …; grep …`），解析器对它们退化为 Unknown；意图参数对任意命令都成立，代价是每次十几个 token。同时修工具缺陷，让探索回到专用工具（§6.5），从源头减少「需要解析的命令」。
- **整轮折叠探索，但改动不折叠**。取 Codex App 的干净，避开它被投诉最多的部分：改动聚合成产物卡常显。
- **不做密度设置**。各家用设置调和两类用户，说明单一默认有张力；本方案先用「过程可展开 + 产物常显」覆盖两头，等真实反馈再说（§10）。

## 4. 设计原则

**用户视角**

1. **层级即注意力**：结论 > 需要我做的 > 改了什么 > 正在做什么 > 做过什么。层级越低越淡、越可折叠。
2. **成功是常态**：成功不画对勾，只有运行中、失败、取消、等待用户有状态标记。
3. **一行一调用，意图先于参数**：标题说「为什么」，目标说「对什么」，结果说「怎么样」；原始参数与完整输出展开才看。
4. **运行中稳定**：新内容只在末尾追加，已出现的元素不换形态、不换位置；整体重排只发生在回合结束这一次。
5. **强调色只给需要人的东西**：信息蓝只用于等待用户，红只用于失败。
6. **克制，少画线**：能用留白和明度分开的不用底色，能用底色分开的不用边框；边框只留给「必须第一眼看到、需要人动作」的容器。规则见 §5.8。

**Agent 视角**（取自我在当前环境中的切身体验：Bash 必填 `description`、清单工具、收尾摘要，都是低成本声明、界面原样采用）

7. **结构由模型声明，界面不猜**：意图、叙述由模型写；界面不解析命令、不从参数反推。
8. **正确的路最省事**：专用工具在结果质量、调用次数、参数负担上不输 shell，否则提示词再怎么要求也会被绕过。
9. **声明要便宜**：意图一句话、叙述一句话；不要求模型每步汇报。

**架构**

10. 每类展示事实一个来源：调用标题由后端按工具投影；子任务状态读任务状态推送；折叠状态在一个 store。
11. 删除先于新增：被取代的字段、渲染器、补丁同步删除。

## 5. 目标形态与样式举例

### 5.1 回合结构

```text
用户消息
└─ 助手回合
   ├─ 过程区      叙述 · 思考 · 调用（运行中展开、只露最近 4 步；结束收成一行）
   ├─ 等待用户区  awaitingUser 的调用（交接、审批），常显
   ├─ 产物区      本轮改动（按文件聚合）、配置草稿、Team 卡，常显
   ├─ 结论        最后一个非正文 part 之后的正文
   ├─ 结局        失败 / 取消说明（仅在非正常结束时）
   └─ 回合脚注    耗时 · 用量 · 复制结论 · Fork · 诊断
```

### 5.2 场景 A：运行中

![运行中](assets/conversation-rendering/running.png)

- 区头实时显示「当前动作」（最新未结束调用的标题，或「思考中」），后面是按动词计数。
- 叙述用正文字号与前景色，它是写给用户的；思考是一行灰字，只露首句，展开看全文。
- 命令行标题是模型写的 `description`，原命令降为等宽灰字；运行中自动展开输出（最多 12 行，落在跟随主题的浅底面上，不再是黑块），结束即收起。
- 过程区体只靠缩进与区头文字对齐，不画竖线。
- 只露最近 4 步，更早的收进「前面还有 N 步」——长回合运行时页面高度有上界。

### 5.3 场景 B：结束（默认）

![结束](assets/conversation-rendering/settled.png)

- 过程收成一行：「已处理 3 分 12 秒 · 读取文件 9 · 搜索 3 · 执行命令 4」，思考不计数。
- 改动按文件聚合成产物卡，行内 `+/−`，点击文件看 diff；卡片是一块浅底面，没有外框，文件行之间也不画分隔线。
- 结论正文原样；复制按钮只复制结论。

### 5.4 场景 C：结束后展开过程

![展开过程](assets/conversation-rendering/expanded.png)

按时间顺序完整回放：叙述、思考、每次调用一行；改动行在过程里也有（与产物卡互为两种视角）。

### 5.5 场景 D：等待用户 / 失败

![等待用户与失败](assets/conversation-rendering/attention.png)

- 等待用户的调用从过程里提出来，以提示卡常显；它是对话流里唯一带边框、唯一用信息蓝的容器，操作按钮就在卡上。
- 结局卡只用浅红底面，不加框。
- 失败的命令行变红，展开时自动带出输出尾部；取消用删除线。
- 回合以失败或取消结束时，结局卡写在最后，过程照常收起，区头带「1 个失败」。

### 5.6 场景 E：调用行解剖与状态词表

![调用行与状态](assets/conversation-rendering/anatomy.png)

### 5.7 场景 F：深色主题

![深色主题](assets/conversation-rendering/dark.png)

同一套规则在深色主题下：命令输出与产物都落在 `card` 面（比背景高一档），仍不加框；第二行是悬停态，用底色而不是描边表达「可点」。浅色主题下的「深色终端岛」放到深色背景上几乎与背景同色，必须换面，这也是命令输出改为跟随主题的另一个理由。

### 5.8 视觉克制：分隔层级与边框规则

现状是每张工具卡各自加框：6 处手写 `bg-card rounded-xl border shadow-sm`、终端卡 `border shadow-sm`、文件操作卡、组内细线，叠在一起就是截图里的「格子感」。项目已有的约定是区域之间靠明度差分隔、不画结构分割线（`apps/desktop/app/globals.css:131`），对话流内部沿用同一思路。

**分隔手段按顺序取用，前一级够用就不升级**：

| 级别 | 手段 | 用在 |
|---|---|---|
| 1 | 留白 | 回合各区之间（区间距 10–14px）、产物卡内的文件行 |
| 2 | 字号、明度、缩进 | 过程 vs 结论（13px 灰 vs 14px 前景）、过程区体（缩进 22px）、已完成调用转灰 |
| 3 | 底色面 | 产物卡、展开体（命令输出、搜索结果、diff）、结局卡（着色）、悬停态、次要按钮 |
| 4 | 边框 | 只有「等待用户」卡 |

**为什么等待用户卡要边框**：它是对话流里唯一需要人动作、可能停留很久（Run 挂起）的容器，必须在深浅两种主题下第一眼看到。只靠 6% 的信息蓝底色，在深色主题下几乎看不出来；加一道 38% 透明的信息蓝边框（与 `apps/desktop/app/globals.css:330` 高亮动画里的信息蓝边框同一配方）成本最低。除此之外，对话流里不出现边框和阴影。

**两级底色面**（新增 token，替代各卡片手写的 `bg-card border`）：

| token | 浅色 | 深色 | 承载 |
|---|---|---|---|
| `--surface` | `muted` 70% | `card` | 产物卡、展开体、命令输出 |
| 着色面 | `info` / `destructive` 6–7% | 同左 | 等待用户卡 / 结局卡 |

命令输出复用终端工作台已经跟随主题的配色（`xtermPaneTheme`，`apps/desktop/components/terminal/xterm-appearance.ts:55`，浅色带专用 ANSI 调色板），只把背景换成 `--surface`；对话流不再使用 `terminal-surface` 深色岛。

**不靠边框的交互可供性**：可展开的行悬停时出现 `muted` 底色（圆角 6px）；chevron 与「前面还有 N 步」本身就是展开提示；次要按钮用 `muted` 底、主按钮实心 `foreground`，都不描边。键盘焦点仍用现有的 `focus-visible` 圈（可访问性要求，不在「克制」的取舍范围内）。

### 5.9 样式规格

| 元素 | 规格（token 取自 `apps/desktop/app/globals.css`） |
|---|---|
| 调用行 | 最小高 28px；13px；图标 14px `muted-foreground`；标题运行中 `foreground`、结束后 `muted-foreground`；目标 12px 等宽 `muted-foreground`、单行截断占满剩余宽度；元信息 12px 右对齐；状态位 14px 只在运行中 / 失败 / 取消出现；可展开时悬停出现 `muted` 底（圆角 6px），无边框 |
| 过程区头 | 13px `muted-foreground`，悬停变 `foreground`；左侧 chevron 展开时旋转 90°；运行中「当前动作」用 `foreground` + 闪烁；失败计数 `destructive` |
| 过程区体 | 不画竖线；左缩进 22px，与区头文字对齐；子项间距 2px |
| 叙述 | 14px `foreground`，与结论同字号——它是给人看的正文 |
| 思考行 | 12.5px `muted-foreground`，「思考 · 首句」单行截断；流式时「思考中 · 最新一行」闪烁 |
| 命令输出 | 只在展开的行下方出现：`--surface` 底、无边框、圆角 8px、12px 等宽、配色跟随主题（`xtermPaneTheme`）、左缩进与行标题对齐、最多 12 行，其余「… 还有 N 行」 |
| 本轮改动卡 | `--surface` 底、无边框、圆角 `--radius`、行间不画线；表头 13px；文件行 12px 等宽、缩进与表头文字对齐，`+` 用 `success`、`−` 用 `destructive`；新建 / 删除 / 移动以 11px 灰标签标注 |
| 等待用户卡 | **唯一带边框**：`info` 38% 透明边框 + 6% 透明底；标题 13px 粗体，说明 `muted-foreground`；主按钮实心 `foreground`，次按钮 `muted` 底 |
| 结局卡 | `destructive` 7% 透明底，无边框，文字 `destructive` |
| 动效 | 展开 / 收起 200ms；闪烁只用于「当前动作」与排队行；减少动态效果时全部关闭 |

## 6. 方案

### 6.1 回合投影：划分规则与精确性

前端新增纯函数 `projectTurn(parts, phase) → TurnLayout`（`apps/desktop/lib/assistant/turn-layout.ts`），按以下规则把一条 assistant 消息的扁平 parts 分区：

1. **结论** = 最后一个非正文 part（思考或调用）之后的全部正文 part。
2. **等待用户** = `awaitingUser` 为真的调用（同时作为一行留在过程里）。
3. **产物** = 展示数据 kind 属于产物类的调用：`file-changes` 聚合为「本轮改动」卡；`agent-configuration-draft`、`agent-team-configuration-draft`、`agent-team-approval` 各自成卡。产物类 kind 的清单在前端 kind 注册表里登记，这是已有的「按 kind 路由」契约，不按工具名。
4. **过程** = 其余全部 part 按原顺序：叙述（结论之前的正文）、思考、调用。

**为什么规则 1 是精确的，而不是启发式**：模型一次响应里正文先于工具调用（冷重建按 reasoning → content → tool_calls 追加，`conversation_task_state_rebuilder.py:316`）；带工具调用的响应不会结束 Run（「最终回答结束当前 Run」）。所以位于最后一个非正文 part 之后的正文，只能来自一次不带工具调用的响应，也就是结论。两个边界：

- **流式期间**：末尾正文可能随后跟出工具调用。运行中它与叙述同字号、同位置，过程区展开时跟出的调用只是追加在它下方，不发生跳动；回合结束那一次才定稿分区。
- **运行中途并入用户输入**：前一段消息没有结论，整段都是过程，结束时照常收起。

`phase` 由消息上下文给出：`running`（本轮活动且 Run 在运行）、`suspended`（Run 挂起，等待用户）、`settled`（终态或非本轮末尾）。

**为什么不用 `MessagePrimitive.GroupedParts`**：它的 `groupBy` 只拿到单个 part、拿不到位置，无法表达规则 1；改用自己的投影后，正文与思考经 `MessagePrimitive.PartByIndex` 渲染（保留 assistant-ui 的流式 Markdown 作用域），调用由统一调用行直接读 part。分区规则因此成为可单测的纯函数，取代现在没有单测的分组。

### 6.2 前端组件结构

```text
AssistantMessage
└─ Turn                     读 projectTurn 结果，按 phase 决定开合
   ├─ ProcessBlock          区头（当前动作 / 已处理 + 计数）+ 区体（活跃尾部）
   │   ├─ NarrationItem     PartByIndex → MarkdownText
   │   ├─ ReasoningItem     一行；展开为 Markdown
   │   └─ CallRow           统一调用行；展开体按 kind 选
   ├─ AttentionZone         AttentionCard（交接：交还 / 打开面板；Team 审批：表单）
   ├─ ArtifactZone          ChangesCard（聚合 file-changes）· 草稿卡 · Team 卡
   ├─ Answer                PartByIndex → MarkdownText
   ├─ TurnError
   └─ TurnFooter            耗时 · 用量 · 复制结论 · Fork · 诊断
```

- **CallRow 是唯一的调用渲染入口**：图标取 `presentation.icon`；标题取 `heading.title`，没有时取 `presentation.verb`；目标取 `heading.target`；元信息与展开体由 kind 视图给出（复用现有 `DETAIL_VIEWS` 的 `summary` / `body`，终端输出视窗、diff 解析原样复用）。同一个调用从排队到终态都是这一行，只有内容在变——F4 不再可能出现。
- **一个状态组件**：`CallStatus` 只表达工具生命周期（运行中 / 失败 / 取消）与两个附加态（在后台、等待用户），取代现在 7 种写法。退出码非 0 仍按现在的约定显示 `exit N`，由终端 kind 视图给出元信息，不改工具状态。
- **委派行**：状态读 `useTaskExecutionStatus(child_task_id)`（`apps/desktop/lib/task-status/task-status-store.ts:28`），点击打开子任务；不再读 `display_data.status`。
- **开合状态集中**：`apps/desktop/lib/assistant/disclosure-store.ts`（zustand，不持久化，按 Task 分桶），键为 `process:{messageId}`、`call:{toolCallId}`、`reasoning:{messageId}:{index}`。值只记用户覆盖；没有覆盖时按规则：过程区 `phase==='running'` 展开，否则收起；命令行运行中展开；失败行在过程区展开时带出输出尾部。虚拟列表卸载再挂载不丢状态（F6）。
- **单调用不加区头**：过程里只有 1 次调用时直接平铺该行（加思考行），不出现「已处理 · 读取文件 1」这种要多点一次的区头；0 次调用时只有思考行。
- **复制只复制结论**：脚注的复制按钮复制 `Answer` 的正文，不再用 assistant-ui 默认的「全部 text part 拼接」。
- **虚拟列表**：粒度仍是整条消息；结束后的回合高度变得紧凑且可预期，同步更新 `message-height-estimate.ts` 的估算（当前估算还假定「思考 + 工具合成一组」，已与实现漂移）。

删除：`tool-group.aui.tsx`、`tool-group-status.ts`（计数逻辑并入区头）、`thread.aui.tsx` 里的 `GroupedParts` 分支与 `ToolTraceGroup`、`tool-card-shell.tsx`、`file-operation-card.tsx`、`terminal-tool.tsx` 与 `terminal-session-tool.tsx` 的卡片形态（输出视窗保留）、`tool-disclosure.ts`、`tool-renderer-registry.ts` 中按 presentation 路由的分支、前端对 `truncated` / `stream_truncated` / `data.shell` 的读取；`tool-layout-tokens.ts` 的 `TERMINAL_CARD_CLASS` / `TERMINAL_CARD_HEADER_CLASS` 与对话流里对 `terminal-surface` 的使用、6 处手写的 `bg-card rounded-xl border shadow-sm` 卡片外壳（统一换成 §5.8 的两级底色面）。

### 6.3 Transport 契约

工具 part 的变化：

| 字段 | 变化 | 说明 |
|---|---|---|
| `heading` | 新增 `{ title: string, target: string \| null }` | running 时随状态事件下发；冷重建时按同一函数从持久化参数计算。参数未通过校验（被准入拒绝）时缺省，前端回落到 verb |
| `awaitingUser` | 新增 `boolean` | 在 `project_tool_result` 一处计算：占位行 `pending` 且不是后台占位即为真（`apps/backend/app/assistant_transport/projection/tool_result.py:22`）；实时与冷重建同口径 |
| `presentation` | 收敛为 `{ verb, icon }` | 见 §6.4 |
| `child_task_id` / `agent_role` | 保留 | 委派行据此读任务状态推送 |

消息与回合：

- **结局加耗时**：`ConversationStateOutcome`（`apps/backend/app/assistant_transport/state/conversation_state_message.py:13`）新增 `durationMs`，取 Run 的创建与终态时间（`apps/backend/app/models/conversation_run_record.py:43`）。只挂在 Run 最后一条消息上；运行中由前端按 `activity` 的开始时间本地计时。
- **冷重建不再产出空正文 part**：`_append_assistant_parts` 只在正文非空时追加（F7），与实时链路一致。

删除：委派状态写回（`apps/backend/app/assistant_transport/service/conversation_task_state_service.py:538` 的 `refresh_parent_delegation` 与 `:332` 的 `_reconcile_child_delegation_statuses`，以及 `conversation_task_context_crud.py:296` 的 `json_set`），`delegation-result` / `child-agent-result` 的 `status` 字段改为只记工具自身结论。

### 6.4 工具定义契约

**`ToolDisplayHints` 收敛为两个字段**：

```python
@dataclass(frozen=True)
class ToolDisplayHints:
    verb: str   # 没有意图时的标题，如「读取文件」
    icon: str   # lucide 图标名
```

删除 `surface`、`variant`、`expandable`、`expand_layout`、`default_open`、`show_result`：分区由回合投影按 kind 与 `awaitingUser` 决定；是否可展开由 kind 视图是否有展开体决定；终端会话各工具的标题改由 `heading` 给出，不再需要 `variant` 充当身份。

**调用标题由工具投影**：`ToolDefinition` 新增 `heading: Callable[[BaseModel], ToolHeading] | None`，纯函数、不做 IO，和展示投影放在一起（外部工具在 `apps/backend/app/tools/display/`，内部工具在各自目录）。为什么放后端：哪个参数是「目标」只有工具自己知道，放前端就只能按工具名分支。示例：

| 工具 | title | target |
|---|---|---|
| `execute_terminal` | `args.description` | 命令首行 |
| `read_file` | verb | `path` |
| `search_content` | verb | `"{pattern}" in {path}` |
| `terminal_write` | verb | 终端名 + 输入摘要 |
| `delegate_task` | `agent_name` | 子 Agent 名 |
| `mcp` | verb | `{server}/{tool}` |

后台看板的标签改用同一个 `heading`，删除 `terminal_command:{command[:80]}` 这种拼接。

**`execute_terminal.description`**（必填，参数模型首个字段，Windows / macOS 两个模型同步）：

```json
{
  "description": "运行后端单元测试",
  "shell": "auto",
  "command": "uv run pytest tests/test_task.py -q",
  "timeout": null, "workdir": "apps/backend", "background": null
}
```

字段描述草案：`One short sentence (about 5–10 words) in the user's language saying what this command does and why, e.g. "Run backend unit tests". Shown to the user in place of the command; do not restate the command.` 语义边界：只用于展示、不可信；执行、安全策略与日志只认 `command`，任何需要用户批准命令的界面展示原命令。

已有 Task 的工具 schema 在创建时固化，新的必填字段会让旧 Task 的调用被拒；绿地项目按约定新建 Task（`apps/backend/app/core/tool/tool_contract.md:7`），不写兼容。

### 6.5 Agent 侧

**(1) 系统提示词新增「工作方式」层**（`system_prompt_builder` 在 tool_layer 之后，按 `allowed_tools` 条件拼接），草案：

```text
<work_layer>
- Use dedicated tools for files: read_file to read, search_content to search, find_files to find,
  list_directory to list. Use execute_terminal for builds, tests, git and running programs,
  not for cat/sed/head/grep/rg/find/ls.
- Independent reads and searches go out together as parallel tool calls in one response.
- Keep the user oriented. Before the first tool call, say in one sentence what you are about to do.
  When something you find changes the plan, say so in one sentence. Do not narrate every call;
  these lines are shown to the user while you work.
- When done, lead with the result. Mention anything you could not verify or any failure you did
  not resolve.
</work_layer>
```

只写当前 Agent 可用的工具（条件拼接），避免引用不存在的工具。代价：每次请求多约 150 token（走前缀缓存）；叙述每次多 20–40 输出 token。系统提示词按 Task 固化，只对新 Task 生效。

**(2) 修正把模型推向 shell 的工具缺陷**

- `read_file`：在工具内按字符预算（低于全局 20K）自行分页，不再被中段截断；结果头写 `lines a–b of N`，读到结尾给 `[end of file]`，否则给下一段 `offset`；描述改为真实上限（A2）。
- 只读工具并行执行：`read_file`、`read_image`、`search_content`、`find_files`、`list_directory`、`web_search`、`web_fetch` 声明 `parallel_mode="parallel"`（A3）。模型并行发出的读取真正并行，界面上是同一时刻出现的几行。
- `list_directory` 输出相对路径（A7）；`terminal_start` 描述改正（A6）；运行时上下文改正读路径边界的说法（A5）；`execute_terminal` 描述删掉「把相关步骤串在一条命令里」的引导，改为说明它不用于读文件和搜索（A4）。

**(3) 不做的 Agent 侧改动**（见 §10）：多目标读取、ripgrep 后端、放宽 strict、剥离历史推理、计划工具（待确认）。

## 7. 行为变化

有意改变：

- 回合分为过程 / 等待用户 / 产物 / 结论；过程运行中展开、只露最近 4 步，结束收成一行；思考不再自成一组。
- 所有调用统一为单行，成功不画对勾；专用卡片只在等待区与产物区。
- 文件改动聚合成「本轮改动」卡常显；复制只复制结论；脚注显示耗时。
- 命令以模型写的意图为标题；委派行状态来自任务状态推送。
- 主 Agent 会在开始与转折处写一句叙述；探索更多用专用工具、并行执行。

保持不变：Run 状态机、工具执行状态的来源、各 kind 的结果载荷（委派状态字段除外）、HITL 与交接的决定命令、后台看板的事实来源、snapshot 的消息与历史窗口机制。

## 8. 影响范围

| 层 | 文件 | 测试 |
|---|---|---|
| 工具定义 | `core/tool/schemas/tool_display.py`、`tool_definition.py`；34 处 `ToolDisplayHints(...)`；各工具 `heading` 投影（`tools/display/`、`core/tool/handler/*/`）；`tools/tool_models/execute_terminal_args.py`；只读工具 `parallel_mode` | `test_tool_display_definitions.py`、`test_execute_terminal_*.py`、`test_tool_argument_coercion.py`、并行批次测试 |
| 工具实现 | `read_file.py`、`list_directory.py`、`terminal_session/start.py`、`execute_terminal.py` 描述 | 各工具单测 |
| 提示词 | `core/context/system_prompt_builder.py` | 系统提示词单测 |
| Transport | `projection/tool_result.py`、`tool_call_event_projection.py`、`state/conversation_state_part/conversation_state_tool_call_part.py`、`conversation_state_message.py`、`service/conversation_task_state_rebuilder.py`、`service/conversation_task_state_service.py`、`storage/crud/conversation_task_context_crud.py`、`state/conversation_state_snapshot.py`（校验白名单） | `test_conversation_event_projector.py`、重建与快照校验测试 |
| 前端契约 | `lib/assistant/contract.ts`、`converter.ts`、`snapshot-validation.ts` | 对应单测 |
| 前端渲染 | `app/globals.css` 新增 `--surface` 两级底色面 token；新增 `lib/assistant/turn-layout.ts`、`lib/assistant/disclosure-store.ts`、`components/assistant-ui/turn/*`；改写 `thread.aui.tsx`；重写 `tools/` 下渲染器为 CallRow + kind 视图 + 产物 / 等待卡；`message-height-estimate.ts`；`use-thread-history-paging.ts` | 新增 `turn-layout` 单测（交替序列、流式末尾正文、中途并入、无结论、单调用）；`disclosure-store` 单测；e2e fixture 增加交替序列与改动聚合 |
| 文档 | 重写 `core/tool/tool_ui_display_contract.md`（修正 B7）；更新 `AGENTS.md`「工具 UI 渲染边界」中 `surface` / `variant` / 已注册 kind 段落 | 新增注册表守卫测试：每个工具都有 `heading` 或明确无目标、kind 已在契约登记 |

## 9. 分阶段

每阶段独立可验证；1、2 可以并行开发，3 依赖 2，4 与其余独立。

1. **契约减法与标题**（后端）：`ToolDisplayHints` 收敛、`heading`、`awaitingUser`、`durationMs`、空正文修复、委派镜像删除、`execute_terminal.description`、契约文档重写与守卫测试。验证：后端单测；snapshot 校验。
2. **回合投影**（前端）：`turn-layout` 纯函数与单测、`disclosure-store`、`Turn` / `ProcessBlock` / `Answer` / `TurnFooter`。验证：Vitest 覆盖 §6.1 全部边界；e2e 交替序列快照。
3. **统一调用行与区卡**（前端）：`CallRow`、`CallStatus`、kind 视图迁移、`ChangesCard`、`AttentionCard`、草稿与 Team 卡迁入产物区；删除旧渲染器。验证：e2e 截图对比场景 A–F（含深色主题）；后台看板定位回归。
4. **Agent 侧**（后端）：工作方式层、`read_file` 分页与总行数、只读并行、描述修正。验证：单测；用同一个「调研代码架构」请求在新 Task 上对比 shell / 专用工具调用比例与叙述出现情况（以本机库样本为基线）。

1 与 2、3 应在同一批交付：只做 1 时旧渲染器读不到被删的 presentation 字段。

## 10. 刻意不做（记下）

- **参数流式（B6）**：排队与生成参数期间只有 verb。需要解析部分 JSON、新增事件类型；只读工具改为并行后排队时间缩短，收益下降。
- **命令解析**：理由见 §3；Agent 侧修正后若 shell 探索仍然很多再评估。
- **显示密度设置**：等真实反馈。
- **块级虚拟化**：长回合内部不虚拟化；回合结束后已收起，运行中有活跃尾部上界，暂不需要。
- **多目标读取 / ripgrep 后端 / 放宽 strict（A8）/ 剥离历史推理（A9）/ 目录深度参数**：都影响 Agent 行为与成本，各自需要单独评估。
- **part 与展示数据的其余重复**（`isError` 与 `status`、`team_run_id` 两处等）与实时 / 冷重建参数口径差异：不影响本次渲染，单独清理。
- **assistant-ui 对工具状态的推导与 converter 的结果哨兵**：本次渲染不再读它，但适配层仍在；是否进一步弱化对 assistant-ui part 模型的依赖，单独评估。

## 11. 待确认

1. **计划工具（A10）**：在我所在的环境里，清单工具是用户跟进长任务的主视图。若引入：`update_plan` 工具写进程内计划 → snapshot 顶层 `plan` → 会话侧栏卡片，过程区头显示当前步骤。代价是每次更新一次工具调用、模型可能不用或滥用。建议在本方案落地、观察叙述效果后再定。
2. **活跃尾部保留几步**：方案取 4。
3. **`description` 必填**：推荐必填；可选时模型常省略，标题回落成命令。

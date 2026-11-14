# ask_user：通用提问工具

> 状态：设计稿，未实施（2026-11-14）。只记需求、设计与契约，实施计划由实现方另出。
> 对标：Claude Code 的 `AskUserQuestion`（工具描述、参数说明与回执原文见根目录 [`claude-code-system-prompt.md`](../../claude-code-system-prompt.md) 的 AskUserQuestion 一节）。
> 关联：[`agent-permission-sandbox-design.md`](agent-permission-sandbox-design.md) §6——越界审查拒绝后，模型用本工具向用户征得许可；本工具与沙箱无关，可以先交付。

## 1. 要什么

**完成标准**：模型被一个真正属于用户的决定卡住时（澄清需求、几个方案选一个、确认取舍、征得越界许可），调用 `ask_user` 弹出提问卡片；用户点选或填写后，回答作为结构化消息交给模型，同一个 Run 接着干。用户也可以不理卡片、直接发消息，旧提问作废，按新消息处理。

**最不想看到**：

- 模型动不动就问，问一些能自己查到或有惯例的事。
- 卡片挂着时用户发消息没反应，或作答后卡片还显示「等你操作」、能重复提交。
- 问答分家：问在一个回合，答落进另一个回合。
- 过时的回答在之后的回合里生效。

**对标与偏离**：形态、文案、参数、卡片与回执都照 Claude Code。偏离只有一处：Claude Code 让对话框把回答写回这次调用的参数（`answers` / `annotations`），cosir 走现有的用户决定通道（`/tool-calls/{id}/decision`），回答不进模型能填的参数。

## 2. 模型看到什么

**工具描述**（英文原文照录；删去 Plan mode 一段，cosir 没有 plan 模式；`codebase` 改 `workspace`，`dialog` 改 `card`，参数名按 cosir 蛇形命名）：

```text
Use this tool only when you are blocked on a decision that is genuinely the user's to make: one you cannot resolve from the request, the workspace, or sensible defaults.

Usage notes:
- Users will always be able to select "Other" to provide custom text input
- Use multi_select: true to allow multiple answers to be selected for a question
- If you recommend a specific option, make that the first option in the list and add "(Recommended)" at the end of the label

Reserve this for decisions where the user's answer changes what you do next — not for choices with a conventional default or facts you can verify in the workspace yourself. In those cases pick the obvious option, mention it in your response, and proceed.

The user may see only the question card: tool results and text you wrote earlier in this turn may not be on their screen while it is open. Make each question stand on its own: if it depends on something you found, say so briefly, and name the items you are asking about in the question itself.
```

**参数**（strict；各字段的 `description` 照录 Claude Code 的参数说明）：

| 字段 | 约束 |
|---|---|
| `questions` | 1–4 个；`question` 在本次调用内不重复（回答按问题原文作键） |
| `questions[].question` | 完整问句，以问号结尾（描述约定，不校验） |
| `questions[].header` | 不超过 12 字，卡片上的短标签 |
| `questions[].options` | 2–4 个，同一题内 `label` 不重复；不写「其他」（卡片自动提供） |
| `options[].label` / `description` | 必填；`label` 1–5 个词（描述约定） |
| `options[].preview` | 可选；选项聚焦时显示的预览。首版前端不渲染，参数照收 |
| `questions[].multi_select` | 缺省 `false` |

**首次调用的结果**：沿用全部 HITL 工具共用的等待占位（`tool_awaiting_decision`，`core/tool/results/tool_awaiting_decision.py`），模型被告知「暂停，回答会作为单独的消息送达，不要重复调用」，Run 挂起。

**作答后的消息**：一条 `user_decision` 消息，正文照 Claude Code 较新的那版回执，批注附在对应回答后：

```text
The user answered: "<question>"="<answer>", "<question>"="<answer>". Read the answers carefully — they may request clarification, changes, or that you not proceed — and follow what they actually say.
```

- 多选的回答是多个 `label` 用逗号连接；填了「Other」就是填的文字。
- 跳过的题写成 `"<question>"="(no answer)"`；全部跳过算驳回，正文为 `The user declined to answer. Adjust your approach instead of asking the same question again.`
- 用户没答、直接发了消息：见 §4。

## 3. 用户看到什么

照 Claude Code 的提问卡片：

- 每次显示一题，右上角「1 of 3」与左右箭头翻页，`header` 作短标签；
- 选项是单选圆点或多选框，每行 `label` 加一行灰字 `description`；`(Recommended)` 原样显示；
- 末尾固定一行「Other」，选中即可输入；每题可附一段批注；
- 底部「Skip」跳过这一题，「Next」（⌘↩）到下一题，最后一题变为「提交」；
- 卡片在回合的「待你处理」区（`turn-layout` 的 attention），不随过程折叠；会话状态显示「等你操作」（现有 `awaitingUser` 与 session feed 计数，不另做）；输入框照常可用；
- 作答后卡片收成一行调用：标题是模型写的 `intent`，元信息「已作答」，展开看每题的问与答；全部跳过后元信息「已跳过」；用户直接发消息导致作废时元信息「未作答」。两者分开，让用户看得出是自己跳过还是没理会。

## 4. 生命周期：提问属于回合

提问问的是「此刻做不做这件事」，它的等待挂在**发起它的回合**上，不按 Run 算（Run 对用户不可见）。

| 事件 | 结果 |
|---|---|
| 首次调用 | 写等待占位（`pending`，`awaitingUser`），Run 挂起 |
| 用户作答（含全部跳过） | 决定入队、唤醒同一个 Run；并入决定的同一事务把占位推到 `completed`，展示数据补上回答；答复留在提问所在的回合 |
| 用户直接发消息 | 消息以 `immediate` 入队、唤醒同一个 Run；它开启新回合，同一事务把更早回合里仍在等待的提问推到 `cancelled`（短提示「未作答」），并给模型一条回合级通知 |
| Agent 消息开启新回合 | 同上（结束条件是「由输入开启的回合」，不区分输入来自谁） |
| 撤回提问所在的回合 | 沿用撤回语义：队列里对这些调用的决定同事务删除，占位随回合移出 |
| Run 停止、失败、完成 | 不结束提问；之后作答照常唤醒或开新 Run（现状规则） |
| 后端重启 | 挂起的 Run 原样保留，占位仍在等待，作答照常唤醒（现状规则） |

**为什么挂起同一个 Run，而不是让 Run 结束**：新 Run 写下的第一行总会开新回合（`RuntimeMessageContextManager._opens_turn`）。作答若开出新 Run，问和答就分属两个回合。挂起时作答只是唤醒，一个回合本来就可以跨多次驱动。

**作废的告知**：用通知，不给原调用补一条结论。等待占位已经是这次调用的最终结果，再补一条会和「结果另行送达」的约定重复。结算时给模型一条回合级通知（经 `mailbox.notify`，与用户新消息同一次 `prepare_input` 写入，排在它前面），文案照 Claude Code：`Your question (tool_call_id=...) was not answered: the user sent a new message instead. Treat that message as their response; do not re-ask.`

**竞态**：决定与用户消息优先级相同（`PRIORITY = 1`），按入队先后并入。

- 决定在先：提问结算为已作答，随后的消息照常开新回合，没有可作废的提问。
- 消息在先：提问先作废，随后并入的决定发现占位已不在等待，只确认、不写入，记日志。决定入口当时校验通过，不代表并入时仍有效，所以并入处要再看一次占位。

## 5. 契约变化

以下四处都是通用 HITL 链路的改造，不为 `ask_user` 开特例；`ask_user` 只是它们的第一个消费方。

### 5.1 决定可以携带结构化载荷

现状：决定只有 `approve` / `reject` 加一句附言（HTTP `DecisionRequest`、消息体 `DecisionBody`、前端 `ToolDecision` 三处同形）。`ToolDefinition.decision_model` 与 `DecisionBody.data` 曾经存在，随 Agent Team 删除时因无消费方一并删掉（见提交 `e41acf90`），恢复它们：

- `ToolDefinition.decision_model`：工具声明决定载荷的形状，与 `args_model` 对称。决定入口按它校验前端输入，不合法时同步拒绝（400 `DECISION_DATA_INVALID`）。
- **校验要看原调用**：所选 `label` 是否属于该题、问题键是否都来自这次提问，只有原调用参数知道。决定入口取出原调用参数（就在 context 里那条调用上），作为 pydantic 校验上下文交给 `decision_model`，不新增钩子。
- `DecisionBody.data` 携带校验后的载荷。
- **谁把载荷写成模型正文**：载荷模型自己（一个渲染方法），因为回执格式是工具的事。`execute` 类工具的载荷不进正文、交给工具执行（原设计）；`reply` 类的载荷渲染进正文。
- `ask_user` 的载荷照 Claude Code：`answers`（以问题原文为键，值为所选 `label`，多选为列表，或「Other」的文字；跳过的题不出现）与 `annotations`（以问题原文为键，带批注）。全部跳过走 `reject`、不带载荷。

### 5.2 回复类决定要结算占位（现存缺陷）

现状：`prepare_input` 并入决定时，只对 `execute` 类在同一事务里把占位推到 `running`（`core/workflow/nodes/prepare_input_node.py` 的 `_write_message`）。`reply` 类的决定只是一条普通消息，占位一直停在 `pending`。后果：

- 卡片永远显示「等你操作」；
- 决定入口按「占位仍在等待」判断，所以能重复提交。

这类工具此前没有一个，所以缺陷一直没暴露。

终态：并入 `reply` 类决定的同一事务里，占位不在等待就只确认不写入（§4 竞态）；在等待则把占位推到 `completed`（全部跳过同样是 `completed`，短提示「已跳过」），展示数据补上回答，提交后发工具状态变化事件。卡片、session feed 的「等你操作」计数随之更新。

### 5.3 决定的有效期由工具声明

`ToolDefinition` 声明决定的有效期，结算逻辑只读这个声明、不按工具名分支：

| 有效期 | 适用 | 何时结束 |
|---|---|---|
| 回合 | `ask_user` | 作答、撤回所在回合、下一个由输入开启的回合 |
| 到交还 | `browser_handoff` | 交还、撤回所在回合、删除 Session（现状不变） |

结算点在 `prepare_input`：并入一条开启回合的输入（`TURN_INPUT_KINDS`）时，同一事务把更早回合里「有效期为回合且仍在等待」的占位推到 `cancelled`，并投回合级通知（§4）。作答不是输入、不开新回合，不会误伤正在作答的提问。

### 5.4 挂起时用户消息可以唤醒

现状：Session 有活动 Run（含挂起）时，用户消息以 `next_run` 排队（`assistant_transport_service.py` 的发送路径），挂起的 Run 不被 `next_run` 唤醒（`dispatch_policy`）。所以提问挂着时，用户发消息会干等。

终态：发送路径判定投递方式时，Run 挂起且 Session 有「有效期为回合、仍在等待」的提问，就用 `immediate`。消息唤醒同一个 Run，并入时按 §5.3 结算。

- 判定与入队仍在 Session 闸门内完成。
- 判定规则归 `dispatch_policy` 这类纯函数，事实由发送路径取来交给它。
- **只放开这一种**：只在等后台命令的挂起维持现状。后台工作属于 Session 不属于回合，「挂起时不唤醒」的原因这次不必查清。挂起时如果同时有后台命令在跑，唤醒后它照常运行，结论稍后照常回投。

## 6. 现有机制已覆盖、不用改的

- **只给侧栏会话**：`user_decision` 工具自动算「需要用户界面」（`ToolDefinition.requires_user_surface`），按 `SESSION_KINDS` 不进子 Agent 的工具列表。Claude Code 同样不给子 Agent。子 Agent 需要用户决定时，在结论里交代，由上级去问。
- **只能单独调用**：需要用户决定的工具不和其他调用同批执行（`tool_batch_runner`）。
- **「等你操作」**：part 的 `awaitingUser`、session feed 计数、侧栏与标题徽标都已经从等待占位推导。
- **fork**：占位在切点前、结论在切点后的调用按未继承结算（现状）。
- **撤回**：撤回回合时删除队列里这些调用的决定（现状）。

## 7. 提示词

- 工具描述见 §2，它本身就包含「什么时候不该问」。
- 系统提示词同时加入 Claude Code 的「信息够了就动手；权衡时给建议，而不是罗列选项」一句（根目录 `claude-code-prompt-checklist.md` 第 4 项），免得提问被滥用。
- 沙箱落地后，越界审查拒绝的观察会引导模型「确需这样做，用 `ask_user` 向用户说明风险、征得许可后重试」（沙箱稿 §6.2）。本工具不感知这个用途。

## 8. 展示契约

- 展示 `kind: "user-question"`。等待时展示数据就是参数原样（`questions`）。结算后补上 `answers` / `annotations`（作答），或不补（作废、驳回），状态与短提示走现有通道。
- 前端：`call-view-registry` 按 kind 注册视图——等待时是提问卡片（操作区），结算后是一行调用加展开的问答。工具目录加 `ask_user` 条目（注册表守卫要求）。
- 提交走 `session-commands` 现有的决定命令，载荷随请求带上；命令暂态沿用 `command-request-store` 的决定槽位。

## 9. 不做

- **选项预览的渲染**：参数照收，首版卡片不显示，以后加。
- **提问超时**：Claude Code 可配，cosir 没有需求。
- **子 Agent 冒泡**：子 Agent 拿不到本工具。
- **带图片的提问**：以后需要时另行设计。
- **专门的权限审批工具**：征得许可只是提问的一种用途。

## 10. 验收

后端单测：

- 参数校验：题数、选项数、`header` 长度、问题重复、同题 `label` 重复都被拒；
- 首次调用写等待占位、Run 挂起，子 Agent 的工具列表里没有它；
- 作答：载荷按原调用校验，非法 `label`、未知问题键、单选多值都被拒（400）；合法回答唤醒同一个 Run，正文符合回执格式（多选、Other、批注、跳过）；占位推到 `completed`，展示数据带上回答；
- 重复提交被拒（409 `DECISION_NOT_AWAITED`）；
- 全部跳过渲染为驳回；
- 用户直接发消息：以 `immediate` 入队、唤醒同一个 Run、开新回合；旧提问推到 `cancelled`，模型收到作废通知；之后迟到的决定只确认不写入；
- 只等后台命令的挂起，用户消息仍以 `next_run` 排队（行为不变）；
- 撤回提问所在回合，队列里的决定被删；Run 停止后作答，开新 Run。

E2E（无头）：卡片翻页、单选 / 多选、Other、批注、Skip、提交后收成「已作答」；挂着卡片直接发消息后卡片变「未作答」。

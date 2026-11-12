# 外部消息收件口

> 状态：2026-11-12 设计稿，未实施。只定扩展约定；第一个真实来源接入时按本稿落地，在那之前不写骨架（空登记表、无人使用的请求分支都是预留扩展点）。
> 相关：唤醒范围 `WakeScope`（`core/workflow/message_queue/message_bodies/base.py:19`）与通知按种类登记的做法（`notice.py:36`、`notice.py:50`）是本稿直接复用的机制。

## 1. 要什么

**场景**

- 飞书：同事在群里 @ Agent，对应 Task 多出一条「来自飞书 · 张三」的消息，Agent 被叫起来处理；群里没 @ 的普通聊天只作为背景入队，不打扰 Agent。
- 界面点击：用户点了卡片上的「用这个方案」，Agent 立刻知道并继续；用户只是打开了一个文件，Agent 下次运行时知道就够了。
- 以后还会有更多来源（CI、定时提醒、其他 IM），现在想不全。

**完成标准**：新增一种来源只做三件事——登记表加一项、写一个适配器、前端（若要显示）加一种来源图标；不改调度、不加消息类型、不新开接口。每种来源自己声明消息到达后怎么办：只入队、Agent 在等时叫醒、或直接让 Agent 开始干活。

**最不想看到**

- 一个吵的来源不停叫醒 Agent，白白花钱。
- 外部内容被模型当成用户本人的指令。
- 每接一个来源改一大片代码，或者每个来源各有一套唤醒、投影、渲染规则。

## 2. 消息的说话方

Task 收到的消息按「谁在说」分四方，外部是新增的一方：

| 说话方 | 消息体 | 模型读到 | 唤醒由谁定 |
|---|---|---|---|
| 用户 | `UserInputBody` | `<user_input>` | 类型固定：空闲时开 Run |
| Agent | `AgentMessageBody` | `<agent_message>` | 类型固定 |
| 系统 | `NoticeBody` | `<system_notice>` | 按通知种类登记（`NOTICE_WAKE_SCOPES`） |
| **外部** | **`ExternalMessageBody`（新增）** | **`<external_message>`** | **按来源登记，单条可往低调** |

不在这张表里的是**调用回执**：后台结论（`BackgroundResultBody`）与用户决定（`DecisionBody`）绑定某次工具调用，不是谁在说话。决定入口 `POST /tasks/{id}/tool-calls/{call_id}/decision` 保持独立：`execute` 类决定不经模型、由 `prepare_input` 直接重放原调用，校验（仍在等待、无重复决定、载荷形状）与冲突码都与发送不同，并进发送入口只会多一个分发器。

外部消息不复用系统通知：通知是系统自己的话，读法是「静默纳入、不回应」；外部内容来自不可信来源、有具体发送人，Agent 可能要回应它。也不为每个来源各写一个消息体：差别只在登记项，不在结构。

## 3. 来源登记表

外部来源是封闭集合，登记在 `ExternalMessageBody` 所在模块，与通知种类表同一做法：

```python
ExternalChannel = Literal["ui_action", "lark"]          # 举例；第一个来源接入时才出现第一项

@dataclass(frozen=True)
class ChannelPolicy:
    wake_ceiling: WakeScope    # 这个来源最多能把 Task 推进到哪一步
    default_wake: WakeScope    # 单条没指定时的唤醒范围，不得高于上限
    projected: bool            # 是否显示在对话里
    http_ingress: bool         # 是否允许经本机 HTTP 投递（见 §6）

EXTERNAL_CHANNELS: dict[ExternalChannel, ChannelPolicy] = {...}
```

- **唤醒范围三档直接复用 `WakeScope`**：`none` 只入队，随下一次推进并入；`active` 只对活动 Run 起作用（驱动中不挂起、挂起中唤醒），Task 空闲时不开 Run；`new_or_active` 可以开新 Run。调度规则（`dispatch_policy.next_action`）与挂起判定（`TaskMessage.wakes`）不用改。
- **单条只能往低调，不能超过上限**：飞书来源上限是 `new_or_active`，适配器发现没 @ Agent 就把这一条降成 `none`。请求超过上限时拒绝（400），不静默降级。
- **登记项不含模型读法**：`<external_message>` 不单写读法（`PROMPT_GUIDE = None`），可信度由消息协议前言的通用规则交代（只有声明为用户本人的话的标签才是指令，其余都是待评估的信息，见 `SystemPromptBuilder._input_protocol_lines`），来源由 `channel`、`from` 属性自解释；更不按来源各写一段——来源多了提示词会跟着膨胀，每加一个来源还要让已固化的系统提示词前缀失效。某个来源需要额外语境时，写成这条消息自己的属性（如飞书的 `mentioned="true"`），在消息出现的地方自解释。
- **去重、合并、过期**按需加进登记项（如 `dedup_key`、`coalesce_key`、`ttl`），由第一个需要它的来源驱动设计，本稿不预设字段。

## 4. 消息体与回合

```python
class ExternalMessageBody(EnqueuedMessageBody):
    KIND = "external_message"
    WAKE = "new_or_active"     # 类型上限；实际范围看 wake_scope()
    channel: ExternalChannel
    sender: str                # 发送人显示名快照（飞书昵称、"你"）
    ref: str | None            # 来源侧的定位信息（飞书消息 id、界面元素 id），只用于展示与排查
    text: str
    data: dict | None          # 结构化载荷，按来源约定；进模型时序列化在正文里
    wake: WakeScope            # 入队时已按登记表校验

    def wake_scope(self) -> WakeScope:
        return self.wake
```

- **渲染**：`<external_message channel="lark" from="张三" ref="...">` 加转义后的正文，与 Agent 消息同一套边界做法（`agent_message.py:67`）；不单写读法：消息协议前言的通用规则已说明它不是用户的指令，来源看属性。
- **并入时机**一律 `immediate`：驱动中下一次模型请求前并入，空闲时随下一次推进（或按唤醒范围开出的新 Run）并入。
- **发起配置**：外部消息不携带 `RunLaunch`，不算开启回合的输入（不进 `LAUNCH_INPUT_BODIES`）；凭它开新 Run 时继承当前 Run 的配置，Task 还没有 Run 时不动（`next_action` 现有规则）。
- **回合**：沿用 Agent 消息的规则——作为新 Run 的第一条输入到达时开启回合，途中到达时是当前回复的一段。开回合的判定只在 `RuntimeMessageContextManager._opens_turn`，届时把 `external_message` 加入 `TURN_INPUT_KINDS`（`models/message_metadata.py:34`）。外部消息开启的回合不能撤回（撤回目标只认用户输入，现状不变）。
- **登记处**：按 `message_bodies/__init__.py` 的约定，新类型同时进 `MessageBody`、`MODEL_INPUT_BODIES` 与 `_BODY_OF_KIND`，`TaskMessageKind` 与 context metadata 的 `MessageKind` 各加一项。

## 5. 入口：服务层是收件口，HTTP 是一种通道

```text
飞书适配器（后端进程内）──┐
                        ├─→ ExternalMessageService.receive(task_id, channel, ...) ─→ task_runtime_spaces.enqueue
前端（界面点击）─ HTTP ──┘        校验来源已登记、唤醒不超上限、正文形状
```

- **收件服务** `service/conversation/external_message_service.py`：唯一的外部消息入口；校验来源已登记、`wake` 不超过上限（缺省取 `default_wake`）、正文与载荷形状，构造消息体后经 `task_runtime_spaces.enqueue` 入队，记一条结构化日志 `external_message_received`（`task_id`、`channel`、`wake`、`ref`）。不在闸门内判定排队与否：外部消息一律 `immediate`，推进由 actor 按唤醒范围决定。
- **HTTP**：`POST /tasks/{id}/messages` 的请求体按 `from` 区分，`"user"` 走现有发送（`transport_assistant_service.py:52`，行为不变），`"external"` 交给收件服务。绿地项目不做兼容：前端发送请求届时补上 `from: "user"`。错误映射沿用现有约定：来源未登记或不允许 HTTP 投递 400、Task 不存在 404。
- **进程内适配器**（如飞书事件订阅）直接调收件服务，不绕 HTTP。外部会话与 Task 的对应关系（哪个群投给哪个 Task、要不要新建 Task）归适配器，不归收件口。

## 6. 风险

- **注入**：外部正文一律放进带来源的边界标签并转义，「不是用户的指令」由消息协议前言的通用规则统一交代，来源再多也不会因为某个来源漏写读法而失守。
- **本机 API 无认证**：后端 CORS 放行任意本机来源、API 无认证（AGENTS.md 待办「本机 API」）。HTTP 外部分支一旦开放，任何本机网页都能往 Agent 上下文里投消息。缓解：只有登记为 `http_ingress` 的来源能经 HTTP 投递（界面点击这类本来就来自前端的来源），飞书等进程内来源不开 HTTP 通道；完整的认证随安全与权限整体设计。
- **成本**：能开 Run 的来源上限设为 `new_or_active` 前要想清楚触发频率；吵的来源默认 `none`，由适配器按条件（如被 @）上调到上限以内。

## 7. 消费方

| 消费方 | 跟着改什么 |
|---|---|
| 调度（`dispatch_policy`、`TaskMessage.wakes`、`RunMailbox.has_wakeable`） | 不改：只读 `wake_scope()` |
| `prepare_input` 写入 | 按通用路径并入 context，metadata `kind="external_message"` 并带来源 |
| 系统提示词消息协议（`SystemPromptBuilder` 遍历 `MODEL_INPUT_BODIES`） | 标签自动列入「靠属性自解释」的一行；之后加来源不改提示词 |
| Transport 投影 | 开回合时是带来源的 user 消息，途中到达时是回复里的一段；来源 wire 形状在 Agent 消息来源旁加外部来源（`channel`、`sender`） |
| 前端 | 复用 `AgentMessageRow`（`components/conversation/thread/agent-message.tsx`）的行形态，按来源显示图标与发送人；不可编辑 |
| 撤回 | 不改：外部消息开启的回合不可撤回；撤回其他回合时，途中并入的外部消息随回合移出 |
| 启动调度（`ADVANCING_KINDS`） | 类型上限不是 `none`，自动算作待处理消息 |

## 8. 分阶段

| 阶段 | 内容 | 验证 |
|---|---|---|
| P0（本稿） | 约定说话方、登记表、收件服务与 HTTP 分支的形状 | 无代码 |
| P1（第一个来源接入时） | 消息体、登记表（只含这一个来源）、收件服务、该来源需要的通道（HTTP 分支或进程内适配器）、投影与前端行 | 每档唤醒范围各一条集成测试（只入队不开 Run、挂起时唤醒、空闲时开 Run 并继承配置）；超上限被拒；渲染转义；回合开启与途中到达两种投影 |
| P2（某个来源需要时） | 去重、合并、过期 | 由该来源的场景定 |

## 9. 被否方案

- **把决定并进发送入口**：决定是对工具调用的回执，校验与冲突语义不同，合并只多一个分发器（§2）。
- **调用方自由指定唤醒范围**：外部系统能随意让 Agent 开 Run，成本与注入风险都失控；改为登记上限、单条只能往低调。
- **每个来源一个消息体**：来源之间差别只在策略，不在结构；按种类查表与通知同一做法。
- **每个来源在系统提示词里各写一段读法**：提示词随来源膨胀、加来源就让系统提示词前缀失效；前言的通用规则加消息自身属性已经够用。
- **外部消息走系统通知**：读法、可信度、是否需要回应都不同（§2）。
- **现在就写骨架**：登记表为空、HTTP 分支无人调用，是预留扩展点；约定写在本稿，第一个来源落地时一次做完。

## 10. 刻意不做

- 外部会话与 Task 的路由（群 → Task、是否自动建 Task）：归各适配器的设计。
- Agent 回发外部来源（如回复飞书）：是工具（或 MCP）能力，不是收件口的事。
- 本机 API 认证：随安全与权限整体设计（AGENTS.md 待办）。

# Agent 权限档位与沙箱

> 状态：2026-11-09 设计稿，未实施；11-13 按 Codex 新版修订（规则模型、提权保留信任根、屏蔽后端端口、缓存可写、Seatbelt 绕过点、终端优先分期），档位定为三个内置档、按回合生效、派生可收窄档位与工作区范围（§3），审批改为回合级（§5.5）、子 Agent 越界冒泡给用户（§5.4），Agent 消息不带档位、档位变化以通知告知（§3），自动审查与自定义档位列为待做（§11）；11-13 随 Agent Team 移除删去 Team 节点相关设计。取代同日 DeepSeek 起草的「终端沙箱」稿与更早的 srt sidecar 稿（git 历史）。
> 前置：[`terminal-ownership-redesign.md`](terminal-ownership-redesign.md) **已实施**——Agent 没有常驻 shell，后端起用户进程只有 Terminal Worker 一处（`tools/terminal_runtime/worker_client.py`），Agent 的命令只经 `execute_terminal` 的执行模式。
> 参考：Codex（`codex-rs`，HEAD `5045589550`）、sandbox-runtime（commit `98a4016`），借鉴清单见附录。

## 1. 要什么

**完成标准**：用户在输入框旁选一个权限档位（仅可查看 / 工作区内修改 / 完全权限），Agent 在档位内照常干活、不打扰；越界时停下来问，用户批准后就执行这一次调用。

**最不想看到**：

- 沙箱让 Agent 频繁失败或频繁弹窗，用户最后只敢开「完全权限」。
- 看似隔离、实际没隔离（某条通道绕过档位；沙箱不可用时悄悄裸跑；批准一次提权就交出模型 Key；命令调后端 API 自己批准自己）。
- 档位、审批、受保护路径各有一套事实，改一处漏一处。
- 用户自己的操作被沙箱拦住。

**本期防什么**：Agent 失误造成的破坏——写到工作区外、写进「下次自动执行」的文件、读到 cosir 自己的机密。**不防注入后外传**：浏览器、`web_extract` 本身能把数据带出去，单封终端网络换不来安全（见 §13）。这一点写进档位说明与提示词，不让人误以为已隔离。

## 2. 模型：三个内置档位

**档位 = 文件系统规则 × 网络**，只有三个内置档，定义在代码里。越界一律由用户审批（§5）；「越界时拒绝」与「谁来批」两个维度要等自定义档位与自动审查（§11 待做）出现第二个取值时再引入。

**文件系统规则**只有一种形态：一组 `path × access`，`access` 取 `read` / `write` / `deny`；路径越具体越优先，同样具体时 `deny > write > read`（Codex `protocol/src/permissions.rs:108-136`）。档位、受保护路径、例外（如系统 `.cosir` 禁读而 `skills/` 可读）都是这组规则里的条目，门禁与 worker 消费同一份。Codex 的旧三档枚举与新模型并存、互转代码很多，这里一开始就只有新模型。

| 档位 | 文件读 | 文件写 | 命令网络 |
|---|---|---|---|
| 仅可查看 | 全盘（受保护除外） | 无 | 关 |
| 工作区内修改（默认） | 全盘（受保护除外） | 工作区 + 临时目录 + 包管理器缓存（受保护除外） | 开 |
| 完全权限 | 不限（不进沙箱，不越界） | 不限 | 开 |

- **受保护路径**不属于任何档位，全局生效（§6）。
- **网络**本期只有开 / 关两态：macOS 上「关」就是 SBPL 不放行网络，几乎零成本；域名白名单要本机代理，放到后面（§13）。

**不变式**

1. 档位只约束 **Agent 发起的动作**。人的终端永远不进沙箱，包括你点回复里命令块的「运行」、或在 Agent 填入后按回车执行的命令（终端改造稿 §3.3、§3.4）。
2. Agent 发起的进程只有一个出口 `execute_terminal`，一律按档位起进程；没有绕过档位的工具、参数或终端。
3. 沙箱不可用时**不裸跑**：退化为「每条命令都要问」（§8）。

## 3. 生效粒度：随回合切换

11-13 定：档位按**回合**生效，不随 Run 冻结。用户想收紧或放宽，下一条消息就生效，不用等当前 Run 结束（Run 可能因等后台结果挂起很久）；Codex 的权限同样是会话设置，改动从后续回合起生效（`protocol/src/protocol.rs` `ThreadSettingsOverrides`）。

- **档位是输入的事实，不是 Run 的发起配置**：用户输入与派生 Task 的首条输入（任务说明，§3.1）的消息体带 `permission_mode`（三个内置档 id 之一），并入时写进该行 metadata；其余 Agent 消息（`send_message`、追加消息、汇报）**不带档位**——派生 Task 已由创建时的档位与上级链两层约束，平级 Task 之间的「借道提权」属于注入威胁，本期不防（§1）；若让汇报携带 min 档位，主 Task 收到只读子 Agent 的汇报就会被降为只读。档位不进 `RunLaunch` / `ConversationRunOptions`（`core/workflow/message_queue/message_bodies/user_input.py:31`）——发起配置在活动 Run 期间锁定，档位不锁。
- **Task 自身档位 = context 里最近一条并入的、带档位的输入**。用户输入总是开启回合（`RuntimeMessageContextManager._opens_turn`），所以对用户而言就是「从这条消息开启的回合起生效」；没有输入开头的回合（唤醒、结果续跑）沿用上一条输入的档位。事实只在 context 一处：重启后续跑、撤回回合（行移出上下文，档位回到更早的输入）、fork（行随 metadata 复制）都自然正确，Run 上不另存。
- **生效档位 = min(自身档位, 上级的生效档位)**，沿上级链递归（上级：委派与 Agent fork 的派生方 `TaskRecord.deriver_id()`；侧栏 Task 没有上级）。上级收紧时下级的下一次调用即按收紧后判定，不用等消息；上级放宽不会超过下级自身档位。门禁每次调用读一次上级链（深度受派生上限约束），不在下级缓存上级的档位。
- **两处的分工**：当前驱动的**策略快照**（只读值对象，放进 `RuntimeContext`，经 `ToolExecutionContext` 交给门禁与工具）只保存**自身档位**，`prepare_input` 并入带档位的输入后替换；**生效档位**在每次调用时由门禁计算——取快照里的自身档位与上级链里的最严者，再展开成规则交给门禁判定与 worker 起进程。审批后的重放同样按执行时的生效档位。已在运行的命令进程不追溯（沙箱在起进程时定死），收紧档位不停它们。
- **前端**：档位选择器不随活动 Run 锁定；运行中发出的消息照常排队，带的是发出时选的档位，并入时生效。选择器的「上次选择」是 localStorage 偏好（同模型选择）；首次默认「工作区内修改」。
- 批准范围（「本 Task 内允许」）记下批准时的档位，切换档位不清空，但只在当前生效档位不比它更严时生效（§5.3）。

三档全序：仅可查看 < 工作区内修改 < 完全权限。现在派生与消息路径只透传模型与推理强度（`core/tool/multi_agent/delegate_task.py:189-205`、`agent_message.py:43`、`send_message.py:164`），派生要补档位与工作区范围（§3.1）。

**让 Agent 知道当前档位**：生效档位变化（用户切档、上级收紧、上级放宽后回到自身档位）后的下一次模型请求前，写入一条 Task 级 `<system_notice>`（新增通知种类，同 `system_prompt_delta` 走「环境变化」通道，`core/workflow/message_queue/message_bodies/notice.py:36`），写明当前档位、可写范围、网络开关与越界怎么申请。比较基准是 context 里上一次通知的档位，所以重启、撤回、fork 后照常补发、不重复。系统提示词只写不变的规则（§10），不随档位改写，也就不破坏提示词缓存。

### 3.1 派生：创建时指定档位与工作区范围

委派（`delegate_task`）与 Agent fork（`fork_task`）各加两个可选参数，**只能收窄、缺省继承**：

| 参数 | 取值 | 缺省 | 约束（工具执行时校验，越界返回错误并写明上限） |
|---|---|---|---|
| `permission_mode` | 三档 id | 发起方当前生效档位 | ≤ 发起方当前生效档位 |
| `workspace_scope` | 工作区内的目录（相对工作区根，或工作区内的绝对路径） | 发起方的工作区范围 | 已存在的目录；取真实路径后在发起方范围之内（含相等）；不能落在受保护路径里（如 `.cosir`、`.git`） |

- **档位**写进派生 Task 首条输入（任务说明）的 metadata，即它的自身档位；之后按上面的「最近一条输入」与「上级链取 min」规则走。参数是严格 schema 里的枚举，不按 Task 收窄 schema——上限随发起方的回合变化，只能在执行时比。
- **工作区范围**是 Task 级事实，创建时定、之后不变：`TaskExtra.workspace_scope`（相对工作区根的路径，缺省 `None` 即整个工作区；侧栏 Task 恒为 `None`）。上级的范围也不可变，所以「⊆ 上级」只在创建时校验一次。
  - 消费方：沙箱规则里的 `:workspace` 展开为范围根（§8）；写类文件工具的工作区边界（P1 前是 `PathResolver` 的 containment 根，P1 后归门禁）；`execute_terminal` 缺省 `workdir`。三者都从 `ToolExecutionContext` 取，装配点只有一处（`core/workflow/node_helper/tool_batch_runner.py:171`），新增 `scope_root` 字段与 `workspace_root` 并列。
  - **不变的**：相对路径仍相对工作区根解析，工作区 `.cosir`（附件、下载、日志）仍在工作区根——Task 树与界面（链接、文件面板）共用一套坐标，范围只收窄可写区与缺省目录，不是另一个工作区。读不受范围限制（读本来就是全盘）。
- 子 Agent 经档位通知（见下）得知自身的档位与范围；委派与分叉行的展示数据带上二者（`delegation-result` / `fork-task-result`），用户在时间线上看得到「只读 · packages/api」。
- 批准范围（grant）不随派生继承。


## 4. 两个执行点

同一份策略快照，按动作的性质落到两处：

| 通道 | 执行点 | 本期规则 |
|---|---|---|
| 读类文件工具（`read_file` / `search_content` / `find_files` / `list_directory` / `read_image`） | 工具自身（`PathResolver`） | 受保护读路径一律拒绝，与档位无关；目录类工具从上层目录遍历时跳过受保护子树 |
| 写类文件工具（`write_file` / `patch_write` / `apply_patch` / `delete_file` / `move_file`） | **准入门禁** | 目标在可写区 → 放行；否则问用户（子 Agent 同样冒泡给用户，§5.4） |
| `execute_terminal` | **OS 沙箱**（worker 起进程时） | 按策略起进程；调用显式请求越权时先问（§5） |
| MCP 工具 | 准入门禁 | 「仅可查看」下问；其余档放行。MCP 服务进程本身不进沙箱 |
| 读用户终端、`terminal_write` 填入、回复里的命令块 | — | 都不执行任何东西（执行由人触发），不受档位约束 |
| 浏览器、`web_search` / `web_extract` | — | 本期不受档位约束（§13），档位说明里写明 |

**为什么文件工具走门禁而不是沙箱**：文件工具在后端进程内执行，OS 沙箱管不到；而它们的目标路径在执行前就能从参数算出来，门禁能在执行前判定、越界时当场问，比「执行失败再申请」少一个来回。命令的影响无法从参数推出，只能由 OS 沙箱在运行时兜住。

**门禁怎么知道一次调用要访问什么**：`ToolDefinition` 声明访问类别（读 / 写 / 执行 / 外部）；写类工具另提供「从已校验参数取目标路径」的纯函数（`apply_patch` 从补丁文本解析目标）。门禁在现有三段检查（`core/tool/execute/tool_access_gate.py:91`：注册表 → Agent 工具权限 → 图片能力 → 参数校验）之后加第四段「权限」，输出 放行 / 要决定 / 拒绝。

**契约随之变化**：写类工具今天在 `PathResolver.resolve_within_workspace` 里自己强制「必须在工作区内」（`write_file.py:117`、`apply_patch_tool.py:145` 等 7 处）。改造后「工作区内」是档位规则，归门禁；`PathResolver` 只守与档位无关的不变量（受保护路径、工作区 `.cosir` 保留区、设备路径），否则「批准写工作区外」会被工具自己再拒一次。

## 5. 越界与审批

### 5.1 怎么发起

- **文件工具**：门禁按目标路径判定，模型不用多写参数。
- **命令**：只有模型知道一条命令需要沙箱外的能力（写 `~/.npmrc`、装全局包），所以由它在调用上声明——`execute_terminal` 增加 `sandbox: "default" | "escalated"` 与 `justification`（一句给用户看的理由）。照 Codex（`core/src/tools/handlers/shell_spec.rs:230-264`）。
- **批准后仍在沙箱里跑**：这一条命令全盘可写、网络开，但 cosir 的信任根（§6 第一类）照旧禁读禁写，后端端口照旧屏蔽（§8）。照 Codex「策略含禁读时不许无沙箱运行」（`core/src/tools/sandboxing.rs:290-302`）：否则一次批准就顺带放开了 `.env` 里的模型 Key。只有「完全权限」档不进沙箱。
- 不做独立的「申请提权」工具：那样要「执行失败 → 申请 → 重跑」三个来回，批准的也不是那条命令本身。

### 5.2 复用 HITL 的等待机制，拆开「在问什么」

现有 HITL：工具声明 `user_decision="execute"` → 首次调用返回等待占位 → 用户决定入队 → `prepare_input` 确认决定后绕过模型重放原调用。等待机制（占位、决定入队、重放）能跨重启，权限审批照用这一套。

**问题**：「要不要问、问完怎么办」今天全靠工具名查 `ToolDefinition`，消费方散在三处——重放还是交给模型（`core/workflow/nodes/prepare_input_node.py:107-114`）、单独成批（`core/workflow/node_helper/tool_batch_runner.py:109`）、是否发布 running（`core/tool/execute/tool_executor.py:89` 以「有没有 `user_decision`」判断是否重放）。权限审批的工具本身（如 `execute_terminal`）不是 HITL 工具，查定义查不出它在等审批，照搬就要在三处各加一个「或」；权限审批还要带载荷（批准范围），而决定载荷的校验已随 Team 一起删除（`decision_model`），要在等待事项上重新引入。

**成熟做法**：Codex 把两件事分开——权限审批归执行管线，工具只声明需要什么（`Approvable` trait，`core/src/tools/sandboxing.rs:331-363`），由编排器去问、等、缓存，工具看不到「在等审批」；工具自己的用户交互是工具（`core/src/tools/handlers/request_user_input.rs`）。Claude Code 同样分开（权限规则与提示 vs `AskUserQuestion` 工具）。

**终态**：占位 metadata 记一个「在问什么」的值对象（等待事项），两种取值，四个消费方都只调它的方法、不再各自判断：

| 取值 | 契约来源 | 决定载荷 | 决定后 | 有效期（§5.5） |
|---|---|---|---|---|
| 工具交互（`browser_handoff`） | 工具的 `ToolDefinition` | 无 | 按工具声明：重放（工具经 `ToolExecutionContext.user_decision` 拿到决定）或交给模型 | 工具声明 |
| 权限审批 | 固定 | `{scope: "once" \| "task"}` | 经门禁带着批准重放；工具照常执行，看不到这次决定（提权体现在交给 worker 的规则上） | 到下一回合 |

1. 门禁判定「要决定」→ 执行器直接返回等待占位（`tool_awaiting_decision`），等待事项为权限审批，展示数据是通用审批卡（`kind: "permission-request"`：动作、目标、理由、发起的 Task）。
2. 决定入口照旧按 `tool_call_id` 寻址，载荷按等待事项校验；`prepare_input` 按等待事项路由。
3. 重放时门禁看到本调用的批准决定 → 放行（命令按提权策略执行、写入放行该路径）；驳回 → 返回错误观察，写明「用户拒绝，这一步没有做」，防止模型绕路后报告做成了。
4. `tool_executor` 发布 running 的条件改为「是不是重放」，不再看 `user_decision`。
5. 「要决定的调用必须单独成批」保留，`_requires_user_decision` 改为调用门禁的同一纯函数——判定在执行前就能做出。

两种取值各有真实用例，是封闭集合，按「抽象按需引入」可以成立；不为它另设注册表。

### 5.3 批准范围

审批卡三个按钮：**批准本次** / **本 Task 内允许** / **拒绝**。

- 「本 Task 内允许」的键：命令按「规范化命令文本 + workdir + shell + 提权类型」精确匹配（Codex `ApprovalStore` 的键同样带 cwd 与权限，`core/src/tools/sandboxing.rs:45-122`）；文件写按目标路径。
- **grant 记下批准时的生效档位**，只在当前生效档位不比它更严时生效：你调到「仅可查看」就是不想让它写，之前给过的写入授权不能让它绕过询问；回到原档位后 grant 恢复，不用重批。
- **grant 只属于发起审批的那个 Task**：子 Task 审批卡上的「本 Task 内允许」只放行那个子 Task，不上传给上级、不下传给它的派生（Codex 的会话级缓存也不跨线程；Claude Code 作用于整个会话，有误放权风险）。
- grant 归进程内 `PermissionGrantRegistry`（按 `task_id`），不持久化、不跨重启，Task 删除时清理——与交接登记表同类。
- 不做「永久记住」与永久规则：本期没有权限配置文件（§7），放宽只靠切档位或单次批准。

### 5.4 子 Agent 越界：冒泡给用户（11-13 定）

委派的子 Agent 没有侧栏对话（`models/task_kind.py` 的 `user_facing=False`），但越界时**不直接拒绝**，审批照常挂在发起调用的子 Task 上，由用户作答：

- **为什么不「报回上级」**：成熟产品在有人值守时都冒泡给用户（Codex 把非当前线程的审批浮到当前视图并标明来源，`tui/src/app/thread_routing.rs:1456`；Claude Code 后台子 Agent 改过三次，现行也是冒泡）；「拒绝 → 汇报 → 上级重做」费时易走样，Claude Code 有子 Agent 被拒后绕路、审查了错误分支却报告无问题的 issue。**上级 Agent 不能替用户批准**——各家都不允许，Agent 消息不算用户的批准。
- **在哪里批**：子 Run 挂起等待；委派行按应用级任务状态推送显示「等你操作」（现有链路），点开在工作台的子 Task 里作答。审批卡写明发起的 Task。
- **不改 `TASK_KINDS`**：「非侧栏 Task 不承接需要用户界面的工具」管的是工具 schema；权限审批由门禁发起、不是工具，不进子 Task 的工具列表。要补的只是工作台能渲染审批卡。
- **上级要收到信号**：`task_status` 与 `wait_messages` 能看到「派生 Task 在等用户批准」，上级不会傻等。Codex 的子 Agent 状态没有这一项（`protocol/src/protocol.rs:2074-2090`），Claude Code 有「后台子 Agent 卡在提示上、父会话收不到信号、流程挂几小时」的 issue。
- **有效期**按 §5.5：上级给子 Task 发了新的 Agent 消息、开启新回合时，旧审批作废——上级已经换了指示。
- **无人值守时直接拒绝**不做开关：它就是自定义档位的「越界时拒绝」（§11 待做）。

### 5.5 审批的有效期：回合级（11-13 定）

现状 HITL 是 Task 级：等待中的决定在 Run 停止、失败、完成、重启后仍可决定，到达后在当时的 Run 里重放原调用。现有 HITL 工具只有 `browser_handoff`（`execute` 类），权限审批是第二个。Task 级有两个问题：

- **过时的批准照样执行**：第 3 回合申请的提权，用户在第 7 回合才点批准，命令在第 7 回合重放，Agent 早已换了做法；档位按回合生效后还多一层「按哪个回合的档位跑」的歧义。
- **审批挂着时用户的新消息被卡住**：等待占位让 Run 挂起（`core/tool/results/tool_awaiting_decision.py:33`），挂起时 `next_run` 不唤醒（`core/workflow/message_queue/dispatch_policy.py:90-94`），用户说的话在队列里等到他去批卡或点「立即发送」。

**终态**：决定的有效期由工具在 `ToolDefinition` 上声明，取值两种，结算逻辑只读这个声明、不按工具名分支：

| 有效期 | 适用 | 何时结束 |
|---|---|---|
| 到下一回合 | 权限审批——问的是「此刻做不做这件事」 | 用户决定；撤回所在回合；**下一回合开启**。Run 停止、失败、重启不结束（所在回合仍是最新回合，停止后照样能批） |
| 到交还 | `browser_handoff`——一段租约，用户可能边登录边发消息问问题 | 交还、撤回所在回合、删除 Task（现状不变） |

- **新回合开启时**：更早回合里仍在等待的「到下一回合」审批一律结算为「未批准：用户发了新消息」，占位推到终态，结果写进原调用，模型在新回合看到。结算放在开启回合的同一事务里（`prepare_input` 并入开启回合的输入时），与「确认决定 + 占位推到 running」互斥，迟到的决定照旧以 `DECISION_NOT_AWAITED` 拒绝。
- **新消息即答复**：Run 挂起、且只在等「到下一回合」的审批时，用户输入唤醒同一个 Run、开启新回合，旧审批随之作废——发新消息就是「我不批，我想说别的」（Claude Code、Codex 同此：权限提示期间输入新指令即拒绝）。现规则「挂起时 `next_run` 不唤醒」是有意定的，实施前先查清原因，并定 Run 挂起在等后台结果时要不要一起改（Q11）。
- **不受影响**：「本 Task 内允许」（§5.3）是已经给出的授权，不是待回答的问题，仍按 Task；Run 级不考虑——Run 对用户不可见（Transport 只按 Task + 回合），一个 Run 也可以有多个回合。

## 6. 受保护路径：一份事实，三处消费

新模块 `app/core/permission/protected_paths.py` 是唯一事实源，消费方：门禁（写判定）、`PathResolver` 的读路径（读拒绝）、策略快照（交给 worker 编译进沙箱）。浏览器 `upload`（`tools/tool_handler/browser/act.py:89-107`）经 `resolve_for_read` 自然跟上。`search_content` / `find_files` 走的是 `resolve_without_boundary`（`search_content.py:76`、`find_files.py:66`），不经 `resolve_for_read`，而且会从上层目录递归进受保护目录——读拒绝要放在两条解析路径共用的地方，遍历时也要剪掉受保护子树。

| 类别 | 内置条目（规则） | 提权后 | 依据 |
|---|---|---|---|
| cosir 信任根 | 系统 `.cosir` `deny`，其 `skills/` `read`；工作区 `.cosir` `read` | 仍生效 | 系统 `.cosir` 有 `.env`（模型 Key）、业务库、日志（布局事实源 `app/utils/path/system_cosir.py`）；工作区 `.cosir` 有 workspace 级 MCP 配置，而 MCP stdio 服务不进沙箱，能写它就能在沙箱外执行任意代码 |
| 下次自动执行 | `.git/hooks`、`.git/config`（`.git` 为指针文件时按 `gitdir:` 保护其实际目录）；`.gitconfig`、`.gitmodules`；shell rc（`.bashrc` `.bash_profile` `.zshrc` `.zprofile` `.profile`）；`.envrc`；`.vscode`、`.idea`——均为 `read` | 解除 | 用户无沙箱的 git、shell、编辑器会执行它们；照 sandbox-runtime 强制清单（`src/sandbox/sandbox-utils.ts:21-50`），加 `.envrc`（direnv）；gitdir 解析照 Codex（`protocol/src/permissions.rs:2376-2419`） |

- **`.git` 本身可写**（提交、切分支照常），只锁 hooks 与 config。Codex 把整个 `.git` 设为只读，提交都要越权，对桌面编码 Agent 太频繁。代价：`git push -u`、`git remote add` 写 config，要提权；不放开，因为 config 能配 `core.hooksPath`、`core.fsmonitor`。
- **包管理器缓存默认可写**（`~/Library/Caches`、`~/.cache`、`~/.npm`）：不放开时 npm / pip / uv / cargo 首次拉依赖即 EPERM，正中「频繁失败」。代价是缓存可被投毒，写进档位说明；清单 S1 用真实项目校准。
- **默认不禁读 `~/.ssh`、`~/.aws`**：会让 `git push`、云命令在沙箱里失败；用户追加受保护路径随自定义档位做（§11）。
- 受保护条目**不要求路径已存在**（首次创建也拦），见 §8。
- 完全权限档不进沙箱，规则对命令不生效；文件工具的禁读照常（那是工具对 cosir 自身机密的规则）。

## 7. 配置

本期没有权限配置文件：三个内置档与受保护路径都定义在代码里（`app/core/permission/`）。档位规则里的路径符号只有 `:root`、`:workspace`（Task 的工作区范围根，§3.1）、`:tmp`、`:caches`，由后端展开成绝对路径，worker 只收绝对路径。自定义档位与 `permissions.json` 见 §11。

## 8. 平台落地（Rust，terminal-worker 内）

**策略快照 → 启动帧**：`StartExec` 增加必填 `sandbox` 字段（`apps/terminal-worker/src/protocol.rs:18-34` 现无此字段）：

```text
sandbox: "unrestricted"
       | { fs: [{ path, access: "read" | "write" | "deny" }], network: bool, blocked_local_ports: [u16] }
```

后端只给展开后的规则；平台规则在 Rust 侧生成（`sandbox/` 按 `cfg` 每平台一个实现，不设 trait）。用户终端的 `StartSession` 没有这个字段，「人的终端不进沙箱」由类型保证。worker 自身不进沙箱，只在起命令时改写 argv（同 Codex `SandboxManager::transform`）；`sandbox-exec` 原地 exec 目标程序，pid 与进程组不变，`containment` 不用改。

**后端端口一律屏蔽**：`blocked_local_ports` 由后端填自己的监听端口，网络开关都生效。后端 API 无认证（AGENTS 待办「本机 API」），不屏蔽时命令能 POST `/tool-calls/{id}/decision` 批准自己的提权；Codex 为同类原因屏蔽 app-server socket（`sandboxing/src/seatbelt.rs:1004-1010`）。

- **macOS**：生成 SBPL，调用固定路径 `/usr/bin/sandbox-exec`（防 PATH 劫持）。路径作为 `-D` 参数传入、不拼进策略字符串；`(deny default)` 起手，子进程继承同一沙箱。规则按路径深度由浅到深输出（SBPL 后者覆盖前者），即「越具体越优先」的唯一实现处。必须补齐的绕过点（Codex `seatbelt.rs:400-470`、`:1013-1029`）：
  - 受保护条目同时写 `literal` 与 `subpath`，路径不存在也生效（拦 `mkdir .cosir`）；
  - 受保护条目的每一级祖先目录拒绝 `file-write-unlink`，放在策略最后（拦 `mv .git x` 再改；附带 `.git` 删不掉）；
  - 拒绝 `system-fcntl 80/110`（经只读描述符改写文件）；
  - 可写根先取真实路径（`/tmp` → `/private/tmp`、`/var` → `/private/var`），否则规则匹配不上。
  - 基础策略以 Codex `seatbelt_base_policy.sbpl` 为起点、删去 PTY 段（执行模式无 PTY），保留其 Apache-2.0 NOTICE；不引用其 crate（绑死 `codex-protocol`）。
- **Windows**（P4）：Codex 的非提权受限令牌**做不到禁读**（`windows-sandbox-rs/src/lib.rs:745-753`），守不住 `.env`，断网只靠环境变量；专用用户与 MXC 容器要管理员 setup。P4 前 Windows 按「沙箱不可用」处理，届时再评估。
- **Linux**：不是桌面目标，不做。

**沙箱不可用**（Windows 未就绪、`sandbox-exec` 缺失、包装失败）：沙箱档下每条 `execute_terminal` 都按「要决定」处理，审批卡写明「此平台沙箱不可用，这条命令将不受限制地运行」。不裸跑，也不让终端整体不可用。子 Agent 的每条命令也冒泡给用户（§5.4），P4 前在 Windows 上体验差但可用。

## 9. 被拦之后

- **判断**：命令非零退出（2 / 126 / 127 除外，照 Codex `sandboxing/src/denial.rs` 的快速排除）且输出含 `Operation not permitted` / `Read-only file system` / 网络关闭时的连接失败特征 → 观察里附一句「可能被沙箱拦截（当前档位：…）；确需沙箱外能力，用 `sandbox: "escalated"` 重新调用并说明理由」；卡片短提示 `status_hint`「沙箱拦截」。
- **只提示、不自动重跑**：Codex 也已取消 `on-failure` 的独立语义，「被拦 → 去沙箱重跑」只剩模型显式 `escalated` 一条路。
- 不做 macOS `log stream` 违规监听与按命令归因：要常驻监听进程并解析系统日志，sandbox-runtime 也默认关闭（`enableLogMonitor`）。启发式误判的代价只是一句多余提示。

## 10. 提示词

`core/context/system_prompt_builder.py:168` 的「Shell commands are not sandboxed」改为**不随档位变化的规则**：命令在沙箱里跑、档位可能在对话中变化且以最近一条档位通知为准、受保护路径、越界怎么申请（`sandbox: "escalated"` + 理由）。当前档位、可写范围、网络开关与「此平台沙箱不可用，命令每条都需要用户批准」都走档位通知（§3），不写进系统提示词——提示词在 `begin_run` 时重建，档位按回合切换，写进去会过时，每次改写还破坏提示词缓存。

## 11. 待做：自动审查与自定义档位

11-13 定：档位只有三个内置档，下面两项不排期，出现真实需求再做。做时各自引入一个新维度（§2），已有规则模型与审批链路不用改。

**自动审查（智能审查）**：引入「谁来批」维度。

- 位置：门禁「要决定」后，审批人是模型时把请求交给审查器而不是挂等待占位；子 Agent 的冒泡审批（§5.4）同样可由它作答。照 Codex，审查依据是根 Task 里用户的原话，不是上级 Agent 的转述（`core/src/agent/control/user_authorization.rs:256-318`）。
- 审查器：只读、无工具的单次模型调用；输入是动作、目标、理由与最近对话摘要；输出严格 JSON `{risk_level, user_authorization, outcome: allow|deny, rationale}`（照 Codex `ext/guardian-reviewer/src/assessment.rs:13-24`）。
- **失败处理（fail-closed）**：超时、解析失败一律拒绝；有用户界面的 Task 是否回落给用户届时再定（Codex 不回落，因其「替我批」档默认用户不在场）。熔断：同一回合连续拒绝 3 次中断（Codex `circuit_breaker.rs:3-7`）。不做「记住」。
- 结论与理由落日志、在审批卡位置回看。它看到的命令是模型写的——**只防失误，不防注入**，写进档位说明。

**自定义档位**：引入「越界时拒绝」，适合无人值守。

- 系统级 `<DATA_DIR>/.cosir/config/permissions.json`，pydantic strict + `extra="forbid"`，原子写，由 `app/service/configuration/permission_configuration_service.py` 现读现写（照 `models.json` / MCP 配置样板）；内容是同结构的 `filesystem` 规则、`network`、`on_out_of_bounds`，以及用户追加的受保护路径。
- 自定义档是**完整声明**，设置页「基于某档位新建」时复制一份再改；不用 Codex 的 `extends` 继承链（受保护路径全局生效，继承无收益）。
- **坏配置不回退默认**（照 sandbox-runtime `README.md:177-185`）：自定义档与追加的受保护路径不可用，设置页显示原因，内置档照常；选了不可用档的 Run 启动即失败。
- 严宽比较：自定义档与内置档不可比时取「仅可查看」（§3）。
- workspace 级配置仍不做（随仓库共享的配置只能收紧，否则是提权路径）。

## 12. 日志

`permission_policy_resolved`（`task_id` `run_id` `turn` `mode` `source`：用户输入 / 派生 / 沿用 / 上级收紧，`scope_root`）、`task_permission_derived`（`task_id` `deriver_id` `mode` `workspace_scope`；越界拒绝另记 `requested`）、`permission_decision_required` / `permission_decided`（`tool_call_id` `target` `scope` `outcome`）、`permission_denied`（用户驳回，带发起的 Task）、`sandbox_wrap_failed`（平台、原因）、`sandbox_denial_suspected`（`tool_call_id`、命中特征）。命令只记 `command_preview[:200]`。

## 13. 刻意不做

- **网络域名白名单 / 本机代理 / MITM**：要做就和浏览器权限一起做，否则防不住外传（浏览器与 `web_extract` 不经终端）。做时还要处理本机 API 无认证（AGENTS 待办「本机 API」）。
- **浏览器、MCP 服务进程的沙箱化**：档位只在门禁层粗管 MCP 调用；浏览器不在档位内。
- **网络开时的提权通道**：docker socket 等本机服务等同 root，网络开时不拦；本期只防失误，写进档位说明。
- **永久规则、命令前缀规则、安全命令白名单与危险命令黑名单**（Codex execpolicy / `prefix_rule` / `is_dangerous_command`）：永久放行只能改配置；Codex 自己已删掉安全命令白名单（`942af8447b`），其 allow 规则还等于免沙箱，粒度太粗。
- **按命令归因的违规监听**（§9）。
- **自动审查、自定义档位**：列为待做（§11）。
- **Linux**。
- **派生后修改下级的工作区范围**：范围创建时定死，「⊆ 上级」因此只需校验一次；要换范围就新派生一个。
- **切换档位时追溯运行中的命令**：沙箱在起进程时定死，收紧档位不停已在运行的命令；需要时用户自己停止（§3）。

## 14. 问题清单

| # | 问题 | 证据 | 层级 | 置信度 / 待验证 |
|---|---|---|---|---|
| Q1 | 读类工具与浏览器 upload 能读系统 `.cosir/.env` | `PathResolver.resolve_for_read` 只拦设备路径（`tools/tool_handler/security/path_resolver.py:227-241`） | 契约缺口 | 高 |
| Q2 | 工作区 containment 写死在 `PathResolver`，与「档位决定可写区」冲突 | 7 处 `resolve_within_workspace` 调用（§4） | 契约 | 高；`session_service.py:295` 的终端 cwd 校验随终端改造重看 |
| Q3 | Agent 消息不带档位，派生与 `send_message` 有借道提权 | §3 所列三处只透传模型与推理强度 | 设计缺口 | 高 |
| Q4 | 混批规则按工具静态声明判定 | `tool_batch_runner.py:54,109` | 结构 | 高；改为门禁纯函数 |
| Q5 | 命令能调用无认证的后端 API 批准自己的提权 | AGENTS 待办「本机 API」；`app/app.py` 无认证 | 设计缺口 | 高；§8 屏蔽后端端口只封终端通道，浏览器通道随浏览器权限另做 |
| Q6 | `sandbox-exec` 被 Apple 标为弃用 | Codex 与 srt 仍在用 | 外部风险 | 中；持续观察 |
| Q7 | Windows 受限令牌做不到禁读 | Codex `windows-sandbox-rs/src/lib.rs:745-753` | 平台 | 高；P4 前按不可用处理 |
| Q8 | 包管理器缓存清单是否够用 | npm / pip / uv / cargo / pnpm 缓存位置各异 | 取舍 | 中；S1 用真实项目校准 |
| Q9 | 命令自己调 `sandbox-exec` 时嵌套失败（如 `swift build` 的 manifest 沙箱） | Seatbelt 不允许嵌套 | 平台 | 中；提权，或提示词引导 `--disable-sandbox` |
| Q10 | 工作区范围之外的上级目录里有子 Agent 需要写的构建产物（如 monorepo 根的 `node_modules`、lockfile） | 包管理器在仓库根安装 | 取舍 | 中；子 Agent 越界走提权（S2），或派生时范围给到仓库根 |
| Q11 | 挂起中的 Run 不被用户的 `next_run` 输入唤醒，审批挂着时新消息干等 | `dispatch_policy.py:90-94`；等待占位 `suspend=True`（`tool_awaiting_decision.py:33`） | 设计缺口 | 高（现象）；当初「挂起时不唤醒」的理由待查，等后台结果的挂起是否一并放开待定（§5.5） |

## 15. 选定路线与被否方案

**选定**：一份规则模型、两个执行点（门禁管进程内工具，worker 内 OS 沙箱管命令进程）、审批挂在单次调用上并复用 HITL 重放链路、提权保留 cosir 信任根；Agent 没有常驻 shell（前置稿），人的终端永远无沙箱。

| 被否方案 | 否决理由 |
|---|---|
| 子 Agent 越界直接拒绝、报回上级（本稿 11-09 版） | 工作白做，模型易绕路后谎报成功；上级也无权批，最后仍要转给用户。成熟产品有人值守时都冒泡 |
| 权限审批在四处判定里各加「或」（本稿 11-09 版） | 特例散落；改为等待事项值对象（§5.2） |
| 提权即完全不进沙箱（本稿 11-09 版） | 一次批准就交出 `.env`；见 §5.1 |
| Agent 消息携带 min(发送方, 接收方) 档位、只收紧（本稿 11-13 版） | 汇报也是 Agent 消息，主 Task 会被只读子 Agent 降档；派生已有上级链兜底，平级借道属注入威胁 |
| 能力说明写进系统提示词 | 提示词按 Run 重建，档位按回合变；改写破坏缓存。改为档位通知 |
| 下发固定的五个列表（本稿 11-09 版） | 受保护路径、例外都要写特例；规则模型一种排序规则覆盖全部 |
| Windows 非提权方案用环境变量断网 | 不是安全边界；宁可如实「不可用、逐条审批」 |
| 「终端沙箱」作为独立能力（DeepSeek 稿） | 档位管的是 Agent 全部动作；只做终端会让「仅可查看」名不副实 |
| 「仅可查看」用 `disabled_tool_groups` 隐藏写工具 | 混淆用户的工具选择与权限；只读的语义是「改要先问」 |
| 独立提权工具 `request_permission_escalation` + 只对新终端生效的 grant | 三个来回；批准的不是那条命令；时序难懂 |
| 用户新建终端也进沙箱 | 拦住用户自己的操作 |
| 命令级包装（在共享 shell 里把 Agent 的每条命令包进沙箱） | 在 ssh、REPL、已激活环境里失效，复杂度高；终端改造后不再需要 |
| 沙箱不可用时让终端不可用 | 功能消失；退化为逐条审批更顺 |
| 结构化违规监听（`log stream`） | 常驻监听 + 日志解析，本期收益低 |

## 16. 分阶段

终端优先（11-13 定）：前置终端改造已实施，命令沙箱先行，只读档与文件工具门禁随后；自动审查与自定义档位不排期（§11）。

| 期 | 内容 | 验证 | 依赖 |
|---|---|---|---|
| S1 | 规则模型与受保护路径事实源；Rust `sandbox/`（macOS Seatbelt）；`StartExec.sandbox` 必填；输入带 `permission_mode`，先只有「工作区内修改」「完全权限」两档、随回合切换，输入框选择器（不随活动 Run 锁定）；派生 Task 继承上级档位、上级链取 min、档位通知（§3）；提示词按策略生成；被拦提示。越界的出路暂为回复里的命令块（前置稿 ③）或下个 Run 换档；Windows 维持现状、提示词如实写「未沙箱」 | Rust 在 macOS 上的集成测试：工作区外写失败、`mkdir <ws>/.cosir` 失败、`mv .git` 失败、读系统 `.cosir/.env` 失败、连后端端口失败、`npm i` 成功 | 无 |
| S2 | `sandbox: escalated` 与提权策略；门禁逐次判定「要决定」（Q4）；等待事项值对象（§5.2）、权限审批占位、通用审批卡、批准范围与 grant 登记（记档位、只属发起 Task）；子 Task 冒泡审批与上级可见的「在等用户批准」（§5.4）；审批有效期声明与新回合结算、新消息唤醒等审批的 Run（§5.5）；不可用平台（含 Windows）退化为逐条审批 | 后端单测：提权批准后工作区外写成功、`.env` 仍不可读；新消息使更早回合的审批作废、迟到决定被拒、交接不受影响、停止后仍可批；收紧档位后 grant 不放行；子 Agent 越界挂审批、`task_status` 报「在等用户批准」；E2E（无头）：审批卡批准与拒绝 | S1 |
| S3 | 派生参数 `permission_mode` / `workspace_scope`（§3.1，委派、Agent fork）；`TaskExtra.workspace_scope` 与 `ToolExecutionContext.scope_root`；沙箱 `:workspace`、写类文件工具边界、`execute_terminal` 缺省 `workdir` 改读范围根；子 Agent 提示词与委派 / 分叉行展示档位与范围 | 后端单测：超出上限的档位与范围被拒、缺省继承；范围外写失败（命令与 `write_file` 各一）；上级收紧后下级下一次调用按收紧判定 | S1 |
| P0 | 读类工具（含搜索、查找、列目录的遍历）与 upload 拒读系统 `.cosir`，消费 S1 的受保护路径 | 单测：`read_file` 读 `.env` 被拒、读 `skills/` 放行；从用户主目录 `search_content` 不返回 `.cosir` 内的命中 | S1 |
| P1 | 「仅可查看」档；门禁第四段（写类工具、MCP）；`PathResolver` 的工作区 containment 移交门禁 | 后端单测覆盖三档 × 写工作区内 / 外 | S2 |
| P4 | Windows 沙箱选型与实现 | 实机验证 | S2 |

## 17. 改动账本（粗估，实施前按阶段细化）

- **精简**：`PathResolver` 的工作区 containment 移交门禁（约 −40）；`system_prompt_builder` 的「not sandboxed」段落替换（约 −8）；`_requires_user_decision` 并入门禁判定（约 −10）。合计约 −60。
- **新增**：
  - 后端 `app/core/permission/`（规则模型与预设、策略快照、受保护路径、grant 登记，约 +450）；门禁第四段与工具访问声明（约 +200）；HITL 等待事项值对象、三处消费方改造与决定载荷校验（约 +120）；子 Task 冒泡审批与上级信号（约 +120）；审批有效期声明、新回合结算与挂起唤醒（约 +120）；`execute_terminal` 参数与沙箱字段、被拦判断（约 +160）；派生首条输入档位、上级链取 min、档位通知（约 +80）；派生参数、`workspace_scope` 校验与 `scope_root` 装配、提示词与展示（约 +180）。
  - Rust `apps/terminal-worker/src/sandbox/`（`mod.rs` 线上类型与 argv 改写约 +120、`seatbelt.rs` 规则编译约 +250、`seatbelt_base.sbpl` 约 +100、`protocol.rs` / `exec.rs` 约 +50、集成测试约 +200；Windows 另计）。
  - 前端：输入框档位选择器、通用审批卡（含工作台子 Task 内的渲染与来源标注，约 +500）。
- **净变化**：约 +2580（不含 Windows 与 §11 待做；其中 S1 + S2 约 +2030，S3 约 +200）；实施时按阶段以 `git diff --stat` 对账。
- **命名**：
  - `app/core/permission/`（新目录）——正向：找「权限、档位、受保护路径」能落到；反向：core 层的权限规则。放 core 是因为门禁在 `core/tool/execute/`，而 `core` 不得导入 `app.tools`。
  - `apps/terminal-worker/src/sandbox/`——worker 内「怎么起受限进程」。
  - `TaskExtra.workspace_scope` / `ToolExecutionContext.scope_root`——「工作区范围」：工作区内的一个目录，与 Workspace 记录区分；不叫 `work_dir`，因为它限定可写区而不只是起始目录。
  - 本文由 `terminal-sandbox-design.md` 改名：内容已是 Agent 权限而非终端。
- **同步**：AGENTS.md「工具 UI 渲染边界」（HITL 不再只由工具静态声明；「等待中的决定在 Run 停止、失败、完成或重启后仍可决定」改为按有效期声明，审批到下一回合、交接到交还）与「进程间接口边界」调度段（挂起 Run 的唤醒规则，Q11）、「代码目录边界」（新目录）、待办「安全与权限」（完成项勾掉；自动审查与自定义档位留在待办）；AGENTS.md「数据与事实所有权」与「前端架构」的运行期配置锁定：档位是输入级事实、不属发起配置，选择器不锁；`tool_ui_display_contract.md` 加 `permission-request`，`delegation-result` / `fork-task-result` 加档位与范围。

## 附录：参考实现借鉴清单（已抽查）

**Codex**（`codex-rs/`）

- 规则模型：`protocol/src/permissions.rs:108-136`（优先级）、`:256-268`（文件系统策略）、`:2376-2419`（受保护元数据与 gitdir）；容器 `protocol/src/models.rs:431-445`。
- 越权参数 `sandbox_permissions` + `justification`：`core/src/tools/handlers/shell_spec.rs:230-264`。
- 提权保留禁读：`core/src/tools/sandboxing.rs:290-302`；审批缓存键与范围：同文件 `:45-122`、`tools/runtimes/unified_exec.rs:89-97`。
- 进程结构：`sandboxing/src/manager.rs:375-636`（spawn 前改写 argv）。
- Seatbelt 策略：`sandboxing/src/seatbelt.rs`（`-D` 参数传路径、`:400-470` 符号链接、`:1004-1029` 屏蔽守护 socket / 祖先 unlink / fcntl）、`seatbelt_base_policy.sbpl`。
- 被拦判断：`sandboxing/src/denial.rs:13-72`。
- 自动审查（§11 待做）：`ext/guardian-reviewer/src/{assessment,completion,circuit_breaker}.rs`。
- 不搬：execpolicy（Starlark 规则）、危险命令黑名单、旧 `SandboxPolicy` 兼容层、exec-server / PathUri、网络代理、整个 `codex-sandboxing` crate。

**sandbox-runtime**

- 强制禁写清单：`src/sandbox/sandbox-utils.ts:21-50`；`.git/hooks` 与 `.git/config` 的处理：`src/sandbox/macos-sandbox-utils.ts:103-135`。
- 坏配置不回退默认（§11 待做）：`README.md:177-185`。
- 网络（以后）：域名判定顺序 deny → allow → strict → ask（`src/sandbox/sandbox-manager.ts:732-830`），局限见 `README.md:1265-1284`。
- 不搬：Node 常驻进程模型、TLS 终结与凭据掩码、Windows 独立账户方案。

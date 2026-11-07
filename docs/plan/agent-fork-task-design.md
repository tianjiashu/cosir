# 回合边界分叉：复用 context 与投影标志

2026-11-08 修订：切点从「输入之前」改为回合边界，用户要的是从回复往后分叉，并且最新回复也能继承。克隆 Run 仍然不做，只使用现有 context 表及字段，不增加历史表或归属列。

## 用户行为

| 操作 | 模型继承 | 对话展示 | 新任务执行 |
| --- | --- | --- | --- |
| 用户在回复下点「从这里分叉」 | 到这条回复为止的保留上下文 | 展示继承的聊天正文与工具记录 | 等待新输入 |
| Agent 调用 `fork_task` | 到上一条回复为止的保留上下文，不含当前回合 | 一行来源提示，再显示新任务说明和回复 | 按工具给出的完整任务说明启动 |

回合从一条输入（真人消息或 Agent 任务说明）或一个新 Run 开始，这与快照把行分组成消息的规则一致；唤醒开出的回复没有输入，也是独立的回合。继承行没有 Run，分支里它们并进上一条回复，切点跟着界面走。已结束的回复随时能分叉；进行中的回复没有入口，后端以 `FORK_REPLY_ACTIVE` 拒绝。

例如历史背景 A →「测试一下 fork task tool」B → 工具调用：子任务继承 A，既不继承 B，也不继承这次工具调用。它收到的新任务必须交代目标、约束、必要的新事实和交付物。边界阻止旧指令被重复继承，不保证模型不会根据新任务主动再次分工。

## 事实归属

- `conversation_task_contexts` 仍是正文唯一事实源。复制行 `run_id=None`，不创建历史 Run。固定系统提示词是 sequence 0，其后无本地 Run 的记录就是继承上下文，不新增历史标记。
- Agent fork 把复制行的 `project_to_transport` 置为 false；snapshot 直接遵守行的投影标志，不另设历史段投影策略。`include_in_context` 不变，模型仍能读取背景。
- 从 Agent fork 手动分叉时，根据已有角色与 `metadata.kind` 恢复展示：assistant/tool 可展示，user 只有 `user_input` / `agent_message` 可展示；内部 notice、decision、background_result、tool_result、tool_image 不显示为气泡。当前生产隐藏路径均属于这些内部 user 种类，不保存另一份原始可见性。
- 继承记录不可编辑，不能用“两标志皆 false”判定它已撤回：中断输出本来 include=false，Agent fork 隐藏后 project 也为 false，仍应保留供手动再分叉。只有本地消息按两标志判断编辑移出。
- 目标自有消息不依赖源 Task 或源 Run 存活。首次本地输入才建立新 Run。审批、后台等待、结果更新、重启结算、编辑重跑只处理有本地 `run_id` 的记录。
- 不复制来源 Run 的结局、总用量、耗时或执行错误摘要。逐消息正文、工具状态与展示内容继续复制；没有对应消息正文的 Run 摘要不额外保存。
- `extra.fork.source_task_id` 是不可变来源，`tree_parent_task_id` 是可变侧栏挂载关系。删除来源只重挂后者。
- 初始启动配置只冻结在 Task fork metadata；新输入沿用它，用户选择优先。在继承消息处再分叉时沿用当前分支的初始配置，不保存每段历史的原始模型配置。

## Task 类型

`models/task_kind.py` 的 `TaskType(StrEnum)` 是唯一类型定义：

| 类型 | 含义 |
| --- | --- |
| `user` | 用户创建的普通对话 |
| `user_fork` | 用户手动分叉 |
| `agent_fork` | Agent 分叉出的侧栏对话 |
| `agent_task` | 委派子 Agent，原 `delegate_task` 类型 |
| `team_node_task` | Team 节点，原 `agent_team_node` 类型 |

`TASK_KINDS` 能力表、派生规则、创建入口、TaskRecord 与响应 schema 使用枚举；数据库继续保存字符串，前端使用封闭联合类型。两种 fork 共同属于侧栏对话，但只有 Agent fork 计入 Agent 派生关系。删掉 `extra.fork.created_by`，snapshot 投影 `fork.taskType`；不保留旧类型别名。工具名 `delegate_task` 未改变。

`TaskRecord.extra` 由字典改为 `TaskExtra`，复用现有 JSON 列：

- `fork: TaskFork | None` 保存来源、切点、树父节点与初始配置，不存创建者或只写不读的工具调用 ID。`TaskForkLaunch` 复用 `ConversationRunOptions`，允许已失效的 Agent/模型身份为空。
- `team_node: TaskNodeConfig | None` 仅保存运行消费者需要的角色、系统提示、结构化输出和模型设置；Team 目标留在输入与 Team 状态，Agent/模型身份归 Run，工具能力归 Task schema。原来两个只写不读的 Team ID 键及整份 Team 运行快照中的闲置副本删除。
- 模型拒绝未知字段；持久化读取边界校验类型与附属数据对应关系，写入时显式 `model_dump(mode="json")`。业务代码按属性读取，冻结模型更新使用 `model_copy`。
- Task API 的 fork 只投影侧栏需要的 `tree_parent_task_id`，不下发初始配置或 Team 节点运行配置；来源与初始配置由对话 snapshot 投影。普通与委派 Task 没有这两类附属数据。

初始配置中的模型 ID 是历史引用，不能把快照存在误当成模型仍可用。已有 `prepare_agent_message → ModelConfigRecord.check_launch_config` 在入队前验证模型；用户输入准备也验证其本次选择。保留这一处边界，模型删除不手工同步 Task JSON、不在初始配置读取处重复验证。失效时明确拒绝且不入队，用户选新模型后可继续。

## 边界与工具协议

`POST /tasks/{id}/fork` 只接受 `{ "replyMessageId": "assistant-123" }`（或 `assistant-run-5`），wire id 在 API 层解码成 `ForkReply`。切点在写事务内计算。Agent 以自己的 `run_id + tool_call_id` 定位调用行，向前走到这一回合的起点。

两入口共用 `service/conversation_run/fork_history.py`。复制前按原 Run 身份执行现有工具闭合计划；缺失结果在目标中紧跟对应调用写入“此分叉未继承结果”的占位，不回捞切点后的结果；占位在切点前、结论在切点后的调用同样改写成未继承，否则卡片永远转圈、模型以为工作还在进行。孤儿或重复结果使事务回滚。闭合完成后副本统一不归属 Run；已闭合的同号调用可顺序复用，模型 SDK 消息里的 ID 和内部结果标签保持原样。

投影按 assistant 序号与调用 ID 保存每次调用，避免同号工具的早期结果被后来结果覆盖。只为继承工具生成序号命名的 UI ID，不改模型消息。上下文观测也按最近的同号调用关联结果。

## 展示与验收

snapshot 的 Task 级 `fork` 提供来源提示与初始配置，不伪装成消息、不占分页位置、不进入模型。继承消息只有现有 `runId:null`，不新增消息级 history 协议。继承与本地消息共用 `Turn`、`projectTurn`、`CallRow`、`CALL_VIEWS`、折叠状态与虚拟估高；由 `runId:null` 推导的展示入参只在工具能力边界禁止活订阅和执行操作。删除独立的 `InheritedTurn` 和 `HISTORY_VIEWS`。文件链接、图片、截图、diff、终端输出阅读及错误提示沿用同一展示契约；配置提案不推断来源草稿是否保存。继承上下文保留单条用量，不计算已丢失 Run 边界的缓存率差分。

真实桌面用例位于 `apps/desktop/tests/desktop-e2e/desktop-smoke.spec.ts` 的 `fork context`。启动隔离 Electron、真实 FastAPI、SQLite、actor、工具和 OpenAI SDK；仅模型返回由 localhost 测试服务控制。捕获实际模型请求验证边界，再点击真实分叉按钮验证展示、重启、删源后继续对话及初始配置。详情见 [fork-context-acceptance.md](fork-context-acceptance.md)。

本方案不新增数据库表或 context 字段，但五种类型与实体化 extra 改变了持久化值及 JSON 契约。按绿地约定，不兼容旧 Task 类型、旧分叉元数据及旧 fork 请求；旧运行数据需要另行处理，不能把“表结构未变”理解为数据兼容。验收使用隔离数据，不清理用户现有数据库。

不处理上下文压缩、准确历史时刻回放、来源资源跨工作区迁移、派生深度策略调整。

Agent fork 不能再分叉：它把继承的第一人称历史当成自己的待办时，嵌套只会放大混淆，所以任务说明前有一条交接通知写清身份与任务归属。工具列表是请求前缀的一部分，分叉原样沿用，`fork_task` 在运行期准入处拒绝。

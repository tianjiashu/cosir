# Agent Team 工具化流水线方案

> 状态：终版设计与实现依据。代码已按本文契约进入开发；实现验收以本文和现有项目架构边界为准。
>
> 核心定位：Agent Team 不是独立聊天入口，而是主 Agent 可以调用的一组工具。主 Agent
> 负责提交共同目标和各节点的预设指令；Agent Team 负责生成可审阅方案、等待用户确认、按确认后的
> 流水线执行，并把 TeamResult 返回给主 Agent。

## 1. 目标与交互结论

本次方案新增数量如下：
类型	数量	说明
数据表	1 张	AgentTeamRunModel，记录一次 Team 执行
主工具	2 个	ProposeAgentTeamConfigurationTool、AgentTeamTool
子 Agent 专用工具	1 个	agent_team_node_status，提交节点 status + output
Team 配置类型	1 种	Team JSON 配置
配置作用域	2 种	system 或 workspace，二选一，不是两套配置

用户希望把多个 Agent 组织成可配置的流水线，例如：

~~~text
主 Agent
  │ 调用 agent_team
  ▼
方案预览：目标、节点、Agent、模型、工具、转移规则
  │
  ├─ 用户确认 ──► 后端确认入口启动 Team ──► 开发 ──► 审查 ──► 测试 ──► TeamResult
  │
  └─ 用户不满意 ──► 主 Agent 再次调用 agent_team ──► 新的方案预览
~~~

本方案的关键结论如下：

1. Agent Team 暴露两个工具：ProposeAgentTeamConfigurationTool 负责生成可复用配置候选，AgentTeamTool（agent_team）负责基于既有配置生成一次运行预览。
2. ProposeAgentTeamConfigurationTool 只在用户确认配置后写入 system/workspace JSON 文件；AgentTeamTool 只创建运行预览。两个工具都不直接创建 TeamRun、子 Agent Run 或执行节点。
3. 用户不满意时，主 Agent 重新调用对应工具创建新的配置候选或运行预览；未确认的 ToolResult 直接丢弃，不产生旧 Team 持久化记录。
4. 用户确认必须是独立、可验证的业务动作，不能只相信模型传入的 approved=true；确认由后端业务入口启动已生成运行预览的 Team。
5. 用户确认前不创建子 Agent Run；确认后由后端从冻结配置和本次目标创建一条 AgentTeamRunModel 记录，再创建入口 ConversationRun。
6. Team 启动后，主 Agent 逻辑上等待 Team 结果，但等待必须是异步 awaitable，不能阻塞 backend event loop，也不能在线程中用 time.sleep 轮询。
7. 用户确认前，主 Agent 的本次对话 Run 会以“等待 Team 确认”的可恢复 cancelled 状态挂起；用户确认后由 Team 终态协调器恢复同一主 Run，并把 TeamResult 注入其下一次模型输入。
8. Team 的聚合状态由 AgentTeamRunModel 保存，单个 Agent Run 状态继续由 ConversationRun.status 保存，不能用 Task.extra 或 ConversationRun.extra 拼出第二套隐式状态机。
9. Team 配置和两个工具的预览不进入数据库：配置确认后写入 system/workspace JSON 文件，运行预览只作为 ToolResult 返回并在后端进程内短暂保留待确认上下文。
10. 运行期只新增一张类似 ConversationRunModel 的 AgentTeamRunModel，记录 Team 状态、目标、配置快照、当前节点和聚合执行状态；节点实际执行继续复用现有 Task/ConversationRun。
11. 子 Agent 通过 agent_team_node_status 提交当前节点的 status 和 output 字符串；Team Engine 根据已确认的 TransitionDefinition 决定下一节点，不接受 Agent 直接指定 next_node_id。

### 1.1 终版能力边界

终版解决“主 Agent 发起一次 Team、用户确认后由可恢复的转移引擎执行完整流程”的闭环：

- 一个共同目标；
- 多个串行节点，以及同一节点根据 status 选择唯一下一节点的状态转移；
- 每个节点引用一个现有 Agent profile，并拥有节点级预设指令；
- Agent profile 决定模型和允许工具；
- 子 Agent 通过专用节点状态工具提交业务状态和任意 output 字符串；
- 每个节点可以定义自己的业务状态集合；
- 转移规则根据节点 status 选择下一个节点，output 原样传递给后续节点；
- 支持串行状态转移和业务回路；不支持节点并行和 Join 汇聚；普通 Team 子 Agent 不再暴露
  Team 创建工具，因此不递归嵌套 Team；
- AgentTeamRunModel 的节点状态、前置输出和转移历史统一保存在其状态字段中，可持久化、恢复和审计；
- 前端可看到方案预览、节点进度、转移原因和最终结果。

终版仍然禁止由 Agent 直接指定下一个节点、绕过确认修改已冻结执行图，以及把公网队列、跨机器调度等基础设施问题混入本机 Agent Team 领域。

## 2. 当前后端调研

### 2.1 目录现状

当前实现位于 `apps/backend/app/agent_team/`，核心职责按配置、预览、协调和注册表拆分：

~~~text
configuration/       Team 配置、节点和转移定义
coordinator.py       单活动节点的执行推进和终态收敛
preview.py           待确认运行预览的进程内存储
preview_builder.py   profile、模型和工具快照物化
registry.py          system/workspace Team 配置加载
workflow.py          主 Agent 等待 Team 确认/终态的工作流边界
~~~

Team 运行 CRUD 位于 `apps/backend/app/storage/crud/agent_team_run_crud.py`，运行模型位于
`apps/backend/app/storage/model/agent_team_run_model.py`。Coordinator 不直接承担单表
持久化细节；它只负责跨 Task/Run/TeamRun 的状态流转。

### 2.2 可复用的 Agent 能力

apps/backend/app/core/agents/agent_profile.py 中的 AgentProfile 已经覆盖节点所需的能力配置：

- agent_id、role、description；
- system_prompt；
- allowed_tools；
- model_config_id、model_settings；
- workflow；
- max_steps；
- derive_for_run() 和 select_tools()。

AgentProfileRegistry 负责 system/workspace 作用域的 profile 解析、JSON 配置加载和并发读写保护。Team 节点只引用 profile，不重新定义完整 system prompt、工具白名单或模型连接字段。

配置装配继续复用：

- apps/backend/app/config/configuration.py：进程级 Agent 与工具装配；
- apps/backend/app/lifespan.py：启动、关闭和运行期依赖初始化；
- apps/backend/app/core/runtime/runner.py：profile 解析、per-run profile 派生和 workflow 执行。

### 2.3 可复用的 Task/Run 执行底座

现有 Task/Run 能够承载节点执行：

- TaskModel：workspace、父子任务关系、当前 Run 和工具定义；
- ConversationRunModel：Agent、模型、输入、输出、状态和终态原因；
- TaskService：Task 创建、子任务关系、任务树删除和 task 级并发协调；
- ConversationRunService：Run 创建、上下文初始化和 current run 维护；
- ConversationRunStateService：pending/running/completed/failed/cancelled 的条件状态迁移；
- ConversationRunExecutor：当前 backend event loop 中的异步启动、登记、取消和兜底收敛；
- AgentRuntime：真正调用 Agent workflow。

这些能力不能单独表达 Team 的共同目标、当前节点、状态转移边、节点结果和 Team 终态，因此需要在 app/agent_team 增加 Team 领域聚合和持久化适配层，而不是继续往 Task.extra、ConversationRun.extra 中堆字段。

### 2.4 Child Agent 工具的复用边界

现有 app/core/tools/tool_handler/child_task/ 中的 delegate_task、child_agent_status、child_agent_wait、child_agent_send 适合临时委派自由文本任务，但不应作为 Team 编排入口：

- 没有节点输入协议、节点 status 定义和状态转移契约；
- 所有权模型围绕父 Task/父 Run，不是 Team Run/Node Run；
- child_agent_wait 目前在线程 handler 中轮询，不适合作为 Team 内部等待机制；
- Team 下一节点必须由引擎根据节点 status 和状态转移决定，不能交给主 Agent 再自然语言路由一次。

Team 可以复用它们底层依赖的 profile、Task/Run、executor、AgentRuntime、工具门禁和取消机制，但应使用新的 Team coordinator，不直接调用 child tool handler 作为 orchestration API。

### 2.5 事实所有权

项目现有边界继续适用：

~~~text
AgentTeamRunModel.status   Team 编排聚合状态
ConversationRun.status     某个 Agent 执行状态唯一事实源
AgentTeamRunModel.state_json 节点游标、前置输出和转移历史
Transport snapshot         面向前端的只读投影
LangGraph checkpoint       workflow 恢复数据，不代表 Run 生命周期
~~~

RuntimeContextManager 继续负责 Agent 上下文；Team 向节点提供共同目标、节点预设指令和前置节点的 output 字符串。Team 不复制完整对话上下文，也不把 Transport snapshot 当作内部业务事实源。

## 3. Agent Team 工具契约

主 Agent 使用两个工具，但两个工具职责不同：

1. ProposeAgentTeamConfigurationTool：创建和审阅可复用的 Team 配置，用户确认后写入 system/workspace JSON 文件；
2. AgentTeamTool：读取已存在的 Team 配置，结合本次 goal 和节点预设指令生成运行预览；用户确认后由后端确认入口创建并启动一次 TeamRun。

两个工具的预览都只是 ToolResult，不写入数据库。配置确认后才写入 Team 配置文件；运行预览确认后才创建 AgentTeamRunModel。

### 3.1 ProposeAgentTeamConfigurationTool

该工具接收用户想要的团队协作方式，生成 Team 配置候选和预览。配置候选至少包含：

~~~text
team_id
name / description
nodes
transitions
~~~

每个节点必须声明 `node_type`：`start` 表示唯一启动节点，`middle` 表示普通流转节点，
`end` 表示执行并提交结果后结束 Team。配置不再单独保存 `entry_node_id`，入口由唯一
的 `start` 节点推导；配置也不再保存终止转移，转移统一指向下一个节点，目标节点为
`end` 时由引擎在该节点完成后结束 Team。

提案工具不接收 `scope`。用户在通用配置保存界面选择 `system` 或 `workspace`，保存请求
再把该作用域写入 `AgentTeamConfiguration`；配置模型仍然保留 `scope` 字段，内部未指定
时使用 `workspace` 默认值。

用户确认后，系统把新的配置写入对应的 system/workspace JSON 配置目录，并由 AgentTeamConfigurationService 加载。用户不满意时，主 Agent 重新调用该工具生成新的候选配置；工具不修改已有配置文件。若候选 team_id 已存在，必须拒绝覆盖或生成新的 team_id。

提案参数使用严格 schema：Team、节点和 Agent 标识只允许字母、数字、下划线和连字符；
业务 status 使用小写 snake_case，例如 `pass`、`fail`、`needs_changes`；`node_type`
只能是 `start`、`middle` 或 `end`。工具 schema
为关键字段提供最小英文示例，节点之间的引用关系、唯一 start 节点、end 节点可达性和
状态转移唯一性仍由 `AgentTeamConfiguration` 做跨字段校验。

### 3.2 AgentTeamTool

工具名建议为 agent_team，由一个 ToolDefinition 暴露统一参数。工具 handler 只负责参数校验和领域服务适配，不负责图遍历、持久化事务或节点执行细节。

Team 执行节点还需要向子 Agent 注入一个受作用域保护的专用工具：agent_team_node_status。它不是普通业务工具，也不是让 Agent 任意修改 ConversationRun 生命周期状态，而是“提交当前节点结果并请求流程继续”的终止工具。

~~~json
{
  "team_id": "code_quality_team",
  "goal": "实现并验证用户指定的功能",
  "instructions": {
    "develop": "实现功能，补充必要测试，并返回变更摘要",
    "review": "检查开发结果，明确返回通过或需要修改",
    "test": "执行验证并返回测试结论"
  }
}
~~~

说明：

- agent_id 必须解析到当前 workspace 可用的 Agent profile；
- 节点的模型、工具、status 集合和 transitions 来自 team_id 对应的配置文件；
- instructions 是本次运行的节点预设指令，不替代 profile 的 system_prompt；
- 运行时配置不允许覆盖配置文件中的 Agent、模型、工具和转移规则。

### 3.3 子 Agent 的节点状态工具

每个 Team 子 Agent 在执行期间自动获得当前 ConversationRun 专属的 agent_team_node_status 工具。工具参数由当前节点的定义约束：

~~~json
{
  "status": "needs_changes",
  "output": "发现接口参数校验缺失，需要返回开发节点补充。"
}
~~~

节点定义声明允许的 status：

~~~text
开发节点：done / blocked
审查节点：passed / needs_changes / rejected
测试节点：passed / failed / flaky / blocked
~~~

工具调用成功后，Team Engine 必须原子地完成以下动作：

1. 校验调用者确实属于当前 AgentTeamRunModel 和当前 ConversationRun；
2. 校验 status 属于当前节点允许的状态集合；
3. 校验 output 是合法字符串并满足长度、脱敏和持久化限制；
4. 保存当前节点结果并收敛当前 ConversationRun；
5. 根据 TransitionDefinition 计算唯一的下一个节点；
6. 把转移记录追加到 AgentTeamRunModel.state_json；
7. 创建下一批 ConversationRun；
8. 终止当前子 Agent，避免继续提交第二个结果。

同一个 ConversationRun 的第一次有效提交生效，后续调用返回 node_already_completed，不得覆盖原结果。子 Agent 自然结束但没有调用该工具时，必须产生明确的 implicit_completion_missing 失败，不得从最后一段自然语言中猜测节点状态。

agent_team_node_status 只能提交业务 status 和 output，不能设置 pending、running、cancelled 等系统生命周期状态，也不能传入 next_node_id。下一个节点完全由引擎根据已确认的转移规则决定。

### 3.4 AgentTeamTool 创建运行预览

agent_team 只有创建语义。主 Agent 每次调用它提交 team_id、共同 goal 和本次节点 instructions，服务端完成：

1. 读取并校验 team_id 对应的 system/workspace 配置文件；
2. 校验节点类型、transitions、唯一 start 节点和 end 节点可达性；
3. 解析每个 agent_id；
4. 展示解析后的有效模型、工具集合、profile 角色和限制；
5. 将 goal、节点 instructions 和候选配置封装为待确认 TeamRun，并按既有
   parent_task_id + parent_run_id 关联到主 Agent Run；
6. 返回只用于 UI 展示的结构化 display_data，其中包含可供用户编辑后回传的配置。

创建工具会创建一条 ``awaiting_confirmation`` 状态的 AgentTeamRunModel，但不创建节点
Task/ConversationRun，也不执行节点。预览只经统一 ToolMessage 流程写入
ConversationTaskContextModel 的 ``display_data``，不复制到 AgentTeamRunModel；同一主 Agent
Run 再次创建预览时，旧的待确认 TeamRun 被替换。

用户不满意时，不修改已有配置或执行记录；主 Agent 重新调用 agent_team 创建新的 Team 预览。

### 3.5 用户确认和执行

用户确认不是 agent_team 工具参数，也不是主 Agent 可以自行决定的模型参数。确认由后端
业务入口或前端确认命令完成。前端提交 parent_task_id、parent_run_id、team_id 和用户
最终编辑后的配置；后端不信任 display_data 或前端指纹，而是重新校验配置、解析运行时
快照并计算最终 preview_fingerprint：

~~~text
主 Agent Run
  └─ agent_team(team_id, goal, instructions)
       └─ 返回预览 display_data，并创建 awaiting_confirmation TeamRun

用户确认
  └─ 后端确认入口按 parent_task_id + parent_run_id + team_id 读取待确认 TeamRun
       └─ 重新校验最终配置并覆盖执行快照，迁移为 pending
            └─ 创建入口 ConversationRun，交给 Coordinator 异步启动 Team
~~~

确认入口允许用户提交修改后的节点图，但不能绕过后端配置和运行时资源校验。确认后的
配置快照和节点运行快照写入同一条 AgentTeamRunModel，后续 Coordinator 只消费这些冻结
事实。用户拒绝或重新提出要求时，旧 TeamRun 被标记为 cancelled。

取消不属于 agent_team 创建工具；使用现有明确的 Team/Run 取消入口，取消沿 AgentTeamRunModel → ConversationRun 传播。SSE 断开只表示订阅中断，不自动取消 Team。

## 4. 方案预览

预览是产品交互中的一等结果，不是日志，也不是把原始工具参数直接展示给用户。后端应返回结构化、脱敏、可重建的预览内容，至少包含：

~~~text
team_id
goal
start_node
nodes:
  node_id
  name
  agent_id
  node_type: start / middle / end
  role
  instruction_summary
  effective_model_name
  effective_tools
  max_steps
edges
configuration
requires_confirmation: true
~~~

预览必须明确显示：

- 实际会调用哪些 Agent；
- 每个 Agent 实际使用哪个模型和哪些工具；
- 节点顺序、状态转移和回路；
- 审查不通过时是否返回开发节点；
- 可能导致失败的配置警告。

预览不得包含 system prompt 原文中的敏感信息、Token、完整上下文、大段模型输出、堆栈或未脱敏路径。

推荐通过 ToolObservation.display_data 投影一个稳定的 kind: agent-team-preview。展示数据只
用于 UI，不能作为后端执行事实；确认接口接收前端最终配置后重新校验，AgentTeamRunModel
只保存确认后的执行快照。展示数据不需要携带 TeamRun 的 ``id`` 或 preview_fingerprint。

## 5. 领域模型与状态机

### 5.1 AgentTeamConfiguration

Team 配置是类似 Agent profile 的可复用静态资产，不进入数据库。配置按 system/workspace 作用域保存为 JSON 文件，由 AgentTeamConfigurationService 加载和解析。

~~~text
system_agent_team_config_dir()
workspace_agent_team_config_dir(workspace.root_path)
~~~

配置包含：

~~~text
team_id
name
description
nodes                    Agent 引用和允许的 status
transitions              status 到目标节点的转移规则
~~~

ProposeAgentTeamConfigurationTool 负责根据用户目标生成配置预览。用户确认后才写入 system 或 workspace 配置文件；用户不满意时重新生成，不修改已存在的配置文件。

### 5.2 AgentTeamRunModel

运行时只新增一张类似 ConversationRunModel 的 AgentTeamRunModel。它记录一次已确认的 Team 执行，不保存工具预览本身，也不为配置预览、节点编排记录、转移决策或命令单独建表。

建议字段语义如下：

~~~text
team_run_id
team_id
workspace_id
parent_task_id
parent_run_id
goal_input
node_instructions_json       本次运行的节点预设指令
configuration_snapshot_json   确认执行时使用的 Team 配置快照
status                       pending / running / completed / failed / cancelled
current_node_id
current_node_status
current_node_output
state_json                   当前节点、previous_outputs 和转移历史
generation                   并发与迟到回调隔离栅栏
failure_kind
failure_message              受控短消息
started_at / ended_at
~~~

state_json 是 Team 聚合状态的持久化容器，可以包含：

~~~text
completed_nodes
node_conversation_refs       node_id → ConversationTask/ConversationRun
previous_outputs
transition_history
current_node_id              同时最多只有一个活动节点
~~~

这些内容属于同一 AgentTeamRunModel 的状态，不拆成 Team 专用子表。配置确认前的预览和用户反馈不写入此表；确认入口从指定主 Agent Run 的待确认上下文读取执行描述后，才创建 AgentTeamRunModel。

### 5.3 节点执行与现有 Run 的关系

Team 节点不创建独立的节点记录表。每个节点实际执行时继续复用现有 TaskModel、ConversationRunModel、ConversationRunService、ConversationRunStateService、ConversationRunExecutor 和 AgentRuntime。

AgentTeamRunModel.state_json 保存 node_id 与 ConversationTask/ConversationRun 的关联，以及节点的 status 和 output。ConversationRun.status 仍是单个 Agent 执行生命周期的唯一事实源；AgentTeamRunModel.status 是 Team 聚合生命周期的唯一事实源。

节点状态工具提交结果后，Team Engine 在一个 SQLite 写事务中同时更新 AgentTeamRunModel
的当前节点、previous_outputs、转移历史和下一个节点引用，并创建下一批 ConversationRun；
事务提交后才发布节点事件和启动执行器。

### 5.4 Team 状态迁移

TeamRun：

~~~text
确认后创建 ──启动成功──► running ──达到配置声明的终止条件──► completed
     │                         │
     └─启动失败─────────────────┴────────────────────► failed
                               │
                               └─明确取消或重启收敛────► cancelled
~~~

工具预览不是 TeamRun 状态。用户不满意时重新创建配置或重新发起 AgentTeamTool 调用，不会修改已有 AgentTeamRunModel。后端进程重启时，未确认的运行预览随进程内上下文一起丢弃。

## 6. 图校验、节点输入与状态契约

### 6.1 图校验

创建阶段必须拒绝：

- 空目标、重复 node_id、未知 edge 节点、多个或缺少 start/end 节点；
- start 节点不可达、没有 end 节点可达或存在无法结束的路径；
- end 节点存在出边；start 节点可以被业务回路重新进入，但仍只有一个 start 节点；
- 同一节点和 status 存在多个转移边；
- 非 end 节点声明的每个 status 都必须有对应转移；
- profile 不存在、不是允许作为子 Agent 的类型，或 profile 工具集合无法物化；
- 节点预设指令、共同目标或前置节点输出契约缺失；
- 超出 Agent profile 的步数限制。

转移只根据声明的节点和 status 匹配，不执行表达式，也不能让 Agent 返回下一个节点 ID 直接改变图。

### 6.2 节点输入协议

节点输入统一由三部分组成，不再配置字段级输入映射：

~~~json
{
  "goal": "Team 的共同目标",
  "instruction": "当前节点的预设指令",
  "previous_outputs": [
    {
      "node_id": "develop",
      "status": "done",
      "output": "已完成基础实现。"
    }
  ]
}
~~~

输入规则固定如下：

- goal 由 Team Engine 自动注入，所有节点共享同一个共同目标；
- instruction 来自当前节点的预设指令，不替代 Agent profile 的 system_prompt；
- previous_outputs 是当前节点直接前置节点提交的 NodeResult 集合，每项包含 node_id、status 和 output；
- 入口节点的 previous_outputs 为空；
- 业务回路重新进入节点时，previous_outputs 包含触发回路的最近一次节点结果；
- 节点只接收前置节点的 status 和 output 字符串，不复制完整对话历史。

Team Engine 负责组装上述输入，子 Agent 不需要声明字段级映射，也不能通过输入协议访问未执行节点的数据。输出过大时应返回摘要或受控引用；输入构造失败属于明确的 node_input_unavailable，不得静默填空。

### 6.3 节点业务状态和结果

每个节点必须声明自己的 status 集合、每个 status 的含义和允许的转移。status 是节点的业务结果，不是 ConversationRun 的技术生命周期状态；output 是不由 Team Engine 解析的任意字符串。

~~~json
{
  "status": "needs_changes",
  "output": "发现两处问题，需要返回开发节点处理。"
}
~~~

节点执行事实与业务结果分开保存：

~~~text
ConversationRun.status        pending / running / completed / failed / cancelled
AgentTeamRunModel.current_node_status  当前节点定义的业务状态
AgentTeamRunModel.current_node_output  任意字符串
AgentTeamRunModel.failure_kind          Team 技术失败、取消或提交错误分类
~~~

正常执行完成但业务上需要返工时，应保存 ConversationRun.status=completed、current_node_status=needs_changes，而不是把 needs_changes 当成技术失败。技术失败则由 ConversationRun.status 和 AgentTeamRunModel.failure_kind 驱动错误处理或 Team 失败路径。

agent_team_node_status 是唯一正式的节点结果提交入口。调用失败、status 不允许、output 不是合法字符串或 Agent 未提交结果时，必须产生可审计的失败分类；不能从 ConversationRun.final_output 的自然语言中模糊推断 status。

### 6.4 转移选择

转移规则只读取当前节点提交的 status；output 原样写入 AgentTeamRunModel.state_json，供后续节点阅读：

~~~text
source_node_id
status
target_node_id
~~~

每条转移都必须有一个 `target_node_id`。`target_node_id` 指向 `end` 节点时，当前节点
之后仍会执行该 end 节点；end 节点提交结果后 Team 完成。end 节点不配置出边，因此
不需要 `transition_kind` 表达终止动作。

节点技术失败直接收敛为 Team failed；业务回路则由正常的状态转移显式返回已经完成的节点。两者都追加到 AgentTeamRunModel.state_json，不能把技术失败伪造成业务 status。

同一 source node 和 status 只允许一条转移边；没有规则命中时必须进入明确的未匹配失败状态，不能静默结束。Team 始终只有一个活动节点，不能激活多个目标节点，也不存在 Join 状态。

### 6.5 业务回路

业务回路通过普通状态转移表达：

~~~text
review.needs_changes → develop
~~~

业务回路会创建新的目标 ConversationRun，并把触发回路的最近一次节点结果放入 previous_outputs；循环是否结束由配置图和实际业务状态决定。

## 7. 执行时序与主 Agent 等待

### 7.1 创建和预览阶段

~~~text
1. 主 Agent 调用 agent_team
2. TeamService 校验图、解析 profile、物化有效模型/工具并创建 Team 快照
3. 工具返回 team_id、preview 和 requires_confirmation=true
4. 主 Agent 将主 Run 挂起在 Agent Team 等待节点；用户界面展示预览，主 Run 不继续生成普通回复
~~~

此阶段不启动任何 Agent 子执行。用户满意则确认当前运行预览；用户不满意则主 Agent 重新调用 agent_team 创建新的运行预览，旧预览不再使用。

### 7.2 用户确认阶段

~~~text
1. 用户确认当前运行预览
2. 后端确认入口按 parent_task_id + parent_run_id 读取仍存在的待确认执行描述，并检查其未被替换或超时清理
3. 冻结执行描述中的 goal、节点 instructions 和 configuration snapshot
4. 同一 SQLite 写事务创建 AgentTeamRunModel、入口 Task 和入口 ConversationRun；事务提交后再发布节点事件并启动 executor
~~~

确认入口不接受新的节点图、模型、工具或指令。用户的修改建议由主 Agent 转化为下一次新的 agent_team 创建调用。

### 7.3 执行阶段

~~~text
1. Coordinator 获取 AgentTeamRunModel 的推进权并启动入口 ConversationRun
2. 关联运行时以 asyncio awaitable 等待 Team terminal result
4. 子 Agent 调用 agent_team_node_status 提交 status 和 output
5. 引擎更新 AgentTeamRunModel.state_json，按 status 和转移策略选择下一条边
6. 创建唯一的下一节点 ConversationRun；如果没有命中转移则收敛为失败
7. Team 完成、失败或取消后返回 TeamResult
8. 协调器把 TeamResult 作为受限 SystemMessage 注入主 Agent 的待消费队列，恢复同一主 Run 继续生成最终回复
~~~

### 7.4 “主 Agent 阻塞”的准确含义

用户看到的语义是主 Agent 在 Team 未结束前不会继续回答；技术实现不能阻塞 backend event loop 或占用线程等待。工具运行层需要提供明确的异步 handler/awaitable 执行路径：

~~~text
主 Agent Run
    └─ Agent Team 预览 → cancelled + LangGraph interrupt
          └─ await TeamCoordinator.wait_until_terminal(team_run_id)
          ├─ Team 节点在 executor 中执行
          ├─ 节点事件唤醒 coordinator
          └─ Team 终态注入 TeamResult → resume 主 Agent Run
~~~

不得把 Team 等待实现为 time.sleep 轮询，也不得把 SSE 连接断开误认为业务取消。HTTP/SSE 重新连接只重新订阅；只有显式 cancel 或主 Run 的明确取消语义才会取消 Team。

Agent Team 不在工具 handler 内阻塞等待。预览工具返回后由 ReAct graph 的等待节点保存断点，Team coordinator 在独立的本地异步任务中等待终态并调用既有 resume 入口。

## 8. 模型、工具和 profile 解析

节点配置只提交 agent_id。goal 和节点 instruction 属于每次 Team 启动的运行时参数。有效运行配置按以下规则确定：

~~~text
AgentProfileRegistry.resolve(agent_id)
  → profile.system_prompt
  → profile.model_config_id / model_settings
  → profile.allowed_tools
  → Team goal、节点 instruction 和 previous_outputs
  → AgentTeamRunModel / ConversationRun 的执行快照
~~~

预览时就解析并展示有效模型、工具和自动注入的 agent_team_node_status；如果解析失败，Team 不得进入确认状态。执行时使用 Team 中冻结的有效快照，不因 workspace profile 被后来修改而改变已确认计划。

节点不能通过 agent_team_node_status 传入 model_config_id 或 allowed_tools 覆盖 profile。需要不同能力时，注册不同 profile，让 Team 节点引用不同 agent_id。这样保留现有 profile registry 的权限和工具门禁，也便于审计。

子 Agent 仍需要遵守现有禁用规则，至少不能继续创建 Team、委派 child agent 或使用不允许的交互终端能力，避免递归 Team 和权限边界失控。

## 9. 持久化、并发和生命周期

### 9.1 存储边界

Team 领域模型、状态迁移和用例编排放在 apps/backend/app/agent_team/；SQLite ORM model 和 CRUD
仍放在 apps/backend/app/storage/。Team Coordinator 通过 storage/crud 使用存储，不从 core
workflow 直接操作数据库。

当前实现的领域文件职责：

~~~text
app/agent_team/
├── __init__.py
├── definitions.py         节点、转移、节点输入协议和限制
├── registry.py            system/workspace Team JSON 配置加载与落盘
├── preview.py             进程内待确认预览的替换、消费和过期清理
├── preview_builder.py     profile、工具快照和前端预览构造
├── coordinator.py         Team 节点调度、转移匹配、等待和终态收敛
└── workflow.py             主 Agent 等待 Team 确认的 graph 节点

app/storage/crud/
└── agent_team_run_crud.py  agent_team_runs 单表 CRUD

apps/backend/app/storage/model/agent_team_run_model.py 只承载唯一新增表的 ORM 模型；
工具适配器和 API 分别位于现有的 core/tools/tool_handler 与 api 边界。
~~~

工具适配器建议放在 apps/backend/app/core/tools/tool_handler/agent_team.py，只做 ToolDefinition 输入输出转换；HTTP/API schema 仍放在 apps/backend/app/api/，不把 Assistant UI wire schema 泄漏进 app/agent_team。

### 9.2 并发锁和状态迁移保护

至少需要以下保护：

- 同一 TeamRun 只有一个调度循环持有推进权；
- 同一节点在 state_json 中最多只有一个活动 ConversationRun；
- Team 取消与节点完成之间使用条件更新和 generation 防止迟到回调覆盖新状态；
- AgentTeamRunModel 创建后，其 configuration_snapshot_json 不得改写。

这些锁是进程内协调和 AgentTeamRunModel 条件更新的组合，不应引入 Redis。确认入口只接受后端进程内仍存在、未被替换或清理的待确认执行描述；确认消费后立即清理该描述，后续确认请求不能再次创建运行。需要跨重启保持的 Team 事实进入同一张运行表；配置事实进入 system/workspace JSON 文件。

### 9.3 节点创建与状态迁移事务

Team 从一个节点转到下一个节点时，至少要保证：

1. 当前 ConversationRun 已以条件更新收敛；
2. 节点 status 和 output 已持久化；
3. 状态转移结果已确定；
4. AgentTeamRunModel.state_json 已追加转移历史和 previous_outputs；
5. 下一 ConversationRun 已创建或已明确记录为待启动；
6. 提交后再发布 Team/节点事件。

任何步骤失败都必须明确将 Team 收敛为 failed，不能留下“AgentTeamRunModel 认为在节点 A、数据库却已启动节点 B”的不可排查状态。

### 9.4 取消、重启和删除

- Team cancel 先设置可观察的取消 fence，再取消当前 Conversation Run；当前节点结束后由 coordinator 更新 AgentTeamRunModel。
- 节点工具、子进程和终端资源继续由现有 executor/tool execution 层负责超时、取消和进程树清理。
- 后端重启时，遗留 active AgentTeamRunModel 和 ConversationRun 都收敛为 cancelled，不自动重放旧 Team。
- app/lifespan.py 关闭时需要停止 Team coordinator、唤醒所有等待者并释放 TeamRun 锁。
- 删除父 Task 或 workspace 前，必须先处理仍活动的 Team；不能只删除 Task 行而遗留 coordinator 或子 Run。
- Team 事件发布失败不得改变业务状态，也不能阻断节点推进；失败需记录结构化日志。

## 10. Transport、日志和前端展示

### 10.1 ToolObservation 展示数据

沿用现有 ToolDisplayHints 和 ToolObservation.display_data 边界。工具只产生一个稳定 kind：

~~~text
agent-team-preview    方案预览、有效节点配置和确认提示
~~~

确认后的进度和终态不伪装成新的 ToolObservation，而是通过 AgentTeamRun 查询接口读取；
同一个前端 Team renderer 根据查询结果展示 progress/result。display_data 不承载 system
prompt、原始异常、Token、完整 Agent 对话或大段工具输出。未知 kind 继续走前端 fallback，
不应让消息流崩溃。

### 10.2 事件投影

TeamRun 表是 Team 状态唯一事实源。当前本地桌面实现不新增 Transport 事件类型；前端通过
``GET /agent-team/runs/{team_run_id}`` 或 wait 接口读取状态，断线后重新查询即可重建进度。
主 Agent 的最终 TeamResult 仍通过既有 Conversation context 注入并由现有 Transport 投影。

日志字段包含 team_id、team_run_id、node_id、conversation_run_id、status、trace_id 和受控
摘要；不得记录完整目标、完整指令、system prompt、密钥或大段模型正文。

## 11. 实施分阶段方案

本文只制定终版方案，后续实现按以下边界落地；不新增独立服务、队列或 Team 专用事件系统。

### 阶段 A：领域契约和 Team 创建预览

- 定义 Team 配置、节点、转移、结果和领域异常；
- 实现 profile 解析、图校验、节点输入协议静态校验；
- 实现 ProposeAgentTeamConfigurationTool 的配置候选预览，以及 AgentTeamTool 的运行预览；
- 不创建子 Agent Run，不进入执行。

验收重点：非法图、未知 profile、工具解析失败、循环预算和脱敏预览均有明确错误。

### 阶段 B：配置落盘、Team 持久化和确认/重新创建

- 接入配置候选确认，并将确认后的 Team 配置写入 system/workspace JSON 文件；
- 增加 AgentTeamRunModel 存储与条件状态迁移；
- 接入用户确认、拒绝、清理和重新创建运行预览；
- 确保确认只作用于仍存在于后端待确认上下文的运行预览；
- 不启动真实节点前先覆盖待确认上下文被替换、确认过期和并发消费。

验收重点：仅凭模型参数不能执行；已确认或已过期的 Team 不能再次启动；执行中的 Team 不可修改。

### 阶段 C：节点执行和异步等待

- 建立 AgentTeamRunModel，并接入现有 ConversationRun；
- 复用 Task/Run/AgentRuntime 执行节点；
- 注入 agent_team_node_status，并让一次有效提交终止当前子 Agent；
- 复用 ReAct graph 的等待节点和现有 ConversationRun resume 入口；
- 实现 Team terminal await、取消传播和重启收敛。

验收重点：主 Agent 逻辑等待但 event loop 不被阻塞；SSE 断开不取消；节点失败和取消可排查。

### 阶段 D：转移引擎和状态流转

- 实现 status、TransitionDefinition、state_json 和 previous_outputs 组装；
- 实现开发 → 审查 → 测试及审查回开发；
- 增加业务回路和迟到回调 fence；
- 统一 TeamResult 返回给主 Agent。

验收重点：只由节点 status 选边；output 原样传递；循环有上限；重复事件不会重复启动节点；最终结果可重连恢复。

### 阶段 E：Transport/UI 与可观测性

- 接入 preview 展示，并由同一 renderer 展示查询到的 progress/result；
- 增加 Team 查询、wait 和取消接口；
- 补齐日志、指标、失败分类和生命周期集成；
- 增加前端确认、重新创建和进度展示。

## 12. 测试与验收清单

### 领域单元测试

- Team 创建图校验、profile 解析和工具物化；
- 节点输入协议、共同目标、预设指令和 previous_outputs；
- 节点 status 定义、状态工具权限和重复提交；
- TransitionDefinition 优先级、无命中和循环上限；
- 节点结果字符串校验、失败分类和 state_json 转移记录；
- 单活动节点、状态转移和业务回路恢复；
- AgentTeamRunModel 状态迁移和待确认预览消费边界；
- Team 终态收敛和 generation 迟到回调隔离。

### 服务与存储测试

- Team 确认与重新创建并发互斥；
- AgentTeamRunModel、Task、ConversationRun 创建事务；
- 子 Task/Run 所有权和 workspace 隔离；
- 删除 Task、后端重启和 lifespan close 的清理。

### 运行时测试

- Agent profile 的模型和工具确实被节点使用；
- 子 Agent 只能提交当前节点允许的 status，不能指定 next_node_id；
- 节点完成后只启动命中的下一节点，并把声明的 output 放入下游 previous_outputs；
- 主 Agent await 不阻塞 event loop；
- Team cancel 能停止当前 Agent、工具子进程和终端；
- HTTP/SSE 断开后可以 attach，不会重复执行；
- 失败、启动失败和非法 JSON 都能返回结构化 TeamResult。

### 端到端场景

~~~text
开发通过 → 审查通过 → 测试通过 → Team completed
开发通过 → 审查要求修改 → 返回开发 → 审查通过
用户打回方案 → 重新调用 agent_team → 新 Team 预览 → 确认后执行
确认后再次确认 → Team 状态拒绝重复启动
主 Agent 执行期间显式取消 → Team 和当前节点 cancelled
后端重启 → 活动 Team cancelled，不自动重放
~~~

本次重写后的优先交互是：

~~~text
主 Agent 调用工具
  → 预览
  → 用户确认或重新创建
  → 确认后的精确方案执行
  → 主 Agent 异步等待
  → 返回结构化 TeamResult
~~~

实现入口位于 `apps/backend/app/agent_team/`，并复用现有 Task/ConversationRun/Executor、配置文件存储、ToolSystem 和前端工具 renderer。数据库仍只新增一张 `agent_team_runs` 表，不创建迁移文件；本地绿地环境通过现有 schema 初始化创建缺失表。

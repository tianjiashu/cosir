# 委派子Agent工具后端技术方案

> 本文是 `docs/委派子Agent工具计划方案.md` 的后端落地方案。它只定义高层技术路线与可执行 checklist，不替代后续代码实现细节。

## 目标

第一版在后端提供生产级 `delegate_task` 工具能力，使父 Agent 能按 `AgentProfile` 委派三类子 Agent：

- `delegate_reviewer`：只审查，不修改代码，不运行测试。
- `delegate_analyst`：只分析事实、代码和文档，不修改代码。
- `delegate_coder`：可编码，但必须受 child `AgentProfile.allowed_tools`、父子权限交集、深度与并发策略约束。

后端必须复用现有 Agent / Workflow / Runtime / ToolSystem / RuntimeEvent 体系。`Subagent` 不实现成平行系统，而是作为 child agent / child turn 复用现有运行底座。

## 当前代码事实

- Agent 执行入口在 `apps/backend/app/core/runtime/runner.py`，`AgentRuntime.run_turn()` 认领 pending turn 后按 `turn.agent_id` 解析 `AgentProfile` 并执行 `agent.workflow.run()`。
- `AgentProfile` 位于 `apps/backend/app/core/agents/agent_profile.py`，当前通过 `allowed_tools` 筛选模型可见工具。
- 工具契约事实源是 `apps/backend/app/tools/schemas/tool_definition.py`，执行链是 `ToolExecutionService -> ToolScheduler -> ToolExecutor -> handler`。
- `ToolScheduler.execute()` 当前强制要求 `ToolExecutionContext`，并以 `allowed_tool_names` 做 profile 级工具门禁。
- 运行事件事实源是后端 `EventType + payload model + runtime_event_payload_registry.py`，前端 `apps/shared/ts/events.ts` 由脚本生成，不能手工改。
- `/turns/{turn_id}/stream` 是 turn 级 SSE。`RuntimeEventBus` 按 `turn_id` 订阅，`RuntimeEventService` 负责持久化与发布。
- 历史回放已有 `GET /tasks/{task_id}/events` 与 `GET /tasks/{task_id}/turns`，前端可按 task/turn 重建 timeline。

## 技术路线

### 1. AgentProfile 作为委派扩展点

后端新增三类内置 child profile，并注册到现有 `AgentProfileRegistry`。`delegate_task` 的输入使用 `child_agent_id` 选择 profile，`delegation_type` 只作为展示、统计和结果 schema 标签，不作为执行分支主键。

child profile 必须显式声明 `allowed_tools`。权限计算统一按：

```text
effective_child_tools =
  requested_tools
  ∩ parent.allowed_tools
  ∩ child.allowed_tools
  ∩ system_policy_allowed_tools
```

`system_policy_allowed_tools` 是 core 层根据运行时策略收窄后的集合，至少覆盖委派深度、并发、是否允许 child 再委派、workspace/web 工具可用性等系统级门禁。审查类与分析类 profile 默认不开放写入、删除、终端执行等破坏性工具。编码类 profile 也只在父 profile 已允许时继承对应能力。

稳定注册 profile 与单次运行 profile 必须分离：从 `AgentProfileRegistry` 解析出的对象只作为模板，child 执行前必须派生本次运行 profile，再绑定 turn、收窄 tools、预算与 prompt context。禁止直接修改 registry 中的稳定 profile，避免并发 child、恢复和后续运行互相污染。

### 2. delegate_task 只做工具入口，不承载执行系统

新增 `delegate_task` 工具 handler 时，tools 层只负责参数校验、展示声明和调用一个窄协议，例如 `DelegateTaskExecutor`。真实执行实现放在 `core/delegation`，由 runtime 在本 turn 作用域注入。

不采用全局 handler 可变状态，不把 child runtime 实现塞进 `ToolDefinition.handler`，避免污染进程级 `ToolSystem`。

建议的注入路径是新增“工具运行期依赖通道”：从 `RuntimeOperations / ToolExecutionService / ToolScheduler / ToolExecutor` 向 handler 透传本 turn 的 runtime dependencies。这样可复用现有工具执行链、权限门禁、输出预算、hook 与实时事件通道，也避免为一个工具克隆整套 `ToolSystem`。

### 3. 持久化与服务边界

新增 delegation 数据模型：

- `apps/backend/app/storage/model/delegation_model.py`
- `apps/backend/app/storage/crud/delegation_crud.py`
- `apps/backend/app/service/delegation/`

职责边界：

- `storage` 只做 ORM 与 CRUD，不做策略。
- `service/delegation` 只做状态流转、策略校验、记录聚合，不 import `core`，不 import `AgentProfile`。
- `core/delegation` 负责解析 parent/child `AgentProfile`、创建 child turn、驱动 child runtime、汇总结果。
- `tools` 不 import `core/service`，只依赖 protocol。

第一版 child turn 复用父 `task_id`，通过 `delegation_id`、`parent_turn_id`、`child_turn_id` 区分，不创建新 task。

### 4. 事件与 SSE 路由选择

第一版选择“父流摘要 + child turn 独立订阅”：

- 父 turn 的 `delegate_task` 工具卡片只展示委派状态摘要。
- 后端在父 turn 事件中发出 `delegation_started`、`delegation_child_started`、`delegation_finished`、`delegation_failed`、`delegation_cancelled`。
- `delegation_child_started` 必须携带 `delegation_id`、`parent_turn_id`、`child_turn_id`、`child_agent_id`。
- 前端收到 `child_turn_id` 后对 child turn 建立独立实时订阅；历史回放则通过 task events + turns 重建。

选择理由：当前 `RuntimeEventBus` 和 SSE endpoint 是 turn 级模型，独立订阅能最大限度复用现有 producer/consumer、去重、历史回放和 timeline 分片，不把 child 事件混入父 turn 造成 sequence 与终态语义混乱。

当前 `/turns/{turn_id}/stream` 只允许 `pending` turn 进入，child turn 由后端 delegation executor 创建并启动时，前端收到 `child_turn_id` 后它可能已经是 `running`。因此第一版必须新增或改造出明确的 subscribe-only 语义：

- 推荐新增 `GET /turns/{turn_id}/events/stream` 作为只订阅端点：`pending/running` 可订阅，已终态返回 409 并提示走历史回放，不负责认领或启动 producer。
- child producer 只由 `core/delegation` 创建和持有；前端 child stream 只订阅 `RuntimeEventBus`，不得触发 child 执行。
- 如果选择改造现有 `/turns/{turn_id}/stream`，也必须区分“pending 认领执行”和“running 已有 producer 只订阅”，并保持状态码语义清晰。
- subscription endpoint 断开不得把 child turn 标记为 failed；只有 producer 断开或运行失败才能决定 child 终态。

### 5. 审批、取消、超时与恢复

- child 运行中的审批属于 child turn，审批事件必须带 `delegation_id` 与 `parent_turn_id`，前端操作路由到 child turn。
- 父 turn 取消时，所有未终态 child delegation 一并取消，child pending approval 同步失效。
- 第一版 child 并发固定为 1，深度固定为 1；超限返回工具错误 observation，并记录 delegation failed。
- 第一版 checkpoint 恢复采用保守策略：进程启动或运行时初始化时扫描 pending/running delegation，将无活跃 producer 且 child/parent 已不可继续的记录落为 failed/cancelled，并写审计日志。自动恢复 child checkpoint 放入后续阶段，避免第一版引入隐式重复执行。

### 6. 可观测性

Langfuse 三方依赖仍只允许在 `core/observability`。delegation 需要建立父子 trace 关联：

- 父工具调用 span 记录 `delegation_id`、`child_turn_id`、`child_agent_id`。
- child turn trace metadata 携带 `parent_turn_id`、`delegation_id`。
- Langfuse 失败不得影响主流程。

## 后端实现 Checklist

- [ ] 在 `AgentProfileRegistry` 初始化路径注册 `delegate_reviewer`、`delegate_analyst`、`delegate_coder`。
- [ ] 新增 `delegate_task` 参数模型，输入以 `child_agent_id` 为执行选择键。
- [ ] 新增 `DelegateTaskExecutor` protocol，并确保 tools 层只依赖 protocol。
- [ ] 设计并实现工具运行期依赖通道，避免全局 handler 状态。
- [ ] registry profile 只作为模板使用；执行前派生本次运行 profile，禁止修改稳定注册对象。
- [ ] 新增 delegation ORM、CRUD、service，并接入 schema 初始化。
- [ ] 在 `core/delegation` 实现 child profile 解析、系统策略收窄、权限交集、深度/并发限制、child turn 创建与执行。
- [ ] 新增 subscribe-only child turn 实时订阅机制，或改造 `/stream` 明确支持 running turn 只订阅。
- [ ] 新增 delegation 事件类型、payload model、payload registry 映射。
- [ ] 重新生成 `apps/shared/ts/events.ts`，禁止手工改共享生成文件。
- [ ] 明确 parent summary event 与 child turn stream 的事件字段契约。
- [ ] 父取消时级联取消未终态 child delegation。
- [ ] child 审批事件带齐 `delegation_id`、`parent_turn_id`，并保持审批执行归属 child turn。
- [ ] 加入启动恢复审计：pending/running delegation 不得长期卡死。
- [ ] Langfuse metadata 增加 delegation 关联字段，失败降级。
- [ ] 为权限交集、系统策略收窄、深度限制、并发限制、取消级联、事件 payload 注册、恢复审计补单元测试。
- [ ] 为 child running turn 订阅、终态 turn 订阅拒绝、订阅断开不影响 producer 补集成测试。
- [ ] 为 `delegate_task` 工具成功、失败、child 失败汇总补集成测试。

## 验收标准

- [ ] reviewer/analyst/coder 三类 profile 可通过同一个 `delegate_task` 工具选择。
- [ ] reviewer/analyst 无法调用写入、删除、终端执行等未授权工具。
- [ ] child 事件可独立实时订阅，父 turn 能显示委派摘要与终态。
- [ ] child turn 已处于 running 时，前端仍可通过只订阅通道接收实时事件，且订阅断开不改变 child 执行终态。
- [ ] 父 turn 取消后 child turn 不残留 running/pending approval。
- [ ] 后端重启后 pending/running delegation 有明确终态或审计记录，不出现不可解释卡死。
- [ ] 所有新增 RuntimeEvent 类型在后端 payload registry 与生成的 shared TS 中一致。
- [ ] Langfuse 能从父工具调用追到 child turn，且 Langfuse 故障不影响执行。

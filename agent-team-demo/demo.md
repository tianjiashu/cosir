# Agent Team 运行语义 Demo

DEMO_MARKER_v1

以下为 Agent Team 的运行语义要点（依据 `apps/backend/app/agent_team/` 源码事实）：

- TeamRun 生命周期状态由 `AgentTeamRunModel.status` 单一承载，分 `pending` 确认门槛与 `running` 执行两态；同一主 Run 同时只允许一个待确认 TeamRun，确认边界用 `update_status_if_in` 做原子条件迁移。
- Team 不支持节点并行：每个节点按业务状态只有一条转移边（`node_transitions[node_id][status] = target_node_id`），由 Coordinator 的一条异步驱动任务串行推进；目标节点为保留字面量 `END` 时 Team 结束。
- 节点运行快照 `state.node_runtime` 只冻结 `allowed_tools` 工具名，不保存工具 schema；Coordinator 在创建节点 Task 时从工具注册表按名字解析 schema 并固化到该 Task 的 `tool_schemas`，TeamRun 快照不重复保存。
- `AgentTeamRunState` 是执行聚合快照（节点 Task 绑定、`current_node_id` 游标、转移历史、节点运行配置），不承载生命周期状态；执行配置只供后端恢复执行，API 投影时必须过滤。
- 超过 `max_runs` 轮或找不到转移边时，Coordinator 用条件更新把 TeamRun 收敛为 `FAILED` 并写入 `end_reason`，同时留下可排查日志，而不是静默停在原地。

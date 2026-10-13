DEMO_MARKER_v1

本文件是 Agent Team 运行时机制验证产物（极简 Demo Team 的 draft 节点写出，review 节点只读复核），跑完即可删除。

- 节点串行执行：一条 Team 由一个异步驱动任务按节点推进，每个节点在同一时刻只有一个在跑，不存在节点并行。
- 上下文不共享：每个节点是独立的 Run，拥有自己的 Agent 上下文，看不到上游节点的对话历史，只能看到 handoff 传过来的内容。
- status 决定路由：节点收尾返回的 status（如 done / blocked / pass / needs_changes）在 `node_transitions[node_id][status]` 里查唯一目标节点，命中保留字面量 `END` 时 Team 结束。
- handoff 是唯一跨节点通道：Coordinator 读取节点 Run 持久化的 `final_output` 并解析结构化结论，据此结算 transition 并创建下一节点 Task，除此之外节点之间没有共享内存或旁路。
- max_runs 限制回路：节点执行轮数超过配置 `max_runs`，或当前 status 找不到转移边时，TeamRun 用条件更新收敛为 `FAILED` 并写入 `end_reason`，不会无限重启或静默停在原地。

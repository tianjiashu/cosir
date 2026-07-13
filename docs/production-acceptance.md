# 第一阶段能力与生产级验收标准

第一阶段目标不是做一个简化 demo，而是参考现有先进 coding-agent，复刻其核心能力，形成可真实使用的桌面 coding-agent 基线。

MCP、checkpoint、subagent、context compaction 等核心能力第一版必须按生产级深度设计和验收，不能只做概念验证或演示级实现。

## 第一阶段必须纳入的能力

- MCP：支持 Model Context Protocol 方向的工具/上下文扩展能力。
- 工具权限审批：危险工具、文件写入、命令执行、网络访问等操作必须有权限边界和审批机制。
- checkpoint：支持任务过程中的状态保存、恢复、回看和必要时的回退基础能力。
- subagent：支持把复杂任务拆给子 Agent 或专门 Agent 执行的能力。
- context compaction：支持长上下文压缩、摘要、保留关键决策和继续执行。
- 工具系统：支持工具注册、工具描述、工具调用、结果回传、错误处理、权限控制和扩展。
- 任务执行闭环：支持从讨论、规划、执行、观察、修复到总结的完整循环。
- 审查与测试闭环：进入代码开发后，必须能支持审查 Agent 和测试 Agent 的独立验证流程。

这些能力在第一阶段不要求一开始做到最终形态，但必须达到生产级可用深度：有清晰边界、错误处理、日志、持久化或状态恢复策略、权限控制、可测试性和后续扩展路径。不能把它们降级为遥远未来规划或临时 demo。

## 第一版扩展能力要求

- Agent Workflow 扩展能力：Workflow 不能被写死为单一 ReAct 流程；ReAct-like Workflow 可以作为默认工作流，但底层必须是可扩展 Agent Runtime，应允许后续替换、组合或新增 Plan-and-Execute、review/test/fix loop、multi-agent review 等工作流。
- Context Compaction 扩展能力：压缩策略、保留策略、摘要格式和恢复策略应可替换。
- Subagent 扩展能力：子 Agent 的角色、能力、上下文输入、输出协议和调度方式应可扩展。
- Tool 扩展能力：工具注册、权限等级、参数 schema、执行方式、结果解析和错误处理应可扩展。

扩展点的目标不是第一版就做复杂插件市场，而是避免核心能力被硬编码锁死。

## 通用生产级验收标准

第一版生产级深度不要求功能最终完美，但要求每个核心能力具备：清晰模块边界、持久化状态、错误处理、日志记录、权限控制、可测试性、可扩展策略，以及失败后可恢复或可诊断的能力。

生产级标准的判断口径：不是能跑通 demo，而是在真实开发任务中可恢复、可审计、可测试、可扩展、失败可定位。

- 有清晰职责边界：模块、目录、接口一眼能看出属于 MCP、checkpoint、subagent、context compaction 或工具系统中的哪一层。
- 有持久化：关键状态不能只放内存，应用重启后能恢复或解释不可恢复原因。
- 有日志：关键输入、输出、状态变化、失败原因必须写入可排查日志文件。
- 有错误处理：失败不能静默吞掉，要能返回可理解错误并保留定位线索。
- 有测试：正常路径、失败路径、边界情况都能测试。
- 有扩展点：不能硬编码死流程，后续能替换策略或接入新实现。
- 有权限边界：涉及文件、命令、网络、工具调用时必须可审批、可拒绝、可追踪。

## Agent Runtime / Loop 验收标准

- 能显式区分 Session、Task、Turn、Step，不把所有状态混在单个循环变量或单个 prompt 中。
- 默认 ReAct-like Workflow 能完成模型调用、工具调度、观察结果回注和继续判断。
- Workflow 可替换或新增，不需要改写工具系统、checkpoint、context compaction 和 UI 事件协议。
- 工具执行必须经过 Tool Scheduler，支持审批、拒绝、超时、取消、错误回注和日志记录。
- continue、finish、compact、retry、recover、delegate subagent 等决策必须显式建模。
- Agent Loop 必须支持 max steps / max turns / loop detection / cancellation 等终止保护。
- Agent Runtime 事件必须能推送到 UI，并写入可排查日志文件。

## MCP 验收标准

- 能注册、加载、启用、禁用 MCP server。
- 能发现 MCP tools/resources，并展示给用户或 Agent。
- 工具调用有参数 schema 校验。
- 工具执行有超时、错误捕获、日志记录。
- 危险工具必须进入权限审批。
- MCP server 失败不能拖垮主 Agent。
- 后续能扩展更多 server，不需要改核心 Agent loop。

## Checkpoint 验收标准

- 每个关键任务阶段能生成 checkpoint。
- checkpoint 至少记录：会话、任务状态、上下文摘要、工具调用历史、文件变更元数据、Agent 当前阶段。
- 应用重启后能恢复任务状态。
- 用户能查看 checkpoint 列表和关键差异。
- 失败后能回到最近可用 checkpoint。
- checkpoint 写入失败必须有日志和错误提示。
- 文件级回退可以后续增强，但第一版必须先保证任务状态可恢复。

## Subagent 验收标准

- 能定义不同 subagent 角色，例如审查、测试、检索、规划、实现辅助。
- 主 Agent 能分配任务给 subagent。
- subagent 有独立上下文输入和输出结果。
- subagent 执行过程可追踪、可取消、可失败恢复。
- subagent 不能无限递归或无限启动。
- subagent 输出必须回到主 Agent，由主 Agent 汇总和决策。
- 后续新增 subagent 类型不需要改核心流程。

## Context Compaction 验收标准

- 上下文接近阈值时能自动触发压缩，或允许手动触发。
- 压缩结果必须保留：用户目标、已确认决策、当前计划、关键文件、工具结果、未解决问题。
- 压缩前后任务能继续执行，不丢关键意图。
- 压缩记录可查看，必要时能追溯原始片段。
- 压缩策略可替换，例如按任务、按文件、按决策、按时间线压缩。
- 压缩失败不能破坏原上下文。
- 必须有测试验证“压缩后仍能继续完成任务”。

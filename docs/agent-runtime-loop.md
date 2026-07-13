# Agent Runtime 与 Agent Loop 定位

本文记录项目对 Agent Loop、ReAct 和可扩展 Agent Runtime 的架构定位。

## 核心判断

本项目不能把 Agent Loop 简化理解为 ReAct。

ReAct 是一种模型行为范式：

```text
Reason -> Act(tool) -> Observe(result) -> Reason ...
```

Agent Loop 是运行时控制机制：

```text
状态管理
模型调用
工具调度
权限审批
checkpoint
context compaction
取消与恢复
错误处理
subagent
事件流
终止条件
```

因此，ReAct 可以作为第一版默认 Workflow，但不能成为底层架构边界。

## 对成熟实现的归纳

现有成熟 coding-agent 大多包含 ReAct-like 行为，但它们的生产级能力主要来自 Agent Runtime，而不是单一 ReAct 循环。

| 实现 | Agent Loop 形态 | 关键启发 |
| --- | --- | --- |
| Claude Code | 显式 State + streaming loop | 状态对象、流式事件、工具并发分级、恢复路径 |
| Codex | Session / Task / Turn 事件驱动 | 任务生命周期、取消、审批、并发工具、UI 事件 |
| Gemini CLI | recursive continuation + scheduler | 工具调度状态机、递归续跑、终止保护 |
| Qwen Code | recursive continuation + loop detection | 循环检测、限流重试、压缩、subagent abort 管理 |
| Kimi CLI | while loop + checkpoint/revert | checkpoint、状态恢复、回滚思路 |
| OpenCode | message/task-driven loop | subtask、compaction、normal turn 并列分支 |
| SWE-agent | attempt loop + step loop | 失败重试、环境重置、结果择优、工程任务闭环 |

共同骨架：

```text
用户输入
-> 构建上下文 / 工具 / 系统提示
-> 调用模型并流式接收
-> 解析 tool calls
-> 执行工具
-> 把 tool results 写回上下文
-> 判断继续、压缩、终止、重试、回滚或派发 subagent
-> 完成或失败退出
```

## 本项目的定位

第一版应实现一个可扩展 Agent Runtime，并内置默认 ReAct-like Workflow。

默认 Workflow 可以是：

```text
model step
-> tool schedule
-> observation update
-> continue decision
```

但底层 Runtime 必须支持替换或新增 Workflow，例如：

- Plan-and-Execute
- review/test/fix loop
- multi-agent review
- research-then-implement
- context-first workflow
- 自定义用户开发习惯 workflow

## 推荐运行时分层

```text
Agent Runtime
  ├─ Workflow Engine
  ├─ Task / Turn / Step State
  ├─ Model Adapter
  ├─ Tool Scheduler
  ├─ Approval System
  ├─ Context Manager
  ├─ Compaction Strategy
  ├─ Checkpoint Manager
  ├─ Subagent Runtime
  ├─ Event Stream
  └─ Error / Recovery / Loop Detection
```

其中 LangGraph 承载 Workflow 和状态流转，但不应把所有业务规则、提示词、工具策略和压缩策略硬塞进单个图节点。

## 第一版设计原则

- ReAct-like Workflow 是默认能力，不是架构锁定。
- Workflow、context compaction、subagent、tool scheduler 都必须是可替换策略。
- Agent Run 至少应显式区分 Session、Task、Turn、Step。
- 每个关键 Step 应产生可追踪事件，并能写入日志和 checkpoint。
- 工具执行必须经过 Tool Scheduler，而不是由模型调用结果直接裸执行。
- continue / finish / compact / retry / recover / delegate subagent 必须是显式决策，不应散落在各处 if 分支中。
- subagent 应作为 child run / child task 处理，拥有独立上下文、事件、审批和取消边界。

## 非目标

- 不把项目定义成“做一个 ReAct Agent”。
- 不把 ReAct prompt 或 ReAct loop 写死在核心 Runtime。
- 不为了快速跑通 demo 牺牲后续 Workflow 扩展能力。
- 不把工具执行、审批、压缩、checkpoint 混在模型调用函数里。


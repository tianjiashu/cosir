# Agent 实现想法文档

本文档用于沉淀 Agent 实现讨论过程中的原始想法、中间判断、已确认小结论、候选方案、风险和开放问题。

本文档不是最终技术方案，不是正式架构文档，也不是代码任务清单。后续当讨论稳定后，再从本文档整理出正式架构文档、接口文档或开发任务。

## 当前主题

讨论第一版基础 Agent 的实现方向。

当前重点是先形成一个可运行、可扩展、可诊断的 ReAct-like 单 Agent 闭环，同时避免把 Agent 架构锁死为单一 ReAct 流程。

## 已确认结论

- 第一版可以先实现 ReAct-like Workflow，作为默认基础工作流。
- ReAct-like 只是第一版默认 Workflow，不等于整个 Agent 架构。
- Agent、Workflow、Runtime 三个概念需要保持分离。
- Subagent 不应实现成一套平行系统；它应作为 child agent / child run 复用 Agent、Workflow 和 Runtime 能力。
- 工具不能由模型响应直接裸执行；模型只提出 tool call，真正执行必须经过 Tool Scheduler。
- 第一版应优先保证单 Agent、单 Task、串行 tool call 的稳定闭环，再逐步接入并发工具、subagent、checkpoint 恢复和 context compaction。
- 第一版客户端输入先只支持纯文本，暂不支持图片和文档附件。
- 图片和文档附件是后续扩展方向，需要提前在概念上避免把消息模型永久写死为单一字符串。
- DeepSeek V4 Flash 当前按官方资料应视为文本模型；后续图片输入需要通过视觉模型代理、OCR 或文档/图片预处理后再进入 DeepSeek。

## 概念边界

### Agent

Agent 是执行主体，描述谁在执行任务。

Agent 至少包含：

- role
- goal
- run_id
- allowed_tools
- context_policy
- state / run metadata

### Workflow

Workflow 是执行策略，描述 Agent 如何完成任务。

候选 Workflow 包括：

- ReAct-like
- Plan-and-Execute
- Review-Fix
- Research-Then-Implement
- Multi-Agent Review

第一版只把 ReAct-like 作为默认工作流候选，不把 ReAct 写死为底层架构边界。

### Runtime

Runtime 是执行底座，负责让 Agent 按 Workflow 运行。

Runtime 负责：

- Session / Task / Turn / Step 状态管理
- 模型调用
- 工具调度
- 权限审批
- checkpoint
- context compaction
- 事件流
- 取消与恢复
- 错误处理
- max steps / loop detection / termination guard

### Subagent / Child Run

Subagent 是 child agent / child run，不是另一套独立运行时。

它与主 Agent 复用：

- Agent 定义
- Workflow 策略
- Runtime 执行机制
- Tool Scheduler
- Context 管理
- Event 事件流
- Checkpoint 机制

差异主要体现在：

- 角色定义
- 输入上下文范围
- 工具权限范围
- 终止条件
- 结果回传协议
- 父子 run 关系
- 递归和并发限制

## 候选实现想法

### 第一版最小闭环

候选 ReAct-like 闭环：

```text
User Task
  -> build context
  -> call model
  -> parse response
  -> if tool calls: schedule tools
  -> collect observations
  -> append observations to context
  -> continue
  -> final answer
```

候选约束：

- 单 Agent
- 单 Task
- 串行 tool call
- 显式 max steps
- 显式 cancellation
- 工具调用统一经过 Tool Scheduler
- 所有关键步骤写入事件流和日志

## ReAct-like Workflow 想法

ReAct-like Workflow 只负责描述任务推进策略，不直接承担工具执行、审批、持久化和日志职责。

候选职责：

- 决定下一次模型调用需要哪些上下文。
- 根据模型响应判断继续、完成或失败。
- 将 tool call 交给 Runtime / Tool Scheduler。
- 将 observation 放回上下文。
- 在达到终止条件时结束 run。

不应承担：

- 直接执行工具。
- 直接访问数据库。
- 直接写 checkpoint。
- 直接操作 UI 事件通道。
- 写死具体模型供应商。

## Runtime 想法

Runtime 是第一版最关键的底座。

候选状态层级：

```text
Session
  Task
    Turn
      Step
```

含义：

- `Session`：一个用户项目或长期会话。
- `Task`：用户发起的一次任务。
- `Turn`：用户与 Agent 的一次交互轮次。
- `Step`：一次模型调用、工具调用、观察回注或最终响应。

候选职责：

- 维护 run state。
- 调用 Model Adapter。
- 调用 Tool Scheduler。
- 记录 events。
- 写入 logs。
- 后续接入 checkpoint。
- 后续接入 context compaction。
- 处理 max steps、取消、失败和恢复。

## Tool Scheduler 想法

Tool Scheduler 是工具执行边界，不能被 Workflow 绕过。

候选职责：

- 校验 tool call 是否存在。
- 校验参数 schema。
- 检查工具权限。
- 触发用户审批或自动拒绝。
- 执行工具。
- 设置超时。
- 捕获 stdout / stderr / exception。
- 标准化 tool observation。
- 写入事件和日志。

## Context 想法

第一版先实现最小上下文构建，不急于引入复杂长期记忆或向量检索。

候选职责：

- 构建系统提示、用户目标、历史消息和工具结果。
- 保留当前 Task 的关键状态。
- 支持后续 context compaction 插入。
- 不把所有参考资料无差别塞进上下文。

风险：

- 如果过早接入大知识库，容易造成上下文噪音。
- 如果 context 与 runtime 状态混在一起，后续恢复和压缩会困难。

## 输入与附件管线想法

第一版客户端输入先只支持：

- 纯文本。

后续扩展再支持：

- 图片附件，例如截图、界面图片、错误截图。
- 文档附件，例如 Markdown、TXT、PDF、DOCX，后续可扩展更多格式。

第一版候选边界：

```text
Client Composer
  -> Text Message Draft
  -> Context Builder
  -> Model Adapter
```

后续附件管线候选边界：

```text
Client Composer
  -> Message Draft
  -> Attachment Store
  -> Attachment Preprocessor
  -> Context Builder
  -> Model Adapter
```

第一版候选职责：

- `Client Composer`：负责纯文本输入框和发送动作。
- `Text Message Draft`：负责保存用户输入文本。
- `Context Builder`：负责选择当前文本消息、历史消息和工具结果进入上下文。
- `Model Adapter`：负责把文本消息发送给当前模型。

后续附件管线候选职责：

- `Client Composer`：负责 UI 输入框、附件卡片、删除附件、发送动作。
- `Attachment Store`：负责保存附件原始文件、文件元数据、hash、mime type、大小、来源。
- `Attachment Preprocessor`：负责把不同附件转换成模型可消费内容，例如文本抽取、OCR、图片描述、缩略图。
- `Context Builder`：负责选择哪些附件内容进入当前上下文，并控制 token 预算。
- `Model Adapter`：负责根据模型能力决定原生发送多模态内容，还是发送预处理后的文本描述。

后续模型能力差异需要显式建模：

```text
ModelCapability
  - text_input
  - image_input
  - document_input
  - tool_calls
  - json_output
  - thinking_mode
  - max_context_tokens
```

DeepSeek V4 Flash 的当前处理策略：

- 文本：可直接发送。
- Markdown / TXT：可提取文本后发送。
- PDF / DOCX：需要先做文本抽取，再按 token 预算送入上下文。
- 图片：不能默认直接送入 DeepSeek；需要先通过视觉模型代理、OCR 或本地图片解析工具生成文本描述。

后续兼容其他模型时：

- 如果模型支持原生 image input，则 Model Adapter 可以直接发送图片内容。
- 如果模型不支持 image input，则走图片预处理或视觉代理。
- 如果模型支持原生 document input，则可以评估是否绕过本地文本抽取。
- 即使模型支持原生附件输入，也应保留本地预处理能力，便于日志、审查、缓存、引用和跨模型降级。

## Checkpoint 想法

第一版 ReAct-like 闭环可以先预留 checkpoint 接口，不一定第一步完整实现文件级回退。

候选最小 checkpoint 内容：

- session_id
- task_id
- run_id
- step_id
- 当前状态摘要
- 工具调用历史
- 模型响应摘要
- 关键上下文摘要
- 文件变更元数据

开放问题：

- 第一版 checkpoint 是否需要支持文件级回退？
- checkpoint 由 Runtime 写入，还是由独立 Checkpoint Manager 订阅事件写入？

## Subagent / Child Run 想法

第一版不优先实现 subagent，但需要避免目录和概念设计把 subagent 做成平行系统。

候选设计方向：

- child run 由 parent run 派生。
- child run 使用同一 Runtime。
- child run 可以选择不同 Agent role 和 Workflow。
- child run 输出必须回到 parent run，由 parent 统一决策。
- 必须限制递归深度、并发数量和总 token / step 预算。

## 风险与冲突

- 风险：如果为了快速跑通，把模型响应直接绑定到工具执行，会破坏后续权限审批、日志、checkpoint 和恢复能力。
- 风险：如果把 ReAct-like Workflow 写死进 Runtime，后续 Plan-and-Execute、Review-Fix 和 multi-agent workflow 会难以接入。
- 风险：如果 subagent 作为独立模块实现完整运行时，后续会重复实现 context、tool、approval、event 和 checkpoint。
- 风险：如果第一版同时做并发工具、subagent、checkpoint、context compaction，基础闭环可能变得不可诊断。
- 风险：第一版只支持纯文本是合理收窄，但如果代码结构把消息永久写死为不可扩展字符串，后续接入图片和文档会产生大改动。
- 风险：后续如果客户端附件格式直接绑定某个模型 API，接入多模态模型或切换供应商会产生大改动。
- 风险：后续如果图片和文档只在 UI 层保存，不进入后端统一附件管线，Agent Runtime 无法追踪、复现、压缩和 checkpoint。
- 风险：后续如果把图片先转文本描述，可能丢失视觉细节；需要保留原始附件引用和预处理产物，便于重处理。

## 开放问题

- 第一版是否只支持单 Agent、单 Task、串行 tool call？
- 第一版状态模型是否采用 Session / Task / Turn / Step？
- 第一版 Tool Scheduler 的权限审批做到什么粒度？
- 第一版是否只预留 checkpoint 接口，还是同步实现状态级 checkpoint？
- ReAct-like Workflow 和 LangGraph 的边界如何划分？
- 后续图片输入是先走视觉模型代理，还是先只做 OCR / 图片描述工具预处理？
- 后续文档输入优先支持哪些格式：Markdown、TXT、PDF、DOCX？
- 后续附件原始文件应保存在哪里，是否进入 checkpoint 和任务历史？
- 后续 UI 中附件卡片与后端 Attachment Store 的协议字段如何设计？

## 被推翻或替换的想法

- 不把 Agent 等同于 Workflow。
- 不把 Subagent 做成一套平行系统。
- 不提前铺大量空目录；后续按能力边界边迭代边创建。
- 不把图片和文档附件纳入第一版基础 Agent 输入范围；第一版先只支持纯文本。

## 后续可整理方向

- 整理正式 Agent Runtime 架构文档。
- 整理 ReAct-like Workflow 流程文档。
- 整理 Tool Scheduler 设计文档。
- 整理 Session / Task / Turn / Step 状态模型。
- 整理输入与附件管线设计文档。
- 整理模型能力矩阵和 Model Adapter 接口。
- 整理第一版开发任务拆分。

## 变更记录

- 2026-07-13：创建文档，沉淀 Agent / Workflow / Runtime / Subagent 概念边界和第一版 ReAct-like 实现想法。
- 2026-07-13：补充客户端需要支持文本、图片、文档输入；记录 DeepSeek V4 Flash 当前按文本模型处理，并预留多模型多模态兼容策略。
- 2026-07-13：收窄第一版范围，确认第一版客户端输入先只支持纯文本；图片和文档附件改为后续扩展方向。

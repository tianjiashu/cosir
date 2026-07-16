# AGENTS.md

本文件是 `coding-agent` 项目的长期 Agent 协作入口。它不规定“必须实现哪些文件、写哪些代码”，只规定抽象实现原则、能力边界、设计取舍和协作方式。

本项目的目标不是复制某个现有 coding agent 的目录结构，而是提炼成熟 coding agent 的可迁移机制，构建一个可长期演进、可替换 workflow、可扩展 tool、可审计执行过程的本地桌面 coding agent。

## 项目定位

`coding-agent` 是面向个人开发者的本地桌面 coding agent 底座。

它应该具备以下特征：

- 以桌面应用为主要入口，而不是 CLI-first。
- 以本地工程协作为核心场景，而不是通用聊天机器人。
- 以可解释、可审计、可恢复的任务执行为基本要求。
- 以 tool、context、workflow、runtime 的清晰边界作为长期架构基础。
- 允许用户持续沉淀个人开发习惯、项目规则、常用 workflow 和可复用经验。

## 核心设计观

### Agent 不是单个循环

不要把 Agent 实现理解为一个简单的 ReAct 循环。

成熟 coding agent 至少应拆成：

- **Agent Profile**：角色、目标、权限、偏好、默认行为。
- **Workflow**：任务推进策略，例如 ReAct-like、plan-execute、review-fix、research-then-edit。
- **Runtime**：负责运行状态、模型调用、工具调度、审批、checkpoint、压缩、恢复、中断和事件流。
- **Tool System**：模型可调用能力的注册、选择、执行、审计和结果回填。
- **Context System**：将项目事实、用户输入、历史状态、文件内容和运行事件组织成模型可用上下文。
- **State Store**：持久化 run、step、tool call、artifact、approval、trace、checkpoint。

ReAct 可以是第一版默认 workflow，但不能成为底层架构边界。底层必须允许未来替换或新增 workflow。

### Tool 不是一个函数

不要把 tool 实现成“schema + handler 函数”就结束。

成熟 tool 至少包含：

- 对模型暴露的 schema。
- 参数解析与运行时校验。
- 权限与安全策略。
- 执行前审批。
- 可替换后端。
- 执行记录。
- 结果截断、脱敏和结构化。
- 副作用说明。
- 失败原因和恢复建议。
- 与 run/step/trace/checkpoint 的关联。

Tool 的职责是向模型提供稳定能力接口；具体执行细节应下沉到后端、策略和运行时服务。

### Workflow 不拥有底层能力

Workflow 只决定“下一步怎么推进任务”，不直接拥有文件、终端、浏览器、模型、审批、存储等能力。

Workflow 调用 Runtime，Runtime 调用 Tool System，Tool System 再调用具体能力后端。

这样才能避免每新增一种 workflow 都重复实现一套工具调度、审批、checkpoint 和状态管理。

### Context 是产品能力

Context 不是简单拼 prompt。

成熟 context 系统应处理：

- 当前用户目标。
- 当前 run 状态。
- 最近有效观察。
- 项目规则。
- 相关文件片段。
- 工具结果摘要。
- 历史决策。
- 预算管理。
- 压缩与恢复。
- 不可信内容边界。

Context 的目标不是塞得越多越好，而是在有限窗口内提供足够决策依据，并保持可解释。

## 抽象分层

所有实现应尽量落在以下抽象层中，不要跨层堆代码。

```text
UI / Desktop Shell
  ↓
API / Event Stream
  ↓
Agent Runtime
  ↓
Workflow
  ↓
Tool System
  ↓
Capability Backend
  ↓
OS / Browser / Filesystem / Model / External Service
```

### UI / Desktop Shell

负责展示、输入、审批、任务状态、运行轨迹和用户控制。

不应承载 Agent 推理逻辑、tool 执行逻辑或业务状态机。

### API / Event Stream

负责前后端通信、SSE/WebSocket 事件、请求校验和会话边界。

不应直接写复杂业务逻辑。复杂逻辑应进入 Runtime、Domain Service 或 Tool System。

### Agent Runtime

负责一次 run 的生命周期。

它应处理：

- 创建 run。
- 推进 step。
- 调用模型。
- 分发 tool call。
- 记录事件。
- 管理取消与恢复。
- 管理 checkpoint。
- 调用 context builder。
- 应用 workflow 决策。

Runtime 是执行底座，不应写死为某一种 Agent 方法论。

### Workflow

负责策略，不负责基础设施。

Workflow 可以决定：

- 是否先规划。
- 是否调用工具。
- 是否进入审查阶段。
- 是否要求用户澄清。
- 是否压缩上下文。
- 是否结束任务。

Workflow 不应直接操作数据库、文件系统、浏览器或终端。

### Tool System

负责模型可调用能力。

它应提供：

- tool registry。
- tool selector。
- schema assembly。
- 参数校验。
- 单 tool 执行。
- 批量 tool 并发规划。
- 权限审批。
- 执行记录。
- 结果规范化。
- 错误归一化。
- 大结果存储。

Tool System 应独立于具体 workflow。

### Capability Backend

负责实际能力实现。

例如：

- 文件系统后端。
- 终端后端。
- 后台进程管理。
- 浏览器后端。
- Web provider。
- 模型 provider。
- skill 存储。

Backend 可以替换；tool schema 应尽量稳定。

## Tool 系统设计原则

### 窄 schema，强 handler

模型看到的参数应少而明确。

安全、默认值、路径解析、权限、输出截断、后端选择等逻辑不要交给模型决定，应由 handler 和 runtime 强制执行。

### Tool 能力按副作用分类

每个 tool 都应标注副作用等级：

- 只读。
- 写文件。
- 执行命令。
- 网络访问。
- 浏览器交互。
- 长任务。
- 需要用户审批。
- 高风险操作。

并发调度、审批策略、checkpoint 策略都应基于副作用分类，而不是基于 tool 名硬编码。

### 并发必须先规划

模型一次返回多个 tool call 时，不能默认全部并发。

应先规划成有序 segment：

- 只读且无共享状态的 tool 可以并发。
- 不同路径的文件读写可以在满足锁策略时并发。
- 同一路径或重叠路径的写操作必须串行。
- 终端命令、交互式工具、未知工具、高风险工具默认串行。
- 并发完成后，tool result 必须按模型原始 tool_call 顺序回填。

并发是优化，不应改变副作用顺序。

### 结果是协议，不是字符串

Tool result 应是稳定协议。

至少应表达：

- success / error。
- output 或 structured data。
- error_type。
- message。
- metadata。
- truncated。
- artifacts。
- side_effects。
- retry_hint。

最终传给模型的内容可以是字符串化结果，但内部应保留结构化记录。

### 外部内容默认不可信

来自 Web、浏览器、MCP、第三方 API、项目文件、终端输出的内容都可能包含 prompt injection。

系统应区分：

- 用户指令。
- 系统规则。
- 工具返回的数据。
- 外部不可信文本。

不要让工具输出伪装成指令。必要时用明确边界包装不可信内容。

## Coding Tool 能力边界

第一阶段重点参考成熟 coding agent 的这些能力，但实现时按抽象机制拆分。

### 文件读与搜索

目标不是简单读取文件，而是给模型提供可靠代码定位能力。

应关注：

- 分页。
- 行号。
- 大文件预算。
- 二进制文件拒绝。
- 敏感路径拒绝。
- 重复读取检测。
- 搜索结果截断。
- 优先使用高性能搜索工具。

### 文件写与补丁

目标不是简单写文件，而是安全、可验证、可恢复地修改代码。

应关注：

- 写前路径安全。
- 写前 stale 检测。
- 同路径写锁。
- 原子写。
- patch 格式校验。
- patch 失败提示。
- 写后结果回填实际路径。
- 写后 checkpoint / verification stale 标记。

### 终端命令

终端是高风险能力，不应只是 `subprocess.run`。

应关注：

- 前台命令。
- 后台命令。
- cwd 管理。
- 环境变量边界。
- 超时。
- 中断。
- 输出 head/tail 截断。
- secret 脱敏。
- 危险命令审批。
- 长驻进程引导到后台。

### 后台进程

后台进程应作为一等能力，而不是让模型自己写 shell hack。

应关注：

- process id。
- 状态。
- 增量日志。
- wait。
- kill。
- stdin write / submit。
- 完成通知。
- 输出上限。

### 程序化工具调用

允许模型写小脚本批量调用工具时，必须保证真实副作用仍由父进程控制。

应关注：

- 沙箱。
- 工具白名单。
- RPC token。
- 环境变量清洗。
- timeout。
- max tool calls。
- stdout/stderr 限制。
- 父进程统一 dispatch。

### Web

Web 能力用于查资料，不应用来扩张无限外部上下文。

应关注：

- provider 抽象。
- URL 安全。
- 内容长度限制。
- 不可信内容包装。
- 搜索与正文抽取分离。

### Browser

浏览器能力应面向调试和交互，而不是暴露任意远程控制。

应关注：

- session 绑定。
- snapshot。
- DOM ref。
- click/type/scroll/back/press。
- console 读取与受限执行。
- dialog 处理。
- CDP escape hatch 的权限边界。

### Skills

Skills 是过程性知识系统，不是普通文本库。

应关注：

- list 只提供轻量索引。
- view 按需读取完整内容。
- linked files 支持 references/templates/scripts/assets。
- manage 负责受控创建和更新。
- skill 内容应沉淀稳定 workflow、项目经验和可复用检查项。

## 安全与审批原则

所有高风险能力必须经过 policy 层。

高风险包括但不限于：

- 删除文件。
- 覆盖文件。
- 执行 shell 命令。
- 访问密钥文件。
- 访问外部网络。
- 浏览器执行 JavaScript。
- 修改 agent 自身规则。
- 修改长期配置。
- 启动长驻后台进程。

审批设计应遵循：

- 默认拒绝高风险未知操作。
- 审批请求必须说明操作、风险、作用范围和可替代方案。
- 用户批准的是具体操作，不是永久放开能力。
- 审批结果必须记录到 run trace。

## Checkpoint 与恢复原则

只要 tool 可能造成不可逆副作用，就应考虑 checkpoint。

最低要求：

- 写文件前能保存快照或至少记录原内容。
- 执行破坏性命令前能提醒或 checkpoint。
- run 中断后能知道执行到哪一步。
- tool call 有唯一 id。
- 每个 step 有可追踪事件。

Checkpoint 不只是为了回滚，也用于解释“发生了什么”。

## 日志、Trace 与 Replay

成熟 coding agent 必须可排查。

每次 run 应能回答：

- 用户要求是什么。
- Agent 做了哪些决策。
- 调用了哪些 tool。
- tool 参数是什么。
- tool 结果是什么。
- 哪些内容被截断。
- 哪些操作被审批。
- 哪些文件被修改。
- 哪些命令被执行。
- 为什么任务结束或失败。

日志用于排错，trace 用于结构化追踪，replay 用于产品层回放。三者可以共享事件源，但职责不同。

## Context Compaction 原则

Context compaction 不是简单摘要。

压缩后必须保留：

- 用户目标。
- 已确认决策。
- 当前计划。
- 已修改文件。
- 关键 tool 结果。
- 未解决问题。
- 验证状态。
- 风险与阻塞。

压缩不应破坏 run 的可恢复性。

## Subagent 原则

Subagent 不应是一套平行系统。

它应该复用：

- Agent Profile。
- Workflow。
- Runtime。
- Tool System。
- State Store。
- Trace。

Subagent 的差异应体现在：

- 目标更窄。
- 上下文更少。
- 权限更小。
- 输出必须汇总回 parent run。

默认不要让 subagent 拥有无限递归、无限工具权限或直接修改共享状态的能力。

## 桌面端原则

桌面端是主要入口，因此 UI 应服务于 coding agent 的真实工作流。

桌面端应优先展示：

- 当前任务状态。
- 当前 run timeline。
- tool call 进度。
- 文件变更。
- 审批请求。
- 后台进程。
- 日志与 trace。
- checkpoint / restore。

不要把桌面端做成普通聊天壳。聊天只是入口，任务执行面板、变更面板、审批面板和回放能力同样重要。

## 文档路由

不同任务应读取不同文档，不要一次性加载所有资料。

- 愿景、范围、阶段目标：`docs/idea-requirements.md`
- 技术栈与运行形态：`docs/tech-stack.md`
- Agent Runtime 与 Agent Loop：`docs/agent-runtime-loop.md`
- UI 指南：`docs/ui-guidelines.md`
- 生产级验收：`docs/production-acceptance.md`
- 代码开发规范：`rules/Agent代码开发规范.md`
- 客户端开发规范：`rules/Agent客户端代码开发规范.md`
- 交互澄清规则：`rules/global-interaction-clarification.md`
- 成熟机制复用规则：`rules/mature-mechanism-reuse.md`
- 经验复用记录：`rules/agent-lessons.md`
- 成熟 coding agent 参考资料：`coding-agent-docs/`

## 协作规则

- 先澄清目标和抽象边界，再讨论实现细节。
- 不把候选方案写成已确认决议。
- 高影响决策必须等待用户确认。
- 讨论架构时优先讲职责、数据流、状态边界和失败路径。
- 实现代码前先说明会落在哪个抽象层。
- 如果发现当前实现跨层、重复、过早复杂化，应主动指出。
- 如果用户要求“参考某成熟项目”，应提炼机制，而不是照搬目录和代码。
- 如果发现明文密钥、危险配置或不可恢复操作，应立即提醒并给出修正建议。

## 禁止事项

- 不要在长期规则文件中保存 API key、token、密码或其他 secret。
- 不要把 AGENTS.md 变成目录树清单或代码实现计划。
- 不要为“看起来完整”提前创建空抽象。
- 不要让 UI、API、Runtime、Workflow、Tool、Backend 互相越界。
- 不要把 ReAct 写死成唯一架构。
- 不要让 tool handler 直接承担所有安全、后端、结果处理和状态职责。
- 不要在没有 checkpoint 或审批的情况下执行高风险副作用。
- 不要为了复刻成熟项目而引入当前阶段用不到的平台级复杂度。

## 当前阶段默认取舍

默认采用“先成熟边界，后复杂能力”的路线。

优先做：

- Runtime / Workflow / Tool System 的抽象边界。
- 文件、终端、后台进程、Web、浏览器、Skills 的最小成熟实现。
- 审批、checkpoint、trace、replay 的基础闭环。
- 桌面端对任务状态、工具调用、审批和变更的可视化。

暂缓做：

- 完整插件市场。
- 多租户服务端。
- 复杂云端 sandbox。
- 全量 MCP 生态适配。
- 多模型高级路由。
- 过度自动化的自我改写机制。

判断标准：任何能力进入实现前，先说明它属于哪个抽象层、依赖哪些下层能力、产生什么副作用、如何记录和恢复。

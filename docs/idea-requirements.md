# Coding Agent 想法需求文档

## 当前主题

开发一个新的 coding-agent 项目。

目标不是复刻某一个现有工具，而是先实现市面上比较先进的 coding-agent 基础能力，后续再基于用户个人开发习惯做专门定制。

当前讨论阶段聚焦项目愿景，不展开具体实现方式。

更准确地说，本项目希望参考现有 coding-agent 的成熟实现，但不把 Agent 固定死在某一种流程范式里。后续应允许根据用户想法定制 Workflow、Context、Tool 等核心能力。

推进路线是先模仿成熟 coding-agent，做出可使用的基线版本；随后在真实使用中暴露问题，再围绕用户个人习惯进行定制开发。

## 已确认想法

- 这是一个 0-1 绿地项目。
- 开发过程中不需要兼容旧版本、旧数据或旧架构。
- 项目需要遵守 `C:\Users\Administrator\Desktop\coding-agent\rules` 下的规则。
- 后端使用 Python。
- Agent Runtime 使用 LangGraph。
- 需要有桌面客户端。
- 桌面客户端需要兼容 Windows 和 Mac。
- 不做 CLI，直接以桌面客户端作为主要入口。
- 前后端当前作为同一个本地桌面应用交付，不部署在服务器。
- Agent 在讨论、规划、执行过程中可以参考 `C:\Users\Administrator\Desktop\coding-agent\coding-agent-docs` 下的开源 coding-agent 实现原理文档。
- 最终需要沉淀一个项目级 `AGENTS.md`。
- 当前优先讨论项目愿景，不先讨论具体实现细节。
- 项目需要预留扩展能力，后续可定制 Workflow、Context、Tool，不局限于 ReAct。
- 第一阶段先参考成熟 coding-agent 实现，模仿出一个可用基线；第二阶段根据使用中暴露的问题再做个人化定制。
- 成熟 coding-agent 的参考方式是取长补短，不固定单一主参考对象。
- 运行形态定为本地一体化桌面应用：Tauri 前端 UI + 本地 Python 后端 sidecar，同机运行，同应用交付。
- Agent 内含或可连接较大的知识库；后续实现过程中，Agent 应主动给出面向项目愿景的建议，而不是只被动执行。
- 模型接入第一阶段优先支持 OpenAI 协议，并优先适配 DeepSeek；后续需要陆续接入其他大模型。
- 第一阶段要复刻现有先进 coding-agent 的核心能力，包括 MCP、工具权限审批、checkpoint、subagent、context compaction 等机制。
- 第一版必须具备 Agent Workflow、context compaction、subagent、tool 的扩展能力。
- MCP、checkpoint、subagent、context compaction 等能力第一版必须做到生产级深度。

## 技术栈决议

### 桌面客户端

- 桌面壳：Tauri 2
- 前端框架：React
- 前端语言：TypeScript
- 构建工具：Vite
- UI 组件：shadcn/ui + Radix UI + Tailwind CSS + lucide-react
- 高密度数据视图：TanStack Table / TanStack Virtual
- 代码与 diff 视图：Monaco Editor 或 CodeMirror 6
- 命令面板：cmdk
- UI 定位：客户端优先，不做 CLI；重点承载会话、项目视图、执行状态、权限确认、变更展示、日志入口。

### 后端与 Agent Runtime

- 后端语言：Python
- API 框架：FastAPI
- Agent 编排：LangGraph
- 模型接入：第一阶段优先支持 OpenAI 协议，优先适配 DeepSeek，后续扩展其他大模型。
- 前后端通信：第一版以 HTTP API + SSE 为主；WebSocket 仅作为后续双向实时场景预留。
- 模型调用：第一版使用模型供应商的 streaming 输出，后端将模型增量输出转换为运行事件并通过 SSE 推送给客户端。
- 本地运行形态：桌面客户端启动本地 Python 后端进程；前后端同机运行，同应用交付，不部署服务器。

### 存储与日志

- 本地数据库：SQLite
- 日志：Python logging / structlog
- 日志文件：必须固定写入可排查日志文件，例如 `logs/app.log`
- 持久化对象：会话、任务、运行事件、用户规则、工具配置、上下文索引、checkpoint 元数据。

### 原理文档与知识源

- 文档来源：`C:\Users\Administrator\Desktop\coding-agent\coding-agent-docs`
- 用途：作为参考成熟 coding-agent 实现的本地知识源。
- 原则：先作为开发和设计参考；后续再决定是否做成应用内可检索知识库。

### 客户端 UI 风格

客户端 UI 参考 Codex 桌面客户端风格，但不做像素级复刻。

- 整体风格：浅色、克制、安静、工程工具感。
- 信息密度：偏高密度，适合长时间开发、阅读日志、查看任务状态和代码变更。
- 视觉语言：低饱和灰阶、细边框、轻阴影、少量强调色。
- 布局原则：左侧项目/任务导航，中间主会话，右侧上下文面板，底部输入框，顶部轻量状态区。
- 组件原则：优先用 shadcn/ui、Radix UI、Tailwind CSS、lucide-react；长列表用 TanStack Virtual/Table；代码和 diff 用 Monaco Editor 或 CodeMirror 6；命令面板用 cmdk。
- 产品气质：服务工程工作流，不做营销式 hero、装饰卡片或大面积无效留白。

#### UI 组件实现决议

- 主 UI 体系：`shadcn/ui + Radix UI + Tailwind CSS + lucide-react`。
- `shadcn/ui`：可复制、可定制的业务组件基础。
- `Radix UI`：Dialog、Popover、Tooltip、Tabs、Dropdown、ScrollArea 等底层可访问交互原语。
- `Tailwind CSS`：布局、间距、颜色、边框、状态和响应式样式。
- `lucide-react`：默认图标体系。
- `cmdk`：命令面板和快捷操作入口。
- `TanStack Table`：表格、任务列表、文件变更列表等结构化数据视图。
- `TanStack Virtual`：日志流、长任务流、长列表等虚拟滚动场景。
- `Monaco Editor`：优先用于代码、diff、patch、只读预览和编辑器区域；如后续确认体积或性能压力过高，再评估 `CodeMirror 6`。
- 不优先使用 Ant Design 或 MUI 作为第一版主 UI 体系。

### 暂不进入的技术决策

- 不先定具体向量数据库。
- 不先定具体模型供应商。
- 不先定插件协议细节。
- 不先定完整打包与自动更新方案。

## 运行形态决议

### 已确认形态

这是一个本地一体化桌面应用，不是前端连接远程服务器后端的 Web 产品。

桌面客户端作为主入口和生命周期管理者，启动并管理一个本地 Python 后端 sidecar 进程。前端不直接执行危险操作，而是通过本地 API 与后端通信；后端负责 Agent Runtime、工具执行、日志、存储、上下文和任务状态。

### 进程分工

- Tauri 桌面客户端：
  - 负责窗口、交互、会话展示、权限确认、项目视图、变更展示。
  - 负责启动、探活、停止 Python 后端进程。
  - 不直接承载核心 Agent Runtime。
- Python 后端进程：
  - 负责 FastAPI 服务、SSE 事件流、LangGraph 执行、工具系统、上下文管理、SQLite、日志文件。
  - 作为本地服务运行，只监听本机地址，不作为服务器部署。
- Agent 任务：
  - 第一阶段可在后端进程内以任务为单位运行。
  - 后续如需要更强隔离，再演化为独立 worker / subprocess / sandbox。

### 通信边界

- UI 与后端通过本地 HTTP API + SSE 通信。
- 后端启动时生成本次会话访问凭据，避免本机其他进程随意调用。
- 长任务事件通过 SSE 推送，普通配置和查询通过 HTTP API。
- 所有通信默认发生在本机回环地址，不依赖远程服务部署。

### 为什么推荐

- 保持客户端体验完整：用户只打开一个桌面应用。
- 保持 Python Agent Runtime 独立：便于测试、调试、日志和后续扩展。
- 避免把复杂 Agent 逻辑塞进 Tauri/Rust 或前端。
- 后续可以自然扩展出 worker、沙箱、远程执行或多 Agent 进程模型。

### 暂不推荐

- 不推荐纯前端运行 Agent：工具执行、文件访问、日志和长任务管理都会变复杂。
- 不推荐一开始做远程后端：本项目是个人开发桌面 agent，先保证本地可靠性。
- 不推荐一开始把每个 Agent 都做成独立进程：复杂度偏高，等真实使用暴露隔离需求后再升级。

## 用户原话

- “我想做一个coding-agent。”
- “开发需要按照C:\Users\Administrator\Desktop\coding-agent\rules 下的规则”
- “C:\Users\Administrator\Desktop\coding-agent\coding-agent-docs下是开源的coding-agent实现原理文档。”
- “我想做的coding-agent 可以让Agent实现讨论过程中可以参考原理文档。”
- “我希望为了还有一个桌面客户端兼容Windows、Mac ，后端使用Python。”
- “必须说明是一个0-1绿地项目，开发过程中不需要进行兼容。”
- “我的想法是先实现市面上比较先进的coding-agent，后续我想基于这个进行专门定制属于我开发习惯的agent。”
- “不做CLI，直接客户端，我喜欢用客户端。”
- “然后Agent实现使用langgrph吧”
- “我们不讨论如何如何实现，我们要定一个项目愿景。”
- “可以参考现在coding-agents实现”
- “后续我们可以根据我的想法，可以定制Workflow(不一定局限在ReAct)，定制context、定制tool等。”
- “所以开发过程中需要预留扩展”
- “第一步参考现在成熟的coding-agent实现，先模仿出来”
- “在使用过程中有什么问题，我再进行定制开发。”
- “都可以取长补短”
- “前后端当前是一起啦，不是部署在服务器的”
- “Agent 内含知识库比较大，我希望后续实现的过程中多给我一些建议，向我们的愿景出发！”
- “模型接入优先支持 OpenAI协议”
- “第一阶段就是要复刻现在先进coding Agent所有能力，包括MCP、工具权限审批、checkpoint、subagent、context compaction 等机制。”
- “第一版的需要有 Agent Workflow扩展能力、context compaction扩展能力、subagent扩展能力、tool扩展能力。”
- “OpenAI 协议下第一阶段优先适配 DeepSeek，后续还需要陆续接入其他大模型。”
- “MCP、checkpoint、subagent、context compaction 等能力第一版做到 生产级深度。”

## 项目愿景草案

### 候选愿景

打造一个以个人开发者为中心的桌面 coding-agent：它不是单纯的代码生成工具，而是能理解项目、遵守规则、参与讨论、执行开发、沉淀经验，并逐步适应用户个人开发习惯的长期协作型工程伙伴。

它的长期方向不是一个固定形态的 Agent，而是一个可演化的个人开发智能体底座：可以吸收现有 coding-agent 的成熟机制，也可以在 Workflow、Context、Tool、规则和协作方式上持续被用户改造。

第一阶段应把“成熟 coding-agent 的基础体验”作为标杆，优先保证它能真实承担开发任务；个性化创新不前置到第一阶段，避免还没有可用基线就过度设计。

### 阶段路线

1. 基线模仿阶段：参考成熟 coding-agent，实现一个能用于真实开发的桌面 agent 基线。
2. 使用验证阶段：在实际项目中使用，记录问题、摩擦、误判、低效环节和用户偏好。
3. 定制演化阶段：围绕用户真实使用问题，定制 Workflow、Context、Tool、规则和交互方式。

### 第一阶段能力范围

第一阶段不是简化 demo，而是参考现有先进 coding-agent，复刻其核心能力，形成可真实使用的桌面 coding-agent 基线。

MCP、checkpoint、subagent、context compaction 等核心能力第一版必须按生产级深度设计和验收，不能只做概念验证或演示级实现。

第一阶段必须纳入：

- MCP。
- 工具权限审批。
- checkpoint。
- subagent。
- context compaction。
- 工具注册、工具调用、工具权限和工具扩展。
- 任务执行闭环。
- 后续代码开发阶段需要承载审查与测试闭环。

这些能力第一阶段不要求一开始做到最终形态，但必须达到生产级可用深度：有清晰边界、错误处理、日志、持久化或状态恢复策略、权限控制、可测试性和后续扩展路径。不能把它们降级为遥远未来规划或临时 demo。

### 生产级验收标准

第一版生产级深度不要求功能最终完美，但要求每个核心能力具备：清晰模块边界、持久化状态、错误处理、日志记录、权限控制、可测试性、可扩展策略，以及失败后可恢复或可诊断的能力。

生产级标准的判断口径：不是能跑通 demo，而是在真实开发任务中可恢复、可审计、可测试、可扩展、失败可定位。

#### 通用验收标准

- 有清晰职责边界：模块、目录、接口一眼能看出属于 MCP、checkpoint、subagent、context compaction 或工具系统中的哪一层。
- 有持久化：关键状态不能只放内存，应用重启后能恢复或解释不可恢复原因。
- 有日志：关键输入、输出、状态变化、失败原因必须写入可排查日志文件。
- 有错误处理：失败不能静默吞掉，要能返回可理解错误并保留定位线索。
- 有测试：正常路径、失败路径、边界情况都能测试。
- 有扩展点：不能硬编码死流程，后续能替换策略或接入新实现。
- 有权限边界：涉及文件、命令、网络、工具调用时必须可审批、可拒绝、可追踪。

#### MCP 验收标准

- 能注册、加载、启用、禁用 MCP server。
- 能发现 MCP tools/resources，并展示给用户或 Agent。
- 工具调用有参数 schema 校验。
- 工具执行有超时、错误捕获、日志记录。
- 危险工具必须进入权限审批。
- MCP server 失败不能拖垮主 Agent。
- 后续能扩展更多 server，不需要改核心 Agent loop。

#### Checkpoint 验收标准

- 每个关键任务阶段能生成 checkpoint。
- checkpoint 至少记录：会话、任务状态、上下文摘要、工具调用历史、文件变更元数据、Agent 当前阶段。
- 应用重启后能恢复任务状态。
- 用户能查看 checkpoint 列表和关键差异。
- 失败后能回到最近可用 checkpoint。
- checkpoint 写入失败必须有日志和错误提示。
- 文件级回退可以后续增强，但第一版必须先保证任务状态可恢复。

#### Subagent 验收标准

- 能定义不同 subagent 角色，例如审查、测试、检索、规划、实现辅助。
- 主 Agent 能分配任务给 subagent。
- subagent 有独立上下文输入和输出结果。
- subagent 执行过程可追踪、可取消、可失败恢复。
- subagent 不能无限递归或无限启动。
- subagent 输出必须回到主 Agent，由主 Agent 汇总和决策。
- 后续新增 subagent 类型不需要改核心流程。

#### Context Compaction 验收标准

- 上下文接近阈值时能自动触发压缩，或允许手动触发。
- 压缩结果必须保留：用户目标、已确认决策、当前计划、关键文件、工具结果、未解决问题。
- 压缩前后任务能继续执行，不丢关键意图。
- 压缩记录可查看，必要时能追溯原始片段。
- 压缩策略可替换，例如按任务、按文件、按决策、按时间线压缩。
- 压缩失败不能破坏原上下文。
- 必须有测试验证“压缩后仍能继续完成任务”。

### 第一版扩展能力要求

- Agent Workflow 扩展能力：不锁死单一 ReAct 流程。
- Context Compaction 扩展能力：压缩策略、保留策略、摘要格式和恢复策略可替换。
- Subagent 扩展能力：子 Agent 角色、能力、上下文输入、输出协议和调度方式可扩展。
- Tool 扩展能力：工具注册、权限等级、参数 schema、执行方式、结果解析和错误处理可扩展。

### 成熟实现参考原则

不以某一个 coding-agent 为唯一模板，而是按能力维度取长补短：

- Claude Code / Codex：参考 agent loop、工具权限、安全控制、上下文管理、任务执行体验。
- OpenCode / Gemini CLI / Kimi CLI / Qwen Code：参考会话管理、工具系统、模型适配、checkpoint、错误处理和并发策略。
- SWE-agent：参考面向真实软件工程任务的执行、验证和反馈闭环。
- Cursor：参考桌面产品体验、项目视图、变更呈现和 checkpoint 体验。
- comm 文档：参考共性抽象、ACP、跨 Agent/客户端通信等通用机制。

原则是“能力维度优先”，而不是“产品形态复刻”。每个成熟实现都只作为候选机制来源，最终是否进入项目取决于是否符合个人开发智能体底座的愿景。

### 愿景关键词

- 个人化：最终服务于用户自己的开发习惯，而不是只提供通用 agent 能力。
- 工程化：强调可维护、可排查、可验证，而不是只追求一次性生成代码。
- 协作型：能和用户讨论、澄清、规划，再进入执行。
- 桌面优先：以客户端作为主要工作入口，贴近日常开发体验。
- 可进化：先拥有先进 coding-agent 的基础能力，再持续吸收用户规则、经验和偏好。
- 原理驱动：允许 Agent 参考成熟 coding-agent 的原理文档，用已有工程实践指导设计和行为。
- 主动建议：Agent 应基于知识库、成熟实现和项目愿景，在实现过程中主动提出取舍建议、风险提醒和改进方向。
- 可扩展：Workflow、Context、Tool、规则、模型适配和执行策略都应允许后续替换或扩展。
- 非 ReAct 锁定：ReAct 可以作为候选基础模式，但项目愿景不绑定单一 Agent 范式。
- 先基线后定制：第一阶段先模仿成熟体验，第二阶段再基于真实问题做个人化改造。
- 取长补短：按能力维度吸收成熟实现，不绑定单一参考产品。

### Agent 协作姿态

- Agent 不是单纯执行器，应作为工程协作伙伴参与判断。
- 当实现路线、产品体验、扩展性或长期维护与项目愿景有关时，Agent 应主动提出建议。
- 建议必须引用或关联项目愿景：个人化、工程化、协作型、桌面优先、可进化、原理驱动、可扩展。
- 建议应区分“必须做”“建议做”“以后做”，避免把知识库中的所有好想法一次性塞进当前阶段。
- 如果发现用户当前想法可能偏离愿景，Agent 应温和指出，并给出更贴近愿景的替代方案。
- 大知识库应服务于决策质量，而不是制造上下文噪音；需要筛选、压缩、对齐当前阶段。

### 非愿景

- 不是先做一个命令行工具。
- 不是一次性复刻某个现有 coding-agent。
- 不是把 Agent 固定为单一 ReAct 流程。
- 不是一开始就做大量未经验证的个人化抽象。
- 不是只做代码补全或聊天问答。
- 不是为了兼容旧系统而牺牲设计清晰度。
- 不是追求“尽快生成代码”，而是追求“长期稳定地完成开发任务”。

## 默认假设

- 默认先做需求和架构讨论，不立即进入代码实现。
- 默认把 `docs/idea-requirements.md` 作为讨论阶段的单一事实源。
- 默认后续 `AGENTS.md` 会从本想法文档中提炼，而不是一次性直接定稿。
- 默认先借鉴成熟 coding-agent 的共同机制，再决定哪些机制进入 MVP。

## 候选建议

- 把项目分成两层目标：
  - 基础层：实现先进 coding-agent 的通用能力。
  - 定制层：沉淀用户个人开发习惯、规则、偏好和工作流。
- 第一阶段先定义“先进 coding-agent”的能力边界，而不是先选 UI 技术或工程目录。
- 原理文档可以作为内置参考知识源，但需要设计检索、引用、压缩和版本管理机制，避免把全部文档直接塞进上下文。
- 后续实现中，Agent 应主动基于知识库提出建议，但建议要围绕项目愿景和当前阶段，不做无边界发散。
- 技术栈候选建议：
  - 桌面壳：Tauri 2。
  - 前端：React + TypeScript + Vite。
  - 后端：Python + FastAPI + HTTP API + SSE。
  - Agent Runtime：LangGraph。
  - 本地存储：SQLite。
  - 日志：Python logging / structlog，固定写入 `logs/app.log`。
  - 打包：桌面壳负责 Windows/Mac 安装包，Python 后端作为本地 sidecar 进程随客户端启动。
- LangGraph 在本项目中的定位：
  - 负责 Agent 状态图、执行流转、checkpoint、人类确认中断、事件流和长任务恢复。
  - 项目自身仍需要实现工具权限、文件系统安全、日志、桌面通信、原理文档检索、模型适配和用户偏好层。
  - 不把业务规则和提示词全部塞进 LangGraph 节点；节点保持单一职责，复杂能力拆成独立模块。
- 扩展性原则：
  - Workflow 应作为可演化对象，而不是硬编码流程。
  - Context 策略应允许替换，例如短期会话上下文、项目上下文、规则上下文、原理文档上下文、长期记忆上下文。
  - Tool 应有清晰注册、权限、审查和扩展边界，方便后续加入新的开发工具。
  - Agent 行为应能吸收用户新增规则和经验，而不是只能依赖初始系统提示词。

## 开放问题

- DeepSeek 之后的大模型接入顺序。
- LangGraph 如何承载 workflow 扩展、checkpoint、interrupts、streaming、subgraphs 等能力。
- 第一阶段各能力的验收标准和优先级排序。

## 风险与冲突

- “先进 coding-agent”范围很大，如果不先收窄，容易变成一次性复刻 Claude Code、Codex、OpenCode、Cursor 的全部能力。
- 桌面客户端跨 Windows/Mac 与 Python 后端组合，需要尽早确定进程管理、日志路径、权限模型和打包方式。
- 让 Agent 参考原理文档是核心优势，但如果没有检索和引用边界，可能导致上下文膨胀、幻觉引用或响应变慢。
- 0-1 绿地项目不需要兼容旧系统，但仍需要从第一天保留日志、测试、审查和可维护架构。

## 被推翻或替换的想法

暂无。

## 后续可整理方向

- `AGENTS.md` 项目规则。
- MVP 范围文档。
- 架构设计文档。
- Agent Runtime 设计文档。
- 桌面客户端与 Python 后端通信设计。
- 原理文档检索与引用机制设计。

## 变更记录

- 2026-07-12：创建初版，记录项目定位、硬约束、默认假设、候选建议和开放问题。

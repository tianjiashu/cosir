# AGENTS.md

本文件是 `coding-agent` 项目的长期 Agent 入口指南。它只保留项目愿景、不可变决议、协作原则和文档路由；详细规则放在 `docs/` 和 `rules/` 下。

当前项目尚未进入代码开发阶段。现阶段重点是愿景、边界、技术栈、架构方向和可持续协作规则。

## 项目愿景

打造一个面向个人开发者的本地桌面 coding-agent 底座。

它参考成熟 coding-agent 的工程机制，但不绑定单一 Agent 范式；它允许用户持续定制 Workflow、Context、Tool 和开发规则，最终演化成符合个人开发习惯的长期协作型工程伙伴。

第一阶段先按能力维度取长补短，复刻先进 coding-agent 的生产级核心能力；第二阶段通过真实使用发现问题，再围绕用户个人开发习惯做定制开发。

## 不可变核心决议

- 这是一个 0-1 绿地项目，不需要兼容旧版本、旧数据或旧架构。
- 不做 CLI，桌面客户端是主要入口。
- 前后端作为同一个本地桌面应用交付，不部署在服务器。
- 桌面客户端兼容 Windows 和 Mac。
- 技术底座：Tauri 2 + React + TypeScript + Vite + Python + FastAPI + LangGraph + SQLite。
- 模型接入：第一阶段优先支持 OpenAI 协议，优先适配 DeepSeek，后续陆续接入其他大模型。
- UI 风格：参考 Codex 桌面客户端，使用 shadcn/ui + Radix UI + Tailwind CSS + lucide-react。
- 第一阶段必须覆盖 MCP、工具权限审批、checkpoint、subagent、context compaction、工具系统、任务执行闭环、审查与测试闭环。
- MCP、checkpoint、subagent、context compaction 等核心能力第一版必须按生产级深度设计和验收。
- 第一版必须预留 Agent Workflow、context compaction、subagent、tool 的扩展能力，不能锁死为单一 ReAct 流程。
- ReAct 只能作为第一版默认 ReAct-like Workflow 的候选形式；底层必须是可扩展 Agent Runtime，支持后续替换或新增 Workflow。

## Agent 协作原则

- Agent 不是单纯执行器，应作为工程协作伙伴参与判断。
- Agent 应围绕项目愿景主动提出建议、风险提醒和取舍方案。
- 建议必须区分“必须做”“建议做”“以后做”，避免无边界发散。
- 如果用户想法可能偏离愿景，Agent 应温和指出，并给出更贴近愿景的替代方案。
- 不把候选建议写成已确认决议。
- 高影响决策必须等用户确认后再升级为决议。
- 大知识库只服务于决策质量，不制造上下文噪音；需要筛选、压缩、对齐当前阶段。

## 当前目录结构与职责

当前目录只保留已确认的项目骨架。后续不要为了完整感提前铺大量空目录；只有当某个能力进入设计或实现，并且职责边界已经明确时，才增量创建更深层目录。

```text
coding-agent/
  apps/
    backend/
      app/
        api/
        config/
        context/
        events/
        logging/
        models/
        runtime/
        storage/
        tools/
      tests/
    desktop/
  packages/
    shared/
  docs/
  rules/
  coding-agent-docs/
  scripts/
  logs/
  storage/
```

目录职责：

- `apps/`：可运行应用集合。
- `apps/backend/`：本地 Python 后端应用，承载 FastAPI、LangGraph、Agent Runtime、工具系统、存储、日志等后端能力。
- `apps/backend/app/`：后端应用源码根目录，按已进入实现的能力边界拆分模块。
- `apps/backend/app/api/`：FastAPI 路由、SSE 格式化和 API 依赖组装。
- `apps/backend/app/config/`：后端运行配置，例如项目根目录、日志文件、SQLite 文件和运行限制。
- `apps/backend/app/context/`：构建模型无关的文本上下文。
- `apps/backend/app/events/`：Runtime 事件定义和事件序列化。
- `apps/backend/app/logging/`：日志落盘配置。
- `apps/backend/app/models/`：模型适配器接口、本地流式测试适配器、OpenAI-compatible / DeepSeek streaming adapter 和流式响应解析。
- `apps/backend/app/runtime/`：Agent Runtime，负责任务状态推进、模型流消费、工具调度、事件记录、取消和终止保护。
- `apps/backend/app/storage/`：SQLite 持久化存储，保存 Session / Task / Turn / Step / Event。
- `apps/backend/app/tools/`：Tool Registry、Tool Scheduler 和 safe_read 内置工具。
- `apps/backend/tests/`：后端测试目录。
- `apps/desktop/`：Tauri 2 + React + TypeScript 桌面客户端，承载会话、任务、审批、工具调用、变更展示、日志入口等 UI。
- `packages/shared/`：前后端共享协议、schema、类型和事件契约。涉及 HTTP/SSE、工具调用、审批、checkpoint、运行事件等跨端数据结构时优先放在这里。
- `docs/`：项目愿景、需求、技术栈、架构、验收标准和后续设计文档。
- `rules/`：项目级协作规则、代码开发规范、交互澄清规则和经验记录。
- `coding-agent-docs/`：成熟 coding-agent 的原理资料库，只作为设计和开发参考，不混入产品源码。
- `scripts/`：开发、检查、构建、生成 schema、维护数据等辅助脚本。
- `logs/`：本地日志约定目录。可运行系统必须有明确日志落盘位置；日志文件可在运行时生成。
- `storage/`：本地 SQLite 等运行状态文件约定目录。数据库文件是运行产物，不应提交。

当前已确认的概念边界：

- `Agent` 是执行主体，描述角色、目标、上下文、工具权限、状态和运行记录。
- `Workflow` 是执行策略，描述 Agent 如何完成任务，例如 ReAct-like、Plan-and-Execute、Review-Fix。
- `Runtime` 是执行底座，负责状态管理、模型调用、工具调度、审批、checkpoint、context compaction、事件流、取消、恢复和终止保护。
- `Subagent` 不应实现成一套平行系统；它应作为 child agent / child run 复用 Agent、Workflow 和 Runtime 能力。

## 开发前必须路由

根据任务类型读取对应文档，不要把所有文档一次性塞进上下文。

- 项目想法和讨论事实源：`docs/idea-requirements.md`
- 技术栈与运行形态：`docs/tech-stack.md`
- Agent Runtime 与 Agent Loop 定位：`docs/agent-runtime-loop.md`
- 客户端 UI 指南：`docs/ui-guidelines.md`
- 第一阶段能力与生产级验收：`docs/production-acceptance.md`
- 代码开发规范：`rules/Agent代码开发规范.md`
- 交互澄清规则：`rules/global-interaction-clarification.md`
- 成熟机制复用规则：`rules/mature-mechanism-reuse.md`
- 经验复用记录：`rules/agent-lessons.md`
- coding-agent 原理文档：`coding-agent-docs`

## 进入代码开发后的铁律

进入代码开发后，必须遵守 `rules/Agent代码开发规范.md`。摘要如下：

- 单一职责：一个文件只做一件事，按职责而非行数判定。
- 不重复造轮子：能用成熟方案就不自己写。
- 改动最小化：改一行能解决的不改十行。
- 目录结构清晰：开发过程中可以持续拆分文件、拆分目录；目标是不看代码，只看目录就能知道项目能力模块和职责边界。
- 可排查日志：系统中必须存在可排查问题的日志文件。
- 函数 docstring：每个函数都必须有完整 docstring，并且随着函数修改同步更新。
- 开发完成后必须形成审查和测试闭环；开发 Agent 不能既当开发又当裁判。

## 非愿景

- 不是先做一个命令行工具。
- 不是一次性复刻某个现有 coding-agent 的产品形态。
- 不是把 Agent 固定为单一 ReAct 流程。
- 不是把 Agent Loop 等同于 ReAct；ReAct 是行为范式，Agent Loop 是运行时控制机制。
- 不是一开始就做大量未经验证的个人化抽象。
- 不是只做代码补全或聊天问答。
- 不是为了兼容旧系统而牺牲设计清晰度。
- 不是追求“尽快生成代码”，而是追求“长期稳定地完成开发任务”。

## 当前开放问题

- DeepSeek 之后的大模型接入顺序。
- LangGraph 的使用深度：如何承载 workflow 扩展、checkpoint、interrupts、streaming、subgraphs 等能力。
- 第一阶段各能力的验收标准和优先级排序。

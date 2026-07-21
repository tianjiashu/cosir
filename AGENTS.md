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
- 技术底座：Tauri 2 + React + TypeScript + Vite + Python + FastAPI + LangGraph（强依赖编排底座）+ SQLite。
- 模型接入：第一阶段优先支持 OpenAI 协议，优先适配 DeepSeek，后续陆续接入其他大模型。
- UI 风格：参考 Codex 桌面客户端，使用 shadcn/ui + Radix UI + Tailwind CSS + lucide-react。
- 第一阶段必须覆盖 MCP、工具权限审批、checkpoint、subagent、context compaction、工具系统、任务执行闭环、审查与测试闭环。
- MCP、checkpoint、subagent、context compaction 等核心能力第一版必须按生产级深度设计和验收。
- 第一版必须预留 Agent Workflow、context compaction、subagent、tool 的扩展能力，不能锁死为单一 ReAct 流程。
- ReAct 只能作为第一版默认 ReAct-like Workflow 的候选形式；Workflow 编排层强依赖 LangGraph（`StateGraph` + `SqliteSaver` checkpoint + `interrupt()` 审批中断 + `Command(resume=)` 恢复 + subgraph/`Send` subagent），底层仍是可扩展 Agent Runtime，支持后续替换或新增 Workflow。
- 工具注册与执行保持自定义，不绑定 LangGraph `Tool`：`ToolDefinition` 是工具契约的单一事实来源，LangGraph graph 的 node 调用自定义 `ToolRuntime`，工具定义不被编排框架绑架。
- LangGraph 为硬依赖，不再保留「缺失即降级为无 checkpoint 模式」的回退分支；最低运行环境要求 Python 3.10+（与 `requirements.txt` 中 `langgraph` 的版本门控一致）。

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
        api/                 # FastAPI 路由与 SSE
        bootstate.py         # 启动状态初始化
        config/              # 运行配置
        core/                # 运行底座
          agents/            # Agent 角色与配置
          context/           # 文本上下文构建
          events/            # 运行时事件定义
          logs/              # 日志查询服务
          runs/              # LangGraph 持久化运行
          runtime/           # Agent Runtime 控制与编排
          workflows/         # Agent 执行策略
        models/              # 业务层 model 定义（一文件一 model，平铺）
        service/             # 领域服务（编排层，仅 xxx_service）
          tool_execution/    # 工具执行编排
          trace/             # 运行追踪服务（仅 xxx_service）
        trace_infra/         # 新建顶层包：trace 基础设施原语（ids/event_names/redaction）
        llm/                 # 新建顶层包：LLM 适配与桥接（factory/langchain_bridge）
        storage/             # SQLite 持久化与 checkpoint 快照
        tools/               # 工具系统
          schemas/           # 核心契约值对象
          tool_execute/      # 工具执行层
          tool_handler/      # 具体工具实现
          tool_models/       # 各工具 pydantic 参数/结果模型
          tool_registry.py   # 工具注册/查询/导出
          tool_system.py     # 工具容器与装配
          validation/        # 参数校验
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
- `apps/backend/app/bootstate.py`：后端启动状态初始化（配置、存储、工具系统）。
- `apps/backend/app/config/`：后端运行配置，例如项目根目录、日志文件、SQLite 文件和运行限制。
- `apps/backend/app/core/`：运行底座，聚合运行态相关模块。
  - `agents/`：Agent 角色定义与默认配置（AgentProfile）。
  - `context/`：构建模型无关的文本上下文。
  - `events/`：Runtime 事件定义和事件序列化。
  - `logs/`：日志查询服务。
  - `runs/`：LangGraph 持久化运行，包含 checkpointer、状态机、恢复、resume 与 graph 构建。
  - `runtime/`：Agent Runtime，负责任务状态推进、模型流消费、工具调度、事件记录、取消和终止保护。
  - `workflows/`：Agent 执行策略，例如 ReAct-like、Plan-and-Execute、StepController 等，可扩展替换。
- `apps/backend/app/models/`：业务层 model 定义（dataclass / 枚举值对象），一个文件一个 model，文件名与 model 相关，平铺组织。不承载服务、适配或 helper 逻辑。
- `apps/backend/app/service/`：领域服务（编排层），只放服务编排类，文件名与类名必须为 `xxx_service.py` / `XxxService`。不承载 model、常量或 helper。
  - `tool_execution/`：工具执行编排，负责 agent 级可见性策略、生命周期事件记录、模型消息编解码（service / codec / run_result）。
  - `trace/`：运行追踪服务（仅 `trace_query_service` / `trace_recorder`）。
- `apps/backend/app/trace_infra/`：trace 基础设施原语（ID 生成/校验、事件名规范化、payload 脱敏）。不依赖 `app.models` 与 `app.service`，避免循环依赖。
- `apps/backend/app/llm/`：LLM 适配与桥接（chat model 构建、运行时消息/工具 schema 边界转换）。只依赖 `app.models.runtime_message` 与 `app.tools.schemas`，不依赖 service 编排层。
- `apps/backend/app/storage/`：SQLite 持久化存储，保存 Session / Task / Turn / Step / Event 以及 checkpoint 快照。
- `apps/backend/app/tools/`：工具系统统一收口。
  - `schemas/`：核心契约值对象（ToolDefinition / ToolCall / ToolObservation / ModelToolDefinition）。
  - `tool_execute/`：工具执行层，负责权限校验、参数校验、子进程隔离执行与超时强杀（tool_scheduler / tool_executor / tool_error / tool_success）。
  - `tool_handler/`：具体工具实现，每个工具独立文件，承载执行逻辑与 ToolDefinition 组装。
  - `tool_models/`：各工具的 pydantic 参数与结果模型（如 ReadFileArgs / TextReadResult）。
  - `tool_registry.py`：工具注册、查询与导出。
  - `tool_system.py`：工具容器，装配内置工具为可运行系统。
  - `validation/`：工具参数校验（pydantic + jsonschema 双路）。
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
- 代码开发规范：`rules/Agent代码开发规范.md`
- Python 代码开发规范（后端工具链/Ruff/mypy/uv 补充）：`rules/Python代码开发规范.md`
- 客户端代码开发规范（Tauri/React/TS 派生附录）：`rules/Agent客户端代码开发规范.md`
- 交互澄清规则：`rules/global-interaction-clarification.md`
- 成熟机制复用规则：`rules/mature-mechanism-reuse.md`
- 经验复用记录：`rules/agent-lessons.md`
- coding-agent 原理文档：`coding-agent-docs`
- 日志规范：`rules/Agent日志开发规范.md`

## CodeGraph 使用规则

当前仓库根目录存在 `.codegraph/` 时，说明本项目已经有 CodeGraph 索引。凡是需要理解代码结构、定位符号、追踪调用关系、分析影响范围或查找实现位置，应先用 CodeGraph 缩小范围，再精读必要源码。

使用顺序：

1. **MCP 优先**：如果当前环境提供 `codegraph_explore`、`codegraph_node` 等 MCP 工具，优先使用 MCP 工具。
2. **Shell 兜底**：如果 MCP 工具不可用，使用本机 `codegraph` CLI。
3. **源码精读**：CodeGraph 用于定位和建立调用视角，最终判断仍以实际源码为准。
4. **传统搜索兜底**：如果 `.codegraph/` 不存在、索引不可用、CodeGraph 命令失败，才回退到 `rg`、`find` 和直接读文件。

常用命令：

```bash
codegraph status
codegraph files
codegraph explore "要理解的模块、符号、文件或问题"
codegraph node "符号名或文件路径"
codegraph callers "函数或方法名"
codegraph callees "函数或方法名"
codegraph impact "准备修改的符号名"
codegraph affected <changed-file>
codegraph sync
```

使用要求：

- 查询必须具体，优先写清楚文件名、符号名、模块名或要解决的问题。
- 修改代码前，如果改动涉及已有实现、跨模块调用、共享类型或公共工具，先用 `codegraph explore` 或 `codegraph impact` 判断影响范围。
- 修改后如需要继续依赖索引，先运行 `codegraph sync` 更新索引，再做后续查询。
- 不要在已有可用索引的仓库里一开始就大范围 grep 或逐文件扫描；先让 CodeGraph 给出候选范围。
- 不要把 CodeGraph 输出当作唯一事实源；涉及行为、边界条件、配置和测试时，必须回到文件本身验证。

环境限制：

- CodeGraph 对 Node 25/26 存在已知拦截风险。若命令提示当前 Node 版本不支持，不要用 `CODEGRAPH_ALLOW_UNSAFE_NODE=1` 强行索引；应切换到 Node 22 LTS 后再执行 `codegraph index` 或 `codegraph sync`。
- 如果只是当前任务需要继续推进、且无法立即切换 Node 版本，可以临时回退到 `rg` 和源码精读，但交付时应说明 CodeGraph 未能使用的原因。

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
- （已决议）LangGraph 使用深度：作为强依赖编排底座，承载 workflow 扩展、checkpoint、interrupts、streaming、subgraphs；工具执行层仍自定义。若未来 LangGraph 无法满足特定需求（如极致自定义调度、客户端体积），再评估抽离 graph 层接口。
- 第一阶段各能力的验收标准和优先级排序。

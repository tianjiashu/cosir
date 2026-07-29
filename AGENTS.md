# AGENTS.md

本文件是 `coding-agent` 项目的长期 Agent 入口指南。它保留项目愿景、不可变决议、协作原则、当前目录结构与职责，以及**已落地的项目约定**；详细规则放在 `docs/` 和 `rules/` 下。

> 当前项目已**进入代码开发阶段**（后端 FastAPI + LangGraph 主体骨架与日志子系统已落地，桌面端 Tauri 2 框架已搭起）。本文件的目录结构与约定以**当前真实代码为准**；`rules/目录组织规范.md` 已于 2026-07-26 与真实代码全量对齐，两者应保持一致，冲突时以真实代码为准并同步修订两份文档。

---

## 一、项目愿景

打造一个面向个人开发者的本地桌面 coding-agent 底座。

它参考成熟 coding-agent 的工程机制，但不绑定单一 Agent 范式；它允许用户持续定制 Workflow、Context、Tool 和开发规则，最终演化成符合个人开发习惯的长期协作型工程伙伴。

第一阶段先按能力维度取长补短，复刻先进 coding-agent 的生产级核心能力；第二阶段通过真实使用发现问题，再围绕用户个人开发习惯做定制开发。

## 二、不可变核心决议

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
- LangGraph 为硬依赖，不再保留「缺失即降级为无 checkpoint 模式」的回退分支；最低运行环境要求 Python 3.11+（与 `pyproject.toml` 的 `requires-python>=3.11` 及 `.python-version` 一致）。

## 三、Agent 协作原则

- Agent 不是单纯执行器，应作为工程协作伙伴参与判断。
- Agent 应围绕项目愿景主动提出建议、风险提醒和取舍方案。
- 建议必须区分“必须做”“建议做”“以后做”，避免无边界发散。
- 如果用户想法可能偏离愿景，Agent 应温和指出，并给出更贴近愿景的替代方案。
- 不把候选建议写成已确认决议。
- 高影响决策必须等用户确认后再升级为决议。
- 大知识库只服务于决策质量，不制造上下文噪音；需要筛选、压缩、对齐当前阶段。

## 四、当前目录结构与职责

目录结构以**当前真实代码**为准。只有能力进入实现、职责边界明确时才增量创建深层目录；不看代码、只看目录就能知道项目能力模块与职责边界。

```text
coding-agent/
  .pre-commit-config.yaml # 提交前强制 ruff-format + ruff --fix + mypy + 自动导出 OpenAPI 文档（配置位于仓库根，不在 apps/backend）
  apps/
    backend/
      pyproject.toml          # 唯一依赖来源（uv）；Ruff/mypy/pytest 配置集中于此
      uv.lock                 # 锁文件，必须提交
      .python-version         # 3.11
      app/
        __main__.py           # CLI 入口，触发 config.logging 与 default_settings
        main.py               # 进程入口，create_app 装配 FastAPI
        bootstate.py          # 启动状态初始化（配置、存储、工具系统）
        api/                  # FastAPI 接入层
          app.py              # 应用装配入口、lifespan、中间件
          agents_api.py / tasks_api.py / turns_api.py / workspaces_api.py / logs_api.py
          depends/dependencies.py   # FastAPI 依赖接线（runtime/tool_system 单例）
          middleware/api_logging.py # 请求/异常日志中间件
          schemas/request/         # API Pydantic 请求模型（一文件一模型；文件名 PascalCase，待确认，见 D12）
          schemas/response/        # API Pydantic 响应模型（一文件一模型；同上）
        config/
          settings.py         # 不可变 BackendSettings 值对象 + default_settings()
          logging/            # 日志子系统聚合包（特例：允许依赖 storage/trace_infra）
            logger.py         # log 单例 + install_msg_relocation（支持 extra["msg"]）
            configuration.py  # configure_logging / install_logging_for_current_process（装配）
            process_bridge.py # get_log_queue / SubprocessQueueHandler（跨进程队列桥）
            common.py         # 日志子系统常量
            context/log_context_store.py   # LogContext 的 ContextVar 存储
            filter/caller_filter.py        # 注入调用方位置 caller
            filter/log_context_filter.py   # 回填 trace_id 等链路键
            formatter/jsonl_formatter.py   # LogRecord → 单行 JSON（9 字段）
            handler/sqlite_handler.py     # 异步写入独立日志库
        core/                 # Agent 运行底座，全基于 LangGraph 体系
          agents/agent_profile.py / agent_profile_registry.py
          context/budget.py（预算校验）/ text_context_builder.py（TextContextBuilder）
          llm/factory.py（build_chat_model）/ langchain_bridge.py / model_settings.py
          llm/llm_provider/base.py / deepseek_provider.py  # OpenAI 协议接入 DeepSeek
          runtime/runner.py（AgentRuntime 总控）/ runtime_operations.py（窄边界门面）
          runtime/runs/checkpointer.py   # LangGraph AsyncSqliteSaver 持久化
          workflows/agent_workflow.py（Protocol）
          workflows/react/    # StateGraph 实现：state / nodes / edges / workflow / runtime_config
        models/               # 业务层值对象（一文件一 model，文件名=类名）
          log_entry_record.py / log_query.py / log_query_result.py / mapped_log_record.py
          runtime_event.py / runtime_message.py / task_record.py / trace_context.py
          turn_record.py / workspace_record.py
          enums/event_type.py / enums/turn_status.py
          payload/            # 运行时事件 payload 值对象（一文件一 payload，共 19 个：run_* / step_started / model_* / tool_call_* / observation_added / human_input_* / final_response / runtime_event_payload 基类型）
        service/              # 领域服务编排层（仅 xxx_service + 结果值对象）
          task/task_service.py / turn_service.py / workspace_service.py
          tool_execution/tool_execution_service.py / run_result.py
          log_query_service.py   # 日志查询业务服务（组合 SQLite 日志库与 JSONL）
        storage/              # SQLite 数据层
          engine_cache.py / init_schema.py / store_engines.py
          crud/log_crud.py / runtime_event_crud.py / task_crud.py / turn_crud.py / turn_message_crud.py / workspace_crud.py
          model/base.py / log_model.py / runtime_event_model.py / task_model.py / turn_message_model.py / turn_model.py / workspace_model.py
        tools/                # 工具系统（不基于 LangGraph）
          schemas/tool_definition.py（契约单一事实来源）/ tool_call.py / tool_observation.py / tool_display.py / tool_execution_context.py
          tool_execute/tool_scheduler.py（执行固定入口）/ tool_executor.py（子进程隔离+超时强杀）
          tool_execute/tool_error.py / tool_success.py（构造 ToolObservation 的纯工厂）/ windows_job_object.py（Windows 子进程树强杀）
          tool_handler/       # 7 个内置工具：read_file / write_file / patch_tool / search_files / list_directory / delete（文件目录统一）/ execute_terminal
            file_io/atomic_write.py       # 原子写支撑
            patch/            # 补丁解析/差异/应用/模糊匹配
            search/           # 内容/文件名搜索与遍历
            security/project_path.py      # ProjectPathResolver：路径边界唯一收口
            terminal/         # 终端执行后端 + 危险命令 deny-list（删除类命令全面硬拒）
          tool_models/        # 各工具 pydantic 参数/结果模型（一工具一 args 文件 + text_read_result.py）
          validation/arguments.py     # 参数校验唯一收口
          guard/             # 工具执行横切子层（语法检查守卫等跨工具共享横切能力，依赖白名单同 tools/）
          tool_registry.py / tool_system.py
        trace_infra/          # trace 基础设施原语（leaf：零 app.* 依赖）
          ids.py / redaction.py
      tests/                  # pytest 测试目录（test_*.py）
      temp/                   # 临时验证/调试脚本（已被 .gitignore 忽略，用完清理）
    desktop/                  # Tauri 2 + React + TS 桌面客户端
      src/                    # React 前端（会话/任务/审批/工具调用/变更展示/日志入口）
      src-tauri/              # Rust 宿主（窗口、系统能力、与后端通信）
      index.html / vite.config.ts / tailwind.config.ts / components.json（shadcn/ui）
  packages/
    shared/                   # 前后端共享协议、schema、类型、事件契约
  docs/                       # 项目愿景/需求/技术栈/架构/验收/设计文档
  rules/                      # 项目级协作规则、代码开发规范、交互澄清、经验记录
  coding-agent-docs/          # 成熟 coding-agent 原理资料库（仅参考，不混入源码）
  scripts/                    # 开发/检查/构建/生成 schema/维护数据等辅助脚本
  logs/                       # 本地日志落盘目录（运行时生成）
  storage/                    # 本地 SQLite 运行状态文件目录（运行产物，不提交）
```

目录职责要点：

- `apps/backend/app/api/`：FastAPI 路由、SSE 格式化和 API 依赖组装（接入层）。**网关约定**：`api/app.py` 必须用 `importlib.import_module("app.api.xxx")` 触发路由注册，不能用 `import app.api.xxx`，否则顶层包名 `app` 被覆盖为模块对象。
- `apps/backend/app/config/logging/`：日志子系统聚合包（配置 + 运行时 filter/formatter/handler/bridge/context）。属配置层特例，明确允许依赖 `storage` / `trace_infra`，豁免通用 config 轻量约束；路径保持 `app.config.logging` 不拆分。
- `apps/backend/app/core/`：Agent 运行底座，**全基于 LangGraph 体系**（LangChain 为 LangGraph 的硬依赖基座）。`core/logs/`、`core/events/` 不存在；运行时事件定义在 `models/runtime_event.py`（payload 在 `models/payload/`），日志查询服务在 `service/log_query_service.py`。
- `apps/backend/app/models/`：业务层值对象（dataclass / 枚举），一文件一 model，文件名与 model 同名；主体平铺，枚举归入 `enums/`，运行时事件 payload 归入 `payload/`。不承载服务、适配或 helper。
- `apps/backend/app/service/`：领域服务编排层，只放 `xxx_service`（及结果值对象 `run_result.py`）；不直接写 SQL、不承载 model/原语。
- `apps/backend/app/storage/`：SQLite 持久化存储，承载引擎缓存、schema 初始化（含列迁移）、进程级引擎装配、ORM 模型与单实体 CRUD（数据层最底层）。
- `apps/backend/app/tools/`：工具系统统一收口，**不基于 LangGraph**；内置 7 工具（read_file / write_file / patch / search_files / list_directory / delete / execute_terminal）经 `core` 调度执行。`ToolDefinition` 是契约单一事实来源，模型可见结构由 `ToolDefinition.to_model_tool_definition()` 投影，禁止平行类；文件类工具路径安全统一收口 `tool_handler/security/project_path.ProjectPathResolver`（workspace 外写/删被 path_escape 强制拒绝）。
- `apps/backend/app/trace_infra/`：trace 基础设施原语（ID 生成/校验、payload 脱敏）。不依赖 `app.models` 与 `app.service`，避免循环依赖。

当前已确认的概念边界：

- `Agent` 是执行主体，描述角色、目标、上下文、工具权限、状态和运行记录。
- `Workflow` 是执行策略，描述 Agent 如何完成任务，例如 ReAct-like、Plan-and-Execute、Review-Fix。
- `Runtime` 是执行底座，负责状态管理、模型调用、工具调度、审批、checkpoint、context compaction、事件流、取消、恢复和终止保护。
- `Subagent` 不应实现成一套平行系统；它应作为 child agent / child run 复用 Agent、Workflow 和 Runtime 能力。
- `core` 是 LangGraph 编排内核，承载 Agent 运行底座；`tools` 不基于 LangGraph，由 `core` 调度执行。

## 五、项目约定（已落地、强制）

以下约定已从 `rules/` 与真实代码反推后固化，是开发 Agent 提交前的事实基线；详细条款见文末「开发前必须路由」指向的规范文件。

### 1. Python 代码与命名
- **一文件一类**：文件名 = 主类名 snake_case（如 `CallerFilter` ↔ `caller_filter.py`）。允许例外：服务类用 `xxx_service.py` / `xxx_recorder`；值对象工厂类、bridge、utils 等工具模块可承载多个强相关公共符号（如 `*_bridge.py`、`log_query_service.py`）。
- **值对象自带工厂方法**：从源对象（如 `logging.LogRecord`）到领域值对象的映射逻辑，收进值对象自身的 `from_xxx` 类方法（如 `MappedLogRecord.from_record(record)`），不单独保留无状态的 Mapper 类。
- **docstring 四段式**：每个函数/方法必须有完整中文 docstring，采用「参数 / 返回 / 异常 / 副作用」Google 风格；函数签名变更时同步更新。
- **工具链（单一来源）**：依赖与锁由 `uv` 管理（`pyproject.toml` + 已提交 `uv.lock`，无 `requirements.txt`）；Ruff（行宽 100、双引号）做 format+lint+import 排序，mypy 做渐进类型检查，pre-commit 提交前强制。**新代码**必须 Ruff 通过、带完整类型注解、无 `import *`。
- **导入纪律**：绝对导入 `from app.xxx import yyy`，不使用相对导入跨包；`__init__.py` 只做薄壳 `from .X import Y` re-export，不放业务逻辑；禁止 `import *`。
- **命名禁止模糊词**：`Utils` / `Helper` / `Common` / `Misc` / `Manager` 等笼统命名不允许（约定别名 `log` / `log_query_service` 等除外）。

### 2. 目录与依赖方向
- **单向依赖总纲**（详细见 `rules/目录组织规范.md` 第一章）：`api → core/service`；`core → service/tools/models/config/trace_infra`；`service → storage/models/config/trace_infra/tools`（tool_execution 编排 tools 合法）；`storage → models`；`tools → config/models/trace_infra`（不依赖 service）；`models → utils/trace_infra`（leaf）；`config` 通用轻量，`config/logging` 聚合特例允许依赖 `storage/trace_infra`。
- **禁止跳层 / 反向依赖**：如 `api` 直 import `storage`/`tools`、`tools → service`、`models → 编排层` 均为违规。

### 3. 日志约定（强制）
- **业务模块统一单例**：一律 `from app.config.logging.logger import log`，**禁止**在业务代码散落 `logging.getLogger("coding_agent.backend")`——同名 `getLogger` 虽返回同一对象，但分散写法易在 name typo 时静默新建分裂 logger。仅 `logger.py`（定义源头）、`configuration.py`（装配 handler）、`process_bridge.py`（桥接 handler）三类子系统内部文件保留 `getLogger` 装配职责。
- **跨进程**：spawn 子进程经 `process_bridge` 队列桥汇入父进程统一管线（子进程入口用 `install_logging_for_current_process(log_queue=...)` 重新挂载），不共享 logger 对象。
- **统一格式**：落盘为单行 JSON（JSONL），固定 9 字段；`event` 稳定英文 snake_case（禁 f-string 拼动态）、`msg` 中文人读、`data` 结构化业务字段、**唯一链路键 `trace_id`**（业务实体 ID 值放 `data`/`msg`，不进顶层）。详见 `rules/Agent日志开发规范.md`。
- **异常纪律**：捕获异常必须用 `.exception()` 带堆栈并 `raise`（底层重抛让上层补上下文）；禁止空 `except`、禁止 `print` 当系统日志、禁止输出 secret。

### 4. 临时脚本约定
- 验证 / 调试脚本统一放 `apps/backend/temp/`（已在 `.gitignore` 忽略），禁止散落项目根或其他位置；**用完及时清理，避免误提交**。

### 5. 测试与闭环
- pytest；`tests/` 下 `test_*.py`，`pytest-asyncio` `asyncio_mode=auto`；关键路径（工具执行、checkpoint、审批、日志）必须有测试。
- 开发完成后必须形成**审查与测试闭环**；开发 Agent 不能既当开发又当裁判（见第六章铁律）。

## 六、进入代码开发后的铁律

进入代码开发后，必须遵守 `rules/Agent代码开发规范.md`（通用）与 `rules/Python代码开发规范.md`（Python 专属）。摘要如下：

- 单一职责：一个文件只做一件事，按职责而非行数判定。
- 不重复造轮子：能用成熟方案就不自己写。
- 改动最小化：改一行能解决的不改十行。
- 目录结构清晰：开发过程中可持续拆分文件/目录；目标是不看代码，只看目录就知道能力模块与职责边界。
- 可排查日志：系统中必须存在可排查问题的日志文件。
- 函数 docstring：每个函数必须有完整 docstring，且随函数修改同步更新。
- 开发完成后必须形成审查和测试闭环；开发 Agent 不能既当开发又当裁判。

## 七、开发前必须路由

根据任务类型读取对应文档，不要把所有文档一次性塞进上下文。

- 项目想法和讨论事实源：`docs/idea-requirements.md`
- 通用代码开发规范：`rules/Agent代码开发规范.md`
- Python 代码开发规范（uv/Ruff/mypy/pre-commit 补充）：`rules/Python代码开发规范.md`
- 客户端代码开发规范（Tauri/React/TS 派生附录）：`rules/Agent客户端代码开发规范.md`
- 日志开发规范（log 单例、JSONL 9 字段、event/msg/data 纪律）：`rules/Agent日志开发规范.md`
- 目录组织规范（分层依赖方向契约、各目录职责）：`rules/目录组织规范.md`
- 交互澄清规则：`rules/global-interaction-clarification.md`
- 成熟机制复用规则：`rules/mature-mechanism-reuse.md`
- 经验复用记录：`rules/agent-lessons.md`
- coding-agent 原理文档：`coding-agent-docs`
- 设计/计划类文档（部分已落地）：`docs/`（含 `logging-guide.md`、`refactor-state-model-and-checkpoint.md`、`工具系统待完善清单.md` 等）

## 八、CodeGraph 使用规则

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
- 修改代码前，若改动涉及已有实现、跨模块调用、共享类型或公共工具，先用 `codegraph explore` 或 `codegraph impact` 判断影响范围。
- 修改后如需要继续依赖索引，先运行 `codegraph sync` 更新索引，再做后续查询。
- 不要在已有可用索引的仓库里一开始就大范围 grep 或逐文件扫描；先让 CodeGraph 给出候选范围。
- 不要把 CodeGraph 输出当作唯一事实源；涉及行为、边界条件、配置和测试时，必须回到文件本身验证。

环境限制：

- CodeGraph 对 Node 25/26 存在已知拦截风险。若命令提示当前 Node 版本不支持，不要用 `CODEGRAPH_ALLOW_UNSAFE_NODE=1` 强行索引；应切换到 Node 22 LTS 后再执行 `codegraph index` 或 `codegraph sync`。
- 如果只是当前任务需要继续推进、且无法立即切换 Node 版本，可以临时回退到 `rg` 和源码精读，但交付时应说明 CodeGraph 未能使用的原因。

## 九、非愿景

- 不是先做一个命令行工具。
- 不是一次性复刻某个现有 coding-agent 的产品形态。
- 不是把 Agent 固定为单一 ReAct 流程。
- 不是把 Agent Loop 等同于 ReAct；ReAct 是行为范式，Agent Loop 是运行时控制机制。
- 不是一开始就做大量未经验证的个人化抽象。
- 不是只做代码补全或聊天问答。
- 不是为了兼容旧系统而牺牲设计清晰度。
- 不是追求“尽快生成代码”，而是追求“长期稳定地完成开发任务”。

## 十、已知偏差与开放问题

### 已知偏差（待修复，详见 `rules/目录组织规范.md` 1.4 节，2026-07-26 对齐）
- **D5（跳层，仍存在且范围扩大）**：`api/depends/dependencies.py`、`api/app.py` 直 import `app.storage.*` / `app.tools.tool_system`（装配点）；`api/tasks_api.py` 直用 `RuntimeEventCrud`、`api/logs_api.py` 直用 `LogStore`（端点级跳层，应优先经 service 收口）。
- **D10（空壳文件，待清理）**：`tools/tool_handler/delete_file.py` / `delete_directory.py`、`tools/tool_models/delete_file_args.py` / `delete_directory_args.py`、`models/tool_execution_context.py` 为 0 字节残留，应删除。
- **D11（空目录，待清理）**：`config/logging/save/`、`service/trace/` 仅剩 `__pycache__`，应删除。
- **D12（命名待确认）**：`api/schemas/request|response` 文件名用 PascalCase（如 `CreateTaskRequest.py`），与 snake_case 约定不一致，待确认豁免或改名。

### 文档待同步
- 无。`rules/目录组织规范.md` 已于 2026-07-26 与真实代码全量对齐（trace 服务层/`event_names.py`/`durable_crud` 等已移除项、storage 重构后文件名、tools 7 工具与支撑子包、models/payload 均已更新）。

### 开放问题
- DeepSeek 之后的大模型接入顺序。
- （已决议）LangGraph 使用深度：作为强依赖编排底座，承载 workflow 扩展、checkpoint、interrupts、streaming、subgraphs；工具执行层仍自定义。若未来 LangGraph 无法满足特定需求（如极致自定义调度、客户端体积），再评估抽离 graph 层接口。
- 第一阶段各能力的验收标准和优先级排序。

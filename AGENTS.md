# AGENTS.md

本文件是 `coding-agent` 项目的长期 Agent 入口指南。它保留项目愿景、不可变决议、协作原则、当前目录结构与职责，以及**已落地的项目约定**；详细规则放在 `docs/` 和 `rules/` 下。

> 当前项目处于代码开发阶段（后端 FastAPI + LangGraph 运行时、自定义工具系统（17 个内置工具）、日志子系统、Langfuse 可观测性、Web 工具均已落地；delegation 子 Agent、Hook 系统、CodeGraph 集成、context compaction、file snapshot / change set 检查点、语法检查已落地；桌面端 Tauri 2 + React 前端与 Rust 后端托管 supervisor 已搭起）。本文件的目录结构与约定以**当前真实代码为准**；`rules/目录组织规范.md` 最近一次全量对齐停留在 2026-08-04，**已滞后于当前代码**（仍引用 `main.py`、`api/app.py`、`api/depends/`、`service/runtime_event/`、顶层 `app/trace_infra`、9 工具等旧状态），两者冲突时以真实代码为准，待其重新对齐后同步修订本文件。本文件已于 2026-08-17 与真实代码全量对齐。

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
- 桌面端支持并发执行：可同时运行多个 task，不同 task 的 turn 并发执行，无论它们是否属于同一 workspace；同一 task 内 turn 保持串行（pending → 认领乐观锁，任意时刻至多一个 running turn）。
- MCP、checkpoint、subagent、context compaction 等核心能力第一版必须按生产级深度设计和验收。
- 第一版必须预留 Agent Workflow、context compaction、subagent、tool 的扩展能力，不能锁死为单一 ReAct 流程。
- ReAct 只能作为第一版默认 ReAct-like Workflow 的候选形式；Workflow 编排层强依赖 LangGraph（`StateGraph` + SqliteSaver checkpoint + `interrupt()` 审批中断 + `Command(resume=)` 恢复 + subgraph/`Send` subagent），底层仍是可扩展 Agent Runtime，支持后续替换或新增 Workflow。
- 工具注册与执行保持自定义，不绑定 LangGraph `Tool`：`ToolDefinition` 是工具契约的单一事实来源，LangGraph graph 的 node 调用自定义 `ToolRuntime`，工具定义不被编排框架绑架。
- LangGraph 为硬依赖，不再保留「缺失即降级为无 checkpoint 模式」的回退分支；最低运行环境要求 Python 3.11+（与 `pyproject.toml` 的 `requires-python>=3.11` 及 `.python-version` 一致）。
- 前后端共享协议落地于 `apps/shared/ts/`（通过 Vite alias `@shared` 引用），不放在 `packages/`。

## 三、Agent 协作原则

- Agent 不是单纯执行器，应作为工程协作伙伴参与判断。
- Agent 应围绕项目愿景主动提出建议、风险提醒和取舍方案。
- 建议必须区分"必须做""建议做""以后做"，避免无边界发散。
- 如果用户想法可能偏离愿景，Agent 应温和指出，并给出更贴近愿景的替代方案。
- 不把候选建议写成已确认决议。
- 高影响决策必须等用户确认后再升级为决议。
- 大知识库只服务于决策质量，不制造上下文噪音；需要筛选、压缩、对齐当前阶段。

## 四、当前目录结构与职责

目录结构以**当前真实代码**为准。只有能力进入实现、职责边界明确时才增量创建深层目录；不看代码、只看目录就能知道项目能力模块与职责边界。

```text
coding-agent/
  .pre-commit-config.yaml # 仓库根（git 根）pre-commit：ruff-format + ruff --fix + mypy(非阻塞) + mypy-new-strict(新增文件阻塞) + generate-openapi + generate-runtime-event-ts
  package.json            # 仓库根脚本集合（dev:all / dev:client）
  CHANGELOG.md            # 变更记录
  apps/
    backend/
      pyproject.toml      # 唯一依赖来源（uv）：fastapi/langgraph/langchain/langchain-litellm(经 ChatLiteLLM 接 DeepSeek)/litellm==1.97.0/langfuse/tree-sitter 系列；Ruff/mypy(strict+存量豁免)/pytest/coverage 配置集中于此
      uv.lock             # 锁文件，必须提交
      .python-version     # 3.11
      .env.example        # 环境变量样例：CODING_AGENT_MODEL_*（openai-compatible + DeepSeek）、DEEPSEEK_API_KEY、LANGFUSE_*、CODING_AGENT_CODEGRAPH_NODE
      mypy.strict.ini     # 新增文件强类型门禁专用（无存量豁免）
      scripts/            # generate_openapi.py / mypy_new_strict.sh / mypy_nonblocking.sh（pre-commit hook 引用）
      app/
        __main__.py       # CLI 入口（python -m app），触发 config.logging 与 Settings.load，写 bootstate
        app.py            # 进程装配入口（create_app + app 单例 + lifespan + importlib 触发路由注册 + CodeGraph supervisor 生命周期），自 api/app.py 上移至顶层
        bootstate.py      # 启动状态文件写入器（booting/ready/failed/stopped 契约，供桌面端 supervisor 轮询）
        api/              # FastAPI 接入层
          agents_api.py / tasks_api.py / turns_api.py / workspaces_api.py / changes_api.py / logs_api.py
          dependencies.py       # FastAPI 依赖接线（re-export config 单例 + 持有 AgentRuntime 单例 set/get/build_runtime）
          middleware/api_logging.py  # 请求/异常日志中间件
          schemas/request/         # API Pydantic 请求模型（PascalCase 文件名，待确认，见 D12）
          schemas/response/        # API Pydantic 响应模型（同上）
        codegraph/        # CodeGraph Kernel 子系统（常驻代码智能后端）
          supervisor.py / kernel_client.py / node_resolver.py / protocol.py / exceptions.py
        config/
          settings.py     # Settings 类级静态命名空间（MAX_*/WEB_*/LANGFUSE_*/CODEGRAPH_*/DELEGATION_*/CONTEXT_* 等，经 CODING_AGENT_* 环境变量覆盖）
          configuration.py# 进程级轻量单例收口（AgentProfileRegistry / ToolSystem / 委派子 Agent 能力摘要），见 D14
          logging/        # 日志子系统聚合包（特例：允许依赖 storage/trace_infra）
            logger.py / configuration.py / process_bridge.py / common.py
            context/log_context_store.py、filter/caller_filter.py、filter/log_context_filter.py
            formatter/jsonl_formatter.py、handler/sqlite_handler.py
        core/             # Agent 运行底座，全基于 LangGraph 体系
          agents/         # agent_profile / agent_profile_registry / define_agents（1 可见 developer + 4 hidden 委派子 profile）/ prompt_ref
          context/        # 上下文构建与压缩：system_prompt_builder / system_prompt_context / runtime_context_manager / runtime_message_store / context_compressor / context_usage_meter / rules/
          delegation/     # 子 Agent 委派执行：child_agent_runner / child_agent_profile_builder / delegation_executor（实现 delegate_task 执行端口）
          llm/            # factory（build_chat_model → ChatLiteLLM 单一收口）/ langchain_bridge / model_settings / model_catalog / context_window_resolver
          observability/  # Langfuse 可观测性唯一收口：langfuse_tracing / langfuse_tool_trace_recorder / langfuse_payload_sanitizer
          runtime/        # runner（AgentRuntime 总控）/ runtime_operations / checkpointer（AsyncSqliteSaver）/ turn_cancellation_registry
          workflows/      # agent_workflow（Protocol）
            nodes/        # 共享节点原语：model_node / tools_node / observation_node / max_steps_node / model_tool_helper / common
            react/        # StateGraph 实现：state / edges / workflow / runtime_config（支持 approval_resolver 审批中断）
        hook/             # Hook 系统（Claude Code 风格 7 类事件，失败安全 ALLOW）
          hook_base.py / hook_event.py（USER_PROMPT_SUBMIT / PRE_TOOL_USE / POST_TOOL_USE / SESSION_START / SESSION_END / STOP / PRE_COMPACT）
          hook_context.py / hook_result.py / hook_registry.py（纯索引）/ hook_interceptor.py（拦截编排收口，Pre/PostToolUse）
          builtins/       # bootstrap_hooks（启动播种）/ file_snapshot_hook（写前文件快照）/ codegraph_index_prepare_hook
        models/           # 业务层值对象（一文件一 model，文件名=类名）
          enums/          # event_type / turn_status / error_kind / hook_event
          event/          # runtime_event（turn 级信封）/ workspace_event（workspace 级，无 task/turn 信封）
          payload/        # 运行时事件 payload（30+：run_* / step_started / model_*（含 output/thinking delta）/ tool_call_* / tool_output_delta / observation_added / human_input_* / delegation_* / file_change_* / context_usage / final_response）+ registry/ + workspace_payload/
          task_record / turn_record / runtime_message / trace_context / delegation_record / file_snapshot_record / workspace_record / workspace_readiness / context_usage / turn_usage_stats / log_entry_record / log_query / log_query_result / mapped_log_record
        service/          # 领域服务编排层（仅 xxx_service + 结果值对象）
          depends.py      # service 层依赖装配（storage CRUD + service 单例；initialize/close/reset_service_dependencies）
          agent_runtime_event/  # 运行时事件总线（runtime_event_bus / runtime_event_service / runtime_event_subscription；持久化 + 进程内广播 + SSE 订阅）
          delegation/     # delegation_service / delegation_policy / delegation_result / delegation_context / delegation_acquire_result（父子 task/turn 委派编排）
          task/           # task_service / turn_service / turn_prepare_service / turn_stream_service（SSE 流编排：订阅/认领 producer/驱动/发布/兜底）/ turn_workspace_resolver / workspace_service / change_set_service（task 级变更集：累积查询 + 单文件撤销/保留）
          tool_execution/ # tool_execution_service / tool_trace_recorder（ToolTraceRecorder 协议）/ run_result
          workspace_event/ # workspace_event_bus / workspace_event_service / workspace_event_subscription（workspace 级 preparing/ready/degraded 事件）
          codegraph_lifecycle_service.py / log_query_service.py / turn_runtime_message_store.py
        storage/          # SQLite 数据层
          engine_cache.py / init_schema.py / store_engines.py
          crud/           # log / runtime_event / task / turn / turn_message / workspace / delegation / file_snapshot
          model/          # 对应 ORM 模型（base + 8 实体）
        tools/            # 工具系统（不基于 LangGraph），17 个内置工具
          schemas/        # tool_definition（契约单一事实来源，含 execution_mode 分级隔离）/ tool_call / tool_observation / tool_display / tool_execution_context / delegate_task_executor（委派执行端口）/ tool_runtime_dependencies
          tool_execute/   # tool_scheduler（执行固定入口）/ tool_executor（分级隔离：thread 直跑 / process 子进程+硬超时强杀）/ tool_error / tool_success / tool_cancelled / windows_job_object
          tool_handler/   # 内置工具 handler：read_file / write_file / replace_tool（原 patch）/ apply_patch_tool（V4A）/ search_files / list_directory / delete / execute_terminal / web_search / web_extract / delegate_task / codegraph_query（6 个查询工具）
            tool_base.py # HandlerBase 抽象基类（name/description/permission/args_model/timeout_seconds/risk_level + execute/to_definition）
            file_io/atomic_write.py、patch/（patch_parser / patch_diff / patch_apply / fuzzy_match / v4a_reverse（反向补丁，供快照撤销）/ file_change_display）
            search/、terminal/（危险命令硬拒）、web/（web_provider 协议 + providers/firecrawl + url_safety + web_content_store）
            security/     # path_resolver（路径边界收口）/ windows_reparse_point
          tool_models/    # 各工具 pydantic 参数/结果模型
          validation/arguments.py  # 参数校验唯一收口
          guard/          # 横切子层：display_data_budget / tool_output_budget / file_resource_paths / file_tool_state_coordinator（revision/stale/路径锁/重复调用）/ file_state/ / syntax_check（tree-sitter 多语言语法检查）
          tool_registry.py / tool_system.py
        utils/            # 叶子工具函数（零 app.* 依赖）
          datetime_utils / file_utils / code_file_utils / inflight_registry / token_estimator
          trace_infra/    # trace 基础设施原语（ids / redaction），自顶层 app/trace_infra 收拢至 utils 下
      tests/              # pytest 测试目录（57+ 测试文件：delegation / apply_patch / replace / context_usage / runner 事件 / projector 等）
      temp/               # 临时验证/调试脚本（已被 .gitignore 忽略，按需创建，用完清理）
    desktop/              # Tauri 2 + React + TS 桌面客户端
      src/                # React 前端
        components/       # chat/（AgentMessage / MarkdownStream / ThinkingBlock / ToolCallCard / TerminalCallCard / ContextUsageRing / FileLink 等）、layout/（Sidebar / ChatPanel / RightPanel / TurnTimeline / ChangesDrawer / InputBar 等）、right-panel/（Changes / Context / Mcp / Outputs / Sources）、sidebar/、logs/、ui/
        hooks/            # useBackend / useBackendBootstrap / useSSE / useTask / useChanges / useDelegationStreams / useStartupTaskResume / useWorkspaceTaskLazyLoad
        services/         # api / sse / sseConnectionBase / sseParser / delegationStream / backend / workspace / logs / dialog / tracePropagation / types / timeline/（projector / groupTools）
        stores/           # zustand：backend / event / task / turn / workspace / workspaceEvent / delegation / contextUsage / clientTrace / conversationTrace
        lib/、pages/（chat / logs）、tests/（50+ vitest）
      src-tauri/          # Rust 宿主（src/lib.rs / main.rs + backend/ supervisor 托管 + commands/）
        src/backend/      # boot_state / health_checker / process_launcher / runtime_locator / supervisor / types
        src/commands/     # backend.rs（start/stop/restart/status/logs_tail）/ fs.rs / logging.rs
    shared/               # 前后端共享协议（15 个 ts：api / events / task / turn / workspace / workspaceEvent / changes / agents / backend / logs / toolDisplay / toolDisplayRules / toolExecution / tracePropagation / index；events.ts 由 pre-commit 从 payload 模型生成）
  docs/                   # 设计/计划/验收文档：idea-requirements / mypy-strict-migration-plan / delegation-parent-child-task-refactor / sse-connection-refactor-plan / ui-guidelines / ui-refactor-plan / 上下文折叠 / 后端审查报告；api/openapi.json（生成物）；codegraph-docs/；plan/（subagent / langfuse / litellm / startup-resume 等专题计划）；langraph/（LangChain/LangGraph 学习笔记）
  rules/                  # 项目级协作规则、代码开发规范、交互澄清、经验记录、impeccable/（UI 打磨 skill）
  skills/                 # log-triage 日志排障技能（SKILL.md + scripts）
  third_party/codegraph/  # 本地 fork 的 CodeGraph 内核（UPSTREAM.md 记录上游），供 app/codegraph 常驻 kernel 与 .codegraph 索引使用
  coding-agent-docs/      # 成熟 coding-agent 原理资料库（仅参考，不混入源码）
  scripts/                # 开发/检查/生成辅助脚本：dev.sh / dev-client.sh / dev.py / generate_api_ts.py / generate_runtime_event_ts.py / generate_desktop_icons.py / query_logs.py / verify_chunk_stats.py / analyze_langfuse_replay.py / fetch_langfuse_replay.py
  logs/                   # 本地日志落盘目录（运行时生成）
  storage/                # 本地 SQLite 运行状态文件目录（运行产物，不提交）
```

目录职责要点：

- `apps/backend/app/app.py`：进程装配入口（自 `api/app.py` 上移）。**网关约定**：必须用 `importlib.import_module("app.api.xxx")` 触发路由注册，不能用 `import app.api.xxx`，否则顶层包名 `app` 被覆盖为模块对象。lifespan 内完成 `Settings.load`、service 依赖初始化、日志挂载、CodeGraph supervisor 生命周期与 bootstate 写入。
- `apps/backend/app/api/`：FastAPI 路由、SSE 格式化与依赖组装（接入层）。`turns_api` 只负责执行 `pending` 轮次（pending → 认领 → 实时流，非 pending 返回 409）；SSE 事件流编排（订阅 / 认领 producer / 驱动 / 发布 / 兜底）收口在 `service/task/turn_stream_service.py`，HTTP 连接作为 consumer 订阅自身 turn。`workspaces_api` 提供 workspace prepare / CodeGraph 索引准备端点；`changes_api` 提供变更集查询与撤销/保留端点。
- `apps/backend/app/codegraph/`：CodeGraph Kernel 常驻子系统（supervisor 进程管理 + RPC client + 协议 + 异常分层）。6 个 `codegraph_*` 查询工具经 `ToolSystem` 注册，kernel 不可用时 execute 优雅降级。
- `apps/backend/app/hook/`：Claude Code 风格 Hook 系统。7 类事件（`USER_PROMPT_SUBMIT` / `PRE_TOOL_USE` / `POST_TOOL_USE` / `SESSION_START` / `SESSION_END` / `STOP` / `PRE_COMPACT`）；`HookInterceptor.fire` 是所有拦截点的统一收口，失败安全（异常/超时/非法结果统一兜底 `ALLOW`）；`PRE_TOOL_USE` 返回 `DENY` 为硬拒绝（不走 `interrupt()` 审批）。内置 hook：`file_snapshot_hook`（写工具执行前对目标文件拍反向 V4A 快照）、`codegraph_index_prepare_hook`（workspace 索引准备）。
- `apps/backend/app/core/`：Agent 运行底座，**全基于 LangGraph 体系**（LangChain 为 LangGraph 的硬依赖基座）。`core/delegation/` 承载子 Agent 委派执行（child agent 复用 Agent/Workflow/Runtime，非平行系统）；`core/context/` 承载系统提示词构建、运行时上下文管理与压缩（context compaction 已落地）；`core/llm/factory` 经 langchain-litellm `ChatLiteLLM` 单一收口接入 DeepSeek；`core/observability/` 是所有 langfuse 三方依赖的耦合收口。
- `apps/backend/app/models/`：业务层值对象。运行时事件定义在 `models/event/runtime_event.py`（payload 在 `models/payload/`，workspace 级事件在 `models/event/workspace_event.py`）。日志查询服务在 `service/log_query_service.py`。
- `apps/backend/app/service/`：领域服务编排层，只放 `xxx_service`（及结果值对象）。`service/depends.py` 是 service 层内部单例装配入口。`service/delegation/` 承载父子 task/turn 委派编排（并发上限 `DELEGATION_MAX_CONCURRENCY=4`，超时 `DELEGATION_TIMEOUT_SECONDS=300`）；`service/task/change_set_service.py` 把 `file_snapshots` 反向快照聚合为 task 级变更集，检查点粒度为「一个 turn = 一个检查点」。
- `apps/backend/app/storage/`：SQLite 持久化存储。LangGraph checkpoint 由 `core/runtime/checkpointer` 经 aiosqlite 直连，不经本层引擎。
- `apps/backend/app/tools/`：工具系统统一收口，**不基于 LangGraph**；17 个内置工具（10 文件/终端/web + delegate_task + 6 codegraph 查询）经 `core` 调度执行。`ToolDefinition` 是契约单一事实来源；所有工具 handler 继承 `tool_handler/tool_base.HandlerBase`。工具执行**分级隔离**（`execution_mode`：`process` 子进程+硬超时强杀用于 execute_terminal；默认 `thread` 当前线程直跑）。文件路径安全统一收口 `tool_handler/security/path_resolver`。工具拦截（Pre/PostToolUse）经 `hook_interceptor` 静态方法收口。
- `apps/backend/app/utils/`：叶子工具函数与 `trace_infra/` 原语（ID 生成/校验、payload 脱敏），均为 leaf，零 `app.*` 依赖。
- `apps/backend/app/bootstate.py`：后端启动状态文件写入器。与桌面端 Rust supervisor 约定 `storage/backend.bootstate.json` 契约（booting/ready/failed/stopped）；错误信息落盘前自动脱敏。
- `apps/shared/`：前后端共享 TypeScript 协议，通过 Vite alias `@shared` 引用；`events.ts` 与 `docs/api/openapi.json` 均由 pre-commit hook 从后端代码自动生成，避免契约漂移。
- `third_party/codegraph/`：本地 fork 的 CodeGraph 内核；`app/codegraph` 的 supervisor 负责其常驻与协议通信。

当前已确认的概念边界：

- `Agent` 是执行主体。内置 5 个 profile（`define_agents.py`）：1 个可见默认 `developer`（全工具，`max_steps=300`）+ 4 个 `hidden` 委派子 profile（`delegate_reviewer` / `delegate_analyst` / `delegate_tester` / `delegate_coder`），模型统一为 `deepseek/deepseek-v4-flash`（`DEEPSEEK_API_KEY`）。
- `Workflow` 是执行策略（ReAct-like 已落地：`workflows/react` + 共享 `workflows/nodes`），描述 Agent 如何完成任务；可扩展 Plan-and-Execute、Review-Fix 等。
- `Runtime` 是执行底座（`core/runtime/runner.AgentRuntime`），负责状态管理、模型调用、工具调度、审批、checkpoint、context compaction、事件流、取消、恢复和终止保护。
- `Subagent` 已按「child agent / child run 复用 Agent、Workflow 和 Runtime」落地：`delegate_task` 工具 → `tools/schemas/delegate_task_executor` 端口 → `core/delegation/delegation_executor` 生产实现 → `service/delegation` 父子 task/turn 编排，事件流含 `delegation_*` payload。
- `core` 是 LangGraph 编排内核；`tools` 不基于 LangGraph，由 `core` 调度执行。
- 工具执行分级隔离：`ToolDefinition.execution_mode` 声明「是否需要 OS 级故障隔离」，`ToolExecutor.execute` 据此分流——`process` 走子进程 + 硬超时强杀 + 树杀（execute_terminal）；`thread`（默认）当前线程直跑。该字段按工具逐个声明。
- context compaction 已落地：`context_usage_meter` 度量（`CONTEXT_WINDOW_TOKENS` 可配）→ `context_usage` 事件推前端（ContextUsageRing）→ `context_compressor` 压缩，`PRE_COMPACT` hook 可介入。
- 并发执行边界：**并发粒度是 task**——不同 task（同 workspace 或跨 workspace）的 turn 并发执行；同一 task 内 turn 仍串行（pending → 认领乐观锁，一次一个 running turn）。跨 task 共享资源（文件路径锁、`file_snapshots.seq` 分配等）必须按 task/workspace 维度隔离；`file_snapshots.seq` 已改为 task 内递增并加 `(task_id, seq)` 唯一索引兜底。

## 五、项目约定（已落地、强制）

以下约定已从真实代码反推固化，是开发 Agent 提交前的事实基线；详细条款见文末「开发前必须路由」指向的规范文件。

### 1. Python 代码与命名
- **一文件一类**：文件名 = 主类名 snake_case。允许例外：服务类用 `xxx_service.py`；值对象工厂类、bridge、utils 等工具模块可承载多个强相关公共符号。工具 handler 统一继承 `tool_handler/tool_base.HandlerBase`。
- **值对象自带工厂方法**：从源对象到领域值对象的映射逻辑，收进值对象自身的 `from_xxx` 类方法，不单独保留无状态的 Mapper 类。
- **docstring 四段式**：每个函数/方法必须有完整中文 docstring，采用「参数 / 返回 / 异常 / 副作用」Google 风格；函数签名变更时同步更新。
- **工具链（单一来源）**：依赖与锁由 `uv` 管理（`pyproject.toml` + 已提交 `uv.lock`）；Ruff（行宽 100、双引号）做 format+lint+import 排序。**新代码**必须 Ruff 通过、带完整类型注解、无 `import *`。
- **mypy 强类型渐进基线**：`pyproject.toml` 声明 `strict = true` + 存量 `app.*` overrides 豁免（迁移计划见 `docs/mypy-strict-migration-plan.md`）；pre-commit 双门禁——存量文件走非阻塞 `mypy_nonblocking.sh`，**本次新增的 `app/` 文件走 `mypy_new_strict.sh`（`mypy.strict.ini`，无豁免，未过阻断提交）**。
- **导入纪律**：绝对导入 `from app.xxx import yyy`，不使用相对导入跨包；`__init__.py` 只做薄壳 re-export，不放业务逻辑；禁止 `import *`。
- **命名禁止模糊词**：`Utils` / `Helper` / `Common` / `Misc` / `Manager` 等笼统命名不允许（约定别名 `log` / `log_query_service` 等除外）。

### 2. 目录与依赖方向
- **单向依赖总纲**（详见 `rules/目录组织规范.md` 第一章，注意该文档对齐滞后，以真实代码为准）：`api → core/service`；`core → service/tools/models/config`；`service → storage/models/config/tools`（tool_execution 编排 tools 合法）；`storage → models`；`tools → config/models/utils`（不依赖 service）；`models → utils`（leaf）；`utils`（含 `trace_infra`）为纯 leaf；`config` 通用轻量，`config/logging` 聚合特例允许依赖 `storage`。
- **禁止跳层 / 反向依赖**：如 `api` 直 import `storage`/`tools`、`tools → service`、`models → 编排层` 均为违规（现存例外见第十章 D5/D14）。
- **依赖倒置范例**：`ToolTraceRecorder` 协议定义在 `service/tool_execution/tool_trace_recorder.py`，实现收口在 `core/observability`，`core → service` 为合法方向。`DelegateTaskExecutor` 端口定义在 `tools/schemas/`，生产实现在 `core/delegation`，同为依赖倒置。

### 3. 日志约定（强制）
- **业务模块统一单例**：一律 `from app.config.logging.logger import log`，**禁止**在业务代码散落 `logging.getLogger(...)`。仅 `logger.py`、`configuration.py`、`process_bridge.py` 三类子系统内部文件保留 `getLogger` 装配职责。
- **跨进程**：spawn 子进程经 `process_bridge` 队列桥汇入父进程统一管线（子进程入口用 `install_logging_for_current_process(log_queue=...)` 重新挂载），不共享 logger 对象。
- **统一格式**：落盘为单行 JSON（JSONL），固定 9 字段；`event` 稳定英文 snake_case（禁 f-string 拼动态）、`msg` 中文人读、`data` 结构化业务字段、**唯一链路键 `trace_id`**。详见 `rules/Agent日志开发规范.md`。
- **异常纪律**：捕获异常必须用 `.exception()` 带堆栈并 `raise`（底层重抛让上层补上下文）；禁止空 `except`、禁止 `print` 当系统日志、禁止输出 secret。

### 4. 临时脚本约定
- 验证 / 调试脚本统一放 `apps/backend/temp/`（已被 `.gitignore` 忽略，目录按需创建），禁止散落项目根或其他位置；**用完及时清理，避免误提交**。

### 5. 测试与闭环
- pytest；`tests/` 下 `test_*.py`，`pytest-asyncio` `asyncio_mode=auto`；真实调用 LLM 的端到端冒烟用 `@pytest.mark.llm` 标注（默认不进普通 pytest）。关键路径（工具执行、checkpoint、审批、日志、web 工具、delegation）必须有测试。
- 开发完成后必须形成**审查与测试闭环**；开发 Agent 不能既当开发又当裁判（见第六章铁律）。

### 6. 工具系统约定（强制）
- **工具契约单一事实来源**：`ToolDefinition`（`schemas/tool_definition.py`）是工具契约的唯一来源，模型可见结构由 `to_model_tool_definition()` 投影，禁止平行类。
- **内置工具继承 HandlerBase**：17 个内置工具（read_file / write_file / replace（原 patch 拆分）/ apply_patch（V4A）/ search_files / list_directory / delete / execute_terminal / web_search / web_extract / delegate_task + 6 个 codegraph 查询工具）handler 继承 `HandlerBase`，声明类级元数据并实现 `execute`/`to_definition`。
- **分级隔离**：`execution_mode` 声明隔离策略——`process`（子进程 + 硬超时强杀）仅用于 execute_terminal 等需 OS 级隔离的工具；其余默认 `thread`。
- **Hook 拦截**：工具执行前后经 `hook_interceptor` 触发 `PRE_TOOL_USE` / `POST_TOOL_USE`；`DENY` 硬拒绝；任何 hook 异常不阻断主流程。
- **文件协作状态**：文件类工具经 `guard/file_tool_state_coordinator` 做 revision/stale/重复调用检测/写路径锁；只读重复调用会被提前拦截。
- **写前快照与变更集**：`file_snapshot_hook` 在写工具执行前对目标文件生成反向 V4A 快照（`file_snapshots` 表）；`change_set_service` 聚合 task 级变更并支持单文件撤销/保留。
- **语法检查**：`guard/syntax_check` 基于 tree-sitter（python/ts/js/go/rust/java/html/css/c/c-sharp）在写入前拦截语法错误。
- **输出预算**：`guard/tool_output_budget` 统一截断模型可见 `content`，超限时落盘 artifact；`guard/display_data_budget` 约束客户端展示通道。
- **Web 工具**：`web_search` / `web_extract` 通过 `web/web_provider_registry` 选择 provider（当前 firecrawl），URL 安全校验（`web/url_safety`）拦截带凭据/内网地址的请求。
- **CodeGraph 工具**：6 个查询工具（explore/search/node/callers/callees/impact）经 `app/codegraph` kernel client 执行；kernel 不可用时降级报错，不崩溃。

### 7. 可观测性约定（Langfuse）
- Langfuse 三方依赖**唯一收口**在 `core/observability/`（tracing / tool trace recorder / payload sanitizer），其余业务代码不得直接 import langfuse。
- 所有 langfuse import 惰性加载；未启用/缺密钥/未安装时运行时行为与集成前完全一致，**绝不因可观测性失败中断 turn 执行**。
- 开启条件：`LANGFUSE_ENABLED` + public/secret key 齐备 + 包可导入。密钥仅经环境变量注入，不写入代码库。

### 8. 契约生成物（pre-commit 自动维护）
- `docs/api/openapi.json`：后端 API 契约快照，`app/` 变更时自动重导出。
- `apps/shared/ts/events.ts`：运行时事件 TS 类型，payload 模型变更时自动再生成。
- 前端 TS 类型另由 `scripts/generate_api_ts.py` 手动生成。**禁止手改生成物**。

## 六、进入代码开发后的铁律

进入代码开发后，必须遵守 `rules/Agent代码开发规范.md`（通用）与 `rules/Python代码开发规范.md`（Python 专属）。摘要如下：

- **第零铁律（最高优先级）：以「方便项目稳定迭代」为最终目标**。所有下述条款都是手段不是目的；当「最小改动 / 零新增依赖」等默认偏好损害长期可维护性时，允许做结构性大改动、允许引入成熟外部依赖。但「不重复造轮子」是不可解除的底线——禁止为「显得敢改」而手写本可复用的通用复杂能力。各质量维度（正确性、可排查性、复用、单一职责、结构清晰、可读性、改动聚焦、性能）同等重要，不设固定优先级链，冲突时以本铁律为最终判据。
- 单一职责：一个文件只做一件事，按职责而非行数判定。
- 不重复造轮子：能用成熟方案就不自己写。
- 改动聚焦（默认偏好，非刚性）：改一行能解决的不改十行；但当结构性重构对长期迭代更优时，可扩大改动面（见第零铁律）。
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
- 客户端 UI 规范：`rules/Agent客户端UI规范.md`
- 日志开发规范（log 单例、JSONL 9 字段、event/msg/data 纪律）：`rules/Agent日志开发规范.md`
- 目录组织规范（分层依赖方向契约；**注意：对齐滞后于当前代码，以真实代码为准**）：`rules/目录组织规范.md`
- 交互澄清规则：`rules/global-interaction-clarification.md`
- 成熟机制复用规则：`rules/mature-mechanism-reuse.md`
- 经验复用记录：`rules/agent-lessons.md`
- CodeGraph CLI 使用指南：`rules/codegraph-cli-agent-guide.md`
- mypy strict 迁移计划：`docs/mypy-strict-migration-plan.md`
- delegation 设计与重构：`docs/delegation-parent-child-task-refactor.md`、`docs/plan/`（subagent / langfuse / litellm / startup-resume 等专题）
- SSE 连接重构：`docs/sse-connection-refactor-plan.md`
- coding-agent 原理文档：`coding-agent-docs`
- 后端 API 契约：`docs/api/openapi.json`（生成物）

## 八、CodeGraph 使用规则

当前仓库根目录存在 `.codegraph/` 时，说明本项目已经有 CodeGraph 索引。本项目同时**将 CodeGraph 作为生产能力集成**（`third_party/codegraph` 内核 + `app/codegraph` 常驻 supervisor + 6 个查询工具 + workspace prepare 索引流程）。凡是需要理解代码结构、定位符号、追踪调用关系、分析影响范围或查找实现位置，应先用 CodeGraph 缩小范围，再精读必要源码。

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
- 不是追求"尽快生成代码"，而是追求"长期稳定地完成开发任务"。

## 十、已知偏差与开放问题

### 已知偏差（待修复）
- **D5（跳层，位置变更）**：`api → tools` 残留一处端点级跳层——`api/changes_api.py` 直 import `app.tools.tool_handler.patch.patch_apply`（`PatchApplyError`）。原装配点 `api/app.py` / `api/depends/dependencies.py` 已随装配上移消除：进程装配现为顶层 `app/app.py`（import `tools`/`codegraph`/`service` 属装配点），`api/dependencies.py` 仅经 `config/configuration` re-export 工具系统访问器。
- **D12（命名待确认）**：`api/schemas/request|response` 文件名用 PascalCase，与 snake_case 约定不一致，待确认豁免或改名。
- **D14（配置层下沉，待定性）**：`config/configuration.py` 收口 `AgentProfileRegistry` / `ToolSystem` / 委派摘要进程级单例，构成 `config → core` / `config → tools` 依赖，超出第一章契约；其 docstring 仍引用已不存在的 `api/app.py`、`api/depends/dependencies.py` 路径，属失效 docstring，应一并清理或定性为装配点特例。
- **D15（空壳文件，待清理或实现）**：`service/workspace_event/workspace_index_service.py` 为 0 字节空壳。
- **D16（拼写错误，待修正）**：`models/payload/registry/workspace_event_payload_registery.py` 文件名 `registery` 应为 `registry`（及其内部类名，同步修正引用）。
- **D17（文档滞后，待对齐）**：`rules/目录组织规范.md` 停留在 2026-08-04（仍引用 `main.py`、`api/app.py`、`api/depends/dependencies.py`、`service/runtime_event/`、顶层 `app/trace_infra`、`tools/security/project_path.py`、9 工具等旧状态）；`apps/backend/README.md` 与 `apps/backend/.env.example` 引用的 `docs/Langfuse可观测性集成技术方案.md` 已不在 docs/ 下；`app/tools/tool_system.py` docstring 中「16 个工具」计数与实际注册数（17）不符。均已以真实代码为准，待批量修订。

### 文档待同步
- 本文件已于 2026-08-17 与真实代码全量对齐（装配入口上移 `app/app.py`、hook / codegraph / delegation / workspace_event / change_set 等新模块、17 工具、models/event 与 utils/trace_infra 归位、pre-commit 双 mypy 门禁与契约生成物）。
- `rules/目录组织规范.md` 需按本次对齐结果重新修订（见 D17）。

### 开放问题
- DeepSeek 之后的大模型接入顺序。
- （已决议）LangGraph 使用深度：作为强依赖编排底座，承载 workflow 扩展、checkpoint、interrupts、streaming、subgraphs；工具执行层仍自定义。
- 第一阶段剩余能力的验收标准和优先级排序：MCP、工具权限审批（`interrupt()` + 审批 UI，当前写/搜工具默认放行，见 `CHANGELOG.md` 已知临时缺口）。

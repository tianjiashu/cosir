# Backend

本地桌面 coding-agent 的 Python 后端，基于 FastAPI + LangGraph + SQLite，由桌面端（Tauri 2）托管启动。

## 技术底座

- **FastAPI**：HTTP / SSE 接入层（`app/api/`），承载任务、轮次、Agent、工作区、日志等端点。
- **LangGraph（强依赖）**：Agent 运行底座。Workflow 编排层基于 `StateGraph` + `SqliteSaver` checkpoint，支持 `interrupt()` 审批中断、`Command(resume=)` 恢复、subgraph/`Send` subagent。最低运行环境 Python 3.11+。
- **自定义工具系统（不基于 LangGraph）**：`ToolDefinition` 是工具契约单一事实来源；内置 9 个工具（read_file / write_file / patch / search_files / list_directory / delete / execute_terminal / web_search / web_extract），全部继承 `tool_handler/tool_base.HandlerBase`，由 `core` 调度执行，采用分级隔离（thread 直跑 / process 子进程+硬超时强杀）。
- **Langfuse 可观测性**：LLM 与工具调用的 trace 唯一收口在 `core/observability/`，惰性加载、缺配置不影响主流程。
- **SQLite**：运行时状态与日志持久化（`app/storage/`）；LangGraph checkpoint 由 `core.runtime.runs.checkpointer` 经 aiosqlite 直连。

## 分层架构

```
api/          FastAPI 接入层（路由、SSE、依赖装配）
core/         Agent 运行底座（全基于 LangGraph：runtime / workflows / llm / context / agents / observability）
service/      领域服务编排层（task / turn / workspace / tool_execution / workspace_event / log_query）
storage/      SQLite 数据层（引擎缓存、schema、CRUD）
tools/        自定义工具系统（schemas / tool_execute / tool_handler / tool_models / validation / guard）
models/       业务值对象地基（dataclass / 枚举 / payload）
config/       运行配置 + 日志子系统聚合包（logging/ 特例允许依赖 storage/trace_infra）
trace_infra/  trace 原语（leaf）
utils/        叶子工具函数（leaf）
```

依赖方向单向：`api → core/service`；`core → service/tools/models/config/trace_infra`；`service → storage/models/config/trace_infra/tools`；`storage → models`；`tools → config/models/utils/trace_infra`（不依赖 service）。禁止跳层与反向依赖。

## 关键约定

- **日志**：业务模块统一 `from app.config.logging.logger import log` 单例；落盘为单行 JSON（JSONL，9 字段），`trace_id` 为唯一链路键。详见 `rules/Agent日志开发规范.md`。
- **启动契约**：`app/bootstate.py` 向运行时 bootstate 文件写入 `booting/ready/failed/stopped` 状态，供桌面端 Rust supervisor 轮询（崩溃瞬间也能拿到脱敏后的失败原因）。
- **配置**：`app/config/settings.py` 以 `Settings` 类级静态命名空间承载进程级配置，消费点静态读 `Settings.X`，不实例化、不传递 Settings 对象。
- **Web 工具**：`web_search` / `web_extract` 经 `tools/tool_handler/web/` 子系统，通过 `web_provider_registry` 选择 provider（当前 firecrawl），URL 安全校验拦截带凭据/内网地址。
- **工具执行分级隔离**：`ToolDefinition.execution_mode` 声明隔离策略，`process` 仅用于 execute_terminal 等需 OS 级隔离的工具。

## 开发工具链

- 依赖与锁文件由 `uv` 管理（`pyproject.toml` + 已提交 `uv.lock`）。
- Ruff（行宽 100、双引号）做 format + lint + import 排序；mypy 渐进类型检查；pre-commit 提交前强制。
- pytest（`pytest-asyncio`，`asyncio_mode=auto`）；关键路径（工具执行、checkpoint、审批、日志、web 工具）必须有测试，置于 `tests/`。

## 启动方式

### 后端（开发期）

在 `apps/backend` 目录下使用模块入口启动：

```bash
cd apps/backend
uv run python -m app
```

可用环境变量覆盖运行参数：

- `CODING_AGENT_PORT`（默认 `8000`）
- `CODING_AGENT_RELOAD`（默认 `true`，设为 `false` 关闭热重载）

后端日志落盘位置：

- 应用结构化日志：由 `Settings.LOG_DIR` 配置并按日期轮转
- 桌面开发时的 uvicorn stdout/stderr：由 Tauri supervisor 写入应用 runtime 目录

### 桌面开发模式启动

Tauri 负责托管后端生命周期。窗口立即显示，前端根据 supervisor 状态展示“后端启动中/可用/失败”，后端仅监听本机 loopback 地址，端口由 Tauri 动态分配：

```bash
npm run tauri:dev --prefix apps/desktop
```

桌面窗口关闭或按 `Ctrl+C` 会终止后端及其子进程。开发期后端使用应用 runtime 目录下的 `uv-cache`，不依赖用户全局 uv 缓存权限；stdout/stderr 会写入桌面运行目录的 `backend-console.log`。

### 后端运行时契约

- 开发态默认使用 `uv run --directory <backend> python -m app`，缓存固定在应用 runtime 目录的 `uv-cache`。
- `COSIR_BACKEND_PYTHON` 仅作为明确的本地开发/测试运行时覆盖，不是启动失败后的隐式兜底。
- 发布态不查找 PATH 中的 `python` 或 `uv`；应用资源目录必须携带 `backend-runtime/python.exe`（Unix 为 `backend-runtime/bin/python`），否则 supervisor 会报告明确的运行时缺失错误。
- 发布包还必须将后端代码放入应用资源目录的 `backend/`。HTTP 仍只作为桌面端与本机后端之间的进程边界。

# Backend

This directory contains the local Python backend for the desktop coding agent.

The first implementation slice focuses on a text-only Agent runtime skeleton:

- task creation
- default Agent profile persistence and prompt injection
- runtime event emission
- injectable workflow strategy boundary
- streaming model adapter boundary
- OpenAI-compatible streaming response parsing
- model-facing tool schema injection for model-visible tools
- context budget guard before model provider calls
- SSE API boundary
- trace/log query boundary
- file logging boundary

`app/agents/` owns Agent execution profiles. The default runtime uses the
built-in `developer` profile, persists its `agent_id` on each task, injects the
profile into the system prompt, emits it in `run_started`, and stores it in
runtime records. The profile also filters model-visible tools and blocks tool
calls outside the Agent boundary before they reach execution. This keeps Agent,
Workflow, and Runtime separate before alternate role support exists.

Model provider streams and client SSE are separate boundaries. The model
adapter consumes provider streaming chunks and emits internal runtime deltas;
the API layer formats stored runtime events as SSE for the desktop client.

`app/runtime/` owns task lifecycle boundaries, event persistence, and the
controlled operation facade exposed to workflows.
`app/workflows/` owns replaceable Agent execution strategies; a workflow decides
which runtime operations to call while the runtime keeps storage, model, tool,
and event side effects behind that facade.

Tool definitions are split between execution-facing and model-facing shapes.
`ToolScheduler` filters registered tools by model visibility, the runtime
converts those definitions into model-facing tool schemas, and the
OpenAI-compatible adapter serializes them as Chat Completions function tools.
Provider tool call ids are preserved across the assistant tool call message and
the following tool observation so follow-up Chat Completions requests remain
protocol-compatible. The first version explicitly requests serial tool calls
with `parallel_tool_calls=false` and rejects multiple tool calls if a provider
returns them anyway. Denied tools are not exposed to the model.

Context size is guarded at the runtime/model-call boundary. The first version
uses `CODING_AGENT_MAX_CONTEXT_CHARS` as a character-count proxy and fails with a
clear `context_window_exceeded` error before provider I/O. Future tokenizer-based
budgeting or context compaction should reuse this boundary.

The first LangGraph integration is intentionally narrow and version-gated:
`app/workflows/step_controller.py` uses a LangGraph `StateGraph` for workflow
step continuation decisions only when a safe LangGraph baseline is installed.
Current Python 3.9 development falls back to deterministic local logic because
the secure LangGraph baseline requires Python 3.10+. The ReAct-like workflow
still owns event streaming and tool/model orchestration; this is a controlled
graph-backed insertion point, not a full migration of the workflow graph yet.

FastAPI and httpx are imported lazily by API/model adapter paths where possible, so the core runtime can be tested before project dependencies are installed.

## 启动方式

### 后端（开发期）

在 `apps/backend` 目录下使用约定的模块入口启动 uvicorn：

```bash
cd apps/backend
.venv/bin/python -m app
```

可用环境变量覆盖运行参数：

- `CODING_AGENT_HOST`（默认 `127.0.0.1`）
- `CODING_AGENT_PORT`（默认 `8000`）
- `CODING_AGENT_RELOAD`（默认 `true`，设为 `false` 关闭热重载）

后端日志落盘位置：

- `logs/app.log`：应用结构化日志（`configure_logging` 落盘）
- `logs/backend.log`：uvicorn 进程输出（启动横幅、访问日志、异常栈），由 `scripts/dev.sh` 重定向

### 前后端并行启动

仓库根提供一键脚本，并行拉起后端 uvicorn 与前端 tauri dev，日志统一到 `logs/`：

```bash
# 方式一：直接运行脚本
bash scripts/dev.sh

# 方式二：通过 npm（仓库根 package.json 提供）
npm run dev:all
```

按 `Ctrl+C` 会同时终止前后端及其子进程（cargo / vite）。

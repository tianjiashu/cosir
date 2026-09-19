---
name: log-triage
description: |
  Use when investigating or root-causing problems in the local coding-agent desktop project:
  bugs, exceptions, crashes, hangs, stuck runs, missing UI updates, tool failures,
  unexpected Agent behavior, or requests to "看日志排查", "从数据库排查", "不启动服务直连数据库",
  "查 task/run/conversation_runs/conversation_task_contexts", "trace 在哪", "logs.sqlite3",
  "app.sqlite3", "frontend.log", "backend.bootstate.json", "Agent 回放", or "复现问题".
allowed-tools: Read,Write,Bash
---

# log-triage — 日志驱动问题排查

本技能用于对本项目（`coding-agent` 本地桌面 coding-agent）进行**证据驱动的问题排查**。
核心理念：**先查日志和本地数据库，再谈猜想；证据不足就补日志并复现，优先自己复现**。

---

## 0. 三套标识别混用（最先读，最容易误判）

排查入口是一个 id。本项目并存**三种**标识，形态相近但来源与查询通道完全不同：

| 标识 | 是什么 | 产生点 | 落盘/可见位置 | 查询通道 |
|------|--------|--------|----------------|----------|
| **链路 trace_id**（32 位小写 hex） | 唯一能**跨前端与后端日志**串起一条请求的键 | 前端 `apps/desktop/lib/trace.ts:newTraceId()`（`crypto.getRandomValues` 128-bit）；后端缺失时由 `new_trace_id()`（`uuid4().hex`）补生成 | 后端 `storage/logs.sqlite3` 的 `log_entries.trace_id`；后端文件日志 `backend-*.log` 的 `trace_id`；前端 `frontend-*.log` 的 `trace_id` | `query_logs.py trace <ID>` |
| **Langfuse trace_id**（32 位小写 hex） | LLM/工具调用的可观测 trace，仅用于 Langfuse 平台 | `conversation_run_trace()`（`core/observability/langfuse_tracing.py`）用 `start_as_current_observation(as_type="span")` 建根 span，Langfuse `CallbackHandler` 经 OTel current context 生成该 trace_id，再经 `ConversationRunTraceResult.trace_id` 回读 | 终态 SSE 事件 payload；Langfuse UI。**不落 `app.sqlite3`，也不落 `log_entries`** | Langfuse 平台 |
| **run_id / task_id**（整数） | 业务事实主键：一次 Agent 执行 = 一个 run | `conversation_runs.id` / `tasks.id` | `storage/app.sqlite3` | `query_app_db.py` |

**链路 trace_id 的前后端同源链路（照此反查）**：
`frontendLog(..., { traceId })` 把同一 id 写进前端日志 → `lib/http/client.ts:requestRaw` 以
`X-Trace-Id` 头发出 → 后端请求日志中间件（`api/middleware/api_logging.py`，由
`install_request_logging` 安装）从 `x-trace-id` 读头并 `merge_log_context(trace_id=...)` →
`LogContextFilter` 自动回填每条 `LogRecord` → 落 `log_entries` 与 `backend-*.log`。因此
**前端日志里的 trace_id 可以直接拿去 `query_logs.py trace` 反查后端全链路**。

**已废弃、不要再用**：`turn_id`、`turns`、`turn_messages`、`runtime_events` 表均已从 schema 删除；
`turn_trace()` 已不存在（改为 `conversation_run_trace()`）。历史文档若提到这些名字，视为过时。

**实战要点**：
- 拿到 32 位 hex **先问来源**：在前端/后端日志里看到的 → 链路 trace_id，用 `query_logs.py trace`；在 Langfuse UI 或 SSE payload 里看到的 → Langfuse trace，**用日志脚本查不到**（数值不同源，纯属巧合才会命中）。
- 排查 Agent 行为/状态一律用整数 `task_id` / `run_id` 走 `query_app_db.py`；不要拿它当 trace_id，反之亦然。
- 两条链路可同时存在但数值无关，不要用其中一条去校验另一条。

---

## 1. 证据源与落盘位置（唯一事实清单）

`<repo>` = 仓库根；`<data_dir>` = 桌面应用数据目录：Windows `%APPDATA%\com.cosir.desktop`、
macOS `~/Library/Application Support/com.cosir.desktop`、Linux `~/.local/share/com.cosir.desktop`。

| 源 | 位置 | 形态 | 查询方式 |
|----|------|------|----------|
| 前端日志 | `<data_dir>/runtime/frontend-YYYY-MM-DD.log` | JSONL，字段 ts/level/logger/trace_id/caller/event/msg/data/error/truncated（`logger=coding_agent.frontend`） | 直接 `Read`/grep；trace_id 可反查后端 |
| 桌面宿主日志 | `<data_dir>/runtime/desktop-YYYY-MM-DD.log` | 同字段结构（`logger=coding_agent.desktop`）；WebView2 进程失败等宿主级诊断 | 直接 `Read` |
| 后端控制台原文 | `<data_dir>/runtime/backend-console-YYYY-MM-DD.log` | 同字段结构（`logger=coding_agent.backend_console`、`event=backend_console_output`、`data.stream=stdout\|stderr`）；**`trace_id` 恒为空串**（Rust 侧写死），不能用于反查 | 直接 `Read`/grep（启动崩溃第一现场） |
| 后端结构化文件日志 | 桌面模式 `<data_dir>/runtime/backend-YYYY-MM-DD.log`；纯后端模式 `<repo>/logs/backend-YYYY-MM-DD.log` | JSONL，字段 ts/level/logger/trace_id/caller/event/msg/data/error/truncated | 直接 `Read`/grep |
| 后端日志库 | `<repo>/storage/logs.sqlite3` → `log_entries` | 结构化列：id/ts/level/logger/trace_id/caller/event/msg/data_json/error_json/truncated | `query_logs.py`（首选，支持 trace/级别/时间窗过滤） |
| 业务库 / Agent 回放 | `<repo>/storage/app.sqlite3` | 见 §3 表清单 | `query_app_db.py`（只读直连，不启动服务） |
| LangGraph checkpoint | `<repo>/storage/langgraph_checkpoints.sqlite` | `checkpoints` / `writes`，按 `thread_id` 分片 | `sqlite3` 直连；`thread_id` = `conversation_runs.checkpoint_thread_id` |
| 后端启动状态 | `<data_dir>/runtime/backend.bootstate.json` | JSON（`phase` / 失败原因） | `Read`；Tauri 据此判定启动失败 |

**为什么后端文件日志有两个可能目录（关键）**：桌面模式下 Rust 宿主 spawn 后端时注入
`CODING_AGENT_LOG_DIR=<data_dir>/runtime`，因此后端结构化日志、控制台日志、前端日志、宿主日志
**同目录**；而 `uv run --project apps/backend python -m app` / `pytest` 不注入该变量，回落
`Settings.LOG_DIR` 默认值 `<repo>/logs`。排查前先确认后端是**怎么起的**，别只看一个目录。

**分片规则**：单文件上限 5MB，同日历史分片为 `.1.log` … `.7.log`（保留 7 个）。日期切分与大小
分片叠加：跨日自动换新日期文件，但 `.1`…`.7` 序号在**当日**内累计，不跨日延续。日志目录与库路径
可被覆盖：`CODING_AGENT_LOG_DIR` / `CODING_AGENT_LOG_DATABASE_FILE` / `CODING_AGENT_CHECKPOINT_FILE`
（见 `apps/backend/app/config/settings.py:Settings.load`）。

**HTTP 通道（后端在跑时可替代直连）**：`GET /logs/query?trace_id=...` 与 `GET /logs/recent`
（`apps/backend/app/api/logs_api.py`），返回结构化 entries 与可直接渲染的 text。

---

## 2. 日志写入契约（决定「你能从日志里查到什么」）

排查前必须知道日志的**能力边界**，否则会在错误的方向上找证据。

**后端怎么写（照抄）**：统一用 `log` 单例，禁止 `print`/`console` 当系统日志。

```python
from app.config.logging.logger import log

# 第一个位置参数 = 机器可筛事件名（snake_case 英文）
# extra["msg"] = 给人看的中文消息；业务字段收进 extra["data"]
log.info("file_written", extra={"msg": "已写入文件", "data": {"path": path, "run_id": run_id}})
log.warning("retry", extra={"msg": "第 2 次失败将降级", "data": {"attempt": 2}})
try:
    ...
except Exception:
    log.error("op_failed", extra={"msg": "执行失败", "data": {"retryable": False}}, exc_info=True)
```

字段提取规则（事实来源：`app/models/mapped_log_record.py`）：

- `event` = `record.msg` 的**首个空白分隔 token**，必须匹配 `^[a-z][a-z0-9_]*$`，否则落为 `log_event`（`query_logs.py --event-prefix` 查的就是它）。
- `msg` = `extra["msg"]`（由 `install_msg_relocation` 在 `makeRecord` 阶段重定位到 `display_message`）；缺失时回退为 `event`。
- `data` = `extra["data"]` 与其余未保留 extra 字段**合并**后统一脱敏；单字段文本上限 **2000 字符**，超限追加 `[TRUNCATED:n]` 并把 `truncated` 置 true。
- `trace_id` 由 `LogContextFilter` 自动回填，**不要手动传**；`task_id`/`run_id` 等业务 id 放 `extra["data"]`。
- 子进程工具的日志经 `SubprocessQueueHandler` 回传父进程统一落盘，不另开日志文件。

> **⚠️ 持久化日志里没有异常堆栈。** `MappedLogRecord` 会把 `error.message` 与 `error.stack`
> 统一替换为「异常详情已省略」，只保留 `error.type`。也就是说 `log.exception(..., exc_info=True)`
> 在文件日志与 `log_entries` 里只贡献**异常类型**。
> 因此排查异常时：① 在 `except` 里把关键上下文（入参、分支、业务 id）写进 `extra["data"]`；
> ② 依赖 `event` + `caller` + `data` 定位到具体阶段；③ 需要堆栈就**本地复现**（见阶段 D），
> 不要指望日志文件。

**前端怎么写（照抄）**：统一从 `@/lib/logging/frontend-log` 调，禁止 `console.log` 当系统日志。

```ts
import { frontendLog } from "@/lib/logging/frontend-log";

// 签名 (level, event, msg, { traceId, data, error })；level 仅 DEBUG|INFO|WARNING|ERROR
await frontendLog("ERROR", "http_request_failed", "前端 HTTP 请求失败", {
  traceId,
  data: { path, method: "GET" },
  error,
});
```

- 前端日志与后端日志**同为同一套字段的同构结构**（`logger=coding_agent.frontend`、`caller=desktop.frontend`）；`event` 必须 snake_case，且 Rust 侧 `write_frontend_log` 强制 `trace_id` 为 32 位 hex（`len()==32` 且逐字符校验）。
- `error` 只保留 `type`，message 固定为「客户端请求失败（错误正文已省略）」。
- 落盘经 Tauri IPC `write_frontend_log`；纯浏览器 `vite dev` 没有该 IPC，只写控制台与内存环形缓冲，**不会落盘**。

---

## 3. 业务库表 → 承载事实 → 典型症状

`storage/app.sqlite3` 的表结构以 `apps/backend/app/storage/model` 为事实源（`schema` 子命令会从在线库读真实结构）。

| 表 | 承载事实 | 典型症状 / 排查点 |
|----|----------|-------------------|
| `workspaces` | 工作区身份与根路径 | 路径越界、找不到文件、工作区切换异常 |
| `tasks` | 任务身份、`current_run_id`、上下文窗口用量（`context_usage_used` / `context_window_total`）；父子/fork 关系由 `parent_task_id` / `parent_run_id` / `delegation_id` 显式承载，`extra` 为自由 JSON（fork 场景实测含 `{"fork": {"source_task_id", "source_run_id"}}`） | 任务列表状态不对、上下文占用异常、fork 关系 |
| `conversation_runs` | **Run 生命周期唯一事实源**：`status` / `end_reason` / `error_json` / `final_output` / `usage_json` / `agent_id` / provider+model / `checkpoint_thread_id` | 一直转圈、失败原因、用量与成本、模型路由错 |
| `conversation_commands` | Transport 命令幂等占用：`(task_id, command_id)` 唯一、`payload_hash`、`error_code` | 重复提交被拒、幂等冲突、命令失败码 |
| `conversation_task_contexts` | **canonical 上下文消息**：`message_json` / `transport_metadata_json` / `sequence` / `tool_call_id` / `is_streaming` / `include_in_context` | Agent 回放、工具调用与结果、上下文缺口、工具状态不符 |
| `delegations` | 子 Agent 委派：父子 run/task/agent、`status`、`summary`、`error` | 委派卡住、子任务结果丢失 |
| `terminal_sessions` | 终端会话元数据（PTY 与输出缓存不落库） | 终端断连、worker 崩溃、会话未收口 |
| `attachment_assets` | 附件资产：`content_sha256`、`idempotency_key`、`storage_state`、尺寸 | 图片上传失败、去重异常 |
| `providers` / `models` | 模型厂商与模型条目（`api_key` 为**明文 secret，任何输出都不得包含**） | 模型解析失败、窗口/能力标志错 |

**状态词表（唯一事实源）**：

- Run：典型路径 `pending → running → completed | failed | cancelled`（`cancelled` 允许在用户明确操作后再次转 `running`，用于续跑恢复）。**权威迁移白名单在 `assistant_transport/event/run_event.py:_ALLOWED_RUN_STATUS_TRANSITIONS`**（投影层闸门）：若后端已落定终态但 UI 状态不更新，优先怀疑该白名单把事件丢弃了（`completed` / `failed` 不可逆，迟到的 active 事件会被丢弃）。
- 工具调用生命周期：`pending / running / completed / failed / cancelled`（Transport `transport_metadata_json.status`）。
  **判读口径**：run 被取消或后端重启恢复时，`ConversationTaskContextService.close_unclosed_tool_calls_for_run`
  （配合 `ConversationRunService.recover_orphaned_runs`）会为未闭合调用**补一行占位 ToolMessage** 并记
  `cancelled`，所以「取消导致未闭合」通常表现为 `cancelled`；只有占位补行未执行（进程被强杀且之后未再启动）
  才表现为缺结果的 `pending`。`tools` 子命令按 `tool_call_id` 配对即可还原两者。
- 消息类型：`ai` / `human` / `tool` / `system`。AI 的工具调用在 `message_json.data.tool_calls[]`（`{id, name, args}`）；工具结果在 `tool` 消息的 `data.content` + `data.tool_call_id` + `data.status`（`success` / `error`）。

**边界提醒**：`app.sqlite3`（业务事实）、`logs.sqlite3`（日志旁路）、`langgraph_checkpoints.sqlite`
（workflow 恢复）三者职责分离，不要跨库关联判断 Run 状态——**Run 状态只看 `conversation_runs`**。

---

## 4. 排查工作流（严格按顺序）

### 阶段 A — 收集症状与入口

1. 问清/确认：问题现象、最早出现时机、是否必现、用户手头有什么（报错文本、截图、某个 id）。
2. 若有 id，先按 §0 判定类型：链路 trace_id → 日志脚本；run_id/task_id → 业务库脚本；Langfuse trace → Langfuse 平台。
3. 若只有现象没有 id：用 `query_logs.py recent --errors-only` 与 `query_app_db.py stuck` 反查最近的异常与未收敛记录，再向用户确认时间点。
4. **先用 `Read` / 代码确认相关模块的真实位置**，不要凭记忆猜路径或表名。

### 阶段 B — 拉取证据

> 脚本位置：本 skill 内置 `scripts/query_logs.py`、`scripts/query_app_db.py`（及其 `appdb_*.py` 查询模块），
> 从**仓库根**执行。所有 Python 脚本一律 `uv run --project apps/backend python <script>`（Python 3.11，uv 托管）。
> 输出若含中文/表情符号，脚本已自行把 stdout 切到 UTF-8，Windows GBK 控制台不会再崩。

- **前端相关**（UI 卡死/报错/不更新/白屏）：直接 `Read <data_dir>/runtime/frontend-YYYY-MM-DD.log`，
  按时间倒序看 ERROR/WARNING，关注 `context.trace_id`、`event`、`data`；宿主级问题看 `desktop-*.log`。

- **后端相关**（API 报错/任务失败/工具执行异常）：

```bash
# 按链路 trace 拉完整链路（已知链路 trace_id 时首选，按时间升序）
uv run --project apps/backend python skills/log-triage/scripts/query_logs.py trace <TRACE_ID> --format json --save /tmp/trace.json --force
# 最近错误 / 关键字 / 事件前缀 / 调用方
uv run --project apps/backend python skills/log-triage/scripts/query_logs.py recent --errors-only --limit 100
uv run --project apps/backend python skills/log-triage/scripts/query_logs.py recent --contains "<关键词>" --caller-contains "tools_node"
uv run --project apps/backend python skills/log-triage/scripts/query_logs.py recent --event-prefix "tool_" --since 2026-09-13T10:00:00Z
# 崩溃/起不来：看启动状态与控制台原文
#   Read <data_dir>/runtime/backend.bootstate.json
#   Read <data_dir>/runtime/backend-console-YYYY-MM-DD.log
```

- **业务状态相关**（不启动服务，直连业务库）：

```bash
# 先确认库与 schema 没漂移
uv run --project apps/backend python skills/log-triage/scripts/query_app_db.py schema --no-columns
# 任务 / 运行
uv run --project apps/backend python skills/log-triage/scripts/query_app_db.py tasks --limit 20
uv run --project apps/backend python skills/log-triage/scripts/query_app_db.py runs --limit 20 --status running
# 单个 task / run 完整排障快照（任务/运行/命令/消息/工具/变更/委派一次拿全）
uv run --project apps/backend python skills/log-triage/scripts/query_app_db.py task <TASK_ID> --limit 30
uv run --project apps/backend python skills/log-triage/scripts/query_app_db.py run <RUN_ID> --format json
# 未收敛体检（活跃 run / 指向活跃 run 的 task / 残留流式草稿）
uv run --project apps/backend python skills/log-triage/scripts/query_app_db.py stuck
```

- **Agent 行为相关**（答非所问/工具调用异常/工具参数错/上下文缺口）：

```bash
# 工具调用与结果的配对汇总（排查「模型给错参数」「工具一直失败」的首选视图）
uv run --project apps/backend python skills/log-triage/scripts/query_app_db.py tools <TASK_ID> --limit 50
uv run --project apps/backend python skills/log-triage/scripts/query_app_db.py tools <TASK_ID> --failures-only
uv run --project apps/backend python skills/log-triage/scripts/query_app_db.py tools <TASK_ID> --contains "search_files"
# 逐条回放上下文消息（message_json 解析后的 kind/text/tool_calls/usage/transport 状态）
uv run --project apps/backend python skills/log-triage/scripts/query_app_db.py messages <TASK_ID> --limit 50
uv run --project apps/backend python skills/log-triage/scripts/query_app_db.py messages <TASK_ID> --run-id <RUN_ID> --exclude-streaming
```

### 阶段 C — 交叉验证（关键）

- **同一条链路两边印证**：带链路 trace_id 的问题，应在 `log_entries`（后端日志）与前端日志里都能找到同一 `trace_id` 的记录；只有一侧有 → 说明另一侧观测缺失（进入阶段 D）。
- **同一时间两种坐标对齐**：日志 `ts`（UTC RFC3339 毫秒）与业务库 `created_at` / `updated_at` /
  `sequence` 对齐；`conversation_task_contexts.sequence` 是 task 内全局插入序，用它排列执行时间线，而不是靠时间戳猜测顺序。
- **状态不要只看日志**：日志显示请求成功 ≠ 业务状态正确。以 `conversation_runs.status` / `end_reason` /
  `error_json`、`conversation_task_contexts` 的 `transport_metadata_json.status` 为准；日志是旁路，
  不是事实源。
- **工具结局两处对齐**：Transport 侧生命周期（`transport_metadata_json.status`）与执行结果
  （tool 消息 `data.status` / content 是否以 `error:` 开头）应一致；不一致本身就是重要线索。

### 阶段 D — 证据不足：补日志 + 复现

当信息不足以定位根因（日志只有事件名、无关键入参、关键分支无记录）时：

1. **补日志**（遵循项目《通用日志开发规范》与 AGENTS.md 日志章节）：
   - 必须带**上下文**：`extra["data"]` 中放 task_id / run_id / 路径 / 命令 / 重试状态等定位字段。
   - 异常路径必须写 error 日志；**注意堆栈不会落盘（见 §2）**，因此要把「异常类型之外的因果」显式写进 `data`。
   - 外部依赖调用记：目标、操作名、耗时、状态码、失败原因。
   - **绝不输出 secret / 敏感信息**（API Key、Token、Cookie、手机号、身份证、完整请求正文）。
   - 日志必须分级（DEBUG/INFO/WARNING/ERROR），调试日志不得污染生产默认输出。
   - 补日志改动需同步更新对应函数 docstring，遵循项目「单一职责 / 不重复造轮子」铁律。
2. **复现问题（优先自己复现）**：
   - 后端逻辑 bug：写/跑 pytest 复现（`uv run --project apps/backend pytest <test>`）；新日志会落
     `<repo>/storage/logs.sqlite3`（pytest 未注入 `CODING_AGENT_LOG_DIR` 时文件日志落 `<repo>/logs`），
     业务状态落临时库。
   - 可端到端触发：起后端（`uv run --project apps/backend python -m app`）后调 API（curl 等），
     确认日志落盘与 `app.sqlite3` 状态变化。
   - 前端纯 UI 交互（点击流、视觉、白屏）：**只能请用户复现** —— 明确要求「在 `tauri dev` 下操作复现，
     并把 `<data_dir>/runtime/frontend-YYYY-MM-DD.log` 的最近片段贴给你」。纯浏览器 `vite dev` 不落盘。
   - 复现后必须能定位到具体 `run_id`：`query_app_db.py runs --limit 5` 找到新 run，再用 `run <RUN_ID>` 看全貌。
3. 复现后回到阶段 B，用新证据重新定位；不要停在「可能是 X」。

### 阶段 E — 结论与修复

- 给出**根因**（哪一行/哪个分支/哪个依赖/哪张表），并区分「证据看清了根因」与「仍需用户补充信息」。
- 若需改代码：按项目规范做聚焦修复（单一职责、Docstring 同步、可排查日志）。
- 交付前走开发-审查-测试闭环（独立审查 Agent + 独立测试 Agent），**不自行宣布完成**。
- 若始终无法自证：给出**精确的复现请求**（操作步骤 + 期望看到的哪个 trace/run + 用户应提供的日志片段）。

---

## 5. 纪律（不可违反）

- **日志优先于猜想**：任何「可能是 X」的假设，先去日志/业务库里找证据。
- **三套标识必区分**（见 §0）：链路 trace_id 可跨前后端日志反查；Langfuse trace 只在 Langfuse；业务排查用 run_id/task_id。拿错标识查错通道 = 白查。
- **日志不是事实源**：Run/Tool/文件变更的权威状态在业务库；日志只做旁路印证。
- **复现优先自己来**：pytest > 起后端调 API > 请用户前端复现。能自己复现就别打扰用户。
- **补日志要合规**：上下文 + 分级 + 不泄密、禁止空 catch；记住**堆栈不落盘**，因果要写进 `data`。
- **落盘可查**：自己复现时必须确认证据已落盘（`logs.sqlite3` / `app.sqlite3` / `frontend-*.log`），否则复现无效。
- **不制造噪音**：查询脚本全部只读（`mode=ro`），不要为了排查改业务行为或写库。

---

## 6. 脚本速查（从仓库根执行）

```bash
# ---------- 后端日志库 ----------
uv run --project apps/backend python skills/log-triage/scripts/query_logs.py recent [--errors-only|--warnings-up|--min-level L|--level L] \
    [--contains T] [--event E] [--event-prefix P] [--caller-contains C] [--logger-contains L] \
    [--since/--until/--around T] [--window 2m] [--limit N] [--format text|json] [--save F] [--force]
uv run --project apps/backend python skills/log-triage/scripts/query_logs.py trace <TRACE_ID> [共享选项]

# ---------- 业务库（只读直连，不启动服务） ----------
# 下同：uv run --project apps/backend python skills/log-triage/scripts/query_app_db.py <子命令>
schema      [--table T] [--no-columns]                        # 真实表清单/行数/列
workspaces  [--limit N]
tasks       [--workspace-id I] [--contains T] [--limit N]
runs        [--task-id I] [--status S] [--contains T] [--limit N]
run         <RUN_ID> [--limit N]                              # run 排障快照
task        <TASK_ID> [--limit N]                             # task 排障快照
commands    [--task-id I] [--run-id I] [--limit N]
messages    <TASK_ID> [--run-id I] [--order asc|desc] [--exclude-streaming] [--limit N]
tools       <TASK_ID> [--run-id I] [--contains T] [--failures-only] [--limit N]
delegations [--task-id I] [--limit N]
sessions    [--task-id I] [--status S] [--limit N]
attachments [--task-id I] [--limit N]
providers   [--limit N]                                       # 不含明文 api_key
models      [--provider-id I] [--limit N]
stuck       [--limit N]
# 共享选项：--db PATH  --format text|json  --save FILE  --force

# ---------- 文件与 HTTP ----------
# Read <data_dir>/runtime/frontend-YYYY-MM-DD.log        # 前端日志
# Read <data_dir>/runtime/desktop-YYYY-MM-DD.log          # 宿主日志
# Read <data_dir>/runtime/backend-console-YYYY-MM-DD.log  # 后端 stdout/stderr 原文
# Read <data_dir>/runtime/backend.bootstate.json          # 启动状态
# GET  /logs/query?trace_id=... | /logs/recent            # 后端在跑时的 HTTP 通道
```

> **实现约定**：`query_app_db.py` 是 CLI 表现层（参数解析/分发/渲染），SQL 与业务语义拆在
> `appdb_readonly.py`（只读访问原语）、`appdb_schema.py`、`appdb_agent_facts.py`、
> `appdb_snapshots.py`、`appdb_context.py`、`appdb_side_effects.py`、`appdb_health.py` 七个模块中；
> 脚本**不导入 `app.*`**，表结构事实以在线库与 `apps/backend/app/storage/model` 为准，因此可在服务
> 未启动、启动失败或 UI 打不开时使用。
> `--format json` 给出未经截断的完整字段（文本模式会压缩长文本），排查细节优先用 json。
>
> **维护说明（双份副本风险）**：仓库 `skills/log-triage/` 是受版本管理的**唯一源**；
> 运行时从用户级目录加载（Windows `%USERPROFILE%\.codebuddy\skills\log-triage\`）。
> 修改任一文件后必须同步 `SKILL.md` 与 `scripts/` 到用户级目录，否则 `use_skill` 加载到的仍是旧版本。
> `test/` 只服务仓库内回归，运行时不需要，**不随安装副本同步**。
> `appdb_*.py` 各查询模块必须随 `apps/backend/app/storage/model` 的表结构演进同步更新。
>
> **回归测试（改脚本后必跑）**：`skills/log-triage/test/` 下有 200+ 用例，覆盖布尔归一的
> Python/SQL 双侧一致性、消息回放流式过滤口径、工具调用配对（含跨 run 复用 `tool_call_id`）、
> 未收敛体检、缺表可诊断性、CLI 参数面与 `--save`。从仓库根执行：
> `uv run --project apps/backend pytest -c apps/backend/pyproject.toml skills/log-triage/test -q`
> 改动 `sqlite_values.py` 或任一过滤条件时，务必确认该套件全绿——口径分叉类缺陷不会报错，只会
> 静默给出错误结论。
> 脚本与测试同时受项目 ruff 规则约束：
> `uv run --project apps/backend ruff check --config apps/backend/pyproject.toml skills/log-triage/scripts skills/log-triage/test`。
>
> **路径解析约定**：两个脚本的默认库路径都先按「脚本所在目录」、再按「当前工作目录」向上查找
> 含 `apps/backend` 的目录作为仓库根。因此**只要 cwd 在仓库根**，用仓库内相对路径或用户级
> 安装路径调用都能正确定位 `storage/app.sqlite3` 与 `storage/logs.sqlite3`；两条路径都找不到
> 时会明确报错，此时用 `--db` 显式指定库路径即可。

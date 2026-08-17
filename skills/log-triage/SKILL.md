---
name: log-triage
description: |
  Use when investigating or root-causing problems in the local coding-agent desktop project:
  bugs, exceptions, crashes, hangs, failed turns, stuck tasks, missing UI updates, tool failures,
  unexpected Agent behavior, or requests to "看日志排查", "从数据库排查", "不启动服务直连数据库",
  "查 task/turn/runtime_events", "trace 在哪", "logs.sqlite3", "app.sqlite3", "desktop.log",
  "Agent 回放", or "复现问题".
allowed-tools: Read,Write,Bash
---

# log-triage — 日志驱动问题排查

本技能用于对本项目（`coding-agent` 本地桌面 coding-agent）进行**证据驱动的问题排查**。
核心理念：**先查日志和本地数据库，再谈猜想；证据不足就补日志并复现，优先自己复现**。

---

## 0. 两种 trace 铁律（最先读，最容易混淆）

本项目里"trace"一词出现**两**套体系，**形态都是 32 位小写 hex，但来源、生成机制、查询通道完全不同，绝不能混用**：

| 维度 | 后端日志 trace_id | Agent turn trace |
|------|-------------------|------------------------|
| 是什么 | 后端结构化日志的**链路键**，由 `LogContextStore` 的 `ContextVar` 维护（入口层绑定，下层继承） | 每次 **turn** 由 `turn_trace()` 预分配的 Agent trace_id，用于排查 Agent 执行过程 |
| 产生点 | 入口中间件 `apps/backend/app/api/middleware/api_logging.py` 从 HTTP 头 `x-trace-id`（前端透传）绑定进 `LogContextStore`；落库时由 `apps/backend/app/config/logging/filter/log_context_filter.py` 自动回填 `trace_id` | `apps/backend/app/utils/trace_infra/ids.py:new_trace_id()`（`uuid4().hex`），经 `apps/backend/app/core/observability/langfuse_tracing.py:turn_trace()` 注入 `CallbackHandler` |
| 写入 | 落盘到 `storage/logs.sqlite3` 的 `log_entries.trace_id` 列 | Agent 执行内容落盘到 `storage/app.sqlite3` 的 `turn_messages` / `runtime_events` / `turns` 等表 |
| 查询通道 | `skills/log-triage/scripts/query_logs.py trace <id>`（skill 内置副本，也可直接用仓库根 `scripts/query_logs.py` 原版） | `skills/log-triage/scripts/query_app_db.py turn <TURN_ID>` / `events --contains <TRACE_OR_KEYWORD>` |
| 前端是否有 | 前端 `logs/desktop.log` 的 `context.trace_id` **就是前端经 `x-trace-id` 注入、后端日志复用的同一链路 trace_id**（同源、同坐标系），可直接 `query_logs.py trace` 反查 | 无（前端不直接持有 Agent turn trace） |

> **前端 trace 的真相（重要，避免误判）**：前端 `clientTraceStore` 生成的 `traceId`（`randomHex(16)`，
> 32 位 hex）随请求以 HTTP 头 `x-trace-id` 发出；后端 `api_logging` 中间件读到后**直接作为后端日志链路
> trace_id** 绑定进 `LogContextStore`。所以 `logs/desktop.log` 里的 `trace_id` 与 `storage/logs.sqlite3`
> 里的 `trace_id` **是同一坐标系**——前端日志里的 trace_id 可以、也应该拿去 `query_logs.py trace` 查。
> 它"不是 Agent turn trace"这一点是真的，但"与后端日志 trace_id 不是同一坐标系"是**错的**。

**实战要点**：
- 用户给一个 32 位 hex，**先问清楚它从哪来**：是后端日志 / 前端日志里看到的（→ 后端日志 trace，用 `query_logs.py trace` 查），还是 Agent turn / runtime event payload 里看到的（→ 用 `query_app_db.py` 查业务库）。
- 不要拿 Agent turn trace 去 `query_logs.py trace` 查（查不到或串错链路）；反之亦然。
- 两套 trace 由**不同机制**生成：Agent turn trace 由 `turn_trace()` 在 runner 内预分配；后端日志 trace_id 由 `api_logging` 入口层从 `x-trace-id`（前端透传，或后端补生成）绑定。**两者不要互相替代查询**。
- 一次 turn 里两套 trace 可并存，但**来源不同、数值无关**，不要用其中一套去关联另一套。

---

## 1. 三类证据源与位置

| 源 | 落盘位置 | 形态 | 查询方式 |
|----|----------|------|----------|
| 前端日志 | `logs/desktop.log`（仓库根，Tauri `tauri dev` / 生产落盘；纯浏览器 `vite dev` 不落盘） | 单行文本 + 结构化 `context`/`stack` | 直接 `Read` 文件，或 `grep` 关键字 |
| 后端日志 | `storage/logs.sqlite3`（`log_entries` 表，JSON 字段） | 结构化 9 列：ts/level/logger/trace_id/caller/event/msg/data/error/truncated | `skills/log-triage/scripts/query_logs.py`（skill 内置副本） |
| 业务数据库 / Agent 回放 | `storage/app.sqlite3`（表以 `apps/backend/app/storage/model` 为事实源） | `workspaces/tasks/turns/turn_messages/runtime_events/delegations/file_snapshots`；Agent 回放直接从 `turn_messages` / `runtime_events` 读 | `skills/log-triage/scripts/query_app_db.py`（只读直连，不启动服务，不导入 `app.*`） |

**前置确认**：开始排查前，先用 CodeGraph / 读代码确认相关模块的真实代码位置（不要凭记忆猜路径）。

---

## 2. 排查工作流（严格按顺序）

### 阶段 A — 收集症状与入口
1. 问清/确认：问题现象、最早出现时机、是否必现、用户手头有什么（报错文本、某个 trace id、截图）。
2. 若有 trace id，**先按 §0 判断它是日志 trace 还是 Agent turn trace**。
3. 若有任务/turn/run id，也能在日志反查表（进程内 `run_id/task_id -> trace_id`）关联，但注意反查表是运行进程内态，
   离线排查时优先直接用日志 `trace_id` 或 `--contains <task_id>`。

### 阶段 B — 拉取证据

> 脚本位置：本 skill 内置 `scripts/query_logs.py` 与 `scripts/query_app_db.py`，从**仓库根**执行
> （路径含 `skills/log-triage/`）。Agent 回放直接读取业务数据库，不需要额外 replay 脚本。

- **前端相关**（UI 卡死/报错/不更新）：`Read logs/desktop.log`，按时间倒序看最近的 ERROR/WARN，
  关注 `context`（含前端 trace_id、task_id）与 `stack`。
- **后端相关**（API 报错/任务失败/工具执行异常）：
  ```bash
  # 按 trace 拉完整链路（优先，若已知日志 trace_id）
  uv run --project apps/backend python skills/log-triage/scripts/query_logs.py trace <LOG_TRACE_ID> --format json --save /tmp/trace.json
  # 或按时间倒序看最近错误
  uv run --project apps/backend python skills/log-triage/scripts/query_logs.py recent --errors-only --limit 100
  # 按关键字/事件/调用方过滤
  uv run --project apps/backend python skills/log-triage/scripts/query_logs.py recent --contains "<关键词>" --caller-contains "<模块>"
  ```
- **业务状态相关**（不启动服务，直连业务库排查 task/turn/event/委派/文件变更）：
  ```bash
  # 看业务库表结构、行数，确认是否查对库
  uv run --project apps/backend python skills/log-triage/scripts/query_app_db.py schema
  # 最近任务 / 最近轮次
  uv run --project apps/backend python skills/log-triage/scripts/query_app_db.py tasks --limit 20
  uv run --project apps/backend python skills/log-triage/scripts/query_app_db.py turns --limit 20
  # 单个 task / turn 的完整排障快照
  uv run --project apps/backend python skills/log-triage/scripts/query_app_db.py task <TASK_ID> --format json
  uv run --project apps/backend python skills/log-triage/scripts/query_app_db.py turn <TURN_ID> --format json
  # 查 runtime_events，定位工具调用、模型输出、错误 payload
  uv run --project apps/backend python skills/log-triage/scripts/query_app_db.py events --task-id <TASK_ID> --order asc
  uv run --project apps/backend python skills/log-triage/scripts/query_app_db.py events --contains "<关键词>" --limit 100
  # 查疑似未收敛的 task/turn
  uv run --project apps/backend python skills/log-triage/scripts/query_app_db.py stuck --limit 50
  ```
- **Agent 行为相关**（答非所问/工具调用异常/模型输出错误）：
  ```bash
  # 直接从业务库读取 Agent 回放内容：turn_messages 是消息轨迹，runtime_events 是执行时间线
  uv run --project apps/backend python skills/log-triage/scripts/query_app_db.py turn <TURN_ID> --format json
  uv run --project apps/backend python skills/log-triage/scripts/query_app_db.py events --turn-id <TURN_ID> --order asc --format json
  uv run --project apps/backend python skills/log-triage/scripts/query_app_db.py events --contains "<TOOL_CALL_ID_OR_TRACE_OR_KEYWORD>" --format json
  ```

### 阶段 C — 交叉验证（关键）
- 同一问题应优先在**后端日志**和**业务数据库 runtime_events/turns/tasks** 两处互相印证；若涉及 agent 行为，用 `turn_messages` 对齐上下文消息，用 `runtime_events.sequence` 对齐执行时间线。
- 用日志里的 `ts` / `event`、业务库里的 `created_at` / `sequence` 对齐时间，确认是否同一事件。
- **若日志里有关键事件但业务库缺失对应 turn/event**（或反之）：说明某一侧观测缺失或持久化失败——这是"证据不完整"的信号，进入阶段 D。
- **若日志显示请求成功但业务库状态异常**：优先查 `turns.status/end_reason/response_text`、`runtime_events.event_type/payload_json`、`delegations.status/error`，不要只凭日志判断执行结果。

### 阶段 D — 证据不足：补日志 + 复现
当信息不足以定位根因（日志缺失上下文、只有"失败了"无堆栈、关键分支无记录）时：

1. **补日志**（遵循项目规范《通用日志开发规范》第六章）：
   - 必须带**上下文**：trace_id、task_id/run_id、关键业务 id、操作名。
   - 异常路径必须写 **error 日志（含异常类型、message、堆栈、可重试状态）**，禁止空 `catch`、禁止只 `return`。
   - 外部依赖调用记：目标、操作名、耗时、状态码、失败原因。
   - **绝不输出 secret / 敏感个人信息**（API Key、Token、Cookie、手机号、身份证等）。
   - 日志必须分级（info/warn/error），调试日志不得污染生产默认输出。
   - 补日志改动需同步更新对应函数 docstring，遵循项目"单一职责 / 不重复造轮子"铁律。

   **后端怎么写（照抄）**：统一用 `log` 单例，禁止 `print`/`console` 当系统日志。
   ```python
   from app.config.logging.logger import log

   # 第一个位置参数 = 机器可筛事件名（snake_case 英文，须匹配 ^[a-z][a-z0-9_]*$，否则落库为 log_event）
   # extra["msg"] = 给人看的中文消息；业务字段建议收进 extra["data"]
   log.info("file_written", extra={"msg": "已写入文件", "data": {"path": path}})
   log.warning("retry", extra={"msg": "第2次失败将降级", "data": {"attempt": 2}})
   # 异常路径：exc_info=True 自动带堆栈，error 级（stack 由框架提取，勿手填）
   try:
       ...
   except Exception as e:
       log.error("op_failed", extra={"msg": "执行失败", "data": {"retryable": False}}, exc_info=True)
   ```
   - 事件名是**第一个位置参数**（英文 snake_case），不是 extra 字段；`query_logs.py --event-prefix` 查的就是它。
   - `extra["msg"]` 写给人看的中文消息；`trace_id` 由 `LogContextFilter` 自动回填，**不用手动传**；`task_id`/`run_id` 等业务 id 放 `extra["data"]`。

   **前端怎么写（照抄）**：统一从 `@/lib/logger` 调，禁止 `console.log` 当系统日志。
   ```ts
   import { logInfo, logWarn, logError } from "@/lib/logger";

   // 事件名写进 message，用 module 标识来源；业务字段放 context
   logInfo("file_written", { module: "file_io", path });
   logWarn("retry", { module: "file_io", attempt: 2 });
   // logError 签名 (message, error, context?)：error 对象必须放第2位，stack 自动提取，勿手填
   logError("op_failed", e, { module: "file_io", retryable: false });
   ```
   - `context` 自动带 `trace_id`（前端 `x-trace-id` 透传，与后端日志同源），**不用手动传**；前端用 `module` 标识模块，无 event 列。
2. **复现问题（优先自己复现）**：
   - 后端逻辑 bug：写/跑 **pytest** 复现（`uv run --project apps/backend pytest <test>`），让新日志落盘到 `storage/logs.sqlite3`，并按需检查 `storage/app.sqlite3` 的 task/turn/event 状态。
   - 可端到端触发：起后端（`uv run --project apps/backend python -m app`）后调 API（curl / 现有 tests 辅助脚本），确认日志落盘。
   - 前端纯 UI 交互（点击流、视觉）：**只能请用户复现**——明确要求用户"在 `tauri dev` 下操作复现，并把 `logs/desktop.log` 的最近片段贴给你"。纯浏览器 `vite dev` 不落盘，必须走 `tauri dev`。
   - 复现时必须**带 trace**：后端日志自动带日志 trace_id；若排查 agent 行为，复现后必须能在业务库查到对应 `turn_id` / `runtime_events`。
3. 复现后回到阶段 B，用新日志重新定位。

### 阶段 E — 结论与修复
- 给出**根因**（哪一行/哪个分支/哪个依赖），区分"日志看清了根因"还是"仍需用户补充信息"。
- 若需改代码：按项目规范做最小、聚焦的修复（单一职责、改动聚焦、Docstring 同步、可排查日志）。
- 交付前走开发-审查-测试闭环（独立审查 Agent + 独立测试 Agent），不自行宣布完成。
- 若始终无法自证：给出**精确的复现请求**（操作步骤 + 期望看到哪个 trace + 用户应提供的日志片段），交给用户。

---

## 3. 纪律（不可违反）

- **日志优先于猜想**：任何"可能是 X"的假设，先去日志里找证据。
- **两种 trace 必区分**（见 §0）：特指"后端日志链路 trace_id"与"Agent turn trace"两套体系——来源、机制、查询通道不同，不要互相替代查询（前者含前端 `x-trace-id` 透传，同源同坐标系；后者用于 Agent 执行排查）。拿错 trace 查错通道 = 白查。
- **复现优先自己来**：pytest > 起后端调 API > 请用户前端复现。能自己复现就别打扰用户。
- **补日志要合规**：上下文 + 堆栈 + 分级 + 不泄密，禁止空 catch。
- **落盘可查**：自己复现时必须确认日志和业务状态已落盘（后端 `storage/logs.sqlite3` / 业务库 `storage/app.sqlite3` / 前端 `logs/desktop.log`），否则复现无效。
- **不制造噪音**：查询脚本只读（`query_logs.py` / `query_app_db.py` 都用只读连接），不要为了排查改业务行为。

---

## 4. 脚本速查（从仓库根执行）

```bash
# 后端日志（skill 内置副本）
uv run --project apps/backend python skills/log-triage/scripts/query_logs.py recent  [--errors-only|--warnings-up] [--contains T] [--event-prefix E] [--caller-contains C] [--since/--until/--around] [--limit N] [--format json] [--save F]
uv run --project apps/backend python skills/log-triage/scripts/query_logs.py trace   <LOG_TRACE_ID> [共享选项] [--format json] [--save F]

# 业务数据库（skill 内置副本；直连 storage/app.sqlite3，不启动服务）
uv run --project apps/backend python skills/log-triage/scripts/query_app_db.py schema
uv run --project apps/backend python skills/log-triage/scripts/query_app_db.py tasks  [--status S] [--contains T] [--limit N] [--format json]
uv run --project apps/backend python skills/log-triage/scripts/query_app_db.py turns  [--status S] [--task-id ID] [--contains T] [--limit N] [--format json]
uv run --project apps/backend python skills/log-triage/scripts/query_app_db.py task   <TASK_ID> [--limit N] [--format json]
uv run --project apps/backend python skills/log-triage/scripts/query_app_db.py turn   <TURN_ID> [--limit N] [--format json]
uv run --project apps/backend python skills/log-triage/scripts/query_app_db.py events [--task-id ID] [--turn-id ID] [--type T] [--contains T] [--order asc|desc] [--limit N] [--format json]
uv run --project apps/backend python skills/log-triage/scripts/query_app_db.py stuck  [--limit N] [--format json]

# 前端
Read logs/desktop.log   # 直接读，按时间倒序关注 ERROR/WARN
```

> `query_app_db.py` 是 skill 专用脚本，表结构以 `apps/backend/app/storage/model` 为事实源，但脚本自身不导入
> `app.*`，只读直连 SQLite，适合服务未启动、启动失败、UI 无法打开时排查问题。
> `query_logs.py` 内置副本与仓库根 `scripts/query_logs.py` 原版逻辑一致；唯一差异是仓库根定位改为"向上查找含 `apps/backend` 的目录"，
> 因此无论从何处调用都能正确指向真实仓库根。也可直接改用仓库根原版脚本，效果相同。
> 所有 Python 脚本一律用 `uv run --project apps/backend python <script>` 执行（Python 3.11，uv 托管）。
>
> **维护说明（双份脚本风险）**：本 skill 内置 `query_logs.py` 与仓库根 `scripts/query_logs.py` 原版应保持一致。
> 修改任一脚本后需同步另一份；`query_app_db.py` 是 skill 专用排障脚本，随 `apps/backend/app/storage/model` 表结构演进。

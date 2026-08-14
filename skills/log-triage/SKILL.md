---
name: log-triage
description: |
  Log-driven problem triage for the coding-agent project. Use this skill whenever the user reports a
  bug, exception, unexpected behavior, or "something broke" in the local desktop coding-agent
  (backend FastAPI/LangGraph or the Tauri/React frontend), and wants it root-caused from logs.

  This skill wires together the three project-native evidence sources — frontend logs
  (logs/desktop.log), backend structured logs (storage/logs.sqlite3), and Langfuse agent
  replays (storage/langfuse-replays/*.json) — into one repeatable triage workflow:

    1. Locate and read the relevant logs.
    2. If evidence is insufficient, add logs, then re-trigger the problem (prefer self-reproduction;
       ask the user only when UI interaction is required).
    3. Cross-check backend log trace_id (same coordinate system as the frontend x-trace-id) vs
       Langfuse agent trace_id (a separate, Langfuse-only trace; do NOT expect them to match).
    4. Conclude with root cause + minimal fix, or a precise reproduction request.

  Trigger scenarios — use when the user mentions any of:
  - bug / 报错 / 崩溃 / 异常 / 闪退 / 卡死 / 不工作 / 行为不对
  - "看日志排查" / "从日志定位" / "日志里有什么" / "trace 在哪"
  - 复现问题 / reproduce / 怎么触发这个 bug
  - 后端日志 / desktop.log / logs.sqlite3 / Langfuse 回放 / replay
  - 日志 trace / agent trace / 两个 trace 不一样（注：前端日志 trace 与后端日志 trace 同源，是同一个）
description_zh: "基于日志的编码助手问题排查技能：串联前端日志、后端结构化日志与 Langfuse Agent 回放，定位根因；证据不足时补日志并重跑复现（优先自己复现），并明确区分后端日志 trace_id 与 Langfuse agent trace。"
description_en: "Log-driven triage for the coding-agent project: correlate frontend logs, backend structured logs, and Langfuse agent replays to root-cause bugs; add logs and self-reproduce when evidence is thin; distinguishes backend log trace_id from Langfuse agent trace."
version: 1.0.0
allowed-tools: Read,Write,Bash
display_name: "log-triage"
display_name_en: "log-triage"
visibility: "project"
---

# log-triage — 日志驱动问题排查

本技能用于对本项目（`coding-agent` 本地桌面 coding-agent）进行**基于日志**的问题排查。
核心理念：**先查日志、再谈猜想；证据不足就补日志并复现，优先自己复现**。

---

## 0. 两种 trace 铁律（最先读，最容易混淆）

本项目里"trace"一词出现**两**套体系，**形态都是 32 位小写 hex，但来源、生成机制、查询通道完全不同，绝不能混用**：

| 维度 | 后端日志 trace_id | Langfuse / Agent trace |
|------|-------------------|------------------------|
| 是什么 | 后端结构化日志的**链路键**，由 `LogContextStore` 的 `ContextVar` 维护（入口层绑定，下层继承） | 每次 **turn** 由 `turn_trace()` 预分配的 Langfuse trace_id |
| 产生点 | 入口中间件 `apps/backend/app/api/middleware/api_logging.py` 从 HTTP 头 `x-trace-id`（前端透传）绑定进 `LogContextStore`；落库时由 `apps/backend/app/config/logging/filter/log_context_filter.py` 自动回填 `trace_id` | `apps/backend/app/utils/trace_infra/ids.py:new_trace_id()`（`uuid4().hex`），经 `apps/backend/app/core/observability/langfuse_tracing.py:turn_trace()` 注入 `CallbackHandler` |
| 写入 | 落盘到 `storage/logs.sqlite3` 的 `log_entries.trace_id` 列 | 上报到 Langfuse 平台（远程），本地仅 replay 文件 |
| 查询通道 | `skills/log-triage/scripts/query_logs.py trace <id>`（skill 内置副本，也可直接用仓库根 `scripts/query_logs.py` 原版） | `skills/log-triage/scripts/fetch_langfuse_replay.py` + `skills/log-triage/scripts/analyze_langfuse_replay.py`（skill 内置副本，也可直接用仓库根原版） |
| 前端是否有 | 前端 `logs/desktop.log` 的 `context.trace_id` **就是前端经 `x-trace-id` 注入、后端日志复用的同一链路 trace_id**（同源、同坐标系），可直接 `query_logs.py trace` 反查 | 无（前端不直接持有 Langfuse agent trace） |

> **前端 trace 的真相（重要，避免误判）**：前端 `clientTraceStore` 生成的 `traceId`（`randomHex(16)`，
> 32 位 hex）随请求以 HTTP 头 `x-trace-id` 发出；后端 `api_logging` 中间件读到后**直接作为后端日志链路
> trace_id** 绑定进 `LogContextStore`。所以 `logs/desktop.log` 里的 `trace_id` 与 `storage/logs.sqlite3`
> 里的 `trace_id` **是同一坐标系**——前端日志里的 trace_id 可以、也应该拿去 `query_logs.py trace` 查。
> 它"不是 Langfuse agent trace"这一点是真的，但"与后端日志 trace_id 不是同一坐标系"是**错的**。

**实战要点**：
- 用户给一个 32 位 hex，**先问清楚它从哪来**：是后端日志 / 前端日志里看到的（→ 后端日志 trace，用 `query_logs.py trace` 查），还是 Langfuse UI 上看到的（→ Langfuse trace，用 fetch/analyze 查）？
- 不要拿 Langfuse trace_id 去 `query_logs.py trace` 查（查不到或串错链路）；反之亦然。
- 两套 trace 由**不同机制**生成：Langfuse trace_id 由 `turn_trace()` 在 runner 内预分配，仅出现在 Langfuse replay 与终态事件 payload；后端日志 trace_id 由 `api_logging` 入口层从 `x-trace-id`（前端透传，或后端补生成）绑定。**两者不要互相替代查询**。
- 一次 turn 里两套 trace 可并存，但**来源不同、数值无关**，不要用其中一套去关联另一套。

---

## 1. 三类证据源与位置

| 源 | 落盘位置 | 形态 | 查询方式 |
|----|----------|------|----------|
| 前端日志 | `logs/desktop.log`（仓库根，Tauri `tauri dev` / 生产落盘；纯浏览器 `vite dev` 不落盘） | 单行文本 + 结构化 `context`/`stack` | 直接 `Read` 文件，或 `grep` 关键字 |
| 后端日志 | `storage/logs.sqlite3`（`log_entries` 表，JSON 字段） | 结构化 9 列：ts/level/logger/trace_id/caller/event/msg/data/error/truncated | `skills/log-triage/scripts/query_logs.py`（skill 内置副本） |
| Langfuse 回放 | `storage/langfuse-replays/<trace_id>.json` | Langfuse trace 树 | `skills/log-triage/scripts/fetch_langfuse_replay.py` 抓取 + `skills/log-triage/scripts/analyze_langfuse_replay.py` 分析（skill 内置副本） |

**前置确认**：开始排查前，先用 CodeGraph / 读代码确认相关模块的真实代码位置（不要凭记忆猜路径）。

---

## 2. 排查工作流（严格按顺序）

### 阶段 A — 收集症状与入口
1. 问清/确认：问题现象、最早出现时机、是否必现、用户手头有什么（报错文本、某个 trace id、截图）。
2. 若有 trace id，**先按 §0 判断它是日志 trace 还是 Langfuse trace**。
3. 若有任务/turn/run id，也能在日志反查表（进程内 `run_id/task_id -> trace_id`）关联，但注意反查表是运行进程内态，
   离线排查时优先直接用日志 `trace_id` 或 `--contains <task_id>`。

### 阶段 B — 拉取证据

> 脚本位置：本 skill 已内置 `scripts/query_logs.py`、`scripts/fetch_langfuse_replay.py`、
> `scripts/analyze_langfuse_replay.py` 三份副本（与仓库根 `scripts/` 原版逻辑一致；
> 仅把仓库根定位逻辑改为"向上查找含 `apps/backend` 的目录"，使其在 skill 目录内也能正确落库）。
> 下例一律用 skill 内置副本，从**仓库根**执行（路径含 `skills/log-triage/`）。

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
- **Agent 行为相关**（答非所问/工具调用异常/模型输出错误）：
  ```bash
  # 抓取 Langfuse replay（需 env: CODING_AGENT_LANGFUSE_* 或 --public-key/--secret-key）
  uv run --project apps/backend python skills/log-triage/scripts/fetch_langfuse_replay.py <LANGFUSE_TRACE_ID>
  # 离线分析
  uv run --project apps/backend python skills/log-triage/scripts/analyze_langfuse_replay.py storage/langfuse-replays/<LANGFUSE_TRACE_ID>.json summary
  uv run --project apps/backend python skills/log-triage/scripts/analyze_langfuse_replay.py storage/langfuse-replays/<LANGFUSE_TRACE_ID>.json timeline
  uv run --project apps/backend python skills/log-triage/scripts/analyze_langfuse_replay.py storage/langfuse-replays/<LANGFUSE_TRACE_ID>.json hotspots
  ```

### 阶段 C — 交叉验证（关键）
- 同一问题应在**后端日志**和（若涉及 agent）**Langfuse replay** 两处都看到对应痕迹。
- 用日志里的 `ts` / `event` 与 replay 的 timeline 对齐时间，确认是否同一事件。
- **若日志里有关键事件但 replay 缺失**（或反之）：说明某一侧观测缺失——这是"日志不清"的信号，进入阶段 D。

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
   - 后端逻辑 bug：写/跑 **pytest** 复现（`uv run --project apps/backend pytest <test>`），让新日志落盘到 `storage/logs.sqlite3`。
   - 可端到端触发：起后端（`uv run --project apps/backend python -m app`）后调 API（curl / 现有 tests 辅助脚本），确认日志落盘。
   - 前端纯 UI 交互（点击流、视觉）：**只能请用户复现**——明确要求用户"在 `tauri dev` 下操作复现，并把 `logs/desktop.log` 的最近片段贴给你"。纯浏览器 `vite dev` 不落盘，必须走 `tauri dev`。
   - 复现时必须**带 trace**：后端日志自动带日志 trace_id；若排查 agent 行为，确保 `LANGFUSE_ENABLED` 开启以拿到 Langfuse trace_id。
3. 复现后回到阶段 B，用新日志重新定位。

### 阶段 E — 结论与修复
- 给出**根因**（哪一行/哪个分支/哪个依赖），区分"日志看清了根因"还是"仍需用户补充信息"。
- 若需改代码：按项目规范做最小、聚焦的修复（单一职责、改动聚焦、Docstring 同步、可排查日志）。
- 交付前走开发-审查-测试闭环（独立审查 Agent + 独立测试 Agent），不自行宣布完成。
- 若始终无法自证：给出**精确的复现请求**（操作步骤 + 期望看到哪个 trace + 用户应提供的日志片段），交给用户。

---

## 3. 纪律（不可违反）

- **日志优先于猜想**：任何"可能是 X"的假设，先去日志里找证据。
- **两种 trace 必区分**（见 §0）：特指"后端日志链路 trace_id"与"Langfuse/Agent trace"两套体系——来源、机制、查询通道完全不同，不要互相替代查询（前者含前端 `x-trace-id` 透传，同源同坐标系；后者仅 Langfuse 独有）。拿错 trace 查错通道 = 白查。
- **复现优先自己来**：pytest > 起后端调 API > 请用户前端复现。能自己复现就别打扰用户。
- **补日志要合规**：上下文 + 堆栈 + 分级 + 不泄密，禁止空 catch。
- **落盘可查**：自己复现时必须确认日志已落盘（后端 `storage/logs.sqlite3` / 前端 `logs/desktop.log`），否则复现无效。
- **不制造噪音**：查询脚本只读（`query_logs.py` 用只读连接），不要为了排查改业务行为。

---

## 4. 脚本速查（均为 skill 内置副本，从仓库根执行）

```bash
# 后端日志（skill 内置副本）
uv run --project apps/backend python skills/log-triage/scripts/query_logs.py recent  [--errors-only|--warnings-up] [--contains T] [--event-prefix E] [--caller-contains C] [--since/--until/--around] [--limit N] [--format json] [--save F]
uv run --project apps/backend python skills/log-triage/scripts/query_logs.py trace   <LOG_TRACE_ID> [共享选项] [--format json] [--save F]

# Langfuse 回放（skill 内置副本）
uv run --project apps/backend python skills/log-triage/scripts/fetch_langfuse_replay.py  <LANGFUSE_TRACE_ID>   # -> storage/langfuse-replays/<id>.json
uv run --project apps/backend python skills/log-triage/scripts/analyze_langfuse_replay.py <replay.json>        summary|timeline|hotspots|generations|inspect|export-llm

# 前端
Read logs/desktop.log   # 直接读，按时间倒序关注 ERROR/WARN
```

> 内置副本与仓库根 `scripts/` 原版逻辑一致；唯一差异是仓库根定位改为"向上查找含 `apps/backend` 的目录"，
> 因此无论从何处调用都能正确指向真实仓库根。也可直接改用仓库根原版脚本，效果相同。
> 所有 Python 脚本一律用 `uv run --project apps/backend python <script>` 执行（Python 3.11，uv 托管）。
>
> **维护说明（双份脚本风险）**：本 skill 内置 `scripts/` 三脚本与仓库根 `scripts/` 原版应保持一致。
> 修改任一脚本后需同步另一份；若担心长期漂移，可改为始终调用仓库根原版（删掉内置副本、`uv run` 命令里路径改回 `scripts/xxx.py`）。

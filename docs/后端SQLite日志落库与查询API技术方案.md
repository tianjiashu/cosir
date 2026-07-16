# 后端 SQLite 日志落库与查询 API 技术方案

## 背景

当前系统已经具备基础日志能力：

- 后端通过 `logging` 标准库输出日志。
- `LogContextFilter` 会自动注入 `trace_id`、`task_id`、`run_id` 等上下文字段。
- `JsonlFormatter` 会把日志写入 `logs/logs-YYYY-MM-DD.log`。
- 客户端到后端的 trace 传导已经收敛为唯一协议：`x-trace-id`。
- 现有 trace 查询仍主要依赖 JSONL 文件扫描，不适合作为长期 UI 查询入口。

下一阶段目标是把日志做成生产级能力：业务代码继续写 `logger.info(...)`、`logger.error(...)`，但日志会同时进入本地日志文件和 SQLite，客户端后续可以按 `trace_id` 查询并渲染完整链路日志。

## 本地桌面应用定位

当前系统是本地桌面客户端形态，不是云服务器形态。因此这里的“生产级”不等于高并发日志平台，也不追求复杂分布式链路追踪。

本方案真正需要解决的是：

- 客户端一次操作触发的后端日志能按同一个 `trace_id` 查到。
- 写日志不能明显拖慢桌面 UI 和 Agent 执行。
- SQLite 故障、锁等待或磁盘异常不能导致业务请求失败。
- 进程退出时尽量 flush 已接收日志。
- 日志文件仍然保留，作为 SQLite 查询链路失效时的兜底事实。

所以方案会保留 `logging.Handler + 有界队列 + 批量写入`，但它的目的不是抗高并发，而是做本地桌面应用所需的响应稳定性、错误隔离和可恢复排查。

## 目标

1. **业务 logger 调用方式不变**
   - 不要求业务代码显式传 `trace_id`。
   - 不新增自定义 logger API。
   - 继续复用 Python 标准 `logging` 机制。

2. **日志双写**
   - 文件日志继续落到 `logs/logs-YYYY-MM-DD.log`，作为人工排查和兜底。
   - SQLite 日志进入独立的 `storage/logs.sqlite3` 的 `log_entries` 表，作为查询与 UI 数据源。
   - 日志库不与运行态、checkpoint、事件存储共用主业务库，避免日志写入故障影响 Agent 运行状态。

3. **按 trace 查询**
   - 第一版查询主维度只围绕 `trace_id`。
   - 可选筛选字段包含 `level`、`task_id`、`run_id`、时间范围和 limit。

4. **生产级写入链路**
   - 日志落库不能阻塞业务主流程。
   - 日志落库失败不能导致业务请求失败。
   - 必须支持有界队列、适度批量写入、优雅关闭、失败降级和测试覆盖。
   - 默认参数按本地桌面使用场景设置，不按云服务高并发场景放大。

5. **客户端可消费**
   - 后端返回结构化日志列表。
   - 后端同时返回通用纯文本日志，客户端可直接渲染。

## 非目标

- 不引入外部日志平台。
- 不接 OpenTelemetry 后端 collector。
- 不做多租户日志权限。
- 不做全文检索引擎。
- 不做高并发日志平台。
- 不做复杂日志采样、分片、冷热分层和外部归档。
- 不在第一版暴露 request_id、client_operation_id、request_trace_id。
- 不把 Agent replay 事件和日志表合并为一张表；trace/replay 事件仍由现有 trace backbone 承载。

## 总体架构

```text
业务代码 logger.info/error
        ↓
Python logging.Logger
        ↓
LogContextFilter 自动注入 trace_id/task_id/run_id
        ↓
LogRecord
        ↓
 ┌───────────────────────┬──────────────────────────┐
 │ FileHandler            │ QueueHandler              │
 │ JsonlFormatter         │ SQLiteLogListener         │
 │ logs/logs-YYYY-MM-DD   │ LogStore 批量写 logs.sqlite3│
 └───────────────────────┴──────────────────────────┘
                                    ↓
                              log_entries
                                    ↓
                              LogQueryService
                                    ↓
                              /logs/query 或 /logs/recent
                                    ↓
                              客户端日志页
```

## 日志级别与事件规范

日志表不仅服务 Agent 执行链路，也服务 HTTP 请求、工具执行、模型调用、审批、存储和系统自身故障排查。因此第一版必须先定义稳定的日志级别、事件命名和错误栈规则。

### 日志级别

#### `DEBUG`

用于开发期细节排查，默认不作为用户主要查看内容。

适用场景：

- prompt 构建摘要。
- context compaction 输入输出摘要。
- workflow 中间状态。
- 工具候选选择过程。
- SQLite 日志队列内部状态。

要求：

- 不打印完整 prompt、完整文件内容、密钥、环境变量值或大段模型输出。
- 只打印可定位问题的摘要字段。

#### `INFO`

用于记录正常业务生命周期节点。

适用场景：

- HTTP 请求开始和正常完成。
- Agent run/task/step 开始与完成。
- 工具调用开始与成功。
- 模型请求开始与成功。
- checkpoint 保存成功。
- 日志查询成功。

要求：

- 每条关键业务日志必须能通过 `trace_id` 串回一次客户端操作。
- Agent 相关日志应尽量带上 `task_id`、`run_id`、`step_id` 或 `tool_call_id`。

#### `WARNING`

用于记录业务仍可继续，但需要关注的异常状态。

注意：

- Python `logging` 标准级别名是 `WARNING`。
- SQLite 和 JSONL 中的 `level` 字段必须保存 `WARNING`。
- 客户端 UI 可以展示为 `WARN`，但查询协议和存储层使用 `WARNING`。

适用场景：

- HTTP 4xx。
- 工具审批等待或被用户拒绝。
- 可恢复重试。
- 使用降级路径。
- 日志队列满后丢弃低级别日志。
- SQLite 日志写入短暂失败但已降级到文件日志。

要求：

- `WARNING` 不代表当前任务一定失败，但后续排查时应该优先看。
- 如果包含异常对象并且有排查价值，应使用 `exc_info=True` 保留栈。

#### `ERROR`

用于记录当前请求、当前任务或当前关键操作失败。

适用场景：

- HTTP 5xx。
- 未捕获异常。
- Agent run 失败。
- workflow step 失败且无法继续。
- 工具执行异常。
- 模型调用失败。
- checkpoint 保存失败。
- SQLite 查询失败。

要求：

- 所有真实异常必须使用 `logger.exception(...)` 或 `logger.error(..., exc_info=True)`。
- 禁止只写 `logger.error(str(error))` 作为最终错误日志。
- `error_type`、`error_message` 和 `stack` 必须进入文件日志与 SQLite。

#### `CRITICAL`

用于记录系统级不可恢复故障，普通业务失败不使用。

适用场景：

- 后端启动关键依赖初始化失败。
- 数据库文件不可访问且无法降级。
- 日志系统自身初始化失败且无法继续提供可排查日志。

要求：

- 第一版不主动设计大量 `CRITICAL` 日志点。
- 能用 `ERROR` 表达的业务失败不要升级为 `CRITICAL`。

### 事件命名

日志事件契约必须区分稳定事件名和可读展示文本。

字段规则：

- `event_name`：稳定事件名，必须使用小写 snake_case，进入 SQLite 独立列，也进入 JSONL。
- `message`：人类可读文本，进入 SQLite 独立列，也进入 JSONL；没有单独展示文本时默认等于 `event_name`。
- `attributes_json`：保存可变业务字段。

业务代码默认写法仍然是：

```python
logger.info("tool_call_finished", extra={"tool_name": tool_name})
```

映射规则：

- `event_name` 从 `LogRecord.getMessage()` 提取。
- `message` 默认等于 `event_name`。
- 如果确实需要更友好的展示文本，使用 `extra={"display_message": "工具执行完成"}`，由 mapper 写入 `message`，同时 `event_name` 仍保持稳定。

禁止把变量拼进 `event_name`。

命名规则：

- 使用小写 snake_case。
- 使用领域前缀。
- 使用过去式或状态词表达结果。

建议事件名：

```text
http_request_started
http_request_finished
http_request_failed
http_unhandled_exception

agent_run_started
agent_run_finished
agent_run_failed
agent_step_started
agent_step_finished
agent_step_failed

tool_call_started
tool_call_finished
tool_call_failed
tool_approval_waiting
tool_approval_denied

model_request_started
model_request_finished
model_request_failed

checkpoint_saved
checkpoint_save_failed
run_resume_started
run_resume_finished
run_resume_failed

log_query_started
log_query_finished
log_query_failed
sqlite_log_write_failed
```

理由：

- 稳定事件名方便 SQLite 查询和客户端筛选。
- 额外说明放入 `attributes`，不要把变量拼进 message。

推荐写法：

```python
logger.info(
    "tool_call_finished",
    extra={"tool_name": tool_name, "duration_ms": duration_ms},
)
```

可选展示文本写法：

```python
logger.info(
    "tool_call_finished",
    extra={
        "tool_name": tool_name,
        "duration_ms": duration_ms,
        "display_message": "工具执行完成",
    },
)
```

不推荐写法：

```python
logger.info(f"tool {tool_name} finished in {duration_ms}ms")
```

### HTTP 请求日志

HTTP middleware 负责统一记录请求日志，业务路由不重复打印入口和出口。

建议规则：

- 请求进入：`http_request_started`，级别 `INFO`。
- 2xx/3xx 完成：`http_request_finished`，级别 `INFO`。
- 4xx 完成：默认 `WARNING`；用户取消、审批拒绝、预期校验失败可以按业务语义降为 `INFO`，并通过 `attributes.reason` 标明原因。
- 5xx 完成：`http_request_failed`，级别 `ERROR`。
- 未捕获异常：`http_unhandled_exception`，级别 `ERROR`，必须带错误栈。

`http_request_started` 必须字段：

- `trace_id`
- `method`
- `path`
- `task_id`、`run_id`、`approval_id`：优先从请求上下文、路由参数或 runtime context 获取；路径解析只作为兜底。

`http_request_finished` 和 `http_request_failed` 必须字段：

- `trace_id`
- `method`
- `path`
- `status_code`
- `duration_ms`
- `task_id`、`run_id`、`approval_id`：优先从请求上下文、路由参数或 runtime context 获取；路径解析只作为兜底。

注意：

- `http_request_started` 发生时尚无响应码，不应强制写入 `status_code`。
- 当前代码中的 `unhandled_exception` 应在实现阶段统一改为 `http_unhandled_exception`。

禁止字段：

- 请求 body。
- Authorization、Cookie、API Key。
- 大段用户输入。

### Agent 执行日志

Agent Runtime 是排查链路的核心，应覆盖 run、step、tool、model、checkpoint 的生命周期。

建议规则：

- run/task 开始和结束使用 `INFO`。
- step 开始和结束使用 `INFO`。
- 工具调用开始和成功使用 `INFO`。
- 审批等待、用户拒绝、可恢复重试使用 `WARNING`。
- 工具异常、模型异常、run 失败使用 `ERROR` 并带栈。
- 大模型响应内容默认不完整打印，只记录 provider、model、duration、token 摘要和失败原因。

必须字段：

- `trace_id`
- `task_id`
- `run_id`
- `step_id`：存在时写入。
- `tool_call_id`：工具相关日志必须写入。

### 错误栈规则

错误栈是生产级排查能力的硬要求。

必须遵守：

- 捕获异常后如果需要记录错误，使用 `logger.exception("event_name")`。
- 如果不在 `except` 块中记录异常，使用 `logger.error("event_name", exc_info=True)`。
- SQLite 表必须提供独立 `stack` 字段。
- JSONL 文件中也必须保留 stack，不能只保存在人类可读 message 中。
- `error_type` 保存异常类型，例如 `RuntimeError`。
- `error_message` 保存异常消息。

示例：

```python
try:
    result = await tool_executor.execute(call)
except Exception:
    logger.exception(
        "tool_call_failed",
        extra={"tool_name": call.name, "tool_call_id": call.id},
    )
    raise
```

禁止：

```python
except Exception as error:
    logger.error(f"tool_call_failed: {error}")
```

理由：

- 第二种写法会丢失栈，后续只能看到错误文本，无法定位源码路径。

### 日志字段归属

固定关联字段应进入独立列：

- `event_name`
- `trace_id`
- `task_id`
- `run_id`
- `span_id`
- `event_id`
- `step_id`
- `tool_call_id`
- `approval_id`
- `error_type`
- `error_message`
- `stack`

可变业务字段进入 `attributes_json`：

- `display_message` 映射后的原始值不保留在 attributes 中，避免重复。
- `method`
- `path`
- `status_code`
- `duration_ms`
- `tool_name`
- `model`
- `provider`
- `retry_count`
- `queue_size`

理由：

- 固定字段用于索引和查询。
- 可变字段用于展示和补充上下文，避免表结构频繁膨胀。

## 文件规划

### `apps/backend/app/storage/log_records.py`

新增日志记录数据结构。

建议定义：

- `LogEntryRecord`
- `LogQuery`
- `LogQueryResult`

职责：

- 描述 SQLite 中一条日志的领域结构。
- 描述查询参数和查询结果。
- 不写 SQL，不依赖 FastAPI。

理由：

- 避免 `storage/log_store.py` 里同时堆数据结构和 SQL。
- 后续 API、测试、文本渲染都可以复用同一组 record。

### `apps/backend/app/storage/log_store.py`

新增日志 SQLite 存储。

职责：

- 初始化 `log_entries` 表和索引。
- 批量插入日志。
- 按 `trace_id`、`task_id`、`run_id`、`level`、时间范围查询日志。
- 保证字段映射和 JSON 序列化稳定。
- 配置 SQLite WAL 和 `busy_timeout`，降低本地读写竞争导致的锁等待问题。
- 维护轻量 schema version，支持后续字段演进。
- 使用独立 `storage/logs.sqlite3`，不与主业务库共用连接、事务或 WAL 文件。

建议表结构：

```sql
CREATE TABLE IF NOT EXISTS log_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  level TEXT NOT NULL,
  logger_name TEXT NOT NULL,
  event_name TEXT NOT NULL,
  message TEXT NOT NULL,
  trace_id TEXT,
  task_id TEXT,
  run_id TEXT,
  span_id TEXT,
  event_id TEXT,
  step_id TEXT,
  tool_call_id TEXT,
  approval_id TEXT,
  error_type TEXT,
  error_message TEXT,
  attributes_json TEXT NOT NULL,
  stack TEXT,
  truncated INTEGER NOT NULL DEFAULT 0
);
```

建议索引：

```sql
CREATE INDEX IF NOT EXISTS idx_log_entries_trace_ts
  ON log_entries(trace_id, ts);

CREATE INDEX IF NOT EXISTS idx_log_entries_task_ts
  ON log_entries(task_id, ts);

CREATE INDEX IF NOT EXISTS idx_log_entries_run_ts
  ON log_entries(run_id, ts);

CREATE INDEX IF NOT EXISTS idx_log_entries_level_ts
  ON log_entries(level, ts);

CREATE INDEX IF NOT EXISTS idx_log_entries_ts
  ON log_entries(ts);

CREATE INDEX IF NOT EXISTS idx_log_entries_event_ts
  ON log_entries(event_name, ts);
```

理由：

- `trace_id + ts` 是日志页面最核心查询。
- `task_id/run_id` 是后续任务详情跳转日志的辅助能力。
- `level + ts` 支持快速查看错误日志。
- `ts` 支持 `/logs/recent` 最近日志入口。
- `event_name + ts` 支持后续按事件类型过滤。

时间格式：

- `ts` 必须保存 UTC 时间。
- 格式固定为 RFC3339/ISO8601，示例：`2026-07-15T15:30:12.123Z`。
- 必须使用可按字典序排序的零填充格式，保证 SQLite `ORDER BY ts ASC/DESC` 与时间顺序一致。

SQLite 连接建议：

```sql
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=3000;
PRAGMA foreign_keys=ON;
```

说明：

- WAL 允许读查询和写日志更好地并行，适合本地桌面应用。
- `busy_timeout` 避免短暂写锁导致直接失败。
- 日志落库失败仍不能影响业务，超时后按写入失败策略降级到文件日志。

schema 版本：

- 第一版使用 `PRAGMA user_version = 1`。
- `LogStore.initialize()` 负责读取当前版本并执行增量迁移。
- 未来新增字段时只允许追加迁移，不允许在运行时依赖手工删库。

故障隔离：

- `logs.sqlite3` 写入失败不能影响 `app.sqlite3` 或其他业务状态库。
- `logs.sqlite3` 文件损坏、被锁定或磁盘空间不足时，SQLite handler 进入降级状态，只保留文件日志。
- 降级状态需要限频写入文件日志，避免日志系统故障反复刷屏。

### `apps/backend/app/logging/sqlite_handler.py`

新增 SQLite 日志 handler。

职责：

- 把 `LogRecord` 转换成 `LogEntryRecord`。
- 与 `QueueHandler/QueueListener` 配合，异步批量写入 `LogStore`。
- 捕获落库异常，避免影响业务流程。

建议实现形态：

- `SQLiteLogHandler(logging.Handler)`
- `LogRecordToEntryMapper`
- `SQLiteLogWriter`

生产级要求：

- 队列有最大长度，例如 1000。
- 批量刷盘，例如 50 条或 1 秒。
- 写库失败时回退到标准错误日志或文件 handler，不向业务抛异常。
- 进程关闭时 flush 剩余日志。
- 日志自身失败不能递归写自身，避免日志风暴。
- 入队必须使用非阻塞 `put_nowait` 或等价策略，禁止业务线程等待 SQLite 队列。
- 队列满且高等级日志也无法入队时，允许丢弃 SQLite 入库副本，但文件日志必须已经保留该条日志。

理由：

- Python logging 已经提供成熟扩展点，使用自定义 `logging.Handler` 是标准方案。
- 队列解耦业务线程和 SQLite 写入，降低本地 UI 卡顿与请求延迟风险。
- 本地桌面应用没有高并发压力，因此队列和批量大小应保持克制，避免把简单能力做成重型日志平台。

### `apps/backend/app/logging/jsonl.py`

修改现有 JSONL 日志格式。

职责变化：

- `JsonlLogLine` 增加 `event_name` 和 `stack` 顶层字段。
- `JsonlFormatter` 与 `LogRecordToEntryMapper` 共享同一套 `LogRecord` 字段提取逻辑。
- 旧 JSONL 文件仍可保留，不要求迁移；查询 API 以后以 SQLite 为准。

理由：

- 文件日志和 SQLite 日志字段必须一致，不能一个有 `stack`、一个没有 `stack`。
- mapper 共享可以避免 `JsonlLogLine` 和 `LogEntryRecord` 字段漂移。
- 旧日志文件只是兜底事实，不作为新查询能力的数据源，因此不需要做历史 JSONL 迁移。

建议新增：

- `apps/backend/app/logging/record_mapper.py`

职责：

- 从 `logging.LogRecord` 提取 `event_name`、`message`、`trace_id`、错误字段和 `attributes`。
- 同时服务 `JsonlFormatter` 和 `SQLiteLogHandler`。

### `apps/backend/app/logging/text_renderer.py`

新增纯文本日志渲染器。

职责：

- 把 `LogEntryRecord` 渲染成稳定、可读、可复制的文本行。
- API 可直接返回 `text` 字段给客户端。

建议格式：

```text
2026-07-15T15:30:12.123Z INFO coding_agent.backend event=tool_call_finished trace_id=... task_id=... run_id=... message="工具执行完成"
```

错误日志追加：

```text
error_type=RuntimeError error_message="..."
```

理由：

- 客户端第一版不需要自己拼日志格式。
- 同一份日志在 API、测试和 UI 中显示一致。

### `apps/backend/app/logging/configuration.py`

修改现有日志配置。

职责变化：

- 继续挂载 `FileHandler + JsonlFormatter + LogContextFilter`。
- 新增 SQLite 日志链路初始化。
- 返回或保存可关闭的 listener 引用。
- 后端业务 logger 统一使用 `coding_agent.backend`。
- SQLite handler 只接收 `coding_agent.*` logger。
- 第三方库日志默认只进入文件日志或保持原有行为，不进入 SQLite 查询表。

建议新增函数：

- `configure_logging(log_dir: Path, log_database_file: Path | None = None) -> logging.Logger`
- `shutdown_logging() -> None`

注意：

- 现有调用方现在只传 `log_dir`，实现时要兼容函数签名演进或同步修改调用方。
- 测试里需要避免重复挂 handler。

理由：

- 日志配置是挂 handler 的唯一入口，不能在 API 或 runtime 内部分散配置。
- 限制 SQLite 入库 logger 范围可以减少噪音，避免第三方库把敏感 header、请求体或大量调试信息写入查询库。
- 固定 logger 命名可以避免有人新建 `app.foo` logger 后日志没有进入 SQLite。

### `apps/backend/app/api/logs.py`

新增日志查询 API。

建议接口：

```http
GET /logs/query?trace_id=...&level=ERROR&limit=200
GET /logs/recent?level=ERROR&limit=200
```

返回：

```json
{
  "entries": [
    {
      "ts": "...",
      "level": "ERROR",
      "logger_name": "coding_agent.backend",
      "event_name": "tool_call_failed",
      "message": "...",
      "trace_id": "...",
      "task_id": "...",
      "run_id": "...",
      "attributes": {},
      "error_type": "...",
      "error_message": "...",
      "stack": "..."
    }
  ],
  "text": "..."
}
```

职责：

- 解析 query 参数。
- 调用 `LogQueryService`。
- 返回共享协议格式。
- 提供按 `trace_id` 查询和最近日志查询两个入口。

不做：

- 不直接写 SQL。
- 不直接读取日志文件。

### `apps/backend/app/core/logs/query_service.py`

新增日志查询服务。

职责：

- 组合 `LogStore` 与 `text_renderer`。
- 处理 limit 默认值和最大值。
- 保持 API 层薄。

建议规则：

- 默认 limit：200。
- 最大 limit：1000。
- `/logs/query` 强制 `trace_id`，避免 UI 无意中扫全表。
- `/logs/recent` 不要求 `trace_id`，但必须按 `ts DESC` 查询最近日志，并强制 limit 上限。

理由：

- 查询规则属于业务服务，不应放在 FastAPI 路由或存储层。
- 日志页面打开时需要能先看到最近日志；trace 查询用于定位一次具体客户端操作。

### `apps/backend/app/api/dependencies.py`

修改依赖组装。

职责变化：

- 初始化并暴露 `LogStore`。
- 初始化并暴露 `LogQueryService`。

理由：

- 现有依赖组装已经承载 runtime、trace/replay 等服务，日志查询服务应沿用同一模式。

### `apps/backend/app/api/app.py`

修改 FastAPI app 注册。

职责变化：

- 注册 `logs` router。
- 确保 app startup/shutdown 能启动和关闭日志 listener。

理由：

- SQLite 日志 listener 有生命周期，必须在应用关闭时 flush。

### `packages/shared/ts/logs.ts`

新增前后端共享日志协议。

建议类型：

- `LogLevel`
- `LogEntry`
- `LogQueryRequest`
- `LogQueryResponse`

`LogLevel` 取值：

```ts
export type LogLevel = "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL";
```

`LogEntry` 必须包含：

- `ts`
- `level`
- `logger_name`
- `event_name`
- `message`
- `trace_id`
- `task_id`
- `run_id`
- `attributes`
- `error_type`
- `error_message`
- `stack`

理由：

- 客户端日志页和后端 API 需要共享字段契约。
- 避免客户端重复定义后端响应结构。
- 共享类型中不提供 `WARN`，避免客户端把 UI 展示文案误当成查询协议值。

### `packages/shared/ts/index.ts`

导出 `logs.ts`。

理由：

- 维持共享类型统一入口。

### `apps/desktop/src/services/logs.ts`

后续客户端接入时新增。

职责：

- 调用 `/logs/query` 和 `/logs/recent`。
- 只做 HTTP 通信和错误映射。
- 自动沿用现有 API trace header 机制。

理由：

- `LogsPage` 不应直接 fetch。
- 先完成后端后再接客户端，减少前后端协议来回变动。

### `apps/desktop/src/pages/logs/LogsPage.tsx`

后续客户端接入时修改。

职责变化：

- 从当前 tail 文件展示，升级为查询页。
- 支持输入 `trace_id`、选择 level、刷新。
- 默认打开时调用 `/logs/recent` 展示最近日志。
- 输入 `trace_id` 后调用 `/logs/query` 展示一次客户端操作的完整链路。
- 展示后端返回的纯文本日志。
- 可选展开结构化字段。

理由：

- 客户端只负责交互和展示，不拼日志格式。

## 写入链路设计

### 业务线程

业务线程只做：

```python
logger.info("tool_call_finished", extra={"display_message": "工具执行完成"})
logger.error("model_request_failed", exc_info=True)
```

不能做：

- 显式传 `trace_id`。
- 直接调用 `LogStore`。
- 直接写 SQLite。

### logging filter

`LogContextFilter` 继续负责补齐：

- `trace_id`
- `task_id`
- `run_id`
- `span_id`
- `tool_call_id`
- `approval_id`

### queue handler

SQLite 写入必须走有界队列。

建议策略：

- 入队使用非阻塞策略，禁止等待队列可用。
- 队列满时优先丢弃 DEBUG/INFO 的 SQLite 入库副本。
- 如果新日志是 WARNING/ERROR/CRITICAL，允许尝试丢弃队列中最旧的 DEBUG/INFO，为高等级日志腾出空间。
- 如果队列仍然满，当前高等级日志也允许放弃 SQLite 入库，但不能阻塞业务线程。
- 丢弃发生时写一条限频 warning 到文件日志，事件名建议为 `sqlite_log_queue_overflow`。
- ERROR 写库失败时不能抛给业务，但文件日志必须已经保留该条日志。
- 本地默认队列不宜过大，避免异常场景下占用过多内存。

### SQLite writer

SQLite writer 单线程消费队列。

建议策略：

- 批量写入。
- 每批事务提交。
- 写失败后短暂退避。
- 关闭时 flush 队列。
- 默认批量大小保持在几十条级别，优先保证桌面应用响应稳定，而不是追求最高吞吐。

## 查询链路设计

```text
GET /logs/query 或 GET /logs/recent
        ↓
logs.py 路由解析参数
        ↓
LogQueryService
        ↓
LogStore.query(...)
        ↓
text_renderer.render(...)
        ↓
LogQueryResponse
```

`/logs/query` 查询参数：

- `trace_id`：必填。
- `level`：可选。
- `task_id`：可选。
- `run_id`：可选。
- `start_time`：可选。
- `end_time`：可选。
- `limit`：可选，默认 200，最大 1000。

`/logs/recent` 查询参数：

- `level`：可选。
- `start_time`：可选。
- `end_time`：可选。
- `limit`：可选，默认 200，最大 1000。

排序规则：

- `/logs/query` 默认按 `ts ASC` 返回，方便阅读一次操作的完整时间线。
- `/logs/recent` 默认按 `ts DESC` 返回，方便打开日志页时查看最近问题。
- `start_time` 和 `end_time` 必须使用 UTC RFC3339 格式，例如 `2026-07-15T15:30:12.123Z`。
- 返回结果中的 `ts` 必须保持同一格式。

## 错误处理

### 写入错误

- SQLite 写入失败不能影响业务请求。
- 错误写入文件日志。
- 需要限频，避免 SQLite 故障导致日志风暴。

### 查询错误

- 参数非法返回 422 或 400。
- SQLite 查询失败返回 500，并写 error 日志。
- 查询无结果返回空数组和空文本，不视为错误。

## 数据保留与清理

第一版可以先不做自动清理，但必须预留位置：

- `apps/backend/app/storage/log_retention.py`

后续策略：

- 默认保留最近 14 天或最近 N 条。
- 可按配置控制。
- 清理任务不能阻塞启动。

本次开发不建议立即做 retention，除非实现 SQLite 写入后日志量明显影响本地使用。

## 配置项

建议新增环境变量：

- `CODING_AGENT_SQLITE_LOGGING_ENABLED`：默认 `true`。
- `CODING_AGENT_LOG_DATABASE_FILE`：默认 `storage/logs.sqlite3`。
- `CODING_AGENT_LOG_QUEUE_SIZE`：默认 `1000`。
- `CODING_AGENT_LOG_BATCH_SIZE`：默认 `50`。
- `CODING_AGENT_LOG_FLUSH_INTERVAL_MS`：默认 `1000`。
- `CODING_AGENT_LOG_QUERY_LIMIT_MAX`：默认 `1000`。

对应修改：

- `apps/backend/app/config/settings.py`

理由：

- 生产级能力必须可调。
- 默认值服务本地桌面应用，不按云服务器高并发场景设置。
- 测试可以通过配置关闭队列或缩小批量。

## 测试规划

### `apps/backend/tests/test_log_store.py`

覆盖：

- 建表和索引创建。
- `PRAGMA user_version` 初始化和迁移。
- `PRAGMA journal_mode=WAL` 与 `busy_timeout` 配置。
- 批量插入。
- 按 `trace_id` 查询。
- 按 level 查询。
- `/logs/recent` 所需的 `ts DESC` 排序和 limit。
- limit 校验。
- attributes JSON 读写。
- UTC RFC3339 时间格式排序。
- `event_name` 独立字段读写和查询。

### `apps/backend/tests/test_sqlite_log_handler.py`

覆盖：

- `logger.info` 会进入 SQLite。
- `logger.exception` 会保存错误类型、错误信息和 stack。
- 落库失败不影响业务。
- queue flush 生效。
- 队列满时策略生效。
- 高等级日志在队列满时也不能阻塞业务线程。
- app shutdown 时剩余队列会 flush。
- `LogRecordToEntryMapper` 能把 logger message 映射为 `event_name`，把 `display_message` 映射为 `message`。

### `apps/backend/tests/test_jsonl_formatter.py`

覆盖：

- JSONL 文件日志包含 `event_name`、`message`、`error_type`、`error_message` 和 `stack`。
- JSONL formatter 与 SQLite handler 使用同一 mapper。
- 旧 JSONL 文件不需要迁移，查询 API 不依赖扫描旧文件。

### `apps/backend/tests/test_logs_api.py`

覆盖：

- `/logs/query` 按 `trace_id` 返回结构化日志。
- `/logs/recent` 返回最近日志且不要求 `trace_id`。
- 返回纯文本 `text`。
- 返回 `event_name` 和 `message`。
- 错误日志响应包含 `error_type`、`error_message` 和 `stack`。
- 非法 limit 返回错误。
- 查询无结果返回空。
- API 自身错误会写 error 日志。
- `/logs/recent` 在较大数据量下仍按 `ts DESC` 和 limit 返回。
- 日志库锁冲突或查询失败会降级为 API 错误并写 error 日志，不影响业务库。

### 现有测试需要调整

- `test_logging_configuration.py`：确认 FileHandler 和 SQLite handler 同时挂载。
- `test_logging_context.py`：确认隐式 `trace_id` 能落库。
- `test_api.py`：增加 HTTP 请求日志可查询验证。

## 实施顺序

### 第一步：存储层

实现：

- `log_records.py`
- `log_store.py`
- `test_log_store.py`

验收：

- SQLite 表和索引稳定。
- 查询行为完整。

理由：

- 先把数据层打稳，避免 handler 和 API 依赖未成型结构。

### 第二步：日志落库 handler

实现：

- `sqlite_handler.py`
- 修改 `configuration.py`
- 修改 `settings.py`
- `test_sqlite_log_handler.py`

验收：

- 业务 logger 调用不变。
- 文件日志和 SQLite 同时有数据。
- 写库失败不影响业务。

### 第三步：查询服务和 API

实现：

- `core/logs/query_service.py`
- `api/logs.py`
- 修改 `dependencies.py`
- 修改 `api/app.py`
- `test_logs_api.py`

验收：

- 可按 `trace_id` 查询。
- 返回结构化日志和纯文本日志。

### 第四步：客户端接入

实现：

- `packages/shared/ts/logs.ts`
- `apps/desktop/src/services/logs.ts`
- 修改 `LogsPage.tsx`

验收：

- 日志页默认调用 `/logs/recent` 展示最近日志。
- 日志页可以输入 `trace_id` 调用 `/logs/query` 查询。
- 查询结果可读。
- 错误态、空态、加载态完整。

## 验收标准

必须全部满足：

- 业务代码不需要显式传 `trace_id`。
- 任意带 `x-trace-id` 的请求触发的后端日志都能在 SQLite 中按同一 `trace_id` 查到。
- SQLite 日志使用独立 `storage/logs.sqlite3`，不与主业务库共用数据库文件。
- 文件日志仍然存在，且不依赖 SQLite 成功。
- SQLite 写入失败不影响 HTTP 请求、Agent Runtime、工具执行。
- `/logs/query` 能返回结构化日志和纯文本日志。
- `/logs/recent` 能返回最近日志和纯文本日志。
- `/logs/query` 默认按 `ts ASC` 返回。
- `/logs/recent` 默认按 `ts DESC` 返回。
- 响应结构包含稳定 `event_name`、可读 `message`、错误字段和 `stack`。
- JSONL 文件日志与 SQLite 日志字段来源于同一 mapper，核心字段不漂移。
- 全量后端测试通过。
- 客户端接入后桌面端类型检查和 Vitest 通过。
- 审查 Agent 和测试 Agent 验收通过。

## 风险与取舍

### 风险：SQLite 写入拖慢请求

处理：

- 使用队列和批量写入。
- 业务线程不直接写 SQLite。
- 批量大小保持克制，避免为了吞吐牺牲桌面端实时反馈。

### 风险：日志队列积压

处理：

- 队列有上限。
- 队列满时按级别丢弃低价值日志。
- 记录限频 warning。

### 风险：日志格式双源漂移

处理：

- `JsonlLogLine` 和 `LogEntryRecord` 字段要保持一致。
- 允许通过共享 mapper 转换，避免两处手写字段列表。

### 风险：日志查询接口变成万能搜索

处理：

- 第一版强制 `trace_id` 查询。
- 不做全文检索。
- limit 有上限。

## 推荐结论

采用生产级本地日志架构：

- 文件日志作为兜底事实。
- SQLite 作为 UI 查询事实。
- Python logging 标准 handler 作为接入点。
- 有界队列 + 适度 batch writer 作为本地生产级落库机制。
- `/logs/query` 和 `/logs/recent` 作为客户端查询入口。

这条路线既不改变业务 logger 行为，也不会把日志查询建立在扫文件之上，符合当前本地桌面 coding-agent 的长期维护需求。

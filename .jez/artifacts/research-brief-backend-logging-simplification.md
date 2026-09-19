# 后端日志系统简化调研

**范围**：focused；只研究“后端仅写固定格式日志文件”的可行性与迁移边界。

**日期**：2026-09-19

## 结论

建议把后端日志收敛为“Python 标准 logging + 一个固定 JSONL 文件 handler + 一个用于工具子进程回传的 QueueHandler/QueueListener”。保留 `trace_id`、事件名、消息、受控结构化数据和错误摘要；移除日志 SQLite、日志查询 API、日志 CRUD/ORM、批量落库线程，以及为日志查询服务的配置项。

不建议让工具子进程各自打开同一个日志文件。Python 官方文档明确指出，单进程内多线程写同一文件可行，但多进程直接写同一文件不受标准 logging 支持；当前父进程集中接收子进程日志的队列桥接仍有本地桌面场景的实际价值。

## 当前实现事实

| 能力 | 当前实现 | 复杂度/判断 |
|---|---|---|
| 业务调用入口 | 全后端大量模块使用 `app.config.logging.logger.log` | 应保持兼容，避免一次性改几十个调用点 |
| 文件写入 | 自定义 `DateSizeRotatingFileHandler`，按日期和大小轮转 | 可用标准 `RotatingFileHandler` 或 `TimedRotatingFileHandler` 替代；若必须保留当前文件名，再保留这个窄 handler |
| 文件格式 | `JsonlFormatter` + `MappedLogRecord`，当前实际输出 10 个键：`ts/level/logger/trace_id/caller/event/msg/data/error/truncated` | 固定 JSONL 是合理方向，但映射链可以压缩为一个 formatter/serializer |
| 上下文 | `ContextVar` 只保存 trace | 保留 `trace_id` 自动回填；run/task 直接放 `data` |
| 跨进程 | `multiprocessing.Queue` + 自定义 `SubprocessQueueHandler` + 父进程 `QueueListener` | 建议保留，但只承担文件汇聚，不再同时服务 SQLite |
| SQLite 日志 | 独立日志数据库、ORM、异步 handler、批量/丢弃策略 | 与“只写文件”目标冲突，应删除 |
| 查询接口 | 旧日志查询 API 及 service/schema/model | 删除；查看日志改为打开日志目录或由外部工具读取 JSONL |
| 启动日志 | Tauri 另外捕获 backend stdout/stderr 到 `backend-console-*.log` | 若要求“严格只有一个后端日志文件”，应将 uvicorn 日志接入同一文件，或把 console 文件降为仅启动失败兜底 |

## 推荐的固定格式

建议继续使用 JSON Lines，每条记录严格输出同一组键，字段缺失时写空值，不动态增删顶层字段：

```json
{"ts":"2026-09-19T10:20:30.123Z","level":"INFO","logger":"coding_agent.backend","trace_id":"...","caller":"app.service.foo:run:42","event":"run_started","msg":"运行开始","data":{"task_id":"..."},"error":null}
```

当前实现顶层固定为 10 个键：

`ts`、`level`、`logger`、`trace_id`、`caller`、`event`、`msg`、`data`、`error`、`truncated`。

`data` 只允许经过截断/脱敏的摘要字段；`error` 只包含 `type`、`message`、`stack`。如果确实需要保留截断信息，保留 `truncated` 作为第 10 个固定键也可以，但必须修正文档中“9 字段”和实现实际 10 字段不一致的问题。不要再根据某条日志临时增加顶层字段。

## 建议的后端结构

```text
FastAPI 后端进程
├─ coding_agent.backend logger
│  └─ 固定 JSONL formatter
│     └─ 一个本地文件 handler（带有限轮转）
├─ HTTP middleware
│  └─ 生成/绑定 trace_id
└─ 工具执行子进程
   └─ QueueHandler → 父进程 QueueListener → 同一个文件 handler
```

行为边界：

- 行为归属后端进程；工具子进程只发送日志记录，不拥有日志文件。
- 数据只写本机应用数据目录下的日志文件，不写日志 SQLite，不新增网络或远程观测服务。
- Tauri 仍负责启动/停止/重启后端；后端正常关闭时停止 QueueListener、flush、关闭文件 handler。
- 后端崩溃时，文件中已有记录保留；未 flush 的少量队列记录可能丢失，但不应阻断业务。启动恢复、Run 收敛和 bootstate 仍是业务生命周期逻辑，不应改由日志系统承担。
- 日志写入失败只能降级到 stderr/桌面 console，不能反向影响 Agent Run。

## 可选方案比较

| 方案 | 变更量 | 优点 | 主要问题 | 建议 |
|---|---:|---|---|---|
| A：保留现有全部能力，仅关闭 SQLite | 小 | 风险最低 | SQLite 模型、查询 API、队列参数和异步线程仍然存在，系统并未真正简化 | 仅适合临时止血 |
| B：保留标准 logging、JSONL、父子进程队列，删除 SQLite 全链路 | 中 | 满足目标；兼容现有调用；保留 trace 和子进程可排查性 | 仍有少量 formatter/context/bridge 代码 | **推荐** |
| C：每个进程直接写自己的文件 | 中 | 实现最简单 | 多进程同文件不安全；日志分散；工具日志无法自然合并 | 不推荐 |
| D：引入 structlog/loguru | 中/大 | API 更现代 | 新增依赖与迁移面；不能自动解决进程汇聚、轮转和生命周期 | 不推荐，当前没有必要 |

## 建议删除或收敛的代码面

删除 SQLite 日志链路：

- `apps/backend/app/config/logging/handler/sqlite_handler.py`
- `apps/backend/app/storage/model/log_model.py`
- `apps/backend/app/storage/crud/log_crud.py`
- `apps/backend/app/service/log_query_service.py`
- `apps/backend/app/api/logs_api.py`
- 日志查询 request/response schema、`LogQuery`、`LogQueryResult`、`LogEntryRecord`
- `init_storage()` 中的日志数据库 engine/schema 初始化
- `Settings` 中的 `LOG_DATABASE_FILE`、`SQLITE_LOGGING_ENABLED`、`LOG_QUEUE_SIZE`、`LOG_BATCH_SIZE`、`LOG_FLUSH_INTERVAL_MS`、`LOG_QUERY_LIMIT_MAX`

保留的日志基础设施：

- `MappedLogRecord`：保留脱敏/截断规则，改为 formatter 内部的小型纯函数或单一模块。
- `log_context_store`：只保留 `ContextVar[str]` 的 trace_id 绑定/恢复；不维护旧上下文包装类或 run/task 反查表。
- `process_bridge`：保留 QueueHandler/QueueListener，但删除 SQLite 相关说明和自定义字段映射。
- `date_size_rotating`：若接受标准文件名，替换为标准库 handler；若桌面 UI/文档依赖 `backend-YYYY-MM-DD.log`，暂时保留并补齐测试。

## 推荐迁移顺序

1. 先冻结并测试固定 JSONL schema，明确不记录 API key、完整 prompt、完整工具参数、文件正文和模型大段输出。
2. 将配置入口改为只接收日志目录、日志级别、单文件上限和保留数量；默认写入 Tauri 注入的 runtime/data 目录。
3. 把 `configure_logging()` 改为只创建一个文件 handler；保留父进程 QueueListener，并让子进程使用标准 QueueHandler。
4. 在应用启动与 lifespan 两条路径验证幂等安装、正常关闭 flush，以及 `reload`/spawn 下不会重复 handler。
5. 删除 SQLite 日志初始化、handler、查询 API 和相关 schema/model/service，再清理依赖注入与导出。
6. 统一 uvicorn/backend console 的策略：如果产品要求单文件，给 `uvicorn.error`、`uvicorn.access` 配置同一文件 handler，并将 Tauri console 文件改为兜底；否则在文档中明确它是启动/崩溃旁路日志。
7. 更新 `AGENTS.md`、启动文档、OpenAPI 和桌面“打开日志目录”入口，补充后端进程日志路径与轮转规则。

## 验收标准

- 启动后只创建预期的后端日志文件，不创建日志 SQLite 数据库。
- 每行都能被 JSON 解析，顶层键集合完全一致，时间为 UTC RFC3339 毫秒格式。
- 同一 HTTP 请求的日志共享 `trace_id`；工具子进程日志能回到父进程文件。
- 连续启动/测试/重载不会重复写同一条日志。
- 文件写入失败不会让请求或 Agent Run 失败。
- 关闭时 QueueListener 和文件 handler 可释放；崩溃后不会无限重启。
- 敏感信息扫描通过，尤其是 provider key、prompt、tool args、文件内容和模型正文。

## 官方依据

- Python logging handlers：`QueueHandler`/`QueueListener` 用于把日志处理移到单独线程，适合避免业务线程执行慢 handler：[Python logging.handlers](https://docs.python.org/3.11/library/logging.handlers.html)。
- Python Logging Cookbook：`contextvars` 适合在 threading/asyncio 中携带请求上下文；多进程直接写同一文件不受标准 logging 支持，应集中到一个进程处理：[Logging Cookbook](https://docs.python.org/3.14/howto/logging-cookbook.html)。
- Uvicorn 支持通过 `log_config`/`--log-config` 统一配置其 logger；这可用于决定 backend console 是否并入固定文件：[Uvicorn Settings](https://www.uvicorn.org/settings/)。

## 未决问题

- “固定格式”是否必须继续是 JSONL，还是希望人类优先的文本格式；本调研推荐 JSONL，因为桌面和外部诊断工具都能直接消费。
- 是否保留现有 `backend-console.log`；它属于 Tauri 捕获的 stdout/stderr，不完全等价于后端结构化日志。
- 是否必须保持 `backend-YYYY-MM-DD.log` 文件命名；如果不是，优先使用标准 `RotatingFileHandler`，进一步减少自研代码。

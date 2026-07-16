# 日志模块重构计划（依据 `rules/Agent日志开发规范.md`）

> 本文档是 `apps/backend/app/config/logging/` 日志模块对齐《通用日志开发规范》的改造计划。
> 规范原文见 `rules/Agent日志开发规范.md`，本计划只描述**改造动作**，不重复规范正文。
>
> 状态：调研完成，决策已确认，待实施。

---

## 一、决策基线（已与用户确认）

| # | 决策点 | 结论 |
|---|--------|------|
| D1 | 共享契约 `LogEntry` 变更 | **接受 breaking change**，同步前端（`packages/shared/ts/logs.ts` 改为 9 字段） |
| D2 | `caller` 字段 | **接受**，精确到 `模块路径:类.方法:行号`（见 §六 实现方案） |
| D3 | 历史日志 SQLite | **不迁移**；旧库重建，旧 `logs-*.log` 文件保留归档、不被新解析器强读 |
| D4 | 查询 API 入参 | **只保留 `trace_id`**；移除 `task_id`/`run_id` 入参，不做"经 trace 反查" |

---

## 二、目标 Schema（固定 9 字段）

落盘 JSONL 与 SQLite 统一为以下 9 字段，字段名、顺序、类型与规范第三章一致：

```json
{"ts":"2026-07-16T08:04:08.518Z","level":"INFO","trace_id":"2a543a9d1f8c4b2e",
 "logger":"coding_agent.backend",
 "caller":"app.core.runs.store:DurableRunStore.save:101",
 "event":"run_saved",
 "msg":"运行记录已持久化","data":{"run_id":"r-1","status":"ok"},"error":null}
```

| 字段 | 来源 | 约束 |
|------|------|------|
| `ts` | 自动 | UTC 毫秒 `...Z` |
| `level` | 自动 | DEBUG/INFO/WARNING/ERROR/CRITICAL |
| `trace_id` | 自动（context filter 回填） | **唯一链路键**；禁止传空串屏蔽 |
| `logger` | 自动 | 固定 `coding_agent.backend` |
| `caller` | 自动 | `模块路径:类.方法:行号` |
| `event` | 手动（位置参数） | 稳定英文 snake_case，禁 f-string 拼动态 |
| `msg` | 手动（extra） | 中文一句话 |
| `data` | 手动（extra） | JSON；业务字段含实体 ID 值（如 `data.task_id`）仅供检索 |
| `error` | 半自动 | 仅出错非空 `{type,message,stack}`，正常 `null` |

`data` 自动脱敏（键名命中 `api_key/token/secret/password/...`）+ 超长截断（标记 `[TRUNCATED:N]`，不新增顶层字段）。

---

## 三、当前实现与差距（要点）

- 日志模块实际位于 `apps/backend/app/config/logging/`（AGENTS.md 预测的 `app/logging/` 不符，落实后建议补一行项目附录说明真实路径）。
- 统一 logger 名 `coding_agent.backend`：✅ 已满足。
- `LogContext` 带 6 个链路键 `trace_id/run_id/task_id/span_id/tool_call_id/approval_id`：`log_context.py:32-37`，且经 `LogContextFilter` 全注入 LogRecord（`:89-91`）。❌ 违反"只保留 trace_id"。
- 记录模型 `MappedLogRecord`/`JsonlLogLine`/`LogEntryRecord` 均带 `run_id/task_id/span_id/event_id/step_id/tool_call_id/approval_id`：`record_mapper.py:55-62`、`jsonl.py:53-64`、`log_records.py:50-57`。❌
- `event` 从 `record.msg` 首词提取，脆弱：`record_mapper.py:140-160`。
- `msg` 不被支持：传 `extra={"msg":...}` 会落入 `attributes`（`record_mapper.py:179-197` ignored 集合不含 `msg`）。❌
- `error` 平铺三字段，非嵌套。❌
- `caller` 缺失。❌
- SQLite 表含全部链路键列：`log_store.py:84-87, 176-182`。❌
- 调用面：22 文件、130+ 处 `logger.*`，普遍把 `task_id/run_id` 当独立链路键传。❌
- 前端契约 `packages/shared/ts/logs.ts:11-48` 暴露全部链路键字段（breaking change）。
- 脱敏/截断 `redact_value`：`core/trace/redaction.py` ✅ 基本满足，复用即可。
- trace 主干 `TraceStore` 已存在（按 `trace_id/run_id/task_id/span_id/payload` 记录），为实体 ID 承载方；但反向索引脆弱（`query_service.py:183` 用 `events[0]["trace_id"]`，不在本计划修复范围，D4 已豁免日志侧反查）。

---

## 四、改造范围（文件清单）

**后端核心（必改）**
- `app/config/logging/record_mapper.py` — 重构为 9 字段映射，识别 `extra["msg"]`/`extra["data"]`，聚合 `error`，计算 `caller`。
- `app/config/logging/save/jsonl.py` — `JsonlFormatter` 输出 9 字段；`JsonlLogLine` 收敛；`query_log_files`/`_matches` 去掉 `run_id` 过滤、容忍旧格式行。
- `app/config/logging/save/sqlite_handler.py` — `entry_from_log_record` 映射到新 9 字段。
- `app/config/logging/log_context.py` — `LogContext` 收敛为只承载 `trace_id`；保留 task/run→trace 进程内反查表（仅供入口层绑定，不再写日志）。
- `app/config/logging/configuration.py` — handler 装配不变，仅依赖新 mapper/formatter。
- `app/config/logging/__init__.py` — 重导出调整（移除 `event_id`/`step_id` 等概念，新增 `caller` 相关导出如有）。
- `app/storage/log_records.py` — `LogEntryRecord` 收敛为 9 字段；`LogQuery` 去掉 `task_id`/`run_id`。
- `app/storage/log_store.py` — schema 升 v2，去链路键列、加 `data_json`/`error_json`/`caller`；旧库重建（D3）。
- `app/core/logs/query_service.py` — `query_by_trace` 仅按 `trace_id`；移除 `task_id`/`run_id` 入参。
- `app/api/logs_api.py` — 只保留 `trace_id` 查询入参（D4）。
- `app/config/logging/text_renderer_service.py` — 渲染 9 字段。
- `app/api/middleware/api_logging.py` — 入口只 bind `trace_id`；路径实体 ID 写入该日志 `data` 并交 `TraceRecorder`。

**调用点迁移（去独立链路键 + 补 `msg`）**
- `app/core/runs/store.py`、`recovery.py`、`resume.py`
- `app/domain/approvals/service.py`、`human_input/service.py`
- `app/core/runtime/runner.py`、`operations.py`
- `app/tools/executor.py`、`execute.py`、`execution/service.py`、`runtime/{platform,concurrency,compatibility}.py`
- `app/core/replay/projector.py`、`app/core/workflows/react_like.py`
- `app/storage/sqlite.py`、`app/api/dependencies.py`
- 其余 22 文件 130+ 处：移除 `task_id/run_id/tool_call_id/approval_id` 独立链路键；实体 ID 值移入 `data` 或 `msg`；补齐中文 `msg`；异常点统一 `.exception()`。

**前端契约（D1）**
- `packages/shared/ts/logs.ts` — `LogEntry` 改为 9 字段；`LogQueryRequest` 仅留 `trace_id`。
- `apps/desktop/` — 确认日志展示消费路径（grep 当前 0 匹配，需人工确认是否有其他入口），同步渲染字段。

**测试**
- `tests/test_jsonl_formatter.py`、`test_logging_configuration.py`、`test_logging_context.py`、`test_sqlite_log_handler.py`、`test_log_store.py`、`test_log_query_service.py`、`test_logs_api.py` 等。

---

## 五、分阶段实施步骤

### Phase 1 — 基础层（不动业务调用点）
1. `record_mapper.py`：新增 `map_log_record` 产出 9 字段。
   - `event` = `record.msg`（已是稳定 snake_case 位置参数）。
   - `msg` = `extra["msg"]`（新增支持）；缺失时退化为 `event`（兼容期，Phase 2 消除）。
   - `data` = 原 `attributes`（改名），= 非保留 extra 字段，经 `redact_value` 脱敏+截断。
   - `error` = 出错时 `{type,message,stack}`；`type/message` 来自 `exc_info` 或 `extra["error"]`，`stack` 来自 traceback。
   - `caller` = 见 §六。
   - `trace_id` = context filter 回填；保留 `to_extra()` 仅含 `trace_id`。
2. `jsonl.py`：`JsonlFormatter.format` 输出 9 字段；`JsonlLogLine` 收敛；`query_log_files`/`_matches` 移除 `run_id` 过滤，`_parse_line` 对缺失字段默认（容忍旧格式行，不抛）。
3. `sqlite_handler.py`：`entry_from_log_record` 映射新 9 字段。
4. `log_store.py`：`SCHEMA_VERSION` 1→2；`_create_schema` 新表（列：`ts,level,trace_id,logger,caller,event,msg,data_json,error_json,truncated`）；`_migrate_schema` 因 D3 不迁移，直接 `DROP+CREATE` 或重建库文件；`insert_many`/`query`/`_entry_values`/`_entry_from_row` 同步。
5. `log_records.py`：`LogEntryRecord` 收敛 9 字段；`LogQuery` 去 `task_id`/`run_id`。
6. `text_renderer_service.py`：渲染 9 字段。
7. 兼容：旧调用传的 `task_id/run_id` 因不再是保留键，自动落入 `data`（不报错，Phase 2 清理噪音）。

### Phase 2 — 调用点迁移（去独立链路键 + 补 `msg`）
- 全局替换 130+ 处：不再把 `task_id/run_id/tool_call_id/approval_id` 当链路键传。
- 入口层（`api_logging.py`）：只 `merge_log_context(trace_id=...)`；从 path 提取的实体 ID 写入该条日志 `data`（`data={"task_id":...,"run_id":...}`）并交 `TraceRecorder` 落 trace。
- 业务/存储/工具层：只依赖 `trace_id` 自动回填；需要本地可见的实体 ID 值放 `data` 或 `msg`。
- 为关键日志补齐中文 `msg`（消除 Phase 1 的退化）。
- 异常点统一 `.exception()` 带 `msg`+`data`（含实体 ID 值），底层 `raise` 重抛。

### Phase 3 — 查询与 API 收敛（D4）
- `logs_api.py`：仅保留 `trace_id` 入参；移除 `task_id`/`run_id` `Query` 参数。
- `query_service.py`：`query_by_trace` 仅按 `trace_id`；移除 `task_id`/`run_id` 形参。
- `LogQuery`/`LogQueryRequest`：移除 `task_id`/`run_id`。

### Phase 4 — 前端契约同步（D1）
- `packages/shared/ts/logs.ts`：`LogEntry` 改 9 字段（`ts,level,trace_id,logger,caller,event,msg,data,error`）；`LogQueryRequest` 仅 `trace_id`。
- `apps/desktop/`：确认并同步日志展示组件字段（如桌面端有日志面板）。

### Phase 5 — 测试与验收
- 更新全部相关测试，覆盖：9 字段输出、脱敏、截断、`error` 嵌套、`caller` 格式、`trace_id` 自动回填、旧格式行容忍、schema v2 重建、API 仅 `trace_id`。
- 对照规范第八章自检清单（§八）逐条核对交付。

---

## 六、`caller` 实现方案（D2）

目标格式：`模块路径:类.方法:行号`，例如 `app.core.runs.store:DurableRunStore.save:101`。

实现（在 `record_mapper` 或新增 `CallerFilter` 中，handler 内单次计算）：
- `模块路径`：由 `record.pathname` 推导为相对项目根的 dotted 路径（如 `app/core/runs/store.py` → `app.core.runs.store`）；缓存避免重复计算。
- `方法`：`record.funcName`。
- `类`：best-effort 通过检查调用帧 `f_locals` 中的 `self`/`cls` 取得 `type(self).__name__` / `cls.__name__`；取不到则仅用 `funcName`。
- `行号`：`record.lineno`。

性能权衡：每次日志记录需一次栈帧探查。缓解：仅在 formatter 内执行；日志量级中等，可接受；`try/except` 包裹，失败回退 `模块:方法:行号`。若后续出现热点，可降级为不含类名的 `模块:方法:行号`。

---

## 七、历史日志处理（D3）

- SQLite 日志库：schema 升 v2；`initialize()` 检测到旧版本（user_version<2）直接重建表（DROP+CREATE），旧数据不保留（D3 明确不迁移）。
- 旧 `logs-YYYY-MM-DD.log` 文件：保留在 `logs/` 目录作归档，新 `query_log_files` 对缺字段的旧行做默认值容错（不抛、不匹配新字段即跳过），不强制重读。
- 启动阶段在 `bootstate` 或日志配置处记录 schema 重建事件（写一条 `info` 日志，注明旧库已重建）。

---

## 八、验收对照（规范第八章自检清单）

- [ ] 统一 logger（`coding_agent.backend`），无 `print` 当系统日志。
- [ ] `event` 稳定 snake_case、可聚合（非中文、非 f-string 拼动态）。
- [ ] `data` 承载业务字段，不拼进 `msg`。
- [ ] 中文 `msg`，可混英文专业词/函数名/实体 ID 值。
- [ ] 捕获异常用 `.exception()` 带上下文；底层 `raise` 重抛。
- [ ] 分层职责正确（底层记现场、上层记链路）。
- [ ] 无空 catch、无吞异常。
- [ ] 不输出 secret/敏感信息（`redact_value` 覆盖）。
- [ ] 日志 extra 仅 `trace_id`（自动回填）+ `msg` + `data`，无 `task_id`/`run_id`/`tool_call_id` 等独立链路键。
- [ ] 入口层才绑定 `trace_id`，下层继承；无空串屏蔽。
- [ ] 落盘 9 字段含 `caller`；`error` 为嵌套 `{type,message,stack}`。
- [ ] API 仅 `trace_id` 查询（D4）。

---

## 九、风险与回滚

- **前端契约 breaking change（D1）**：`LogEntry` 字段变更，需桌面端同步；实施 Phase 4 前先改 shared 类型并通知前端，避免联调错位。
- **调用点遗漏**：130+ 处易漏 `msg`/误留链路键；用 Phase 5 测试 + 一次全仓 grep `extra={` 复查守护。
- **`caller` 性能**：见 §六 缓解；必要时降级不含类名。
- **旧日志不可读**：D3 已接受；归档文件不影响新解析。
- **回滚**：Phase 1-3 可借 git 分支隔离；SQLite schema 升版不可逆，回滚需删除新库文件恢复旧代码（D3 已豁免历史数据）。

---

## 十、建议提交顺序（最小可用优先）

1. Phase 1 基础层 + Phase 5 基础层测试 → 保证 9 字段落盘正确。
2. Phase 3 + Phase 4（API/前端契约收敛）→ 先定契约，避免后续返工。
3. Phase 2 调用点迁移（可分批，按模块 PR）→ 每批配测试。
4. 全量自检（§八）+ 旧格式容忍验证。

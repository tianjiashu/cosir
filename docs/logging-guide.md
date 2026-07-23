# 本项目日志打印实操指南

> 本文是**项目级实操指南**：讲清楚在本仓库前后端代码里「如何正确地打日志」。
> 通用规范（字段纪律、可读性、分层职责、自检清单）见 `rules/Agent日志开发规范.md`，
> 历史重构计划见 `docs/logging-refactor-plan.md`。本文档优先给出可直接复制的写法。

---

## 一、架构速览（心里有数再打）

日志能力集中在 `apps/backend/app/config/logging/`，由 `configuration.py` 在进程启动时装配：

- **统一 logger 名**：`coding_agent.backend`（全项目只有一个，禁止自建多个 logger）。
- **落盘**：`FileHandler` → JSONL 文件，路径由 `log_files_dir_service.current_log_file()` 生成（`logs/logs-YYYY-MM-DD.log`）。
- **可选副本**：`SQLiteLogHandler` → SQLite 库（受 `settings.sqlite_logging_enabled` 开关控制，失败自动降级为仅文件）。
- **自动字段**：
  - `trace_id`：由 `LogContextFilter` 自动回填（唯一链路键，入口层 `bind_log_context` 绑定）。
  - `caller`：由 `CallerFilter` 自动回填，格式 `模块:类.方法:行号`。
  - `ts` / `level` / `logger`：标准 logging 自动填。
- **序列化**：`JsonlFormatter` 把 `LogRecord` 映射为固定 **9 字段**后 `json.dumps(..., ensure_ascii=False, sort_keys=True)`。

> 子进程日志走 `process_bridge` 的跨进程队列回主进程落盘，写法与父进程**完全一致**。

---

## 二、第一步：拿到 logger

**推荐：直接复用包里已导出的 `log`**（定义在 `app/config/logging/logger.py:56`），全项目同一个 logger 实例：

```python
from app.config.logging.logger import log

log.info("task_created", extra={
    "msg": "新任务已创建，等待调度",
    "data": {"agent_id": agent_id, "status": "pending"},
})
```

> `log = logging.getLogger("coding_agent.backend")` 与统一 logger 名一致，导入即用，
> **不需要**在每个文件里 `getLogger(...)` 自建常量、`logging.getLogger` 重新取，也不要自建多个 logger 名。

**例外：被注入 logger 的组件**（业务服务类通过构造注入 `self._logger` 时），仍用注入实例：

```python
class TaskService:
    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger
```

---

## 三、标准写法（核心）

调用约定：**第一个位置参数 = 稳定英文 event 名（snake_case）；中文描述放 `extra["msg"]`；业务字段放 `extra["data"]`**。

```python
# 普通 INFO：状态变更 / 入口 / 关键步骤
_LOGGER.info("task_created", extra={
    "msg": "新任务已创建，等待调度",
    "data": {"agent_id": agent_id, "status": "pending"},
})
```

```python
# WARNING：可预期的降级 / 超时 / 校验失败
_LOGGER.warning("tool_call_timeout", extra={
    "msg": f"调用 read_file 工具超时，tool_call_id={tool_call_id}，已触发熔断",
    "data": {"tool": "read_file", "duration_ms": 30500, "threshold_ms": 30000},
})
```

```python
# ERROR：捕获异常必须 .exception()，不要 .error()；记完重新抛出
except Exception:
    _LOGGER.exception("db_write_failed", extra={
        "msg": f"更新任务状态写入数据库失败，task_id={task_id}",
        "data": {"operation": "update_status"},
    })
    raise  # 让上层也能补上下文
```

### 字段是怎么从调用里提取的（`record_mapper.py` 行为）

| 字段 | 来源 | 说明 |
|------|------|------|
| `event` | 第一个位置参数的首个词 | 必须匹配 `^[a-z][a-z0-9_]*$`，否则回退为 `"log_event"`。**不能用中文、不能 f-string 拼动态**。 |
| `msg` | `extra["msg"]` | 经 `install_msg_relocation` 重定位到 `display_message`，自动避开 logging 保留字，不会与 `event` 冲突。两者皆缺时回退到 `event`。 |
| `data` | `extra["data"]` + 其余非保留 extra 字段合并 | 自动经 `redact_value` 脱敏、超长截断（`MAX_LOG_TEXT_LENGTH=2000`）。 |
| `error` | `.exception()` 的 `exc_info` 或跨进程桥接写入的 `error_type/error_message/stack` | 正常为 `null`。**不要**手写在 `extra` 里塞 `error`。 |

---

## 四、链路上下文（trace_id）

- **入口层**（API 路由 / 业务入口）绑定，并在 `finally` 重置：

```python
from app.config.logging import bind_log_context, reset_log_context

bind_log_context(trace_id=ctx.trace_id)
try:
    ...
finally:
    reset_log_context()
```

- 下层模块（存储 / 工具 / 外部调用）**继承**上下文，只靠 formatter 自动回填 `trace_id`，**不**自行绑定，`trace_id` 也**不要**在 `extra` 里传（空串会屏蔽自动回填）。

---

## 五、必须避免（会破坏聚合 / 序列化 / 排查）

```python
# ❌ f-string 拼 event：不可聚合
_LOGGER.error(f"写任务 {task_id} 失败: {exc}")
# ❌ print 当系统日志
print("something failed")
# ❌ 空 catch
except Exception:
    pass
# ❌ 把中文塞进 event（event 必须稳定英文）
_LOGGER.info("任务创建成功", extra={...})
# ❌ 顶层塞 task_id / run_id / tool_call_id 等独立关联键（只认 trace_id，值放 data/msg）
_LOGGER.info("tool_call", extra={"tool_call_id": cid, ...})
# ❌ data 里放 set / 不可 JSON 序列化的对象
_LOGGER.info("step_done", extra={"data": {"tags": {"a", "b"}}})  # ← 会 json.dumps 抛 TypeError
```

> **重要约束**：`data` 最终走 `json.dumps` 序列化。`set`、`datetime`（未转字符串）、函数、`Path` 等不可序列化对象都会直接抛 `TypeError`，使整条日志失败。**`data` 里只放标量 / list / dict（其内仍是可序列化类型）**；`set` 需先 `list(...)` 或只取标量。

---

## 六、分层职责（谁该记什么）

| 层 | 职责 |
|----|------|
| 接入层（API） | 不记业务细节；预期异常交给全局处理器，端点 `except` 不手写 logger。 |
| 运行时 / 业务层 | **主力层**：状态变更 `info`、关键路径（恢复/审批）`info`、意外异常 `.exception()` 并 `raise`。 |
| 存储层 | 记数据操作现场，写失败 `.exception()` 并 `raise`；不绑上下文、不塞实体 ID 顶层键。 |
| 工具 / 外部调用 | 入口/出口 `info`（名称、参数摘要、耗时、状态）；失败 `.exception()` 带中文 `msg`。 |
| 启动 / 关闭 | 启动记配置加载结果（不输出 secret 原文）；关闭资源释放失败记 `error`。 |
| 子进程 | 同在线服务标准，写法不变，经队列回主进程落盘。 |

---

## 七、产出物与排查

- 文件日志：`logs/logs-YYYY-MM-DD.log`（JSONL，每行一个对象）。
- 可选 SQLite 副本（受 `sqlite_logging_enabled` 控制）。
- 查询：`JsonlFormatter.query_log_file` / `query_log_files`（支持 `trace_id` / `level` / 时间窗过滤）。
- 前端查看：日志查看 UI 见 `docs/log-viewer-ui.md`。

---

## 八、自检清单（交付前）

```
□ 用统一 logger（注入 self._logger 或模块级 _LOGGER），非自建 / print？
□ event 稳定 snake_case、可聚合（非中文、非 f-string 拼动态）？
□ 业务字段进 data，非拼进 msg？
□ 带中文 msg，可混英文专业词 / 函数名 / 实体 ID 值？
□ 捕获异常用 .exception() 并带上下文？底层 raise 重抛？
□ 正确层级记日志（底层记现场、上层记链路）？
□ 避免空 catch、不吞异常？
□ data 无 secret、且全部可 JSON 序列化（无 set / 不可序列化对象）？
□ extra 只带 trace_id（自动回填）+ msg + data，未塞 task_id / run_id 等独立键？
```

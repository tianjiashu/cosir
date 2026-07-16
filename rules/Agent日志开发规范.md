# 日志开发规范（后端派生附录）

> 本规范是 `Agent代码开发规范.md` 第六节「可排查日志」在项目后端的具体落地。
> 通用原则（必须记录 / 禁止记录 / 实现要求）以主规范为准，此处只规定**为什么打、怎么打、什么格式、在哪里打、写给谁看**。

---

## 一、目的：我们为什么打日志

打日志的目的不是「记录发生了什么」这种口号，而是**「出事时能定位」**。落到本项目的架构现实（本地桌面 Agent、SQLite + JSONL 双写、全链路 trace），核心目的有五个，按重要性排序：

### 1. 故障复盘 —— 哪里错了、为什么错、影响了谁
这是首要目的。一个 coding-agent 跑任务时，模型流中断、工具执行失败、审批卡住、checkpoint 损坏，都可能发生。没有日志，你只能看到「任务失败了」，但看不到根因。

日志必须能回答：
- **什么时间**（`ts`）
- **哪个任务 / run**（`task_id` / `run_id` / `trace_id`）
- **哪一层、哪一个事件**（`event_name` + `logger_name`）
- **什么错误、什么堆栈**（`error_type` / `error_message` / `stack`）

这也是为什么 `.exception()` 必须带堆栈、底层 DB 失败不能静默——否则你连「是 SQLite 写崩了还是模型断了」都分不清。

### 2. 全链路串联 —— 一次用户操作从头跟到尾
本项目用 `trace_id` 贯穿 API → Runtime → 工具 → 存储（`LogContextFilter` 自动回填）。目的是：给你一个 ID，就能从前端一次点击，一路追到后端哪一步、哪个工具、哪条 SQL 出了问题。

如果存储层不记日志、或记了却没带 `trace_id`，这条链就断了——这正是 `sqlite.py` 当前静默的问题：**不是不能跑，是出了事你追不下去**。

### 3. 行为可观测 —— 任务到底走到哪了
Agent 是长时间运行的异步流程（`run_task` 是 `AsyncIterator`）。用户 / 开发者需要知道：任务创建了没、卡在审批还是模型、run 恢复了没、checkpoint 存了没。

`runner.py` 里 `task_created` / `task_cancelled` / `runtime_event` 这些 `info` 日志，目的就是**把隐形的状态机变成可见的时间线**，而不是只等最终成功或失败。

### 4. 审计与可解释 —— Agent 做了什么决策
工具调用、审批同意 / 拒绝、人工干预，都是「Agent 自主行为」，需要留痕。这不是调试用的，是**信任问题**：用户要知道 Agent 替他执行了什么副作用操作。所以 `approval_*`、`tool_call_*` 这类事件必须落盘，且不能丢上下文。

### 5. 不阻断主流程的「旁路证据」
日志是**旁路**——它记录系统，但不参与系统决策。目的是即使主流程崩了，证据还在（本项目用队列桥 `process_bridge` + SQLite 双写，就是防止日志本身拖垮业务）。这也反向约束了日志写法：不能抛异常、不能阻塞、不能输出 secret。

### 什么**不是**日志的目的
- **不是给用户看的 UI 文案** —— 那是事件流（`RuntimeEvent`）和前端的事，日志是给开发 / 运维排查的。
- **不是业务数据流** —— 别把日志当消息总线传递状态。
- **不是装饰** —— `print("ok")`、空 `catch` 里的日志，没有上述目的，就是噪音。
- **不是替代断点调试** —— 运行时临时 debug 可以用，但落盘日志要为「出事后再看」服务，所以 `event_name` 要稳定、可聚合。

---

## 二、日志门面与 Logger 来源

后端统一 logger 名：`coding_agent.backend`。**不要**在业务代码里 `logging.getLogger()` 自建 logger（除模块级常量场景，见下）。

按模块角色分两种拿法：

| 模块角色 | 拿 logger 的方式 | 示例 |
|---------|----------------|------|
| 被注入 logger 的组件 | 用构造时注入的 `self._logger` | `runner.py` 的 `AgentRuntime` |
| 无注入的底层模块 | 文件顶部定义模块级常量 `_LOGGER = logging.getLogger("coding_agent.backend")` | `sqlite.py` 的 `SQLiteTaskStore` |

```python
# ✅ 模块级常量（每个文件只定义一次）
_LOGGER = logging.getLogger("coding_agent.backend")
```

---

## 三、日志格式（JSONL 固定 Schema）

落盘格式为**单行 JSON**（JSONL），文件位于 `logs/logs-YYYY-MM-DD.log`。由 `JsonlFormatter` 序列化，字段固定：

```json
{"ts":"2026-07-16T08:04:08.518Z","level":"INFO","logger_name":"coding_agent.backend",
 "event_name":"task_created","message":"task_created",
 "trace_id":"2a543a9d...","run_id":"","task_id":"5311de5d-...","span_id":"",
 "event_id":"","step_id":"","tool_call_id":"","approval_id":"",
 "error_type":"","error_message":"","stack":"",
 "attributes":{"agent_id":"developer","session_id":null,"description":""},"truncated":false}
```

### 字段纪律
1. `event_name` —— **稳定 snake_case 事件名**（如 `task_created`、`task_failed`）。用于聚合查询，**不要**用中文或 f-string 拼动态内容。
2. `message` —— 默认等于 `event_name`。**不要**把可读描述塞进 message，描述放 `extra`（进 `attributes`）。
3. `extra={...}` 里的字段 → 进 `attributes`（除保留名外）。
4. 保留顶层字段：`trace_id` / `run_id` / `task_id` / `span_id` / `event_id` / `step_id` / `tool_call_id` / `approval_id` / `error_type` / `error_message` / `stack`。这些由框架 / 上下文自动填，**不要**在 `extra` 里传空串（空串会屏蔽自动回填）。
5. `ts`（UTC 毫秒 `Z`）、`level`、`logger_name` 自动填。
6. `attributes` 自动脱敏（`redact_value`），超长自动截断并标 `truncated:true`。

### 打印写法
```python
# 普通结构化日志（带中文 description）
_LOGGER.info("task_created", extra={
    "task_id": task.task_id,
    "agent_id": agent_id,
    "description": "新任务已创建，等待运行时调度",
})

# 捕获异常必须带堆栈 —— 用 .exception()，不要 .error()
except Exception as exc:
    _LOGGER.exception("task_failed", extra={
        "task_id": task.task_id,
        "description": "模型流式接口中断，任务执行失败",
    })
    raise  # 存储层 / 底层：记完重新抛出，让上层也能记上下文

# 预期内校验失败 —— 用 .warning()
_LOGGER.warning("task_create_rejected", extra={
    "reason": "empty_input_text",
    "description": "任务创建被拒绝：输入文本为空",
})
```

### 禁止
```python
# ❌ f-string 拼 message：不可聚合、detail 进 message 而非 attributes
_LOGGER.error(f"写任务 {task_id} 失败: {exc}")
# ❌ print 当系统日志
print("something failed")
# ❌ 空 catch
except Exception:
    pass
```

---

## 四、可读性：中文开发者能看懂

日志的最终读者主要是**中文开发者**。因此日志里「给人看的那部分」要用中文写清楚，让人一眼能懂；但**机器聚合用的稳定键、专业术语、函数名、变量名、错误类型可以保留英文**——不必强行全中文翻译。

### 原则
1. **机器键保留英文（不变）**：`event_name`（如 `task_failed`）、`trace_id`、`error_type`、函数名、`tool_call_id` 等——这些是给程序聚合、检索、串联用的，必须稳定、可解析。
2. **给人看的描述写中文**：每条有「业务含义」的日志都应带 `description` 字段（放在 `extra` 里，落进 `attributes.description`），说明**发生了什么、为什么**。
3. **中文里可以混英文**：`description` 里可混用英文专业词 / 函数名 / 变量值（如「调用 openai 流式接口失败，tool_call_id=xxx 超时」），不必全中文。
4. **纯技术现场不翻译**：`.exception()` 自动填的 `error_message` / `stack`（Python traceback 本身是英文）保持不变——开发者读堆栈习惯英文，翻译会丢失精确性，也不该翻译。

### 写法
```python
# ✅ event_name 英文（可聚合），description 中文（可读）
_LOGGER.exception("sqlite_write_failed", extra={
    "task_id": task_id,
    "operation": "update_status",
    "description": f"更新任务状态写入 SQLite 失败，task_id={task_id}",  # 中文 + 变量值
})

# ✅ 中文描述里混英文专业词 / 函数名
_LOGGER.warning("tool_call_timeout", extra={
    "tool_call_id": tool_call_id,
    "description": f"调用 read_file 工具超时，tool_call_id={tool_call_id}，已触发熔断",
})
```

```python
# ❌ 全英文 description，中文开发者需脑内翻译
_LOGGER.exception("sqlite_write_failed", extra={"description": "failed to write task status into sqlite"})

# ❌ 把中文塞进 event_name（破坏聚合）
_LOGGER.info("任务创建成功", extra={...})  # event_name 必须是稳定英文键
```

---

## 五、在哪里打印日志（代码位置与时机）

按分层架构，各层日志职责不同。**核心原则：谁掌握上下文，谁记；底层记现场，上层记链路。**

### 1. API 层（`app/api/`）
- **不记业务细节**。端点只做：捕获预期异常 → 转 `HTTPException`。
- 业务异常的 `detail` 由全局 `HTTPException` 处理器统一记（已实现于 `middleware/logging.py`），端点代码**不重复记**。
- 端点 `except` 块**不要**手写 `logger.*`（已由全局处理器覆盖）；只有意外 500 才在 `except Exception` 里额外 `.exception()`（如 `logs_api.py` 现状，保留即可）。

### 2. 运行时 / 业务层（`app/core/runtime/`、`app/core/*`）
- **这是日志主力层**，负责链路级上下文与状态变更记录。
- 进入任务处理前 `set_log_context(ctx)`，`finally` 里 `reset_log_context(token)`（`runner.py` 模式）。
- 状态变更、事件、失败一律记：
  - 正常状态变更 → `info`（如 `task_created`、`task_cancelled`）。
  - 工作流 / 恢复 / 审批等关键路径 → `info` + `event_name` 稳定名。
  - 捕获意外异常 → `.exception()` 并 `raise`（`runner.py:587` 的 `task_failed`）。
- 运行事件统一走 `_record()`（`runner.py:979`），它已经统一记 `runtime_event` 并带 `trace_log_extra`，**不要**在每处事件散着记。

### 3. 数据 / 存储层（`app/storage/`）
- **记 DB 操作现场**。当前 `sqlite.py` 几乎静默，DB 写入 / 查询失败只抛不记，需补齐。
- 写操作（`create_task` / `update_status` / `append_event` / `create_checkpoint` 等）`except sqlite3.Error` 时 `.exception()` 并 `raise` 重抛。
- **不要**在存储层 `set_log_context` —— 它跑在运行时已绑定上下文的作用域内，`trace_id` / `task_id` 由 `LogContextFilter` 自动回填。只补存储层已知字段（如 `task_id`、`operation`）。
- 可预期校验（如空 `input_text`）由上层转 400，存储层可不记或 `.warning()`。

### 4. 工具执行层（`app/tools/`）
- 工具调用入口 / 出口记 `info`（工具名、参数摘要、耗时、状态）。
- 工具执行失败 → `.exception()` 带 `tool_call_id`、`task_id`。
- 敏感参数（如 API Key）不进 `attributes`（脱敏会自动处理，但也不要主动拼）。

### 5. 启动 / 关闭 / 配置（`app/api/app.py`、`bootstate.py`）
- 启动：记配置加载结果（模型 provider、日志路径等，**不输出 secret 原文**）。
- 关闭：`close()` 失败记 `error`（`runner.py:128` 的 `runtime_close_failed` 模式）。

### 6. 后台任务 / 子进程
- 与 Web / API 同标准：入口、关键步骤、异常、退出都记。
- 子进程日志经 `process_bridge` 队列回父进程落盘，写法不变。

---

## 六、上下文注入规则（全链路串联关键）

- `set_log_context` 仅由**运行时层**在任务 / run 入口调用，并在 `finally` 重置。
- 下层模块（存储、工具）**继承** contextvars，不自行绑定；只靠 `LogContextFilter` 自动回填 `trace_id` 等。
- 事件类日志用 `extra={**trace_log_extra(ctx), ...}` 显式带 trace（`runner.py:_record` 模式）。
- **禁止**在 `extra` 传 `"task_id": ""` 等空串，会屏蔽自动回填。

---

## 七、级别约定

| 级别 | 用途 | 示例 |
|------|------|------|
| `DEBUG` | 开发期细节，默认不污染生产 | 参数级追踪 |
| `INFO` | 正常入口 / 状态变更 / 关键步骤 | `task_created`、`http_request_finished` |
| `WARNING` | 可预期异常 / 降级 / 校验失败 | `sqlite_wal_unavailable`、`task_create_rejected` |
| `ERROR` | 意外异常、失败路径（`.exception()` 自动 ERROR） | `task_failed`、`sqlite_write_failed` |

---

## 八、自检清单（交付前）

```
□ 是否用项目 logger（self._logger 或模块级 _LOGGER），而非自建 / print？
□ event_name 是否稳定 snake_case、可聚合（非中文、非 f-string 拼动态）？
□ 业务字段是否进 extra / attributes，而非拼进 message？
□ 是否带中文 description，让中文开发者一眼能懂（可混英文专业词 / 函数名）？
□ 捕获异常是否用 .exception() 并带上下文？底层是否 raise 重抛？
□ 是否在正确层级记日志（底层记现场、上层记链路）？
□ 是否避免空 catch、避免吞异常？
□ 是否未输出 secret / 敏感信息？
□ 是否未在 extra 传空串屏蔽上下文回填？
□ 是否未手写 set_log_context（除非是运行时入口层）？
```

---

> 本规范与第一章「目的」一一对应：目的 1（故障复盘）→ 底层记现场；目的 2（全链路）→ 运行时层绑定 `trace_id`；目的 3（可观测）→ 状态变更点必记；目的 4（审计）→ 副作用操作必记；目的 5（旁路证据）→ 日志不抛异常、不阻塞、不输出 secret。
> 第四章「可读性」是对「写给谁看」的约束：机器键用英文保证可聚合，中文 `description` 保证中文开发者可读。

# 通用日志开发规范

> 本规范定义一套**与具体项目无关**的日志编写基线：为什么打、打什么格式、字段怎么组织、写在哪一层、写给谁看。
> 任何后端项目都可在此基线上落地，项目特有的约束（具体路径、框架名、存储实现）请在项目级附录中追加，不要污染本通用基线。
>
> **核心约束**：日志层**只保留 `trace_id` 一个链路关联键**。其余业务实体 ID（如 `task_id` / `run_id` / `tool_call_id` / `approval_id` …）不进日志顶层字段，由链路 / trace 体系承载；需要按业务实体查日志时，先经 trace 体系定位其 `trace_id`，再回日志查询。

---

## 一、目的：我们为什么打日志

打日志的目的不是「记录发生了什么」这种口号，而是**「出事时能定位」**。核心目的有五个，按重要性排序：

### 1. 故障复盘 —— 哪里错了、为什么错、影响了谁
这是首要目的。长时间运行的异步流程里，外部调用中断、执行失败、状态卡住、数据损坏都可能发生。没有日志，你只能看到「任务失败了」，但看不到根因。

日志必须能回答：
- **什么时间**（`ts`）
- **哪个链路**（`trace_id`，进而通过 trace 体系定位业务实体）
- **哪一层、哪一个事件**（`event` + `logger`）
- **哪段代码打的**（`caller`，精确到 `模块:类.方法:行号`）
- **什么错误、什么堆栈**（`error.type` / `error.message` / `error.stack`）

这也是为什么 `.exception()` 必须带堆栈、底层失败不能静默——否则你连「是存储写崩了还是外部服务断了」都分不清。

### 2. 全链路串联 —— 一次操作从头跟到尾
用 `trace_id` 贯穿一次请求的完整生命周期（接入 → 业务 → 工具/外部调用 → 存储）。`trace_id` 是日志层唯一的链路键：给你一个 `trace_id`，就能把这次操作从入口一路追到每一步、哪个外部调用、哪条数据访问出了问题。

如果某一层不记日志、或记了却没带 `trace_id`，这条链就断了——**不是不能跑，是出了事你追不下去**。

### 3. 行为可观测 —— 流程到底走到哪了
异步 / 长流程里，用户和开发者需要知道：流程创建了没、卡在哪一步、恢复没、关键状态存了没。状态变更点的 `info` 日志，目的是**把隐形的状态机变成可见的时间线**，而不是只等最终成功或失败。

### 4. 审计与可解释 —— 系统做了什么决策
副作用操作（外部调用、审批同意/拒绝、人工干预）都是「系统自主行为」，需要留痕。这不是调试用的，是**信任问题**：要知道系统替你执行了什么。这类事件必须落盘，且不能丢上下文。

> 注意：审计所需的实体 ID 记在 **trace 体系**里，不在日志顶层。日志只认 `trace_id`，需要审计追溯时通过 trace 反查。

### 5. 不阻断主流程的「旁路证据」
日志是**旁路**——它记录系统，但不参与系统决策。目的是即使主流程崩了，证据还在（异步落盘、队列回写等都是为此）。这也反向约束了日志写法：不能抛异常、不能阻塞、不能输出 secret。

### 什么**不是**日志的目的
- **不是给用户看的 UI 文案** —— 那是前端/事件流的事，日志是给开发 / 运维排查的。
- **不是业务数据流** —— 别把日志当消息总线传递状态。
- **不是装饰** —— `print("ok")`、空 `catch` 里的日志，没有上述目的，就是噪音。
- **不是替代断点调试** —— 运行时临时 debug 可以用，但落盘日志要为「出事后再看」服务，所以 `event` 要稳定、可聚合。

---

## 二、日志门面与 Logger 来源

统一 **一个 logger 名**（如 `your.app.backend`），**不要**在业务代码里随意 `logging.getLogger()` 自建多个 logger（除模块级常量场景）。统一 logger 名便于全局级别控制与格式化。

按模块角色分两种拿法：

| 模块角色 | 拿 logger 的方式 | 示例 |
|---------|----------------|------|
| 被注入 logger 的组件 | 用构造时注入的 `self._logger` | 业务服务类 |
| 无注入的底层模块 | 文件顶部定义模块级常量 `_LOGGER = logging.getLogger("your.app.backend")` | 存储层类 |

```python
# ✅ 模块级常量（每个文件只定义一次）
_LOGGER = logging.getLogger("your.app.backend")
```

---

## 三、日志格式（JSONL 固定 Schema）

落盘格式为**单行 JSON**（JSONL），由结构化 formatter 序列化，字段固定为 **9 个**：

```json
{"ts":"2026-07-16T08:04:08.518Z","level":"INFO","trace_id":"2a543a9d1f8c4b2e",
 "logger":"your.app.backend",
 "caller":"app.services.task:TaskService.create:120",
 "event":"task_created",
 "msg":"新任务已创建，等待调度",
 "data":{"agent_id":"developer","status":"pending"},
 "error":null}
```

### 九个字段定义

| 字段 | 谁填 | 角色 | 格式约束 |
|------|------|------|---------|
| `ts` | 自动 | 时间 | UTC 毫秒，`...Z` |
| `level` | 自动 | 级别 | DEBUG / INFO / WARNING / ERROR |
| `trace_id` | 自动 | **唯一链路键** | 由上下文 / 中间件自动回填 |
| `logger` | 自动 | 来源 | 固定统一 logger 名 |
| `caller` | 自动 | **代码位置** | `模块路径:类.方法:行号`，从日志记录取 |
| `event` | 手动 | 机器键 | 稳定英文 snake_case，**禁** f-string 拼动态 |
| `msg` | 手动 | 人读键 | 中文一句话，说清"发生了什么、为什么" |
| `data` | 手动 | 结构化业务字段 | JSON，可检索/过滤；无则 `{}` |
| `error` | 半自动 | 错误现场 | 仅出错非空 `{type,message,stack}`，正常 `null` |

### 字段纪律（四部分手写 + 五个自动 + 唯一关联键）
1. `event` —— **机器键，稳定英文 snake_case 事件名**（如 `task_created`、`tool_call_failed`）。用于聚合 / 告警 / 检索，**不要**用中文或 f-string 拼动态内容。
2. `msg` —— **人读键，中文一句话描述**"发生了什么、为什么"。与 `event` 分离：`event` 管聚合、`msg` 管人读，二者不再相等。
3. `data` —— **结构化业务字段（JSON）**，所有业务上下文（实体标识、状态、参数摘要、耗时等）都放这里，机器可检索、可过滤。业务实体 ID（`task_id` / `tool_call_id` …）的具体值可放在 `data` 里当检索字段，或混在 `msg` 里中文提及，但**绝不作为顶层关联键**。
4. `error` —— **错误现场块**，仅出错时非空：`{"type":..., "message":..., "stack":...}`。正常日志为 `null`。
5. **唯一关联键只有 `trace_id`**。日志层**不再**保留 `task_id` / `run_id` / `span_id` / `event_id` / `step_id` / `tool_call_id` / `approval_id` 等独立关联键——这些由链路 / trace 体系承载。框架 / 上下文自动填 `trace_id`，**不要**在 `extra` 里传空串（空串会屏蔽自动回填）。
6. `ts`（UTC 毫秒 `Z`）、`level`、`logger`、`caller` 自动填，**开发者不手写**。
7. `data` 自动脱敏，超长自动截断（表现为对应值被截断标记，不新增顶层字段）。

### 落盘示例（五个通用场景）
```json
// 1. 流程/任务创建（普通 INFO）
{"ts":"2026-07-16T10:02:11.318Z","level":"INFO","trace_id":"2a543a9d1f8c4b2e","logger":"your.app.backend","caller":"app.services.task:TaskService.create:120","event":"task_created","msg":"新任务已创建，等待调度","data":{"agent_id":"developer","status":"pending"},"error":null}

// 2. 外部调用超时（WARNING，data 带耗时）
{"ts":"2026-07-16T10:02:45.902Z","level":"WARNING","trace_id":"2a543a9d1f8c4b2e","logger":"your.app.backend","caller":"app.tools.executor:ToolExecutor.run:88","event":"tool_call_timeout","msg":"调用 read_file 工具超时，已触发熔断，tool_call_id=9c1a","data":{"tool":"read_file","duration_ms":30500,"threshold_ms":30000},"error":null}

// 3. 数据写入失败（ERROR，带 error 块）
{"ts":"2026-07-16T10:03:02.117Z","level":"ERROR","trace_id":"2a543a9d1f8c4b2e","logger":"your.app.backend","caller":"app.storage.task_store:TaskStore.update_status:64","event":"db_write_failed","msg":"更新任务状态写入数据库失败，task_id=t-7781","data":{"task_id":"t-7781","operation":"update_status"},"error":{"type":"OperationalError","message":"database is locked","stack":"Traceback (most recent call last):\n  File \"app/storage/task_crud.py\", line 64, in update_status\n    ..."}}

// 4. 审批通过（INFO，业务字段在 data）
{"ts":"2026-07-16T10:03:30.441Z","level":"INFO","trace_id":"7b0e2c5a9d3f1a64","logger":"your.app.backend","caller":"app.service.approval:ApprovalService.resolve:142","event":"approval_resolved","msg":"用户已批准工具调用，审批通过","data":{"approval_id":"ap-22","tool":"write_file","decision":"allow"},"error":null}

// 5. 步骤开始（DEBUG，参数摘要在 data）
{"ts":"2026-07-16T10:03:31.009Z","level":"DEBUG","trace_id":"7b0e2c5a9d3f1a64","logger":"your.app.backend","caller":"app.tool_execute.engine:Engine.step:455","event":"step_start","msg":"开始执行第 3 步，准备调用模型","data":{"step_index":3,"prompt_tokens":1820},"error":null}
```

### 打印写法
```python
# 普通结构化日志：event(英文聚合) + msg(中文人读) + data(业务字段)
_LOGGER.info("task_created", extra={
    "msg": "新任务已创建，等待调度",
    "data": {"agent_id": agent_id, "status": "pending"},
})

# 捕获异常必须带堆栈 —— 用 .exception()，不要 .error()
except Exception as exc:
    _LOGGER.exception("task_failed", extra={
        "msg": "模型流式接口中断，任务执行失败",
        "data": {"task_id": task_id},   # 实体 ID 值放 data，不作关联键
    })
    raise  # 存储层 / 底层：记完重新抛出，让上层也能记上下文
```

### 禁止
```python
# ❌ f-string 拼 event：不可聚合、破坏检索
_LOGGER.error(f"写任务 {task_id} 失败: {exc}")
# ❌ print 当系统日志
print("something failed")
# ❌ 空 catch
except Exception:
    pass
# ❌ 在 extra 里塞 task_id / run_id 等独立关联键（应只留 trace_id，实体 ID 值放 data/msg）
_LOGGER.info("task_created", extra={"task_id": task.task_id, ...})
# ❌ 把可读描述塞进 event（event 必须稳定英文，描述走 msg）
_LOGGER.info("新任务已创建", extra={...})
```

---

## 四、可读性：中文开发者能看懂

日志的最终读者主要是**中文开发者**。因此日志里「给人看的那部分」要用中文写清楚，让人一眼能懂；但**机器聚合用的稳定键、专业术语、函数名、变量名、错误类型可以保留英文**——不必强行全中文翻译。

### 原则
1. **机器键保留英文（不变）**：`event`（如 `task_failed`）、`trace_id`、`error.type`——这些是给程序聚合、检索、串联用的，必须稳定、可解析。**日志层只保留 `trace_id` 一个链路键**；业务实体 ID 由 trace 体系承载，不进日志顶层字段。
2. **给人看的写中文 `msg`**：每条有「业务含义」的日志都应带 `msg`（放在 `extra` 里），用中文说明**发生了什么、为什么**。业务实体 ID 的**具体值**可在 `msg` 里以中文混排提及（如「task_id=xxx 创建失败」），便于人读，但不作为日志索引键。结构化字段放 `data`，不塞进 `msg`。
3. **中文里可以混英文**：`msg` 里可混用英文专业词 / 函数名 / 变量值（如「调用 openai 流式接口失败，tool_call_id=xxx 超时」），不必全中文。
4. **纯技术现场不翻译**：`.exception()` 自动填的 `error.message` / `error.stack`（traceback 本身是英文）保持不变——开发者读堆栈习惯英文，翻译会丢失精确性，也不该翻译。

### 写法
```python
# ✅ event 英文（可聚合），msg 中文（可读），实体 ID 值混在 msg / data
_LOGGER.exception("db_write_failed", extra={
    "msg": f"更新任务状态写入数据库失败，task_id={task_id}",
    "data": {"operation": "update_status"},
})

# ✅ 中文 msg 里混英文专业词 / 函数名 / 实体 ID 值
_LOGGER.warning("tool_call_timeout", extra={
    "msg": f"调用 read_file 工具超时，tool_call_id={tool_call_id}，已触发熔断",
    "data": {"tool": "read_file"},
})
```

```python
# ❌ 全英文 msg，中文开发者需脑内翻译
_LOGGER.exception("db_write_failed", extra={"msg": "failed to write task status into db"})

# ❌ 把中文塞进 event（破坏聚合）
_LOGGER.info("任务创建成功", extra={...})  # event 必须是稳定英文键

# ❌ 把实体 ID 当日志顶层独立键（应只留 trace_id，值放 data/msg）
_LOGGER.warning("tool_call_timeout", extra={"tool_call_id": tool_call_id, ...})
```

---

## 五、在哪里打印日志（分层职责）

按分层架构，各层日志职责不同。**核心原则：谁掌握上下文，谁记；底层记现场，上层记链路。**

### 1. 接入层（API / 网关 / 路由）
- **不记业务细节**。端点只做：捕获预期异常 → 转统一异常响应。
- 业务异常的 `detail` 由全局异常处理器统一记，端点代码**不重复记**。
- 端点 `except` 块**不要**手写 `logger.*`（已由全局处理器覆盖）；只有意外 500 才在 `except Exception` 里额外 `.exception()`。

### 2. 运行时 / 业务层（编排、领域服务）
- **这是日志主力层**，负责链路级上下文与状态变更记录。
- 进入业务处理前绑定链路上下文（如 `trace_id`），`finally` 里重置。
- 状态变更、事件、失败一律记：
  - 正常状态变更 → `info`（如 `task_created`、`task_cancelled`）。
  - 关键路径（恢复、审批、外部编排）→ `info` + `event` 稳定名。
  - 捕获意外异常 → `.exception()` 并 `raise`。

### 3. 数据 / 存储层
- **记数据操作现场**。当前常见问题是存储层几乎静默，写入 / 查询失败只抛不记，需补齐。
- 写操作 `except` 数据访问异常时 `.exception()` 并 `raise` 重抛。
- **不要**在存储层自行绑定链路上下文 —— 它跑在已绑定上下文的作用域内，`trace_id` 由上下文自动回填。只补存储层已知字段（如 `operation`）。**业务实体 ID 不进日志顶层**——日志只认 `trace_id` 一个链路键。

### 4. 工具 / 外部调用层
- 外部调用入口 / 出口记 `info`（名称、参数摘要、耗时、状态）。
- 执行失败 → `.exception()` 带 `msg`（中文说明名称与失败原因，含实体 ID 值），**不**把实体 ID 作为日志独立键。
- 敏感参数（如 API Key）不进 `data`（脱敏会自动处理，但也不要主动拼）。

### 5. 启动 / 关闭 / 配置
- 启动：记配置加载结果（关键开关、路径等，**不输出 secret 原文**）。
- 关闭：资源释放失败记 `error`。

### 6. 后台任务 / 子进程
- 与在线服务同标准：入口、关键步骤、异常、退出都记。
- 子进程日志经异步通道回主进程落盘，写法不变。

---

## 六、上下文注入规则（全链路串联关键）

- 链路上下文（如 `trace_id`）仅由**入口层**（接入层 / 业务入口）绑定，并在 `finally` 重置。
- 下层模块（存储、工具、外部调用）**继承**上下文，不自行绑定；只靠 formatter / 中间件自动回填 **`trace_id`**。**日志只认 `trace_id` 一个链路键**，不再回填 `task_id` / `run_id` 等独立关联键。
- 事件类日志用 `extra={**trace_extra(ctx), ...}` 显式带链路（入口层统一封装的模式），避免每处散着记。
- **禁止**在 `extra` 传 `"trace_id": ""` 等空串，会屏蔽自动回填。

---

## 七、级别约定

| 级别 | 用途 | 示例 |
|------|------|------|
| `DEBUG` | 开发期细节，默认不污染生产 | 参数级追踪 |
| `INFO` | 正常入口 / 状态变更 / 关键步骤 | `task_created`、`request_finished` |
| `WARNING` | 可预期异常 / 降级 / 校验失败 | `db_unavailable`、`create_rejected` |
| `ERROR` | 意外异常、失败路径（`.exception()` 自动 ERROR） | `task_failed`、`db_write_failed` |

---

## 八、自检清单（交付前）

```
□ 是否用统一 logger（注入的 self._logger 或模块级 _LOGGER），而非自建 / print？
□ event 是否稳定 snake_case、可聚合（非中文、非 f-string 拼动态）？
□ 业务字段是否进 data，而非拼进 msg？
□ 是否带中文 msg，让中文开发者一眼能懂（可混英文专业词 / 函数名 / 实体 ID 值）？
□ 捕获异常是否用 .exception() 并带上下文？底层是否 raise 重抛？
□ 是否在正确层级记日志（底层记现场、上层记链路）？
□ 是否避免空 catch、避免吞异常？
□ 是否未输出 secret / 敏感信息？
□ 日志 extra 是否只带 trace_id（自动回填）+ msg + data，未塞 task_id / run_id / tool_call_id 等独立关联键？
□ 是否未手写链路上下文绑定（除非是入口层）？
```

---

> 本规范与第一章「目的」一一对应：目的 1（故障复盘）→ 底层记现场；目的 2（全链路）→ 入口层绑定 `trace_id`，且日志层**只保留 `trace_id`** 一个链路键；目的 3（可观测）→ 状态变更点必记；目的 4（审计）→ 副作用操作必记（实体 ID 走 trace 体系）；目的 5（旁路证据）→ 日志不抛异常、不阻塞、不输出 secret。
> 第四章「可读性」是对「写给谁看」的约束：机器键用英文保证可聚合，中文 `msg` 保证中文开发者可读。
> **日志只保留 `trace_id` 的取舍**：`trace_id` 覆盖一次完整请求且与业务实体正交，是日志层唯一稳定的链路键；去掉 `task_id` / `run_id` 等独立列可大幅简化日志存储与索引，需要按实体追溯时统一经 trace 体系反查，避免日志与业务表字段重复、漂移。

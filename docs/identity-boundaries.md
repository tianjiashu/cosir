# 身份标识（ID）说明

> 本文档说明 `coding-agent` 后端各类 ID 的**作用与边界**，供日志排查、联调、架构决策参考。
> 基于 `apps/backend/app` 实际代码整理，最后更新 2026-07-16。

---

## 一、先记住这张图

系统的 ID 分两类，互相**正交**（一个可以没有另一个，不能合并）：

```
【业务链路】做什么、做到哪、能否续跑
  taskId ─┬─ threadId      执行环境（LangGraph 线程，可续跑）
          ├─ turnId        对话轮次（回放按轮重演）
          ├─ runId         执行实例（卡在哪、等什么、能否恢复）
          ├─ checkpointId  状态恢复点
          ├─ toolCallId    单次工具调用
          └─ approvalId    工具权限审批

【请求追踪】这次请求经历了什么、花了多久
  traceId ─┬─ eventId      发生的「点」（回放/排序/去重）
           └─ spanId       耗时的「线段」（性能剖析）
```

**日常你只需盯 2 个：`taskId`（业务锚点）和 `traceId`（排查链路）。** 其余都是库内主键，不用记。

**两类怎么关联**：trace 记录里带 `task_id` 字段，把「请求」挂到「任务」上。但一个纯查询请求可以只有 `traceId` 没有 `taskId`。

---

## 二、两个最核心的 ID

### `taskId` —— 任务锚点
一次用户提交的 Agent 工作（"帮我加个登录功能"）。是几乎所有记录的外键，日志里最稳定的关联键。**问"这是哪件事"就看它。**

### `traceId` —— 排查链路
和日志绑定，覆盖**所有客户端请求**（不止对话，拉列表、查详情都有）。用来把散落各处的日志串成一条链。**问"这次请求出了什么问题"就追它。**

> 关键：`traceId` 不是 `taskId` 的别名。一个请求可以没有 task；一个任务多轮对话，每轮是独立请求、各有独立 traceId，但共享同一个 taskId。两者维度不同，不能合并。

---

## 三、业务链路的其余 ID

| ID | 作用 | 一句话记忆 |
|----|------|-----------|
| `threadId` | LangGraph 持久化线程 | 任务的"执行环境"，定时任务回来可在此续跑 |
| `turnId` | 一次用户↔Agent 交互 | "第几轮对话"，回放按它重演 |
| `runId` | 可恢复的执行生命周期 | "跑到哪、卡在哪、等审批还是等输入" |
| `checkpointId` | 运行时状态快照 | 续跑/回放"从哪一步恢复" |
| `toolCallId` | 一次工具调用请求 | 回放定位"第几步调了哪个工具" |
| `approvalId` | 工具权限审批 | 独立业务流："这次权限请求批没批" |

**最易混的一对**：
- `taskId` = 做什么（任务定义）；`runId` = 做到哪（执行状态）。涉及 checkpoint / 恢复 / 等待用 `runId`，涉及输入 / 定义用 `taskId`。
- `turnId` = 对话轮；`runId` = 执行周期。都挂 task 下，别互相替代。

---

## 四、请求追踪的其余 ID

| ID | 作用 | event vs span |
|----|------|---------------|
| `eventId` | 一条事件的主键，"某刻发生了某事" | **点**：工具调用已开始 |
| `spanId` | 一段操作的耗时与嵌套 | **线段**：工具调用耗时 45ms、成功 |

- `eventId` 用于回放排序、前端去重（按 `sequence_no + eventId` 重建时间线）。
- `spanId` 用于性能剖析——看 `duration_ms` 就知道是模型慢还是工具慢；树状结构靠 `parent_span_id` 串成调用树。主链路排查不依赖它，属可选性能层。

---

## 五、更细的子主键（库内用，不进日志）

这些只在各自数据库表里当主键，不暴露到通用日志，日常无需关注：

| ID | 作用 | 父级 |
|----|------|------|
| `session_id` | 一组任务的容器（多会话 UI 才有意义） | 无 |
| `step_id` | turn 内一次运行时动作 | `turn_id` |
| `execution_id` | 工具 handler 一次实际执行（支持重试） | `tool_call_id` |
| `decision_id` | 一次审批结论（幂等） | `approval_id` |
| `artifact_id` | 工具产出物 | `run_id`+`step_id` |
| `request_id` / `response_id` | 人工输入提问 / 回答 | `run_id`+`step_id` / `request_id` |
| `command_id` | 一次 resume 指令（幂等） | `run_id` |

---

## 六、已知问题与改进建议

代码现状与理想模型有几处偏差，供后续决策：

1. **trace 被锁死在 task 上**：当前 `recorder.py` 按 `task_id` 缓存 traceId，导致非对话请求进不了 trace 体系。应改为**中间件每请求生成一个 traceId**，task 仅作 trace 上的标签。
2. **eventId 有两套互不相通**：trace ledger 一套（`trace/records.py`）、SSE 一套（`events/types.py`），回放事件和实时事件对不上。应合并成一套。
3. **runId 与 task 严格 1:1**：`create_for_task` 复用既有 run，不支持重跑。若未来要"重跑/从检查点分支"，应放开为 1:N。
4. **threadId 可派生自 taskId**：因 task 与 thread 严格 1:1，可省掉 run 对 thread 的间接持有，简化执行环境锚点。

---

## 七、日志该带哪些 ID

- **必带**：`task_id`（最稳锚点）、`trace_id`（自动回填，全链路）。
- **执行相关**：`run_id`（定位 checkpoint / 等待原因）。
- **工具/审批失败**：补 `tool_call_id` / `approval_id`，否则追不到具体哪次调用。
- 其余子主键按需，不强制。详见 `rules/Agent日志开发规范.md`。

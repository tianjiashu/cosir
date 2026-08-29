# 上下文占用圆环主 Agent 挂载与并发 Task 修复方案

> 状态：提案待审（2026-08-28）。
> 目标：修复 `CONTEXT_USAGE` 从后端上下文管理到前端 usage 圆环的整条链路缺陷，并保持两个产品语义：**只有主 Agent 挂载上下文占用统计**；**前端支持并发 task，后台 task 事件不得污染当前任务圆环**。
> 约束：本文只给方案，不修改业务代码。实现时需遵守第零铁律：以长期稳定迭代为准，修复根因而不是在 UI 层遮丑。

---

## 一、设计本意

1. **统计对象只覆盖主 Agent**  
   usage 圆环面向用户当前对话的主任务上下文余量。委派子 Agent 是内部执行细节，不应把 child task 的上下文占用推到用户输入栏，也不应回写父任务的 usage。

2. **并发 task 必须隔离**  
   桌面端允许多个 task 的 turn 同时运行。后台 task 的 `context_usage` 事件可以被前端接收和缓存，但不能覆盖当前 active task 的圆环。

3. **上下文窗口占用是 task 级最新值**  
   一个 task 内多轮 turn 共享上下文历史，圆环显示该 task 最近一次主 Agent 上下文估算值。打开历史任务时优先用后端持久化值快速回填，实时运行时再由 SSE 事件更新。

---

## 二、现状缺陷

### 2.1 主 Agent listener 被反向过滤

当前 `RuntimeContextManager.add_change_listener()` 的守卫逻辑为：

```python
if not listener.subAgent_need and self.agent_profile.main_agent:
    return self
```

而 `ContextUsageComputeListener.subAgent_need = False`。结果是主 Agent 恰好被跳过，主任务不会产生 `context_usage` 事件，也不会回写 `tasks.context_usage_used`。

这是命名和判断语义反了：字段名表达“子 Agent 是否需要”，但守卫实际把“不需要子 Agent”的 listener 排除在主 Agent 之外。

### 2.2 `ListenerEvent` 字段未初始化

`ListenerEvent.__init__()` 只设置了 `type` 和 `messages`，没有设置 `usage` / `total_tokens`。`ContextUsageComputeListener.listen()` 会访问这两个字段。只要 listener 真正挂载并触发，就会抛 `AttributeError`。

### 2.3 `load_history()` 阶段不适合直接写 SSE 事件

manager 创建后马上 `load_history()`，此时还没进入 `graph.astream()`，`write_event()` 内部的 `get_stream_writer()` 没有 LangGraph 运行上下文。若 listener 在 `load_history()` 里直接写 `CONTEXT_USAGE`，会因为 writer 不存在而失败。

### 2.4 本轮用户输入未进入实时 usage

turn 启动时用户消息通过 `add_message(..., write_memory=False)` 只落库、不写入 `messages`，因此不会触发 `mark_context_changed()`。这会导致本轮输入、附件文本、图片 block 至少在实时事件里被低估。

### 2.5 前端 usage store 是全局单值

`contextUsageStore` 只保存一份 `usedTokens/totalTokens`。`useSSE` 支持多 turn 并发连接，任何后台 task 的 `context_usage` 都会覆盖圆环。`connect()` 建立任意新连接时还会全局 `reset()`，可能清掉当前任务的 usage。

### 2.6 `openTask()` 回填 usage 缺少竞态保护

`openTask()` 在 `setActiveTask()` 的序号竞态判断之前就回填 `contextUsageStore`。快速切换 task 时，旧请求即使不能切回 active task，也仍可能覆盖圆环 usage。

### 2.7 软上限默认口径不一致

`Settings.CONTEXT_WINDOW_TOKENS` 类字段默认是 `200000`，但 `Settings.load()` 未配置时用 `"0"` 覆盖，语义变成“不设软上限”。如果产品预期圆环默认分母为 200K，需要统一配置默认值和注释。

---

## 三、后端修复方案

### 3.1 明确 listener 挂载语义

建议废弃 `subAgent_need` 这种易误读字段，改成正向语义：

```python
class ContextListener(Protocol):
    main_agent_only: bool
```

或更直接：

```python
def should_attach_to(self, agent_profile: AgentProfile) -> bool: ...
```

推荐第一阶段采用 `main_agent_only`，改动小且语义清晰：

| listener | 语义 | 配置 |
|---|---|---|
| `ContextUsageComputeListener` | 只挂主 Agent | `main_agent_only = True` |
| `ContextCompressListener` | 如仍需所有 Agent | `main_agent_only = False` |

`add_change_listener()` 判断改为：

```python
if listener.main_agent_only and not self.agent_profile.main_agent:
    return self
```

这样严格满足“只有主 Agent 才可以挂载 usage listener”。

### 3.2 修复 `ListenerEvent` 完整初始化

`ListenerEvent.__init__()` 必须设置：

```python
self.type = type
self.messages = messages
self.usage = usage
self.total_tokens = total_tokens
```

同时建议把它改成 frozen dataclass，避免以后再漏字段：

```python
@dataclass(frozen=True)
class ListenerEvent:
    type: ContextEventType
    messages: list[RuntimeMessage]
    usage: int
    total_tokens: int
```

### 3.3 拆分“计算/回写”和“SSE 事件发送”的时机

`load_history()` 发生在 graph writer 可用前，不能直接强依赖 `write_event()`。推荐给 usage listener 增加安全写入策略：

1. `load_history`：允许计算 `used_tokens` 并回写 `task.context_usage_used`，但不强制发 SSE。
2. `add_message` / `context_compressed`：在 graph 运行期发 `CONTEXT_USAGE` 并回写 task。
3. writer 不可用时：记录 debug/warn 后跳过 SSE，不中断主流程。

实现形态可以是注入一个 `emit_context_usage(payload) -> bool` 回调，内部捕获 `RuntimeError`，返回是否成功发送。listener 不直接知道 LangGraph。

### 3.4 本轮用户输入的 usage 口径

当前 `write_memory=False` 让本轮用户消息不进内存上下文，下一次 `load_message()` 也不会看到本轮输入。若这是已有架构的刻意设计，需要单独审查；仅从 usage 圆环角度，推荐至少做到：

1. 用户消息如果会实际进入模型请求，就必须进入 usage 估算。
2. 不想重复写内存时，可以新增 `notify_context_changed=True` 或专门的 `record_usage_only` 路径，但长期看更清晰的是让 `RuntimeContextManager.messages` 与实际发给模型的上下文保持一致。
3. 图片 `content_blocks` 只存在运行期内存，若不写 memory，就无法统计图片 token。支持多模态后这会明显低估。

验收口径：用户提交一条新 turn 后，在第一次模型请求前或模型请求刚开始时，usage 至少包含系统提示、历史消息、本轮用户输入和本轮图片估算。

### 3.5 `total_tokens` 口径统一

`RuntimeContextManager.bind_turn()` 已按 `turn.model_name` 调 `resolve_context_window()`，API `get_task()` 也按最近 turn model 算 `context_window_total`。建议保留该口径。

需要明确 `Settings.load()` 默认值：

| 预期 | 设置 |
|---|---|
| 默认软上限 200K | `os.environ.get("CODING_AGENT_CONTEXT_WINDOW_TOKENS", "200000")` |
| 默认不设软上限 | 类字段、注释、文档全部改为 0 |

建议选其一，不保留类字段 200K、加载默认 0 的双轨状态。

---

## 四、前端修复方案

### 4.1 usage store 改为 task 维度

把全局单值：

```ts
usedTokens: number
totalTokens: number
updatedAt: string | null
```

改为按 taskId 存储：

```ts
usageByTaskId: Record<string, {
  usedTokens: number
  totalTokens: number
  updatedAt: string | null
}>
```

动作建议：

```ts
setUsage(taskId: string, payload: ContextUsagePayload, updatedAt: string): void
resetTask(taskId: string): void
clearAll(): void
selectUsage(taskId: string | null): ContextUsageView
```

### 4.2 SSE 事件按 event.task_id 写入对应 task

`useSSE` 收到 `context_usage` 时：

```ts
setContextUsage(event.task_id, event.payload, event.created_at)
```

不再使用当前 active task 作为隐式归属，也不再让后台事件覆盖当前圆环。

### 4.3 圆环只读 active task 的 usage

`ContextUsageRing` 从 `taskStore.activeTaskId` 取当前任务，再从 `contextUsageStore.usageByTaskId[activeTaskId]` 派生显示值。无 active task 或该 task 无 usage 时显示 0/0 或隐藏，按现有 UI 取舍决定。

### 4.4 删除全局 reset 副作用

`useSSE.connect()` 不应在任意连接建立时全局 `reset()`。替代策略：

1. 新建真实 task 后，如果没有后端回填值，可以 `resetTask(newTaskId)`。
2. 打开任务时，用 `getTask()` 返回的 `context_usage_used/context_window_total` 写入该 task。
3. 删除 task / 清空 workspace 时清理对应 task usage。

### 4.5 `openTask()` 回填纳入竞态保护

`openTask()` 拉到 task 后，应先判断本次请求仍是最新序号，再更新 active task 对应的 visible usage。或者更稳妥：无论是否过期，都只写 `usageByTaskId[taskId]`，而圆环由 activeTaskId 选择展示。这样旧请求最多刷新自己的 task 缓存，不会污染当前 UI。

推荐第二种，因为它与 task 维度 store 天然一致。

---

## 五、事件与持久化链路

修复后链路应为：

```text
RuntimeContextManager.add_message / load_history / maybe_compact
  -> mark_context_changed(event_type, messages)
  -> ContextUsageComputeListener（仅 main_agent）
  -> 估算 used_tokens + 读取 total_tokens
  -> 回写 tasks.context_usage_used
  -> graph 运行期发送 RuntimeEvent(context_usage)
  -> RuntimeEventService save_and_publish
  -> /turns/{turn_id}/stream SSE
  -> useSSE 按 event.task_id 写 contextUsageStore.usageByTaskId
  -> ContextUsageRing 按 activeTaskId 读取显示
```

历史打开链路：

```text
GET /tasks/{task_id}
  -> TaskResponse(context_usage_used, context_window_total)
  -> useTask.openTask()
  -> contextUsageStore.setUsage(task_id, payload, task.updated_at)
  -> ContextUsageRing 按 activeTaskId 读取显示
```

---

## 六、测试方案

### 6.1 后端单元测试

新增或修复以下测试：

1. `test_usage_listener_attaches_only_to_main_agent`  
   主 Agent 挂载 `ContextUsageComputeListener`，子 Agent 不挂载。

2. `test_listener_event_initializes_usage_and_total_tokens`  
   构造 `ListenerEvent` 后能读取 `usage` / `total_tokens`。

3. `test_usage_listener_load_history_does_not_require_stream_writer`  
   `load_history()` 触发 usage 计算和 task 回写时，即使 writer 不可用也不抛。

4. `test_usage_event_emitted_during_graph_runtime`  
   graph 运行期 `add_message()` 后产生 `CONTEXT_USAGE` payload，字段为 `used_tokens/total_tokens`。

5. `test_child_agent_does_not_emit_context_usage`  
   委派 child task 不产生 `context_usage`，也不写父 task usage。

6. `test_current_turn_user_input_counted_in_usage`  
   新 turn 的本轮输入被纳入估算，图片附件场景至少覆盖一例。

### 6.2 前端单元测试

新增或修复以下测试：

1. `contextUsageStore.taskIsolation.test.ts`  
   A/B 两个 task 分别 setUsage，互不覆盖。

2. `useSSE.contextUsageIsolation.test.tsx`  
   后台 task B 的 `context_usage` 到达后，active task A 的圆环仍显示 A 的 usage。

3. `useTask.openTask.contextUsageRace.test.ts`  
   快速打开 A 再打开 B，A 的迟到响应不会让 B 的圆环显示 A 的 usage。

4. `ContextUsageRing.activeTask.test.tsx`  
   activeTaskId 切换时圆环读取对应 task usage。

5. `taskDelete.contextUsageCleanup.test.ts`  
   删除 task 后清理该 task usage 缓存。

---

## 七、实施步骤

1. **后端基础修复**  
   修 `ListenerEvent` 字段初始化；把 listener 挂载判断改成 `main_agent_only` 正向语义。

2. **后端事件时机修复**  
   调整 usage listener：writer 不可用时不抛；`load_history` 只计算/回写，运行期再 emit。

3. **后端 usage 口径修复**  
   确认本轮用户输入是否应写入 `RuntimeContextManager.messages`；若实际会发给模型，usage 必须统计它。

4. **前端 task 维度 store 改造**  
   `contextUsageStore` 从全局单值改为 `usageByTaskId`。

5. **前端 SSE 和 openTask 接入改造**  
   SSE 按 `event.task_id` 写入；`openTask()` 按 taskId 回填；移除全局 reset。

6. **统一窗口上限默认值**  
   决定 `CONTEXT_WINDOW_TOKENS` 默认是 200K 还是 0，并同步代码注释、文档和测试。

7. **补齐测试闭环**  
   后端覆盖 listener 装配/事件/回写/child 隔离；前端覆盖并发 task 隔离和 openTask 竞态。

---

## 八、验收标准

1. 主任务运行时能收到 `context_usage` SSE 事件，payload 含正确的 `used_tokens` / `total_tokens`。
2. `GET /tasks/{task_id}` 能回填该 task 最近一次 `context_usage_used` 和 `context_window_total`。
3. 委派子 Agent 不产生用户圆环 usage 事件，不污染父 task usage。
4. 两个 task 并发运行时，后台 task 的 usage 事件不会改变当前 active task 圆环。
5. 快速切换任务时，迟到的 `openTask()` 响应不会覆盖当前 active task 圆环。
6. 本轮用户输入和图片附件纳入 usage 估算。
7. `CONTEXT_WINDOW_TOKENS` 默认值在代码、注释、测试中一致。

---

## 九、关键取舍

1. **不建议前端过滤掉非 active task 的 usage 事件**  
   过滤能避免污染当前 UI，但会丢掉后台 task 的最新 usage 缓存。按 taskId 存储更符合并发模型。

2. **不建议让 child task usage 事件进入同一个圆环 store 再靠 UI 隐藏**  
   这会把内部执行细节暴露到全局状态，后续很容易被别的组件误用。后端不挂载 child usage listener 是更干净的边界。

3. **不建议继续使用 `subAgent_need` 命名**  
   它已经造成反向判断。正向布尔或 `should_attach_to()` 比注释修补更稳。

4. **不建议只修 `ListenerEvent` 字段**  
   这样会暴露 `load_history()` 阶段 writer 不可用的新异常。必须同时处理事件发送时机。

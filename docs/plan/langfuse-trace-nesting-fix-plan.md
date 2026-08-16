# Langfuse 父子 Agent trace 嵌套与记录修复方案

> 状态：审查通过（2026-08-15，已对照 langfuse 4.14.1 源码 F1-F7 与当前代码 C1-C6 逐条核对）；方案设计，未落地
> 日期：2026-08-15
> 范围：`apps/backend/app/core/observability/`、`apps/backend/app/service/tool_execution/`、`apps/backend/tests/`
> 关联：`docs/plan/langfuse-future-roadmap.md`（P0：LLM generation 与工具 observation 稳定归入同一 turn trace）

---

## 0. 结论速览

当前 `turn_trace` 存在两个相互独立、但都会导致「Langfuse 上父子 Agent 行为割裂」的问题：

| # | 问题 | 根因 | 后果 |
|---|------|------|------|
| **A** | trace_id 双轨 | `turn_trace` 的根 span 用 OTel 自动 trace_id，`CallbackHandler` 用预分配 `new_trace_id()`，两者永不相等 | 主 turn 的 LLM generation 与「turn 根 span + 工具 span」被拆到两个不同 trace |
| **B** | delegate 子 Agent 脱离 | `delegate_task` 走 `ThreadPoolExecutor.submit`（不复制 contextvars），丢 OTel current context | 子 Agent 的 tool/turn/LLM 全部脱离父 turn trace |

修复目标：**一个 turn = 一棵 langfuse trace**，子 Agent turn 作为父 turn 内 `delegate_task` 工具 span 的子树嵌套，达到如下理想结构：

```text
父 turn trace（session_id = task_id）
└── span "turn {父 turn_id}"                     ← 根 observation
    ├── generation（主 LLM 调用）                 ← 修复 A
    ├── tool span（serial 工具，如 read_file）     ← 现状已正确
    └── tool span（delegate_task）                ← 修复 B
        └── span "turn {子 turn_id}"              ← 修复 B（asyncio.run 复制 context）
            ├── generation（子 LLM 调用）          ← 修复 A（子 turn 同样走 turn_trace）
            └── tool span（子工具调用）
```

> 注：上图为**理想简化结构**。实际 ReAct + LangGraph 链路中，graph 作为 Runnable 会触发 `CallbackHandler.on_chain_start`，在根 span 与 LLM generation 之间额外产生一层「graph chain span」（`start_observation(trace_context=None)`，跟随 current 挂根 span 下）。该层不影响「同一 turn 一棵 trace」的核心目标，探针验证时可能看到比上图多一层的结构，属 langfuse-langchain 插件正常行为，勿当作异常。

两个修复均**复用 langfuse 官方 OTel context 传播机制 + Python 标准库 `contextvars`**，不引入新依赖、不自研轮子。

---

## 1. 源码事实依据

### 1.1 langfuse 4.14.1 关键机制（`.venv/Lib/site-packages/langfuse/`）

**（F1）`start_as_current_observation` 无 `trace_context` 时跟随 OTel current context 并 `set_current`。**

`_start_as_current_otel_span_with_processed_media`（`_client/client.py`）内部走 `self._otel_tracer.start_as_current_span(name=..., end_on_exit=True)`。OTel 的 `start_as_current_span` 语义：
- 有父 → 新 span 作为当前 span 的子 span；
- 无父 → 新 span 成为根，trace_id 由 OTel 自动生成；
- 无论有无父，都把新 span 设为 current（后续无 `trace_context` 的 observation 会挂其下）。

**（F2）LLM generation 的创建路径：`__on_llm_action` 直接传 `self._trace_context`。**

`CallbackHandler.__on_llm_action`（`langchain/CallbackHandler.py` 第 1232-1243 行）在链根（`parent_run_id is None`）时，`_get_parent_observation(None)` 返回 `self._langfuse_client`（Langfuse 客户端本身，第 680-694 行），随后：

```python
parent_observation = self._get_parent_observation(parent_run_id)
if isinstance(parent_observation, Langfuse):          # 链根：parent 是 Langfuse 客户端
    generation = parent_observation.start_observation(
        trace_context=self._trace_context,             # ← 直接传构造时注入的 trace_context
        as_type="generation", **content,
    )
else:                                                  # 子 run：parent 是具体 observation
    generation = parent_observation.start_observation(
        as_type="generation", **content                # ← 不传 trace_context
    )
```

注意：LLM generation 走的是 `__on_llm_action`，**直接传 `self._trace_context`**，不经过 `_take_root_trace_context`（后者是 `on_chain_start` 里 chain/agent/span 类型用的，第 608-612 行，逻辑等价：`self._trace_context` 非 None 则用之，否则检测 OTel current span 是否 valid）。

**（F3）`start_observation` 对 `trace_context` 的分流（决定 generation 挂到哪个 trace）。**

`client.start_observation`（`_client/client.py` 第 712-761 行）：

```python
if trace_context:                                       # ← 有 trace_context（现状）
    trace_id = trace_context.get("trace_id", None)
    if trace_id:
        remote_parent_span = self._create_remote_parent_span(
            trace_id=trace_id, parent_span_id=parent_span_id
        )
        with otel_trace_api.use_span(remote_parent_span):
            otel_span = self._otel_tracer.start_span(name=name)
            otel_span.set_attribute(..., AS_ROOT, True)  # ← 强制挂预分配 trace，AS_ROOT
            return self._create_observation_from_otel_span(...)

# trace_context 为 None 时（修复 A 后）
otel_span = self._otel_tracer.start_span(name=name)      # ← 读 current context 的 current span 作为 parent
return self._create_observation_from_otel_span(...)
```

关键结论（修复 A 正确性的最终依据）：**`start_observation(trace_context=None)` 走第 744 行 `self._otel_tracer.start_span(name=name)`**。OTel 的 `start_span`（非 `start_as_current_span`）默认读取 current context 的 current span 作为 parent（不 `set_current`）。因为 turn 根 span 已由 F1 的 `start_as_current_span` 设为 current，所以 generation 会挂到根 span 下（同 trace）。反之，传 `trace_context` 时走 `_create_remote_parent_span` + `use_span`，强制挂到预分配 trace 并打 `AS_ROOT=True`，**完全绕过 current context**——这就是 Bug A 的直接机制。

**（F4）`trace_context` 支持 `parent_span_id` 显式父子挂载（备用机制，本次不采用）。**

langfuse 官方自身在 `_persist_resume_trace_context` 里使用 `trace_context={"trace_id": ..., "parent_span_id": ...}` 保存 resume 父子关系，印证 `trace_context` 的完整形态。本方案选择 F2/F3 的「context 自然传播」而非显式 `parent_span_id`，因为前者无需跨线程/跨 `asyncio.run` 手动传递 id，改动最小且语义正确。

**（F5）根 observation 对象暴露 `trace_id`（32-hex）与 `id`（16-hex）。**

`LangfuseObservationWrapper.__init__`（`_client/span.py`）：
- `self.trace_id = self._langfuse_client._get_otel_trace_id(otel_span)`（32-hex 字符串）
- `self.id = self._langfuse_client._get_otel_span_id(otel_span)`（16-hex 字符串）

`start_as_current_observation` 返回的上下文管理器 `__enter__` 返回 `LangfuseSpan`（generator 内 `yield span_class(...)`），故 `root_span.trace_id` 可取到根 span 的实际 OTel trace_id。

**（F6）`propagate_attributes` 通过 OTel context 传播 trace 级属性。**

`_propagate_attributes`（`_client/propagation.py`）对 current span 设置 attribute 并经 OTel context 传播。因此根 span 之后的 generation / 工具 span 只要跟随同一 current context，即继承 `session_id` / `user_id` / `tags` / `metadata`。

**（F7）`asyncio.to_thread` 复制 contextvars，`ThreadPoolExecutor.submit` 不复制。**

`asyncio.to_thread` 内部 `copy_context()` + `ctx.run`，会携带 OTel current span；`concurrent.futures.ThreadPoolExecutor.submit` 的 worker 用全新 context 运行，不携带。

### 1.2 当前代码事实（`apps/backend/app/`）

**（C1）`turn_trace`（`core/observability/langfuse_tracing.py`）**

```python
270:  trace_id = new_trace_id()                                  # 预分配 32-hex
275:  root_span_cm = client.start_as_current_observation(
276:      as_type="span", name=f"turn {metadata.turn_id}"         # 无 trace_context → OTel 自动 trace_id
277:  )
278:  attr_cm = propagate_attributes(session_id=..., user_id=..., ...)
...
298:  root_span_cm.__enter__()                                    # 返回值被丢弃
300:  attr_cm.__enter__()
302:  handler = CallbackHandler(
303:      public_key=Settings.LANGFUSE_PUBLIC_KEY,
304:      trace_context={"trace_id": trace_id},                    # 有 trace_context → 预分配 trace_id
305:  )
...
329:  yield TurnTraceResult(callbacks=[handler], trace_id=trace_id)
```

矛盾点：docstring（第 237-243 行）宣称「`CallbackHandler` 的 generation 经 OTel current context 自然传播自动挂到根下」，但第 304 行实际传了 `trace_context`，按 F2 会强制挂预分配 trace —— **docstring 与源码事实相悖，且预分配 `trace_id` 与根 span 的 OTel trace_id 永不相等**。

**（C2）工具 trace recorder（`core/observability/langfuse_tool_trace_recorder.py` 第 143-150 行）**

```python
span_cm = self._client.start_as_current_observation(
    as_type="tool", name=call.tool_name, ...                     # 无 trace_context → 跟随 current
)
```

工具 span 跟随 OTel current context，无 trace_context。

**（C3）`run_agent`（`core/runtime/runner.py` 第 338-361 行）**

```python
metadata = TraceMetadata(task_id=task_id, turn_id=turn_id, agent_id=agent.agent_id)
...
with turn_trace(metadata) as trace_result:
    async for event in agent.workflow.run(
        operations, callbacks=trace_result.callbacks,
        langfuse_trace_id=trace_result.trace_id,                 # 透传，仅供前端跳转
    ):
```

`langfuse_trace_id` 只透传到终态事件 payload（`run_finished`/`run_failed`/`run_cancelled`），供前端展示/跳转 Langfuse UI，**不参与 trace 结构**。

**（C4）子 Agent 复用 `run_agent`（`core/runtime/runner.py` + `core/delegation/child_agent_runner.py`）**

`ChildAgentRunner` 持有 `run_agent` 入口（第 38 行 `self._run_agent = run_agent`），`run_child`（第 78 行）用 `asyncio.run(self._consume_child_events(child_profile))` 驱动，`_consume_child_events`（第 138 行）`async for event in self._run_agent(child_profile)`。**子 Agent 同样进入 `run_agent` 里的 `with turn_trace(metadata)`**（metadata 用子 turn 的 task_id/turn_id/agent_id），因此同样触发 Bug A。

**（C5）delegate 工具执行走 `ThreadPoolExecutor`（`service/tool_execution/tool_execution_service.py`）**

- `run_calls_with_events` 入口按工具 `parallel_mode` 分流；`delegate_task` 声明 `parallel_mode="parallel"`（`tools/tool_handler/delegate_task.py` 第 162 行）。
- 并行组进入 `_run_calls_with_parallel_modes`，第 258-261 行 `with ThreadPoolExecutor(...) as pool:`，第 288-294 行 `pool.submit(self._execute_tool_call, step_id, batch_call, execution_context, loop)` —— **未复制 contextvars**。
- `_execute_tool_call` 第 338 行 `with self._trace_recorder.span(call, step_id) as tool_span:` 创建 delegate 工具 span（无 trace_context）。

**（C6）执行路径 context 传播全景**

```text
主 turn_trace 根 span set_current（主事件循环线程）
  └─ workflow.run → tools_node
       └─ asyncio.to_thread(run_tool_calls)     ← F7：复制 context（含根 span current）
            └─ run_calls_with_events
                 ├─ serial 工具 → _execute_tool_call（同线程，current=根 span）→ 工具 span 挂根 span 下 ✓
                 └─ parallel 工具（delegate_task）
                      └─ _run_calls_with_parallel_modes
                           └─ ThreadPoolExecutor.submit  ← F7：不复制 context，current=空
                                └─ _execute_tool_call（worker 线程，current=空）
                                     └─ delegate 工具 span 成为独立根（新 trace） ✗
                                          └─ set_current（delegate 工具 span 成为 worker 线程 current）
                                               └─ ChildAgentRunner.run_child → asyncio.run（复制 worker 线程 current）
                                                    └─ 子 turn_trace 根 span 挂 delegate 工具 span 下（同新 trace，脱离父） ✗
```

### 1.3 实测证据（`InMemorySpanExporter` 探针，此前已验证）

| 阶段 | OTel trace_id | parent | 说明 |
|---|---|---|---|
| 父 turn 根 span | `2317858…` | None | 真根 |
| 父 handler trace_id | `8d114e…` | — | 预分配，**≠ 根 span** |
| serial 工具 span | `2317858…` | 父根 span | 嵌套正确 |
| **parallel（delegate）工具 span** | `9548676…` | **None** | 脱离父，独立根 |
| parallel 子 turn 根 span | `9548676…` | delegate 工具 span | 挂在独立 trace 下 |
| 子 handler trace_id | `73ee8c…` | — | 预分配，又不同 |

结论与 F1-F7、C1-C6 完全吻合。

---

## 2. 修复方案

### 2.1 修复 A：消除 `turn_trace` 的 trace_id 双轨

**改动文件**：`app/core/observability/langfuse_tracing.py`

**原则**：让 `CallbackHandler` 跟随 OTel current context（F2/F3），使 generation 自动挂到根 span 下；`trace_id` 改为从根 span 读取实际 OTel trace_id（F5），供前端跳转。

**修复前**（第 270、298、302-305、329 行）：

```python
trace_id = new_trace_id()
...
root_span_cm.__enter__()                                    # 丢弃返回值
...
handler = CallbackHandler(
    public_key=Settings.LANGFUSE_PUBLIC_KEY,
    trace_context={"trace_id": trace_id},                   # 双轨根源
)
...
yield TurnTraceResult(callbacks=[handler], trace_id=trace_id)
```

**修复后**：

```python
root_span_cm = client.start_as_current_observation(
    as_type="span", name=f"turn {metadata.turn_id}"
)
attr_cm = propagate_attributes(...)
...
root_span = root_span_cm.__enter__()                        # 捕获根 span（LangfuseSpan）
root_entered = True
attr_cm.__enter__()
attr_entered = True
handler = CallbackHandler(
    public_key=Settings.LANGFUSE_PUBLIC_KEY,                # 去掉 trace_context
)
...
yield TurnTraceResult(
    callbacks=[handler],
    trace_id=getattr(root_span, "trace_id", None),          # 根 span 的实际 OTel trace_id
)
```

**同步改动**：
- 删除 `from app.utils.trace_infra.ids import new_trace_id` 导入（若 `new_trace_id` 不再被本模块其他位置使用）。
- 改写 `turn_trace` docstring 第 237-243 行：去掉「预分配 trace_id 经 CallbackHandler(trace_context=...) 注入」，改为「根 observation 由 `start_as_current_observation(as_type="span")` 建立并成为 OTel current，`CallbackHandler` 不传 `trace_context`、跟随 current context 使 generation 自动挂到该根下；`trace_id` 取自根 observation 的实际 OTel trace_id」。
- 改写 `TurnTraceResult` docstring 第 55 行：把「本 turn 预分配的 Langfuse trace_id」改为「本 turn 根 observation 的实际 Langfuse trace_id」。

**效果**：
- 根 span：OTel 自动 trace（F1），`set_current`。
- generation：`CallbackHandler` 不传 `trace_context`（F2）→ `start_observation(trace_context=None)` 走 OTel `start_span`（F3）→ 以 current 根 span 为 parent，挂同 trace。
- serial 工具 span：跟随 current → 挂根 span 下（同 trace）。
- `session_id`/`user_id`/`tags`/`metadata`：`propagate_attributes` 经 OTel context 传播（F6），generation/工具 span 跟随 current 继承，归属正确。
- `langfuse_trace_id`（前端跳转）：改为根 span 实际 trace_id，指向真实存在的 trace。

### 2.2 修复 B：delegate 并行工具保留 OTel context

**改动文件**：`app/service/tool_execution/tool_execution_service.py`

**原则**：在 `_run_calls_with_parallel_modes` 里用标准库 `contextvars.copy_context()` 捕获当前线程 context（含父 turn 根 span 的 OTel current，见 C6 的 `asyncio.to_thread` 已复制 context），再用 `ctx.run` 包装 `pool.submit`，使 worker 线程在捕获的 context 里执行工具。

**修复前**（第 288-294 行）：

```python
future_by_call[
    pool.submit(
        self._execute_tool_call,
        step_id,
        batch_call,
        execution_context,
        loop,
    )
] = (batch_index, batch_call)
```

**修复后**：

```python
# 文件顶部新增 import contextvars

# _run_calls_with_parallel_modes 内、ThreadPoolExecutor 之前捕获一次：
ctx = contextvars.copy_context()   # 含父 turn 根 observation 的 OTel current context
...
future_by_call[
    pool.submit(
        ctx.run,                   # 让 worker 线程在捕获的 context 里执行
        self._execute_tool_call,
        step_id,
        batch_call,
        execution_context,
        loop,
    )
] = (batch_index, batch_call)
```

**同步改动**：
- `_run_calls_with_parallel_modes` docstring 补充说明「捕获并复制当前线程 contextvars，使并行工具（尤其 delegate_task）的 tool observation 与子 turn 正确嵌套在父 turn trace 下」。
- 该文件顶部新增 `import contextvars`。

**效果**（结合 C6 全景）：
- delegate 工具 span：在 `ctx.run` 里执行，OTel current = 父 turn 根 span → `start_as_current_observation(as_type="tool")` 挂到父根 span 下（同 trace）。
- 子 turn 根 span：`asyncio.run` 复制 worker 线程 current（此时 current = delegate 工具 span）→ 挂 delegate 工具 span 下（同 trace）。
- 子 turn 的 generation/工具：由修复 A 保证挂子 turn 根 span 下。
- 并行语义不变：仍是 `ThreadPoolExecutor` 多 worker 并发，只是共享同一份捕获的 context。

### 2.3 语义决策（需用户确认）

**子 Agent 可观测语义 = 嵌套在父 turn trace 下**（作为 delegate 工具 span 的子树），而非「独立 trace + session/标签关联」。

理由：
1. 复用 langfuse OTel context 自然传播，不重复造轮子（F2/F3/F7 已具备）。
2. 与 `langfuse-future-roadmap.md` P0「LLM generation 与工具 observation 归入同一 turn trace」一致。
3. 因果清晰：子 turn 因 delegate 工具调用产生，嵌套直接表达因果。
4. 同一 task 的父子 turn 仍通过 `session_id=task_id` 在 session 维度聚合（现有能力保留）。

---

## 3. 改动清单

| 文件 | 改动 | 性质 |
|---|---|---|
| `app/core/observability/langfuse_tracing.py` | 修复 A：去预分配 trace_id + 去 CallbackHandler trace_context + 读根 span 实际 trace_id；改写 `turn_trace`/`TurnTraceResult` docstring | 修复 + 文档同步 |
| `app/service/tool_execution/tool_execution_service.py` | 修复 B：`contextvars.copy_context()` + `ctx.run` 包装 `pool.submit`；补 docstring；新增 `import contextvars` | 修复 + 文档同步 |
| `tests/test_langfuse_tracing_trace_id_format.py` | 同步断言：CallbackHandler 不再传 `trace_context`；`result.trace_id` 等于根 span 的 `trace_id`；**stub 必须在 `with turn_trace(...)` 之前设置**（见 4.1 时序陷阱） | 测试同步 |

不新增依赖；`langfuse` 版本不变（4.14.1，已锁定）。

---

## 4. 验证方案

### 4.1 单元测试（`tests/test_langfuse_tracing_trace_id_format.py` 同步）

现有测试第 80-83 行断言：

```python
_, kwargs = langchain_mod.CallbackHandler.call_args
assert kwargs["trace_context"]["trace_id"] == result.trace_id
```

改为：

```python
# 0) 关键时序陷阱：root_cm.__enter__.return_value.trace_id 的 stub 必须放在
#    `with turn_trace(...)` 之前——turn_trace 的 yield 在求值结果时已执行
#    `getattr(root_span, "trace_id", None)`；若在 with 块内才设置，result.trace_id
#    将是 MagicMock，且现有第 74-79 行的 is_trace_id / len==32 断言也会失败。
#    root_cm 的获取路径与现有 test_turn_trace_exit_failure_does_not_escape（第 110 行）一致。
root_cm = stubs["langfuse"].Langfuse.return_value.start_as_current_observation.return_value
root_cm.__enter__.return_value.trace_id = "0123456789abcdef0123456789abcdef"

with patch.dict("sys.modules", stubs), langfuse_tracing.turn_trace(metadata) as result:
    # 1) CallbackHandler 不再传 trace_context（修复 A 核心）
    _, kwargs = stubs["langfuse.langchain"].CallbackHandler.call_args
    assert "trace_context" not in kwargs or kwargs["trace_context"] is None

    # 2) result.trace_id 等于根 observation 的实际 trace_id（提前 stub 保证）
    assert result.trace_id == "0123456789abcdef0123456789abcdef"
```

### 4.2 探针验证（临时，`apps/backend/temp/`）

复现 1.3 的 `InMemorySpanExporter` 探针，验证修复后：
- 父 turn 根 span、主 LLM generation、serial 工具 span 落在同一 trace；
- delegate 工具 span 挂父根 span 下（同 trace，parent 非 None）；
- 子 turn 根 span 挂 delegate 工具 span 下（同 trace）；
- 子 LLM generation 挂子 turn 根 span 下（同 trace）。

> 预期结构说明：修复后探针中，根 span 与 LLM generation 之间会额外出现一层 LangGraph graph chain span（`CallbackHandler.on_chain_start` 触发，见第 0 节注），属预期行为，验证时按「同一 trace + 正确的父子链路」判定，不要求严格等于理想结构图的层数。

### 4.3 边界验证

- Langfuse 未启用：`turn_trace` 走第 259-261 行零开销分支，`TurnTraceResult([], None)` 不变。
- Langfuse 初始化异常：降级 `TurnTraceResult([], None)`，不中断 turn（第 284-293、306-325 行路径不变）。
- `root_span` 无 `trace_id` 属性（防御）：`getattr(root_span, "trace_id", None)` 返回 None，前端跳转降级为无 trace_id，与现状兼容。
- 并行组取消：`ctx.run` 不改变 `_should_cancel` / `_build_result_with_cancel_placeholders` 逻辑，取消占位收口不变。

---

## 5. 风险与边界

| 风险 | 说明 | 缓解 |
|---|---|---|
| F3 依赖 OTel current span 有效性 | `start_observation(trace_context=None)` 走 `start_span` 读 current span 为 parent，根 span 必须已 `set_current`，否则 generation 仍会独立 | 根 span 由 `start_as_current_span` 创建即 set_current（F1）；用探针 4.2 验证 |
| `propagate_attributes` 在无 trace_context 时的 session/user 归属 | 机制上应继承（F6），但需实证 | 探针 4.2 断言 generation/工具 span 的 `session_id`/`user_id` 与根 span 一致 |
| `contextvars.copy_context()` 能否携带 OTel current span | OTel SDK 用 contextvars 存 current span，机制上成立 | 探针 4.2 断言 delegate 工具 span parent = 父根 span |
| `getattr(root_span, "trace_id", None)` 类型 | `LangfuseObservationWrapper.trace_id` 为 32-hex str（F5），与旧 `new_trace_id()` 同格式 | 前端跳转链接格式不变 |
| `ctx.run` 包装不影响并行吞吐 | 每个 worker 仍在独立线程，仅共享捕获的只读 context | 无锁、无共享可变状态，并发语义不变 |

---

## 6. 第零铁律符合性

- **复用而非自研**：修复 A 复用 langfuse 官方的「OTel current context 自然传播」机制（`CallbackHandler` 不传 `trace_context` 时，LLM generation 经 `start_observation(trace_context=None)` 走 OTel `start_span` 以 current 根 span 为 parent）；修复 B 复用 Python 标准库 `contextvars`。二者都是生态成熟能力，不手写 trace 上下文传递轮子。
- **改动聚焦、结构不膨胀**：仅 2 个业务文件 + 1 个测试，均为「修现有职责」而非「新增职责」；`core/observability` 仍是 langfuse 耦合唯一收口，`service/tool_execution` 仍只做执行编排。
- **长期可维护**：消除「预分配 id 与 OTel 自动 id 双轨」这一隐性不一致，使 trace_id 单一事实来源（根 observation 的实际 id），后续任何新增 observation 类型都天然跟随同一 current context，不再需要手动对齐 id。
- **不引入新依赖**：`contextvars` 为标准库，`langfuse` 版本锁定不变。

---

## 7. 待确认项

1. 子 Agent 语义定为「嵌套在父 turn trace 下」（2.3）是否接受？（备选：子 turn 独立 trace + `parent_trace_id`/`parent_span_id` 关联，需额外跨线程传 id，改动更大）
2. 是否需要在 `langfuse-future-roadmap.md` 同步标注本方案的落地状态？

---

## 附：关键源码引用索引

| 引用 | 位置 |
|---|---|
| F1/F2/F3 | `.venv/Lib/site-packages/langfuse/_client/client.py`（`_start_as_current_otel_span_with_processed_media`、`start_observation` 第 712-761 行）、`langchain/CallbackHandler.py`（`__on_llm_action` 第 1232-1243 行、`_get_parent_observation` 第 680-694 行） |
| F4 | `langchain/CallbackHandler.py`（`_persist_resume_trace_context`） |
| F5 | `.venv/Lib/site-packages/langfuse/_client/span.py`（`LangfuseObservationWrapper.__init__`） |
| F6 | `.venv/Lib/site-packages/langfuse/_client/propagation.py` |
| C1 | `apps/backend/app/core/observability/langfuse_tracing.py:270-329` |
| C2 | `apps/backend/app/core/observability/langfuse_tool_trace_recorder.py:143-150` |
| C3 | `apps/backend/app/core/runtime/runner.py:338-361` |
| C4 | `apps/backend/app/core/delegation/child_agent_runner.py:38-138` |
| C5 | `apps/backend/app/service/tool_execution/tool_execution_service.py:258-294,338` |

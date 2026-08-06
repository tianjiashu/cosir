# 计划方案：客户端实时显示终端运行输出

> 状态：方案设计（待实现，已据独立审查 Agent 四轮意见修订第 5 版）
> 关联需求：用户希望 Agent 执行的终端命令运行过程实时显示在客户端 UI
> 决策：采用「只读 xterm.js 输出查看器」方案（非可交互 PTY），复用现有 SSE 增量事件链，零 Tauri Rust / WebSocket 改动
> 修订说明：
> - v1 不通过：误写 `asyncio.to_thread`、虚构 `is_persisted`。
> - v2 不通过：引用不存在的 `ToolOutputBudget.MAX_CHARS`、预算未传子进程、成功 return 前未 drain、同步性误判、注入路径不清。
> - v3 不通过（阻塞）：误称 `_tools_node` 经 `run_in_executor` 跑 executor 线程（无此配置）；漏把 tools 节点移出事件循环这一硬前提；`_execute_handler` 无 tool name 致路由落不了地。
> - v4 不通过（阻塞）：B7 用 `add_node(..., run_in_executor=True)` 是 LangGraph **不存在的 API**，会 `TypeError` 致 graph 无法编译；B6 线程模型建立在该失效前提上。
> - 本版（v5）将 B7 改为 LangGraph 官方支持的 `async def` 节点 + `asyncio.to_thread` 包装，并显式处理 `get_stream_writer()` contextvar 跨线程失效问题、列出强制验证项；落实 v4 非阻塞 S1-S4。见文末对照。
> - v6 不通过（阻塞）：B7 误判 `asyncio.to_thread` 派生线程 contextvar 不跟随、且 `write_event` 修复依赖 `_runtime_config()` 自相矛盾；改为闭包捕获 writer。事实基线准确。
> - v7（当前）：清除 v6 残留的 `RuntimeConfig.stream_writer` 死引用；B5 补齐「轮询内实时 drain（实时流核心落点）」与「`on_output_chunks` 在 executor 线程触发」的线程归属澄清；落实跨行脱敏/反向压力两路/父进程侧取消队列/实时性质断言等建议。见文末对照。
> - v8（终版，审查通过）：第八轮复审结论「通过，可进入实现」。据复审 5 条非阻塞建议补正：取消路径 drain 落点、_drain_output_queue 须 @staticmethod、line 330 先取 result 再 drain、S5 改直接访问 `tool.display.expand_layout`（不跨层引用 `_display_by_name`）、跨行脱敏措辞修正（redact_terminal_output 是整块正则、子进程逐行调用致拆段）。

---

## 一、目标与决策

### 1.1 目标
让 `execute_terminal` 工具执行期间，客户端能在对应 `TerminalCallCard` 中实时滚动展示命令的标准输出 / 错误输出，命令结束后再以终态完整输出呈现（支持复制、截断提示）。

### 1.2 关键决策（已确认）
- **只读输出查看器，非 PTY 双向交互**：只用 xterm.js 渲染能力做「单向只读流」，不接管输入、不创建 PTY，不触及 Tauri Rust / WebSocket。
- **复用现有 SSE 增量事件链**：后端已有 `FILE_CHANGE_UPDATED`「运行中增量广播、不持久化」范式（`_publish_file_change_updated`）。新增同类事件 `TOOL_OUTPUT_DELTA` 驱动前端，沿用同一广播管线与异常隔离策略。
- **前端节流靠既有 rAF 攒批**：`useSSE` 已把同帧事件合并为一次 `appendEvents`，后端按行切片发送即可，无需前端再加节流层。
- **执行模型核心是跨进程 IPC + 必须移出事件循环**：`execute_terminal` 的 `execution_mode="process"`，经 `ToolExecutor._execute_in_process` → `multiprocessing.Process` 子进程执行。但 **tools 节点当前在事件循环线程同步执行**（事实，见 §2.1），`execute_terminal` 阻塞最长 120s，**会卡死 SSE 事件循环、使实时流退化为命令结束后一次性刷出**。本方案必须把 `_tools_node` 改为 `async def` + 内部 `asyncio.to_thread` 包装同步工具执行段（LangGraph 官方支持路径），这是实时流成立的**硬前提**。

### 1.3 范围边界（不做）
- 不创建 PTY、不接管终端输入、不支持客户端向子进程写 stdin。
- 不改动 Tauri Rust `backend/`、不新增 WebSocket 通道。
- 不做跨 turn 的输出回放（增量事件不持久化，仅驱动实时流；历史 turn 仅展示 `TOOL_CALL_FINISHED` 的终态 `content`）。

---

## 二、代码事实基线（已逐条抽验真实源码）

### 2.1 后端现状（已查证）
- `core/workflows/react/workflow.py`
  - `ReactLikeWorkflow._build_graph`（line 63-82）：`builder.add_node("tools", _tools_node)`（line 78），**无 `run_in_executor`**（`langgraph==1.2.0` 的 `add_node` 仅支持 `defer/metadata/input_schema/retry_policy/cache_policy/error_handler/destinations/timeout` 等，**无 run_in_executor**——已 web 核验官方 `state.py` 与 `reference.langchain.com`）；`builder.compile(checkpointer=checkpointer)`（line 82）。
  - `ReactLikeWorkflow.run`（line 84-245）：`graph.astream(..., stream_mode=["custom"])` 驱动（line 178）。LangGraph 对**同步节点**在 `astream`(async) 下默认于**事件循环线程内同步执行**（会阻塞）；对 **`async def` 节点**则在事件循环协程上执行，节点内 `await asyncio.to_thread(...)` 可把同步段移出事件循环线程。→ 本方案必须让 `_tools_node` 成为 `async def` 并用 `to_thread` 包装同步执行段（见 B7）。
- `core/workflows/react/nodes.py`
  - `_tools_node`（line 501）是同步 `def`，内部经 `operations.run_tool_calls(..., write_event=write_event)`（line 593-598）执行工具；执行 `execute_terminal` 时会同步阻塞最长 120s（`execute_terminal.py:64` timeout）。
  - `write_event`（line 42-44）用 `get_stream_writer()`（LangGraph 把 custom stream writer 存入 **contextvar**，仅在节点运行线程/协程上下文内可取出）。`model` 节点的 `MODEL_OUTPUT_DELTA` 在节点内 `model.astream()` 就地翻译即为此机制。
  - **关键约束（contextvar 机制，已核实）**：Python 3.9+ 的 `asyncio.to_thread` 实现为 `contextvars.copy_context().run(func, ...)`，**会把调用协程当前的 contextvars 复制到 executor 线程**。因此 `get_stream_writer()` 与 `get_config()` 的 contextvar 在 `to_thread` 线程**实际会跟随**（值可读），不像早期版本那样"不跟随"。但 `get_stream_writer()` 返回的 **writer 对象本身** 是否能在裸线程安全写入 custom stream，取决于 LangGraph 内部对 writer 的实现（可能是 asyncio Queue / 协程绑定），属于与 LangGraph 内部耦合的脆弱点。为规避该耦合，本方案不依赖 `write_event` 内部的 contextvar 重新读取，而是用**闭包捕获 writer**（见 B7 修复）。
- `core/runtime/runner.py`
  - `AgentRuntime.run_turn`（line 193）是 **async 入口**，即 SSE producer：`emit`→`_publish_runtime_event`→`yield`（line 218-272）。在调用 `workflow.run`（line 357）前处于事件循环线程，可在此捕获 `loop = asyncio.get_running_loop()` 并注入 `ToolExecutionService`（单例）——这是 B6 loop 捕获的确定落点。`run_turn` 内部 `_save_runtime_event` 已用 `asyncio.to_thread`（line 239），证明 to_thread 模式在本项目已有使用。
- `service/agent_runtime_event/runtime_event_bus.py`
  - `RuntimeEventBus.publish`（line 145-182）用 `threading.RLock` 保护 + `asyncio.Queue.put_nowait`（line 171）。**该 publish 线程安全**，可从 executor 线程直接调用。本方案仍优先用 `loop.call_soon_threadsafe` 把 publish 调度回事件循环线程（更稳妥）。
- `tools/tool_handler/execute_terminal.py`
  - `to_definition()` 声明 `execution_mode="process"`、`expand_layout="terminal"`；`execute()` 调 `create_backend("local").execute(...)` 同步执行，输出仅在 `tool_success(content=redact_terminal_output(result.output))` 一次性回传；handler 签名为 `execute(command, timeout, workdir, execution_context)`（无 `output_queue`）。
- `tools/tool_execute/tool_executor.py`
  - `ToolExecutor._execute_in_process`：起 `multiprocessing.Process`，入口 `_execute_handler`（line 493-572，签名 `(handler, arguments, result_queue, log_queue, execution_context)`，**无 tool name**）；`arguments` dict + `execution_context` 经 `handler(**arguments, execution_context=execution_context)`（line 550）注入。
  - `Process` args（line 157-161）不含 tool name。
  - `_execute_handler` 在 `finally`（line 561-571）对 `result_queue.close()+join_thread()`，确保 feeder 刷完。
  - `_wait_for_result`（line 324-344）：0.05s 步长 `result_queue.get(timeout=0.05)` 轮询；**成功时直接 `return result_queue.get(...)`（line 330）**，不进 `queue.Empty` 分支也不进 `process.is_alive()==False` 分支；超时强杀在 line 339 `if process.is_alive(): raise TimeoutError()`。→ 父进程 drain `output_queue` 必须在 line 330 的 return 之前插入（见 B5）。
- `tools/tool_handler/terminal/local_backend.py`
  - `LocalExecutionBackend.execute` 用 `subprocess.Popen` + `_OutputCollector`；`_OutputCollector.run()` 在**单 reader 线程逐行读**（line 88-89），做 **head+tail 有界截断**（`MAX_OUTPUT_CHARS=200_000` / `HEAD_CHARS=100_000` / `TAIL_CHARS=100_000`，line 29-31），`truncated` 标记超限置真。天然增量采集点。
- `service/tool_execution/tool_execution_service.py`
  - `run_calls_with_events` 是**同步 `def`**（line 94 附近），经 `_tools_node` 由 `asyncio.to_thread` 包装后运行在 **executor 线程**（不在事件循环线程）。故 `asyncio.get_running_loop()` 此线程会抛 `RuntimeError`——必须靠 `run_turn` 异步入口捕获 loop 并注入。
  - 完成后 `_publish_file_change_updated`（经 `self._event_bus.publish`，**不调用 `save_event` = 不持久化**，整体 try/except + `log.exception` 不影响主流程）。范式参照。
- `utils/trace_infra/redaction.py`：`redact_terminal_output(text)` 已存在，脱敏（凭据→`[REDACTED]`，**改变字符长度**）。增量片段与终态均可直接复用。
- `tools/guard/tool_output_budget.py`：无 `MAX_CHARS` 类级常量；预算上限真实来源 `Settings.MAX_TOOL_OUTPUT_CHARS`（默认 20000，可配置，settings.py:47,246；tool_system.py:98 用它构造）。`apply()` 先 `redact_terminal_output` 后截断（line 61,79），截断逻辑为保留前缀 + 附 hint（非简单 `[:max]`）。运行态 delta 预算须与终态对齐。
- `models/event/runtime_event.py`：`RuntimeEvent` **无 `is_persisted` 字段**（line 43-51）；`__post_init__` 中 `EVENT_PAYLOAD_MODELS[self.event_type]` 未登记则抛 `KeyError`（line 72）。
- `models/payload/runtime_event_payload.py`：`RuntimeEventPayload(BaseModel, ConfigDict(extra="forbid"))` 基类；新 payload 必须继承。
- `models/payload/registry/runtime_event_payload_registry.py`：`EVENT_PAYLOAD_MODELS: Mapping[EventType, type]` 登记；`FILE_CHANGE_UPDATED` 已登记。
- `tools/tool_execute/tool_scheduler.py`：`ToolScheduler.execute(call, execution_context, allowed_tool_names, should_cancel) -> ToolObservation` 为**同步方法**（line 104）；非 filesystem 工具在 line 303-309 调 `self._executor.execute(...)`。需扩展签名注入 `on_output_chunks`。

### 2.2 前端现状（已查证）
- `hooks/useSSE.ts`：`onEvent` 推入 `pendingEventsRef`，`scheduleFlush` 用 `requestAnimationFrame` 每帧 flush 一次 `appendEvents(batch)`。高频 delta 已自动合并，无需额外节流。
- `stores/eventStore.ts`：`appendEvents` 把 delta 喂给 projector；`processedEventIds` 已按 `event_id` 幂等去重（SSE 单通道有序，**无需 delta 再设计 `seq` 字段**）。
- `services/timeline/projector.ts`：`projectTool` 按 `tool_call_started` / `tool_call_finished` 的 `tool_call_id` 合并同一条目；`TimelineToolItem`（line 35-68）无 `output` 字段。新增 `tool_output_delta` 分支按 `tool_call_id` 累积 `output`。
- `components/chat/TerminalCallCard.tsx`：当前 `hasOutput = status !== "running" && result != null`；运行中 `hasOutput=false`，不渲染输出块。无 xterm 组件。
- `components/layout/TurnTimeline.tsx`：已按 `display.expandLayout === "terminal"` 分发 `TerminalCallCard`，无需改逻辑，仅补 `output` prop 透传。

### 2.3 依赖现状
- `@xterm/xterm` 与 `@xterm/addon-fit` 当前未安装，需新增（锁版本）。

---

## 三、实现步骤

### 后端（apps/backend）

**B1. 新增 payload 值对象**
- `models/payload/tool_output_delta_payload.py`：`ToolOutputDeltaPayload(RuntimeEventPayload)`，字段 `task_id` / `turn_id` / `tool_call_id` / `text`（本片段新增输出）。单一职责：承载一次终端输出增量。必须继承 `RuntimeEventPayload` 满足 `extra="forbid"` 基类契约。

**B2. 事件类型枚举**
- `models/enums/event_type.py`：新增 `TOOL_OUTPUT_DELTA = "tool_output_delta"`（与 `FILE_CHANGE_UPDATED` 同级，属「运行中增量、不持久化」类别）。

**B3. payload 注册**
- `models/payload/registry/runtime_event_payload_registry.py`：在 `EVENT_PAYLOAD_MODELS` 登记 `EventType.TOOL_OUTPUT_DELTA: ToolOutputDeltaPayload`（否则 `RuntimeEvent` 构造抛 `KeyError`）。

**B4. 子进程侧输出采集（含预算对齐）**
- `tools/tool_handler/terminal/local_backend.py`
  - `LocalExecutionBackend.execute` 新增可选参数 `output_queue: multiprocessing.Queue | None = None` 与 `max_output_chars: int`（**不设硬编码默认，由父进程显式传入 `Settings.MAX_TOOL_OUTPUT_CHARS`**，注释说明与 Settings 一致；与既有 `MAX_OUTPUT_CHARS=200_000` head+tail 截断是两套独立预算——前者为实时增量上限、后者为终态回收上限，注释明确分工）。
  - `_OutputCollector` 在 reader 线程每读完一行（line 88-89）后，若 `output_queue is not None`：先 `redact_terminal_output(line)` 得脱敏行，累计 `sent_chars += len(redacted_line)`；当且仅当未超 `max_output_chars` 时 `output_queue.put(redacted_line)`，否则停止 put（以**脱敏后长度**为基准，与终态 `apply` 对齐）。
  - 子进程**不做**额外脱敏（此处已统一 put 前脱敏，父进程不再重复）。注：`redact_terminal_output` 本身是对**整块文本**应用正则（非逐行实现），但本方案在 reader 线程对**每行**调用它（`redact_terminal_output(line)`），因此**跨行凭据会被拆成两段各自处理**——这是子进程逐行采集的已知边界，§5.1 已约定接受该边界。
  - 维持既有 `output` 整段累积与 head+tail 截断逻辑不变（终态回传仍用原始 `output`，由 `ToolOutputBudget.apply` 脱敏+预算）。
  - 反向压力（两路同时生效，不可二选一）：`output_queue` 设 `maxsize`（有界，如 2000 条）防瞬时积压阻塞 reader→子进程 stdout 管道；同时 `sent_chars` 预算兜底防超预算持续 put。两者叠加确保大输出不卡死子进程。
- `tools/tool_handler/execute_terminal.py`
  - `execute()` 新增可选 `output_queue` / `max_output_chars` 参数，透传给 `create_backend("local").execute(..., output_queue=output_queue, max_output_chars=max_output_chars)`。handler 签名不新增 `output_queue`（仅在内部传给 backend），对 `arguments` 透明。
  - 保持返回 `tool_success(content=redact_terminal_output(result.output))` 不变（幂等）。

**B5. 跨进程 drain + 父进程侧回调（核心 IPC）**
- `tools/tool_execute/tool_executor.py`
  - `_execute_handler` 新增参数 `tool_name: str`，由 `Process` args 注入；仅在 `tool_name == "execute_terminal"` 时把 `output_queue` / `max_output_chars` 作为**额外关键字**传给 `handler(**arguments, execution_context=..., output_queue=output_queue, max_output_chars=max_output_chars)`（line 550）。其他工具 `output_queue=None`，行为与现状一致。→ 明确「按 tool name 显式分支」唯一路径。
  - `_execute_in_process` 新增：先按 `tool.display.expand_layout == "terminal"` 判断（`tool` 是 `ToolDefinition`，自带 `display: ToolDisplayHints`，`execute_terminal` 的 `display.expand_layout=="terminal"` 可直接访问，**不跨层引用 service 层的 `_display_by_name`**），仅对终端类工具创建 `output_queue = multiprocessing.Queue(maxsize=2000)` 并作为额外关键字参数传给 `_execute_handler`（与 `result_queue` / `log_queue` 并列）；非终端工具 `output_queue=None`，行为与现状一致、零侵入。父进程侧创建后调 `output_queue.cancel_join_thread()`（参照 `result_queue` line 152-153），避免父进程退出被子进程 feeder 残留线程阻塞。
  - 新增辅助 `_drain_output_queue`：**定义为 `@staticmethod`（或模块级函数）**，因调用方 `_wait_for_result` 是 `@staticmethod`（line 296），static 方法内无法访问实例方法，否则 `NameError`。非阻塞 `get_nowait` 直到空，收集成 `chunks`，若非空调 `on_output_chunks`。**两种 drain 必须同时成立**（不可二选一）：
    - **实时 drain（核心，保证实时流不被退化为一次性刷出）**：`_wait_for_result` 的既有 0.05s 轮询循环内，每次 `result_queue.get` 抛出 `queue.Empty`（line 331-333）的 `continue` **之前**调用一次 `_drain_output_queue`。命令执行 120s 期间，子进程持续 `put` 片段，父进程每次轮询都顺带把已到达的片段经 `on_output_chunks` 回流 → SSE 在命令运行中持续 flush。这是本方案「实时」目标成立的关键落点。
    - **终态 drain（兜底，防尾部丢失）**：四处（同 `@staticmethod` 约束）需各调用一次 `_drain_output_queue`：
      1. 成功 return 前（line 330）：实现时**先 `result = result_queue.get(...)` 取到结果，再 drain output_queue，最后 `return result`**（不能在单条 `return` 语句前直接插入 drain）；
      2. `process.is_alive()==False` 分支（line 336）；
      3. 超时强杀 `raise TimeoutError()` 前（line 339）；
      4. **取消路径**（line 326-327 `if should_cancel(): raise _ToolExecutionCancelled()`）：在 raise 前补一次 `_drain_output_queue`（或在 `_execute_in_process` 的 `except _ToolExecutionCancelled` 分支 line 195-213 内 drain，二选一，实现时明确），确保 §5.1「取消路径 drain」验收断言成立。
      共「实时 1 + 终态 4」五处调用，无复制歧义（Rule of Three 适用于终态结构相似处）。
  - `output_queue` 同样在子进程 `finally` 中 `close()+join_thread()`（参照 line 570-571），确保 feeder 刷完。
  - **线程归属（与 B6 对齐）**：`_wait_for_result` 经 B7 的 `asyncio.to_thread` 运行在 **executor 线程**，故 `_drain_output_queue` 内 `on_output_chunks` 也在 executor 线程被调用。B6 的 `loop.call_soon_threadsafe` 正是依赖此前提把 publish 调度回事件循环线程——两者线程模型必须自洽，不得让 `on_output_chunks` 在事件循环线程直接调用 publish。
  - 透传链：`on_output_chunks` 同步覆盖 `ToolExecutor.execute` / `_execute_in_process` / `_wait_for_result` 三处签名（默认 `None`，其他工具不受影响）。
- `tools/tool_execute/tool_scheduler.py`
  - `ToolScheduler.execute` 签名新增可选 `on_output_chunks: Callable[[list[str]], None] | None = None`，在两处 `self._executor.execute(...)` 调用（line 272-278、line 302-309）透传；scheduler 仅做调度、不加业务。

**B6. 编排层广播（executor 线程 → 事件循环）**
- `service/tool_execution/tool_execution_service.py`
  - **loop 捕获（落点明确）**：`loop` 必须在 **async 入口 `AgentRuntime.run_turn`（`runner.py:193`）** 捕获——在 `run_turn` 调用 `workflow.run`（line 357）之前 `loop = asyncio.get_running_loop()`。注意 `ToolExecutionService` 在 `RuntimeOperations.__init__` 中**每次新建**（非进程级单例），故 loop 经 `execution_context`/方法参数注入到本次 `run_calls_with_events`（executor 线程）即可，不新增跨对象长生命周期注入通道。
  - `on_output_chunks` 回调（运行在 executor 线程的 drain 路径）：对每个 `chunk` 不做脱敏（子进程已脱敏），`loop.call_soon_threadsafe(self._publish_tool_output_delta, execution_context, call.call_id, chunk)`。回调整体包 try/except + `log.exception`（覆盖 `call_soon_threadsafe` 在 loop 已关闭时抛 `RuntimeError` 等），**不影响主流程**。
  - 新增 `_publish_tool_output_delta`：复用 `_publish_file_change_updated` 异常隔离范式（整体 try/except + `log.exception` warning，不影响主流程），经 `self._event_bus.publish` 发送 `TOOL_OUTPUT_DELTA`。**仅 `bus.publish`、不调 `save_event`** = 不持久化。
  - 仅对 `execute_terminal` 启用：优先按 `ToolDisplayHints.expand_layout == "terminal"` 判断是否注入 `on_output_chunks`（避免硬编码工具名漂移；与 B5 的 `tool_name` 子进程分支分工：一个在编排层、一个在子进程入口，各司其职，补注释说明）。

**B7. 把 tools 节点移出事件循环（实时流硬前提，LangGraph 官方支持路径）**
- `core/workflows/react/nodes.py`
  - `_tools_node` 改为 `async def`（去掉同步 def）。在节点协程内（事件循环上下文）先 `writer = get_stream_writer()` 捕获 custom stream writer，再定义一个**不依赖 contextvar 的闭包** `_node_write_event(etype, payload): writer({"event_type": str(etype), "payload": payload})`，把闭包作为 `write_event` 传入同步执行段。**不改动模块级 `write_event`（line 42-44），避免让 `write_event` 在 to_thread 线程里重新调 `get_stream_writer()` / `_runtime_config()`（后者依赖 `get_config()` contextvar，构成自相矛盾的修复）**。
  - 将同步执行段改为 `await asyncio.to_thread(operations.run_tool_calls, task.task_id, approved_calls, step_id, write_event=_node_write_event)`（替代 line 593-598）。`asyncio.to_thread` 把同步工具执行（含 `execute_terminal` 阻塞 120s）移到 executor 线程，**事件循环线程不再被阻塞**，SSE 在工具运行期间可实时 flush（`yield` 不再被工具阻塞）。
  - 为何闭包捕获而非 `RuntimeConfig.stream_writer`：`asyncio.to_thread` 会 `copy_context().run` 复制 contextvars，故 `get_config()` / `get_stream_writer()` 在 to_thread 线程**实际可读**（值跟随）；但 `get_stream_writer()` 返回的 **writer 对象**是否能在裸线程安全写 custom stream 取决于 LangGraph 内部实现（与内部耦合）。用节点协程内捕获的 `writer` 闭包，使 `write_event` **完全不重新读取任何 contextvar**，彻底规避该耦合，是当前最稳写法。`RuntimeConfig` 不加 `stream_writer` 字段（避免无效字段）。
  - 影响评估：`_tools_node` 改为 async + to_thread 是 LangGraph 官方推荐异步节点写法（**非虚构 API**）；闭包捕获 writer 是「不重新读 contextvar」的防御性写法，规避 LangGraph writer 内部实现耦合。属本方案最关键的前提改动，缺之则实时流完全不成立。
  - **强制实现期验证项**（阻塞，实现时先验证再继续）：
    1. 闭包 capture 的 `writer` 在 `to_thread` 派生线程内 `write_event` 仍能正确写入 custom stream，`astream(stream_mode=["custom"])` 外层 `run_turn` 能 `yield` 收到（验证 writer 对象跨线程写可用）；同时分别确认 `get_config()` / `get_stream_writer()` 在 to_thread 线程是否可用（记录两条路径实际行为，供未来耦合判断）。
    2. checkpoint / `interrupt()`（审批）在 async 节点 + to_thread 下的行为不变（审批恢复后仍能继续；`interrupt` 发生在 `to_thread` 之外、节点协程内，不受影响）。
    3. 事件流时序：`TOOL_CALL_STARTED` → `TOOL_OUTPUT_DELTA`(多次) → `TOOL_CALL_FINISHED` 在 tool 运行期间按序产出，不被 120s 阻塞。
  - 若验证 1 失败（闭包 writer 跨线程不可用）：后备路径 = 将 `write_event` 改为经 `RuntimeEventBus.publish`（事件总线，已线程安全，见 `runtime_event_bus.py:163-171`）发出，`run_turn` 在 `astream` 外层监听 bus 并 yield（更大重构，作为后备）。

### 前端（apps/desktop）

**F1. 新增依赖**
- `package.json` 增加 `@xterm/xterm@^5.5.0` 与 `@xterm/addon-fit@^0.10.0`（锁版本于包管理锁文件）。引入 `@xterm/xterm/css/xterm.css`。

**F2. 新增只读输出查看器组件**
- `components/chat/TerminalViewer.tsx`（新建）：用 xterm.js `Terminal` 渲染只读输出流。
  - props：`output: string`（累积全文）、`streaming: boolean`、`className?`。`disableStdin: true`、`convertEol: true`、主题走现有 CSS 变量。
  - 初始化：`useEffect` 内 `new Terminal(...)` + `FitAddon`，`open(ref)`；卸载 `dispose`。`fit()` 包 try/catch（jsdom 无真实尺寸不抛）。
  - 增量写入：`useEffect([output])` 计算 `newText = output.slice(prevLenRef.current)`，`term.write(newText)`，`fit.fit()`，`term.scrollToBottom()`。
  - 终态策略（与 F3 配合）：`streaming=false` 后由 `<pre>` 接管终态输出，`TerminalViewer` 在 `streaming` 变 false 时**卸载**（停止接收 `output` 或忽略），避免 `projector` 清空 `output` 触发 xterm 空 prop 重渲染清屏。明确：`TerminalCallCard` 在 `status==="running"` 时挂 `TerminalViewer`，`status` 进入终态后立即切回 `<pre>`（不依赖 `output` 是否为空）。

**F3. projector 累积输出**
- `services/timeline/projector.ts`
  - `TimelineToolItem` 接口新增 `output?: string`。
  - `projectTool` 新增 `else if (event.event_type === "tool_output_delta")` 分支：按 `payload.tool_call_id` 找到既有条目，`output: (prev.output ?? "") + payload.text`；未找到对应 started 条目时丢弃（不新建孤立条目）。**无 `seq` 字段**（`processedEventIds` 已幂等 + SSE 有序）。
  - 终态 `tool_call_finished` 分支：merge 时**把条目 `output` 清空/置空**（运行态专用，终态由 `<pre>` 展示 `result`），避免 xterm 与 `<pre>` 双渲染或脏状态。明确策略：终态 `output = undefined`，`result` 保留供现有 `<pre>` 路径。

**F4. TerminalCallCard 接入**
- `components/chat/TerminalCallCard.tsx`
  - props 新增 `output?: string`（来自 `tool.item.output`，由 TurnTimeline 透传）。
  - `hasOutput` 改为：`status === "running" ? Boolean(output) : (result != null)`。
  - 运行中且 `hasOutput`：渲染 `<TerminalViewer output={output} streaming />`（替代空态）。
  - 终态：`status` 非 running 时渲染现有 `<pre>` 输出块（用 `result`），**不挂 `TerminalViewer`**（见 F2 终态策略）。
  - 自动展开交互：仅在「running 且首次收到 output」且用户尚未手动折叠时 `setIsOpen(true)`；若用户已手动折叠不再强制展开（状态标志区分，避免覆盖用户意图的回归）。

**F5. TurnTimeline 透传**
- `components/layout/TurnTimeline.tsx`：在 `TerminalCallCard` 调用处新增 `output={tool.item.output}` 透传（已按 terminal 分发，仅补 prop）。

### 共享协议（apps/shared）
- `events.ts`（由脚本生成，勿手改）：新增后端 `TOOL_OUTPUT_DELTA` payload + 枚举后，运行 `scripts/generate_runtime_event_ts.py` 重新生成（`events.ts` 文件头声明脚本生成，脚本扫描 `EventType` + `EVENT_PAYLOAD_MODELS` 并强制校验，方案 F-shared「运行脚本重新生成」真实可行）。前端无硬编码 switch 遗漏（`projector.ts` 对未知事件返回 null）。

---

## 四、改动文件清单

| 层 | 文件 | 改动 |
|----|------|------|
| backend | `models/payload/tool_output_delta_payload.py` | 新增（继承 RuntimeEventPayload） |
| backend | `models/enums/event_type.py` | 加枚举值 |
| backend | `models/payload/registry/runtime_event_payload_registry.py` | 登记 payload |
| backend | `tools/tool_handler/terminal/local_backend.py` | 新增 `output_queue` + `max_output_chars` 采集点 + 脱敏后计预算 + 有界队列 + 双预算分工注释 |
| backend | `tools/tool_handler/execute_terminal.py` | 透传 `output_queue` / `max_output_chars` |
| backend | `tools/tool_execute/tool_executor.py` | 新增 `output_queue` + `tool_name` 路由 + `_drain_output_queue` 辅助（成功/退出/超时三处）+ `on_output_chunks` 回调 + close/join_thread |
| backend | `tools/tool_execute/tool_scheduler.py` | `execute` 签名加 `on_output_chunks` 并透传 |
| backend | `service/tool_execution/tool_execution_service.py` | 接收注入 loop + `_publish_tool_output_delta` + `call_soon_threadsafe` 调度 + 异常隔离 |
| backend | `core/workflows/react/nodes.py` | **`_tools_node` 改 `async def` + `await asyncio.to_thread(run_tool_calls)`；节点协程内闭包捕获 `get_stream_writer()` 的 writer 传给 `write_event`，不依赖 contextvar** |
| desktop | `package.json` | 加 xterm 依赖（锁版本） |
| desktop | `components/chat/TerminalViewer.tsx` | 新增只读查看器 |
| desktop | `services/timeline/projector.ts` | 累积 output / 新事件分支 / 终态清空 output |
| desktop | `components/chat/TerminalCallCard.tsx` | 接入运行态查看器 + 自动展开交互 + 终态切回 `<pre>` |
| desktop | `components/layout/TurnTimeline.tsx` | 透传 output prop |
| shared | `events.ts`（脚本生成） | 运行 `generate_runtime_event_ts.py` 重新生成 |

---

## 五、验收标准（审查 / 测试闭环）

### 5.1 后端
- `on_output_chunks` 回调单测（同步）：用 fake `output_queue` 预置若干脱敏片段 + `result_queue` 返回观察，断言回调 chunks 拼接 == 子进程侧 put 的脱敏片段；`tool_name` 非 `execute_terminal` 时 `on_output_chunks` 不触发；预算达上限后不再 put；成功/超时/取消三路径均 drain（用假 `on_output_chunks` 断言调用次数）。
- `_publish_tool_output_delta` 单测：mock `event_bus`，断言 `bus.publish` 被调用且 `event_type == TOOL_OUTPUT_DELTA`、无 `is_persisted` 字段（仅 publish 不 save）；回调抛异常不污染主流程且产生 error 级日志（带 task_id/turn_id）。
- 非 terminal 工具（read_file）不产出 `TOOL_OUTPUT_DELTA`（按 expand_layout / tool_name 过滤）。
- `loop.call_soon_threadsafe` 在 loop 已关闭时抛错被回调 try/except 兜住，不冒泡。
- 预算一致：构造超 `Settings.MAX_TOOL_OUTPUT_CHARS` 输出且含凭据，断言 delta 总字符数（脱敏后）不超过该上限；终态展示用 `<pre>` + `apply` 结果，接受超限时运行态头部与终态 `apply` 头部语义可能不完全一致（已列入约束对齐项）。
- 跨行凭据脱敏（边界）：构造含**跨行凭据**（凭据被 `\n` 拆开）的输出，断言子进程侧逐行脱敏不漏脱敏字段（若 `redact_terminal_output` 为逐行正则、跨行凭据会被拆成两段各自处理，则接受该边界并注明，不要求跨行合并脱敏）。
- **B7 回归 + 实时性质断言**：`_tools_node` 改 async + to_thread 后跑 `execute_terminal`（含 120s 超时、中途取消、审批恢复路径）单测，断言：经**闭包捕获的 writer**在 to_thread 线程内仍能写入 custom stream、`astream` 外层能 yield、checkpoint/interrupt 行为不变。额外运行一个 **>5s 的命令**，断言首个 `TOOL_OUTPUT_DELTA` 在命令结束**之前**到达前端（直接证明实时流未被 120s 阻塞），且全程 `TOOL_OUTPUT_DELTA` 多次按序到达（与 B5 轮询内实时 drain 落点呼应）。

### 5.2 前端
- `projector` 单测：`tool_output_delta` 按 `tool_call_id` 累积到同一条目 `output`；重复 event_id 幂等；无对应 started 时丢弃；`tool_call_finished` 后 `output` 被清空（仅 `<pre>` 展示 result）。
- `TerminalViewer` 单测（jsdom + xterm mock）：`output` 增长时 `write` 被调用且增量长度 == 新增片段；卸载 `dispose`；`fit()` 异常不抛；终态（`streaming=false`）不依赖 `output` 清空触发清屏。
- `TerminalCallCard` 单测：运行中 `output` 非空时渲染 xterm 容器；终态（非 running）渲染 `<pre>` 且不挂 xterm；自动展开仅在首次收到 output 且未手动折叠时触发。

### 5.3 规范符合性（独立审查 Agent 逐条核对）
- 单一职责 / 不重复造轮子 / 分层 / 防膨胀 / docstring / 日志 / 目录结构 / 依赖锁定：见第一节决策与第三节各步说明。
- 重点：IPC 链路分工（子进程采集 → 父进程 drain → executor 线程回调 → `call_soon_threadsafe` 回事件循环广播）符合单向依赖；脱敏复用 `redact_terminal_output`、广播复用 `_publish_file_change_updated`、节流复用 rAF 攒批、预算复用 `ToolOutputBudget` + `Settings.MAX_TOOL_OUTPUT_CHARS`；B7 把 tools 节点移出事件循环是实时流的硬前提，已用 LangGraph 官方支持的 `async def` + `asyncio.to_thread` 落地，并修复 `write_event` 跨线程失效；非阻塞 S1-S4 已落实。

---

## 六、风险与缓解
- **实时流硬前提（最致命）**：`_tools_node` 当前在事件循环线程同步执行，`execute_terminal` 阻塞 120s 会卡死 SSE 事件循环，实时流退化为命令结束后一次性刷出。缓解：B7 把 `_tools_node` 改 `async def` + `await asyncio.to_thread(run_tool_calls)`，使其同步段在 executor 线程执行，事件循环不被阻塞，SSE 可实时 flush。配合 B6 的 `call_soon_threadsafe` publish。**实现期强制验证**：经闭包捕获的 writer 在 to_thread 线程内仍能写入 custom stream 并被 `astream` 外层 yield（规避 LangGraph writer 内部实现耦合的防御性写法）；若失败走后备路径（事件总线）。
- **跨进程 IPC**：子进程 `output_queue.put` 回传片段；父进程在 `_wait_for_result` 既有 0.05s 轮询里 drain；子进程退出前 `output_queue.close()+join_thread()`（参照 `result_queue` line 570-571 修复）确保 feeder 刷完。
- **尾部片段丢失**：新增 `_drain_output_queue` 在成功 return 前、进程退出分支、超时强杀前各 drain 一次。
- **高频事件压力**：后端按行切片（非逐字符），前端 rAF 攒批兜底；`output_queue` 有界 maxsize 防积压反压。
- **预算双轨不一致**：运行态增量统一以 `Settings.MAX_TOOL_OUTPUT_CHARS`（脱敏后长度）为上限；终态 `ToolOutputBudget.apply` 同样脱敏后截断。两套预算分工（实时增量 vs 终态回收）已在 `local_backend.execute` 注释明确；超限 + artifact spill 场景运行态头部与终态 `apply` 头部语义可能不完全一致，已接受并写入验收。
- **脱敏遗漏**：子进程 put 前统一 `redact_terminal_output`，父进程不再重复；终态 `apply` 也脱敏，幂等。
- **loop 捕获链**：`loop` 在 async 入口（`run_turn`）捕获注入同步 `run_calls_with_events`，经回调闭包传到 executor 线程 `call_soon_threadsafe`；已验证 `run_calls_with_events` 为同步 def、executor 线程内 `get_running_loop()` 会抛错，注入方案成立。
- **自动展开回归**：仅 running 首次收到 output 且未手动折叠时触发。
- **xterm fit 异常**：`fit()` 包 try/catch，jsdom 下不抛。
- **终态 xterm 清屏**：`streaming=false` 后 `TerminalCallCard` 直接切回 `<pre>`，卸载 `TerminalViewer`，不依赖 `output` 清空触发重渲染（见 F2/F3/F4 协同）。

---

## 七、实施顺序（建议）
1. 后端 B1–B3（payload / 枚举 / 注册）——纯新增，低风险。
2. 后端 B4（子进程采集 `output_queue` + `max_output_chars` 预算对齐 + 脱敏后计数 + 有界队列）。
3. 后端 B5（`tool_executor` 新增 `output_queue` + `tool_name` 路由 + `_drain_output_queue` 三处 + `on_output_chunks` 回调 + close/join_thread；`tool_scheduler.execute` 扩展透传）。
4. 后端 B6（编排层：async 侧 `run_turn` 捕获 loop → 注入 → `call_soon_threadsafe` 广播 + 异常隔离）。
5. 后端 B7（`nodes.py` `_tools_node` 改 `async def` + `await asyncio.to_thread`；**节点协程内闭包捕获 `get_stream_writer()` 的 writer 作为 `write_event` 传入，不改模块级 `write_event`、`RuntimeConfig` 不加字段**）——实时流硬前提。**实现时先跑 §5.1 B7 回归验证**（custom stream 跨线程写 / checkpoint/interrupt 不变 / 增量按序）。
6. 共享协议：新增后端 payload/枚举 → 运行 `scripts/generate_runtime_event_ts.py` 重新生成 `events.ts`。
7. 前端 F2（TerminalViewer）——独立组件，可并行开发。
8. 前端 F3–F5（projector / Card / TurnTimeline）——接线。
9. 依赖 F1 安装 + 锁版本。
10. 启动独立审查 Agent + 测试 Agent，按规范闭环到通过。

---

## 八、审查意见落实对照（v6 → v7）

| 第 6 版关键问题 | 本版修正 |
|---|---|
| P1（阻塞）B5 只列三处终态 drain（line 330/336/339），漏写 §一/§六/§八反复要求的「轮询内实时 drain」；若按 B5 三处实现，实时流退化为结束后一次性刷出（恰为本方案要消除的缺陷） | B5 明确区分两类 drain：**实时 drain（核心）** = `_wait_for_result` 每次 `queue.Empty` 分支（line 331-333）`continue` 前调用 `_drain_output_queue`，保证 120s 执行期间持续回流；**终态 drain（兜底）** = 成功 return 前 / 进程退出 / 超时强杀前三处。改「三处」为「实时 1 + 终态 3」的明确结构，与 §一目标、§六风险、§七步骤自洽 |
| P2（阻塞，澄清）`on_output_chunks` 触发线程归属未与 B6 对齐 | B5 明确：`_wait_for_result` 经 B7 的 `asyncio.to_thread` 运行于 **executor 线程**，故 `on_output_chunks` 在 executor 线程被调用；B6 的 `loop.call_soon_threadsafe` 依赖此前提。线程模型两段自洽 |
| S1（建议）跨行凭据脱敏边界 | §5.1 验收新增「跨行凭据输出」用例：断言子进程侧逐行脱敏不漏脱敏字段；若 `redact_terminal_output` 为逐行正则则接受跨行不合并边界并注明 |
| S2（建议）父进程侧 `output_queue` 清理 | B5 明确父进程创建 `output_queue` 后调 `cancel_join_thread()`（参照 `result_queue` line 152-153），避免父进程退出被残留 feeder 线程阻塞 |
| S3（建议）反向压力二选一措辞 | B4 改为「`maxsize` 有界 + `sent_chars` 预算兜底**同时**生效」的确定表述 |
| S4（建议）实时性质断言 | §5.1 B7 回归新增：运行 >5s 命令，断言首个 `TOOL_OUTPUT_DELTA` 在命令结束**之前**到达前端（直接证明实时流未被阻塞），且全程多次按序到达 |
| S5（建议）父进程侧也按 `expand_layout` 判断 | B5 明确父进程侧 `_execute_in_process` 先按 `tool.display.expand_layout=="terminal"` 仅对终端工具创建并传入 `output_queue`，非终端工具 `None`，与 B6 编排层判断统一、零侵入 |

历史版本（v1-v5）已落实项无回归：v4 移除虚构 `run_in_executor`、v5 改 `async def`+`to_thread`、v6 闭包捕获 writer，在 v7/v8 均无回归。

## 九、第八轮复审收尾建议落实（v8，非阻塞）

| 复审建议 | 本版修正 |
|---|---|
| 取消路径 drain 落点缺失（§5.1 断言取消路径 drain 但 B5 四处未含取消） | B5 终态 drain 改为「实时 1 + 终态 4」，第 4 处为取消路径（line 326-327 raise 前补 drain 或 except 分支内 drain，二选一） |
| `_drain_output_queue` 若为实例方法则 `_wait_for_result`(@staticmethod) 内 NameError | B5 明确 `_drain_output_queue` 定义为 `@staticmethod`（或模块级函数） |
| line 330 单条 return 前无法直接插 drain | B5 注明实现顺序：先取 result、再 drain、再 return |
| S5 误引 service 层 `_display_by_name` 跨层 | B5 改为直接访问 `tool.display.expand_layout`（`ToolDefinition` 自带），不跨层引用 |
| `redact_terminal_output` 是整块正则非逐行 | B4 注明：子进程对**每行**调用它导致跨行凭据拆段，§5.1 接受该边界 |

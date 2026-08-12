# 委派子 Agent 并发执行技术方案

> 本文是 `delegate_task` 已落地后的并发扩展方案。修订原则以项目“第零铁律”为最高准绳：所有设计都优先服务长期稳定迭代，而不是追求最小改动或短期可跑通。

## 一、背景与目标

当前 `delegate_task` 已作为工具接入父 Agent，真实执行由 `core/delegation` 中的运行时 executor 完成。现状下同一父 turn 的工具调用批次整体串行执行，委派策略也以 `max_concurrency=1` 作为第一版保守限制。

并发扩展的目标是：允许父 Agent 在同一轮工具批次中派发多个相互独立的 child Agent 并行执行，例如同时执行代码审查、事实分析和小范围编码任务；同时保持普通工具安全边界、模型 tool call 协议闭合、父子 turn 生命周期可追踪，并为后续并发扩展留下清晰路径。

第一版目标：

- 支持同一父 turn 下多个 `delegate_task` bounded parallel execution。
- 并发上限可配置，默认较小，例如 2。
- 并发额度 acquire 具备数据库原子性，不依赖非原子 `count -> insert`。
- child runner 采用 async-first 设计，避免把 `ThreadPoolExecutor + asyncio.run()` 固化为长期底座。
- `messages_for_model` 按模型原始 tool call 顺序回填。
- 前端运行时事件可体现真实开始/完成顺序。
- 父 turn 取消时能级联取消 active child turn。

第一版非目标：

- 不开放 child 递归委派。
- 不把 `read_file`、`write_file`、`patch`、`delete`、`execute_terminal` 等普通工具直接并发化。
- 不为了短期并发而引入无法取消、无法恢复、难以排查的线程桥接核心路径。
- 不采用 LangGraph-native（`Send` / subgraph）作为并发委派实现，也不把「向 LangGraph 收敛」列为设计依赖或评估项（见 §3.3）。

## 二、当前代码事实

- `ToolDefinition` 已具备并发调度声明字段：`parallel_mode: Literal["serial","parallel"]="serial"` 与 `parallel_group: str="default"`（tool_definition.py:77-78），**不需要重复引入 `parallel_safe`**。
- `delegate_task` 工具定义已声明 `execution_mode="thread"`、`parallel_mode="parallel"`（delegate_task.py:138-139），即当前已具备并行调度资格。
- `ToolExecutionService.run_calls_with_events()` 已按 `parallel_mode` 分流（tool_execution_service.py:172-216）：串行调用逐个执行；`parallel` 调用统一交给 `_run_calls_with_parallel_modes`（`ThreadPoolExecutor`，worker 数受 `Settings.MAX_PARALLEL_TOOL_CALLS=8` 约束），最终由 `_build_result_with_cancel_placeholders` 按原始 index 合并、补占位并统一序列化为模型消息。⚠️ 「当前按 `for call in calls` 串行执行」的旧描述已不成立。
- `DelegationPolicyContext.max_concurrency` 默认 1（定义于 delegation_context.py:41）；策略通过 `running_children >= max_concurrency` 拒绝超限委派（检查点在 delegation_policy.py:35）。
- 当前并发判断由 `DelegationExecutor` 先调用 `DelegationService.count_active_children(parent_turn_id)`，再调用 `create_pending()` 创建 delegation（delegation_executor.py:125/137），两个操作不是同一事务，未来并发执行时存在 TOCTOU 竞态。
- `ChildAgentRunner` 是同步桥：`run_child` 在无 running loop 时用 `asyncio.run()` 驱动 `AgentRuntime.run_agent()`；当前线程已有 running event loop 时返回 `delegation_runner_event_loop_thread` 失败（child_agent_runner.py:59-67）。
- `tools_node` 把整批工具执行经 `asyncio.to_thread(operations.run_tool_calls, ...)` 移出事件循环线程，并显式传入 `running_loop` 供实时输出调度回环（tools_node.py:278-285）。
- `ToolExecutor` 对 thread 模式不执行硬超时；`timeout_seconds` 只作为元数据。

## 三、核心判断

### 3.1 当前线程池方案只能作为兼容过渡

`ThreadPoolExecutor + asyncio.run()` 能以较小改动跑通多个 child，但它不适合作为长期并发委派底座：

- Python 线程无法安全强杀，超时和取消只能软协作。
- child runtime 本身是 async generator，用线程桥接会让事件循环、checkpoint、取消和恢复语义分裂。
- 工具线程池并发会把 subagent 运行时能力藏在工具执行层：事件循环、checkpoint、取消与恢复语义分裂，任何并发增强都要在线程桥上叠加补丁。
- 当前 thread 模式 timeout 只是元数据，容易造成“看似有超时，实际无法停止”的维护陷阱。

因此本方案把线程池并发降级为“必要时的临时兼容实现”，不作为推荐主路径。

### 3.2 推荐主路径：async-first delegation runtime

第一版并发应优先建立 async-first child runner 和声明式并发调度：

- child runner 提供 async 入口，直接在父 turn 的事件循环中创建 child task。
- 并发上限通过 `asyncio.Semaphore` 或数据库 acquire 约束。
- 取消通过 task cancellation + 现有 cancellation registry 协作。
- timeout 使用 `asyncio.timeout()` / `asyncio.wait_for()` 表达软超时边界。
- 工具执行层只负责把 `delegate_task` 调度到 async delegation runner，不把 child runtime 长期塞进普通工具线程。

这一方向改动面更大，但更符合长期稳定迭代：并发、取消、超时、事件流和 checkpoint 都留在 async runtime 语义内。

### 3.3 并发委派不采用 LangGraph-native（当前决策）

**决策（2026-08-12）：本方案不采用 LangGraph `Send` / subgraph 作为并发委派实现，也不把「向 LangGraph 收敛」列为设计依赖或后续评估项。**

原因：

- 当前 `delegate_task` 已经作为工具协议落地，父模型通过工具调用表达委派意图。
- 现有 delegation service、turn 记录、runtime event、前端 timeline 都围绕工具委派实现。
- 直接迁移到 `Send` 会同时改变 workflow state、checkpoint、事件投影和前端订阅边界，属于独立的架构迁移，不是并发止损的第一步。

因此并发委派以独立的 `AsyncChildAgentRunner`（直接消费 `AgentRuntime.run_agent` 异步生成器）为第一版实现，其内部实现可独立演进，但**不预设 LangGraph 收敛路径**。唯一要求是：不把工具线程池（`ThreadPoolExecutor + asyncio.run`）固化为不可替换核心——这是可维护性要求，不是为 LangGraph 铺路。

## 四、设计原则

1. **长期演进优先**：允许为 async-first delegation runner 做结构性改动，不为了少改动固化线程桥接。
2. **并发能力声明化**：不要用“连续 `delegate_task` 分组”作为长期语义。复用现有 `ToolDefinition.parallel_mode="parallel"` 声明 `delegate_task` 可并发，后续可扩展到其他经验证安全的只读工具。
3. **并发额度必须原子 acquire**：不能依赖 `count_active_children()` 与 `create_pending()` 的分离判断。
4. **事件真实，模型稳定**：运行时事件按真实发生顺序发布；模型可见 tool message 按原始 call 顺序组装。
5. **失败可闭合协议**：任何拒绝、取消、超时、内部异常都必须生成对应 `ToolObservation`，闭合每个 tool call id。
6. **不污染稳定 profile**：并发 child 必须继续使用派生运行时 profile，禁止修改 registry 中的稳定 profile 对象。
7. **普通工具默认串行**：写入、patch、delete、terminal 默认保持串行，除非工具显式声明并通过线程安全验证。

## 五、总体方案

第一版采用“声明式并发调度 + async-first child runner + 原子 delegation acquire”。

工具批次执行按“工具并发能力声明”调度（现有 `run_calls_with_events` 已实现该分流，本方案在其上演进）：

```text
输入 calls
  -> 保留原始 index
  -> 普通工具（parallel_mode="serial"）：串行执行
  -> 可并发工具（parallel_mode="parallel"）：bounded parallel execution
  -> 第一版只有 delegate_task 声明 parallel_mode="parallel"
  -> observation 按原始 calls 顺序回填
  -> messages_for_model 按原始 calls 顺序生成
```

第一版只把同一批次中声明为 `parallel_mode="parallel"` 的 `delegate_task` 放入并发池，普通工具仍串行。语义表达为“并发能力声明”，而不是“连续分组技巧”。

## 六、核心改造

### 6.1 配置项

新增进程级配置：

```python
DELEGATION_MAX_CONCURRENCY: int = 2
DELEGATION_TIMEOUT_SECONDS: float = 300.0
```

约束：

- `DELEGATION_MAX_CONCURRENCY` 第一版默认 2（决策确定，非待议项）。
- 最小值为 1；非法配置在 `Settings.load()` 或配置校验阶段归一化/报错。
- `DELEGATION_TIMEOUT_SECONDS` 是 child 委派软超时（建议默认与现有 `delegate_task.timeout_seconds=300.0` 对齐，delegate_task.py:33），不等同于线程硬杀。其与工具定义 `timeout_seconds` 的关系：async 路径（`AsyncDelegationExecutor`）以 `DELEGATION_TIMEOUT_SECONDS` 为生效超时；`timeout_seconds` 保留为 `ToolDefinition` 元数据供 thread 兼容路径与展示使用，不双轨生效。
- 后续可增加全局 child runner 并发上限，避免多个父 turn 同时创建过多 child。

### 6.2 ToolDefinition 并发声明（复用现有字段，不新增）

`ToolDefinition` 已具备并发调度声明字段（tool_definition.py:77-78），**不再新增 `parallel_safe`**：

```python
parallel_mode: Literal["serial", "parallel"] = "serial"
parallel_group: str = "default"
```

语义：

- `parallel_mode="parallel"`：声明该工具可被 bounded parallel 调度（与 `execution_mode` 正交——`execution_mode` 决定隔离方式，`parallel_mode` 决定批次调度）。
- `parallel_group`：预留资源族分组，第一版所有可并发工具用默认组；未来互斥资源可借组做更细粒度约束。

现状与第一版：

- `delegate_task` **已声明** `parallel_mode="parallel"`、`execution_mode="thread"`（delegate_task.py:138-139）。
- 其余 9 个标准内置工具（read_file / write_file / patch / search_files / list_directory / delete / execute_terminal / web_search / web_extract）与 6 个 CodeGraph 查询工具均默认 `serial`（tool_system.py:91-109 注册区间，其中 :100-102 为 `delegate_task` 注册，其余工具均未声明 `parallel_mode` 故走默认 `serial`），无需改动。

长期扩展：

- 只读工具可在验证 file state coordinator 线程安全后声明并发。
- 写入、patch、delete、terminal 默认不声明并发。
- 未来如果存在互斥资源，可通过 `parallel_group` 或 resource key 做更细粒度调度。

### 6.3 静态策略与原子 acquire 拆分

当前 `DelegationPolicy.resolve()` 同时处理静态策略和 active child 数量。并发后需要拆分：

静态策略保留在 `DelegationPolicy`：

- child agent 是否已注册且允许被委派。
- 委派深度是否超过上限。
- parent / child / system 三方工具权限交集是否为空。
- `delegate_task` 不进入 child effective tools，避免递归委派。

并发额度下沉到 `DelegationService.try_create_pending()`：

```python
@dataclass(frozen=True)
class DelegationAcquireResult:
    acquired: bool
    delegation_id: str
    reason: str
```

推荐接口：

```python
def try_create_pending(
    self,
    *,
    task_id: str,
    parent_turn_id: str,
    parent_agent_id: str,
    child_agent_id: str,
    delegation_type: str,
    prompt: str,
    effective_tools: tuple[str, ...],
    max_concurrency: int,
    runtime_event_loop: asyncio.AbstractEventLoop | None = None,
) -> DelegationAcquireResult:
    ...
```

语义：

- acquire 成功：创建 `pending` delegation，发出 `DELEGATION_STARTED`，返回 `acquired=True`。
- acquire 失败：不创建 delegation，返回 `reason="delegation_concurrency_exceeded"`。
- 持久化异常：向上抛出，由 `DelegationExecutor` 收口为 error observation。

与现有 `DelegationService.create_pending()` 的关系：

- **`try_create_pending` 是创建 delegation 的唯一生产入口**：新增方法将「并发额度校验 + 创建」合并进同一事务（见 §6.4），`create_pending()` 不再承载并发校验职责。
- 现有 `create_pending()` 作为薄封装保留：只做纯创建（供测试、迁移与旧调用点过渡），不再与并发判断耦合；或直接由 `try_create_pending` 内部复用其纯创建逻辑。
- Phase 1A 落地时须同步迁移现有调用点（`DelegationExecutor.execute`），禁止新代码直接调用 `create_pending()` 绕过并发校验。

### 6.4 SQLite 原子实现

`DelegationCrud` 新增事务方法，完成同一 parent turn 下 active child 数量检查与 insert。

事务伪代码：

```text
BEGIN IMMEDIATE
SELECT COUNT(*)
  FROM delegations
 WHERE parent_turn_id = ?
   AND status IN ('pending', 'running')

if count >= max_concurrency:
    ROLLBACK
    return rejected

INSERT INTO delegations (...)
COMMIT
return acquired
```

原因：

- SQLite `BEGIN IMMEDIATE` 会取得写锁，使多个并发 child acquire 在 active count + insert 边界排队。
- 可以避免两个 worker 同时看到 active=0 后都 insert 的 race。
- 第一版是本地桌面应用，SQLite 写锁排队成本可接受。

注意：

- `count_active_children()` 可以保留用于查询展示或兼容测试，但不能作为并发安全依据。
- 项目 storage 层统一基于 SQLAlchemy `sessionmaker`（`engine_cache.py:104` 的 `create_session_factory`，CRUD 用 `with self._session_factory.begin() as session`），`BEGIN IMMEDIATE` 需要在 session 工厂层用 `isolation_level` 或 SQLAlchemy 事件监听配合，必须对齐现有封装，不另起平行连接管理。
- 与 SQLAlchemy `sessionmaker` 的对接方式（推荐二选一，落地时实现确认）：
  - 方案 A（推荐）：SQLAlchemy 事件监听（`event.listen(engine, "begin", ...)`）在事务开始时执行 `PRAGMA` 或改写连接隔离级别，使 `try_create_pending` 的写事务以 `BEGIN IMMEDIATE` 语义开始——与现有 `with session_factory.begin()` 封装兼容，不需改 CRUD 写法。注意：SQLAlchemy 的 pysqlite dialect 只支持有限几个 `isolation_level` 取值（`""` / `"DEFERRED"` / `"IMMEDIATE"` / `"EXCLUSIVE"` / `"AUTOCOMMIT"` 中按版本映射），且 `"IMMEDIATE"` 的语义映射需以实测为准，落地时先写并发验证测试再固定实现。
  - 方案 B：engine 级 `connect_args={"isolation_level": "IMMEDIATE"}` 或事件监听配合 `PRAGMA busy_timeout`，让所有写事务默认以 IMMEDIATE 开始；代价是普通写路径也带上 IMMEDIATE 锁，需评估对既有 CRUD 写并发的影响。
  - 无论哪种方案，都必须让 `try_create_pending` 的「count + insert」落在**同一个** `session_factory.begin()` 事务内，并验证并发测试下不出现两个 worker 同时 active=0 都 insert 的情况（具体实现细节落地时确认，不锁死方案）。

### 6.5 AsyncChildAgentRunner

新增 async-first child runner，建议位置：

```text
apps/backend/app/core/delegation/async_child_agent_runner.py
```

推荐接口：

```python
class AsyncChildAgentRunner:
    async def run_child(
        self,
        child_profile: AgentProfile,
        *,
        timeout_seconds: float,
    ) -> DelegationResult:
        ...
```

职责：

- 直接消费 `AgentRuntime.run_agent(child_profile)` async generator。
- 提取 `FINAL_RESPONSE` 与 `RUN_FINISHED` / `RUN_FAILED` / `RUN_CANCELLED`。
- 使用 `asyncio.timeout()` 或 `asyncio.wait_for()` 表达软超时。
- 捕获 `asyncio.CancelledError` 并转为 cancelled delegation result，同时重新设置 cancellation registry。
- 不调用 `asyncio.run()`。

**与现有 `ChildAgentRunner` 共享事件流消费器（不重复造轮子）**：

- 现有 `ChildAgentRunner` 已实现「遍历 `run_agent` 事件流 → 提取终态 → 组装 `DelegationResult`」的完整逻辑。此段逻辑与同步/异步无关，应抽取为独立共享组件（如 `delegation/child_agent_event_consumer.py`），其输入为 async generator 事件流、输出为 `DelegationResult`。
- `AsyncChildAgentRunner` 与现有 `ChildAgentRunner` **均复用该共享消费器**：`ChildAgentRunner` 只做「经 `asyncio.run()` 桥接驱动 generator + 调用共享消费器」的薄封装；`AsyncChildAgentRunner` 直接 `async for` 驱动 generator + 调用同一消费器。禁止两处平行复制事件抽取逻辑。
- 共享消费器的职责边界须明确：**只做事件流消费与 `DelegationResult` 组装**（遍历事件、提取 `FINAL_RESPONSE` 与 `RUN_FINISHED` / `RUN_FAILED` / `RUN_CANCELLED`）；`should_cancel` 轮询归属 runner 层——`AsyncChildAgentRunner` 以 `asyncio` 取消语义 + `TurnCancellationRegistry` 实现，`ChildAgentRunner` 同步桥以现有轮询方式实现，共享消费器不内嵌取消轮询，避免两套取消语义耦合进同一组件。
- 抽取后需以现有 `ChildAgentRunner` 测试为准迁移测试，保证行为不回归。

兼容：

- 现有 `ChildAgentRunner` 可暂时保留，作为同步桥接或测试兼容层。
- 新并发路径必须使用 `AsyncChildAgentRunner`，避免继续扩大同步桥使用范围。

### 6.6 AsyncDelegationExecutor

现有 `DelegationExecutor.execute()` 是同步 port，适配 thread 模式工具。并发主路径建议新增 async executor，而不是在同步 executor 内部再嵌套线程池：

```python
class AsyncDelegationExecutor:
    async def execute(
        self,
        args: DelegateTaskArgs,
        execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        ...
```

流程：

```text
resolve child profile
build child prompt
resolve static policy
if denied -> return policy error observation

try_create_pending(max_concurrency)  # 通过 asyncio.to_thread 包装同步 SQLite 操作
if rejected -> return concurrency error observation

create child turn
claim child turn
mark_child_started
build derived child profile
await AsyncChildAgentRunner.run_child(child_profile)
finalize delegation result
```

**必须复用现有 `DelegationExecutor` 的编排辅助，禁止平行复制**：

- 现有 `DelegationExecutor.execute()`（delegation_executor.py:61-202）已完整实现上述编排，并沉淀了 5 个与同步/异步无关的辅助方法：`_build_agent_input_text`（:204，结构化参数拼装为英文任务文本）、`_delegation_type_from_child_agent_id`（:258，委派类型标签派生）、`_policy_error`（:278，策略拒绝 observation）、`_finalize_result`（:317，终态落库 + 事件 + 消息回填）、`_child_error`（:376，child 失败/取消 observation）。
- `AsyncDelegationExecutor` **必须复用或迁移这 5 个辅助方法**：推荐把编排步骤抽取为共享核心（如 `delegation/delegation_orchestration.py`），同步与 async 两个 executor 均调用同一核心；至少也要把 5 个辅助方法迁到可复用位置由两者引用。禁止两处平行复制编排逻辑（与 §6.5 / §6.7 复用纪律一致）。
- 同步 executor 与 async executor 的去留：Phase 1B 落地后，`delegate_task` 生产路径唯一走 `AsyncDelegationExecutor`（见 §6.7）；现有 `DelegationExecutor` 保留为兼容层供 thread 模式工具与测试过渡，但不继续扩大其使用范围。

边界：

- storage / service 如果仍是同步实现，可用 `asyncio.to_thread` 包装窄调用点，但不能把整个 child runtime 放进线程。
- acquire 后的任何异常都必须把 delegation 落到 failed/cancelled 终态。
- 并发超限未创建 delegation 时，只返回 tool error observation；如需审计超限事件，先写日志，不伪造 failed delegation。

### 6.7 ToolExecutionService 异步批次调度（明确与现有入口的边界）

现状：`tools_node` 把整批工具执行经 `asyncio.to_thread(operations.run_tool_calls, ...)` 移出事件循环（tools_node.py:278-285）；`run_calls_with_events` 内部已按 `parallel_mode` 分流，并行组走 `_run_calls_with_parallel_modes`（`ThreadPoolExecutor`，每个 worker 线程直跑 handler，`delegate_task` 的 handler 在其中以 `asyncio.run()` 驱动 child，事件循环分裂）。

本方案新增 async 批次入口，**并明确两条入口的边界**：

```python
async def run_tool_calls_async(...) -> ToolRunResult:
    ...
```

边界规则（生产路径统一、同步入口只作兼容）：

- **生产路径唯一**：`tools_node` 在 async 入口落地后统一切换为 `await run_tool_calls_async(...)`。普通工具与 `delegate_task` 都在 async 入口内部调度（普通工具单次 `asyncio.to_thread` 包装、`delegate_task` 走 `AsyncDelegationExecutor`），不再经 `run_calls_with_events`。
- **`run_calls_with_events` 降级为纯兼容入口**：保留给测试、无并发需求的调用方与历史调用点过渡。其对 `delegate_task` 不做任何处理分支——凡在同步入口发现 `parallel_mode="parallel"` 的委派调用直接抛错拒绝（显式报错，禁止静默回落串行），强制调用方迁移到 async 入口。
- 同步入口的 `ThreadPoolExecutor` 并行组仅承载未来声明为 `parallel_mode="parallel"` 的非委派只读工具（当前无此类工具，路径休眠）。

async 入口内部实现策略：

- 普通同步工具用 `asyncio.to_thread` 包装单次执行（保留串行语义）。
- `delegate_task` 走 `AsyncDelegationExecutor.execute()`。
- 并发调度使用 `asyncio.TaskGroup`（Python 3.11+ 已满足）或 `asyncio.gather` + `asyncio.Semaphore`。
- 结果按原始 index 合并。

**必须复用既有共享辅助，不重写批次逻辑**：

- `run_tool_calls_async` 与 `run_calls_with_events` 应共享同一套横切辅助，禁止平行复制：事件发布（`TOOL_CALL_STARTED` / `TOOL_CALL_FINISHED` / `TOOL_OUTPUT_DELTA`，其中 `TOOL_OUTPUT_DELTA` 为命令类工具运行期逐段实时广播，见 tool_execution_service.py:82 与 :692）、observation 后处理（脱敏 / `_handle_completed_observation` 类逻辑）、取消占位与序列化（`_build_result_with_cancel_placeholders`，按原始 index 排序 + 补占位 + 统一序列化消息）。
- 推荐实现：将上述辅助从 `run_calls_with_events` 内部提取为可复用方法/模块，async 入口调用同一套；验收时检查两入口对同一批工具产生的 `messages_for_model` 完全一致。

推荐调度伪代码：

```text
for call in calls:
    if should_cancel:
        mark remaining calls as cancelled placeholders
        break

    if _is_parallel_call(call) and call.tool_name == "delegate_task":
        collect into delegation batch
    else:
        flush pending delegation batch
        run normal tool serially (asyncio.to_thread 单次包装)

flush pending delegation batch
sort observations by original index
messages_for_model = observations.map(_to_model_message)
```

注意：调度依据是 `ToolDefinition.parallel_mode` 声明，不是“位置相邻”。第一版仅 `delegate_task` 声明为 `parallel`，因此“委托批”与“串行批”的划分自然正确。

### 6.8 事件顺序与模型顺序

事件发布：

- `TOOL_CALL_STARTED` 按实际启动顺序发布。
- `TOOL_CALL_FINISHED` 按真实完成顺序发布。
- delegation lifecycle events 继续由 `DelegationService` 和 child runtime 按真实时间发布。

模型消息：

- `ToolRunResult.observations` 最终按原始 `calls` 顺序。
- `ToolRunResult.messages_for_model` 最终按原始 `calls` 顺序。
- 每个 `RuntimeMessage.metadata.tool_call_id` 必须匹配原 call id。

这能同时满足前端看见真实并发、模型上下文保持确定性。

### 6.9 取消

父 turn 取消时继续沿用现有级联取消机制：

- 标记 parent turn cancelled。
- 找到 active child delegation / child turn。
- 标记 child cancellation registry。
- child runner 在事件消费过程中检查 `should_cancel`。

async 并发路径补充：

- `TaskGroup` 中尚未启动的 delegate call 返回占位 error observation。
- 已启动 child task 收到 cancel 后，先设置 child cancellation registry，再等待 child runtime 协作退出。
- 等待超出软取消窗口时，delegation 落 failed/cancelled，并记录日志。
- 所有未执行或未完成 call 都必须闭合 tool call 协议。

### 6.10 超时

第一版采用 async 软超时：

- `AsyncChildAgentRunner.run_child()` 内使用 `asyncio.timeout(DELEGATION_TIMEOUT_SECONDS)`。
- 超时后对模型返回 timeout error observation。
- 对已 acquire 的 delegation 标记 failed 或 cancelled。
- 对 child turn 设置 cancellation registry，促使 child runtime 尽快停下。

仍需明确的风险：

- 软超时不等于 OS 级强杀。如果底层同步工具或外部调用不可取消，child runtime 仍可能延迟退出。
- 因此第一版默认并发上限必须小，且超时后要有日志与审计。
- 若后续需要硬隔离，可评估 child runner 进程隔离，但不能在第一版手写复杂进程调度系统；应先复用现有 `ToolExecutor` 进程隔离经验或成熟 runtime 机制。

## 七、与 LangGraph 的关系（当前决策：不采用 LangGraph-native）

**决策（2026-08-12）：并发委派不采用 LangGraph `Send` / subgraph，内部实现不预设 LangGraph 收敛路径**（见 §3.3）。本节仅明确边界，不再展开“后续评估点”。

- `AsyncDelegationExecutor` 是 delegation runtime port，不绑定具体 child 执行实现。
- `AsyncChildAgentRunner` 第一版直接消费 `AgentRuntime.run_agent()` async generator。
- 保持「可替换」仅出于可维护性：若未来架构决策改变，runner 内部实现可独立演进，`delegate_task` 工具契约、delegation service、前端事件不受影响。

## 八、数据与事件契约

第一版无需新增 delegation lifecycle 事件类型，继续复用已落地的：

- `DELEGATION_STARTED`
- `DELEGATION_CHILD_STARTED`
- `DELEGATION_FINISHED`
- `DELEGATION_FAILED`
- `DELEGATION_CANCELLED`

`TOOL_CALL_FINISHED` 中的 `data` 应继续携带：

```json
{
  "delegation_id": "...",
  "child_turn_id": "...",
  "status": "completed"
}
```

并发超限且未 acquire delegation 时，建议 error observation 的 `data` 至少包含：

```json
{
  "reason": "delegation_concurrency_exceeded",
  "parent_turn_id": "...",
  "max_concurrency": 2
}
```

## 九、错误处理

| 场景 | delegation 记录 | 工具 observation | 说明 |
|---|---|---|---|
| 静态策略拒绝 | 不创建 | error | 如 unknown child、depth exceeded、no effective tools |
| 并发额度超限 | 不创建 | error | acquire 失败，不伪造 failed delegation |
| acquire 后 child turn 创建失败 | failed | error | 已有 delegation 必须落终态 |
| child runner failed | failed | error | 复用现有 finalize 语义 |
| child runner cancelled | cancelled | error | 父取消或 child 取消 |
| async task 内部异常 | 视 acquire 状态 | error | acquire 前不创建；acquire 后落 failed |
| 未启动即取消 | 不创建 | error placeholder | 闭合 tool call 协议 |
| 软超时 | failed/cancelled | error | 同时设置 child cancellation |

## 十、测试计划

### 10.1 单元测试

- `DelegationPolicy`：静态策略不再依赖 active child count。
- `DelegationService.try_create_pending`：
  - active < max 时创建 pending。
  - active >= max 时返回 `delegation_concurrency_exceeded`。
  - 并发两个 acquire、max=1 时只能一个成功。
- `AsyncChildAgentRunner`：
  - 从 child runtime events 提取 final summary。
  - child failed/cancelled 正确映射。
  - timeout 正确返回 failed/cancelled result。
  - cancellation 不吞掉协议终态。
- `AsyncDelegationExecutor`：
  - 并发超限返回 tool error。
  - acquire 后异常会 mark failed。
  - 成功路径继续创建 child turn、claim、mark started、run child、mark completed。
- `ToolExecutionService.run_tool_calls_async`：
  - 普通工具仍串行。
  - `delegate_task` 根据 `parallel_mode="parallel"` bounded parallel。
  - 非并发工具不被重排。
  - `messages_for_model` 顺序等于原 calls 顺序。

### 10.2 集成测试

- 同一批 2 个 child 分别 sleep 1s，总耗时接近 1s 而不是 2s。
- `DELEGATION_MAX_CONCURRENCY=1` 时，同一父 turn 两个并发委派只有一个 acquire 成功。
- 父 turn cancel 后 active child delegation 全部进入 cancelled/failed。
- child 乱序完成时，前端事件按完成时间到达，模型消息按 tool call 顺序生成。
- 普通文件写工具不被并发执行。
- `asyncio.run()` 不再出现在新并发委派主路径。

## 十一、实施阶段

### Phase 1A：并发安全底座

- 新增并发配置。
- 新增 `DelegationAcquireResult`。
- 新增原子 `try_create_pending`。
- 调整 `DelegationPolicy`，把 active count 从静态策略中移除。
- 调整 `DelegationExecutor` 或新增 `AsyncDelegationExecutor` 使用 acquire 结果。
- 补并发 acquire race 测试。

### Phase 1B：Async-first child runner

- 新增 `AsyncChildAgentRunner`。
- 新增 async delegation executor port。
- 避免在新并发路径使用 `asyncio.run()`。
- 用 async timeout/cancel 表达 child 生命周期。
- 补 child runner timeout/cancel/failure 测试。

### Phase 1C：声明式并发调度

- 复用现有 `ToolDefinition.parallel_mode`（`delegate_task` 已声明 `"parallel"`），不新增 `parallel_safe`。
- 新增 `run_tool_calls_async` 或等价 async 批次入口，并明确其为 `delegate_task` 唯一调度路径（见 §6.7 边界规则）。
- `tools_node` 切换为 await async 批次入口。
- 保证普通工具串行、并发工具 bounded parallel、模型消息按原顺序。

### Phase 2：普通工具声明式并发（可选后续）

- 只开放只读且已验证线程安全的工具。
- 写入、patch、delete、terminal 默认保持串行。
- 如需资源级互斥，引入 resource key 或 lock group。

## 十二、验收标准

- [ ] 同一工具批次中两个 `delegate_task` 可并行执行，总耗时接近最长 child 耗时。
- [ ] 并发上限为 1 时，两个并发委派不会同时创建 active delegation。
- [ ] 新并发委派主路径不依赖 `ThreadPoolExecutor + asyncio.run()`。
- [ ] `delegate_task` 通过声明式并发能力参与调度，而不是依赖隐式连续分组作为长期语义。
- [ ] 并发 child 乱序完成时，`messages_for_model` 按原始 tool call 顺序。
- [ ] 普通工具仍保持串行执行。
- [ ] 父 turn cancel 后，active child delegation 和 child turn 不残留 running/pending。
- [ ] delegate soft timeout 会返回 error observation，并尽力取消 child turn。
- [ ] 所有失败路径都闭合 tool call 协议，不产生悬空 `AIMessage.tool_calls`。
- [ ] 新增并发逻辑有单元测试和至少一个端到端集成测试覆盖。

## 十三、开放问题

- 并发上限默认值已确定为 2（见 §6.1），不再待议；真实使用稳定后再评估是否扩大。
- 并发超限是否需要写 runtime event。建议第一版只写日志和 tool error observation，避免引入未创建 delegation 的生命周期事件。
- soft timeout 后 child runner 如果仍在后台继续执行，是否需要 UI 显示“取消中”。第一版可先落 failed/cancelled 终态并记录日志，后续阶段再完善强一致停止语义。
- 是否允许多个不同父 turn 同时各自运行 child。建议允许，由 per-parent-turn concurrency 控制局部并发；全局并发上限可作为后续保护项。
- 已决策不采用 LangGraph-native（见 §3.3），因此不再评估 `Send` 收敛时机；若 implementation 阶段发现 async runner 与 checkpoint 恢复冲突，应在独立 runner 内修复，而不是退回线程桥补丁。

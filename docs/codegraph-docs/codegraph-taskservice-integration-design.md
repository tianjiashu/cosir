# CodeGraph 第二阶段技术方案（二）：`TaskService` 接入与异步准备状态机

> 状态：**已评审（第四轮独立审查「符合」）**
> 日期：2026-08-04
> 前置：`codegraph-workspace-lifecycle-design.md`（第二阶段方案一：Workspace 索引生命周期，已通过独立审查，代码已落地）
> 本文范围：把方案一的 `ensure_ready` 接入真实运行链路；定 D8 为 **异步状态机（一步到位）**，并落地准备阶段事件。Agent 可见的 6 个 `codegraph_*` 工具、前端 UI 不在本文。

---

## 一、本文要解决的问题

方案一交付了 `CodeGraphLifecycleService.ensure_ready(workspace_path)`（编排 `status→init/sync`、singleflight、降级），但它是**孤立能力**——没有接进任何运行链路。本文解决：

1. **接入点**：`ensure_ready` 在哪个真实时机、对哪个 workspace 被调用？
2. **异步化（D8 = 一步到位异步状态机）**：`init` 可能阻塞数分钟，不能同步卡死 API 线程；要 task/turn 先建为准备态，进度可观测。
3. **准备阶段事件**：把方案一 6.5「推迟到 TaskService 接入」的事件化正式落地（`RuntimeEvent` 需要 task_id/turn_id，本链路才具备）。

---

## 二、核实后的关键事实（决定接入点）

### 2.1 `create_task` 不跑 Agent —— 真正的执行入口是 turn stream

核实 `turns_api.py` 与 `workspaces_api.py` 后确认：

- `POST /workspaces/{workspace_id}/tasks` → `TaskService.create_task`（`workspaces_api.py:174`）**只创建 task + 首 turn（pending），不启动 Agent**。
- Agent 运行由 **`GET /turns/{turn_id}/stream`**（`turns_api.py:108`）触发：它校验 turn 为 `pending`，经 `RuntimeEventBus.claim_turn_producer` 认领后，`_drive_runtime_turn` 调 `runtime.run_turn(turn_id, turn)` 启动执行。
- 后续轮次复用同一 `stream_turn` 路径（`POST /tasks/{task_id}/turns` 只追加 pending turn）。

**结论**：索引就绪约束的真正时序约束点在「**turn 被认领执行之前**」，而不是「task 创建时」。task 创建只需要 workspace 元数据，不需要索引；首次真正用上 CodeGraph 的是 turn 执行。

### 2.2 准备阶段事件的接入范式（已核实）

`RuntimeEvent` 链路要求（方案一已确认）：
- `EventType` 枚举成员（`app/models/enums/event_type.py`）；
- 对应 payload Pydantic 模型（`app/models/payload/<xxx>_payload.py`，继承 `RuntimeEventPayload`）；
- 注册进 `EVENT_PAYLOAD_MODELS`（`registry/runtime_event_payload_registry.py:24`）；
- 前端 `apps/shared/ts/events.ts` 的 `RuntimeEventType` 联合类型同步。

### 2.3 `ensure_ready` 需要 `workspace_path`，而 turn 关联 workspace

`TurnService.create_turn` / `TurnRecord` 本身不直接持有 `workspace_path`——workspace 关联在 **task** 上（`TaskRecord.workspace_id`）。因此准备阶段编排需要：turn → task → workspace_id → workspace_path（经 `WorkspaceService`/`WorkspaceCrud` 解析）。

---

## 三、决策（D8 正式拍板）

| # | 决策 | 结论 | 依据 |
|---|------|------|------|
| D8 | init 阻塞模型 | **异步状态机（一步到位）**：turn 先建为 `preparing`，`ensure_ready` 在后台执行，就绪后放行 `run_turn` | `init` 耗时不可控，同步阻塞会卡死 API 线程；讨论文档已定义 `preparing_workspace` 流程；事件化需要 task_id/turn_id（本链路才有） |
| D10 | 接入点 | **挂在 turn 执行前**（`stream_turn` 认领 → 后台准备 → 就绪放行 `run_turn`），非 `create_task` | §2.1：`create_task` 不跑 Agent，真正时序约束在 turn 执行；后续 turn 复用同一路径 |
| D11 | 降级策略 | prepare 失败 → **放行 `run_turn` + 文件搜索兜底**，但 emit `workspace_degraded` 事件 + 日志标记 CodeGraph 不可用 | 方案一 D6 沿用；不阻塞用户 |
| D12 | 准备态可观测 | **不新增持久化 `preparing` 列**；准备期间 turn 持久化状态仍为 `pending`，前端经 SSE 事件流感知准备态（`workspace_preparing` → `workspace_ready`/`workspace_degraded` 三事件，§4.5），`task_display_status` 在准备期间仍派生 `pending` | §2.1 已核实 `task_display_status` 读的是持久化 DB 记录，不落库则无法派生 preparing；见 §4.5 事件流方案 |

> **D8 一步到位的含义**：不做「最小切片」（如先同步阻塞再演进）。直接实现完整异步状态机——turn 创建不阻塞、准备在后台线程/任务执行、经事件驱动推进到执行。

---

## 四、接入点与状态机设计

### 4.1 状态机

在既有 `pending → running → terminal` 基础上，插入准备阶段（仅当该 workspace 需要 CodeGraph 就绪）：

```text
turn 创建:   pending
              │  stream_turn 认领（claim_turn_producer）
              ▼
           preparing   ◄── 后台执行 ensure_ready（不阻塞 SSE 连接）
              │
              ├── 就绪 ──► running ──► completed / failed / cancelled
              │
              └── 降级 ──► running（放行，CodeGraph 不可用标记已 emit）
```

**关键**：`preparing` 是一个**瞬时/短暂态**（不落库，D12），SSE 连接在 `stream_turn` 时已经建立并保持；准备阶段进度经同一 SSE 通道下发（`workspace_preparing` → `workspace_ready` / `workspace_degraded` 三个事件，见 §4.5），就绪后**立即**进入 `running` 并启动 `run_turn`。前端在 `preparing` 阶段展示「正在建立代码索引…」（不确定态进度）。**持久化层面 turn 在准备期间仍是 `pending`**（`task_display_status` 派生 `pending`），前端感知准备态靠 SSE 事件流而非轮询状态。

### 4.2 接入点：`stream_turn`（而非 `create_task`）

```text
GET /turns/{turn_id}/stream  （turns_api.py:108）
  │
  ├─ turn 非 pending → 409（既有）
  ├─ event_bus.subscribe(turn_id)
  ├─ claim_turn_producer(turn_id) 成功后：
  │     producer = asyncio.create_task(_drive_turn_with_prepare(...))
  │       ├─ task_id = turn.task_id                                   # turn → task 归属（既有字段）
  │       ├─ workspace_path = turn_workspace_resolver.resolve(turn)  # API 层先解析（唯一解析点）
  │       ├─ await turn_prepare_service.prepare_then_execute(task_id, turn_id, workspace_path, execute)
  │       │     · workspace_path=None → 跳过 prepare，直接 execute（§五）
  │       │     · 否则 ensure_ready 经线程池后台跑（init 阻塞不占事件循环）
  │       │     · 开始 emit workspace_preparing；结束 emit workspace_ready / workspace_degraded
  │       │     · 完成后回调 execute() = runtime.run_turn(turn_id, turn)（既有执行）
  └─ 其余走既有 _sse_turn_events 订阅消费
```

**职责归属**：`turn_workspace_resolver.resolve(turn)` 由 API 层 `_drive_turn_with_prepare` 调用（唯一解析点，避免 service 内重复解析），再把解析出的 `workspace_path` 传入 `turn_prepare_service.prepare_then_execute`（§4.4 签名）。service 不持有 resolver，职责单向清晰。

**为什么能一步到位且不破坏现有结构**：`_sse_turn_events` 已经是「producer 认领 + SSE 订阅消费」的双角色模型（`turns_api.py:244-255`）。我们只需把 producer 的回调从「直接 `run_turn`」改成「先异步准备、再 `run_turn`」，SSE 消费侧无需改动。`preparing` 期间的进度事件和 `running` 后的执行事件都经同一 `RuntimeEventBus` 下发，前端零协议变更消费。

#### 4.2.1 prepare 阶段的取消与断开兜底（必须覆盖，否则破坏 producer 契约）

现有 `_drive_runtime_turn` 的 producer 释放（`release_turn_producer` / `close_turn`）在 `finally` 中随 `run_turn` 结束执行（`turns_api.py:342-346`）。把 prepare 插入后，prepare 阶段（可经 `asyncio.to_thread` 阻塞数分钟）出现 SSE 断开时，`_sse_turn_events` 的 finally 会 `producer.cancel()`（`turns_api.py:294-297`）。此时必须保证：

1. **`release_turn_producer` 置于覆盖「prepare + run_turn」整体的最外层 `finally`**（而非只在 `run_turn` 结束处）。否则 prepare 中途断开会导致该 turn 的 producer 槽位永久残留，`claim_turn_producer` 恒返回 False，后续任何重连都无法执行该 turn。
   2. **prepare 期间断开需兜底终态**：现有「断开兜底把孤儿 running 落 failed」在 `run_turn` 内部，prepare 期间 turn 仍为 `pending`，该兜底不触发。需为 prepare 阶段定义兜底策略——prepare 中途被 cancel 时，将 turn 落为 **`failed`**（`end_reason="client_disconnected"`，与 `run_turn` 断开语义一致，§九.3）并 emit `run_failed`，避免留下无终态事件的孤儿 `pending` turn。**兜底职责归 API 层 `_drive_turn_with_prepare` 的 finally**（§九.4，prepare_service 不触碰 turn 状态）。注意：`fail_turn_if_running` 只对 `running` 态生效，prepare 期间 turn 为 `pending`，实现 T6 前须核实是否支持 `pending`→`failed`（§九待核实点）。
   3. **`ensure_ready` 的 `to_thread` 后台线程**在连接断开后仍会继续跑完（线程不可强杀），这是可接受的——其结果在 turn 已落终态后不再被消费；但 prepare_service 须在 await 处对 `CancelledError` 正确处理，不泄漏异常、不误触发降级放行。**CancelledError 处理规则**：prepare 被 cancel 时，兜底落 `failed` + emit `run_failed` 后**直接 re-raise**（不吞掉），让 `_sse_turn_events` 的 `suppress(CancelledError)` / `producer.result()` 分支按既有语义对齐收敛，避免二次判断。

> 实现 T6（turns_api 接入）时，`_drive_turn_with_prepare` 必须完整复刻现有 `_drive_runtime_turn` 的 finally 释放顺序，并把范围扩到 prepare+execute 整体。此点纳入验收标准（§六）。

### 4.3 分层与新增/改动文件

```text
# 新增
apps/backend/app/service/task/
  turn_workspace_resolver.py     # turn → task → workspace_path 的纯解析（单一职责）
  turn_prepare_service.py        # 编排：异步 prepare → 放行 run_turn（薄，组合 LifecycleService）
apps/backend/app/models/enums/event_type.py      # [改] 新增 3 个准备阶段事件
apps/backend/app/models/payload/                 # [改] 新增 3 个 payload 模型 + 注册 registry + __init__
apps/backend/app/api/turns_api.py                # [改] producer 回调插入 prepare 步骤

# 复用（不改）
apps/backend/app/service/codegraph/lifecycle_service.py   # 方案一
apps/backend/app/service/runtime_event/runtime_event_bus.py  # 事件总线
```

`turn_workspace_resolver` 与 `turn_prepare_service` **同置 `service/task/`**（二者同属 turn 执行流程编排，需经 `TaskCrud`/`WorkspaceCrud`，与索引生命周期无关）；`service/codegraph/` 只承载方案一的 `lifecycle_service.py` + `inflight_registry.py`（索引生命周期），不混入 turn 领域解析。命名对齐既有 `xxx_service` / `xxx_resolver` 范式，不用 `Orchestrator`/`Manager` 等模糊词。

### 4.4 `turn_prepare_service` 的职责边界

```python
class TurnPrepareService:
    """编排单个 turn 执行前的 CodeGraph 索引准备，并在完成后放行执行。

    职责边界：
        - 负责：接收调用方解析好的 workspace_path、组合 LifecycleService.ensure_ready、
                发布准备阶段事件、返回「放行 or 降级」信号。
        - 不负责：turn → workspace_path 解析（归 API 层 turn_workspace_resolver，
                见 §4.2）、索引算法（上游 CodeGraph）、RPC（Client）、
                并发去重（InflightRegistry）、Agent 执行（run_turn 归调用方）。
    """

    async def prepare_then_execute(
        self,
        task_id: str,
        turn_id: str,
        workspace_path: str | None,
        execute: Callable[[], Awaitable[None]],
    ) -> None:
        """异步准备索引；就绪或降级后回调 execute 启动 run_turn。

        task_id 供构造 RuntimeEvent 信封（RuntimeEvent.task_id 必填）；由 API 层
        从 turn.task_id 传入。
        """
```

`workspace_path` 与 `task_id` 由 **API 层**从 `turn` 记录预先解析并传入（§4.2 唯一解析点），prepare_service 不持有 resolver、不自行解析。解析失败（`workspace_path=None`）即跳过准备。`ensure_ready` 是同步阻塞 RPC（方案一 D8 修订），故在 prepare_service 里经 `asyncio.to_thread` 跑，不占事件循环。

### 4.5 事件定义

新增 `EventType` 成员（沿用 `str(Enum)` 范式）。**只保留「开始」+「结束」两类事件**，不定义 `indexing`/`syncing` 进行中事件：

```python
WORKSPACE_PREPARING = "workspace_preparing"   # 开始准备（prepare_service 进入时发）
WORKSPACE_READY = "workspace_ready"            # 就绪（结束，放行）
WORKSPACE_DEGRADED = "workspace_degraded"      # 降级（结束，放行 + 文件搜索兜底）
```

> **为什么删掉 `indexing`/`syncing` 进行中事件（评审修订）**：已落地的方案一 `ensure_ready` 是**同步单次阻塞调用**（`lifecycle_service.py`），`_do_init`/`_do_sync` 是内部同步步骤，仅在结束时返回最终 `WorkspaceIndexReadiness`（含 `action_taken`）。经 `asyncio.to_thread` 调用后只能拿到最终结果，**无法在 init/sync 进行中发布中间事件**。若强行发，要么时序倒置（先 ready 再补发 indexing），要么根本没有进行中事件——都不符合验收。故**删去两个进行中事件**，由 `workspace_ready.action_taken` 区分本次动作是 `init` 还是 `sync`，前端据此展示「已建立索引」或「已同步索引」。前端进度展示为不确定态（转圈 + 阶段文案）。

payload 范式（一事件一文件，继承 `RuntimeEventPayload`）。**payload 只承载领域差异字段**——`task_id`/`turn_id` 已在 `RuntimeEvent` 信封层（`runtime_event.py:43-49`），不重复写入 payload：

```python
# workspace_preparing_payload.py
class WorkspacePreparingPayload(RuntimeEventPayload):
    workspace_path: str

# workspace_ready_payload.py
class WorkspaceReadyPayload(RuntimeEventPayload):
    workspace_path: str
    action_taken: Literal["init", "sync"]
    files_changed: int
    duration_ms: int

# workspace_degraded_payload.py
class WorkspaceDegradedPayload(RuntimeEventPayload):
    workspace_path: str
    state: str                       # 'failed' | 'unavailable'
    degraded_reason: str             # 面向人/模型的英文摘要
```

**事件持久化策略**：准备阶段事件经 `RuntimeEventService.save_and_publish` 落库（与 `run_turn` 执行事件同构），保证前端刷新后仍可见「已准备」状态；仅内存广播会导致刷新即消失，与执行事件行为不一致。`task_id` 来自 §4.4 的 `prepare_then_execute` 参数。

同步改动（四处，方案一第八节已列）：`EventType` → payload 模型 → `registry.EVENT_PAYLOAD_MODELS` → `apps/shared/ts/events.ts` 的 `RuntimeEventType` 联合类型。前端 `RuntimeEventType` 新增成员用 `never`/占位策略由前端实现时定（第三阶段）。

---

## 五、降级与失败闭环

- **prepare 失败（Kernel 不可用 / init 失败 / indexing / failed）**：`ensure_ready` 返回 `ready=False` + `degraded_reason`（方案一已定绝不抛异常）。`turn_prepare_service` 捕获该结果，emit `workspace_degraded` 事件，然后**照常放行 `run_turn`**——Agent 以文件搜索兜底（既有 9 工具不受影响）。
- **`run_turn` 阶段**：即使索引降级，Agent 的 6 个 `codegraph_*` 工具（第二阶段另文）若被调用，会命中 `WORKSPACE_NOT_INDEXED`，其 description 应引导回退 `search_files`（属工具面文档范围，本文只保证 prepare 阶段不阻断执行）。
- **无 CodeGraph 需求的 turn**：`turn_workspace_resolver` 解析出 `workspace_path=None`（workspace 解析失败或 CodeGraph 未启用）时，`turn_prepare_service` 直接跳过 prepare 并放行，`pending → running` 照旧（不产生准备事件）。`ensure_ready` 必须对可解析的 workspace **幂等**（方案一 D5 已保证 singleflight + 每次 sync）。

---

## 六、任务拆分

| # | 任务 | 产出 | 依赖 |
|---|------|------|------|
| T1 | 事件枚举扩展 | `event_type.py` 新增 3 成员 | —— |
| T2 | payload 模型 | 3 个 `*_payload.py` + 注册 `EVENT_PAYLOAD_MODELS` + `payload/__init__.py` | T1 |
| T3 | 前端事件类型同步 | `apps/shared/ts/events.ts` `RuntimeEventType` 联合类型新增 3 成员 | T1 |
| T4 | 解析器 | `turn_workspace_resolver.py`：turn → task → workspace_path | —— |
| T5 | prepare service | `turn_prepare_service.py`：异步准备（to_thread）+ 事件发布 + 放行信号 | T1 T2 T4 + 方案一 L8 |
| T6 | API 接入 | `turns_api.py` producer 回调插入 prepare 步骤（含 §4.2.1 取消兜底） | T5 |

**验收标准**（端到端，真实第二 workspace）：

1. 无索引 workspace：`POST /workspaces/{id}/tasks` → `stream` → 收到 `workspace_preparing` → `workspace_ready(action_taken='init')`，随后 `run_started`；`.codegraph/` 真实产出非空索引。
2. 已索引 workspace：收到 `workspace_preparing` → `workspace_ready(action_taken='sync')`。
3. 并发两次 `stream` 同一 turn：只准备一次（方案一 singleflight），第二个连接仅订阅事件（`turn_stream_producer_already_running` 日志），不重复准备。
4. Kernel 停止：`stream` 收到 `workspace_degraded`，**turn 仍正常进入 running**（不阻断），Agent 可走文件搜索。
5. **prepare 中途断开（§4.2.1）**：SSE 断开后 producer 槽位被释放（`claim_turn_producer` 可再次认领），turn 落 `failed`（`end_reason="client_disconnected"`）并 emit `run_failed`，无孤儿 pending；`to_thread` 后台 ensure_ready 跑完不被消费且不泄漏异常。
6. 单测：`turn_workspace_resolver` 解析、`turn_prepare_service` 各分支（就绪/降级/跳过）、payload 注册完整性、前端类型生成校验。

---

## 七、明确不做（本文范围外）

| 不做 | 原因 |
|------|------|
| 6 个 `codegraph_*` 工具 ToolDefinition | 归第二阶段另文 |
| 前端准备阶段 UI（进度条/文案） | 第三阶段（本阶段只保证事件可下发、协议可消费） |
| 索引进度百分比 | 方案一 D4：单次阻塞 RPC 拿不到；前端展示不确定态 |
| prepare 的取消/超时强杀 | 需额外参数设计；第一版降级即够（YAGNI） |
| 非首轮重复初始化 | 每次 turn 前 `ensure_ready` 幂等 sync（方案一 D2），无重复 init |

---

## 八、已收敛的决策（不再待确认）

1. **`preparing` 不落库（D12）**：`preparing` 是瞬时态，只经 SSE 事件流感知；持久化状态仍 `pending/running/terminal`，`task_display_status` 在准备期间派生 `pending`。**代价**：前端刷新后无法从 task/turn 状态看到「准备中」（只能靠未消费的 SSE 事件）。若实际使用发现需要「刷新可见准备中」，再评估新增持久化列。
2. **归属层（D13）**：`turn_prepare_service` 与 `turn_workspace_resolver` 同放 `service/task/`（turn 执行流程编排），`service/codegraph/` 只承载索引生命周期（方案一）。理由：二者需经 `TaskCrud`/`WorkspaceCrud`，编排的是 turn 流程而非索引本身。

---

## 九、已收敛的决策（续，2026-08-04 用户确认）

3. **prepare 断开兜底 = `failed`**（`end_reason="client_disconnected"`，**非 `cancelled`**）。依据（核实 `runner.py`）：`run_turn` 的断开兜底是 `_mark_turn_disconnected_if_running` → `fail_turn_if_running(end_reason="client_disconnected")`（`runner.py:376-423`）——既有体系对「客户端断开」的统一终态就是 `failed`；`cancelled` 保留给**用户主动取消**（`POST /turns/{id}/cancel`）。prepare 阶段断开与 run 阶段断开是同一类事件，若 prepare 用 `cancelled`、run 用 `failed` 会造成同行为两种终态，前端语义混乱。§4.2.1 原「倾向 cancelled」推翻。
4. **兜底职责归 API 层**：prepare 断开兜底放 `_drive_turn_with_prepare` 的 finally（与 `_drive_runtime_turn` 的 finally 释放模式对齐），**`turn_prepare_service` 不触碰 turn 状态持久化**（保持「只编排索引 + 发事件」单一职责）。turn 状态由 API 层经 `_turn_service.fail_turn_if_running`（或等价方法）统一管理。

> **实现 T6 前需核实**：`fail_turn_if_running` 只对 `running` 态生效——该原子约束位于存储层 `turn_crud.py:278-285`（`WHERE status == "running"`，turn 为 `pending` 时 `rowcount=0` 返回 None），`runner.py:381-423` 只是调用方。而 prepare 期间 turn 仍是 `pending`。需确认能否以 `pending`→`failed` 落终态；若否，prepare 期间需先把 turn 置为 `running`（或引入过渡态）再执行 prepare，使断开兜底可复用既有方法。此为 T6 的明确前置核实点。

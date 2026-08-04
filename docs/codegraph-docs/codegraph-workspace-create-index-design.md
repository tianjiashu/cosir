# CodeGraph workspace 创建时同步建索引 + workspace 级 SSE 进度 设计方案（两段式）

> 状态：两段式（已定稿，代码已实现）
> 日期：2026-08-04
> 关联文档：`codegraph-workspace-lifecycle-design.md`（索引生命周期）、`codegraph-taskservice-integration-design.md`（turn 前准备）、`codegraph-integration-discussion.md`（生命周期讨论）
> 变更记录：
> - 2026-08-04：曾讨论「方案 B」（POST /workspaces 返回 SSE 单请求），**用户最终拍板回两段式**——理由是 create 之后可能还有其他操作，两段式（create 只落库、prepare 独立触发）便于扩展。

## 一、背景与目标

当前 CodeGraph 索引就绪（`ensure_ready`）**只在 turn 执行前**触发（`TurnPrepareService.prepare_then_execute` → `_drive_turn_with_prepare`）。首次索引（init，大仓库可达数分钟，`CODEGRAPH_INDEX_INIT_TIMEOUT_SECONDS=600s`）被压在用户第一条消息的同步路径里，体验差。

**目标**：在 **workspace 创建时**同步阻塞执行 `ensure_ready(root_path)`，把最重的首次索引提前到创建阶段；前端通过**独立的 workspace 级 SSE 事件流**展示索引进度条与友好提示。

**设计原则**（用户确认，2026-08-04）：所有前置 ensure_ready 的本质，都是为「**agent 实际调 codegraph 那一刻索引最新**」服务。turn 前 ensure_ready 是唯一硬保证；创建 workspace 时建索引是**体验优化**（前移首次 init 等待）；创建 task 时不重复建（turn 前已保证）。

## 二、关键架构约束（已核实）

### 2.1 现有 SSE 事件流是 turn 级的，无法承载创建时进度

- `RuntimeEventBus`（`app/service/runtime_event/runtime_event_bus.py`）按 **turn_id** 路由订阅：`subscribe(turn_id)` / `_subscribers: dict[turn_id, set[queue]]`，`publish` 对 `turn_id=None` **直接 return**。
- `RuntimeEvent.task_id` 为**必填**字段（`app/models/runtime_event.py`）。
- 创建 workspace 时**既无 task_id 也无 turn_id**，因此不能复用现有 turn 级 `RuntimeEvent` + `RuntimeEventBus`。

### 2.2 复用现有事件类型与 payload，不新建

- 复用 `EventType.WORKSPACE_PREPARING / WORKSPACE_READY / WORKSPACE_DEGRADED`（`event_type.py`）。
- 复用 `WorkspacePreparingPayload / WorkspaceReadyPayload / WorkspaceDegradedPayload`（`app/models/payload/`）。
- 但事件**信封**不复用 `RuntimeEvent`（因 task_id 必填），改用一个轻量 workspace 级事件值对象。

### 2.3 与 turn 前准备解耦，不冲突

创建时 `ensure_ready` 是**预热的先做**；turn 前 `TurnPrepareService` 仍保留作为兜底（万一创建后索引尚未就绪/降级，turn 前再确保）。`ensure_ready` 幂等（singleflight + status 分流：已 ready 只 sync），两次触发功能正确，只是可能重复 sync 一次（可接受）。

## 三、总体方案（两段式）

采用**两段式创建**（用户拍板，create 后可能还有其他操作，方便扩展）：

```
① POST /workspaces                            ② GET /workspaces/{id}/index/stream (SSE)
   │ 仅落库空 workspace，快速返回 id               │  按 workspace_id 订阅（此时索引未触发）
   │ （不触发 ensure_ready、不发事件）              │  ③ POST /workspaces/{id}/index/prepare
   │                                              │      → 同步 ensure_ready(root_path)
   │                                              │      → workspace_index_bus 发 preparing→ready/degraded
   │                                              │      → SSE 端实时收进度，展示进度条
```

- **后端**：
  1. `app/service/codegraph/workspace_index_bus.py`：workspace 级事件总线（按 workspace_id 路由，独立于 turn 级 `RuntimeEventBus`）。
  2. `app/service/codegraph/workspace_index_service.py`：索引准备编排 service（组合 `ensure_ready` + `WorkspaceIndexBus` 发布）。
  3. `app/models/workspace_index_event.py`：轻量 workspace 级事件值对象（不带 task/turn，自带 workspace_id/path）。**放 `models/` 与 `runtime_event.py` 同级，不放 `models/payload/`**（后者是运行时事件 payload 值对象目录，`WorkspaceIndexEvent` 是信封而非 payload 模型）。
  4. `app/api/workspace_index_api.py`：**独立 API 文件**，承载 `GET /workspaces/{workspace_id}/index/stream`（SSE 订阅）与 `POST /workspaces/{workspace_id}/index/prepare`（触发索引准备）。
  5. `app/api/workspaces_api.py`：`create_workspace` **只负责落库**（不触发 ensure_ready、不发布事件），保持薄 CRUD。
  6. `app/api/schemas/response/`：`IndexPrepareResponse`（prepare 结果响应）。
- **前端**：workspace 级 SSE 订阅器 + 创建时进度条组件。

## 四、后端详细设计

### 4.1 workspace 级事件值对象

`app/models/workspace_index_event.py`（与 `runtime_event.py` 同级，非 `models/payload/`）：

```python
@dataclass(frozen=True)
class WorkspaceIndexEvent:
    """workspace 索引进度事件（创建时触发，不带 task/turn 信封）。"""
    event_id: str          # uuid4
    event_type: EventType  # 限 WORKSPACE_PREPARING/READY/DEGRADED
    workspace_id: str
    workspace_path: str
    payload: dict[str, object]  # 复用对应 Workspace*Payload 的 model_dump
    created_at: datetime

    def to_dict(self) -> dict[str, object]: ...
```

理由：不强行复用 `RuntimeEvent`（避免给 task_id 传空串的语义污染，也避免把 workspace 事件塞进 turn 级持久化表）。

### 4.2 workspace 级事件总线

`app/service/codegraph/workspace_index_bus.py`：

```python
class WorkspaceIndexBus:
    def __init__(self, queue_size: int = 256): ...
    def subscribe(self, workspace_id: str) -> WorkspaceIndexSubscription: ...
    def unsubscribe(self, subscription): ...
    def publish(self, event: WorkspaceIndexEvent) -> None: ...  # 按 workspace_id 路由
    def close(self, workspace_id: str) -> None: ...             # 关订阅（结束哨兵）
```

订阅对象与 `RuntimeEventSubscription` 同构（`async for` + `_QUEUE_CLOSED` 哨兵），但字段为 `workspace_id`。**独立于 turn 级 `RuntimeEventBus`**（单一职责：workspace 索引进度，不进 turn 事件表）。队列管理（RLock + set 订阅表 + QueueFull 丢弃 + 关闭哨兵）与 `RuntimeEventBus` 同构，因路由键/事件类型不同且 RuntimeEventBus 属已验收核心，暂不提取公共基类（改动最小化，后续 Rule of Three 再提）。

### 4.3 `create_workspace` 接入（两段式，仅落库）

`app/api/workspaces_api.py` 的 `create_workspace` **只落库**，不触发 ensure_ready、不发布事件：

```python
@app.post("/workspaces")
async def create_workspace(payload, workspace_service=Depends(get_workspace_service)) -> WorkspaceResponse:
    workspace = workspace_service.create_workspace(payload.name, payload.root_path)  # 仅落库
    return WorkspaceResponse(**workspace.to_dict())
```

**为什么两段式**：若落库后立即 `ensure_ready` 并发布 preparing 事件，而客户端此时尚未建立 SSE 订阅（bus 只向已注册订阅分发，无缓冲/重放），`preparing` 必然丢失；`ready/degraded` 也因同步阻塞（可达 600s）大概率在订阅建立前已发出，进度流无意义。两段式把「触发索引」推迟到订阅建立之后，保证 preparing→ready/degraded 全部可达。同时，create 之后可能还有其他操作（两段式便于在 create 与 prepare 之间扩展）。

### 4.4 索引准备 service

`app/service/codegraph/workspace_index_service.py`（把编排从 API 层下沉，单一职责）：

```python
class WorkspaceIndexService:
    def __init__(self, lifecycle: CodeGraphLifecycleService, bus: WorkspaceIndexBus): ...

    def prepare(self, workspace_id: str, root_path: str) -> WorkspaceIndexReadiness:
        """发布 preparing → ensure_ready(root_path) → 发布 ready/degraded → close。
        同步阻塞（调用方经 to_thread 跑，避免卡事件循环）。Kernel 不可用降级不抛。"""
```

- 组装 `WorkspaceIndexEvent`（preparing/ready/degraded）并经 `bus.publish` 分发；**终态后** `bus.close(workspace_id)`（close 必须置于所有 emit 之后，否则 close 后 publish 为 noop 会吞掉终态事件——独立审查暴露的致命时序 bug）。
- `ensure_ready` 同步阻塞（`to_thread` 到 executor，与 `TurnPrepareService` 一致）。
- Kernel 不可用：`ensure_ready` 返回 `ready=False`（不抛），发布 degraded，prepare 接口照常返回。

### 4.5 独立 API：SSE 端点 + prepare 触发

`app/api/workspace_index_api.py`（独立文件，单一职责：workspace 索引进度流）。**SSE 帧组装与订阅循环抽成独立模块级函数 `_stream_workspace_index_events`**（对齐 `turns_api._sse_turn_events` 范式，可单测）：

```python
async def _stream_workspace_index_events(
    index_bus: WorkspaceIndexBus, workspace_id: str,
) -> AsyncIterator[str]:
    """把 workspace 索引进度事件转换为 SSE 帧；终态后结束，finally 退订。"""
    subscription = index_bus.subscribe(workspace_id)
    try:
        async for event in subscription:
            yield f"event: {event.event_type.value}\ndata: {json.dumps(event.to_dict())}\n\n"
            if event.event_type in {EventType.WORKSPACE_READY, EventType.WORKSPACE_DEGRADED}:
                break
    finally:
        index_bus.unsubscribe(subscription)

@app.get("/workspaces/{workspace_id}/index/stream")
async def stream_workspace_index(workspace_id: str,
                                 index_bus: WorkspaceIndexBus = Depends(get_workspace_index_bus)):
    return StreamingResponse(
        _stream_workspace_index_events(index_bus, workspace_id),
        media_type="text/event-stream; charset=utf-8",
    )

@app.post("/workspaces/{workspace_id}/index/prepare")
async def prepare_workspace_index(workspace_id: str,
                                  workspace_service=Depends(get_workspace_service),
                                  index_service=Depends(get_workspace_index_service)):
    ws = workspace_service.get_workspace(workspace_id)   # 404 守卫
    if index_service is None:                            # Kernel 不可用降级
        return IndexPrepareResponse(ready=False, state="unavailable", action_taken="none", ...)
    readiness = await asyncio.to_thread(index_service.prepare, workspace_id, ws.root_path)
    return IndexPrepareResponse(**readiness 字段)
```

- **时序关键**：客户端必须**先建立 SSE（GET /index/stream），再触发 prepare（POST /index/prepare）**，确保订阅先就绪、事件全部可达。前端流程编排保证（见第六节）。
- 终态事件（`workspace_ready`/`degraded`）后关闭订阅并结束流。
- **依赖装配**：`get_workspace_index_bus()` / `get_workspace_index_service()` 到 `service/depends.py` + `api/depends/dependencies.py`（薄壳 re-export，已落地）。

## 五、vendor 无需改动

本方案**不触碰 vendor**。理由：
- `ensure_ready`（`app/service/codegraph/lifecycle_service.py`）已完整实现 status→init/sync/degraded 编排，后端直接用 `root_path` 作为参数调用 `client.index_status/index_init/index_sync`。
- vendor `lifecycle-service.ts` 内部用 `findNearestCodeGraphRoot(workspacePath)` 解析；单项目场景下（workspace root 即独立项目根）向上找到的就是 `root_path` 自己，行为等价。
- 本方案仅新增后端调用时机 + workspace 级事件通道 + 前端进度 UI，无 vendor 改动。

## 六、前端设计

### 6.1 workspace 级 SSE 订阅器

`apps/desktop/src/services/workspaceSse.ts`（独立于 `sse.ts` 的 `SSEConnection`，因后者硬编码 `TURN_STREAM` 路径 + turn_id）：

- 复用 `fetch + ReadableStream` 解析范式；
- 路径：`GET /workspaces/{workspace_id}/index/stream`；
- 事件类型收窄为 `workspace_preparing | workspace_ready | workspace_degraded`；
- 终态（ready/degraded）后自动结束。

### 6.2 创建流程编排（两段式）

```
1. 用户触发「创建 workspace」
2. POST /workspaces 仅落库空 workspace，立即返回 workspace_id（快速，不建索引）
   → 展示「正在创建 workspace…」
3. 拿到 id 后，先建立 GET /workspaces/{id}/index/stream SSE 订阅（索引尚未触发，订阅先就绪）
   → 展示进度条「正在为 workspace 建立代码索引…」
4. 再发 POST /workspaces/{id}/index/prepare 触发 ensure_ready（同步阻塞）
5. 进度条随 workspace_preparing（开始）→ workspace_ready（成功，显示 files_changed）/
   workspace_degraded（降级，提示「索引不可用，已降级到文件搜索」）更新
6. prepare 返回（或终态事件到达）后，进入 workspace
```

### 6.3 进度条 UI

- 新增组件（如 `apps/desktop/src/components/workspace/IndexProgressDialog.tsx`）；
- 三态：准备中（spinner + 文案）、就绪（绿勾 + `已索引 N 个文件`）、降级（黄色警示 + `已降级到文件搜索`）；
- 友好提示文案：首次建索引大仓库可能需要数分钟，避免用户误判卡死。

## 七、事件协议与 shared 类型

- 复用现有 `events.ts` 中 `workspace_preparing/ready/degraded` 类型与 payload（`WorkspacePreparingPayload` 等已存在）。
- **不需要**重新生成 `events.ts`（无新增事件类型/payload 模型）。
- **需新增前端信封类型**：`WorkspaceIndexEvent` 的信封结构与现有 `events.ts` 的 `RuntimeEventEnvelope` **有差异**——后者含 `task_id`（必填）/`turn_id`/`sequence`/`message_id`/`tool_call_id`，而 workspace 级信封**无 task/turn**，改带 `workspace_id`/`workspace_path`。建议新增 `apps/shared/ts/workspaceIndex.ts`，定义 `WorkspaceIndexEvent` 与 `WorkspaceIndexEventType`，明确与 `RuntimeEventEnvelope` 的字段差异，避免前端双份解析混淆。

## 八、验收清单

1. **两段式**：`POST /workspaces` 仅落库返回 id；`POST /workspaces/{id}/index/prepare` 同步触发 `ensure_ready` 并发布 preparing→ready/degraded。
2. 独立 workspace 级 SSE 端点可订阅进度，终态后流结束。
3. **时序**：先连 SSE 再触发 prepare，preparing 事件不丢失（订阅先就绪）。属**集成/时序测试**（事件循环编排「先消费后 publish」），非纯单元测试。
4. Kernel 不可用时：prepare 不阻断、发布 degraded、前端展示降级提示。
5. `ensure_ready` 幂等：已索引 workspace 只 sync，不重复 init（现有 `test_ensure_ready_same_workspace_singleflight` 已覆盖）。
6. 单元测试覆盖（独立测试确认的可测清单，落三个测试文件）：
   - `tests/test_workspace_index_bus.py`：路由/关闭/哨兵/幂等 close/空 id 校验/非法 event_type/QueueFull 丢弃。
   - `tests/test_workspace_index_service.py`：`prepare` 三分支（ready/degraded/不可用）+ 字段映射 + 终态可达（真实 bus 集成）。
   - `tests/test_workspace_index_api.py`：SSE 帧格式/终态 break/finally unsubscribe + prepare 404 + Kernel 不可用降级。
7. 独立审查 + 独立测试闭环通过。

## 九、开放项 / 待拍板

1. ~~两段式 vs 方案 B~~ **已拍板：两段式**（用户 2026-08-04：create 后可能还有其他操作，两段式便于扩展）。
2. **持久化**：workspace 索引状态是否要落库（供刷新后回显）？本方案仅事件流推送，不落库（对齐「preparing 不落库」既有决策）。刷新后需重新 ensure_ready 或查 status。
3. **`GET /workspaces/{id}/index/stream` 是否保留**：两段式下创建进度走独立 SSE 端点（必要）；是否还需一个「纯状态查询」端点（查某 workspace 当前索引状态）供 UI 回显，待定。

## 十、明确不做

- **不新建事件类型与 payload 模型**（复用现有 `WORKSPACE_PREPARING/READY/DEGRADED` 与三个 `Workspace*Payload`）；但需**新建一个信封值对象 `WorkspaceIndexEvent`**（放 `models/`，非 `models/payload/`），它承载 event_id/workspace_id/path/payload，不是 payload 模型也不是新 EventType。
- 不改 vendor `lifecycle-service.ts`（除非「索引根不推导」另行拍板落地）。
- 不做「创建后异步后台预热」（本方案是同步阻塞）。
- 不做 workspace 索引状态持久化/回显（留给后续）。
- 不做方案 B（POST /workspaces 返回 SSE）——已回退，两段式便于 create 后扩展其它操作。

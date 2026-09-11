# CodeGraph 第二阶段技术方案（一）：Workspace 索引生命周期

> 状态：**待评审**
> 日期：2026-08-04
> 上游文档：`codegraph-integration-discussion.md`（集成方向讨论）、`codegraph-agent-kernel-design.md`（第一阶段：Kernel 常驻）
> 本文范围：第二阶段的 **Workspace 索引生命周期**（`status` / `init` / `sync`）。Agent 可见的 6 个 `codegraph_*` 工具面、`TaskService` 接入、前端 UI 不在本文，另文承载。

---

## 一、本文要解决的问题

第一阶段打通了「后端常驻 Kernel + 只读查询」。但只读查询有一个前提没人负责：

**这个 workspace 的索引存在吗？是最新的吗？**

第一阶段的答案是「假设已经有索引」——`tool-service` 遇到没索引的 workspace 只能返回 `WORKSPACE_NOT_INDEXED`。第二阶段必须补上这个缺口：在 Agent 开始干活之前，把 workspace 的索引准备好。

这就是 **Workspace 索引生命周期**。

---

## 二、三层生命周期的边界（先对齐概念）

「生命周期」在本项目里有三套，互不相同，不能混谈：

| 层 | 粒度 | 状态 | 归属 |
|----|------|------|------|
| ① Kernel 进程生命周期 | 应用级 | stopped/starting/ready/degraded/restarting/failed/stopping | **第一阶段已落地**（`CodeGraphKernelSupervisor`） |
| ② **Workspace 索引生命周期** | workspace 级 | unknown/checking/unindexed/indexing/syncing/ready/failed | **本文** |
| ③ Agent 工具调用 | task/turn 级 | —— | 第二阶段另文（工具面） |

②只在①为 `ready` 时才有意义；③只消费②为 `ready` 的 workspace。

---

## 三、决定实现形态的上游事实（已核实）

以下均从 vendored 源码核实，不是推测。

### 3.1 `CodeGraph` 主类的索引 API（`apps/codeIndex/src/index.ts`）

| 动作 | 上游 API | 行号 | 说明 |
|------|---------|------|------|
| 创建索引 | `static async CodeGraph.init(projectRoot, options)` | 240 | 创建 `.codegraph/` + 库；**仅当 `options.index === true` 才首次索引**（见 3.1.1） |
| 打开已存在索引 | `static async CodeGraph.open(projectRoot, options)` | 296 | **不创建**，只打开；未初始化则抛错（301） |
| 打开（同步） | `static CodeGraph.openSync(projectRoot)` | 377 | 同上 |
| 全量索引 | `async cg.indexAll(options)` | 432 | 内部 `indexMutex.withLock` |
| 增量同步 | `async cg.sync(options)` | 643 | 内部 `indexMutex.withLock` |
| 索引状态 | `cg.getIndexState()` | 941 | `'indexing' \| 'complete' \| 'partial' \| 'failed' \| null` |
| 最近索引时间 | `cg.getLastIndexedAt()` | 928 | `number \| null` |
| 是否已初始化 | `static CodeGraph.isInitialized(projectRoot)` | 402 | 判断 `.codegraph/` 是否已建立 |
| 向上查找索引根 | `findNearestCodeGraphRoot(from)`（`src/directory`） | —— | 上游 `MCPEngine`（`engine.ts:15,161,204`）用它解析 root |
| 实例级并发锁 | `private indexMutex = new Mutex()` | 149 | **实例级，非进程级** |

#### 3.1.1 `init` 的两个致命细节（必须按此实现）

```249:265:apps/codeIndex/src/index.ts
    // Check if already initialized
    if (isInitialized(resolvedRoot)) {
      throw new Error(`CodeGraph already initialized in ${resolvedRoot}`);
    }
    ...
    // Run initial indexing if requested
    if (options.index) {
      await instance.indexAll({ onProgress: options.onProgress });
    }
```

- **`InitOptions.index` 默认 `undefined`**（`index.ts:97-103`），即 `CodeGraph.init(root)` **只建空库、不产生任何索引数据**。必须显式传 `{ index: true }`，否则会出现「init 成功但状态仍为 unindexed」的死循环。
- **`init` 对已初始化目录直接抛异常**（245-247）。因此「`.codegraph/` 已存在但库里无索引记录」的场景**不能走 `init`**，必须走 `open` + `indexAll`。

### 3.1.2 `IndexResult` / `SyncResult` 字段异构（`src/extraction/index.ts:85-115`）

| 类型 | 字段 |
|------|------|
| `IndexResult`（indexAll） | `success / filesIndexed / filesSkipped / filesErrored / filesDiscovered? / nodesCreated / edgesCreated / errors / durationMs` |
| `SyncResult`（sync） | `filesChecked / filesAdded / filesModified / filesRemoved / nodesUpdated / durationMs / changedFilePaths?` |

两者**没有共同的 `filesProcessed` 字段**，且语义不可互换。协议层的字段映射必须显式定义（见 5.4），不能留给实现者临场挑字段。两者均自带 `durationMs`，Kernel 侧**不再重复计时**。

### 3.2 `MCPEngine` 不能承担 init（`src/mcp/engine.ts`）

`MCPEngine.doInitialize` 只做 `findNearestCodeGraphRoot` → `CodeGraph.open` → `startWatching` → `catchUpSync`。找不到 `.codegraph/` 时**直接返回**（`cg = null`），**它永远不会创建索引**。

且 `MCPEngine` 是「单 default project」设计：`ensureInitialized(searchFrom)` 只决定 default project；跨 workspace 查询是靠 `ToolHandler.projectCache` 路由的（第一阶段判断二修订），而 `projectCache` 只服务查询，不暴露写入型生命周期动作。

**结论：init/sync/status 必须走一条独立于 `MCPEngine` 的新通道。**

### 3.3 后端 Client 已支持逐次超时

`CodeGraphKernelClient.call(method, params, timeout)` 的 `timeout` 参数可逐次覆盖默认 30s（`client.py:74-119`）。**`init` 的长超时无需改动第一阶段 Client。**

---

## 四、已拍板的决策

| # | 决策 | 结论 | 依据 |
|---|------|------|------|
| D1 | 实现形态 | **形态 A**：Kernel 新增写入型 `lifecycle-service.ts` + 后端 `CodeGraphLifecycleService` 编排 | 唯一能复用上游 `CodeGraph.init/sync` 全部能力、且不污染上游 `mcp/` 的路径 |
| D2 | 新鲜度策略 | **已索引 → 每次 task 启动前显式 `sync`（阻塞）；未索引 → `init`** | 非 default workspace 无 watcher，不能靠 pending/时间戳判新鲜度；且绕开「阈值怎么定」的难题 |
| D3 | `status` 形态 | **显式 `codegraph_status` RPC**（非探测式错误分支） | 状态机由 status 驱动，清晰可测；第三阶段前端要展示索引状态，迟早需要 |
| D4 | 进度回传 | `init`/`sync` 用**单次阻塞 RPC**（完成才返回摘要）；进度仅经结构化日志回传，**不发 `RuntimeEvent` 事件**（事件化推迟至 `TaskService` 接入，见 6.5） | 不破坏第一阶段 JSON-line 单行 request/response 协议形态 |
| D5 | singleflight | **后端按 `workspace_path` 做 singleflight**，不依赖上游 | 上游 `indexMutex` 是实例级，Kernel 单进程多 workspace 时不同实例无共享锁 |
| D6 | `init` 失败策略 | **放行 task + 降级到文件搜索**，但日志/事件明确标记 CodeGraph 不可用 | 讨论文档默认；不阻塞用户 |
| D7 | 删除/重建 | 第二阶段**完全不碰** `uninitialize` / `rebuild` / `unlock` | 讨论文档安全规则：不允许自动删除 `.codegraph/` |

### 尚未拍板（本文遗留，需在实现前定）

- **D8：`init` 的阻塞模型** —— (a) 同步阻塞调用线程 vs (b) task 先建 `preparing` 态、异步准备完再启动 Agent。
  本文的 `CodeGraphLifecycleService` 接口对两者**都兼容**（`ensure_ready` 是**同步阻塞**语义，调用方决定在哪个线程/任务里调用——同步 RPC 阻塞自然可放进线程池/后台任务等待）。该决策影响的是 **`TaskService` 接入方式**，归属第二阶段另文，不阻塞本文实现。

---

## 五、Kernel 侧设计（vendor `agent-kernel/`）

### 5.1 新增文件与职责

```text
apps/codeIndex/src/agent-kernel/
  protocol.ts           # [改] 新增 3 个 method + 结果类型 + 2 个错误码
  workspace-service.ts  # [不改] 单 MCPEngine 持有者（只读底座）
  tool-service.ts       # [不改] 只读查询分发
  lifecycle-service.ts  # [新增] 写入型索引生命周期：status/init/sync
  server.ts             # [改] 新增 3 个 method 的路由分支
```

**职责分离铁律**：`tool-service` 只读、`lifecycle-service` 写入。二者不互相调用，各自独立持有到上游的入口。

### 5.2 `lifecycle-service.ts` 接口

```ts
/**
 * 写入型索引生命周期服务：status / init / sync。
 * 不负责：只读查询（归 tool-service）、进程生命周期（归 server）、
 *        删除与重建（第二阶段不提供）。
 */
export class LifecycleService {
  /** 已打开的 CodeGraph 实例缓存，键为规范化 workspace_path。 */
  private readonly instances = new Map<string, CodeGraph>();

  async status(workspacePath: string): Promise<IndexStatusPayload>;
  async init(workspacePath: string): Promise<IndexActionPayload>;
  async sync(workspacePath: string): Promise<IndexActionPayload>;
}
```

**为什么 `lifecycle-service` 自持 `instances` 而不复用 `ToolHandler.projectCache`**：
`projectCache` 是上游 `ToolHandler` 的私有实现细节，无对外的「取实例」API；从只读通道里掏实例来做写入操作会把两条通道耦死，且违反窄适配纪律。自持一个 `Map` 是 ~10 行代码，边界干净。

> **已知取舍（含 SQLite 并发）**：同一 workspace 可能在 `projectCache`（查询用）与 `instances`（写入用）各有一个 `CodeGraph` 实例，二者对**同一个 SQLite 文件**持有独立 `DatabaseConnection`（`index.ts:313`）。上游 `indexMutex` 只保护同实例并发；后端 singleflight（D5）只去重 `ensure_ready` 调用。**二者都挡不住「A workspace 正在 init/sync」与「Agent 同时查询 A workspace」的读写并发** —— 而 D2「每次 task 启动前 sync」会让这种并发常态化。
>
> 处理方式：`lifecycle-service` 的 `init`/`sync` 必须捕获 SQLite 忙碌错误（`SQLITE_BUSY` / `database is locked`）并映射为 `INDEX_LOCKED`（`retryable: true`），由后端按可重试语义降级。**实现 L3 时必须先核实上游 `DatabaseConnection` 是否启用 WAL 与 `busy_timeout`**；若未启用，需在 `lifecycle-service` 打开实例时显式设置（这属于必要的正确性兜底，不是造轮子）。

### 5.3 各方法的上游映射

**root 解析必须与只读通道一致**：上游全链路（`MCPEngine.doInitialize` `engine.ts:204`、`ToolHandler` `tools.ts:1074`）用 `findNearestCodeGraphRoot(searchFrom)` **向上查找**最近的 `.codegraph/`。若 `lifecycle-service` 改用「`workspacePath` 下精确存在性」判定，在 monorepo 子目录场景下（索引建在仓库根）会解析到不同 root，导致**在子目录建第二份索引，而查询走仓库根那份**。故：

```ts
// 三个方法统一先解析 root，与只读通道语义对齐
const resolvedRoot = findNearestCodeGraphRoot(workspacePath);   // null 表示向上都没有索引
```

| RPC method | `lifecycle-service` 实现 | 上游调用 |
|-----------|-------------------------|---------|
| `codegraph_status` | 解析 root，无 root 直接返回；有 root 才打开实例读状态 | `findNearestCodeGraphRoot` → `CodeGraph.open` → `getIndexState()` + `getLastIndexedAt()` |
| `codegraph_init` | **按是否已初始化分流**（见下） | `CodeGraph.init(path, { index: true })` 或 `open` + `indexAll()` |
| `codegraph_sync` | 增量同步 | `open`（或取缓存实例）→ `cg.sync()` |

`status` 判定（无 root 时**不打开实例**，避免无谓 IO）：

```text
resolvedRoot = findNearestCodeGraphRoot(workspacePath)
if (resolvedRoot === null) → { state: 'unindexed', last_indexed_at: null }
else:
  cg = openOrCached(resolvedRoot)
  raw = cg.getIndexState()            // 'indexing'|'complete'|'partial'|'failed'|null
  归一化：
    null      → 'unindexed'   （目录在但库里没索引记录）
    'indexing'→ 'indexing'
    'complete'→ 'ready'
    'partial' → 'ready'       （可用但不完整，后端会 sync；不阻断）
    'failed'  → 'failed'
  → { state, last_indexed_at: cg.getLastIndexedAt() }
```

`init` 的分流（吸收 3.1.1 的两个上游细节，**让后端编排保持单一 `init` 语义**）：

```text
init(workspacePath):
  resolvedRoot = findNearestCodeGraphRoot(workspacePath) ?? workspacePath
  if (!CodeGraph.isInitialized(resolvedRoot)):
      cg = await CodeGraph.init(resolvedRoot, { index: true })   // 必须显式 index:true
      result = 该次 indexAll 的 IndexResult                        // 见下方注
  else:
      cg = openOrCached(resolvedRoot)                            // 目录在但无索引记录
      result = await cg.indexAll()
  → { state: 'ready', files_indexed: result.filesIndexed, duration_ms: result.durationMs }
```

> 注：`CodeGraph.init({index:true})` 返回的是实例而非 `IndexResult`。为拿到摘要，`init` 分支实现为 `CodeGraph.init(root)`（不带 index）+ `await cg.indexAll()`，两步等价且能取到 `IndexResult`。这样两个分支收敛为同一行 `indexAll()`，逻辑更简。

### 5.4 协议扩展（`protocol.ts`）

```ts
// MethodName 新增
| 'codegraph_status'
| 'codegraph_init'
| 'codegraph_sync'

// KernelErrorCode 新增
INDEXING_FAILED = 'INDEXING_FAILED',   // init/sync 执行失败（retryable: true）
INDEX_LOCKED    = 'INDEX_LOCKED',      // 索引被占用（另一进程/实例持锁，retryable: true）

/** 归一化索引状态（status 与 action 共用，避免两侧取值域漂移）。 */
export type IndexState = 'unindexed' | 'indexing' | 'ready' | 'failed';

/** codegraph_status 响应。 */
export interface IndexStatusPayload {
  state: IndexState;
  /** 最近一次索引完成时间戳（ms）；未索引为 null。 */
  last_indexed_at: number | null;
}

/**
 * codegraph_init 响应。字段直取上游 IndexResult，不做二次加工。
 */
export interface IndexInitPayload {
  state: IndexState;              // 正常为 'ready'
  files_indexed: number;          // IndexResult.filesIndexed
  duration_ms: number;            // IndexResult.durationMs（上游自带，不重复计时）
}

/**
 * codegraph_sync 响应。SyncResult 与 IndexResult 字段异构（见 3.1.2），
 * 故分成两个类型，不强行压成一个 files_processed。
 */
export interface IndexSyncPayload {
  state: IndexState;              // 正常为 'ready'
  files_added: number;            // SyncResult.filesAdded
  files_modified: number;         // SyncResult.filesModified
  files_removed: number;          // SyncResult.filesRemoved
  duration_ms: number;            // SyncResult.durationMs
}
```

`PROTOCOL_VERSION` 从 `1.0.0` → **`1.1.0`**（新增方法与错误码，向后兼容旧 client 的既有调用）。

> ⚠️ 第一阶段 `client.hello()` 做的是**精确相等**校验（`hello.protocol_version != PROTOCOL_VERSION` 即拒绝）。因为后端与 Kernel 是同仓库同版本发布，**保持精确相等即可**，两侧常量必须同一次提交内同步改。不引入 semver 兼容区间（YAGNI）。

### 5.5 `server.ts` 路由

现有 `server.ts` 用 `ToolService.isQueryMethod(method)` 判定只读查询。新增分支：

```text
kernel.*                      → 握手/健康/关闭（既有）
LifecycleService.isLifecycleMethod(method) → lifecycleService.handle(method, params)   // 新增
ToolService.isQueryMethod(method)          → toolService.handle(...)（既有）
其他                           → INVALID_REQUEST（既有）
```

生命周期分支必须放在查询分支**之前**判定（两者方法名前缀同为 `codegraph_`，避免误落只读通道）。

---

## 六、后端侧设计（`CodeGraphLifecycleService`）

### 6.1 落点与分层

```text
apps/backend/app/service/codegraph/
  lifecycle_service.py    # [新增] CodeGraphLifecycleService：编排（本文核心）
  inflight_registry.py    # [新增] 按 key 去重的进行中调用注册表（通用并发原语，~30 行）
apps/backend/app/models/workspace_index_readiness.py   # [新增] 准备结果值对象

apps/backend/app/tools/codegraph/     # 第一阶段薄层，本文只扩协议与客户端
  protocol.py       # [改] 镜像新增 method 常量、错误码、IndexStatus/Init/Sync 值对象
  exceptions.py     # [改] 新增 2 个异常类 + error_from_code 映射补齐
  client.py         # [改] 新增 index_status/index_init/index_sync（对齐既有 query()）
  supervisor.py     # [不改]
  node_resolver.py  # [不改]
```

目录/文件命名对齐既有范式（`service/runtime_event/runtime_event_service.py`、`service/tool_execution/tool_execution_service.py`）：**目录名承载领域，文件名不重复目录名**，故为 `codegraph/lifecycle_service.py` 而非 `codegraph/codegraph_lifecycle_service.py`（类名仍为 `CodeGraphLifecycleService`）。未使用 `manager`/`helper`/`utils` 等模糊词。

**为什么 `CodeGraphLifecycleService` 放 `service/` 而非 `tools/codegraph/`**：

- 它是 **workspace/task 启动的编排动作**，调用方是 `TaskService`（第二阶段另文），与之同层协作最自然；`AGENTS.md` 分层允许 `service → tools`，方向合法。
- `tools/codegraph/` 应保持「与 Kernel 通信的薄层」单一职责：协议 + 进程 + RPC 客户端 + （另文的）工具 handler。把「多步编排 + 降级判定 + 与 task 启动流程耦合」的逻辑放进 `tools/` 会让该层承担新职责（防膨胀第三章）。
- 补充：本文最终**不在本层发 `RuntimeEventBus` 事件**（见 6.5 修订），故落点论证不依赖事件通道。

### 6.2 接口

```python
class CodeGraphLifecycleService:
    """Workspace 索引生命周期编排：确保 workspace 在 Agent 使用前索引就绪。

    职责边界：
        - 负责：把 status 结果编排为 init/sync 动作，并把失败归一化为降级结果。
        - 不负责：并发去重（组合 InflightRegistry）、Kernel 进程管理（归 Supervisor）、
                 RPC 细节（归 Client）、索引算法（归上游 CodeGraph）、
                 task 状态机与事件发布（归 TaskService，第二阶段另文）。
    """

    def __init__(self, client: CodeGraphKernelClient, inflight: InflightRegistry) -> None: ...

    def ensure_ready(self, workspace_path: str) -> WorkspaceIndexReadiness:
        """确保 workspace 索引就绪（阻塞至完成）。绝不抛异常，失败以降级结果返回。"""
```

**singleflight 抽为独立组件**（`inflight_registry.py`）：按 key 去重的进行中调用注册表是**与 CodeGraph 领域无关的通用并发原语**，混进领域编排类会让该类同时承担并发控制与业务编排（多职责）。拆开后两者可各自独立单测（对应验收标准 3 与 5）。

```python
class InflightRegistry:
    """按 key 去重「进行中」的调用：同 key 并发只执行一次，其余等待同一结果。

    不负责：结果缓存（调用结束即移除 key）、key 的语义（由调用方规范化）。
    """
    def run(self, key: str, fn: Callable[[], T]) -> T: ...
```

返回值（新增值对象 `app/models/workspace_index_readiness.py`）：

```python
@dataclass(frozen=True)
class WorkspaceIndexReadiness:
    """workspace 索引准备结果。"""
    ready: bool                  # True=CodeGraph 可用；False=降级到文件搜索
    state: str                   # 'ready' | 'failed' | 'unavailable'
    action_taken: str            # 'none' | 'init' | 'sync'
    files_changed: int           # init=filesIndexed；sync=added+modified+removed；未执行为 0
    duration_ms: int             # 本次准备总耗时（后端侧计时，含 RPC 往返）
    degraded_reason: str | None  # ready=False 时的英文原因（供日志与系统提示消费）
```

`files_changed` 是**后端层为 UI/日志做的单一数字摘要**，其来源在此显式固定（init → `files_indexed`；sync → `files_added + files_modified + files_removed`），不留给实现者临场挑字段。协议层仍保持 init/sync 两个异构 payload（5.4），不在协议层压扁。

**`ensure_ready` 绝不抛异常**（D6 降级策略的直接体现）：Kernel 不可用、init 失败、超时，一律转为 `ready=False` + `degraded_reason`，由调用方决定是否放行 task。这样 `TaskService` 接入点不需要写 try/except 分支，降级路径唯一。

### 6.3 编排逻辑

```text
ensure_ready(workspace_path):
  ├─ key = 规范化路径；inflight.run(key, _prepare)      # 并发去重，见 6.4
  │
  └─ _prepare():
      ├─ log.info('codegraph_workspace_preparing', data={workspace_path})
      │
      ├─ status = client.index_status(path)              # 短超时（默认 30s）
      │    Kernel 不可用 / 超时 → degraded('kernel unavailable: ...')
      │
      ├─ match status.state:
      │    'unindexed' → client.index_init(path, timeout=INIT_TIMEOUT)   # 长超时
      │                  失败 → degraded('index init failed: ...')
      │                  → action='init'
      │
      │    'ready'     → client.index_sync(path, timeout=SYNC_TIMEOUT)   # D2：已索引也总是 sync
      │                  失败 → degraded('index sync failed: ...')        # 注①
      │                  → action='sync'
      │
      │    'indexing'  → degraded('index is being built by another process')  # 注②
      │
      │    'failed'    → degraded('index is in failed state; rebuild required')  # 注③
      │
      └─ log.info('codegraph_workspace_ready', ...) → return ready(...)
```

- **注①**：`sync` 失败时索引其实**仍可用（只是陈旧）**。但第二阶段不做「陈旧可用」这个中间态——多一个状态就多一条前端/提示分支，YAGNI。统一降级，语义简单可预期。若实测 sync 失败频繁，再单独提案。
- **注②**：`indexing` 说明另有进程正在建索引（本进程的并发已被 singleflight 挡住）。第二阶段**不等待、不轮询**，直接降级；等待策略需要轮询间隔/上限/取消三套参数，属过度设计。
- **注③**：`failed` 需要 rebuild，而 D7 明确第二阶段不做 rebuild。降级并在 `degraded_reason` 里明示需要重建，引导用户走（第三阶段的）手动入口。

超时常量入 `Settings`（对齐既有配置约定，不散落魔法数）：

```python
CODEGRAPH_INDEX_INIT_TIMEOUT_SECONDS: ClassVar[float] = 600.0   # 首次建索引，大仓库可能数分钟
CODEGRAPH_INDEX_SYNC_TIMEOUT_SECONDS: ClassVar[float] = 120.0   # 增量同步
```

### 6.4 singleflight（D5）

同一 workspace 的并发 `ensure_ready` 只真跑一次，其余等待同一结果：

由 `InflightRegistry.run(key, fn)` 承担（6.2）。实现要点：

```python
self._inflight: dict[str, Future[Any]]
self._lock: threading.Lock

# 认领：锁内查 dict，命中则拿 Future 出锁后 .result()；
#      未命中则放入自己的 Future、出锁执行、finally 里 set_result + 从 dict 移除。
```

- 键用**规范化后的绝对路径**（`os.path.normcase(os.path.realpath(path))`），避免 `C:\X` 与 `c:\x\` 被当作两个 workspace。Windows 下 `realpath` 对不存在的路径行为与 POSIX 有差异，需 fallback 到 `os.path.abspath`。
- **锁内只做 dict 存取，不做 RPC**（RPC 可能阻塞数分钟，持锁会把所有 workspace 一起卡死）。
- `finally` 必须移除 key，否则失败结果会被永久缓存（验收标准 4 须验证无 key 泄漏）。
- 不缓存历史结果：每次 task 启动都要重新 `sync`（D2），本组件只去重「同时进行中的」调用，不是结果缓存。

### 6.5 可观测性：本阶段只发日志，不发 `RuntimeEvent`

> **修订说明（评审后）**：初稿计划在本层经 `RuntimeEventBus` 发 5 个准备阶段事件。核实既有契约后该方案**不成立**：
>
> - `RuntimeEvent.task_id` 是必填字段（`models/runtime_event.py:44`）；
> - `RuntimeEventBus.publish` 开头即 `if event.turn_id is None: return`（`runtime_event_bus.py:161-162`），**无 turn_id 的事件被静默丢弃**；
> - `RuntimeEvent.__post_init__` 强制按 `EventType` 校验 payload 模型，`EventType` 当前无任何 workspace 相关成员。
>
> 而 `ensure_ready(workspace_path)` 本身没有 task_id/turn_id —— 其来源在 `TaskService` 接入（第二阶段另文；`ensure_ready` 的接口层同步语义已定，但 `TaskService` 采用哪种接入方式仍属 D8 待定）。在本文范围内强行发事件必然是静默失败。

因此本阶段**只输出结构化日志**，事件化随 `TaskService` 接入一并落地（已列入第八节「明确不做」）。

由于 `ensure_ready` **绝不抛异常**（6.2），失败信息只能靠日志暴露，故日志约定是本设计的必需项而非可选项：

| 稳定 event 名 | 级别 | data 字段 |
|--------------|------|----------|
| `codegraph_workspace_preparing` | info | `workspace_path` |
| `codegraph_workspace_ready` | info | `workspace_path` / `action_taken` / `files_changed` / `duration_ms` |
| `codegraph_workspace_degraded` | **warning** | `workspace_path` / `state` / `degraded_reason` / `duration_ms` |

对捕获到的异常统一用 `log.exception` 保留堆栈，`degraded_reason` 只放面向人/模型的英文摘要（对齐既有 `ToolObservation.error` 的英文约定），不塞堆栈。

**进度百分比第二阶段不做**：D4 单次阻塞 RPC 中途拿不到进度；上游 `IndexOptions.onProgress` 虽存在，但跨 RPC 回传需要 notification 通道，超出本文范围。

---

## 七、任务拆分

| # | 任务 | 产出 | 依赖 |
|---|------|------|------|
| L1 | 协议扩展（**两侧必须同一次提交**） | `protocol.ts` + `protocol.py`：3 method、2 错误码、`IndexState`/`IndexStatusPayload`/`IndexInitPayload`/`IndexSyncPayload`、版本 1.1.0 | —— |
| L2 | 异常扩展 | `exceptions.py`：`CodeGraphIndexingFailedError` / `CodeGraphIndexLockedError` + `error_from_code` 映射 | L1 |
| L3 | Kernel `lifecycle-service.ts` | status/init/sync（含 `findNearestCodeGraphRoot` root 解析、`isInitialized` 分流、`SQLITE_BUSY`→`INDEX_LOCKED`）+ `isLifecycleMethod` + 实例缓存 | L1 |
| L4 | `server.ts` 路由 | 生命周期分支（置于查询分支前） | L3 |
| L5 | Client 薄封装 | `client.py`：`index_status/index_init/index_sync`（对齐既有 `query()` 形态，逐次超时） | L1 L2 |
| L6 | 值对象 | `app/models/workspace_index_readiness.py` | —— |
| L7 | `InflightRegistry` | 按 key 去重的进行中调用注册表（通用并发原语，独立单测） | —— |
| L8 | `CodeGraphLifecycleService` | 编排 + 降级 + 结构化日志（组合 L7） | L5 L6 L7 |
| L9 | 配置 | `Settings` 两个超时常量 | —— |

> **L1 硬约束**：`PROTOCOL_VERSION` 在 `protocol.ts` 与 `protocol.py` 两侧必须同一次提交内改（`client.hello()` 是精确相等校验，分两次提交会让 Kernel 直接握手失败、整个 CodeGraph 能力不可用）。

**验收标准**（端到端，在真实第二 workspace 上验证）：

1. 无索引的 workspace → `ensure_ready` 触发 `init` → `ready=True, action_taken='init'`，且 `.codegraph/` 真实产出并**含非空索引数据**（`status` 复查为 `ready`，`files_changed > 0`）——专门覆盖「`init` 未传 `index:true` 会建空库」的坑。
2. `.codegraph/` 已存在但库内无索引记录 → 走 `open + indexAll` 分支，**不得触发上游 `already initialized` 异常**。
3. 已索引 workspace → `ensure_ready` 触发 `sync` → `ready=True, action_taken='sync'`。
4. monorepo 子目录作为 workspace_path（索引在仓库根）→ 解析到仓库根，**不在子目录新建第二份索引**。
5. 并发 5 次 `ensure_ready` 同一 workspace → Kernel 侧只观察到 1 次 `init`（去重生效）。
6. Kernel 停止后调用 `ensure_ready` → `ready=False`，**不抛异常**，`degraded_reason` 可读，且 `InflightRegistry` **无 key 泄漏**。
7. 单测覆盖：状态归一化映射、编排各分支、去重、降级路径（不依赖真实 Kernel，Client 打桩）。

---

## 八、明确不做（第二阶段本文范围外）

| 不做 | 原因 |
|------|------|
| `rebuild` / `unlock` / `uninitialize` | D7：安全规则禁止自动删除 `.codegraph/`；留第三阶段用户手动入口 |
| 索引进度百分比 | D4 单次阻塞 RPC 拿不到；上游进度回调未确认 |
| `indexing` 状态的等待/轮询 | 需要轮询间隔/上限/取消三套参数，过度设计 |
| 「陈旧但可用」中间态 | 多一状态多一堆分支，YAGNI |
| watcher / 自动增量 | 非 default workspace 无 watcher（第一阶段判断二已知取舍）；D2 用「每次 task 前 sync」替代 |
| **准备阶段 `RuntimeEvent` 事件** | 现有事件契约要求 task_id/turn_id（`publish` 会静默丢弃无 turn_id 的事件），而本层没有该上下文；随 `TaskService` 接入一并落地。本阶段用结构化日志替代（6.5） |
| `TaskService` 接入与 task 状态机 | 归第二阶段另文（D8 未定，不阻塞本文） |
| Agent 可见的 6 个 `codegraph_*` 工具 | 归第二阶段另文 |
| 前端索引状态 UI | 第三阶段 |

> 事件化落地时需一并改动四处，不可遗漏：`EventType` 新增成员 → `models/payload/` 新 payload → `payload/registry/` 登记 → `apps/shared/ts/events.ts` 同步（`RuntimeEventType` 联合类型）。

---

## 九、待确认

1. **D8（`init` 阻塞模型）** —— 同步阻塞 vs task `preparing` 态异步准备。不阻塞本文实现，但在 `TaskService` 接入前必须定。
2. **协议版本策略** —— 本文按「精确相等 + 两侧同提交同步改」处理（5.4）。若你希望支持 Kernel/后端版本漂移，需改 `client.hello()` 的校验逻辑，请明示。

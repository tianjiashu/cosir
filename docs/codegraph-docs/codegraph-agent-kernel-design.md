# CodeGraph 融入 coding-agent · 第一阶段技术方案（Kernel 常驻）

本文档从 `codegraph-integration-discussion.md` 收敛而来，聚焦**第一阶段唯一目标：把内置 CodeGraph Kernel 常驻进程跑起来，并能通过一个 workspace 的索引完成一次 `explore` 查询**。此目标达成前，不展开 LifecycleService、Agent 工具面、前端 UI 等工作。

本文档是讨论结论的固化，不是全部设计的终点。后续阶段的 LifecycleService / Agent 工具 / 前端 UI 设计在本文档第五节预留接口，不在此展开。

## 一、范围

### 第一阶段必须交付（MVP）

1. `third_party/codegraph/src/agent-kernel/` 新建常驻适配层，在 `MCPEngine` 之上封装 stdio JSON-line RPC 服务。
2. vendor 侧新增最小 `tsc` 构建配置，产出 `dist/agent-kernel/server.js`（不破坏现有 `dist/` 构建）。
3. 后端 `CodeGraphKernelSupervisor`：从项目内固定目录解析锁定的 `node` 可执行文件，启动 Kernel 子进程，管理握手、健康检查、重启、关闭、stderr 桥接。
4. 后端 `CodeGraphKernelClient`：通过子进程 stdin/stdout 发送 JSON-line RPC、管理 `request_id`、超时、取消、协议错误转换。
5. 端到端验证：后端启动 Kernel → 握手 → 对当前仓库（已有 `.codegraph/`）发起一次 `codegraph_explore` 查询 → 收回结构化/文本结果。

### 第一阶段不做（明确排除）

- `CodeGraphLifecycleService` 的 `status/init/sync/rebuild/unlock` 编排（第二阶段）。
- Agent 可见的 6 个 `codegraph_*` 工具定义与接入（第二阶段）。
- `WorkspaceManager` 与 `TaskService` 的启动前 `ensure_ready` 接入（第二阶段）。
- 前端 workspace UI 的索引状态展示（第三阶段）。
- Kernel 跨平台打包进桌面安装包（最后阶段；第一阶段锁定路径但可暂用本地固定目录验证）。

## 二、两个核心生产判断（基于项目事实）

### 判断一：构建形态 —— 编译成 `dist` 后跑，锁定内置 `node`

**事实依据：**

- `third_party/codegraph/package.json` 已是标准 `tsc` 工程：`"build": "tsc && npm run copy-assets ..."`，`main: dist/index.js`，`bin: dist/bin/codegraph.js`，`engines.node: ">=20.0.0 <25.0.0"`。新增 `dist/agent-kernel/server.js` 只是扩展现有构建，不引入新范式。
- 项目 `AGENTS.md` 的 CodeGraph 使用规则明确：CodeGraph 对 Node 25/26 有已知拦截风险，应锁定 Node 22 LTS。这与 `engines` 约束一致。
- 后端已有成熟的子进程管理范式：`tool_executor.py`（multiprocessing 子进程 + 超时强杀 + Windows Job Object 树杀）、`terminal/local_backend.py`、`config/logging/process_bridge.py`（跨进程队列桥）。
- 桌面端 `src-tauri/src/backend/runtime_locator.rs` 已有「从项目内固定目录解析运行时、不读用户 PATH」的成熟范式（`resolve_python_binary` 从 `apps/backend/.venv` 解析）。

**结论：** 第一版 Kernel **必须编译成 `dist` 后由锁定的 `node` 运行**，不使用 `tsx` 直跑 TS（`tsx` 每次冷启动有 JIT 编译开销，违背「应用级预热常驻」目标，且需额外打包 tsx 依赖）。`node` 可执行文件由后端从项目内固定资源目录解析，缺失即返回结构化错误，绝不回退到用户 `PATH`。

**Node 版本锁定：** Node 22 LTS（满足 `engines >=20 <25`，避开 25/26 拦截风险）。

### 判断二：workspace 关联 —— 按请求带 `workspace_path`，常驻 Kernel 多 workspace 懒加载

**事实依据：**

- 讨论文档核心架构：Kernel 是**应用级**常驻，索引是 **workspace 级**；`MCPEngine` 设计意图是「one engine, many sessions」，direct mode 即单 stdio 会话按路径找索引。
- 本项目产品模型：`workspace → task1..N`，多 task 共享同一 workspace 索引。若启动即固定单一 workspace，等于每 workspace 起一个进程，违背「一个常驻 Kernel 服务多 workspace」。
- 但 `MCPEngine.ensureInitialized(searchFrom)` 缓存的是**单个 default project**（`this.cg` 唯一），并非多 workspace 映射。因此适配层必须自行维护 workspace 到 engine 的映射。

**结论：** Kernel 单进程应用级常驻；`agent-kernel` 内部维护 `Map<workspace_path, MCPEngine>`；每个 RPC 请求带 `workspace_path`，适配层路由到对应 engine 实例并懒加载/复用其索引。同一 workspace 的索引初始化必须 singleflight（并发请求只触发一次 `ensureInitialized`）。

## 三、agent-kernel 适配层设计（vendor 内新建）

位置：`third_party/codegraph/src/agent-kernel/`，严格作为窄适配层，不修改 `mcp/`、`index.ts` 等上游核心（遵守 `UPSTREAM.md` 约定）。

### 3.1 文件职责

- `server.ts` —— 进程入口。解析启动参数（如 `--port` 不用，默认 stdio）、实例化 `RpcServer`，注册 `workspace-service` 与 `tool-service`，监听 stdin JSON-line，写 stdout JSON-line，stderr 只写内部日志。
- `protocol.ts` —— 定义 RPC 消息形状：`RpcRequest { id, method, params }`、`RpcResponse { id, result?, error? }`、`KernelError` 错误码枚举、`handshake` 协议类型。纯类型 + 序列化辅助，无副作用。
- `workspace-service.ts` —— 封装 `Map<workspace_path, MCPEngine>` 的管理：按 `workspace_path` 取/建 `MCPEngine`、`ensureInitialized` singleflight、`getStatus`（是否已索引/是否 ready）、`stop` 全部。不直接理解查询语义。
- `tool-service.ts` —— 封装查询分发：接收 `method=codegraph_explore|node|callers|callees|impact|affected` + `workspace_path` + 参数，路由到对应 `MCPEngine.getToolHandler()` 执行，返回文本或结构化结果。复用 `ToolHandler` 现有查询能力，不重写核心逻辑。

### 3.2 复用关系（关键，避免重写）

- `workspace-service` 复用 `MCPEngine`：`new MCPEngine({ watch: true, queryPool: true })` → `engine.setProjectPathHint(path)` → `engine.ensureInitialized(path)` → `engine.getToolHandler()`。
- `tool-service` 复用 `ToolHandler`：调用其底层查询方法（探索/节点/调用方/被调用方/影响/受影响），**第一版直接复用 MCP 文本输出**，不在适配层重新定义结构化响应。
- 不再引入 `mcp/server.ts` / `mcp/daemon.ts` 那套完整 MCP 协议栈；`agent-kernel` 只借 `engine` + `tools` 两个内部模块，协议自定（更薄、不占端口）。

### 3.3 构建配置

- 在 `third_party/codegraph/tsconfig.json` 现有配置下，确保 `src/agent-kernel/**` 被纳入编译（若现有 `include` 已覆盖 `src/**` 则无需改；否则追加）。
- 不修改 `package.json` 的 `build` 主体；如需单独构建 agent-kernel，新增 `"build:agent-kernel": "tsc -p tsconfig.agent-kernel.json"` 或在现有 `build` 中自然产出 `dist/agent-kernel/server.js`。
- 注意 `copy-assets` 已把 `src/db/schema.sql` 与 `src/extraction/wasm/*.wasm` 拷到 `dist/`；agent-kernel 运行时依赖这些资源，必须随 `build` 一起产出。

## 四、后端 Kernel 管理设计

位置：`apps/backend/app/tools/codegraph/`（新建目录，不污染现有 `tools/tool_handler` 文件工具范式；CodeGraph 是独立子系统，经 `ToolSystem` 在后续阶段接入）。

### 4.1 `CodeGraphKernelSupervisor`

单一职责：管理常驻 Kernel 子进程生命周期。负责启动、握手、健康检查、重启、关闭、stderr 日志桥接。**不理解 workspace 索引业务，不解析查询结果。**

对齐项目现有范式：

- 进程启动：参照 `tool_executor.py` 的隔离执行思路，但 Kernel 是长驻单进程，不使用 `multiprocessing`（那是 per-tool 隔离）。使用 `subprocess.Popen` 启动锁定的 `node dist/agent-kernel/server.js`。
- `node` 路径解析：参照 `runtime_locator.rs` 的「从固定目录解析、缺失即结构化报错」范式，后端实现 `resolve_node_binary()`，从项目内固定目录（第一阶段：`apps/backend/.codegraph-node/node.exe` 或等价资源目录；最终阶段：桌面资源目录）解析，绝不读 `PATH`。
- 健康检查：启动时发送 `kernel.hello` 握手，校验 `protocol_version` 兼容；之后周期性 `kernel.ping`（或依赖 stderr 心跳）。
- 崩溃恢复：子进程退出 → 标记 `degraded` → 短退避重启（0.5s/1s/2s/5s，上限后 `failed`）。重启后不假设 workspace ready（与讨论文档一致）。
- 关闭：发送 `kernel.shutdown`，有限等待，超时强杀子进程（可借鉴 `windows_job_object` 树杀思路清理进程树）。
- stderr 桥接：子进程 stderr 经 `process_bridge` 队列汇入后端统一日志（复用 `config/logging/process_bridge.py` 的 `SubprocessQueueHandler` 模式）。

Kernel 进程状态（与讨论文档一致）：`stopped / starting / ready / degraded / restarting / failed / stopping`。

### 4.2 `CodeGraphKernelClient`

单一职责：通过子进程 stdin/stdout 与 Kernel 通信的极薄客户端。负责 JSON-line 序列化、 `request_id` 生成、超时、取消、协议错误码 → Python 异常转换。**不持有进程生命周期，不解析业务语义。**

- stdin 写入必须串行化（讨论文档调度规则），避免多请求交错。
- 每个请求带 `request_id` + 超时；超时返回可重试错误。
- `workspace_path` 作为查询请求的必带参数（见判断二）。
- 错误码映射：`KernelError` → Python 侧 `CodeGraphKernelError`（区分协议不兼容 / 超时 / Kernel 不可用 / workspace 未索引等）。

## 五、握手与调度协议（从讨论文档收敛）

### 5.1 握手

```text
kernel.hello  ->  protocol_version / kernel_version / codegraph_version / capabilities / platform
kernel.ping   ->  ok / uptime_ms / active_workspaces
```

`kernel.hello` 返回协议版本；协议不兼容时 `Supervisor` 拒绝继续并结构化报错。

### 5.2 查询方法（第一阶段仅验证 explore，接口预留全部）

```text
codegraph_explore(workspace_path, query)
codegraph_node(workspace_path, symbol)
codegraph_callers(workspace_path, symbol)
codegraph_callees(workspace_path, symbol)
codegraph_impact(workspace_path, symbol)
codegraph_affected(workspace_path, changed_file)
```

`workspace_path` 为必带参数；`tool-service` 据其路由到对应 `MCPEngine`。

### 5.3 调度规则

- stdout 只传 JSON-line RPC；stderr 只传 Kernel 内部日志。
- 查询请求可并发；第一阶段由 Kernel 内部串行或队列限流，避免打爆 query pool。
- 第一阶段不触发写入型 `sync`（只读查询）。

## 六、第一阶段任务拆分（可移交开发）

| 序 | 任务 | 产出 | 依赖 |
|----|------|------|------|
| T1 | vendor 构建验证 | 在 `third_party/codegraph` 跑通 `npm run build`，确认产出 `dist/` 且 `dist/agent-kernel` 被纳入（先加空 `server.ts` 验证编译链路） | 无 |
| T2 | `agent-kernel/protocol.ts` | RPC 消息类型 + 错误码 + 握手类型 | T1 |
| T3 | `agent-kernel/workspace-service.ts` | `Map<workspace_path, MCPEngine>` 管理 + singleflight `ensureInitialized` + `getStatus` + `stop` | T2 |
| T4 | `agent-kernel/tool-service.ts` | 6 个查询方法路由到 `ToolHandler`，第一版复用文本输出 | T3 |
| T5 | `agent-kernel/server.ts` | stdio JSON-line 服务入口，串起 T2–T4 + `kernel.hello/ping/shutdown` | T2–T4 |
| T6 | 后端 `resolve_node_binary()` | 从固定目录解析锁定 node，缺失即结构化错误；对齐 `runtime_locator` 范式 | T1 |
| T7 | 后端 `CodeGraphKernelSupervisor` | 启动/握手/健康检查/重启/关闭/stderr 桥接 | T5, T6 |
| T8 | 后端 `CodeGraphKernelClient` | JSON-line RPC、request_id、超时、取消、错误转换 | T5 |
| T9 | 后端集成验证脚本 | `apps/backend/temp/` 下临时脚本：启 Kernel → 握手 → 对当前仓库 explore → 收回结果 | T7, T8 |
| T10 | 单元测试 | `CodeGraphKernelClient` 协议序列化/错误映射单测；`workspace-service` singleflight 单测（需 Node 环境） | T7, T8 |

> 交付纪律：开发完成后走独立审查 + 独立测试闭环（遵守 `Agent代码开发规范` 第八章）。T9 为临时验证脚本，验证通过后清理，不进 `tests/`。

## 七、风险与开放问题收敛

### 已收敛（本文档已决策）

- 构建形态：编译 `dist` + 锁定 node（判断一）。
- workspace 关联：按请求带 path + 多 workspace 懒加载（判断二）。
- 适配层位置：`third_party/codegraph/src/agent-kernel/`，窄适配不污染上游。
- 查询输出：第一版复用 MCP 文本，不重新定义结构化响应。

### 待第二阶段解决（不在本阶段）

- `CodeGraphLifecycleService` 的 `status/init/sync/rebuild/unlock` 编排与阻塞策略。
- Agent 可见 6 工具的 `ToolDefinition`、参数模型、前端展示契约。
- `WorkspaceManager` 与 `TaskService` 启动前 `ensure_ready` 接入点。
- 前端 workspace UI 索引状态展示。

### 开放风险

- `MCPEngine` 的 `ensureInitialized` 仅缓存单个 default project；多 workspace 映射在 `workspace-service` 维护，需验证并发 `ensureInitialized` 的 singleflight 正确性（T3/T10 重点）。
- vendor TS 未被仓库根 CodeGraph 索引覆盖，适配层开发期调试依赖 `tsc` + 直接运行，不依赖 CodeGraph。
- Node 运行时固定目录第一阶段用本地占位目录，最终需接入桌面资源打包（Tauri `resourcesDir`）；路径解析接口预留，不写死。
- `dist/` 产物是否提交仓库：当前 `.gitignore` 行为需确认；若随桌面端打包则不加仓库，若本地验证则需临时产出（T1 确认）。

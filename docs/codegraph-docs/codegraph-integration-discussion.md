# CodeGraph 融入 coding-agent 讨论文档

本文档用于持续记录 CodeGraph 融入 coding-agent 的讨论、决策和开放问题。它不是最终技术方案；当本文档中的方向稳定后，再整理为正式技术方案和开发任务。

## 当前主题

把 CodeGraph 从外部工具升级为 coding-agent 的内置代码智能能力。目标是让 Agent 在理解代码结构、定位实现、追踪调用链和分析影响范围时优先使用 CodeGraph，并逐步减少对传统文件搜索工具的依赖。

## 当前决策快照

- CodeGraph 默认启用。
- CodeGraph 采用内置常驻 CodeGraph Kernel 方案。
- CodeGraph 索引生命周期按 workspace 级管理，不按 task 级管理。
- 每个 workspace 下可以有多个 task，多个 task 共享同一个 workspace 的 `.codegraph/` 索引。
- 每次新建 task 前，系统自动检查该 workspace 的 CodeGraph 状态。
- 如果 workspace 没有 `.codegraph/`，系统默认创建索引，第一版不再请求用户确认。
- 首次创建索引应阻塞 task 启动，等待索引完成后再开始 Agent 执行。
- 首次索引期间，客户端展示友好的等待进度。
- 如果 workspace 已有索引但不是最新，系统应在 task 启动前自动同步，且同步必须阻塞 task 启动。
- 内置 Node runtime 随桌面端一起打包，不依赖用户本机 Node。
- `agent-kernel` 内部协议采用 stdio JSON line RPC。
- Node Kernel 按应用级生命周期管理；CodeGraph 索引按 workspace 级生命周期管理；Agent 工具调用按 task/run 级请求管理。
- 后端侧需要独立的 `CodeGraphKernelSupervisor` 管理 Kernel 进程启动、健康检查、崩溃恢复和关闭。
- CodeGraph 索引状态显示到前端 workspace UI。
- CodeGraph 的 `status/init/sync` 等生命周期管理不作为 Agent 可见 tool 暴露。
- 第一版配齐 Agent 可见的 CodeGraph 查询工具。
- 文件搜索工具当前保留，作为 CodeGraph 不可用、索引未覆盖或需要纯文本匹配时的兜底。
- CodeGraph vendor 源码已放入 `third_party/codegraph`，并保留 MIT `LICENSE` 与 `UPSTREAM.md` 来源说明。
- 长期方向不是依赖用户机器上的全局 `codegraph` CLI。
- Python 后端不重写 CodeGraph 核心逻辑。
- 倾向复用 CodeGraph MCP 背后的执行层，而不是把 CodeGraph 当外部 MCP 服务接入。

## 核心架构

```text
coding-agent
  apps/backend/
    CodeGraphKernelSupervisor
      - 管理内置 Node Kernel 进程生命周期
      - 负责启动、握手、健康检查、重启、关闭和日志桥接
      - 不理解 workspace 索引业务

    WorkspaceManager
      - workspace 级协调入口
      - 管理 workspace 状态与 task 启动前准备
      - 协调 CodeGraphLifecycleService

    CodeGraphLifecycleService
      - 管理 workspace 的 CodeGraph 状态
      - 执行 status/init/sync/rebuild 等生命周期动作
      - 处理首次索引默认创建、并发保护、错误降级

    CodeGraphKernelClient
      - Python 侧极薄客户端
      - 通过 stdio JSON line RPC 与常驻 CodeGraph Kernel 通信

    ToolSystem
      - 向 Agent 暴露 CodeGraph 查询工具

  third_party/codegraph/
    src/agent-kernel/
      server.ts
      protocol.ts
      workspace-service.ts
      tool-service.ts

    复用现有核心：
      src/index.ts
      src/mcp/engine.ts
      src/mcp/tools.ts
```

关键原则：

- Python 不直接调用全局 CLI。
- Python 不直接理解 CodeGraph 内部数据结构。
- Node Kernel 常驻，复用 CodeGraph 的 `MCPEngine`、`ToolHandler`、watcher 和 query pool。
- Node runtime 随桌面端打包，运行期不要求用户安装 Node。
- CodeGraph 索引生命周期由后端服务层自动管理，不交给 Agent 自己决定。
- Node Kernel 生命周期和 workspace 索引生命周期分开：前者解决“运行时是否可用”，后者解决“某个 workspace 的索引是否可用且最新”。
- `WorkspaceManager` 作为 workspace 级协调入口；`CodeGraphLifecycleService` 是它管理的子能力之一。
- `agent-kernel` 采用 stdio JSON line RPC：不占端口、跨平台简单、生命周期跟随后端进程，第一版优先于 HTTP localhost。

## 内置 Node Kernel 生命周期

Node Kernel 的生命周期应和 CodeGraph 索引生命周期分层管理：

- Node Kernel 是应用级基础设施，跟随后端进程或桌面应用生命周期。
- CodeGraph 索引是 workspace 级资源，跟随 workspace 路径和 `.codegraph/` 状态。
- Agent 工具调用是 task/run 级请求，只消费已经准备好的 Kernel 和索引。

推荐第一版策略：

```text
后端启动
  -> 启动内置 Node Kernel 进程
  -> 完成 hello/ready 握手
  -> Kernel 进入 ready

打开 workspace 或创建 task
  -> WorkspaceManager 请求 CodeGraphLifecycleService.ensure_ready(workspace)
  -> CodeGraphLifecycleService 通过 CodeGraphKernelClient 调用 Kernel
  -> Kernel 按 workspace 加载或创建 CodeGraph 实例
  -> init/sync 完成后，workspace index 进入 ready

Agent 运行中
  -> ToolSystem 调用 CodeGraphKernelClient
  -> Kernel 执行 explore/node/callers/callees/impact/affected
  -> 返回文本或结构化结果

应用关闭
  -> 后端发送 kernel.shutdown
  -> 等待有限时间
  -> 超时后强制结束 Kernel 子进程
```

启动时机采用“应用级预热 + workspace 级懒加载”：

- 后端启动时预热 Node Kernel，避免首次 Agent 工具调用才发现 Kernel 不可用。
- workspace 的 CodeGraph 实例和索引检查仍在打开 workspace 或创建 task 时懒加载。
- 创建 workspace 本身只记录路径，不立即强制建索引；创建 task 前必须完成 `ensure_ready(workspace)`。

职责拆分：

- `CodeGraphKernelSupervisor`：负责启动、停止、重启、健康检查、进程退出监听和退避策略。
- `CodeGraphKernelClient`：负责 stdio JSON line RPC、请求 ID、超时、取消、协议错误转换。
- `CodeGraphLifecycleService`：负责 workspace 级 `status/init/sync/rebuild` 编排，不直接管理进程。
- `WorkspaceManager`：负责把 CodeGraph 准备阶段接入 workspace/task 启动流程。

Kernel 进程状态建议：

```text
stopped
starting
ready
degraded
restarting
failed
stopping
```

workspace 索引状态建议：

```text
unknown
checking
unindexed
indexing
syncing
ready
degraded
failed
```

握手协议建议：

```text
kernel.hello
  <- protocol_version
  <- kernel_version
  <- codegraph_version
  <- capabilities
  <- platform

kernel.ping
  <- ok
  <- uptime_ms
  <- active_workspaces
```

调度规则：

- stdout 只传 JSON line RPC 协议消息。
- stderr 只传 Kernel 内部日志，由后端桥接进统一日志系统。
- Python 侧写入 stdin 必须串行化，避免多请求交错。
- 每个请求必须有 `request_id`、超时和错误码。
- 同一个 workspace 的 `init/sync/rebuild` 必须 singleflight，避免并发重复索引。
- 查询工具可以并发，但第一版可先由 Kernel 内部队列限制并发，避免打爆 query pool 或文件系统。
- task 启动前的 `sync` 必须阻塞；Agent 运行中的查询不应触发隐式写入型 sync。

崩溃恢复：

- Kernel 异常退出后，Supervisor 标记 Kernel 为 `degraded`，所有进行中的请求返回可重试错误。
- Supervisor 按短退避重启 Kernel，例如 0.5s、1s、2s、5s，上限后进入 `failed`。
- Kernel 重启成功后不自动假设 workspace ready；下一次 task 启动或 workspace 状态刷新时重新执行 `status/sync`。
- 如果崩溃发生在 `init/sync` 中，workspace 索引状态标记为 `degraded` 或 `failed`，前端展示“重新同步/重建索引”入口。

日志要求：

- 所有 Kernel 生命周期事件进入后端统一日志。
- 关键字段至少包括：`workspace_id`、`workspace_path`、`kernel_pid`、`operation`、`duration_ms`、`status`、`error_code`。
- stdout 协议解析失败、握手超时、Kernel 退出码、stderr 错误摘要都必须可排查。

版本与打包：

- Node runtime 和 CodeGraph Kernel 构建产物随桌面端打包。
- Kernel 启动命令由后端从桌面资源目录解析，不读取用户 PATH。
- `kernel.hello` 必须返回协议版本；协议不兼容时后端应拒绝继续，并给出明确错误。
- Node runtime 版本锁定在项目内，不随用户机器环境漂移。

## Workspace / Task 生命周期

当前产品模型：

```text
workspace
  task 1
  task 2
  task N
```

CodeGraph 索引属于 workspace 级资源。task 可以消费同一个 workspace 的 `.codegraph/` 索引，但 task 不负责创建、同步或修复索引。

推荐生命周期：

```text
创建 workspace
  -> 记录 workspace 路径
  -> 不立即强制建索引

创建 task
  -> TaskService 请求 WorkspaceManager.prepare_task_start(workspace)
  -> WorkspaceManager 调用 CodeGraphLifecycleService.ensure_ready(workspace)
     -> 检查 CodeGraph 状态
     -> 无 .codegraph/
        -> 默认 init + 首次索引
        -> 阻塞等待索引完成
     -> 有 .codegraph/
        -> 检查 pendingChanges / index state
        -> 如索引不是最新，自动 sync，并阻塞等待完成
  -> WorkspaceManager 返回 workspace ready 结果
  -> TaskService 创建 task / first turn
  -> AgentRuntime 启动 Agent
  -> Agent 通过 codegraph_explore 查询代码结构
```

这个流程中的 `status/init/sync` 是系统行为，不是 Agent tool 调用。

## CodeGraph 生命周期命令使用规则

`status`、`init`、`sync`、`rebuild`、`unlock` 都属于系统生命周期命令，由 `CodeGraphLifecycleService` 调用，不暴露给 Agent 自主调用。

### status

用途：

- 判断当前 workspace 是否存在 `.codegraph/`。
- 判断索引是否可用、是否过期、是否正在被其它进程占用、是否需要同步。
- 为前端 workspace UI 提供状态展示。

使用时机：

- 打开 workspace 时可轻量调用一次，用于刷新 UI。
- 创建 task 前必须调用，作为 `ensure_ready(workspace)` 的第一步。
- Kernel 重启后、索引操作失败后、用户点击刷新状态时调用。

阻塞规则：

- 创建 task 前的 `status` 必须阻塞，因为后续要决定是否 `init` 或 `sync`。
- 普通 UI 刷新可以异步，不阻塞用户浏览界面。

第一版原则：

- `status` 是生命周期入口，不应该由 Agent 作为普通工具调用。
- `status` 结果要转换成项目内稳定状态模型，不把 CodeGraph 原始输出直接泄露到业务层。

### init

用途：

- 在 workspace 内创建 `.codegraph/` 并完成首次索引。

使用时机：

- 创建 task 前，`status` 发现 workspace 未索引时调用。
- 第一版默认调用，不再弹出用户确认。

阻塞规则：

- 首次 `init` 必须阻塞 task 启动。
- 客户端展示 `indexing_codegraph` 进度，索引完成后再进入 `workspace_ready`。

限制：

- 只能在合法 workspace 根目录内执行。
- 不允许对用户 home、磁盘根目录等高风险路径执行。
- 同一个 workspace 的 `init` 必须 singleflight，多个 task 同时创建时只允许一个真实索引任务。

失败处理：

- `init` 失败时，workspace 索引状态进入 `failed`。
- 第一版可以允许 task 继续以文件搜索兜底启动，但必须在 UI 和日志中明确标记 CodeGraph 不可用。
- 如果失败原因是路径、权限或环境不可恢复，不应自动无限重试。

### sync

用途：

- 在已有 `.codegraph/` 的 workspace 中同步增量代码变更。

使用时机：

- 创建 task 前，`status` 发现索引不是最新时调用。
- 用户在 workspace UI 点击“同步索引”时调用。
- Kernel 重启后不自动假设索引最新；下一次 task 启动前仍通过 `status` 决定是否 `sync`。

阻塞规则：

- 创建 task 前触发的 `sync` 必须阻塞 task 启动。
- 用户手动触发的 `sync` 可以异步展示进度，但如果此时有新 task 创建，该 task 必须等待同一个 `sync` 完成。

限制：

- 同一个 workspace 的 `sync` 必须 singleflight。
- Agent 运行中的普通查询不应隐式触发写入型 `sync`。
- 如果 task 已在运行，是否允许后台 `sync` 需要谨慎；第一版建议只在 task 启动前同步，减少索引版本漂移。

失败处理：

- `sync` 失败时，workspace 索引状态进入 `degraded` 或 `failed`。
- 后续 Agent 可以回退文件搜索，但工具描述和系统提示应提醒 CodeGraph 当前不可用或可能过期。

### rebuild

用途：

- 丢弃或覆盖当前损坏、严重过期、版本不兼容的索引，重新完整构建。

使用时机：

- `status` 明确返回索引损坏、版本不兼容、schema 不兼容时。
- 连续多次 `sync` 失败，且错误指向索引内部状态不可恢复时。
- 用户在 workspace UI 手动点击“重建索引”时。

阻塞规则：

- 如果 `rebuild` 发生在 task 创建前，必须阻塞 task 启动。
- 如果用户在 workspace UI 手动触发，可以作为显式维护操作异步展示进度；执行期间新 task 需要等待。

权限规则：

- 第一版不建议自动 `rebuild`。
- 自动 `rebuild` 风险高于 `init/sync`，因为它可能覆盖已有索引状态；建议先提供用户操作入口。
- 只有在确定 rebuild 不删除用户源码、只影响 `.codegraph/` 内部数据时，未来才考虑自动化。

失败处理：

- `rebuild` 失败时保持 workspace 索引状态为 `failed`。
- 前端提供错误摘要、重试入口和“继续但不使用 CodeGraph”的解释。

### unlock

用途：

- 清理异常退出留下的 CodeGraph 锁，解除“索引正在被占用但实际无进程工作”的状态。

使用时机：

- `status` 返回 locked，并且锁持有进程不存在或已确认不是当前 Kernel。
- Kernel 崩溃后重启，发现 workspace 仍处于 stale lock。
- 用户在 workspace UI 手动点击“解除索引锁”。

阻塞规则：

- task 创建前如果需要 `unlock` 才能继续 `sync/init`，可以阻塞执行。
- 如果锁状态不确定，不应自动 unlock，应提示用户或进入 failed/degraded。

安全规则：

- 只能解除明确 stale 的锁。
- 如果锁由当前仍存活的 Kernel 或其它正在运行的进程持有，不允许自动 unlock。
- unlock 后必须立刻重新 `status`，不能直接假设索引可用。

失败处理：

- `unlock` 失败时保持 `degraded` 或 `failed`，并回退文件搜索。

### 推荐编排

task 启动前推荐使用固定编排：

```text
ensure_ready(workspace)
  -> status
  -> if unindexed:
       init
       status
  -> else if stale_lock:
       unlock
       status
  -> else if stale_or_pending_changes:
       sync
       status
  -> else if corrupted_or_incompatible:
       require_manual_rebuild 或进入 failed
  -> if ready:
       allow task start
  -> else:
       mark codegraph unavailable
       allow task start with file-search fallback 或按产品策略中止
```

第一版建议：

- 自动执行：`status`、`init`、`sync`。
- 条件自动执行：`unlock`，仅限明确 stale lock。
- 用户入口触发：`rebuild`。
- Agent 可见：只读查询工具，不可见生命周期命令。

## Task 启动前准备状态

task 创建前可以有一个明确的准备阶段：

```text
preparing_workspace
  -> checking_codegraph
  -> indexing_codegraph
  -> syncing_codegraph
  -> workspace_ready
  -> task_created
  -> agent_running
```

客户端进度文案候选：

```text
正在检查代码索引...
正在为当前工作区建立代码索引，首次可能需要一些时间...
正在同步最近的代码变更...
代码索引已准备好，正在启动任务...
```

首次索引说明文案候选：

```text
正在为当前工作区创建 .codegraph/ 本地索引目录，用于让 Agent 更准确地理解代码结构和调用关系。索引保存在本机项目目录内，不上传代码。
```

第一版默认创建索引，不弹出首次确认。

## Agent 可见工具边界

第一版配齐 Agent 可见的 CodeGraph 查询工具：

```text
codegraph_explore
codegraph_node
codegraph_callers
codegraph_callees
codegraph_impact
codegraph_affected
```

其中 `codegraph_explore` 是 Agent 理解代码、定位实现、追踪调用路径和判断影响范围的首选工具。

不作为 Agent tool 暴露的系统能力：

```text
codegraph_status
codegraph_init
codegraph_sync
codegraph_rebuild
codegraph_unlock
```

这些能力由后端生命周期服务调用，并通过系统事件、进度事件和日志呈现给用户。

## Agent 使用策略

系统提示和工具描述应逐步把 Agent 从文件搜索迁移到 CodeGraph：

```text
理解代码、定位实现、追踪调用链、分析影响范围时，优先使用 codegraph_explore。
CodeGraph 不可用、未初始化、索引失败、结果不足或需要纯文本匹配时，才回退到 search_files/read_file。
```

迁移目标：

```text
先 CodeGraph 建立结构视角，再精读必要源码，再用文件搜索兜底。
```

不要立刻删除文件搜索工具。

## 权限与安全边界

CodeGraph 能力分两类：

- Agent 可见只读工具：`explore`、`node`、`callers`、`callees`、`impact`、`affected`。
- 系统生命周期能力：`status`、`init`、`sync`、`rebuild`、`unlock`。

系统生命周期能力中的写入操作必须遵守项目现有路径安全边界：

- 第一版默认允许系统在 workspace 内创建 `.codegraph/`。
- 不允许自动删除 `.codegraph/`。
- 不允许把用户 home、磁盘根目录等高风险路径作为索引根，除非未来有专门保护逻辑。
- workspace 路径必须经过项目现有路径安全边界校验。
- 如果未来引入“重建索引”“删除索引”“排除大目录”等高影响操作，再接入显式用户确认。

## 已放弃方向

- 不把“每次调用 CLI”作为长期方向。
- 不把 CodeGraph 当普通外部 MCP 服务接入 coding-agent。
- 不在 Python 侧重写 CodeGraph 核心。
- 不把 CodeGraph 的 `init/status/sync` 暴露为 Agent 自主调用的普通 tool。
- 不让 task 创建逻辑直接散落处理 CodeGraph 生命周期细节。

## 风险与约束

- 内置 Node Kernel 仍是独立运行时，不是 Python 进程内库；需要管理生命周期、启动失败、超时和崩溃恢复。
- 如果直接复用 MCP `ToolHandler` 的文本输出，第一版实现简单，但结构化程度有限。
- 如果过早裁剪 `third_party/codegraph`，后续 upstream 合并和问题定位会变复杂。
- 默认启用会增加首次 workspace 使用时的索引等待，需要前端把索引行为解释清楚。
- CodeGraph 写入 `.codegraph/` 会影响用户仓库目录，需要明确权限和忽略策略。
- 每次新建 task 前检查和阻塞同步索引会增加启动耗时，需要定义快速状态检查和用户可见反馈策略。
- 首次索引阻塞 task 启动会增加用户等待时间，需要客户端明确展示进度，并说明这是为了提高 Agent 后续代码理解质量。
- 第一版配齐全部查询工具会扩大工具面，需要通过清晰描述约束 Agent 优先用 `codegraph_explore`，其它工具用于更窄查询。
- Kernel 进程、workspace 索引、Agent 工具调用是三套状态，前端和日志需要避免把它们混成一个“CodeGraph 状态”。

## 开放问题

- `third_party/codegraph` 第一阶段是否保留 site、installer、telemetry-worker 等非运行核心目录。
- CodeGraph 查询结果在前端工具调用卡片中如何展示：全文文本、折叠摘要，还是结构化分区。
- `WorkspaceManager` 如何与现有 `WorkspaceService`、`TaskService`、`AgentRuntime` 分层对齐，避免把服务层和运行时层搅在一起。
- 内置 Node runtime 的具体资源路径、版本锁定、升级策略和跨平台产物组织方式。
- 前端 workspace UI 需要同时展示哪些 Kernel 状态和 workspace 索引状态，以及索引失败时给用户哪些操作入口。
- CodeGraph Kernel 返回结果第一版是继续复用 MCP 文本，还是先定义最小结构化响应。

## 后续可整理方向

- 从本文档整理出正式技术方案。
- 从正式技术方案拆出第一阶段开发任务。
- 为 `WorkspaceManager` 设计职责边界、状态模型和与 `TaskService` 的调用关系。
- 为 `CodeGraphLifecycleService` 设计 workspace/task 启动接入点、进度事件、错误事件和日志策略。
- 为全量 CodeGraph 查询工具设计 Agent 可见 `ToolDefinition`、参数模型和前端展示契约。
- 从内置 Kernel 生命周期设计拆出 `CodeGraphKernelSupervisor`、`CodeGraphKernelClient`、Kernel 协议和状态事件开发任务。
- 为桌面端打包内置 Node runtime 与 CodeGraph Kernel 设计构建/发布流程。
- 为前端 workspace UI 设计 CodeGraph 索引状态展示。

## 用户原话摘录

- “我想吧codegraph，封装为tool，使我的codingagent，逐渐替代文件搜索工具（当前可以保留）。”
- “我想的是后续安装我的codeing agent，就附带安装了codegraph。”
- “CLI很麻烦啊，还得找内置的，还得启动独立子进程。对codegraph做一层薄封装不好啦？”
- “codegraph 不是有MCP吗？我们把它的MCP拆了，直接调用它的API行吗？内置在工具中常驻。”
- “默认启用，但首次建索引需要用户确认吧。”（历史想法，已被第一版默认构建索引替换）
- “每新建一个workspace，都会新建一个task，一个workspace下会有多个task，每次新建task，都要检测当前是否存在codegraph，没有则新建，如果有，看看索引是否是最新，没有就更新。”
- “codegraph的init、status等管理codegraph的工具，不应该是agent的tool，而是自动执行的。”
- “等待索引完成再开始 task，客户端可以给一些友好的进度等待。”
- “CodeGraph 生命周期应该按 workspace 级别管理。我觉得可以有一个类似workspace 管理器的东西。”
- “内置 Node runtime 随桌面端打包可以吗？”
- “第一版先不让用户确认了，默认就是构建索引。”
- “已有索引的 sync 必须是阻塞的。”
- “把 CodeGraph 索引状态显示到前端 workspace UI吧。”
- “第一版就把所有工具配齐。”

## 变更记录

- 2026-08-02：创建本文档，最初记录 CodeGraph 默认启用、首次索引需用户确认、内置常驻 Kernel 方向与第一阶段工具范围；其中“首次索引需用户确认”后续已被“第一版默认构建索引”替换。
- 2026-08-02：确认 `status/init/sync` 属于系统生命周期管理，不作为 Agent 可见 tool；确认每次新建 task 前自动检查 workspace 索引状态；其中“缺索引时请求用户确认”后续已被“第一版默认构建索引”替换。
- 2026-08-02：确认首次索引应阻塞 task 启动，客户端展示友好进度；确认 CodeGraph 生命周期按 workspace 级管理，并倾向引入 `WorkspaceManager` 作为 workspace 级协调入口。
- 2026-08-02：整理文档结构，移除过时的“候选方案优先级”表述，将当前确认方向整理为决策快照、生命周期、工具边界、风险和开放问题。
- 2026-08-02：更新第一版策略：内置 Node runtime 随桌面端打包；`agent-kernel` 采用 stdio JSON line RPC；首次索引默认创建不再确认；已有索引 sync 阻塞 task 启动；前端 workspace UI 展示索引状态；第一版配齐 Agent 可见查询工具。
- 2026-08-02：补充内置 Node Kernel 生命周期管理，并整理上下文一致性：区分应用级 Kernel 生命周期、workspace 级索引生命周期、task/run 级工具请求；将早期“首次索引需用户确认”统一标注为历史想法。
- 2026-08-04：讨论收敛出第一阶段技术方案，落地于 `codegraph-agent-kernel-design.md`。第一阶段唯一目标为「打通 Kernel 常驻 + 一次 explore 查询端到端跑通」，明确排除 LifecycleService / Agent 工具面 / 前端 UI。达成两项核心生产判断：（1）构建形态=编译 `dist` 后由锁定内置 Node 22 LTS 运行，不用 tsx 直跑；（2）workspace 关联=按请求带 `workspace_path`、常驻 Kernel 多 workspace 懒加载（`agent-kernel` 内维护 `Map<workspace_path, MCPEngine>`）。两项判断均基于项目事实（`package.json` 的 tsc 工程、`runtime_locator.rs` 固定目录解析范式、`tool_executor.py` 进程管理范式、AGENTS.md 的 Node 25/26 拦截风险）。

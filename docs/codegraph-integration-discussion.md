# CodeGraph 融入 coding-agent 讨论文档

本文档用于持续记录 CodeGraph 融入 coding-agent 的讨论、决策和开放问题。它不是最终技术方案；当本文档中的方向稳定后，再整理为正式技术方案和开发任务。

## 当前主题

把 CodeGraph 从外部工具升级为 coding-agent 的内置代码智能能力。目标是让 Agent 在理解代码结构、定位实现、追踪调用链和分析影响范围时优先使用 CodeGraph，并逐步减少对传统文件搜索工具的依赖。

## 当前决策快照

- CodeGraph 默认启用。
- CodeGraph 采用内置常驻 CodeGraph Kernel 方案。
- CodeGraph 生命周期按 workspace 级管理，不按 task 级管理。
- 每个 workspace 下可以有多个 task，多个 task 共享同一个 workspace 的 `.codegraph/` 索引。
- 每次新建 task 前，系统自动检查该 workspace 的 CodeGraph 状态。
- 如果 workspace 没有 `.codegraph/`，系统请求用户确认后创建索引。
- 首次创建索引应阻塞 task 启动，等待索引完成后再开始 Agent 执行。
- 首次索引期间，客户端展示友好的等待进度。
- 如果 workspace 已有索引但不是最新，系统应在 task 启动前自动同步。
- CodeGraph 的 `status/init/sync` 等生命周期管理不作为 Agent 可见 tool 暴露。
- Agent 第一阶段只需要看到 `codegraph_explore` 查询工具。
- 文件搜索工具当前保留，作为 CodeGraph 不可用、索引未覆盖或需要纯文本匹配时的兜底。
- CodeGraph vendor 源码已放入 `third_party/codegraph`，并保留 MIT `LICENSE` 与 `UPSTREAM.md` 来源说明。
- 长期方向不是依赖用户机器上的全局 `codegraph` CLI。
- Python 后端不重写 CodeGraph 核心逻辑。
- 倾向复用 CodeGraph MCP 背后的执行层，而不是把 CodeGraph 当外部 MCP 服务接入。

## 核心架构

```text
coding-agent
  apps/backend/
    WorkspaceManager
      - workspace 级协调入口
      - 管理 workspace 状态与 task 启动前准备
      - 协调 CodeGraphLifecycleService

    CodeGraphLifecycleService
      - 管理 workspace 的 CodeGraph 状态
      - 执行 status/init/sync/rebuild 等生命周期动作
      - 处理首次索引确认、并发保护、错误降级

    CodeGraphKernelClient
      - Python 侧极薄客户端
      - 与常驻 CodeGraph Kernel 通信

    ToolSystem
      - 只向 Agent 暴露 codegraph_explore

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
- CodeGraph 生命周期由后端服务层自动管理，不交给 Agent 自己决定。
- `WorkspaceManager` 作为 workspace 级协调入口；`CodeGraphLifecycleService` 是它管理的子能力之一。

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
        -> 请求用户确认
        -> 用户确认后 init + 首次索引
        -> 阻塞等待索引完成
     -> 有 .codegraph/
        -> 检查 pendingChanges / index state
        -> 如索引不是最新，自动 sync
  -> WorkspaceManager 返回 workspace ready 结果
  -> TaskService 创建 task / first turn
  -> AgentRuntime 启动 Agent
  -> Agent 通过 codegraph_explore 查询代码结构
```

这个流程中的 `status/init/sync` 是系统行为，不是 Agent tool 调用。

## Task 启动前准备状态

task 创建前可以有一个明确的准备阶段：

```text
preparing_workspace
  -> checking_codegraph
  -> waiting_codegraph_consent
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

首次索引确认文案候选：

```text
该操作会在当前工作区创建 .codegraph/ 本地索引目录，用于让 Agent 更准确地理解代码结构和调用关系。索引保存在本机项目目录内，不上传代码。是否继续？
```

确认后才允许由 `CodeGraphLifecycleService` 执行 `init`。

## Agent 可见工具边界

第一阶段 Agent 只看到一个核心查询工具：

```text
codegraph_explore
```

`codegraph_explore` 是 Agent 理解代码、定位实现、追踪调用路径和判断影响范围的首选工具。

不作为 Agent tool 暴露的系统能力：

```text
codegraph_status
codegraph_init
codegraph_sync
codegraph_rebuild
codegraph_unlock
```

这些能力由后端生命周期服务调用，并通过系统事件、审批事件和日志呈现给用户。

后续可考虑开放更多 Agent 查询工具：

```text
codegraph_node
codegraph_callers
codegraph_callees
codegraph_impact
codegraph_affected
```

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

- Agent 可见只读工具：`explore`，后续可增加 `node`、`callers`、`callees`、`impact`、`affected`。
- 系统生命周期能力：`status`、`init`、`sync`、`rebuild`、`unlock`。

系统生命周期能力中的写入操作必须遵守项目现有权限审批机制：

- 不允许在用户未确认时创建 `.codegraph/`。
- 不允许自动删除 `.codegraph/`。
- 不允许把用户 home、磁盘根目录等高风险路径作为索引根，除非未来有专门保护逻辑。
- workspace 路径必须经过项目现有路径安全边界校验。

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
- 默认启用会增加首次 workspace 使用时的确认步骤，需要前端把索引行为解释清楚。
- CodeGraph 写入 `.codegraph/` 会影响用户仓库目录，需要明确权限和忽略策略。
- 每次新建 task 前检查和同步索引会增加启动耗时，需要定义快速状态检查、同步阈值和用户可见反馈策略。
- 首次索引阻塞 task 启动会增加用户等待时间，需要客户端明确展示进度，并说明这是为了提高 Agent 后续代码理解质量。

## 开放问题

- 内置 Node runtime 是随桌面端打包，还是第一阶段先复用项目开发环境里的 Node。
- `third_party/codegraph` 第一阶段是否保留 site、installer、telemetry-worker 等非运行核心目录。
- `agent-kernel` 的内部协议采用 JSON line RPC 还是 HTTP localhost。
- 首次索引确认入口放在工具权限审批弹窗，还是工作区设置页。
- 用户拒绝首次索引后，本 task 是否继续启动并回退文件搜索，还是取消 task 创建。
- 已有索引的 `sync` 是否必须阻塞 task 启动，还是设置短超时后转后台同步。
- 是否把 CodeGraph 索引状态显示到前端 workspace UI。
- CodeGraph 查询结果在前端工具调用卡片中如何展示：全文文本、折叠摘要，还是结构化分区。
- `WorkspaceManager` 的职责边界如何与现有 `WorkspaceService`、`TaskService`、`AgentRuntime` 分层对齐。

## 后续可整理方向

- 从本文档整理出正式技术方案。
- 从正式技术方案拆出第一阶段开发任务。
- 为 `WorkspaceManager` 设计职责边界、状态模型和与 `TaskService` 的调用关系。
- 为 `CodeGraphLifecycleService` 设计 workspace/task 启动接入点、审批事件和日志策略。
- 为 `codegraph_explore` 设计 Agent 可见 `ToolDefinition`、参数模型和前端展示契约。
- 为内置 Kernel 设计启动、重启、健康检查和日志策略。

## 用户原话摘录

- “我想吧codegraph，封装为tool，使我的codingagent，逐渐替代文件搜索工具（当前可以保留）。”
- “我想的是后续安装我的codeing agent，就附带安装了codegraph。”
- “CLI很麻烦啊，还得找内置的，还得启动独立子进程。对codegraph做一层薄封装不好啦？”
- “codegraph 不是有MCP吗？我们把它的MCP拆了，直接调用它的API行吗？内置在工具中常驻。”
- “默认启用，但首次建索引需要用户确认吧。”
- “每新建一个workspace，都会新建一个task，一个workspace下会有多个task，每次新建task，都要检测当前是否存在codegraph，没有则新建，如果有，看看索引是否是最新，没有就更新。”
- “codegraph的init、status等管理codegraph的工具，不应该是agent的tool，而是自动执行的。”
- “等待索引完成再开始 task，客户端可以给一些友好的进度等待。”
- “CodeGraph 生命周期应该按 workspace 级别管理。我觉得可以有一个类似workspace 管理器的东西。”

## 变更记录

- 2026-08-02：创建本文档，记录 CodeGraph 默认启用、首次索引需用户确认、内置常驻 Kernel 方向与第一阶段工具范围。
- 2026-08-02：确认 `status/init/sync` 属于系统生命周期管理，不作为 Agent 可见 tool；确认每次新建 task 前自动检查 workspace 索引状态，缺索引时请求用户确认，索引过期时自动同步。
- 2026-08-02：确认首次索引应阻塞 task 启动，客户端展示友好进度；确认 CodeGraph 生命周期按 workspace 级管理，并倾向引入 `WorkspaceManager` 作为 workspace 级协调入口。
- 2026-08-02：整理文档结构，移除过时的“候选方案优先级”表述，将当前确认方向整理为决策快照、生命周期、工具边界、风险和开放问题。

# 项目架构边界

本文件只描述稳定的架构边界、进程边界、事实所有权和开发约束。模块、类、函数的具体职责、参数、返回值、副作用和异常契约由源码 docstring 描述；不要把函数级算法、字段清单、临时方案或容易变化的实现参数堆进本文件。

## 产品形态与运行拓扑

本项目是单用户、本机运行的桌面 Agent。前后端分离用于隔离职责和进程；HTTP/SSE 是 localhost 进程边界，不代表公网服务或独立部署边界。

除非代码事实或明确需求证明必要，不引入认证、多租户、云端队列、Redis、Postgres、Kubernetes 或公网服务安全方案。

```text
Tauri 桌面应用
├─ Rust 主进程：窗口、IPC、后端生命周期
├─ WebView2：React 前端
└─ Python/FastAPI 后端子进程
   ├─ Agent Runtime / LangGraph workflow
   ├─ 本机 SQLite
   ├─ 可选 CodeGraph Kernel Node 子进程
   └─ 按需创建的工具执行子进程
```

开发期还可能存在 Vite 和 Uvicorn reload 辅助进程；它们不构成新的业务服务层。

## 开发约定

进行改造或代码修复时，先判断应做边界清晰的结构化改造，还是局部补丁修复。避免为了短期改动小而持续叠加补丁；选择结构化改造时也必须保持目标聚焦，并说明影响范围。

### 第零铁律（最高优先级）

一切以方便项目长期稳定迭代为最终目标。“最小改动”“零新增依赖”等偏好都只是手段；当它们损害长期可维护性、可读性或可演进性时，以本条为准。

- 允许进行结构性重构、扩大合理改动范围，不为追求改动小而保留补丁式结构。
- 编辑器、图表、虚拟化、Markdown/Diff、日期、表单校验等通用复杂能力，优先复用成熟且适配项目的方案。
- 引入依赖不是目的。已有项目能力或成熟方案能够解决问题时，禁止重复造轮子。
- 以改动是否让后续迭代更稳、更易维护、更易演进为判断准绳，而不是单纯比较改动行数或依赖数量。

### 五条铁律

1. **单一职责**：一个文件按职责只承担一类主要工作，不以行数作为唯一拆分标准。
2. **不重复造轮子**：能复用项目已有能力或成熟方案时，不自行实现等价基础设施。
3. **改动聚焦**：只修改与目标相关的代码；结构性重构明显有利于长期迭代时，可以扩大改动面。
4. **目录结构清晰**：可以持续拆分文件和目录，使能力模块与职责边界仅从目录结构即可初步辨认。
5. **可排查日志**：关键流程必须保留结构化、可落盘日志，不得只依赖临时终端输出。

正确性、可排查性、不重复造轮子、单一职责、目录结构、可读性、改动聚焦和性能同等重要。发生张力时，以第零铁律为最终判据，不使用固定的机械取舍链。

### 日志打印与排查

- React 正式诊断日志使用 `frontendLog(level, event, msg, { traceId, data, error })`；后端使用 `log.info`、`log.warning`、`log.error` 或 `log.exception`，通过 `extra={"msg": "...", "data": {...}}` 附带说明和业务字段。
- `event` 使用稳定的 snake_case 名称；`task_id`、`run_id` 等标识放入 `data`。同一请求或执行链路复用同一个 `trace_id`；前端 HTTP 请求通过 `X-Trace-Id` 传入后端，后端绑定日志上下文。
- 前端 Tauri 日志查看 `app_data_dir()/runtime/frontend.log`；浏览器开发模式查看 WebView/浏览器控制台。
- 后端运行日志查看 `logs/backend.log` 和 `storage/logs.sqlite3`；启动、停止或崩溃问题查看 `app_data_dir()/runtime/desktop.log`、`backend-console.log` 和 `backend.bootstate.json`。
- 日志和观测是诊断旁路，不是业务事实。不得记录未经脱敏的密钥、Token、密码、完整请求正文或大段模型/工具内容；日志或观测失败不得阻断 Agent 主流程。

## 进程生命周期边界

- Tauri Rust 主进程是桌面宿主，也是 FastAPI 后端子进程生命周期的唯一所有者。React 不得直接创建、停止或重启后端进程。
- Tauri 显示 WebView 后在后台启动后端；窗口显示与后端 readiness 解耦。React 通过 boot gate 呈现 `starting`、`ready`、`failed`，不得假定页面出现时后端已经可用。
- 后端绑定 `127.0.0.1`，桌面模式由 Tauri 动态分配端口。前端必须读取 runtime config，不得硬编码生产端口；开发或测试 fallback 端口不是桌面生产契约。
- Tauri 负责等待 bootstate 与 `/health`、报告失败、有限重试以及退出时清理后端进程树。`/health` 仅表示 liveness，不代表数据库、模型或 CodeGraph 已全部可用。
- 后端 lifespan 负责初始化和关闭数据库、Run executor、运行期依赖、CodeGraph 与观测组件。工具子进程由工具执行层负责取消、超时和进程树清理。
- 后端崩溃恢复必须有限且串行，不得无限重启。恢复耗尽后由用户显式重启；生命周期 generation 用于防止旧进程或旧线程覆盖当前状态。
- 后端重启时，遗留 active Run 收敛为 `cancelled`，遗留 active delegation 标记失败；不得隐式重放旧 Agent 执行。进程内 registry、subscriber 和运行任务不跨重启恢复。

## 进程间接口边界

### Tauri IPC

前端与 Rust 宿主之间的控制面仅包括：

- `backend_status`
- `backend_runtime_config`
- `restart_backend`
- `write_frontend_log`

新增桌面能力前必须判断其属于宿主控制面还是后端业务接口。React 业务组件不得绕过现有 runtime/logging 边界自行管理进程。

### 本机 HTTP 与 Assistant Transport

- 领域 JSON API 统一通过前端 `lib/http/client.ts` 处理动态基地址、`no-store`、`X-Trace-Id` 和非 2xx 错误。Assistant SSE、业务 resume 和资源请求由各自 transport/resource 边界管理，但仍须遵守动态地址和 trace 规则。
- Assistant UI wire schema 只属于 `apps/backend/app/assistant_transport/`，不得泄漏到 workflow、领域 service 或 storage。
- `POST /assistant` 创建或继续业务 Run；attach 只订阅既有 Run；state 读取 canonical snapshot；cancel 显式取消 Run。
- Assistant 命令以 `(task_id, command_id)` 作为幂等标识；同一标识的不同 payload 必须拒绝。同一 task 同时只允许一个 active Run，command、Run 和初始 snapshot 的创建由同一用例事务保护。
- HTTP/SSE 断开只表示订阅中断，不自动取消或重放业务 Run。前端可以重新 attach 已有 Run；只有明确的 business resume 才能继续业务执行。

## 数据与事实所有权

- 后端拥有 task、workspace、Run、command、Agent context、Transport snapshot、delegation、文件变更记录和 provider/model 配置等持久化事实；前端状态只负责交互和渲染。
- 主业务库默认是 `storage/app.sqlite3`；日志库是 `storage/logs.sqlite3`；LangGraph checkpoint 使用独立的 `storage/langgraph_checkpoints.sqlite`。三者职责和访问路径分离，不得跨层复用 session 或事实模型。
- `ConversationRunModel.status` 是 Run 生命周期状态的唯一事实源。Transport snapshot、Agent context 和 LangGraph checkpoint 都不能演化成第二套 Run 状态机。
- Agent context 的持久化事实由 `conversation_task_contexts` 承载；`RuntimeContextManager` 是 Task 级 context 的唯一运行时协调入口和进程内 working copy owner，但不是数据库事实源。
- `ConversationTaskSnapshotService` 是 Transport snapshot 的 owner；`ConversationEventProjector` 只负责把 conversation event 投影到 snapshot。`ConversationStateSnapshot` 面向前端 Transport，不是 Agent context 的镜像。
- context 与 Transport snapshot 允许短暂不一致，以最终一致性收敛。读取 snapshot 时必须以 Run 数据库状态校正生命周期状态，不得为了消除流式时序差异而强行把 Run、context、snapshot 放入一个全局事务。
- LangGraph checkpoint 只服务 workflow 恢复，不代表 Run 生命周期状态；file snapshot/change set 只服务文件变更审阅、保留和回退，不是 Conversation snapshot。
- `task_runtime`、取消 registry、snapshot subscriber 等属于当前后端进程内的协调状态，不是持久化事实。
- WebView `localStorage` 只保存模型选择、最近 workspace 等用户偏好；各类偏好由对应 storage module 管理，不得存储任务或对话事实。

## 前端架构

- React 负责页面、用户交互、HTTP/SSE 客户端和渲染状态；Rust/Tauri 负责窗口与本地进程控制。前端不得直接访问 SQLite、Agent Runtime 或工作区文件来绕过后端业务边界。
- 后端 snapshot 是对话、Run、usage 和 tool parts 的权威读取来源。Assistant UI runtime state 只用于当前渲染和传输控制，不得原样写回后端作为事实。
- `WorkspaceShell` 是 workspace/task 导航的稳定容器；`TaskPage`、初始 snapshot 和 Assistant runtime 按 `taskId` 建立。普通 workspace 数据刷新不应无故卸载正在执行的 Assistant runtime。
- transport resume/attach 只重新订阅已有 Run；business resume 才继续后端执行。前端实现和命名必须保持两种语义可辨认。
- 前端测试分层：Vitest 验证模块行为；当前 Playwright E2E 使用 Vite 与独立内存测试服务，不覆盖真实 Tauri IPC、动态后端端口、BackendSupervisor 或后端崩溃恢复。不得把它视为完整桌面集成测试。

## 工具 UI 渲染边界

工具执行结果的前端渲染是后端 `ToolObservation.display_data` 与前端 renderer 之间的稳定契约，不新增进程或服务；完整字段规范见 `apps/backend/app/core/tools/tool_ui_display_contract.md`，本文件只描述边界。

- 两层契约：静态展示声明 `ToolDisplayHints` 随 `ToolDefinition` 传给客户端，只声明 `verb`/`icon`/`surface`/`expandable`/`expand_layout`/`default_open`/`show_result` 等 UI 意图，不含动态结果、渲染函数或业务数据；动态展示数据 `ToolObservation.display_data` 是一次执行完成后的结构化 JSON，每个 payload 必须有稳定 `kind`，由后端 `apps/backend/app/core/tools/display/` 纯函数投影，不执行额外 IO。
- 状态唯一来源：工具生命周期 `pending`/`running`/`completed`/`failed`/`cancelled` 由 `ToolObservation.status` 投影；前端不得建立第二套状态机，也不得从 `args`/`result` 反推展示结果。
- 前端路由：`components/assistant-ui/tools/tool-part.tsx` 的 `routeToolPart` 依据 `data.kind` 与 `presentation.expand_layout` 选择只读布局（`details`/`list`/`diff`/`terminal`/`none`）；禁止按工具名编写专用渲染分支，未知 `kind` 走 `ToolFallback`，不导致消息流崩溃。
- 错误三通道隔离：模型诊断走 `error`/`reason`；UI 短提示走受控 `display_data.status_hint`（约 5 字，来自后端分类映射，不得复制原始异常或 provider 响应）；生命周期走 Transport status。前端绝不展示堆栈、原始异常、原始 prompt、凭据或大段模型正文；失败 `display_data` 不得携带成功态的目标、结果或输出字段。
- 当前工具 `kind` 与布局（汇总，完整字段见契约文档）：`read_file`→`read-file-meta`(`none`)、`search_files`→`file-list`(`list`)、`list_directory`→`directory-list`(`list`)、`write_file`/`replace`/`apply_patch`→`file-changes`(`diff`)、`execute_terminal`→`terminal-result`(`terminal`)、`delete`→`delete-result`(`none`)、`web_search`→`web-search-results`(`list`)、`web_extract`→`web-extract-urls`(`list`)、`delegate_task`→`delegation-result`(`details`)。
- 展示数据不是后端事实源：`display_data` 只服务 UI 渲染与重连恢复；`artifact_data` 只服务文件快照、ChangeSet、回退与审计，不得进入 Assistant Transport；`ToolObservation.content` 不被当作通用 UI 展示数据来源。

## 后端架构

- `api` 负责 HTTP schema、依赖注入和错误映射；`service` 负责领域规则与用例编排；`storage/CRUD` 负责数据库访问。API 和 Runtime 不应绕过依赖装配与 service 直接组织业务 CRUD。
- CRUD 不承载领域状态迁移或事件语义。无外部 Session 时可以完成单次存储事务；传入外部 Session 时加入调用方事务且不自行提交。跨表、状态迁移及提交后发布由 command/use-case/service 负责。
- Run 状态事件只能在数据库条件更新成功后发布，重复状态迁移不得重复发布。workflow 节点只产生普通 conversation event，经统一 workflow 消费边界交给 projector；不得直接发布 Run 状态事实。
- `task_runtime` 是进程内并发协调层：task operation 串行化同一 task 的 Run 创建、编辑、resume 等互斥操作；workspace operation 仲裁运行、删除和关闭冲突。其锁和 registry 不跨进程、不跨重启。
- AgentRuntime/workflow 在后端进程内运行。工具按 `execution_mode` 在线程内或独立进程执行；独立工具进程负责队列通信、取消、超时和进程树清理，但不拥有业务事实，也不构成新服务层。
- Hook 是正式的运行期扩展边界：registry 负责注册和索引，interceptor 负责唯一触发、匹配、拒绝短路和失败安全。文件快照、CodeGraph 索引准备等旁路能力通过 Hook 接入；Hook 异常默认记录并放行，不阻断主 Run。
- CodeGraph 分为两层：`codegraph/` 负责 Kernel 子进程、RPC、握手、健康、有限重启和关闭；`CodeGraphLifecycleService` 负责 workspace index 的初始化、同步、singleflight 和降级。工具与 Hook 只消费 client，不直接管理 Kernel。
- provider/model 配置是数据库事实，由 provider service 管理；运行时模型构建只消费解析后的配置。能力目录不等于连接可用性，provider 连接测试也不等于 Agent Run。
- Observability 是可降级旁路。workflow/service 通过窄接口记录 trace，不直接依赖具体观测实现；初始化、记录或 flush 失败不得改变 Run 结果。
- RuntimeContextManager 是agent上下文唯一管理事实源，负责系统提示词构建、上下文修复加载、run续跑/恢复的上下文管理、上下文压缩（未实现）、上下文token计算、上下文序列管理、模型消息存储和持久化。
    - ContextEntry 作为上下文消息单位，记录消息的run_id、message、sequence。
- context 和 snapshot不需要事务保持强一致性，只需要在读取的时候，保持最终一致性即可


### 架构取舍

context和snapshot允许不一致。比如，AI说：好，我来看看... 。还没完整message，这个时候无需保持一致，允许context有一定滞后。
context是由app/core/context/runtime_context_manager.py维护，ConversationEventProjector和ConversationTaskSnapshotService仅维护快照

## 代码目录边界

- `apps/desktop/src-tauri/`：桌面宿主、Tauri IPC、后端进程生命周期和桌面日志。
- `apps/desktop/src/`、`app/`、`components/`、`hooks/`：React 页面、交互和展示组合。
- `apps/desktop/lib/api/`、`lib/http/`：领域 API 与通用 HTTP 客户端。
- `apps/desktop/lib/assistant/`、`components/assistant*/`：Assistant Transport 契约、runtime 装配和 UI 呈现。
- `apps/backend/app/api/`：HTTP 路由、schema 和错误映射。
- `apps/backend/app/assistant_transport/`：Assistant wire 协议、SSE、命令幂等、snapshot 和事件投影。
- `apps/backend/app/service/`：Task、Workspace、Provider、Run、Delegation、CodeGraph index 等领域用例。
- `apps/backend/app/core/runtime/`、`core/workflows/`、`core/tools/`：Agent 执行、workflow、checkpoint、工具系统和工具隔离。
- `apps/backend/app/hook/`：运行期 Hook 注册、触发和内置旁路能力。
- `apps/backend/app/storage/`：SQLite engine、schema、CRUD、事务和持久化模型。
- `apps/backend/app/task_runtime/`：进程内 task/workspace 并发协调。
- `apps/backend/app/config/`、`core/observability/`：进程配置、日志和可降级观测。
- `apps/backend/app/codegraph/`：CodeGraph Kernel 进程与 RPC 管理，不负责 workspace index 业务编排。

## docstring 约定

源码 docstring 描述具体职责，不重复本文件的架构宣言。公共模块、类、函数至少说明：

- 负责什么，以及明确不负责什么；
- 输入、输出或生成值的语义；
- 持久化、进程、线程、网络或全局状态等副作用；
- 可能抛出的异常及调用方处理方式；
- 涉及生命周期时的初始化、关闭、取消、重试或恢复条件；
- 涉及边界转换时，从输入契约到输出契约的转换规则。

docstring 必须随代码事实更新；算法、字
  `# 项目架构边界

本文件只描述稳定的架构边界、进程边界、事实所有权和开发约束。模块、类、函数的具体职责、参数、返回值、副作用和异常契约由源码 docstring 描述；不要把函数级算法、字段清单、临时方案或容易变化的实现参数堆进本文件。

# 重要提示：当前是绿地项目，不需要做兼容，包括代码兼容和运行兼容，数据库、日志、配置等文件再进行破坏性变更后，可以删除

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
   └─ 按需创建的工具执行子进程
```

开发期还有 Vite 与 Uvicorn reload 辅助进程，不构成新的业务服务层。

## 开发约定

进行改造或代码修复前，须按第零铁律先比较「结构化改造」与「补丁式修复」的优劣再定路线，并保持目标聚焦、说明影响范围。

### 第零铁律（最高优先级）

一切以方便项目长期稳定迭代为最终目标。“最小改动”“零新增依赖”等偏好都只是手段；当它们损害长期可维护性、可读性或可演进性时，以本条为准。

- 进行改造或修复缺陷前，须先比较「结构化改造」与「补丁式修复」两条路线的优劣，以对长期稳定迭代更优者为准；既不默认求改动最小而持续叠加补丁，也不无脑上大重构。选定路线后须说明影响范围。
- 允许进行结构性重构、扩大合理改动范围，不为追求改动小而保留补丁式结构。
- 编辑器、图表、虚拟化、Markdown/Diff、日期、表单校验等通用复杂能力，优先复用成熟且适配项目的方案。
- 引入依赖不是目的。已有项目能力或成熟方案能够解决问题时，禁止重复造轮子。
- 以改动是否让后续迭代更稳、更易维护、更易演进为判断准绳，而不是单纯比较改动行数或依赖数量。

### 五条铁律

1. **单一职责**：一个文件按职责只承担一类主要工作，不以行数作为唯一拆分标准。
2. **不重复造轮子**：能复用项目已有能力、类、函数或成熟方案时，不自行实现等价基础设施。
3. **改动聚焦**：只修改与目标相关的代码；结构性重构明显有利于长期迭代时，可以扩大改动面。
4. **目录结构清晰**：可以持续拆分文件和目录，使能力模块与职责边界仅从目录结构即可初步辨认。
5. **可排查日志**：关键流程必须保留结构化、可落盘日志，不得只依赖临时终端输出。

正确性、可排查性、不重复造轮子、单一职责、目录结构、可读性、改动聚焦和性能同等重要。发生张力时，以第零铁律为最终判据，不使用固定的机械取舍链。

### 禁止的写法

- **禁止用 `getattr(obj, "方法名", None)` + `if callable(...)` 探测本仓类型上的「可选能力 / 可选钩子」再静默跳过。** 契约方法必须直接调用，缺失就让 `AttributeError` 暴露；可选能力在装配期确定并显式声明（`Protocol`/`ABC` 或注册表）；确实要容忍「尚未装配」时，用显式类型判断 + 明确的 `except` 分支 + 日志，不得静默降级。
- 理由：这类写法让「看似在清理 / 通知、实际什么都没做」的静默失效同时骗过调用点、被调用方和测试，只有行为不成立（典型后果：`TaskService` 曾用该写法调用两个并不存在的方法，导致进程内状态永不释放且无任何日志暴露）。凡是注释里承诺了清理 / 通知意图的钩子，必须能在运行时被验证到效果。
- 例外：仅适用于读取第三方或标准库对象的**数据属性**（如 `getattr(exc, "errno", None)`、按平台探测 `os.killpg`），不适用于探测本仓自有类型上的方法。

### 不层层防御校验（链路契约）

加校验、守卫或 `try/except` 前，先顺着调用链路判断「这一层是否该守、上游是否已保证」：

- **只守信任边界，不重复校验。** 每层只守自身输入契约；上游已保证或下游不消费的内容不再重复校验，同一不变量不在调用链多层各验一遍。
- **顺着链路定归属。** 校验落在数据来源边界（持久化 / 前端 / 进程外输入）；中间层直接信任上游契约，缺失就让异常上浮，不为「保险」再包一层。
- **不为低概率事件上过重防御。** 并发竞争、磁盘损坏、手工篡改等极少见情况，用一笔原子操作或一次边界校验覆盖即可，不为它叠多层前置防御；明确「失败可接受」的边界，做好取舍。
- **捕获就地承担语义，不在链路上转手。** 捕获后必须明确处理（记录 + 收敛 / 转换 / 向上抛），不得只为「包住」而捕获；「未找到」等契约在一处明确归属，不靠 `return None` 再 `raise` 又上层 `except` 中转。映射 404 的「未找到」契约统一由 service 抛 `KeyError` 自然上浮、API 一处 `except KeyError → 404` 收敛，service 内部不得 `get` 吞成 `None` 再 `raise` 中转。
- **不为不可能情形设防御（删死代码）。** 上游分支已抛异常被外层捕获、或调用方已保证返回非 `None` 时，不写 `if x is None: raise` 之类的不可达分支；此类死防御无收益且误导维护者，应直接删除。

理由：层层重复校验让契约归属模糊、改一处要改多处，还掩盖「上游已保证」的事实；低概率事件上过重防御无收益且抬升维护成本。本条与「禁止静默失效」互补：那条管捕获后的行为，本条管「该不该捕、捕在哪一层」。

### 日志打印与排查

- React 诊断日志使用 `frontendLog(level, event, msg, { traceId, data, error })`；后端使用 `log.info`、`log.warning`、`log.error` 或 `log.exception`，通过 `extra={"msg": "...", "data": {...}}` 附带说明和业务字段。
- `event` 使用稳定的 snake_case 名称；`task_id`、`run_id` 等标识放入 `data`。同一执行链路复用同一 `trace_id`；前端 HTTP 请求通过 `X-Trace-Id` 传入后端，后端绑定日志上下文。
- 前端 Tauri 日志查看系统级 `.cosir/logs/frontend-YYYY-MM-DD.log`（同日大小分片为 `.1.log`、`.2.log`）；浏览器开发模式查看 WebView/浏览器控制台。
- 后端运行日志查看系统级 `.cosir/logs/backend-YYYY-MM-DD.log`（同日大小分片为 `.1.log`、`.2.log`）；启动、停止或崩溃问题查看系统级 `.cosir/logs/desktop-YYYY-MM-DD.log`、`backend-console-YYYY-MM-DD.log` 及其大小分片；`backend.bootstate.json` 位于系统级 `.cosir/runtime/`。
- 系统级 `.cosir` 由 Tauri 按平台放在用户主目录：**macOS 为 `~/.cosir`，Windows 为 `%USERPROFILE%\.cosir`**；经桌面宿主启动（含 `tauri dev`）时 `CODING_AGENT_DATA_DIR` 由 `spawn_backend` 按 `data_dir` 是否提供**条件注入**，桌面宿主恒传用户主目录，故等价于主目录，无 dev/prod 分支；后端统一追加 `.cosir`。绕过 Tauri 直跑后端时，macOS/Windows 同样回落到用户主目录，其他平台回落到 `<repo>`。系统运行数据包括 `.env`、SQLite、checkpoint、日志和 runtime；工作区级 `.cosir` 仍位于各工作区根目录下。
- 日志和观测是诊断旁路，不是业务事实。目前不做脱敏，日志或观测失败不得阻断 Agent 主流程。

## 进程生命周期边界

- Tauri Rust 主进程是桌面宿主，也是 FastAPI 后端子进程生命周期的唯一所有者。React 不得直接创建、停止或重启后端进程。
- Tauri 显示 WebView 后在后台启动后端；窗口显示与后端 readiness 解耦。React 通过 boot gate 呈现 `starting`、`ready`、`failed`，不得假定页面出现时后端已经可用。
- 后端绑定 `127.0.0.1`，桌面模式由 Tauri 动态分配端口。前端必须读取 runtime config，不得硬编码生产端口；开发或测试 fallback 端口不是桌面生产契约。
- Tauri 负责等待 bootstate 与 `/health`、报告失败、有限重试以及退出时清理后端进程树。`/health` 仅表示 liveness，不代表数据库和模型已全部可用。
- 后端 lifespan 负责初始化和关闭数据库、Run executor、运行期依赖与观测组件。工具子进程由工具执行层负责取消、超时和进程树清理。
- 后端崩溃恢复必须有限且串行，不得无限重启。恢复耗尽后由用户显式重启；生命周期 generation 用于防止旧进程或旧线程覆盖当前状态。
- 后端重启时，遗留 active Run（含 child Run）收敛为 `cancelled`；不得隐式重放旧 Agent 执行。进程内 registry、subscriber 和运行任务不跨重启恢复。

## 进程间接口边界

### Tauri IPC

前端与 Rust 宿主之间的控制面仅包括：

- `backend_status`、`backend_runtime_config`、`restart_backend`、`write_frontend_log`
- `file_access`：`read_selected_attachment_file`、`resolve_selected_attachment_path`

新增桌面能力前必须判断其属于宿主控制面还是后端业务接口。React 业务组件不得绕过现有 runtime/logging 边界自行管理进程。

### 本机 HTTP 与 Assistant Transport

- 领域 JSON API 统一通过前端 `lib/http/client.ts` 处理动态基地址、`no-store`、`X-Trace-Id` 和非 2xx 错误。Assistant SSE、业务 resume 和资源请求由各自 transport/resource 边界管理，但仍须遵守动态地址和 trace 规则。
- Assistant UI wire schema 只属于 `apps/backend/app/assistant_transport/`，不得泄漏到 workflow、领域 service 或 storage。
- `POST /assistant` 创建或继续业务 Run；attach 只订阅既有 Run；state 读取 canonical snapshot；cancel 显式取消 Run。
- Assistant 命令的 `commandId` 仅用于单次请求内的结构化边界与去重（同请求内须唯一），不入库、不承担跨请求幂等。同一 task 同时只允许一个 active Run，command、Run 和初始 snapshot 的创建由同一用例事务保护。
- HTTP/SSE 断开只表示订阅中断，不自动取消或重放业务 Run。前端可以重新 attach 已有 Run；只有明确的 business resume 才能继续业务执行。

## 数据与事实所有权

- 后端拥有 task、workspace、Run、Agent context、model_config 配置、Agent Team 配置（文件系统持久化）与 Agent Team run（SQLite）等持久化事实；前端状态只负责交互和渲染。
- 主业务库为 `<DATA_DIR>/.cosir/storage/app.sqlite3`；运行日志在 `.cosir/logs/`；checkpoint 在 `.cosir/storage/`。三者职责与路径分离，不得跨层复用 session 或事实模型。
- `ConversationRunModel.status` 是对话 Run 生命周期状态的唯一事实源；Agent Team run 由独立的 `AgentTeamRunModel.status` 承载其生命周期，二者各为自身 Run 类型的唯一事实源。Transport snapshot、Agent context 和 LangGraph checkpoint 都不得演化成任一 Run 类型的第二套状态机。
- Agent Team 的 `AgentTeamRunModel` 分 `pending` 确认门槛与 `running` 执行两态；同一主 Run 同时只允许一个待确认 TeamRun（pending 唯一约束），确认边界以 `update_status_if_in` 原子迁移。节点运行快照（`AgentTeamRunState.node_runtime`）只冻结 `allowed_tools`（工具名）；Coordinator 创建节点 Task 时从注册表解析对应 schema，并固化到该 Task 的 `tool_schemas`，不要在 TeamRun 快照重复保存工具 schema。
- Agent context 的持久化事实由 `conversation_task_contexts` 承载；`RuntimeContextManager` 是 Task 级 context 的唯一运行时协调入口和进程内 working copy owner，但不是数据库事实源。
- `ConversationTaskStateService` 负责 Transport snapshot 的重建与投影编排；`TaskRuntimeSpace` 按 taskId 持有 snapshot working copy，首次读取时懒加载重建，后续复用内存对象。`ConversationEventProjector` 只负责把 conversation event 投影到 snapshot。`ConversationStateSnapshot` 面向前端 Transport，不是 Agent context 的镜像。
- context 与 Transport snapshot 允许短暂不一致，以最终一致性收敛。snapshot 普通读取不重复重建；数据库写入后由 projector 或明确的 rebuild 边界更新内存 snapshot。不得为了消除流式时序差异而强行把 Run、context、snapshot 放入一个全局事务。
- LangGraph checkpoint 只服务 workflow 恢复，不代表 Run 生命周期状态。
- `task_runtime`、取消 registry、snapshot subscriber 等属于当前后端进程内的协调状态，不是持久化事实。
- WebView `localStorage` 只保存用户偏好（模型选择、最近 workspace、收藏提示词等）；各类偏好由对应 storage module 管理，不得存储任务或对话事实。

## 前端架构

- React 负责页面、用户交互、HTTP/SSE 客户端和渲染状态；Rust/Tauri 负责窗口与本地进程控制。前端不得直接访问 SQLite、Agent Runtime 或工作区文件来绕过后端业务边界。
- 后端 snapshot 是对话、Run、usage 和 tool parts 的权威读取来源。Assistant UI runtime state 只用于当前渲染和传输控制，不得原样写回后端作为事实。
- `WorkspaceShell` 是 workspace/task 导航的稳定容器；`TaskPage`、初始 snapshot 和 Assistant runtime 按 `taskId` 建立。普通 workspace 数据刷新不应无故卸载正在执行的 Assistant runtime。
- transport resume/attach 只重新订阅已有 Run；business resume 才继续后端执行。前端实现和命名必须保持两种语义可辨认。
- 前端测试分层：Vitest 验证模块行为；Playwright E2E 用 Vite + 内存测试服务，WDIO 经 `desktop-e2e` 驱动真实 Tauri 窗口；都不覆盖后端崩溃恢复，不得视为完整集成测试。

## 工具 UI 渲染边界

工具执行结果的前端渲染是后端 `ToolObservation.display_data` 与前端 renderer 之间的稳定契约，不新增进程或服务；完整字段规范见 `apps/backend/app/core/tools/tool_ui_display_contract.md`，本文件只描述边界。

- 两层契约：静态展示声明 `ToolDisplayHints` 随 `ToolDefinition` 传给客户端，只声明 `verb`/`icon`/`variant`/`surface`/`expandable`/`expand_layout`/`default_open`/`show_result` 等 UI 意图，不含动态结果、渲染函数或业务数据；成功态非空 `ToolObservation.display_data` 必须有稳定 `kind`，由后端 `apps/backend/app/core/tools/display/` 纯函数投影，不执行额外 IO。错误态按受控短提示规则，不带成功态 `kind`。
- human-in-the-loop 请求不是展示数据：工具要求用户先作出决定时，把请求挂在 `ToolObservation.user_input_request`（工具层领域类型 `UserInputRequest`），由 `wait_user` 节点据此挂起图，并在挂起前把它投影进该调用的 tool part 载荷；工具、工作流与前端都不再把审批声明塞进 `display_data`，也不为「是否等待作答」另设布尔字段（请求存在与否即判据）。
- 状态唯一来源：`ToolObservation.status` 表达工具执行结果，取值 `success`/`error`/`cancelled`；Transport 侧工具 part 生命周期为 `pending`/`running`/`completed`/`failed`/`cancelled`，其中 `completed`/`failed`/`cancelled` 由 observation 经 `success→completed`、`error→failed`、`cancelled→cancelled` 投影，`pending`/`running` 为执行前/执行中状态。前端不得建立第二套状态机，也不得从 `args`/`result` 反推展示结果。
- 前端路由：`components/assistant-ui/tools/tool-part.tsx` 通过 `tool-renderer-registry.ts` 按 `data.kind` 与 `presentation.expand_layout` 选择 renderer；通用布局为 `details`/`list`/`diff`/`write`/`terminal`/`none`，专用 renderer 可承载明确的业务交互。禁止按工具名编写专用渲染分支；未知 `kind` 走 `ToolFallback`，不导致消息流崩溃。
- 错误三通道隔离：工具 UI 短提示走受控 `display_data.status_hint`（约 5 字，来自后端分类映射）；Run 错误 `error.message` 可直接使用模型 HTTP 响应体的 `message` 字段供用户排查，但不得透传完整响应体。生命周期走 Transport status。前端不展示堆栈、原始异常、原始 prompt、凭据或大段模型正文；失败 `display_data` 不得携带成功态的目标、结果或输出字段。
- 当前已注册工具 `kind`（布局见契约）：`read_file`→`read-file-meta`、`search_content`→`content-search-results`、`find_files`→`file-list`、`list_directory`→`directory-list`、`write_file`/`patch_write`/`apply_patch`/`delete_file`/`move_file`→`file-changes`，文件工具重复调用→`repeated-call`，`execute_terminal`→`terminal-result`、`terminal_*`→`terminal-session`、`web_search`→`web-search-results`、`web_extract`→`web-extract-urls`、`delegate_task`→`delegation-result`、`child_agent_*`→`child-agent-result`/`child-agent-wait-result`、`propose_agent_configuration`→`agent-configuration-draft`。Agent Team 的 `agent_team`→`agent-team-preview` 与 `propose_agent_team_configuration`→`agent-team-configuration-draft` 已实现但当前未注册。
- 文件变更职责：`apply_patch` 只修改已有文件内容；新建、删除和移动分别由 `write_file`、`delete_file`、`move_file` 负责。
- 展示数据不是后端事实源：`display_data` 只服务 UI 渲染与重连恢复；`artifact_data` 只承载内部工具产物，不得进入 Assistant Transport；`ToolObservation.content` 不被当作通用 UI 展示数据来源。

## 后端架构

- `api` 负责 HTTP schema、依赖注入和错误映射；`service` 负责领域规则与用例编排；`storage/CRUD` 负责数据库访问。API 和 Runtime 不应绕过依赖装配与 service 直接组织业务 CRUD。
- CRUD 不承载领域状态迁移或事件语义。无外部 Session 时可以完成单次存储事务；传入外部 Session 时加入调用方事务且不自行提交。跨表、状态迁移及提交后发布由 command/use-case/service 负责。
- Run 状态事件只能在数据库条件更新成功后发布，重复状态迁移不得重复发布。workflow 节点只产生普通 conversation event，经统一 workflow 消费边界交给 projector；不得直接发布 Run 状态事实。
- `task_runtime` 是进程内并发协调层：task operation 串行化同一 task 的 Run 创建、编辑、resume 等互斥操作；workspace operation 仲裁运行、删除和关闭冲突。其锁和 registry 不跨进程、不跨重启。
- AgentRuntime/workflow 在后端进程内运行。工具按 `execution_mode` 在线程内或独立进程执行；独立工具进程负责队列通信、取消、超时和进程树清理，但不拥有业务事实，也不构成新服务层。
- Hook 是运行期扩展边界：registry 负责注册索引，interceptor 负责触发、匹配、拒绝短路与失败安全；异常默认记录放行，不阻断主 Run。
- provider/model 配置是数据库事实，由 model config service（`ModelConfigService`）管理；运行时模型构建只消费解析后的配置。能力目录不等于连接可用性，provider 连接测试也不等于 Agent Run。
- Observability 是可降级旁路。workflow/service 通过窄接口记录 trace，不直接依赖具体观测实现；初始化、记录或 flush 失败不得改变 Run 结果。
- RuntimeContextManager 是agent上下文唯一管理事实源，负责系统提示词构建、上下文修复加载、run续跑/恢复的上下文管理、上下文压缩（未实现）、上下文token计算、上下文序列管理、模型消息存储和持久化。
    - ContextEntry 作为上下文消息单位，记录消息的run_id、message、sequence。
- context 和 snapshot不需要事务保持强一致性，只需要在读取的时候，保持最终一致性即可

## 代码目录边界

- `apps/desktop/src-tauri/`：桌面宿主、Tauri IPC、后端进程生命周期与桌面日志。
- `apps/desktop/`：`src/`（React 入口）、`app/`（根组件与全局样式）、`components/`（页面与展示）、`hooks/`、`lib/`（api/http/assistant/logging 等客户端与契约）。
- `apps/backend/app/api/`：HTTP 路由、schema 与错误映射。
- `apps/backend/app/assistant_transport/`：Assistant wire 协议、SSE、命令幂等、snapshot 与事件投影。
- `apps/backend/app/service/`：conversation_run/attachment/terminal/model_config/configuration/agent_team 用例编排。
- `apps/backend/app/agent_team/`：Agent Team 领域（配置、状态、注册表、协调器），不含 HTTP 与用例编排。
- `apps/backend/app/models/`：ORM 记录与枚举（`ConversationRunModel`、`*_record` 等）。
- `apps/backend/app/storage/`：SQLite engine、schema、CRUD 与事务；`task_runtime/`：进程内 task/workspace 并发协调。
- `apps/backend/app/core/`：agents/context/llm_provider/runtime/workflows/tools/hook/observability，覆盖 Agent 定义、上下文、模型供应、执行、workflow、工具与可降级观测。
- `apps/backend/app/config/`：进程配置；`utils/`：路径等通用工具；`lifespan.py`/`app.py`/`bootstate.py`：应用装配与启动。

## docstring 约定【必须中文】【**主线任务**执行用户指令，**支线任务**发现docString漂移，并向用户说明，不擅自主动改】

注释与 docstring 是本仓的「记忆」：优先记录**不明显**的信息——设计决策（为什么这样写、为什么不能那样写）、上下游链路契约（谁保证什么输入、谁消费什么输出、边界与事实源在哪）、复杂逻辑的取舍，以及非显而易见的约束与副作用。

- **记不明显的，不记明显的。** 简单代码逻辑（一眼看懂的赋值、循环、返回）与函数名已自解释的简单职责，不必为「凑完整」而写；复杂逻辑（为何分这几步、算法或方案取舍）可以且应当说明。
- **决策与链路契约优先于逻辑复述。** 比「做了什么」更重要的是「为什么这么做、与上下游怎么衔接、踩过什么坑」。例如为何必须在某操作前判定集合、某状态事实源在别处、重复发起会造成什么副作用。
- **避免膨胀，保持精炼。** 没有不明显信息时，短句或省略都好过套话；不堆砌字段清单、参数逐一翻译或显而易见的描述。
- 公共模块、类、函数覆盖以下维度中**适用且不显然**的部分：负责什么与不负责什么、输入/输出语义、持久化/进程/线程/网络/全局状态副作用、可能抛出的异常及调用方处理、生命周期的初始化/关闭/取消/重试/恢复、边界转换规则。

如果**注释**或者**docstring**与**代码事实**不一致，则必须修正注释以及docString 与代码事实对齐；若仅复述明显逻辑、无记忆价值，应删减而非保留。

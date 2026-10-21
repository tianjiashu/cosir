# 项目架构边界

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
   ├─ Agent Runtime / 原生 ReAct workflow（OpenAI SDK）
   ├─ 本机 SQLite
   └─ 按需创建的工具执行子进程
```

开发期还有 Vite 与 Uvicorn reload 辅助进程，不构成新的业务服务层。

## 交互约定

用户提出功能需求时，agent 不应只照字面实现，也不应只反问用户，而应基于对本项目架构边界、铁律、既有能力与成熟方案的掌握，主动补全需求：

- **主动补边界与遗漏点**：如实现「工具调用」时，应想到错误重试、超时、取消、失败安全等用户未提及但必要的维度；实现「侧边栏」时，应想到当下成熟实现方案（虚拟列表、受控组件等）并给出推荐。
- **给有依据的建议，不空泛**：每条补充都关联到项目事实（架构边界、既有能力、第零铁律取舍）或业界成熟方案，说明「为什么该考虑、推荐怎么做」。
- **方案呈现而非替用户拍板**：补全内容作为方案的一部分交用户确认；用户不采纳时尊重决定，不强行叠入。
- **优先复用与对齐**：补全时先查项目已有能力和已采用的方案，避免重复造轮子或引入与现有架构冲突的做法。


## 开发约定

不仅是进行改造或代码修复前，还有在改造过程中发现漏洞、缺陷、问题，都必须按第零铁律先比较「结构化改造」与「补丁式修复」的优劣再定路线，并保持目标聚焦、说明影响范围。

### 第零铁律（最高优先级）

一切以方便项目 长期稳定迭代为【简洁高效的架构】作为最终目标。“最小改动”“零新增依赖”等偏好都只是手段；当它们损害长期可维护性、可读性或可演进性时，以本条为准。

- 进行改造或修复缺陷前，须先比较「结构化改造」与「补丁式修复」两条路线的优劣，以对长期稳定迭代更优者为准；既不默认求改动最小而持续叠加补丁，也不无脑上大重构。选定路线后须说明影响范围。
- 允许进行结构性重构、扩大合理改动范围，不为追求改动小而保留补丁式结构。
- 编辑器、图表、虚拟化、Markdown/Diff、日期、表单校验等通用复杂能力，优先复用成熟且适配项目的方案。
- 引入依赖不是目的。已有项目能力或成熟方案能够解决问题时，禁止重复造轮子。
- 以改动是否让后续迭代更稳、更易维护、更易演进为判断准绳，而不是单纯比较改动行数或依赖数量。
- 无论是结构化改造还是补丁修复，不需要面面具到，代码设计要学会做取舍，不怕改动大，既要做代码 加法，也要做代码减法。

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

### 抽象按需引入（不预留接口）

默认直接写具体类与函数。抽象基类（`ABC`）、`Protocol`、抽象接口及配套工厂，只在以下情形引入：

- **已有多个真实实现**：同一契约当前就有两个及以上生产实现在使用；测试替身不算，「将来可能扩展」「方便替换」也不构成理由。
- **设计模式或架构边界确实需要**：如注册表 / 插件需按同一契约装配多种实现、策略需在运行期切换，或为遵守依赖方向必须做依赖倒置（例：`core` 经 `EventSink` 回调把事件交给 `assistant_transport`，不能反向 import）。

配套约束：

- 单一实现不配工厂类或工厂函数，调用方直接构造、直接使用该实现。
- 测试替身依靠鸭子类型与 monkeypatch 注入，不为可测试性新增抽象。
- 发现只剩一个实现的抽象时，按第零铁律评估后删除，类型标注改回具体类；出现第二个真实实现时再提取接口。

理由：单实现抽象让读者多跳一层才能找到真实逻辑，签名改动要同步两处，还常伴随「预留但从未使用」的扩展点，抬升维护成本却换不来可替换性。

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
- 系统级 `.cosir` 由 Tauri 按平台放在用户主目录：**macOS 为 `~/.cosir`，Windows 为 `%USERPROFILE%\.cosir`**；经桌面宿主启动（含 `tauri dev`）时 `CODING_AGENT_DATA_DIR` 由 `spawn_backend` 按 `data_dir` 是否提供**条件注入**，桌面宿主恒传用户主目录，故等价于主目录，无 dev/prod 分支；后端统一追加 `.cosir`。绕过 Tauri 直跑后端时，macOS/Windows 同样回落到用户主目录，其他平台回落到 `<repo>`。系统运行数据包括 `.env`、SQLite、日志和 runtime；工作区级 `.cosir` 仍位于各工作区根目录下。
- 日志和观测是诊断旁路，不是业务事实。目前不做脱敏，日志或观测失败不得阻断 Agent 主流程。

## 进程生命周期边界

- Tauri Rust 主进程是桌面宿主，也是 FastAPI 后端子进程生命周期的唯一所有者。React 不得直接创建、停止或重启后端进程。
- Tauri 显示 WebView 后在后台启动后端；窗口显示与后端 readiness 解耦。React 通过 boot gate 呈现 `starting`、`ready`、`failed`，不得假定页面出现时后端已经可用。
- 后端绑定 `127.0.0.1`，桌面模式由 Tauri 动态分配端口。前端必须读取 runtime config，不得硬编码生产端口；开发或测试 fallback 端口不是桌面生产契约。
- Tauri 负责等待 bootstate 与 `/health`、报告失败、有限重试以及退出时清理后端进程树。`/health` 仅表示 liveness，不代表数据库和模型已全部可用。
- 后端 lifespan 负责初始化和关闭数据库、`WorkflowSession` 的 Run 执行、运行期依赖与观测组件。工具子进程由工具执行层负责取消、超时和进程树清理。
- 交互终端归 Task 所有，允许同一 Task 的后续 Run 复用。Run 完成、失败、取消或历史 Run 删除不关闭终端；Agent 显式关闭、shell 自然退出、Task 删除提交后或后端退出时清理。活会话由 `TerminalSessionService` 管理，不进入 Run 执行进度，不跨后端重启恢复；前端仅订阅只读预览。
- 后端崩溃恢复必须有限且串行，不得无限重启。恢复耗尽后由用户显式重启；生命周期 generation 用于防止旧进程或旧线程覆盖当前状态。
- 后端重启时，遗留 running Run（含 child Run）收敛为 `cancelled`，显式恢复从 model node 重新请求；waiting Run 保留待决请求，用户提交决定后从 wait node 继续。入口节点由 workflow 按 Run 持久化进度推导（待决调用已绑定决定 → wait node，其余 → model node），调用方不传恢复模式。inbox 保留，不得隐式重放旧 Agent 执行。进程内 registry、subscriber 和运行任务不跨重启恢复。

## 进程间接口边界

### Tauri IPC

前端与 Rust 宿主之间的控制面仅包括：

- `backend_status`、`backend_runtime_config`、`restart_backend`、`write_frontend_log`
- `file_access`：`read_selected_attachment_file`、`resolve_selected_attachment_path`

新增桌面能力前必须判断其属于宿主控制面还是后端业务接口。React 业务组件不得绕过现有 runtime/logging 边界自行管理进程。

### 本机 HTTP 与 Assistant Transport

- 领域 JSON API 统一通过前端 `lib/http/client.ts` 处理动态基地址、`no-store`、`X-Trace-Id` 和非 2xx 错误。Assistant SSE、业务 resume 和资源请求由各自 transport/resource 边界管理，但仍须遵守动态地址和 trace 规则。
- Assistant UI wire schema 只属于 `apps/backend/app/assistant_transport/`，不得泄漏到 workflow、领域 service 或 storage。
- 命令只对 Task 或消息寻址，不携带 run_id：`POST /tasks/{id}/messages`（发送）、`/messages/{message_id}/edit`（编辑重跑）、`/resume`（恢复）、`/decisions`（HIL 决定）、`/cancel`（停止）、`/tool-calls/{call_id}/cancel`（取消单个工具调用），返回 JSON 回执；`/assistant/attach` 只订阅 Task 当前 Run；`/assistant/state` 读取 canonical snapshot。Run 身份由后端按 Task 最新 Run 解析，前端只把 Run 用于渲染分组。
- 调度规则只在 `WorkflowSession`（`service/conversation_run/workflow_session.py`）：每个入口在 Task 闸门内「读状态 → 条件迁移 → 启动执行」。它是新建 Run 与启动执行的唯一路径，子 Agent、Team 节点经 `open_run` 进入；Run 直接以 running 创建，没有待认领态。它同时持有执行登记（每个 Task 至多一个后台执行）：执行结束后先完成交接、等待态取消与取消传播，再释放登记并取走 Task 取消信号（信号的唯一清理点）。单次 Run 由 `service/conversation_run/run_driver.py` 的 `drive_run` 装配 `RuntimeContext`（只含本次 Run / Task 级事实与对象、事件回调和驱动期状态）并驱动 core 的 `ReactLikeWorkflow`，它也是 Run 失败的唯一异常边界；进程级单例（Run 状态写入、inbox、工具系统、Agent registry、终端会话服务等）由 workflow 节点与工具 handler 经 `app.service.depends` / `app.config.configuration` 直接取用，不经 `RuntimeContext` 或 `ToolExecutionContext` 转手；Run / Task 级事实与跨层回调（如 `EventSink`）才经它们注入，不经转发门面。同一 task 同时只允许一个 active Run 和一个执行者。Run、初始 inbox 输入和 Task 当前 Run 指针由同一用例事务保护；snapshot 在提交后投影。
- HTTP/SSE 断开只表示订阅中断，不自动取消或重放业务 Run。前端可以重新 attach 已有 Run；只有明确的 business resume 才能继续业务执行。

## 数据与事实所有权

- 后端拥有 task、workspace、Run、Agent context、model_config 配置、Agent Team 配置（文件系统持久化）与 Agent Team run（SQLite）等持久化事实；前端状态只负责交互和渲染。
- 主业务库为 `<DATA_DIR>/.cosir/storage/app.sqlite3`；运行日志在 `.cosir/logs/`。业务库与日志职责、路径分离，不得跨层复用 session 或事实模型。
- `ConversationRunModel.status` 是对话 Run 生命周期状态的唯一事实源；Agent Team run 由独立的 `AgentTeamRunModel.status` 承载其生命周期，二者各为自身 Run 类型的唯一事实源。Transport snapshot 与 Agent context 都不得演化成任一 Run 类型的第二套状态机。
- Agent Team 的 `AgentTeamRunModel` 分 `pending` 确认门槛与 `running` 执行两态；同一主 Run 同时只允许一个待确认 TeamRun（pending 唯一约束），确认边界以 `update_status_if_in` 原子迁移。节点运行快照（`AgentTeamRunState.node_runtime`）只冻结 `allowed_tools`（工具名）；Coordinator 创建节点 Task 时从注册表解析对应 schema，并固化到该 Task 的 `tool_schemas`，不要在 TeamRun 快照重复保存工具 schema。
- Agent context 的持久化事实由 `conversation_task_contexts` 承载；`RuntimeMessageContextManager` 是 Task 级 context 的唯一运行时协调入口和进程内 working copy owner，但不是数据库事实源。
- `ConversationTaskStateService` 负责 Transport snapshot 的重建与投影编排；`TaskRuntimeSpace` 按 taskId 持有 snapshot working copy，首次读取时懒加载重建，后续复用内存对象。`ConversationEventProjector` 只负责把 conversation event 投影到 snapshot。`ConversationStateSnapshot` 面向前端 Transport，不是 Agent context 的镜像。
- 模型流式输出由 SDK 聚合，同一次调用持续替换同一条 context 记录，流式期间不纳入模型上下文；响应完整且通过工具准入后才设置 `include_in_context=True`。中断输出保留供 Transport 重建，续跑不加载；不维护另一份流式聚合缓存。inbox 变更只投影排队消息，不以全量重建覆盖正在输出的 Transport 内容。
- context 与 Transport snapshot 允许短暂不一致，以最终一致性收敛。snapshot 普通读取不重复重建；数据库写入后由 projector 或明确的 rebuild 边界更新内存 snapshot。不得为了消除流式时序差异而强行把 Run、context、snapshot 放入一个全局事务。
- Run 同时拥有生命周期、冻结执行选项与持久执行进度，不保存用户正文、图片或附件副本，也不维护独立 checkpoint。普通续跑从模型边界继续，等待恢复只消费已持久化的当前请求及决定；不重放已执行工具。
- Task inbox 持久化尚未消费的用户输入、发起配置与投递方式（`immediate` / `after_run`）；发起配置仅用于交接新 Run，不修改正在执行的 Run，也不进入 context。每次普通模型调用前，将当前批次的 `immediate` 输入按序原子转入 context；`after_run` 输入等当前 Run 正常完成后交给新 Run，新 Run 开始时全部待消费输入转为 `immediate`。一条 Run 可包含多条 user message。最终回答结束当前 Run，旧执行者释放后才为剩余 inbox 新建 Run（收到停止请求时不交接）。没有活动 Run（含取消、失败后）时发送直接新开 Run；等待审批时 `immediate` 发送按驳回结算待决调用，消息作为反馈并入。
- 编辑定位稳定消息身份，只允许 Task 已到终态、目标为最新一条已消费的用户消息：截断其后 context 并重置同一 Run，从模型节点重跑；排队中的消息不可编辑。外部工具副作用不回滚。
- `task_runtime`、取消 registry、snapshot subscriber 等属于当前后端进程内的协调状态，不是持久化事实。
- WebView `localStorage` 只保存用户偏好（模型选择、最近 workspace、收藏提示词等）；各类偏好由对应 storage module 管理，不得存储任务或对话事实。

## 前端架构

- React 负责页面、用户交互、HTTP/SSE 客户端和渲染状态；Rust/Tauri 负责窗口与本地进程控制。前端不得直接访问 SQLite、Agent Runtime 或工作区文件来绕过后端业务边界。
- 后端 snapshot 是对话、Run、usage 和 tool parts 的权威读取来源。Assistant UI runtime state 只用于当前渲染和传输控制，不得原样写回后端作为事实。
- `AppLayout`（react-router 布局路由，路由表见 `src/router.tsx`）是 workspace/task 导航的稳定容器；`TaskPage`、初始 snapshot 和 Assistant runtime 按 `taskId` 建立，并由布局直接渲染：切到配置或上下文观测时最近的任务页保活，只有进入其他任务或新对话才替换。普通 workspace 数据刷新不应无故卸载正在执行的 Assistant runtime。
- transport resume/attach 只重新订阅已有 Run；business resume 才继续后端执行。前端实现和命名必须保持两种语义可辨认。
- 业务命令统一经 `lib/assistant/task-commands.ts` 按 Task / 消息寻址；回执 snapshot 只用于提交后的首次导入，后续输出由 attach 订阅。运行中发送的投递方式（立即并入 / 本轮结束后）是用户偏好，由 `lib/input-delivery-storage.ts` 管理；排队消息的 `delivery` 由后端 snapshot 投影，前端不自行推断。
- 前端测试分层：Vitest 验证模块行为；Playwright E2E 用 Vite + 内存测试服务，WDIO 经 `desktop-e2e` 驱动真实 Tauri 窗口；都不覆盖后端崩溃恢复，不得视为完整集成测试。

## 工具 UI 渲染边界

工具执行结果的前端渲染是后端 `ToolObservation.display_data` 与前端 renderer 之间的稳定契约，不新增进程或服务；完整字段规范见 `apps/backend/app/core/tools/tool_ui_display_contract.md`，本文件只描述边界。

- 两层契约：静态展示声明 `ToolDisplayHints` 随 `ToolDefinition` 传给客户端，只声明 `verb`/`icon`/`variant`/`surface`/`expandable`/`expand_layout`/`default_open`/`show_result` 等 UI 意图，不含动态结果、渲染函数或业务数据；成功态非空 `ToolObservation.display_data` 必须有稳定 `kind`，由后端 `apps/backend/app/core/tools/display/` 纯函数投影，不执行额外 IO。错误态按受控短提示规则，不带成功态 `kind`。
- human-in-the-loop 请求不是展示数据：工具要求用户先作出决定时，把请求挂在 `ToolObservation.user_input_request`（工具层领域类型 `UserInputRequest`），由 `wait_user` 步骤据此保存 Run 等待数据并退出执行，并在挂起前把它投影进该调用的 tool part 载荷；用户或系统给出的决定只在写入 Run 进度前经 `bind_decision` 校验一次，`wait_user` 信任已绑定的决定；工具、工作流与前端都不再把审批声明塞进 `display_data`，也不为「是否等待作答」另设布尔字段（请求存在与否即判据）。
- 状态唯一来源：`ToolObservation.status` 表达工具执行结果，取值 `success`/`error`/`cancelled`；Transport 侧工具 part 生命周期为 `pending`/`running`/`completed`/`failed`/`cancelled`，其中 `completed`/`failed`/`cancelled` 由 observation 经 `success→completed`、`error→failed`、`cancelled→cancelled` 投影，`pending`/`running` 为执行前/执行中状态。前端不得建立第二套状态机，也不得从 `args`/`result` 反推展示结果。
- 前端路由：`components/assistant-ui/tools/tool-part.tsx` 通过 `tool-renderer-registry.ts` 按 `data.kind` 与 `presentation.expand_layout` 选择 renderer；通用布局为 `details`/`list`/`diff`/`write`/`terminal`/`none`，专用 renderer 可承载明确的业务交互。禁止按工具名编写专用渲染分支；未知 `kind` 走 `ToolFallback`，不导致消息流崩溃。
- 错误三通道隔离：工具 UI 短提示走受控 `display_data.status_hint`（约 5 字，来自后端分类映射）；Run 错误 `error.message` 可直接使用模型 HTTP 响应体的 `message` 字段供用户排查，但不得透传完整响应体。生命周期走 Transport status。前端不展示堆栈、原始异常、原始 prompt、凭据或大段模型正文；失败 `display_data` 不得携带成功态的目标、结果或输出字段。
- 当前已注册工具 `kind`（布局见契约）：`read_file`→`read-file-meta`、`search_content`→`content-search-results`、`find_files`→`file-list`、`list_directory`→`directory-list`、`write_file`/`patch_write`/`apply_patch`/`delete_file`/`move_file`→`file-changes`，文件工具重复调用→`repeated-call`，`execute_terminal`→`terminal-result`、`terminal_*`→`terminal-session`、`web_search`→`web-search-results`、`web_extract`→`web-extract-urls`、`delegate_task`→`delegation-result`、`child_agent_*`→`child-agent-result`/`child-agent-wait-result`、`propose_agent_configuration`→`agent-configuration-draft`。Agent Team 的 `agent_team`→`agent-team-approval` / `agent-team-execution` 与 `propose_agent_team_configuration`→`agent-team-configuration-draft` 已注册，是否开放由 Agent 工具选择与 Run 授权决定。
- 文件变更职责：`apply_patch` 只修改已有文件内容；新建、删除和移动分别由 `write_file`、`delete_file`、`move_file` 负责。
- 展示数据不是后端事实源：`display_data` 只服务 UI 渲染与重连恢复；`artifact_data` 只承载内部工具产物，不得进入 Assistant Transport；`ToolObservation.content` 不被当作通用 UI 展示数据来源。

## 后端架构

- `api` 负责 HTTP schema、依赖注入和错误映射；`service` 负责领域规则与用例编排；`storage/CRUD` 负责数据库访问。API 和 Runtime 不应绕过依赖装配与 service 直接组织业务 CRUD。
- CRUD 不承载领域状态迁移或事件语义。无外部 Session 时可以完成单次存储事务；传入外部 Session 时加入调用方事务且不自行提交。跨表、状态迁移及提交后发布由 command/use-case/service 负责。
- Run 状态事件只能在数据库条件更新成功后发布，重复状态迁移不得重复发布。workflow 节点只产生普通 conversation event，经统一 workflow 消费边界交给 projector；不得直接发布 Run 状态事实。
- `task_runtime` 是进程内并发协调层：task operation 串行化同一 task 的 Run 创建、编辑、resume 等互斥操作；workspace operation 仲裁运行、删除和关闭冲突。其锁和 registry 不跨进程、不跨重启。
- `drive_run` 与 workflow 在后端进程内运行。工具按 `execution_mode` 在线程内或独立进程执行；独立工具进程负责队列通信、取消、超时和进程树清理，但不拥有业务事实，也不构成新服务层。
- Hook 是运行期扩展边界：registry 负责注册索引，interceptor 负责触发、匹配、拒绝短路与失败安全；异常默认记录放行，不阻断主 Run。
- 模型工具统一导出 strict function schema，复用 OpenAI SDK 从参数模型生成，再做必要的平台收窄并固化到 Task；模型请求不临时重写 schema。工具执行入口仍负责本地业务校验。
- provider/model 配置是数据库事实，由 model config service（`ModelConfigService`）管理；运行时模型构建只消费解析后的配置。能力目录不等于连接可用性，provider 连接测试也不等于 Agent Run。
- 诊断使用本地结构化日志与上下文观测页，不接入外部 tracing；日志失败不得改变 Run 结果，上下文仍由既有持久化事实管理。
- RuntimeMessageContextManager 是 Task 上下文的运行时协调入口与 working copy owner，负责系统提示词、消息序列、SDK 消息持久化和工具协议闭合；持久化事实归 context 表。输入服务负责 inbox 原子转移，Transport 服务负责提交后投影，Run service 负责生命周期与恢复决策。
    - ContextEntry 作为上下文消息单位，承载稳定消息身份、Run 归属与排序；正文采用 OpenAI SDK 请求消息格式，附属信息按消息角色使用单一强类型 metadata 保存；用户展示绑定、模型响应信息和工具展示事实不进入模型请求。
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
- `apps/backend/app/core/`：agents/context/llm_provider/workflow/tools/hook/events，覆盖 Agent 定义、上下文、模型供应、workflow（含取消信号）、工具、运行期扩展与执行事件。`core` 不依赖 `assistant_transport`：执行事件只定义事实，经 `EventSink` 回调交给消费方；workflow 与工具的其余协作者直接使用具体类型（见「抽象按需引入」）；Transport snapshot 的投影在 `assistant_transport/projection/`。
- `apps/backend/app/config/`：进程配置；`utils/`：路径等通用工具；`lifespan.py`/`app.py`/`bootstate.py`：应用装配与启动。

## docstring 约定【必须中文】【**主线任务**执行用户指令，**支线任务**发现docString漂移，并向用户说明，不擅自主动改】

注释与 docstring 是本仓的「记忆」：优先记录**不明显**的信息——设计决策（为什么这样写、为什么不能那样写）、上下游链路契约（谁保证什么输入、谁消费什么输出、边界与事实源在哪）、复杂逻辑的取舍，以及非显而易见的约束与副作用。

- **记不明显的，不记明显的。** 简单代码逻辑（一眼看懂的赋值、循环、返回）与函数名已自解释的简单职责，不必为「凑完整」而写；复杂逻辑（为何分这几步、算法或方案取舍）可以且应当说明。
- **决策与链路契约优先于逻辑复述。** 比「做了什么」更重要的是「为什么这么做、与上下游怎么衔接、踩过什么坑」。例如为何必须在某操作前判定集合、某状态事实源在别处、重复发起会造成什么副作用。
- **避免膨胀，保持精炼。** 没有不明显信息时，短句或省略都好过套话；不堆砌字段清单、参数逐一翻译或显而易见的描述。
- 公共模块、类、函数覆盖以下维度中**适用且不显然**的部分：负责什么与不负责什么、输入/输出语义、持久化/进程/线程/网络/全局状态副作用、可能抛出的异常及调用方处理、生命周期的初始化/关闭/取消/重试/恢复、边界转换规则。

如果**注释**或者**docstring**与**代码事实**不一致，则必须修正注释以及docString 与代码事实对齐；若仅复述明显逻辑、无记忆价值，应删减而非保留。

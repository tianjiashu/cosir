# 项目架构边界

本文件只描述稳定的架构边界、进程边界、事实所有权和开发约束。模块、类、函数的具体职责、参数、返回值、副作用和异常契约由源码 docstring 描述；不要把函数级算法、字段清单、临时方案或容易变化的实现参数堆进本文件。

# 重要提示：当前是绿地项目，不需要做兼容，包括代码兼容和运行兼容，数据库、日志、配置等文件再进行破坏性变更后，可以删除

## 产品形态与运行拓扑

本项目是单用户、本机运行的桌面 Agent。前后端分离用于隔离职责和进程；HTTP/SSE 是 localhost 进程边界，不代表公网服务或独立部署边界。

除非代码事实或明确需求证明必要，不引入认证、多租户、云端队列、Redis、Postgres、Kubernetes 或公网服务安全方案。

```text
Electron 桌面应用
├─ main 进程（apps/desktop/electron/main）：窗口、托盘、宿主 IPC、后端生命周期
├─ 渲染进程（Chromium）：React 前端，经 preload 暴露的 window.cosirHost 访问宿主
└─ Python/FastAPI 后端子进程
   ├─ Agent Runtime / 原生 ReAct workflow（OpenAI SDK）
   ├─ 本机 SQLite
   └─ 按需创建的工具执行子进程
```

开发期还有 Vite 与 Uvicorn reload 辅助进程，不构成新的业务服务层。

能力归哪个进程，按它离不开的运行时依赖与事实所有者定，不按「风格统一」定：只有 Electron 才能提供的（窗口、原生对话框、`WebContentsView` / CDP 驱动的内置浏览器）归宿主；由 Agent 工具、Task 生命周期或 snapshot 消费的（交互终端会话、工具子进程）归后端，即使其底层实现（如 PTY sidecar）放在哪个进程都能拉起。宿主工具链路只由后端发起请求、宿主无法主动通知后端，需要向后端上报事件的能力放进宿主时须把这一代价计入。

## 交互约定

用户对需求的描述常停留在表象，或直接给出自己设想的做法；agent 掌握项目全貌与业界成熟方案，应据此理解并还原真实需求，不只照字面实现，也不只反问用户：

- **先还原需求，再给方案**：区分用户看到的现象、想达成的目标与提出的做法——做法只是手段，有更合适的路线时说明理由；描述不清时，结合代码事实给出自己最合理的理解并标明假设，只就会改变方案的分歧点提问。
- **讲清原理**：用户只看到表象时，用对方能理解的方式说明背后的机制——问题为何出现、方案为何这样设计、成熟方案通常怎么做与取舍，让用户能判断方案是否对路。
- **主动补边界与遗漏点**：如实现「工具调用」时，应想到错误重试、超时、取消、失败安全等用户未提及但必要的维度；实现「侧边栏」时，应想到当下成熟实现方案（虚拟列表、受控组件等）并给出推荐。
- **理解与建议都要有依据**：每条理解与补充都关联到读过的代码、项目事实（架构边界、既有能力、第零铁律取舍）或业界成熟方案，说明「为什么该考虑、推荐怎么做」；不凭印象下判断，不确定处说明置信度。
- **优先复用与对齐**：补全时先查项目已有能力和已采用的方案，避免重复造轮子或引入与现有架构冲突的做法。
- **方案呈现而非替用户拍板**：理解与补全作为方案的一部分交用户确认；用户坚持原想法时说明风险，之后尊重决定，不强行叠入。


## 开发约定

### 第零铁律（最高优先级）

**目标**：一切改动以「支撑项目长期稳定迭代的简洁高效架构」为最终目标。「最小改动」「零新增依赖」「不动既有代码」等偏好只是手段；与长期可维护性、可读性、可演进性冲突时，以本条为准。

**适用时机**：新增功能、修复缺陷、重构都先按本条定路线再动手。任务类型按入口定；调查中类型变了（修复的根因在契约或结构 → 按重构执行，新增发现现有结构容不下 → 先重构再新增）写进调查结论；实施中途发现原路线或类型不成立，停下重新定。执行途中新发现的问题：与当前目标相关的并入本次调查；无关的记下并向用户说明，不在本次处理。

**按任务类型定调查重点**：

- **修复：先定根因层级。** 问题出在哪一层、哪个事实源、哪条契约，就在那里解决；不在症状处打补丁，也不在调用链多处各补一刀。表象出现的位置不等于根因所在：根因可能是局部逻辑、契约或整体结构，在契约或结构时按重构的要求执行。
  - **列问题清单**：每条写明现象、证据（`文件:行`）、初判根因层级与置信度、待验证假设。
  - **警惕结构性信号**：同类问题反复出现或需多处同步修补；修复要靠特例、标志位才能成立；同一事实有两个来源或靠手工同步；依赖方向反了、循环依赖或模块知道不该知道的内部细节；职责放在哪层都说得通；测试需要大量无关上下文。出现这些信号时按结构问题调查，不在单点上找答案。
- **新增：先定归属再写。** 查已有能力与事实源，确定新功能落在哪一层、谁拥有新事实、影响哪些契约；再判断现有结构能否自然容纳——需要特例、旁路或第二个事实源才放得进去时，先重构再新增。不预留扩展点（见「抽象按需引入」）。
- **重构：先定终态再动手。** 说清目标结构与动机（解决哪个结构性问题）；查清全部调用方与消费方；区分「行为不变」与「有意改变的行为」并分别说明；大重构拆成每步可验证的阶段。

**调查方法**（三类共用）：

- **深度与风险相称**：跨层或跨进程边界、涉及事实源 / 状态机 / 契约、初判置信度低、改动会改变契约或数据，出现任一情形都不凭第一层代码定路线；局部、落点明确且证据确凿时直接定路线，不为走流程而深挖。
- **按方向深读**：链路方向沿上游调用链（谁产生输入、谁保证契约）、下游消费方（谁读取、改动波及谁）、仓内同类写法与已有能力，读到事实源或信任边界；结构方向看职责边界、事实源归属、依赖关系与数据流，对照本文件的架构边界。再往下读已不改变结论时停止。
- **按需委派**：运行环境支持子 Agent 时，由主 Agent 按「独立调查方向数 × 单方向阅读量」决定是否委派、委派几个（一个方向一个子 Agent，不重叠；疑似结构问题时单设结构方向）；单方向、少量文件时自己读。简报只给现象、证据或需求，不写主 Agent 倾向哪个结论；有多个候选解释时并列给出、不排序，要求子 Agent 独立判断并找出反例或其他解释；子 Agent 只读不改，返回带 `文件:行` 的证据、对调查对象的判断（修复为根因层级，新增为归属，重构为影响面）和可选解法。
- **汇总核对**：子 Agent 结论是线索不是证据，主 Agent 抽查关键论断引用的代码，冲突处补查；再按「判断方法」选路线，按「交付要求」交付。

**判断方法**：

- **比较两条路线。** 「结构化改造」调整职责、边界或数据流，让问题不再可能出现、需求自然落位；「补丁式修复」在现有结构上局部修正。补丁仅在问题确属局部、现有结构本身合理、且不引入新特例或隐式耦合时可取；否则走结构化改造。既不默认求改动最小而持续叠补丁，也不无脑上大重构。
- **以迭代成本为准绳，不以改动量为准绳。** 自问：改完后下一次同类需求是否更容易做、更难做错；读者是否更快找到真实逻辑。改动行数和依赖数量不是判据，不怕改动大。
- **加法与减法并重。** 引入新路径时同步删除被取代的旧路径、死代码、只剩单实现的抽象与过时注释（绿地项目不保留兼容分支），不让新旧两套并存。
- **复用优先于自造。** 先查项目已有能力，再看成熟方案（编辑器、图表、虚拟化、Markdown/Diff、日期、表单校验等通用复杂能力尤其如此）；依赖既非目的也非禁区，以是否适配项目、是否降低长期维护成本取舍。
- **有取舍，不求面面俱到。** 聚焦主路径与真实风险，低概率情形在一处边界兜住即可（见「不层层防御校验」）。

**交付要求**：动手前（或改路线时）交用户确认：调查结论（修复时附问题清单）、选定路线及理由、影响范围（涉及模块、契约 / 数据 / 行为变化、需同步的测试与文档），以及刻意不做的部分；实施中保持目标聚焦，不顺手夹带无关改动。

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
- 前端日志查看系统级 `.cosir/logs/frontend-YYYY-MM-DD.log`（经宿主写入，同日大小分片为 `.1.log`、`.2.log`）；浏览器开发模式没有宿主，只看浏览器控制台。
- 后端运行日志查看系统级 `.cosir/logs/backend-YYYY-MM-DD.log`（同日大小分片为 `.1.log`、`.2.log`）；启动、停止或崩溃问题查看系统级 `.cosir/logs/desktop-YYYY-MM-DD.log`、`backend-console-YYYY-MM-DD.log` 及其大小分片；`backend.bootstate.json` 位于系统级 `.cosir/runtime/`。
- 系统级 `.cosir` 由宿主按平台放在用户主目录：**macOS 为 `~/.cosir`，Windows 为 `%USERPROFILE%\.cosir`**，其他平台为 Electron 应用数据目录（`electron/main/paths.ts`）；设置了 `CODING_AGENT_DATA_DIR` 时宿主以它为数据根（桌面 E2E 与开发期隔离数据用）。宿主拉起后端时恒注入 `CODING_AGENT_DATA_DIR`，开发与打包无分支；后端统一追加 `.cosir`。绕过宿主直跑后端时，macOS/Windows 同样回落到用户主目录，其他平台必须显式设置 `CODING_AGENT_DATA_DIR`（否则启动报错）。系统运行数据包括 `.env`、SQLite、日志和 runtime；工作区级 `.cosir` 仍位于各工作区根目录下。
- 日志和观测是诊断旁路，不是业务事实。后端日志目前不做脱敏；前端日志只对错误消息与堆栈里形似凭据的文本脱敏（`lib/logging/frontend-log.ts`）。日志或观测失败不得阻断 Agent 主流程。

## 进程生命周期边界

- Electron main 进程是桌面宿主，也是 FastAPI 后端子进程生命周期的唯一所有者（`electron/main/backend/supervisor.ts`）。React 不得直接创建、停止或重启后端进程。
- 宿主显示窗口后在后台启动后端；窗口显示与后端 readiness 解耦。React 通过 boot gate 呈现 `starting`、`ready`、`failed`，不得假定页面出现时后端已经可用。
- 后端绑定 `127.0.0.1`，桌面模式由宿主动态分配端口。前端只从宿主的后端快照（`window.cosirHost.backend`，带 `generation`，过期快照丢弃）取地址，订阅推送、不轮询，不得硬编码生产端口；开发或测试 fallback 端口不是桌面生产契约。
- 宿主负责等待 bootstate 与 `/health`、报告失败、有限重试以及退出时清理后端进程树（先关闭后端 stdin 请求优雅退出，宽限期后强杀进程树）。`/health` 仅表示 liveness，不代表数据库和模型已全部可用。
- 宿主存活契约：后端的 stdin 是宿主持有、从不写入的管道（`CODING_AGENT_PARENT_PIPE=stdin`），后端读到 EOF 即经 uvicorn 优雅关闭自行退出（`app/parent_watch.py`）。宿主主动停止时关闭这条管道，宿主崩溃或被强杀时由操作系统关闭它，两者是同一条退出路径，后端不会被遗留成孤儿进程。
- 关闭主窗口只隐藏到托盘；真正退出只有一条路径：托盘「退出并停止运行」、Cmd+Q、应用菜单与 Dock 退出都经 `before-quit` → 退出确认（有运行中终端会话时提醒）→ `will-quit` 停止后端。应用单实例，二次启动只唤起已有窗口。
- 后端 lifespan 负责初始化和关闭数据库、各 Task 的调度 actor（经 `task_runtime_spaces`）与 Run 执行、运行期依赖与观测组件。工具子进程由工具执行层负责取消、超时和进程树清理。
- MCP 服务连接归进程级 `MCPConnectionPool`（`app/tools/mcp/`）所有：在独立事件循环线程上每个服务一条长连接（stdio 服务即常驻子进程，stderr 落 `.cosir/logs/mcp/`），首次调用时建立、连接指纹变化或断线后重建、空闲回收，后端退出时经 `close_service_dependencies` 关闭；失败的调用不在池内重放。配置文件是唯一事实源，池不接收变更通知；配置界面的连接测试仍走一次性短连接，不进池。
- 交互终端归 Task 所有，允许同一 Task 的后续 Run 复用。Run 完成、失败、取消或历史 Run 删除不关闭终端；Agent 显式关闭、shell 自然退出、Task 删除提交后或后端退出时清理。活会话由 `TerminalSessionService` 管理，不进入 Run 执行进度，不跨后端重启恢复。会话列表与实时状态投影进 snapshot 顶层 `terminal_sessions`；人和 Agent 都可新建会话与输入，用户输入从不被拒，Agent 输入在用户打字时等其停手、前台是用户启动的命令时立即被拒，前端经每会话一条可写 `terminal-v2` WebSocket 交互；终端工具只开放给侧栏对话 Task 的主 Agent。
- 内置浏览器的标签页归宿主所有（`electron/main/browser/BrowserHost`，每个标签一个 `WebContentsView` 加一个 CDP 会话），每个标签绑定一个 Task、只有该 Task 的 Agent 能操作；不持久化，后端重启不影响，宿主退出即全部消失，Task 删除提交后由后端经宿主工具链路通知回收。全部 Task 共用一个持久分区 `persist:cosir-browser`（与应用自身会话隔离、只允许 http(s)、站点权限与下载一律拒绝、JS 弹窗与文件选择框自动拦下）；标签上限的事实源是后端配置中心，随每次请求带给宿主。
- 后端崩溃恢复必须有限且串行，不得无限重启。恢复耗尽后由用户显式重启；生命周期 generation 用于防止旧进程或旧线程覆盖当前状态。
- 后端重启时，遗留 running Run（含 child Run）收敛为 `cancelled`，显式恢复从 prepare_input node 重新进入模型循环；`suspended` Run 没有执行者也没有未闭合调用，原样保留，等可唤醒消息继续同一个 Run。workflow 只有 prepare_input 一个入口，调用方不传恢复模式。inbox 保留，不得隐式重放旧 Agent 执行。进程内 registry、subscriber、后台 Team 任务和运行任务不跨重启恢复（遗留 running TeamRun 由 Coordinator 收敛为 cancelled，pending TeamRun 保留可决定）。重启恢复（收敛、拉起待调度 Task、延迟结果收尾）只由 `RecoverService.recover_after_restart` 按序编排，lifespan 接好运行时后调用这一个入口。

## 进程间接口边界

### 宿主控制面（Electron IPC）

前端与宿主之间的控制面只经 preload 暴露的 `window.cosirHost`，契约（通道名与载荷类型）唯一定义在 `apps/desktop/electron/shared/host-contract.ts`，入参只在 main 的 `electron/main/ipc.ts` 校验；渲染进程只经 `lib/desktop-host/` 取用，业务组件不得直接读 `window.cosirHost`。当前能力：

- `backend`：`snapshot`、`restart`、`onChange`（后端状态推送）
- `log.write`：前端结构化日志落盘
- `dialog.openDirectory`：原生目录选择（目录授权边界）；选文件走 `<input type="file">`，本机路径经 `files.pathOf(file)`（`webUtils.getPathForFile`）取得，渲染进程只能对用户交给页面的 `File` 取路径，路径只作为附件描述交给后端，前端不读工作区文件
- `window.setThemeSource`：同步外观偏好到原生外观（系统对话框、Windows 窗口按钮）
- `browser`：内置浏览器标签页的列表与变化推送（交接状态不在标签上，见 snapshot `handoffs`）、用户在当前 Task 里新开标签（`open`，带配置中心的两个上限；标签记下开启方 `createdBy`，与 Agent 开的标签同归该 Task、Agent 可见可操作）、把某个标签的原生画面摆到浏览器面板的占位区域（同一时刻只挂一个，摆放事实在宿主）、冻结帧（弹层盖住网页区域时先放一帧图再收起画面，观察在 `lib/browser/overlay-watch.ts` 一处）、用户在地址栏导航与关闭；标签列表是宿主事实，渲染进程经 `lib/browser/tabs-store.ts` 只读镜像，不进后端 snapshot
- 窗口外框：macOS 隐藏标题栏保留原生红绿灯，Windows 隐藏标题栏并由系统窗口按钮叠在右上角（`titleBarOverlay`）；拖动区用 CSS 类 `app-drag-region`（`globals.css` 统一把其中的可交互元素排除）。标题栏形态只在 `lib/platform.ts` 判断，页面布局只读 `globals.css` 的左右安全区变量让位。

渲染进程开启 `contextIsolation` 与 `sandbox`，不得开放 Node 能力；页面只能停留在自身来源，站外链接交给系统浏览器，新窗口一律拒绝。新增桌面能力前必须判断其属于宿主控制面还是后端业务接口；属于宿主的，同步更新 `host-contract.ts` 与 `ipc.ts` 的入参校验。React 业务组件不得绕过现有 runtime/logging 边界自行管理进程。

### 宿主工具链路（后端工具 → 宿主方法）

后端工具调用宿主（Electron main）能力的通用通道，不属于任何具体能力：链路只认「方法名 + JSON 参数」，宿主侧方法注册表（`electron/main/host-link/methods.ts`）按名分发，后端工具只经 `app/tools/host/host_call.py` 的 `call_host` 调用（等待、取消、时限与链路错误映射都在这里收口）。宿主是客户端：随 supervisor 快照在后端 ready 时连 WebSocket `/host/link`、generation 变化时换连接；首帧 `hello` 出示宿主拉起后端时注入的 `CODING_AGENT_HOST_TOKEN`，不一致即 4003 拒绝（网页也能连本机 WebSocket，token 是唯一身份依据）。请求只由后端发起；后端同一时刻只保留最新一条连接，断开或被顶替时未决请求以 `UNAVAILABLE` 失败，链路内不重放。渲染进程不在这条链路上，不作为工具执行端；需要用户在界面上作答的工具仍走 HITL 决定命令。当前注册的能力是内置浏览器（`browser.*`，`electron/main/browser/methods.ts`）。

### 本机 HTTP 与 Assistant Transport

- 领域 JSON API 统一通过前端 `lib/http/client.ts` 处理动态基地址、`no-store`、`X-Trace-Id` 和非 2xx 错误。Assistant SSE、业务 resume 和资源请求由各自 transport/resource 边界管理，但仍须遵守动态地址和 trace 规则。
- Assistant UI wire schema 只属于 `apps/backend/app/assistant_transport/`，不得泄漏到 workflow、领域 service 或 storage。
- 命令只对 Task 或消息寻址，不携带 run_id：`POST /tasks/{id}/messages`（发送）、`POST /tasks/{id}/queued-messages/{message_id}/send-now`（排队消息改为立即处理）与 `DELETE .../queued-messages/{message_id}`（撤回排队消息；前端的「编辑排队消息」即撤回后把内容放回输入框），消息已不在队列中时 409 `MESSAGE_CONSUMED`、`/messages/{message_id}/rerun`（编辑重跑：已并入的最新用户输入移出后重跑）、`/resume`（恢复）、`/tool-calls/{call_id}/decision`（对等待用户决定的 HITL 调用作出决定）、`/cancel`（停止）、`/tool-calls/{call_id}/cancel`（取消单个工具调用；登记了延迟结果的调用不要求 Run 在执行）、`/tool-calls/{call_id}/background`（把前台等待转到后台，409 `TOOL_CALL_NOT_DETACHABLE`）。发送的并入时机由后端定、请求不带：Task 有活动 Run（含挂起）时排队（`next_run`，挂起时不唤醒），空闲时立即处理（`immediate`），判定与入队在 Task 闸门内完成；立即发送撤回原行并以 `immediate` 新身份重新入队（同一事务），由 actor 按新消息照常并入、唤醒或开新 Run。用户命令（发送、重跑、恢复、审批决定）等后端处理完这一轮再返回：输入被拒 400、Task 不存在 404、状态冲突（`RUN_NOT_RESUMABLE`、`RUN_NOT_STOPPED`、`EDIT_TARGET_STALE`、`MESSAGE_CONSUMED`、`DECISION_NOT_AWAITED`、`DECISION_ALREADY_SUBMITTED` 等）409，成功 200（响应体 `{}`，不带 snapshot）；编辑重跑只在 Task 静止时接受（不替用户停止，运行中、挂起或执行者尚在收尾时 409 `RUN_NOT_STOPPED`）。只有取消是纯投递（202），是否停下以 snapshot 的 `activity` 为准。命令被拒不进 snapshot。`api` 层只依赖 `TransportAssistantService`（`assistant_transport/service/transport_assistant_service.py`）并做 HTTP 错误映射；该服务承载只属于 HTTP 用户路径的编排（排队消息撤回与立即发送、重跑前的目标校验与输入解析、命令前的 Task 存在性检查、snapshot 读取与订阅），消息经 `task_runtime_spaces.enqueue` 入队、恢复直接调用 Task 的 actor。`/assistant/attach` 是跟随 Task 的长订阅（没有 Run 时也照常订阅，跨 Run 完成、交接、挂起、取消不断开，只在客户端断开、`resync_required` 或 Task 删除时结束）；`/assistant/state` 读取 canonical snapshot。snapshot 是 Task 视角：Task `title`（投影自 `tasks.title`）、扁平 `messages`（每条带 `runId` 与终态时的 `outcome`）、当前 `activity`（最新 Run 的状态、用量与冻结的 `launch`）与排队消息；历史窗口：后端工作副本保存完整历史，完整快照离开后端（`/assistant/state`、attach 首帧与重新发布）时只带最近一段（起点对齐到用户消息，`messageOffset` 为之前没带的条数），更早的按页取（`GET /tasks/{id}/assistant/messages?end=`，位置失效 409 `HISTORY_STALE`）；增量路径里的消息下标始终是完整历史里的位置，投影只在末尾追加或原位替换消息，位置只在完整快照处才变，前端在一处换算并丢弃指向未加载消息的增量（见 `snapshot_window` 与前端 `TransportFrameStore`）；完整快照只在订阅首帧、编辑重跑与背压重新同步时出现，新 Run 与子 Agent 状态回写都走增量；对话界面不按 Run 分组，`runId` 只供诊断与定位。
- 调度：每个 Task 一个 `TaskActor`（`core/task_actor.py`），由 `TaskRuntimeSpace` 持有、随 space 物化拉起、常驻到 Task 删除或进程关闭；`TaskRuntimeSpaceRegistry`（`task_runtime_spaces`）在 lifespan 启动时 `bind_loop`，并为外部队列有待处理消息或当前 Run 挂起的 Task 物化 space；关闭时 `aclose`，删除前 `stop`（停止执行并等它落定；停止即删除的墓碑，之后 loop 不再推进、恢复被拒绝，删除没提交时 `restart_stopped` 换上新 actor）。往外部队列投递一律经 `task_runtime_spaces.enqueue`（先确保 actor 已拉起）。actor 的常驻 loop 只按水位等外部队列的新消息（投递不删行，按「非空」等会空转），醒来后在 Task 闸门内读当前 Run，交给 `core/workflow/message_queue/dispatch_policy` 的纯函数判定：有执行者就把 `immediate` 消息投进 Task 级 mailbox；挂起时新到达的可推进消息唤醒同一个 Run（`suspended → running`）；终态或尚无 Run 时新到达的可推进消息开新 Run（新 Run 只消费一条用户输入——先取最早的 `immediate`，没有再取最早的 `next_run`——配置取这一条的，没有用户输入时继承当前 Run）。「有消息就处理」：用户输入、后台结果、用户决定在取消、失败后到达也会开新 Run；系统通知（`system_notice`）例外，不单独开 Run 或唤醒，随下一次推进写入。Run 作为子 asyncio task 运行，loop 不等它；执行结束后若 Run 落定为完成或挂起且队列还有未消费消息，就再推进一次，取消、失败不自动接续（等新消息）。恢复（`resume`）不经 loop，直接调用 actor，但同样在 Task 闸门内判定与启动，同一 Task 不会出现两个执行者。Run 直接以 running 创建，没有待认领态。`ConversationRunLedger.create_run` 只按调度方选定的发起配置建 Run，不读队列。子 Agent 与 Team 节点只按 Task 寻址，结论只从子 Task 最新 Run 的 run model 读，不记录「哪次调用触发了哪个 Run」：投递输入后等子 Task「无事可做」（`TaskActor.wait_quiescent`：无执行者、最新 Run 已到终态，且 actor 不会再主动推进——没有尚未推进完的可推进消息，最新 Run 完成时队列里也没有可推进消息；挂起不算），再读它的最新 Run。actor 不知道委派的存在：委派（`delegate_task` / 开新 Run 的 `child_agent_send`）的延迟结果作业在投递后于事件循环上等子 Task 无事可做，把结论交给以父调用 `tool_call_id` 登记的作业、经 `background_result` 回投父 Task，父 Agent 用 `wait_messages` 挂起等待；用户在子 Task 里直接发送没有人在等，不回投；Team 节点由 Coordinator 经 `task_runtime_spaces.enqueue` 投递输入，在 `agent_team` 工具线程里分段 `wait_quiescent` 等节点落定，期间记下新开的节点 Run、检出 TeamRun 不再运行即停止节点（Coordinator 是节点的唯一停止者，TeamRun 取消入口只迁移状态）；工具展示数据只带 `child_task_id`，委派卡片状态跟随子 Task 最新 Run。actor 经 `TaskExecution`（`core/task_execution.py`）持有执行登记（每个 Task 至多一个后台执行，并承载「有执行者才打信号、注销执行时取走信号」的不变量）：执行收尾时注销登记并取走 Task 取消信号（信号的唯一清理点，信号带原因 `user` / `parent`，只有用户取消会给下一个 Run 留「用户已取消」通知），再完成交接、挂起后复查唤醒（防丢唤醒）、迟到取消与向子 Task 的取消传播；运行中的取消在入口同步打信号、不排在其他轮之后，并立即取消本 Task 的全部活动 TeamRun（待确认与运行中）；停止期间 loop 不往即将作废的 mailbox 投递，等执行落定后再判定，停止后才到的消息因此开新 Run、停止前排着的不自动接续；挂起中的 Run 没有执行者，取消时由 actor 直接迁移为 `cancelled`。单次 Run 由 `core/workflow/run_driver.py` 的 `RunDriver.drive` 装配 `RuntimeContext`（只含本次 Run / Task 级事实与对象、事件回调和驱动期状态）并驱动 core 的 `ReactLikeWorkflow`，它也是 Run 失败的唯一异常边界；进程级单例（Run 状态写入、外部消息队列、工具注册表与执行器、Agent registry、终端会话服务等）由 workflow 节点与工具 handler 经 `app.service.depends` / `app.config.configuration` 直接取用，不经 `RuntimeContext` 或 `ToolExecutionContext` 转手；Run / Task 级事实与跨层回调（如 `EventSink`）才经它们注入，不经转发门面。同一 task 同时只允许一个 active Run（`running` / `suspended`）和一个执行者。Run 与 Task 当前 Run 指针由同一事务保护；snapshot 在提交后投影。
- HTTP/SSE 断开只表示订阅中断，不自动取消或重放业务 Run。前端可以重新 attach 同一 Task；只有明确的 business resume 才能继续业务执行。
- Task 执行状态（当前 Run 的 `status`，事实源仍是 `ConversationRunModel.status`）经应用级单条 NDJSON 长连接 `GET /task-status/stream` 推送：首帧整表（`full`）、之后只发变化（`changed`，同一 Task 只留最新值）、定期心跳与整表对账；发布点在 `ConversationEventProjector` 收到 Run 创建与状态变化事件处（`service/task_status/TaskStatusFeed`，无订阅者不读库）。前端由 `AppLayout` 挂一条订阅写入 `lib/task-status/` 的 store，侧栏、命令面板、标题徽标与工作台标签只从这里读状态，不轮询、不从 workspace 列表或对话 snapshot 推断；断开时保留上次值并标为未同步，重连以整表帧收敛。

## 数据与事实所有权

- 后端拥有 task、workspace、Run、Agent context、model_config 配置、Agent Team 配置（文件系统持久化）与 Agent Team run（SQLite）等持久化事实；前端状态只负责交互和渲染。
- 主业务库为 `<DATA_DIR>/.cosir/storage/app.sqlite3`；运行日志在 `.cosir/logs/`。业务库与日志职责、路径分离，不得跨层复用 session 或事实模型。
- `ConversationRunModel.status` 是对话 Run 生命周期状态的唯一事实源；Agent Team run 由独立的 `AgentTeamRunModel.status` 承载其生命周期，二者各为自身 Run 类型的唯一事实源。Transport snapshot 与 Agent context 都不得演化成任一 Run 类型的第二套状态机。
- Agent Team 的 `AgentTeamRunModel` 分 `pending` 确认门槛与 `running` 执行两态；同一主 Run 同时只允许一个待确认 TeamRun（pending 唯一约束），确认边界以 `update_status_if_in` 原子迁移。节点运行快照（`AgentTeamRunState.node_runtime`）只冻结 `allowed_tools`（工具名）；Coordinator 创建节点 Task 时从注册表解析对应 schema，并固化到该 Task 的 `tool_schemas`，不要在 TeamRun 快照重复保存工具 schema。
- Agent context 的持久化事实由 `conversation_task_contexts` 承载；`RuntimeMessageContextManager` 是 Task 级 context 的唯一运行时协调入口和进程内 working copy owner，但不是数据库事实源。
- `ConversationTaskStateService` 负责 Transport snapshot 的重建与投影编排；`TaskRuntimeSpace` 按 taskId 持有 snapshot working copy，首次读取时懒加载重建，后续复用内存对象；它只是投影缓存，没有订阅者且 Run 不在运行时（最后一个订阅者离开、Run 状态变化两个时刻判断）卸载，下次读取或事件到达时重建。`ConversationEventProjector` 只负责把 conversation event 投影到 snapshot。`ConversationStateSnapshot` 面向前端 Transport，不是 Agent context 的镜像。
- 模型流式输出由 SDK 聚合，同一次调用持续替换同一条 context 记录，流式期间不纳入模型上下文；响应完整且通过工具准入后才设置 `include_in_context=True`。中断输出保留供 Transport 重建，续跑不加载；不维护另一份流式聚合缓存。inbox 变更只投影排队消息，不以全量重建覆盖正在输出的 Transport 内容。
- context 与 Transport snapshot 允许短暂不一致，以最终一致性收敛。snapshot 普通读取不重复重建；数据库写入后由 projector 或明确的 rebuild 边界更新内存 snapshot。不得为了消除流式时序差异而强行把 Run、context、snapshot 放入一个全局事务。
- Task 标题的事实源是 `tasks.title`：创建时以用户输入兜底；侧栏对话 Task（`user` / `fork`）的每个 Run 首次并入用户输入后（每个 Run 至多一次，记在执行状态 `title_requested`），由标题支线（`core/workflow/branch/task_title.py`）复用本次 Run 的模型并行生成，写库后经 `TaskTitleUpdatedEvent` 投影到 snapshot `title`。支线成败不影响 Run，用量不计入 Run；workflow 关闭模型 client 前收尾它（有取消信号立即取消，否则等其自身时限）。子 Agent 与 Team 节点的标题不改写。
- Run 同时拥有生命周期、冻结执行选项与执行状态（`ReactExecutionState`，workflow 每过一个节点整体存进 `execution_data`；含步骤号、本次驱动的步数上限、节点游标与当前工具批次；新驱动只延续步骤号，重算步数上限、复位游标并清空批次，见 `for_new_drive`），不保存用户正文、图片或附件副本，也不维护独立 checkpoint、不冻结任何工具调用。续跑与唤醒都从模型边界继续；不重放已执行工具。
- Task inbox 是两级消息队列。外部队列（`task_messages` 表，`TaskMessageQueue`）是持久化事实源，只负责入队、按序读取、撤回与确认，按入队顺序保存尚未并入 context 的消息（`TaskMessage`：按 `kind` 判别的 body、`delivery`、`project_to_transport`）；只有用户输入能交接新 Run、携带发起配置（`RunLaunch`），发起配置不进入 context。所有消息进模型时都是 user 角色，来源由 context metadata 的 `kind` 表达，后端规则只读 `kind`、不解析正文；通知合成一条带 `<system_notice>` 标签的 user 消息排在用户输入前。何时并入只看 `delivery`（`immediate` 在当前执行的下一次模型请求前并入，`next_run` 只交给下一个 Run；用户输入据此分两条通道：活动 Run 期间发送的排队为 `next_run`，空闲时发送或被立即发送的为 `immediate`），是否投影只看 `project_to_transport`；判定规则在 `dispatch_policy`（纯函数），`TaskActor` 只执行判定结果。运行期配置锁定：活动 Run 期间前端锁定配置控件、以当前 Run 冻结的 `launch` 发送（主防线，不做配置比较）；后端入队边界不校验配置（`immediate` 输入并入当前 Run，`next_run` 输入交给下一个 Run，新 Run 用它消费的那条输入的配置）。内部队列（`RunMailbox`）随每次执行创建、不持久化，只是投递通道：新 Run 的初始投递取全部非用户输入的消息与一条用户输入（排队消息因此一轮一条依次处理），续跑 / 等待恢复与运行中只投递 `immediate`；Run 内产生的通知（工具准入、续写）与工具产出给模型看的图片（`tool_image`：tool 消息只能是文本，observe 节点在整批 tool 消息之后把带 `images` 的成功观察投进 mailbox）直接投进 mailbox、不落库。每次普通模型调用前，`prepare_input` 取光 mailbox，在同一事务内确认（删除）外部行并写入 context，写入以确认删除那一刻的行内容为准；投递不删行，执行结束、取消或重启丢掉 mailbox 时未确认消息仍在外部队列。一条 Run 可包含多条 user message。最终回答结束当前 Run，旧执行者释放后才为剩余 inbox 新建 Run（收到停止请求时不交接；取消、失败后排队消息留着，等新消息或用户立即发送）。没有活动 Run（含取消、失败后）时发送即推进出新 Run，且先于留下的排队消息；Run 挂起（如等审批）时用户发送只排队、不唤醒，要唤醒由用户对排队消息立即发送（`immediate` 唤醒同一个 Run、消息随之并入）；等待中的 HITL 决定不受影响，仍由决定命令处理。
- 排队投影只镜像外部队列：入队、撤回之后整表刷新（`refresh_queued_messages`），不区分增删；消费投影按 `UserInputConsumedEvent` 自带的展示绑定构造 user 消息、不从排队条目里搬，所以两者之间没有顺序约束。编辑排队消息不是后端命令：前端撤回这一条、把正文与附件放回输入框（排在已有草稿之前），改完照常发送。编辑重跑按「改 context + 接收消息」处理已并入的消息：只在 Task 静止（无执行者、最新 Run 已到终态）时接受，不替用户停止——运行中的 Run 用的是 `begin_run` 时建好的内存上下文，看不到移出，前端也只在终态时开放入口；目标须为最新一条已投影的用户输入（按 metadata `kind` 判断）；撤回它所在的一轮：它及其后 context 不删除，移出模型上下文与前端投影（保留供观测页查看，fork 不复制），但更早调用在这一轮才并入的结果（决定、后台结论、HITL 执行结果，按 metadata `tool_call_id` 归属）保留；这一轮发起的调用仍在进行的延迟工作在提交前撤销；编辑后的消息以新消息身份入队，移出、撤销与入队同一事务提交，随后推进出新 Run。Run 只会前进，不重置旧 Run。外部工具副作用不回滚。
- `task_runtime`、取消 registry、snapshot subscriber 等属于当前后端进程内的协调状态，不是持久化事实。
- 渲染进程 `localStorage` 只保存用户偏好（模型选择、最近 workspace、收藏提示词等）；各类偏好由对应 storage module 管理，不得存储任务或对话事实。

## 前端架构

- React 负责页面、用户交互、HTTP/SSE 客户端和渲染状态；Electron main 进程负责窗口与本地进程控制。前端不得直接访问 SQLite、Agent Runtime 或工作区文件来绕过后端业务边界。
- 后端 snapshot 是对话、Run、usage 和 tool parts 的权威读取来源。Assistant UI runtime state 只用于当前渲染和传输控制，不得原样写回后端作为事实。
- `AppLayout`（react-router 布局路由，路由表见 `src/router.tsx`）是 workspace/task 导航的稳定容器；`TaskPage`、初始 snapshot 和 Assistant runtime 按 `taskId` 建立，并由布局直接渲染：切到配置或上下文观测时最近的任务页保活，只有进入其他任务或新对话才替换。普通 workspace 数据刷新不应无故卸载正在执行的 Assistant runtime。
- 会话侧栏（`components/task-rail/`）属于对话区：只在对话页显示当前 Task 的状态摘要卡片（后台任务等，卡片注册表按 id 装配），宽度足够时固定为对话右侧一列、不够时浮在对话区右上角；卡片顺序、折叠与开关是全局 localStorage 偏好。右侧停靠区同一时刻只显示一个面板（选择在 `lib/layout/dock-store.ts`，只是界面状态、不持久化，宽度按面板记在布局偏好），三者各自独立、不共用标签条：文件面板（`components/files/`，当前工作区的文件浏览器，浏览状态按工作区在 `lib/workspace-files/`）、浏览器面板（`components/browser/`，默认跟随当前对话 Task 的标签，可铺满对话区；面板显示哪个 Task 在 `lib/browser/panel-store.ts`）、工作台（界面在 `components/workbench/`，状态在 `lib/workbench/`，按工作区存放子 Agent 与配置草稿标签）。会话侧栏与停靠面板不互相承载。
- transport resume/attach 只重新订阅 Task；business resume 才继续后端执行。前端实现和命名必须保持两种语义可辨认。
- 业务命令统一经 `lib/assistant/task-commands.ts` 按 Task / 消息寻址；后端处理完这一轮才返回，命令被拒以 HTTP 409 同步抛出（发送类命令收回乐观消息、恢复草稿），全部输出由 Task 长订阅推送。「一轮输出结束」按 `activity` 从活动态（`running` / `suspended`）落到终态判定（`onActivitySettled`），不按 Run 身份比对；fork 入口挂在带 `outcome` 的消息上，请求按 `messageId` 切分。运行中发送一律由后端排队，前端不选投递方式；排队消息不进消息流，在输入框上方的队列区（`components/assistant-ui/elements/queued-messages.tsx`）按 id 渲染为线程消息，提供立即发送、编辑（撤回后放回输入框）与删除；活动 Run 期间模型与工具控件只读，发送使用当前 Run 冻结的 `launch`（`lib/assistant/run-launch.ts`）；排队消息的 `delivery` 由后端 snapshot 投影，前端不自行推断。
- 前端测试分层：Vitest 验证模块行为（含 main 进程的 supervisor 规则）；Playwright E2E 用 Vite + 内存测试服务；桌面 E2E（`playwright.desktop.config.ts`）用 Playwright 驱动真实 Electron 宿主与后端，只覆盖进程与窗口行为；都不覆盖后端崩溃恢复，不得视为完整集成测试。

## 工具 UI 渲染边界

工具执行结果的前端渲染是后端 `ToolObservation.display_data` 与前端 renderer 之间的稳定契约，不新增进程或服务；完整字段规范见 `apps/backend/app/core/tool/tool_ui_display_contract.md`，本文件只描述边界。

- 两层契约：静态展示声明 `ToolDisplayHints` 随 `ToolDefinition` 传给客户端，只声明 `verb`/`icon`/`variant`/`surface`/`expandable`/`expand_layout`/`default_open`/`show_result` 等 UI 意图，不含动态结果、渲染函数或业务数据；成功态非空 `ToolObservation.display_data` 必须有稳定 `kind`，由后端展示纯函数投影（外部工具在 `apps/backend/app/tools/display/`，内部工具在 `core/tool/` 各自目录），不执行额外 IO。错误态按受控短提示规则，不带成功态 `kind`。
- 挂起不是展示数据：工具要求 Agent 等待（等审批、等后台结果）时在 `ToolObservation.suspend` 声明，调用当步闭合、结果照常写入，observe 节点只写入本批观察，由 prepare_input（请求模型前的唯一关口，依次判定取消、挂起、步数上限）从执行状态里的工具批次读出挂起请求、迁移 Run 为 `suspended` 并结束驱动，读完即清空批次——这是挂起的唯一决策点；挂起前已有外部可唤醒消息时直接继续，不挂起。HITL：需要用户决定的工具声明 `ToolDefinition.user_decision`（批准载荷形状由 `decision_model` 声明），首次调用只登记待决事项并返回等待占位（`tool_awaiting_decision`）；决定入口是通用的，按 `tool_call_id` 寻址、以等待占位行的 `pending` 状态判定是否仍在等待，同步校验队列里没有重复决定、载荷符合 `decision_model` 后入队 `user_decision` 消息，不读工具自己的业务事实；等待中的决定在 Run 停止、失败、完成或重启后仍可决定，到达后唤醒或开新 Run；`execute` 类工具的决定（批准与驳回）由 prepare_input 先确认消息再绕过模型重放原调用，执行结果追加为 `<tool_result>`、占位只更新卡片状态；`reply` 类工具的决定作为消息交给模型。交接（`terminal_handoff` / `browser_handoff`）= `execute` 类 HITL + 把一个资源暂时交给用户：交接事实统一在进程内交接登记表 `core/tool/handoff/handoff_registry`（以 `tool_call_id` 为键，同一资源同一时刻至多一条，资源一侧不另记），首次调用登记并返回等待占位，用户交还即作出决定、重放时取回登记并报告交还时的资源状态（资源已不在时如实说明、按成功收尾）；全部结束路径都经登记表——交还（`end`）、资源在交接中意外结束时代用户驳回（`settle`，如 shell 退出）、编辑重跑撤回这一轮（`drop`）、Task 删除（`drop_task`），Run 停止不结束交接；不持久化、不跨重启（卡片上的交还入口仍在）。登记表投影为 snapshot 顶层 `handoffs`，前端 `lib/handoff/` 据 `surface` 与资源 id 在对应面板显示交接提示、交接出现时弹出面板，「交还」统一经 `useHandBack`；浏览器标签在交接中被关掉时宿主无法通知后端，用户在面板里关闭交接中的标签即交还。新增一种可交接的资源：登记表加一个 `surface`、工具调用 `begin` / `end`、前端 `revealHandoff` 加一个分支并在该面板放提示条。决定入队统一经 `service/conversation_run/tool_decision_service.py`，HTTP 决定入口与代为落定共用。Agent Team 审批是 `execute` 类：`agent_team` 首次调用建 pending TeamRun（记下 `tool_call_id`）；工具按决定执行时批准即确认并在工具线程里阻塞运行 Team、落定后把结论作为工具结果返回，驳回即落定；thread 工具执行中途不检查取消信号，所以主 Task 停止时由 actor 在发起停止那一刻取消该 Task 的全部活动 TeamRun（待确认与运行中，按 Task 而非 Run 寻址：批准可能由提出方案之后新开的 Run 执行；不等 Run 落定，否则互等）——这是「等待中的决定在 Run 停止后仍可决定」的例外，停止后要继续须让主 Agent 重新提出方案；单独停止 Team 就是取消这次 `agent_team` 调用（通用 `/tool-calls/{call_id}/cancel`，没有 TeamRun 专属取消入口），由 Coordinator 在等待节点期间现读工具级取消信号、落定 TeamRun 并停下节点；审批门由后端强制，模型无法自行推进 TeamRun。前端审批卡按 `team_run_id` 读取 TeamRun 状态（`pending` 即待确认），不依附工具调用生命周期，也不在 `display_data` 里放审批声明。
- 状态唯一来源：`ToolObservation.status` 表达工具执行结果，取值 `success`/`error`/`cancelled`；Transport 侧工具 part 生命周期为 `pending`/`running`/`completed`/`failed`/`cancelled`，其中 `completed`/`failed`/`cancelled` 由 observation 经 `success→completed`、`error→failed`、`cancelled→cancelled` 投影，`pending`/`running` 为执行前/执行中状态。前端不得建立第二套状态机，也不得从 `args`/`result` 反推展示结果。
- 前端路由：`components/assistant-ui/tools/tool-part.tsx` 通过 `tool-renderer-registry.ts` 按 `data.kind` 与 `presentation.expand_layout` 选择 renderer；通用布局为 `details`/`list`/`diff`/`write`/`terminal`/`none`，专用 renderer 可承载明确的业务交互。禁止按工具名编写专用渲染分支；未知 `kind` 走 `ToolFallback`，不导致消息流崩溃。
- 错误三通道隔离：工具 UI 短提示走受控 `display_data.status_hint`（约 5 字，来自后端分类映射）；Run 错误 `error.message` 可直接使用模型 HTTP 响应体的 `message` 字段供用户排查，但不得透传完整响应体。生命周期走 Transport status。前端不展示堆栈、原始异常、原始 prompt、凭据或大段模型正文；失败 `display_data` 不得携带成功态的目标、结果或输出字段。
- 当前已注册工具 `kind`（布局见契约）：`read_file`→`read-file-meta`、`read_image`→`image-read`（一次多张，逐张校验，部分失败不整批失败；走通用 `details` 行，展开后方形缩略图并排）、`search_content`→`content-search-results`、`find_files`→`file-list`、`list_directory`→`directory-list`、`write_file`/`patch_write`/`apply_patch`/`delete_file`/`move_file`→`file-changes`，`execute_terminal`→`terminal-result`、`terminal_*`→`terminal-session`、`web_search`→`web-search-results`、`web_extract`→`web-extract-urls`、`delegate_task`→`delegation-result`、`child_agent_send` / `child_agent_status`→`child-agent-result`、`wait_messages` 无展示 `kind`（走通用卡片）、`propose_agent_configuration`→`agent-configuration-draft`、`browser_navigate` / `browser_snapshot` / `browser_act` / `browser_screenshot` / `browser_handoff`→`browser-page`（截图带附件 asset id，交接带要用户做的事；`browser_tabs` 无展示 `kind`，走通用卡片）。Agent Team 的 `agent_team`→`agent-team-approval`（批准后的执行卡片按 part 的 `team_run_id` 读取 TeamRun，不另发展示 `kind`）与 `propose_agent_team_configuration`→`agent-team-configuration-draft` 已注册，是否开放由 Agent 工具选择与 Run 授权决定。
- 工具产出图片：图片按与用户上传相同的规则存进工作区附件目录（与上传共用发布内核 `AttachmentService._publish`：内存里的图片走 `save_image`，本机图片文件走 `import_image`；图片规则只此一份，工具不自行校验格式或尺寸），观察经 `tool_success(images=...)` 携带相对路径；声明 `ToolDefinition.returns_images` 的工具在本次 Run 的模型不支持图片时由准入门禁统一拒绝，各工具不另做判断；请求时由 `vision_input` 解析，与用户上传图片同一条路。
- 文件变更职责：`apply_patch` 只修改已有文件内容；新建、删除和移动分别由 `write_file`、`delete_file`、`move_file` 负责。
- 展示数据不是后端事实源：`display_data` 只服务 UI 渲染与重连恢复；`artifact_data` 只承载内部工具产物，不得进入 Assistant Transport；`ToolObservation.content` 不被当作通用 UI 展示数据来源。

## 后端架构

- `api` 负责 HTTP schema、依赖注入和错误映射；`service` 负责领域规则与用例编排；`storage/CRUD` 负责数据库访问。API 和 Runtime 不应绕过依赖装配与 service 直接组织业务 CRUD。
- CRUD 不承载领域状态迁移或事件语义。无外部 Session 时可以完成单次存储事务；传入外部 Session 时加入调用方事务且不自行提交。跨表、状态迁移及提交后发布由 command/use-case/service 负责。
- Run 状态事件只能在数据库条件更新成功后发布，重复状态迁移不得重复发布。workflow 节点只产生普通 conversation event，经统一 workflow 消费边界交给 projector；不得直接发布 Run 状态事实。
- `task_runtime` 是进程内并发协调层：task operation 串行化同一 task 的调度轮次（Run 创建、唤醒、恢复、编辑重跑，均由该 Task 的 actor 发起）；workspace operation 仲裁 fork、删除和关闭等结构性冲突。其锁和 registry 不跨进程、不跨重启。
- 删除 Task = 停止 + 删除记录（`TaskService.delete_task_tree`，workspace 同理）：先在任何锁之外停下所有权子树（沿 `parent_task_id` 的委派子 Agent 与 Team 节点）的 actor，再只持 workspace 闸门在一个事务里级联删除；fork 的来源在 `extra.fork`、不在子树里，默认不随源删除、只改挂到被删根的来源；用户选择「一起删除」（`DELETE /tasks/{id}?withForks=true`）时全部 fork 后代作为额外的根一并停止和删除，删除的就是停止阶段收集到的那批，响应返回实际删除的 Task id。Task 级资源（终端、延迟结果）在删除提交后回收，不依赖停止时有无执行者。
- `RunDriver` 与 workflow 在后端进程内运行。工具按 `execution_mode` 在线程内或独立进程执行；独立工具进程负责队列通信、取消、超时和进程树清理，但不拥有业务事实，也不构成新服务层。
- Hook 是运行期扩展边界：registry 负责注册索引，interceptor 负责触发、匹配、拒绝短路与失败安全；异常默认记录放行，不阻断主 Run。
- 模型工具统一导出 strict function schema，复用 OpenAI SDK 从参数模型生成，再做必要的平台收窄并固化到 Task；模型请求不临时重写 schema。工具执行入口仍负责本地业务校验。
- provider/model 配置是数据库事实，由 model config service（`ModelConfigService`）管理；运行时模型构建只消费解析后的配置。能力目录不等于连接可用性，provider 连接测试也不等于 Agent Run。
- 诊断使用本地结构化日志与上下文观测页，不接入外部 tracing；日志失败不得改变 Run 结果，上下文仍由既有持久化事实管理。
- RuntimeMessageContextManager 是 Task 上下文的运行时协调入口与 working copy owner，负责系统提示词、内存消息列表（每次 `begin_run` 从库重建，改库用例无需通知或卸载它）、消息序列、SDK 消息持久化与工具协议闭合；持久化事实归 context 表。inbox 消息由 `TaskActor` 投递进执行的 mailbox，`prepare_input` 节点经 `add_message(session=...)` 把队列确认与 context 写入放在同一事务，提交后经 `EventSink` 发 `UserInputConsumedEvent` 投影；节点不持 Task 闸门。Run service 负责生命周期与恢复决策。
    - ContextEntry 作为上下文消息单位，承载稳定消息身份、Run 归属与排序；正文采用 OpenAI SDK 请求消息格式，附属信息按消息角色使用单一强类型 metadata 保存；用户展示绑定、模型响应信息和工具展示事实不进入模型请求。
- context 和 snapshot不需要事务保持强一致性，只需要在读取的时候，保持最终一致性即可

## 代码目录边界

- `apps/desktop/electron/`：桌面宿主——`main/`（窗口、托盘、退出流程、宿主 IPC、桌面日志，`backend/` 为后端进程生命周期，`host-link/` 为宿主工具链路客户端与方法注册表，`browser/` 为内置浏览器）、`preload/`（`window.cosirHost`）、`shared/host-contract.ts`（宿主控制面契约）、`resources/`（图标）。
- `apps/desktop/`：`src/`（React 入口）、`app/`（根组件与全局样式）、`components/`（页面与展示）、`hooks/`、`lib/`（api/http/assistant/logging 等客户端与契约）。
- `apps/backend/app/api/`：HTTP 路由、schema 与错误映射。
- `apps/backend/app/assistant_transport/`：Assistant wire 协议、SSE、用户命令编排（`TransportAssistantService`）、snapshot 与事件投影。
- `apps/backend/app/service/`：conversation_run/attachment/model_config/configuration/agent_team 用例编排；`service/agent_team/` 另含 TeamRun 读模型与审批决定入队；Team 配置注册表与配置来源在 `service/configuration/`，Team 协调器、审批门与节点执行画像在 `core/tool/handler/agent_team/`。
- `apps/backend/app/models/`：ORM 记录与枚举（`ConversationRunModel`、`*_record` 等），以及持久化的数据契约；`models/agent_team/` 是 Team 配置（含图校验）与 TeamRun 运行状态，只承载数据与纯规则，不触碰进程内状态。
- `apps/backend/app/storage/`：SQLite engine、schema、CRUD 与事务；`task_runtime/`：进程内 task/workspace 并发协调。
- `apps/backend/app/core/`：agents/context/llm_provider/workflow/tool/hook/events，覆盖 Agent 定义、上下文、模型供应、workflow（含 inbox、取消信号与模型支线）、工具、运行期扩展与执行事件。`core/workflow/message_queue/` 承载 inbox 消息语义（`TaskMessage` 与 `message_bodies/`）、外部队列 `TaskMessageQueue` 与调度规则 `dispatch_policy`（纯函数）；投递通道 `RunMailbox` 与执行状态、`RuntimeContext` 在 `core/workflow/execution_state/`；调度 actor 在 `core/task_actor.py`，其执行登记在 `core/task_execution.py`、取消的下游副作用在 `core/task_cancellation.py`。`core/workflow/branch/` 承载与主线并行、随本次驱动收尾的模型支线（Task 标题）。`core/tool/handoff/` 承载交接登记表（见「工具 UI 渲染边界」的交接说明）。`core/tool/background/` 承载延迟工具结果（设计见 `docs/plan/deferred-tool-result-design.md`）：工具把耗时工作包装成作业（`DeferredJob`，通用实现 `ThreadJob` / `ManualJob`）交给统一执行入口 `run_deferred`（登记、前台 / 后台、转到后台、到上限自动转后台与回投；声明 `ToolDefinition.deferred` 的工具在参数模型里带 `background` 字段），以 `tool_call_id` 为键的进程内登记表 `deferred_registry`（委派子 Agent 与 `execute_terminal` 的后台命令经它回投 `background_result`，`wait_messages` 只据它判定可等待的工作，Run 停止时撤销该 Task 全部条目）与重启收尾 `restart_settlement`（按 `parent_task_id` 向等待的父 Agent 回投被重启收敛的子 Run、告知旧终端失效、把残留的后台占位推到「已中断」）。看板条目投影进 snapshot 顶层 `background_tasks`（进程内事实，只含后台工作，服务看板），可转到后台的前台等待另投影为 `foreground_waits`（只服务工具卡片的「转到后台」，前台等待不进看板）；工具卡片状态的事实源是后台占位行 metadata 的 `deferred` / `status`，沿用 HITL 占位「只更新展示状态」的做法，结论并入时推到 `completed`（作业带卡片终态时一并写入展示数据，如后台命令的完整输出）、撤销时推到 `cancelled`。`core/tool/` 承载工具契约（schemas、结果构造、handler 基类、参数校验）、执行管线（`execute/`：准入、隔离执行、输出预算）、延迟结果（`background/`，见上）、注册表与 Agent 内部工具（`handler/` 下每个工具一个入口文件、同族参数模型与辅助代码在同名子包：子 Agent、Agent Team、web、wait_messages、mcp、skill；MCP 服务随 Agent 在后端本机运行）。`core` 不依赖 `assistant_transport`：执行事件只定义事实，经 `EventSink` 回调交给消费方；workflow 与工具的其余协作者直接使用具体类型（见「抽象按需引入」）；Transport snapshot 的投影在 `assistant_transport/projection/`。
- `apps/backend/app/tools/`：外部工具实现（文件、搜索、终端）及其参数模型、展示投影、交互终端运行时（`terminal_runtime/`：会话服务、Terminal Worker 客户端、交互契约）、本机 shell 定位（`shell/`：`execute_terminal` 与交互终端共用的唯一事实源；各自开放哪些 shell、`auto` 指什么、怎样启动仍归各工具；平台判定统一用 `utils/host_platform.is_windows`）、宿主工具链路（`host/`：连接持有者 `HostLink` 与工具侧入口 `call_host`）、MCP 客户端与长连接池（`mcp/`；服务配置契约在 `service/configuration/mcp_configuration.py`，因为 core 构建提示词目录也要读它）与工具装配（`tool_registration.build_tool_registry`，在 lifespan 经 `set_tool_registry` 注入；运行期经 `get_tool_registry` / `get_tool_executor` 取用）。依赖方向只允许 `app/tools → core/tool`，`app.core` 不得导入 `app.tools`（`tests/test_core_import_boundary.py` 守卫）。
- `apps/backend/app/config/`：进程配置；`utils/`：路径等通用工具；`lifespan.py`/`app.py`/`bootstate.py`：应用装配与启动。

## docstring 约定【必须中文】【**主线任务**执行用户指令，**支线任务**发现docString漂移，并向用户说明，不擅自主动改】

注释与 docstring 是本仓的「记忆」：优先记录**不明显**的信息——设计决策（为什么这样写、为什么不能那样写）、上下游链路契约（谁保证什么输入、谁消费什么输出、边界与事实源在哪）、复杂逻辑的取舍，以及非显而易见的约束与副作用。

- **记不明显的，不记明显的。** 简单代码逻辑（一眼看懂的赋值、循环、返回）与函数名已自解释的简单职责，不必为「凑完整」而写；复杂逻辑（为何分这几步、算法或方案取舍）可以且应当说明。
- **决策与链路契约优先于逻辑复述。** 比「做了什么」更重要的是「为什么这么做、与上下游怎么衔接、踩过什么坑」。例如为何必须在某操作前判定集合、某状态事实源在别处、重复发起会造成什么副作用。
- **避免膨胀，保持精炼。** 没有不明显信息时，短句或省略都好过套话；不堆砌字段清单、参数逐一翻译或显而易见的描述。
- 公共模块、类、函数覆盖以下维度中**适用且不显然**的部分：负责什么与不负责什么、输入/输出语义、持久化/进程/线程/网络/全局状态副作用、可能抛出的异常及调用方处理、生命周期的初始化/关闭/取消/重试/恢复、边界转换规则。

如果**注释**或者**docstring**与**代码事实**不一致，则必须修正注释以及docString 与代码事实对齐；若仅复述明显逻辑、无记忆价值，应删减而非保留。

## 待办（TODO）

文件工具状态协调（原 `FileToolStateCoordinator`）已整体删除，以下能力当前缺失，待统一设计后再补；设计时按「工具层是外部工具（类 MCP 服务端）」的边界放在工具一侧，不嵌回通用执行器：

- [ ] **防覆盖未观察内容**：模型读取文件后若文件被外部修改，`write_file` / `patch_write` / `apply_patch` 当前会直接写入，不再拒绝。
- [ ] **同路径写入串行化**：没有路径锁，多个 Task（如父子 Agent）可同时写同一文件；单次写入由 `atomic_write` 保证原子性，但两次写入之间可能相互覆盖。
- [ ] **重复只读调用拦截**：重复的 `read_file` / `search_content` / `find_files` 会照常执行，不再提前返回或阻断。

Electron 桌面宿主目前只在 macOS 上验证过，以下 Windows 行为尚未实测（逐项验证后勾掉；方案与已验证范围见 `docs/plan/electron-host-migration-design.md` 第 12、14 节）：

- [ ] **后端进程树清理**：退出时关闭 stdin 后后端优雅退出并清掉工具子进程、Terminal Worker；宽限期后 `taskkill /T /F` 兜底。
- [ ] **宿主存活契约**：宿主被强杀后，后端经 stdin EOF（`app/parent_watch.py`，Windows 分支用 `raise_signal`）自行优雅退出，不留孤儿进程。
- [ ] **窗口外框**：`titleBarOverlay` 透明底色、窗口按钮颜色随外观偏好切换、右上角安全区让位（`lib/platform.ts` / `globals.css`）。
- [ ] **启动与测试入口**：`scripts/start-desktop.ps1`、`npm run electron:dev`、`npm run test:e2e:desktop` 在 Windows 上可用。

内置浏览器工具（`app/tools/tool_handler/browser/`）以下行为暂不处理，待真实任务出现需求或失败案例后再定：

- [ ] **文件上传**：宿主一律自动拦下文件选择框（见「进程生命周期边界」中内置浏览器的分区约定），Agent 无法替用户上传文件；需要时是否开放、经什么授权边界开放待定。
- [ ] **标签上限写进工具描述**：上限事实源是配置中心、每次调用现读，而工具描述随 Task 固化，写死数字会与配置漂移；目前靠 `TAB_LIMIT` 错误告知模型实际上限，描述只说「只有几个」。
- [ ] **`type` 不触发逐键事件**：输入经 CDP `Input.insertText` 一次写入（`electron/main/browser/page-input.ts`），依赖 keydown / keypress 的自动补全控件可能不响应；尚无实测失败案例，先观察。
- [ ] **iframe 内容**：快照只读主框架的无障碍树，子框架只标出数量（`electron/main/browser/snapshot-format.ts`），Agent 既看不到也操作不了 iframe 里的元素；支付、验证码这类场景已由 `browser_handoff` 交给用户。跨框架需要按框架取树、ref 带框架归属、输入坐标换算，成本高，待出现嵌入式表单 / 编辑器的真实需求再做。

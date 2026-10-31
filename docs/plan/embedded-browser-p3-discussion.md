# P3 内置浏览器：设计讨论稿（活文档）

> 状态：设计已定稿，待实施（实施中发现问题继续在本稿修订）。每轮讨论后直接改正文，已拍板的写进「决定记录」，未定的留在「待决问题」。
> 本稿取代 `embedded-browser-design.md`（初稿）；本稿定稿后删除初稿。

## 1. 目标与非目标

**目标**：Agent（主 Agent、子 Agent、Team 节点）能操作内置浏览器（打开页面、读页面、截图看画面、点击、输入），用户在工作台里实时看到同一个页面，必要时接手（登录、验证码、确认）后交还。

**v1 不做**：

- 任意 JS 执行工具、下载、文件上传、站点权限（摄像头、定位等）
- 用户独立浏览（不归属 Task 的标签页）
- 跨应用重启恢复标签页

## 2. 与初稿相比的关键变化

| 初稿 | 本稿 | 理由（第零铁律） |
| --- | --- | --- |
| 新开本机管道（Unix socket / Windows 命名管道）+ `COSIR_HOST_PIPE` + 自定义 JSON-RPC 服务端 | **宿主反向连接后端的 WebSocket** `/host/link` | 后端已有 FastAPI WebSocket（终端 `terminal-v2`），宿主本来就知道后端地址并订阅其 generation；不新增 IPC 机制，不新增未在 Windows 验证的管道 |
| 12 个工具 | **6 个工具**（动作合并进 `browser_act`） | 每多一个工具就多一份 schema、展示、测试；动作是同一能力的参数差异 |
| 标签页列表投影进后端 snapshot | **标签页是宿主事实**，前端经 `window.cosirHost.browser` 直接读 | 标签页活在宿主进程，后端只是使用方；绕一圈投影等于造第二份事实 |
| 浏览器只给主 Agent | **所有 Agent 可用**，只有交接工具按名字从子 Agent / Team 节点剥离 | 复用既有 `CHILD_DISALLOWED_TOOLS` / `TEAM_NODE_DISALLOWED_TOOLS` 名单，不需要新的分组机制 |
| 截图只给前端看 | **截图进模型** | 存储复用上传附件，投递复用 Run 内 mailbox 通道，解析复用 `vision_input` |

## 3. 现状调研结论（与设计相关的代码事实）

- **工具体系**：`ToolDefinition`（`execution_mode` thread/process、`parallel_mode`、`user_decision`、`timeout_seconds`、`display`），注册集中在 `app/tools/tool_registration.py`；分组常量在 `core/tool/tool_grouping.py`。
- **子 Agent / Team 节点的工具剥离**：按名字的 `Constant.Tools.CHILD_DISALLOWED_TOOLS`、`TEAM_NODE_DISALLOWED_TOOLS`，外加按组剥离 `TOOL_GROUP_TERMINAL_SESSION`（`child_agent_create.strip_child_disallowed_tools`、`agent_team_preparation_service.materialize_node_tools`）。
- **子 Task 在前端只读**：工作台 `agent-run-surface.tsx` 用 `ReadonlyThread`，用户在子 Task 里不能作 HITL 决定——这就是交接工具不能给子 Agent 的原因（§4.6）。
- **HITL 交接现成**：`terminal_handoff` 是 `execute` 类决定，首次调用 `tool_awaiting_decision` 挂起，用户交还即决定，`prepare_input` 重放。
- **工具结果以 user 消息追加的先例**：`ToolResultBody`（HITL 迟到结果）在 Run 内直接写 context，渲染为带 `tool_call_id` 的 `<tool_result>` user 消息，metadata `kind="tool_result"`。
- **视觉输入**：`core/context/vision_input.py` 在请求副本里把**任意 user 消息**中的 `cosir-attachment://` 图片块解析为 data URL；tool 消息只能是文本（OpenAI Chat Completions 协议限制）。模型能力由 `model_config.supports_image` 声明，运行期在 `ResolvedModel.supports_image`。
- **附件存储**：`AttachmentService.upload` 规范化（EXIF、最长边）后以 SHA-256 幂等存到工作区 `.cosir/Attachment`，不入库、不绑定 Task。
- **可配置项**：配置中心白名单 `config/environment_field_catalog.py`（支持 integer + minimum），值由 `Settings` 加载，写入后热更新（`EnvironmentConfigurationService`）。
- **线程工具调用事件循环**：`asyncio.run_coroutine_threadsafe(..., loop)`（`child_run_job.py`、`coordinator.py`、MCP 连接池）。
- **Task 删除回收**：`TaskService.finalize_deleted_task_spaces` 在提交后回收终端会话与延迟结果。
- **工作台**：`lib/workbench/types.ts` 的 tab `kind` 联合，新增 `browser` 是同构扩展。
- **输出预算**：工具结果 content 上限 `MAX_OUTPUT_CHARS = 20_000`，快照必须自己先裁剪。

## 4. 架构

```text
渲染进程（React）
  工作台 browser 标签 ──占位矩形 bounds──▶ window.cosirHost.browser.*（IPC）
                                                │
Electron main                                   ▼
  browser/  BrowserHost ── 每 Task 一组 WebContentsView（全局持久分区）
     │   ├─ page-snapshot（CDP Accessibility → 带 ref 的文本快照）
     │   ├─ page-input（CDP Input.dispatch*）
     │   ├─ capturePage（截图）
     │   └─ host-link（WebSocket 客户端，随 supervisor ready/generation 连接与重连）
     │                         │  ws://127.0.0.1:<port>/host/link?token=…
     ▼                         ▼
Python 后端  api/host_link_api.py ── app/tools/host/（HostLink 单连接持有者 + call_host 工具调用助手）
                                         ▲ run_coroutine_threadsafe
                                  browser_* 工具（thread 模式）
```

### 4.1 事实所有权

- **标签页、页面、登录态**：宿主进程事实（`BrowserHost`），不持久化（分区 cookie 除外，那是 Chromium 自己的）。
- **标签页归属**：每个标签页绑定且只绑定一个 `task_id`；Agent 只能看到、操作本 Task 的标签页。父子 Agent 各用各的标签页，互不可见。
- **后端**：不保存标签页状态，只在工具调用时经 link 询问宿主。`display_data` 里的 url / title 是调用当时的展示快照，不是事实源。
- **后端重启**：宿主与标签页不受影响，新后端连上后照常使用。
- **宿主退出**：一切随之消失。

### 4.2 宿主工具链路（`/host/link`，通用，不属于浏览器）

host-link 是「后端工具调用宿主能力」的通用通道，浏览器只是第一批方法。以后要让 Agent 用宿主侧的其它能力（系统通知、剪贴板、原生对话框、录屏等），只需在宿主注册方法、在后端写工具，链路本身不改。

**分层**（链路只认「方法名 + JSON 参数」，不认识任何业务）：

| 层 | 位置 | 职责 | 不负责 |
| --- | --- | --- | --- |
| 链路 | 后端 `app/tools/host/host_link.py` + `api/host_link_api.py`；宿主 `electron/main/host-link/` | 连接、token、请求/应答配对、超时、`$/cancel`、断线与顶替 | 任何业务语义 |
| 方法注册表 | 宿主 `electron/main/host-link/methods.ts` | 方法名 → handler（入参校验、可取消的 `AbortSignal`） | 具体能力实现 |
| 能力 | 宿主 `electron/main/browser/` 等 | 实现并注册自己的方法（`browser.*`） | 链路细节 |
| 工具调用助手 | 后端 `app/tools/host/host_call.py` | 工具线程里 `call_host(method, params, execution_context)`：桥到事件循环、超时取工具的 `timeout_seconds`、轮询 `should_stop_call` 取消、把链路错误映射为受控 `ToolObservation` | 工具自己的参数模型与展示 |
| 工具 | 后端 `app/tools/browser/` 等 | 参数模型、调用 `call_host`、结果与 `display_data` 投影、能力错误码的提示 | 链路与取消 |

注册表的引入依据「抽象按需引入」：P3 落地时就有多个真实实现（`host.ping` 与一组 `browser.*`），不是预留扩展点。不做能力协商或版本号（绿地项目，宿主与后端同包发布）。

**方向**：宿主是客户端。supervisor 进入 ready 后连接；generation 变化时断开重连；后端同一时刻只保留最新一条连接（新连接顶掉旧连接，旧连接上的未决请求以 `UNAVAILABLE` 失败）。

**认证**：宿主拉起后端时注入随机 `CODING_AGENT_HOST_TOKEN`，连接时校验。理由：网页也能对 `127.0.0.1` 发起 WebSocket（CORS 不管 WS），动态端口可被扫描。

**协议**：JSON 帧 `{id, method, params}` / `{id, result}` / `{id, error:{code, message}}`；只由后端发起请求（宿主→后端的推送目前没有真实需求，不做）；取消发 `{method:"$/cancel", params:{id}}`，宿主 abort 对应 handler 的 `AbortSignal`。二进制（截图）以 base64 放在 result 里。

**错误码分两层**：
- 链路层（链路统一产生，`call_host` 统一映射）：`UNAVAILABLE`（宿主未连接：浏览器开发模式、E2E 内存服务、宿主尚未连上）、`TIMEOUT`、`CANCELLED`、`METHOD_NOT_FOUND`、`INVALID_PARAMS`、`INTERNAL`。
- 能力层（由各方法定义，工具自己解释）：浏览器为 `TAB_NOT_FOUND`、`REF_STALE`、`NAVIGATION_BLOCKED`、`NAVIGATION_FAILED`、`PAGE_CRASHED`、`TAB_LIMIT`、`USER_ACTIVE`。

**日志**：两端各记一条结构化日志（`host_call_started` / `host_call_finished`，带 `method`、`id`、耗时、错误码；`task_id`、`tool_call_id` 由 `call_host` 放进 `params` 的元数据字段，宿主日志可按 Task 追查）。参数与结果正文不落日志（截图太大）。

**和「前端工具」的关系**：用户希望的「调用前端 tool 的链路」就是这一层——「前端」指宿主（Electron main），它拥有原生能力并且始终在线。渲染进程（React 页面）不作为工具执行端（理由见下）；某个宿主方法如果确实需要页面配合（例如在工作台打开某个标签），由宿主经既有 IPC 通知渲染进程，渲染进程不在等待链路上。

**考虑过但不采用：assistant-ui 前端工具（`add-tool-result` 命令）**

assistant-ui 支持「前端工具」：模型发起工具调用，由浏览器端执行，结果经 `add-tool-result` 命令发回后端（`useAssistantTransportRuntime` 的命令通道；本项目用的是 `useExternalStoreRuntime` + 自有 `task-commands.ts`，对应物是 HTTP 命令）。套到这里就是「后端发调用 → 渲染进程经 `window.cosirHost` 让 main 执行 → 渲染进程 POST 结果」。不采用，理由：

- **依赖渲染进程挂着且订阅了该 Task**：子 Agent、Team 节点在后台跑，没有页面在订阅它们，没人执行；要兜住就得在 `AppLayout` 挂一个订阅所有 Task 的全局执行器——那就是绕道渲染进程的 host-link，多一跳，还多了页面刷新、渲染进程崩溃时执行丢失的问题。
- **每个动作都是一次挂起与唤醒**：结果以命令回来，相当于每次点击都走一遍 HITL（等待占位 → 入队 → 唤醒 → 重放），延迟高，context 里每步多一条占位与一条迟到结果，Agent 体验与 token 都变差。
- **违背进程边界**：渲染进程只负责页面与交互，不应进入 Agent 工具执行链路；浏览器本来就归 main 所有，渲染进程中转不增加任何能力。
- **项目已有它的合理用法**：「需要人在界面上作答」的工具就是这个模式，对应现有的 HITL 决定命令（`/tool-calls/{call_id}/decision`），`browser_handoff` 正是走这条路。机器执行的浏览器动作走 host-link，需要人参与的交接走决定命令，各用其所。

### 4.3 宿主 BrowserHost（`electron/main/browser/`）

| 文件 | 职责 |
| --- | --- |
| `methods.ts` | 向 host-link 方法注册表注册 `browser.*` 方法（入参校验在这里） |
| `browser-host.ts` | 标签页表（`tabId → {taskId, view}`）、按请求携带的上限校验、创建/关闭/按 Task 批量关闭、向渲染进程推送标签变化 |
| `page-snapshot.ts` | CDP `Accessibility.getFullAXTree` → 裁剪后的带 ref 文本快照；ref 即 `backendDOMNodeId`，导航后整体失效 |
| `page-input.ts` | ref → `DOM.getBoxModel` → `Input.dispatchMouseEvent` / `insertText` / `dispatchKeyEvent`；滚动到可见再点 |
| `view-placement.ts` | 按渲染进程上报的占位矩形摆放 / 隐藏 `WebContentsView` |

**截图在不可见时也要能拍**：标签页没在工作台显示时视图不挂在窗口上。宿主让所有标签页始终以固定视口尺寸（如 1280×800）离屏渲染（`webContents.setBackgroundThrottling(false)` + 显式设置视口），`capturePage` 不依赖用户是否正在看；工作台显示时再按占位矩形摆放（尺寸变化即视口变化，快照与截图以当时视口为准）。

安全（每个 `WebContentsView` 统一设置，复用 `window.ts` 的收紧思路）：
- 全局持久分区 `persist:cosir-browser`，与应用自身会话隔离；`sandbox`、`contextIsolation`、无 preload。
- 只允许 `http:` / `https:` 导航；页面要开新窗口时在同 Task 内开新标签（受上限约束）。
- 下载取消、权限请求一律拒绝、文件选择框拒绝（v1）。
- CDP 只在宿主内部使用固定方法集合，不向后端暴露通用 CDP 透传。

### 4.4 后端工具（`app/tools/browser/`，分组 `TOOL_GROUP_BROWSER`）

| 工具 | 作用 | 返回 |
| --- | --- | --- |
| `browser_navigate` | 打开 URL（无标签时新建）、或 `back` / `forward` / `reload` | 页面快照 |
| `browser_snapshot` | 重新读取当前页面结构 | 页面快照 |
| `browser_screenshot` | 截取当前视口（可选整页） | 文本说明 + 图片（§4.5） |
| `browser_act` | 一组动作按序执行：`click` / `type` / `press` / `select` / `scroll` / `wait_for`（参数是 discriminated union） | 动作结果 + 新快照 |
| `browser_tabs` | `list` / `new` / `select` / `close` | 标签列表 |
| `browser_handoff` | 交给用户操作，HITL `execute` 类，照搬 `terminal_handoff`；**仅主 Agent** | 交还后的页面快照 |

约定：
- 都是 `execution_mode=thread`、`parallel_mode=serial`。
- 写操作类动作返回新快照，省掉一轮 `browser_snapshot`。
- 快照由宿主裁剪到预算内（可交互元素 + 可见文本，超长时给截断提示与续读方式）。
- **标签上限可配置**：配置中心新增分组「浏览器」与两个字段，由 `Settings` 加载、改后热更新：
  - `BROWSER_MAX_TABS_PER_TASK`（integer，minimum 1，默认 5）
  - `BROWSER_MAX_TABS`（integer，minimum 1，默认 20；全进程上限——每个标签一个渲染进程，约 100MB+，父 Agent 开多个子 Agent 时标签数会成倍增长）

  后端每次请求新建标签时把两个当前值随请求带给宿主，宿主据此校验并返回 `TAB_LIMIT`（错误信息区分是本 Task 满了还是全局满了，模型可先关自己的旧标签）——上限的事实源只在后端配置一处，宿主不另存配置。调小上限不关闭已有标签，只拒绝新建。用户在工作台手动新开标签同样受这两个上限约束（经后端读取当前值）。
- 展示：`display_data.kind = "browser-page"`（url、title、tabId、动作摘要、截图的附件 id），卡片点击在工作台打开对应标签；截图在卡片里显示缩略图。

### 4.5 截图进模型：复用上传附件 + user 消息投递通道

问题：OpenAI Chat Completions 的 tool 消息只能是文本，图片只能放在 user 消息里。

做法：截图走和用户上传图片**同一套存储**、和 Run 内通知**同一条投递通道**，不另开写 context 的旁路。

```text
browser_screenshot ──png──▶ AttachmentService.save_image ──▶ 工作区 .cosir/Attachment/<sha256>.png
        │                       （与 upload 共用规范化 + 幂等发布内核）
        ▼
ToolObservation(content="截图见下一条消息", images=("cosir-attachment://…",))
        │
observation 节点写完整批 tool 消息 ──▶ rc.mailbox.post(TaskMessage.of(ToolImageBody(...)))
        │
prepare_input 下一次模型请求前取光 mailbox ──▶ 写入 context（user 角色，kind="tool_image"）
        │
vision_input 在请求副本里把 cosir-attachment:// 解析成 data URL（与用户上传图片同一条路）
```

1. **存图复用上传附件**：`AttachmentService` 抽出 `upload` 里「规范化（EXIF、最长边）+ SHA-256 幂等发布」的内核，`upload` 读完流后调用它，新增的 `save_image(workspace_id, data, content_type)` 也调用它——只有一套发布逻辑。截图与上传图片放在同一个工作区附件目录，历史截图就是普通附件文件，不另设保留策略。
2. **观察带图**：`ToolObservation` 新增通用字段 `images: tuple[str, ...]`（`cosir-attachment://` 定位符）；tool 消息仍是文本。
3. **投递复用 mailbox**：observation 节点写完整批 tool 消息后，把带图观察转成 `ToolImageBody` 投进 `rc.mailbox`——与工具准入通知、续写提示同一条「Run 内消息只进 mailbox、不落外部队列」的通道。`prepare_input` 是请求模型前的唯一关口，天然保证图片消息排在整批 tool 消息之后、下一次模型请求之前，不会打断 assistant tool_calls 与 tool 消息的连续性。
4. **`ToolImageBody`**（`message_bodies/` 新增，继承 `EnqueuedMessageBody`）：`KIND="tool_image"`、`DEFAULT_PROJECTED=False`（截图在工具卡片里展示，不在消息流里冒出一条「用户消息」）、优先级排在通知与用户输入之前（图片属于刚结束的工具批次）、带 `tool_call_id`。正文是一行 `<tool_image tool_call_id=… tool_name=…>` 标签 + 图片块；「文字 + 附件图片块」的拼装从 `UserInputBody.render` 抽成共用函数，两者共用。
5. **不进外部队列的后果**：Run 在工具结束与下一次 `prepare_input` 之间被取消时，mailbox 随执行丢弃，这张图不会进 context——tool 消息文本仍在、文件仍在附件目录，模型下次需要可重新截图。与 Run 内通知「丢了下个 Run 重新产生」同一取舍。
6. **模型不支持图片**：由工具准入按 `ToolDefinition.returns_images` 统一判定（见下方复用边界），本次 Run 模型 `supports_image` 为 false 时返回受控错误「当前模型不支持图片输入」；`browser_screenshot` 的工具描述引导此时改用 `browser_snapshot`。
7. **请求时解析不改**：`vision_input` 已处理任意 user 消息中的附件图片。

**复用边界（工具图片通道不属于浏览器）**：后续会新增 `read_image`（读工作区图片给模型看），它和 `browser_screenshot` 只有「图片从哪来」不同。所以除取图本身外，链路上每一段都是工具无关的通用能力，放在 `core`，浏览器只是第一个使用方：

| 环节 | 归属 | 新工具要做的事 |
| --- | --- | --- |
| 存图 | `AttachmentService.save_image`（规范化 + 幂等发布，与上传共用） | 拿到字节后调用一次 |
| 结果构造 | `core/tool/results/` 新增 `tool_success_with_images(content, images, display_data=…)` | 用它代替 `tool_success` |
| 能力声明与准入 | `ToolDefinition.returns_images: bool`；工具准入（`core/tool/execute/`）统一判定：本次 Run 模型 `supports_image` 为 false 时直接返回受控错误，不进 handler | 定义里声明 `returns_images=True` |
| 投递 | observation 节点：观察带 `images` 即转 `ToolImageBody` 投进 mailbox | 无 |
| 并入与解析 | `prepare_input` + `vision_input` | 无 |
| 展示 | 卡片按 `display_data` 里的附件 id 显示缩略图（前端共用一个图片缩略组件） | 在 `display_data` 带附件 id |

因此「模型不支持图片」的判定从 `browser_screenshot` 里挪到准入一处（上面第 6 点按此实现），`read_image` 只需「读文件 → `save_image` → `tool_success_with_images`」三步。`read_image` 本身不在 P3 范围，但 P3-3 的单测用一个测试替身工具验证通道，不依赖浏览器。

历史截图全部保留在 context 里（作为附件定位符，请求时解析），不做「只留最近 N 张」的裁剪：前缀不变、prompt 缓存稳定；token 增长交给将来的上下文压缩统一处理。

### 4.6 子 Agent 与 Team 节点

- 浏览器工具组对所有 Agent 开放，是否给某个 Agent 由其 Profile 的工具选择决定。
- **`browser_handoff` 按名字加入 `CHILD_DISALLOWED_TOOLS` 与 `TEAM_NODE_DISALLOWED_TOOLS`**：子 Task 在前端只读，用户无法交还，挂起就会永远等下去。子 Agent 遇到登录墙时如实汇报给父 Agent，由父 Agent 决定是否自己打开页面再交接。
- 用户仍可在工作台打开子 Task 的浏览器标签，**直接在页面上操作**（例如登录）——这不经 HITL，子 Agent 下次读快照自然看到新状态（共享全局分区，登录态对所有 Task 生效）。

### 4.7 前端

- `window.cosirHost.browser`（`host-contract.ts` 新增，入参在 `ipc.ts` 校验）：`listTabs(taskId)`、`onTabsChanged`、`setPlacement(tabId, rect | null)`、`closeTab`、`navigate`（用户手动地址栏）。
- 工作台新增 tab `kind: "browser"`（按 `tabId`），界面是地址栏 + 一个占位 div；`ResizeObserver` 上报矩形，标签不可见时上报 `null`。
- 会话侧栏可加一张「浏览器」卡片列出本 Task 标签页（卡片注册表已有，按需）。
- 交接卡片：`browser_handoff` 的等待占位渲染「交还给 Agent」按钮，走既有决定命令。

## 5. 需要同步的改动

- **Task 删除回收**：`finalize_deleted_task_spaces` 增加一次 `host_link.notify("browser.dropTasks", task_ids)`；宿主未连接时记日志跳过。
- **宿主注入 token**：`backend/process.ts` 注入 `CODING_AGENT_HOST_TOKEN`；缺失时 `/host/link` 拒绝一切连接。
- **工具剥离名单**：`browser_handoff` 加入两份禁用名单（§4.6）。
- **AGENTS.md**：拓扑图加 host-link，进程间接口边界新增「宿主工具链路」一节（通用通道、方向、认证、分层），宿主控制面加 `browser`、生命周期加「浏览器标签归宿主、不随后端重启消失」、工具 kind 列表加 `browser-page`、inbox 段加「工具产出图片经 mailbox 以 `tool_image` user 消息并入」。

## 6. 难点与取舍

### 6.1 原生视图遮挡 DOM 弹层

`WebContentsView` 是一块独立的原生画面，**永远画在整个 React 页面之上**，不参与 CSS 层叠（`z-index` 对它无效）。所以凡是会盖到浏览器区域的 React 浮层——下拉菜单、命令面板、对话框、Toast、右键菜单——都会被网页挡在下面，看起来像「弹层打开了却看不见」。

候选：
- **A. 弹层打开时隐藏视图**：全局监听浮层挂载 → `setPlacement(null)`，关闭后恢复。最简单，代价是弹层期间网页区域变空白。
- **B. 弹层打开时换成静态截图**：先 `capturePage` 拿一帧，以 `<img>` 放进占位 div，再隐藏原生视图；浮层正常盖在图片上，关闭后恢复。观感无感知，每次多一次截图（几十毫秒）。
- 两者都只改前端占位组件与一个 IPC。

**已定 B（冻结帧），只在浮层与浏览器区域相交时触发。**

- **第零铁律**：A 与 B 的结构完全相同——都是「浏览器占位组件一处观察浮层 → 调一个宿主 IPC」，差别只在 IPC 是 `hide` 还是 `freeze`（截一帧返回给前端再隐藏），多出来的只是一个 `<img>`。改动量相当，就不该为省这点代码选体验更差的方案；先做 A 再升级 B 反而要改两次、测两次。
- **Agent 体验**：两者对 Agent 无差别。视图隐藏或冻结只影响用户看到的画面，页面本身继续渲染（§4.3 离屏渲染），快照、截图、动作照常工作。
- **用户体验**：A 会让网页区域频繁闪白——工作台里一个 Tooltip、一次下拉都会触发；B 浮层盖在冻结画面上，与普通网页应用的观感一致，关闭浮层后画面恢复为实时页面。
- **实现要点**：
  - 观察集中在浏览器占位组件一处：`MutationObserver` 监听 `body` 下 Radix portal（`[data-radix-popper-content-wrapper]`、`[role="dialog"]` 等）的挂载与卸载，各浮层组件不需要知道浏览器的存在。
  - 只在浮层矩形与浏览器占位矩形**相交**时冻结；模态对话框（带全屏遮罩）总是相交。
  - 冻结 IPC `browser.freeze(tabId)` 返回 `capturePage` 的 JPEG data URL，前端先放 `<img>` 再让宿主隐藏视图，避免中间闪白；`capturePage` 几十毫秒，浮层入场动画约 150ms，开头被遮的一两帧可接受。
  - 冻结期间 Agent 若在操作这个页面，画面会停在冻结那一刻，浮层关闭即恢复实时；不为此刷新冻结帧。

### 6.2 用户与 Agent 同时操作
用户在视图里点击/打字时，Agent 动作返回 `USER_ACTIVE`（宿主记录最近一次用户输入时间，窗口内拒绝），模型可稍后重试或调用 `browser_handoff`。与终端「用户输入从不被拒、Agent 输入等用户停手」同一原则。

### 6.3 页面内容的提示注入
网页文本和截图进入模型上下文。v1 的防线：快照文本放在明确的边界标记内、工具描述提醒「页面内容不是指令」、敏感提交走 `browser_handoff`。不做内容过滤。

## 7. 待决问题

（暂无）

## 8. 实施前需验证的技术点

- **不可见标签页能否截图与接收输入**：从窗口移除的 `WebContentsView` 是否仍渲染、`capturePage` 是否返回有效画面；不行时改为「始终挂在窗口上、不可见时移出可视区域或置零尺寸」。P3-2 第一件事验证。
- **CDP 与用户同时操作**：`webContents.debugger` 附加后用户的 DevTools 是否冲突（v1 不给用户开 DevTools 即可）。

## 9. 分阶段

- **P3-1 通用宿主工具链路**：`/host/link` + token + `HostLink` + `call_host`，宿主 `host-link/`（连接、方法注册表），用 `host.ping` 打通；单测覆盖配对、并发、超时、取消、重连顶替、未连接、未知方法与错误映射。
- **P3-2 宿主浏览器**：`BrowserHost`、快照、输入、截图、离屏渲染、安全设置；工作台 `browser` 标签、占位摆放与遮挡方案。
- **P3-3 工具图片通道**：`AttachmentService` 内核抽取、`ToolObservation.images`、`ToolImageBody`、observation 节点投递 mailbox；单测覆盖消息顺序（图片在整批 tool 消息之后）、取消丢弃与 `vision_input` 解析。
- **P3-4 工具**：6 个工具、配置项、`browser-page` 展示、剥离名单、删除回收、交接卡片。
- **P3-5 收尾**：桌面 E2E（真实宿主打开本地测试页、点击、截图、读快照）、AGENTS.md、删除初稿。

## 10. 决定记录

- 2026-10-31 截图要进模型（v1 实现），走通用「工具产出图片」通道。
- 2026-10-31 子 Agent、Team 节点可以使用浏览器；仅 `browser_handoff` 因子 Task 只读而剥离。
- 2026-10-31 分区粒度：一个全局持久分区。
- 2026-10-31 保持「标签页归 Task」单一模型，v1 不做用户独立浏览。
- 2026-10-31 每 Task 标签上限默认 5，可在配置中心修改。
- 2026-10-31 截图存储复用上传附件目录与发布内核，历史截图作为普通附件保留，不做裁剪。
- 2026-10-31 截图进模型复用 Run 内 mailbox 投递通道（`ToolImageBody`），不在 observation 节点另写 context。
- 2026-10-31 增加全局标签上限，默认 20，可在配置中心修改。
- 2026-10-31 弹层遮挡采用 B 冻结帧，只在浮层与浏览器区域相交时触发。
- 2026-10-31 Task 空闲时标签页保留，直到 Task 删除或用户关闭（与终端归 Task 一致）。
- 2026-10-31 `USER_ACTIVE` 窗口 2 秒：用户最近一次输入后 2 秒内 Agent 动作被拒。
- 2026-10-31 离屏视口固定 1280×800；截图最长边 1568，由 `AttachmentService` 规范化内核统一处理。
- 2026-10-31 工具图片通道做成工具无关的通用能力（存图、结果构造、`returns_images` 准入、mailbox 投递），后续 `read_image` 直接复用。
- 2026-10-31 host-link 做成通用的宿主工具链路（方法注册表 + `call_host`），不为浏览器专门化；渲染进程不作为工具执行端。

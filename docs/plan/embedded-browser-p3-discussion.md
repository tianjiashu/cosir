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
     │   ├─ CDP Page.captureScreenshot（截图与冻结帧）
     │   └─ host-link（WebSocket 客户端，随 supervisor ready/generation 连接与重连）
     │                         │  ws://127.0.0.1:<port>/host/link（首帧 hello 携带 token）
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

注册表的引入依据「抽象按需引入」：P3-2 落地时就有一组 `browser.*` 真实方法，不是预留扩展点；不设只为测试存在的 `host.ping`，链路的往返由两端单测覆盖，真实宿主下的往返随 P3-2 第一个浏览器方法验证。不做能力协商或版本号（绿地项目，宿主与后端同包发布）。

**方向**：宿主是客户端。supervisor 进入 ready 后连接；generation 变化时断开重连；后端同一时刻只保留最新一条连接（新连接顶掉旧连接，旧连接上的未决请求以 `UNAVAILABLE` 失败）。

**认证**：宿主拉起后端时注入随机 `CODING_AGENT_HOST_TOKEN`，连接后的首帧 `hello` 携带 token 校验（与终端 `terminal-v2` 首帧 `attach` 同一做法；不放 URL 查询串，避免进日志）。理由：网页也能对 `127.0.0.1` 发起 WebSocket（CORS 不管 WS），动态端口可被扫描。

**协议**：JSON 帧 `{id, method, params, meta}` / `{id, result}` / `{id, error:{code, message}}`；只由后端发起请求（宿主→后端的推送目前没有真实需求，不做）；取消发 `{method:"$/cancel", params:{id}}`，宿主 abort 对应 handler 的 `AbortSignal`。二进制（截图）以 base64 放在 result 里。

**错误码分两层**：
- 链路层（链路统一产生，`call_host` 统一映射）：`UNAVAILABLE`（宿主未连接：浏览器开发模式、E2E 内存服务、宿主尚未连上）、`TIMEOUT`、`CANCELLED`、`METHOD_NOT_FOUND`、`INVALID_PARAMS`、`INTERNAL`。
- 能力层（由各方法定义，工具自己解释）：浏览器为 `TAB_NOT_FOUND`、`REF_STALE`、`NAVIGATION_BLOCKED`、`NAVIGATION_FAILED`、`PAGE_CRASHED`、`TAB_LIMIT`、`USER_ACTIVE`。

**日志**：后端每次调用记一条 `host_call_finished`（`method`、`request_id`、耗时、错误码，以及请求帧 `meta` 里的 `task_id` / `tool_call_id`）；宿主只记失败（`host_method_failed` / `host_method_not_found`），按同一 `meta` 关联。参数与结果正文不落日志（截图太大）。

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

**不可见时照常工作**（已实测，见 §8）：标签页没在工作台显示时视图从窗口上摘下（`removeChildView`），保持 `setBounds(1280×800)` 与 `setBackgroundThrottling(false)`。摘下的视图 `capturePage` 不可用，但 CDP 的截图、无障碍树、点击与输入全部正常，所以**截图与冻结帧一律用 CDP `Page.captureScreenshot`**，不用 `capturePage`。工作台显示时再挂回窗口、按占位矩形摆放（尺寸变化即视口变化，快照与截图以当时视口为准）。

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
- **B. 弹层打开时换成静态截图**：先截一帧，以 `<img>` 放进占位 div，再隐藏原生视图；浮层正常盖在图片上，关闭后恢复。观感无感知，每次多一次截图（几十毫秒）。
- 两者都只改前端占位组件与一个 IPC。

**已定 B（冻结帧），只在浮层与浏览器区域相交时触发。**

- **第零铁律**：A 与 B 的结构完全相同——都是「浏览器占位组件一处观察浮层 → 调一个宿主 IPC」，差别只在 IPC 是 `hide` 还是 `freeze`（截一帧返回给前端再隐藏），多出来的只是一个 `<img>`。改动量相当，就不该为省这点代码选体验更差的方案；先做 A 再升级 B 反而要改两次、测两次。
- **Agent 体验**：两者对 Agent 无差别。视图隐藏或冻结只影响用户看到的画面，页面本身继续渲染（§4.3 离屏渲染），快照、截图、动作照常工作。
- **用户体验**：A 会让网页区域频繁闪白——工作台里一个 Tooltip、一次下拉都会触发；B 浮层盖在冻结画面上，与普通网页应用的观感一致，关闭浮层后画面恢复为实时页面。
- **实现要点**：
  - 观察集中在浏览器占位组件一处：`MutationObserver` 监听 `body` 直接子节点（Base UI 与 Radix 的 portal 都挂在这里）与 Sonner 的 Toaster 区域，不按某个组件库的选择器判断；各浮层组件不需要知道浏览器的存在（见 §13）。
  - 只在浮层矩形与浏览器占位矩形**相交**时冻结；模态对话框（带全屏遮罩）总是相交。
  - 冻结 IPC `browser.freeze(tabId)` 返回 CDP `Page.captureScreenshot` 的 JPEG data URL，前端先放 `<img>` 再让宿主隐藏视图，避免中间闪白；截一帧几十毫秒，浮层入场动画约 150ms，开头被遮的一两帧可接受。
  - 冻结期间 Agent 若在操作这个页面，画面会停在冻结那一刻，浮层关闭即恢复实时；不为此刷新冻结帧。

### 6.2 用户与 Agent 同时操作
用户在视图里点击/打字时，Agent 动作返回 `USER_ACTIVE`（宿主记录最近一次用户输入时间，窗口内拒绝），模型可稍后重试或调用 `browser_handoff`。与终端「用户输入从不被拒、Agent 输入等用户停手」同一原则。

### 6.3 页面内容的提示注入
网页文本和截图进入模型上下文。v1 的防线：快照文本放在明确的边界标记内、工具描述提醒「页面内容不是指令」、敏感提交走 `browser_handoff`。不做内容过滤。

## 7. 待决问题

（暂无）

## 8. 技术点实测结果

2026-10-31 在 Electron 44.7.2（macOS）上用探针脚本实测，结论已并入 §4.3 与 §12：

| 场景 | `capturePage` | CDP 截图 | CDP 点击 / 输入 | 视口 |
| --- | --- | --- | --- | --- |
| 视图摘离窗口 | 不可用（`Current display surface not available`） | 可用 | 可用 | 1280×800，`visibilityState=visible` |
| 挂在窗口上但 `setVisible(false)` | 不可用 | 可用 | 可用 | 1280×800 |
| 挂在窗口上、移到屏幕外 | 空图 | 超时 | 点击无效 | 0×0（不可用） |
| 挂在主页面下层 | 可用 | 可用 | 可用 | 1280×800；窗口隐藏到托盘后仍可用 |

其余实测：
- **用户与 Agent 输入可区分**：`before-input-event` 只对真实输入（含 `sendInputEvent`）触发，CDP `Input.dispatch*` 不触发——`USER_ACTIVE` 据此实现。
- **CDP 必须在首次加载后才可用**：未加载任何页面的 webContents 上 `Page.enable` 会一直挂起；新建标签先 `loadURL("about:blank")` 再附加调试器。
- **JS 弹窗**：`Page.javascriptDialogOpening` 可拦截并 `Page.handleJavaScriptDialog` 关闭，页面不会卡住。
- **文件选择框**：`Page.setInterceptFileChooserDialog` 后不弹原生框，只收到 `Page.fileChooserOpened` 事件。
- **新窗口**：`target=_blank` 链接走 `setWindowOpenHandler`；无用户激活的 `window.open` 被 Chromium 拦截，不触发。
- **导航后 ref 失效**：旧文档的 `backendNodeId` 报 `No node found for given backend id`；跨进程导航后新文档的 id 会从小重新编号，不能用 id 本身判断新旧。
- **导航失败**：`loadURL` 以 `ERR_*` 码拒绝（如 `ERR_UNSAFE_PORT`）；DNS 慢时可能很久不返回，需自己限时并 `stop()`。
- **渲染进程崩溃**：`render-process-gone` 触发、`isCrashed()` 为真；再次 `loadURL` 即恢复，调试器保持附加、可继续用。
- **UA**：默认 UA 含 `Electron/44.7.2`，按分区 `setUserAgent` 去掉后页面读到的是标准 Chrome UA。
- CDP 与用户 DevTools 的冲突：v1 不给用户开 DevTools，不需要处理。

## 9. 分阶段

- **P3-1 通用宿主工具链路**：落点与验收见 §11。
- **P3-2a 浏览器引擎 + 最薄工具**：宿主 `browser/` 引擎与 `browser.*` 方法、配置项、后端 `browser_navigate` / `browser_snapshot` 两个工具、删除回收；Agent 能打开页面并读到内容。落点见 §12。
- **P3-2b 工作台显示**：宿主控制面 `browser` IPC、工作台 `browser` 标签与占位摆放、会话侧栏「浏览器」卡片、冻结帧。落点见 §13。
- **P3-3 工具图片通道**：`AttachmentService` 内核抽取、`ToolObservation.images`、`ToolImageBody`、observation 节点投递 mailbox；单测覆盖消息顺序（图片在整批 tool 消息之后）、取消丢弃与 `vision_input` 解析。
- **P3-4 工具**：补齐 `browser_screenshot`（接 P3-3 通道）、`browser_act`、`browser_tabs`、`browser_handoff`，`browser-page` 展示 renderer，子 Agent / Team 节点开放与剥离名单，交接卡片。
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
- 2026-10-31 离屏视口固定 1280×800；截图最长边 1568，由 `AttachmentService` 规范化内核统一处理。（P3-3 修订：附件策略最长边是 8192，截图分辨率改由截图时的 CDP `clip.scale` 控制为 1280×800，见 §14。）
- 2026-10-31 工具图片通道做成工具无关的通用能力（存图、结果构造、`returns_images` 准入、mailbox 投递），后续 `read_image` 直接复用。
- 2026-10-31 host-link 做成通用的宿主工具链路（方法注册表 + `call_host`），不为浏览器专门化；渲染进程不作为工具执行端。
- 2026-10-31 P3-1 不设测试专用的 `host.ping`；token 走首帧 `hello`；MCP 的线程等待逻辑抽成共用 `wait_for_call`。
- 2026-10-31 实测后改定：截图与冻结帧用 CDP `Page.captureScreenshot`（摘离窗口的视图 `capturePage` 不可用）；不可见标签摘离窗口而不是移到屏幕外。
- 2026-10-31 文件与浏览器改为独立的右侧停靠面板，与工作台三选一显示；浏览器面板的 `+` 让用户在当前 Task 里新开标签（行为类似可交互终端），修订上面「v1 不做用户独立浏览」：仍无独立浏览，用户开的标签同样归 Task。
- 2026-10-31 P3-2 拆成 P3-2a（引擎 + `browser_navigate` / `browser_snapshot` 最薄工具，端到端可用）与 P3-2b（工作台显示）。

## 11. P3-1 实施清单：通用宿主工具链路

> 状态：已实现（未提交）。实施中的两处调整：被顶替的旧连接由 `retire()` 投回它自己的事件循环收尾（pending 表与 WebSocket 都属于各自的循环，不跨循环直接改）；`HostLinkClient` 构造时接收宿主身份与方法注册表，不直接读 Electron，便于单测。

**范围**：建好「后端工具 → 宿主方法」的通用通道，P3-1 结束时没有任何业务方法，也没有新工具；前端（渲染进程）零改动。

**路线（第零铁律）**：结构化改造 + 复用。链路放进既有的「后端 WebSocket 端点 + `service/depends` 进程级单例 + 工具线程桥到事件循环」三种既有模式里，不引入新依赖（Electron 44 内置 Node 24，main 进程有全局 `WebSocket`）。顺带做一处减法：MCP 工具里「工具线程等待 future、轮询取消与时限」的 `_wait` 抽成共用函数，host-link 复用，不写第二份。

### 11.1 后端

| 文件 | 动作 | 内容 |
| --- | --- | --- |
| `app/core/tool/execute/thread_wait.py` | 新建 | `wait_for_call(future, execution_context, timeout_seconds) -> T`：工具线程里等 `concurrent.futures.Future`，每 0.1s 轮询 `should_stop_call`，命中取消抛 `ToolCallStopped`、超时抛 `TimeoutError`，两种都先 `future.cancel()`。从 `mcp_tool._wait` 原样提炼 |
| `app/tools/tool_handler/mcp_tool.py` | 改 | 删除 `_wait`、`_CallStopped`、`_POLL_SECONDS`，改用 `wait_for_call` / `ToolCallStopped`；行为不变 |
| `app/tools/host/__init__.py` | 新建 | 包说明：宿主工具链路，不认识任何业务方法 |
| `app/tools/host/errors.py` | 新建 | `HostCallError(code, message)`；链路层错误码常量 `UNAVAILABLE` / `TIMEOUT` / `CANCELLED` / `METHOD_NOT_FOUND` / `INVALID_PARAMS` / `INTERNAL`；能力层错误码由各能力自己定义，同样以 `HostCallError` 上浮 |
| `app/tools/host/host_link.py` | 新建 | `HostLink`：持有当前连接、其事件循环与 pending 表（`id → asyncio.Future`）。`serve(websocket, hello)` 由端点调用：顶替旧连接（旧连接以 4001 关闭、其未决请求以 `UNAVAILABLE` 失败）、循环读应答帧、断开时让未决请求以 `UNAVAILABLE` 失败。`async call(method, params)` 发请求等应答；被取消时补发 `$/cancel` 再上抛。`submit(method, params) -> Future`：线程侧入口，未连接时立即 `UNAVAILABLE`，否则 `run_coroutine_threadsafe` 投到连接所在循环。`notify(method, params)`：发后即忘（P3-4 的 `browser.dropTasks` 用），未连接时记日志返回。token 在构造时读 `CODING_AGENT_HOST_TOKEN`，`verify(token)` 用 `secrets.compare_digest` |
| `app/tools/host/host_call.py` | 新建 | 工具侧唯一入口 `call_host(method, params, *, execution_context, timeout_seconds) -> dict`：`get_host_link().submit` + `wait_for_call`；`params` 外层附 `meta: {task_id, tool_call_id}` 供宿主日志关联。`host_error_observation(tool_name, error)`：把链路层错误转成受控 `tool_error` / `tool_cancelled`（如 `UNAVAILABLE` →「桌面宿主不可用」）；能力层错误码由工具自己解释 |
| `app/api/host_link_api.py` | 新建 | `@app.websocket("/host/link")`：accept → 读首帧 `hello`（`{type, token, host: {platform, pid, version}}`）→ 校验失败以 4003 关闭并记 `host_link_rejected`，成功交 `HostLink.serve`；帧格式错误以 1008 关闭。结构对齐 `terminal_stream_api.py` |
| `app/app.py` | 改 | `importlib.import_module("app.api.host_link_api")` |
| `app/service/depends.py` | 改 | `get_host_link()`（`lru_cache` 单例，与终端服务、MCP 连接池同一处装配）；`reset_service_dependencies` 里 `cache_clear`。不加关闭钩子：uvicorn 关闭时取消 WS 连接，`serve` 的 finally 已让未决请求失败 |
| `app/__main__.py` | 改 | 模块 docstring 的环境变量清单补 `CODING_AGENT_HOST_TOKEN` |

**日志**（后端 `backend-*.log`）：`host_link_connected`（宿主 platform / pid / version）、`host_link_rejected`、`host_link_replaced`、`host_link_disconnected`、`host_call_finished`（method、id、耗时、错误码，带 `task_id` / `tool_call_id`；不记参数与结果正文）。

### 11.2 宿主（`apps/desktop/electron/main/`）

| 文件 | 动作 | 内容 |
| --- | --- | --- |
| `host-link/methods.ts` | 新建 | 方法注册表：`registerHostMethod(name, handler)`、`HostMethodHandler = (params, { signal, meta }) => Promise<unknown>`、`HostMethodError(code, message)`；重复注册直接抛错。P3-1 无注册项 |
| `host-link/client.ts` | 新建 | `HostLinkClient`：`follow(snapshot)` 订阅 supervisor——`ready` 且 generation 变化时连 `ws://<host>:<port>/host/link`、发 `hello`；非 ready 时断开。收到请求帧 → 查注册表 → 带 `AbortController` 调 handler → 回应答帧；`$/cancel` → abort；未知方法回 `METHOD_NOT_FOUND`，handler 抛非 `HostMethodError` 回 `INTERNAL` 并记日志。意外断开且仍是同一 generation 时退避重连（1s 起、上限 10s）；被 4003 拒绝不重连（token 不一致是缺陷，记 ERROR） |
| `backend/process.ts` | 改 | `BackendLaunch` 增加 `hostToken`，注入 `CODING_AGENT_HOST_TOKEN` |
| `backend/supervisor.ts` | 改 | 构造函数接收 `hostToken`，透传给 `spawnBackend` |
| `app.ts` | 改 | `bootstrap()` 里生成一次 token（`crypto.randomBytes(32).toString("hex")`，宿主进程生命周期内不变），分别交给 `BackendSupervisor` 与 `HostLinkClient`；`supervisor.onChange` 里多调一次 `hostLink.follow(snapshot)` |

日志走既有 `logDesktopEvent`：`host_link_connected` / `host_link_disconnected` / `host_link_rejected` / `host_method_failed`，`meta.task_id` / `tool_call_id` 放进 `data`，与后端日志按调用对上。

### 11.3 测试

| 文件 | 覆盖 |
| --- | --- |
| `apps/backend/tests/test_host_link.py`（新建） | 用 FastAPI `TestClient.websocket_connect` 扮演宿主：token 正确 / 错误 / 缺失；请求与应答配对、并发请求乱序应答；超时；工具取消触发 `$/cancel`；新连接顶替旧连接且旧连接未决请求失败；未连接时立即 `UNAVAILABLE`；`host_error_observation` 的映射 |
| `apps/backend/tests/test_thread_wait.py`（新建） | `wait_for_call` 的完成、取消、超时三个出口；MCP 既有用例保持通过 |
| `apps/desktop/tests/unit/host-link-client.test.ts`（新建） | `vi.stubGlobal("WebSocket", Fake)`：随快照连接 / 断开、generation 过期丢弃、hello 帧、分发与 abort、未知方法、handler 异常、退避重连、4003 不重连 |
| `apps/desktop/tests/unit/backend-supervisor.test.ts`（改） | 断言每次拉起都把 `hostToken` 交给 `spawnBackend`；环境变量注入本身由桌面 E2E 端到端验证（token 不一致就连不上） |
| `apps/desktop/tests/desktop-e2e/desktop-smoke.spec.ts`（改） | 真实宿主与后端就绪后，临时数据根的后端日志里出现 `host_link_connected` |

### 11.4 文档

- `AGENTS.md`「进程间接口边界」新增「宿主工具链路」小节：通用通道、宿主为客户端、首帧 token、只由后端发起、方法注册表与 `call_host` 的分层；「代码目录边界」补 `app/tools/host/` 与 `electron/main/host-link/`。

### 11.5 刻意不做

- 不加只为测试存在的方法（`host.ping`），不加业务方法，不加工具。
- 不做宿主→后端的请求、能力协商与版本号。
- 不改渲染进程与 `host-contract.ts`（链路不经渲染进程）。
- 不顺手修终端 WebSocket 不校验来源的问题（另议）。

## 12. P3-2a 实施清单：浏览器引擎 + 最薄工具

> 状态：已实现（未提交）。实施中与下面清单不同的地方：
> - **方法收敛为三个**：`browser.navigate`（Task 没有标签时自己新开，所以不需要单独的 `browser.open`）、`browser.snapshot`、`browser.dropTasks`；`browser.close` 随 P3-4 的标签管理一起加。
> - **JS 弹窗改用 `webPreferences.disableDialogs`**：实测不关时，页面加载中弹出的 `confirm()` 会卡死整个宿主主进程（超时也触发不了）；关掉后 alert 直接返回、confirm / prompt 视为取消，CDP 仍发出 `Page.javascriptDialogOpening`，只用它给模型记备注，不再调用 `Page.handleJavaScriptDialog`。
> - **崩溃判断直接查 `webContents.isCrashed()`**：`render-process-gone` 是异步到达的，崩溃后到事件之间发出的 CDP 命令会挂到超时。
> - **新增能力层错误码 `PAGE_UNRESPONSIVE`**：CDP 命令 10s 不返回（页面卡死）。
> - **网页脚本跳 `file:` 由 Chromium 自己拦下**（不触发 `will-navigate`），引擎的协议拦截兜住其余非网页协议。
> - **E2E 暂不覆盖 `REF_STALE` 与文件选择框**：两者都要先有动作（点击）才能触发，随 P3-4 的 `browser_act` 补上；ref 映射随导航清空已在代码里。
> - 引擎 E2E 里 Playwright 会把每个 `WebContentsView` 当页面接管并自己去关弹窗，用例里挂一个空的 `dialog` 监听让它不插手（只影响测试）。
> - **没声明编码的页面按 UTF-8 解码**（`webPreferences.defaultEncoding`）：手动验收时发现，Chromium 默认按系统语言猜编码，中文页会成乱码。
> - **Agent 路径手动验收已完成（2026-10-31）**：真实桌面应用 + 配置中心里的 DeepSeek 模型，在临时工作区里让主 Agent 打开本地测试页。模型调用 `browser_navigate` → 链路 → 宿主新开 `t1` → 快照回到模型，模型正确复述暗号并列出两个带 ref 的可点击元素；工具卡片拿到 `browser-page` 展示数据；后端 `host_call_finished`（419ms）与宿主 `browser_tab_opened` 按 `task_id` / `tool_call_id` 对上。强杀宿主后后端经 stdin EOF 自行退出；重启后再次调用正常（标签从 `t1` 重新开始，符合「标签不跨宿主重启」）。删除临时工作区时宿主记录 `browser_tab_closed`，删除回收生效。验收用的工作区与 Task 已删除。

**范围**：宿主里把网页真正跑起来并注册 `browser.*` 方法；后端只加 `browser_navigate` 与 `browser_snapshot` 两个工具，让「Agent 打开页面、读到内容」这条主路径端到端可用。用户看不到画面（P3-2b），截图不进模型（P3-3）。

**路线（第零铁律）**：
- **引擎与 Electron 解耦的部分抽成纯模块**：标签表（归属、上限、当前标签）、快照格式化、入参校验不依赖 Electron，单测直接覆盖；只有「一个标签页 = 一个 `WebContentsView` + 一个 CDP 会话」的胶水依赖 Electron。
- **一个能力只有一个入口对象**：`BrowserHost` 是浏览器的唯一所有者，host-link 方法（P3-2a）与渲染进程 IPC（P3-2b）都只调它，不各自操作 `WebContentsView`。
- **CDP 只在 `browser-tab.ts` 一处使用**，方法集合固定，不向外透传。
- **复用**：入参校验沿用 `ipc.ts` 的「信任边界一处校验」写法；日志用 `logDesktopEvent`；后端工具沿用 `terminal_session/` 的「同族工具一个包 + 一个 `build_*_definitions`」结构与 `call_host` / `host_error_observation`；配置沿用配置中心白名单与 `Settings._env_positive_int`。

### 12.1 宿主（`apps/desktop/electron/main/browser/`，新建目录）

| 文件 | 依赖 Electron | 内容 |
| --- | --- | --- |
| `tab-table.ts` | 否 | 标签表：`tabId`（`t1`、`t2`… 宿主进程内单调递增）→ `{ taskId }`；每个 Task 的当前标签（最近新建或选中的）；`assertCanOpen(taskId, limits)` 按请求带来的两个上限抛 `TAB_LIMIT`（消息区分本 Task 满 / 全局满）；`require(taskId, tabId?)` 校验归属，不属于该 Task 一律 `TAB_NOT_FOUND`（不泄露别的 Task 的标签存在） |
| `snapshot-format.ts` | 否 | CDP 无障碍树（`getFullAXTree` 的节点数组）→ 文本快照：跳过 `ignored`、`InlineTextBox`，无名 `generic` / `none` 折叠，合并相邻 `StaticText`；只给可交互角色（link、button、textbox、checkbox、radio、combobox、listbox、option、menuitem、tab、switch、slider、searchbox）分配 `e1`、`e2`… ref，返回 `{ text, refs: Map<ref, backendNodeId> }`；带 `value`、`checked`、`disabled`、`expanded`、标题级别；超出字符预算（12,000）截断并附「还有 N 行未显示」 |
| `params.ts` | 否 | `browser.*` 方法入参校验（URL 只允许 `http:` / `https:`，`NAVIGATION_BLOCKED`；`task_id` 正整数；`tab` 形如 `t\d+`；上限正整数），不合法抛 `HostMethodError("INVALID_PARAMS")` |
| `browser-tab.ts` | 是 | 一个标签页：创建 `WebContentsView`（分区 `persist:cosir-browser`、`sandbox`、`contextIsolation`、无 preload），`setBounds(1280×800)`、`setBackgroundThrottling(false)`，先 `loadURL("about:blank")` 再 `debugger.attach("1.3")` 并开启 `Page` 域、`Page.setInterceptFileChooserDialog`。提供 `navigate(url \| back/forward/reload, signal)`（自己限时 30s，超时 `stop()` 并 `NAVIGATION_FAILED`，`ERR_*` 码原样带回）、`snapshot()`（取树 → `snapshot-format` → 记下本页 refs）、`resolveRef(ref)`（不在当前 refs 里或 CDP 报节点不存在 → `REF_STALE`）、`close()`。主框架导航提交（`did-navigate`）时清空 refs；`before-input-event` 记录最近一次用户输入时间（CDP 输入不触发，见 §8），供 P3-4 的 `USER_ACTIVE`；`render-process-gone` 标记崩溃（操作返回 `PAGE_CRASHED`，`navigate` / `reload` 恢复）；JS 弹窗由 `Page.javascriptDialogOpening` 自动关闭（`alert` 确认、`confirm` / `prompt` 取消、`beforeunload` 允许离开），记进下一次快照头部；文件选择框只记一条「已阻止」；`setWindowOpenHandler` 一律 `deny`，把目标 URL 交给 `BrowserHost` 在同 Task 内开新标签（超上限就记进快照头部，不开） |
| `browser-session.ts` | 是 | 浏览器分区的一次性设置（宿主就绪时调用一次）：UA 去掉 ` Electron/x`；`setPermissionRequestHandler` / `setPermissionCheckHandler` 全拒；`will-download` 取消并记日志 |
| `browser-host.ts` | 是 | 浏览器能力的唯一所有者：持有 `TabTable` 与 `tabId → BrowserTab`；`open` / `navigate` / `snapshot` / `close` / `dropTasks`；P3-2b 的摆放与冻结也在这里。每个快照结果带页头：`tab t1 · <title> · <url>`，以及本 Task 的其他标签 id |
| `methods.ts` | 否（只依赖 `BrowserHost` 类型） | 把方法注册进 `HostMethodRegistry`：`browser.open`、`browser.navigate`、`browser.snapshot`、`browser.close`、`browser.dropTasks`（通知）；先经 `params.ts` 校验，再调 `BrowserHost`。P3-4 在这里补 `browser.act`、`browser.screenshot`、`browser.tabs` |

`app.ts` 改动：`app.whenReady` 后 `configureBrowserSession()`、`const browser = new BrowserHost(getMainWindow)`、`registerBrowserMethods(hostMethods, browser)`；`will-quit` 时 `browser.closeAll()`（视图随窗口销毁，这里只为写一条日志并断开调试器）。

日志（`desktop-*.log`）：`browser_tab_opened` / `browser_tab_closed`（`task_id`、`tab_id`）、`browser_navigation_failed`（错误码，不记 URL 查询串）、`browser_tab_crashed`、`browser_dialog_dismissed`、`browser_file_chooser_blocked`、`browser_download_blocked`、`browser_popup_opened` / `browser_popup_dropped`。

### 12.2 后端

| 文件 | 动作 | 内容 |
| --- | --- | --- |
| `app/core/tool/schemas/tool_names.py` | 改 | `TOOL_BROWSER_NAVIGATE`、`TOOL_BROWSER_SNAPSHOT`，进 `ALL_TOOL_NAMES` |
| `app/core/tool/tool_grouping.py` | 改 | `TOOL_GROUP_BROWSER = "浏览器工具"` |
| `app/tools/tool_models/browser_args.py` | 新建 | `BrowserNavigateArgs`（`url` 或 `action: back/forward/reload` 二选一、可选 `tab`）、`BrowserSnapshotArgs`（可选 `tab`）；不带 `task_id`——归属取自执行上下文，模型不能指定别的 Task |
| `app/tools/tool_handler/browser/__init__.py` | 新建 | `build_browser_definitions()`，结构同 `terminal_session/__init__.py` |
| `app/tools/tool_handler/browser/common.py` | 新建 | 组装方法参数（注入 `task_id` 与当前两个上限）；把能力层错误码（`TAB_NOT_FOUND`、`TAB_LIMIT`、`NAVIGATION_BLOCKED`、`NAVIGATION_FAILED`、`PAGE_CRASHED`）转成给模型的英文建议与中文 `status_hint`，其余交给 `host_error_observation` |
| `app/tools/tool_handler/browser/navigate.py` / `snapshot.py` | 新建 | 两个 handler：`execution_mode="thread"`、`parallel_mode="serial"`、`resource_keys=("browser",)`；`call_host` 后把快照文本作为 `content`（页面文本放在 `<page_snapshot>` 边界内，并提醒其中内容不是指令），`display_data` 暂只带 `kind: "browser-page"`、`tab_id`、`url`、`title`——前端 renderer 在 P3-4 补，这之前按契约走 `ToolFallback` |
| `app/tools/display/browser_display.py` | 新建 | `build_browser_page_display_data(...)` 纯函数 |
| `app/tools/tool_registration.py` | 改 | 注册 `build_browser_definitions()` |
| `app/core/agents/define_agents.py` | 改 | 主 Agent `allowed_tools` 加入两个工具（子 Agent / Team 节点在 P3-4 随剥离名单一起开放） |
| `app/config/environment_field_catalog.py` | 改 | 新分组 `browser`「浏览器」；字段 `BROWSER_MAX_TABS_PER_TASK`（默认 5）、`BROWSER_MAX_TABS`（默认 20），integer、minimum 1 |
| `app/config/settings.py` | 改 | 两个类级字段，`load()` 里经 `_env_positive_int` 读取；保存配置后 `Settings.load()` 重读，下一次工具调用即生效 |
| `app/task_runtime/service/task_service.py` | 改 | `finalize_deleted_task_spaces` 增加一行 `service_depends.get_host_link().notify("browser.dropTasks", {"task_ids": [...]})` |
| `apps/desktop/components/assistant-ui/tools/tool-icons.tsx` | 改 | `display.icon` 用已有的 `globe`，不新增图标 |

### 12.3 测试

| 文件 | 覆盖 |
| --- | --- |
| `apps/desktop/tests/unit/browser-snapshot-format.test.ts`（新建） | 用录制的 AX 树样本：ref 只给可交互节点、`generic` 折叠、文本合并、状态字段、超预算截断 |
| `apps/desktop/tests/unit/browser-tab-table.test.ts`（新建） | 两个上限与错误消息、跨 Task 访问报 `TAB_NOT_FOUND`、当前标签切换与关闭后的回落、`dropTasks` |
| `apps/desktop/tests/unit/browser-methods.test.ts`（新建） | 入参校验（非 http(s)、坏 tab id、缺 task_id）、方法分发到 `BrowserHost`（鸭子类型替身） |
| `apps/desktop/tests/desktop-e2e/browser-engine.spec.ts`（新建） | 真实 Electron 下的引擎：测试入口 `tests/desktop-e2e/browser-harness/main.ts`（只在测试里用 esbuild 打包，直接 import `BrowserHost`，生产代码不加任何测试钩子）打开本地测试页 → 快照含 ref → 导航后旧 ref 报 `REF_STALE` → JS 弹窗不卡住 → 文件选择框被拦截 → 导航到 `file:` 被拒 → 崩溃后可恢复 |
| `apps/backend/tests/test_browser_tools.py`（新建） | monkeypatch `get_host_link` 为替身：参数组装（注入 `task_id` 与上限）、各能力层错误码的观察、`<page_snapshot>` 边界、`display_data` 形状 |
| `apps/backend/tests/test_task_service*.py`（改） | 删除提交后发 `browser.dropTasks` 通知 |

**Agent 路径的验收**：没有可在 CI 里跑的模型，所以「模型调用 → 后端工具 → 链路 → 宿主 → 网页」的完整往返由开发期手动验证一次（真实模型打开一个本地页面并复述内容），结果记进本节；链路与两端各自的行为由上面的自动化测试覆盖。

### 12.4 刻意不做

- 动作（点击、输入）、截图、多标签管理、交接工具：P3-4。
- 用户可见的画面与任何渲染进程改动：P3-2b。
- iframe 内的元素：快照只覆盖主框架（`getFullAXTree` 默认行为），页头注明「页面含 N 个子框架未展开」。
- 用户在 Task 里自己新开标签：与「标签页归 Task」不冲突，但 v1 没有需求，不做。

## 13. P3-2b 实施清单：工作台显示

> 状态：已实现（未提交）。与下面清单不同的地方：
> - **标签已关闭时不自动关掉工作台标签**：显示「标签页已关闭」与「关闭标签」按钮，用户知道发生了什么再关。
> - **崩溃的页面不挂到窗口上**：原生画面会盖住「页面已崩溃 · 刷新」提示。
> - **矩形按页面缩放换算**：渲染进程上报 CSS 像素，宿主乘以主窗口 `webContents.getZoomFactor()` 后摆放（Cmd + / - 缩放时不错位）。
> - **工作台贴在窗口右侧，窗口缩放时位置变而尺寸不变**：除 `ResizeObserver` 外还监听窗口 `resize`。
> - **遮挡观察跳过应用自身的 DOM 变化**，只看 `#root` 以外的 portal 与 Sonner Toaster；空的常驻 portal 容器不触发逐帧复核。
> - 标签列表 store 在首次使用时订阅一次宿主推送（与 `runtime-config` 订阅后端快照同一写法），不在 `AppLayout` 另挂 hook。
> - **真实应用验收（2026-10-31）**：Agent 打开本地测试页后，从会话侧栏「浏览器」卡片在工作台打开，原生视图正好摆在占位区域（953, 94, 478×756，与占位矩形一致），页面实时显示；按 Cmd+K 打开命令面板时视图收起、冻结帧随对话框背景一起被模糊，命令面板完整可见；Esc 后视图挂回原位。验收用的临时工作区已删除。截图方式：`BrowserWindow.capturePage` 只截主页面，验收脚本分别截主页面与网页视图后按视图位置合成。

**范围**：用户能在工作台里看到并直接操作 Agent 的标签页；弹层不被网页遮住。

**路线（第零铁律）**：
- **同一时刻至多一个视图挂在窗口上**：工作台只渲染激活标签的面板（`workbench.tsx` 现状），面板卸载即上报 `null`、视图摘下；挂上新视图时宿主自动摘下旧的——摆放状态只有宿主一处事实。
- **遮挡观察集中在一处**：浏览器占位组件自己观察 `body` 的直接子节点（Base UI / Radix 的 portal 都挂在这里）与 Sonner 的 Toaster 区域，算与自身矩形是否相交；各弹层组件不知道浏览器存在。项目同时用 `@base-ui/react`、`radix-ui`、`cmdk`、`sonner`，所以按「body 直接子节点 + 矩形相交」判断，不按某个库的选择器。
- **标签列表是宿主事实**：渲染进程经 IPC 读取与订阅，不进后端 snapshot、不进 localStorage。

### 13.1 宿主控制面

| 文件 | 动作 | 内容 |
| --- | --- | --- |
| `electron/shared/host-contract.ts` | 改 | 通道 `browserTabs`、`browserTabsChanged`（推送）、`browserPlace`、`browserFreeze`、`browserNavigate`、`browserClose`；类型 `BrowserTabSummary`（`tabId`、`taskId`、`url`、`title`、`loading`、`crashed`）、`BrowserPlacement`（`x`、`y`、`width`、`height`，CSS 像素）；`CosirHost.browser` |
| `electron/main/ipc.ts` | 改 | 注册上述通道并校验入参（矩形为有限非负数、`tabId` 形如 `t\d+`、URL 只允许 http(s)）；`registerHostIpc` 增加 `browser: BrowserHost` 参数 |
| `electron/preload/index.ts` | 改 | 暴露 `browser.*`，`onTabsChanged` 同 `backend.onChange` 写法 |
| `electron/main/browser/browser-host.ts` | 改 | `place(tabId, rect \| null)`：挂上（`addChildView`，坐标取整）并摘下其它视图；`null` 或面积为 0 即摘下并恢复 1280×800。`freeze(tabId)`：CDP `Page.captureScreenshot`（JPEG，质量 70）返回 data URL。标签增删、标题与地址变化、崩溃时向主窗口推送全部标签 |
| `electron/main/app.ts` | 改 | `browser.onChange` 转发到 `browserTabsChanged`（与后端快照推送同一写法） |

### 13.2 渲染进程

| 文件 | 动作 | 内容 |
| --- | --- | --- |
| `lib/browser/tabs-store.ts` | 新建 | zustand store：启动时 `browser.listTabs()` 一次，之后只订阅推送；按 `taskId` 选取；不在桌面宿主中时为空 |
| `lib/browser/overlay-watch.ts` | 新建 | `watchOverlays(target, onChange)`：`MutationObserver` 观察 `body` 直接子节点（排除 `#root`）与 `[data-sonner-toaster]`，有覆盖层时按动画帧复核矩形相交，全部消失后停止；返回「当前是否被遮挡」 |
| `lib/workbench/types.ts` / `store.ts` / `context.tsx` | 改 | 新 tab `{ id: "browser:<tabId>", kind: "browser", taskId, tabId, title }`，`openBrowser` 动作；`forgetTasks` 现有的「按 `taskId` 移除」自动覆盖 |
| `components/workbench/browser-surface.tsx` | 新建 | 地址栏（后退、前进、刷新、地址输入，走 `browser.navigate`）+ 占位 div；`ResizeObserver` 上报矩形，卸载上报 `null`；被遮挡时先 `freeze` 取图放进 `<img>` 再上报 `null`，遮挡解除后重新上报矩形；宿主推送里标签已不存在时提示「标签页已关闭」并关闭工作台标签；崩溃时提示「页面已崩溃，刷新重试」 |
| `components/workbench/surface.tsx` / `workbench.tsx` | 改 | 面板注册表加 `browser`；`TAB_KIND_ICONS` 加 `GlobeIcon` |
| `lib/task-rail/store.ts` / `components/task-rail/cards.tsx` / `components/task-rail/browser-tabs-card.tsx`（新建） | 改 / 新建 | 会话侧栏「浏览器」卡片：列出当前 Task 的标签页（标题、地址、加载中 / 已崩溃），点击在工作台打开；没有标签时卡片显示「暂无标签页」 |

### 13.3 测试

| 文件 | 覆盖 |
| --- | --- |
| `apps/desktop/tests/unit/overlay-watch.test.ts`（新建） | jsdom：body 子节点出现 / 消失、相交与不相交、Toaster 区域、停止观察后不再回调 |
| `apps/desktop/tests/unit/browser-tabs-store.test.ts`（新建） | 首次读取与推送合并、按 Task 选取、无宿主时为空 |
| `apps/desktop/tests/unit/workbench-store.test.ts`（改） | `openBrowser` 去重激活、`forgetTasks` 移除浏览器标签 |
| `apps/desktop/tests/desktop-e2e/browser-engine.spec.ts`（扩） | 摆放：挂上后视图位于给定矩形、切换标签只挂一个、`null` 后摘下；冻结返回非空图片 |

### 13.4 刻意不做

- DevTools、缩放、页面内查找、书签与历史。
- 冻结期间刷新冻结帧：遮挡解除即恢复实时画面。
- 多窗口：浏览器视图只挂在主窗口。

## 14. P3-3 实施记录：工具图片通道

> 状态：已实现（未提交）。按 §4.5 的「复用边界」落地，与设计不同的地方：
> - **不新增 `tool_success_with_images`**：给既有的 `tool_success` 加 `images` 参数，结果构造仍只有一个收口。
> - **`ToolImageBody` 登记进持久化读回表**：它只投 mailbox、不落外部队列，但消息体契约要求「入队类型与读回表一一对应」，登记无副作用，契约保持简单。
> - **图片的 `kind` 归入 `CALL_RESULT_KINDS`**：它带 `tool_call_id`、属于那次调用，编辑重跑按调用归属处理。
> - **observe 节点只有观察真的带图时才取 mailbox**：常见路径不碰运行时上下文。
> - **截图尺寸不靠附件规范化控制**：附件策略的最长边是 8192（为用户上传设计），不是 §10 记的 1568；截图分辨率在 P3-4 截图时用 CDP 的 `clip.scale` 控制（视口 1280×800 → 图片 1280×800，不随屏幕倍率翻倍）。
> - **已知限制**：工具结束到下一次请求之间 Run 被取消或挂起时，mailbox 随执行丢弃，这批图片不进 context（与 Run 内通知同一取舍，见 `ToolImageBody` docstring）。

| 文件 | 内容 |
| --- | --- |
| `app/service/attachment/attachment_service.py` | 抽出发布内核 `_publish`（规范化 → SHA-256 → 硬链接发布 → 描述），`upload` 与新增的 `save_image(workspace_id, data, name) -> SavedImage` 共用；`SavedImage.path` 是模型请求里引用图片用的工作区相对路径 |
| `app/core/tool/schemas/tool_observation.py` / `tool_definition.py` | `ToolObservation.images`；`ToolDefinition.returns_images` |
| `app/core/tool/results/tool_success.py` | `images` 参数 |
| `app/core/tool/execute/tool_access_gate.py` | `returns_images` 的工具在模型不支持图片时拒绝（短提示「模型不支持图片」） |
| `app/core/workflow/message_queue/message_bodies/rendering.py` | `user_message_with_images`：文字 + 附件图片的 user 消息，用户输入与工具图片共用 |
| `app/core/workflow/message_queue/message_bodies/tool_image.py` | `ToolImageBody`（`KIND="tool_image"`、`PRIORITY=-1`、不投影、不唤醒） |
| `app/core/workflow/node_helper/tool_observation_projection.py` + `nodes/observation_node.py` | `post_tool_images`：整批 tool 消息写完后投进 mailbox |
| `app/models/message_metadata.py` | `MessageKind` 加 `tool_image` 并归入 `CALL_RESULT_KINDS` |
| `tests/test_tool_image_channel.py` | 测试替身工具走完整通道：存图去重 → 渲染 → `vision_input` 解析成 data URL；准入拒绝；图片排在通知前；只投成功且带图的观察；`ToolBatch` 落库往返保留 `images` |

## 15. P3-4 实施记录：补齐浏览器工具

> 状态：已实现（未提交），真实模型验收通过。

**落点**

| 层 | 文件 | 内容 |
| --- | --- | --- |
| 宿主 | `electron/main/browser/page-input.ts`（新建） | 动作执行：click / type（可清空、可回车提交）/ press / select / scroll / wait_for；遇错即停，结果说明停在哪一步 |
| 宿主 | `browser-tab.ts` | `act`（用户 2 秒内操作过即 `USER_ACTIVE`）、按 CSS 像素出图的截图（`clip.scale = 1/devicePixelRatio`，整页高度封顶 4000）、动作后的加载等待 |
| 宿主 | `browser-host.ts` / `params.ts` / `methods.ts` | `browser.act`、`browser.screenshot`、`browser.tabs`（list / select / close）；`browser.navigate` 加 `new_tab` |
| 后端 | `app/tools/tool_handler/browser/`（`act.py`、`screenshot.py`、`tabs.py`、`handoff.py`） | 四个工具；截图经 `save_image` 与 `tool_success(images=...)` 进模型（`returns_images=True`）；交接是 `execute` 类 HITL |
| 后端 | `browser_args.py`、`browser_display.py`、`common.py`、`tool_names.py` | 动作参数（`type` 区分的联合）、卡片的截图 / 交接字段、`USER_ACTIVE` 与动作失败的建议 |
| 后端 | `define_agents.py`、`constant.py` | 主 Agent 开放全部浏览器工具；通用子 Agent 开放除交接外的工具；`browser_handoff` 进两份禁用名单 |
| 前端 | `components/assistant-ui/tools/browser-page-tool.tsx` / `browser-page-display.ts`，注册表 | `browser-page` 卡片：标题、标签与地址、在工作台打开、截图缩略图（复用消息图片组件）、交接说明与「交还给 Agent」（通用决定命令） |

**实测中改掉的设计**
- **CDP 合成按键也会触发 `before-input-event`**（提交表单时回车的 keyUp），P3-2a 的「CDP 输入不触发」只对鼠标与文字输入成立。改为：动作执行期间及之后 500ms 的输入事件归 Agent，其余才记为用户输入。
- **视图不挂在窗口上时 CDP 滚轮事件永远不返回**：滚动改用页面内 `scrollBy`。
- **工具 handler 收到的是校验后 `model_dump()` 的字典**，不是参数模型实例；首轮真实模型验收里 `browser_act` 因此全部失败（单测直接传了模型实例，没测出来）。已修，单测改走执行器真实的参数校验路径。

**验收**
- 引擎 E2E（真实 Electron）13 条：选择、点击、等待文字、输入并回车提交、滚动；导航后旧 ref 报 `REF_STALE`；文件选择框被拦下；`target=_blank` 开成同 Task 新标签；用户输入时拒绝 Agent；截图为 1280×800；标签列出 / 切换 / 关闭。
- 后端 2530 条、前端单测 622 条通过。
- 真实模型（DeepSeek，临时工作区）：打开表单页 → `browser_act` 输入并提交 → `browser_screenshot` 读出只在画布图片里的文字「蓝色鲸鱼 8086」（快照里只有空的 Canvas 节点）→ `browser_tabs` 列出标签；再 `browser_handoff` 交给用户，Run 挂起，经决定接口交还后 Agent 读回页面标题。临时工作区已删除。

## 16. 文件与浏览器改为独立面板（用户 2026-10-31 提出，已确认并实施）

**需求**：参考 Claude Code 桌面版，浏览器与文件各自是独立的右侧面板，不与子 Agent、配置草稿共用工作台标签条。

**方案**
- **右侧停靠区同一时刻只显示一个面板**：`files`（文件浏览器）、`browser`（内置浏览器）、`workbench`（子 Agent 与配置草稿）。页头三个按钮各自切换自己的面板，点亮的再点一次收起；打开一个即替换另一个。选择状态是不持久化的界面状态（`lib/layout/dock-store.ts`），每个面板各记自己的宽度（`lib/layout-preferences` 的 `dockWidths`）。
- **浏览器面板**：顶栏是当前对话 Task 的标签条（宿主标签列表按 Task 过滤）+ `+` 新建 + 最大化 + 关闭；下面是地址栏与网页。没有标签时显示「输入网址」的空状态。最大化时面板铺满对话区（再点还原）。卡片上的「打开」改为打开浏览器面板并切到该标签。
- **文件面板**：把现有 `WorkspaceFilesSurface` 原样搬进独立面板，按工作区，不再是工作台里的一个标签。
- **工作台**只剩子 Agent 与配置草稿；`WorkbenchTab` 删掉 `files` 与 `browser` 两种 kind（加法减法并重，不留两套入口）。
- **会话侧栏的「浏览器」卡片删除**：浏览器面板自带标签条，侧栏卡片重复；页头地球按钮带当前 Task 的标签数角标。
- **`+` 新建标签**：标签仍归当前 Task（Agent 经 `browser_tabs` 能看到并操作用户开的标签），受同样的两个上限约束——渲染进程从配置中心接口读当前上限随请求带给宿主。这会改动决定记录里的「v1 不做用户独立浏览」：不是独立浏览，而是用户在 Task 里开标签。

**确认结果**：用户在当前 Task 里开，行为类似可交互终端——人和 Agent 都能新开、都能操作；标签记下开启方（`createdBy`），`browser_tabs` 对用户开的标签标注 `(opened by the user)`。

### 16.1 实施记录

- 宿主：`BrowserHost.userOpen(taskId, limits, url?)` 走与 Agent 相同的上限检查；`BrowserTab` 记 `createdBy`（`agent` / `user`）进标签摘要。控制面新增 `browser.open`，`ipc.ts` 校验 Task id、上限与网址（无协议补 https，只放行 http(s)）。
- 后端：`browser_tabs` 列表行带开启方标注；工具与宿主方法不变。
- 渲染进程：`lib/layout/dock-store.ts`（停靠区三选一与浏览器铺满）、`components/layout/use-dock-panels.ts`（可显示条件、宽度、快捷键，取代 `use-workbench-panel.ts`）、`components/layout/dock-frame.tsx`（文件与浏览器面板共用外框）、`components/files/files-panel.tsx`、`components/browser/browser-panel.tsx` + `browser-view.tsx`、`lib/browser/panel-store.ts`、`lib/browser/open-tab.ts`。工作台删去 `files` / `browser` kind 与 `panelOpen`；会话侧栏的浏览器卡片删除。
- 测试：单测 622 条通过；浏览器 E2E 改为面向独立面板（文件面板标签「文件」、收起按钮「收起文件」、宽度 `setDockWidth`），测试宿主替身补上 `browser` 能力（空标签）；剩余失败与 HEAD 基线一致。桌面 E2E 17 条通过。真实应用里验证：地球按钮打开面板、空状态输入网址新开标签并渲染页面、角标计数、铺满与还原、文件面板。
- 已知：对话区被压到最小宽度（400px）时，输入框工具栏的「暂无可用模型 / 模型设置」文字会竖排换行，属于输入框工具栏自身的收窄问题，与面板无关，未在本次处理。


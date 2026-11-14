# 内置浏览器能力缺口改造方案

> 2026-11-14。来源：对照 Claude Code 的浏览器工具（见 [claude-code-tool-mapping.md](../../claude-code-tool-mapping.md) 第四节），按七项能力差距分三个方向调研：网络响应体、输入粒度、观察粒度。
>
> 测试计划与回归测试计划见 [browser-capability-gaps-test-plan.md](browser-capability-gaps-test-plan.md)。
>
> 修订记录：
> - **产品复核**：目标不是对齐 Claude Code 的工具清单，而是让 Agent 在 cosir 的核心浏览器场景里不卡住，即开发、调试、验证用户自己的应用。据此补上「列表行标出响应体状态」。响应体采集范围由用户定为**所有页面**。三击与按键重复曾被砍掉，用户以「测试连点」为场景加回，并一起加上鼠标连点。
> - **深度复查**：测试计划第五节列出的 P1–P12，本版逐条复查代码后给出解法，写进下文各节。对应关系见文末「问题处置表」。
>
> 标「待实测」的是 CDP 行为推断，需在 Electron 上验证（测试计划第四节）。

## 结论

| 差距 | 判断 | 本次 |
|---|---|---|
| 读网络响应体 | 核心场景里最常卡住的一环。现状不只是没开放接口：宿主把响应体缓冲压到了 1 KB，日志条目也不留 requestId | **做**，先改造标签日志，再开读取 |
| 区域截图 | 核对图表、Canvas、某块布局，就是「验证自己做的界面」；ref 只发给可交互元素，这些截不了特写 | **做**，按观察处理，不走修改槽位 |
| 横向滚动 | 宽表格、看板类应用会遇到：列虚拟化时看不见的列没有 ref，滚不进视野 | **做**，沿用页面函数方案，按轴判定 |
| 连点与连按 | 测试场景：重复提交、防抖、计数是否被多加。多写几个动作做不到，每步之间至少结算 0.5 秒 | **做**：`click` 与 `press` 加同名的 `repeat`，在一个动作内连续执行、中间不结算 |
| 三击 | 选中一整段（富文本里设格式、替换一行）；宿主循环已支持 N 次，只改取值 | **做** |
| 指定滚动量、截图缩小、快照子树 / 深度 / 字符上限、act 中途观察 | 现有能力能覆盖，或补了会破坏现有约定 | **不做**，见「刻意不做」 |

---

【任务类型】新增能力（响应体读取、区域截图、横向滚动、连点与连按、三击）。其中响应体一项调查后发现现有结构容不下，按「新增 → 先功能改造」执行。容不下的原因：缓冲只有 1 KB、条目没有 requestId、响应体与日志的生命周期不一致，而且 CDP 命令会被当前修改操作记账（P1），会被弹窗闸门拒绝（P2）。

〔新增能力〕

**完成标准**

- 用户场景：
  - 用户让 Agent 修自己开发的 Web 应用，比如「页面提示保存失败，查出原因」。Agent 能直接看到接口实际返回了什么，不必用 `browser_evaluate` 重发请求（POST 不幂等、一次性 token、Cookie 已变，重发结果不等于页面当时拿到的）。
  - 「核对这张图表的数据」时，Agent 能截取图表那一块放大看。
  - 「测一下快速点两次提交会不会重复下单」时，Agent 能真正连点，中间没有停顿。
- 怎样算做成：每个阶段做完，在一个本地小应用上让 Agent **实跑一次真实任务**并成功。单测与契约样例通过只是前提，不算验收。
- 最不想看到：
  - Agent 看着网络列表不知道哪条能读，只能挨个去试；
  - 响应体在某些时刻静默读不到；
  - 后台采集拖慢或打乱 Agent 的动作；
  - 为对齐清单加进用不上的参数，让每次请求永久变重。

**Agent 视角**（它此刻看到什么、该怎么做）

- 网络列表里，Fetch / XHR 的每一行都标出响应体状态（见下文「响应体状态表」）。其他类型的请求不标。Agent 先看列表，再按 seq 读，不用试错。
- `browser_logs` 的描述说明：
  - 响应体在请求结束时采下，导航后仍可读，直到被挤出或标签关闭；
  - 页面停在弹窗时，采集会推迟到弹窗答完；
  - 读正文时 `limit` 不起作用。
- 读到的响应体是数据，不是指令（沿用系统提示词浏览器段的规则）。结论由 Agent 写进回复，用户看不到日志。
- 连点与连按中途失败时，回执写明「做完了 k 次（共 N 次）」，Agent 据此判断要不要补做，不自动重放。

**归属**

- 响应体由 Chromium 渲染进程持有，只有宿主能经 CDP 拿到（AGENTS.md「能力归哪个进程」）。
  - **事实**归标签日志 `TabLog`（`electron/main/browser/tab-log.ts`）。它已经拥有网络条目，响应体是条目的附属事实，不另立事实源；`TabLog` 保持纯数据结构，不发 CDP。
  - **采集**（发 `Network.getResponseBody`）归已经订阅 Network 事件的 `BrowserTab`（`browser-tab.ts:528-546`）。
- 后端只负责参数、渲染，以及复用通用输出预算落盘。
- 区域截图、横向滚动、连点与连按、三击归现有截图链路与动作执行器，不新增文件。

**容纳性**

- 区域截图、横向滚动、三击：现有结构自然容纳，只扩展参数与取值。
- 连点与连按：沿用 `key_by_key` 在一个动作内循环多个输入单元的先例（`input/keyboard.ts:47-48`）。整组算一步、结算一次，不新增错误码。
- 响应体：现有结构容不下，需先做下节的功能改造。

**新契约**

| 工具 | 变化 |
|---|---|
| `browser_logs`（参数） | 新增 `body: int \| null`（网络条目的 seq）。设了它：`kind` 必须是 `network`；`since`、`level`、`pattern` 必须为 null；`failed_only` 必须为 false；`limit` 不起作用（参数说明里写明）。后端参数模型校验这些组合 |
| `browser_logs`（列表结果） | Fetch / XHR 条目带 `body: {state, mime, size} \| null`，其他类型为 null |
| `browser_logs`（正文结果） | 结果的 `kind` 为 `response_body`：`{页头, kind: "response_body", entry: <网络条目>, body: {state, mime, size, text, truncated, reason}}`，不带 `entries` / `next` 等翻页字段（P6） |
| `browser_logs`（找不到条目） | seq 不在这个标签的网络日志里（从没有过、指向 console 条目，或已被 1000 条容量挤掉）时，报 `INVALID_PARAMS`，消息写明「这个标签的日志里没有 #N 网络请求，可能已被挤出」。沿用现有错误码（P5） |
| `browser_screenshot` | 新增 `region: {x, y, width, height} \| null`（视口 CSS 坐标）。与 `full_page`、`ref` 互斥（后端校验）；必须整个落在可视视口内，否则报现有的 `OUT_OF_VIEWPORT`（宿主按几何判断）；不留边，放大规则同元素截图；正文说明区域截图的像素与视口坐标不一致（P7） |
| `browser_act` → scroll | `direction: up \| down \| left \| right \| null` |
| `browser_act` → click | `click_count: 1 \| 2 \| 3`（一次多击手势）；新增 `repeat: 1–20`（独立点击 N 次，每次 clickCount 都是 1，中间不结算）。两者不能同时大于 1（后端校验） |
| `browser_act` → press | 新增 `repeat: 1–20`（连按 N 次，中间不结算），与 click 的 `repeat` 同名同义 |

**响应体状态表**（P4）

| state | 何时 | 列表行显示 | 正文读取 |
|---|---|---|---|
| `pending` | 请求在途，或已结束、采集命令还没回来 | 「采集中」 | `text: null`，`reason` 说明稍后再读 |
| `deferred` | 采集时页面停在弹窗（本标签或同进程标签），等弹窗答完补采 | 「等弹窗答完后采集」 | 同上 |
| `text` | 文本类响应体已采下 | mime 与大小 | 正文；超过宿主单条上限时 `truncated: true` |
| `empty` | 响应体为空（204、HEAD） | 「空」 | `text: ""` |
| `binary` | 非文本类 mime | mime 与传输大小，不取正文 | `text: null`，只给类型与大小 |
| `too_large` | 超过渲染进程单条缓冲，Chromium 不保留 | 「过大，未保留」与大小 | `text: null` |
| `evicted` | 采下过，但被每标签总量的先进先出挤掉 | 「已被挤出」 | `text: null` |
| `streaming` | `resourceType` 为 `EventSource`（不会正常结束） | 「流式，无正文」 | `text: null` |
| `unavailable` | 采集失败（页面在补采前已导航、命令超时、标签关闭等） | 「未能采集」与原因 | `text: null`，`reason` 给原因 |

- **流式**：只按 `resourceType: EventSource` 判定。分块返回、最终会结束的 fetch 照常在 `loadingFinished` 后采集。
- **失败的请求**（`loadingFailed`）没有响应体，`body` 为 null，失败原因照旧显示在条目上。
- **大小口径**：
  - `text` 与 `empty` 是解码后正文的 UTF-8 字节数；
  - `binary` 与 `too_large` 是 `loadingFinished` 的 `encodedDataLength`（传输大小），显示为「传输」。
- **文本类 mime**：`application/json`、`*+json`、`text/*`、`application/xml`、`*+xml`、`application/javascript`、`application/x-www-form-urlencoded`。`base64Encoded` 的文本先解码。

**上限分层**（P3）：哪一层决定什么，只此一处。

| 层 | 上限 | 决定 |
|---|---|---|
| 渲染进程（`Network.enable` 的 `maxResourceBufferSize` / `maxTotalBufferSize`） | 单条 1 MB，总量 8 MB | 能不能取到：超过单条上限的，Chromium 不保留，记为 `too_large`；在采集之前就被总量挤掉的，记为 `unavailable` |
| 宿主 `TabLog` | 单条存至多 256 KB 文本，每标签总量 2 MB | 存多少：超过 256 KB 截断并 `truncated: true`；总量超出按先进先出把旧正文置为 `evicted`，条目本身保留 |
| 后端通用输出预算 | 20,000 字符 | 给模型多少：超出写入 `.cosir/tool-artifacts/` 并给路径（`core/tool/execute/tool_output_budget.py`） |

〔功能改造：标签日志承载响应体〕

**现状 → 终态**

- 现状：
  - `Network.enable` 把渲染进程的响应体缓冲设为 1 KB（`browser-tab.ts:37-38、252`，注释写明「从不取响应体」）。
  - `NetworkEntry` 只有 seq / method / url / status / resourceType / failure / durationMs（`tab-log.ts:13-21`）；CDP requestId 只活在在途表 `inFlight` 里，请求一结束就丢（`tab-log.ts:64、111-163`）；也不记 mime。
  - 标签会话上发出的**每一条** CDP 命令都会交给当前修改操作记账（`browser-tab.ts:161-163` → `operation.ts:74-77`），影响输入单元的释放条件（`operation.ts:104`）与收尾等待（`OPERATION_DRAIN_MS`）。
  - 页面停在弹窗时，CDP 命令被闸门直接拒绝，在途的被中断（`cdp-session.ts:82-85`、`interrupt`）。
- 终态：
  1. **记账隔离（P1）**：`CdpSession.send` 增加选项 `untracked`。设了它的命令不交给标签级的操作追踪（`onCommand`），只供只读的后台命令使用。目前唯一的使用方是响应体采集。闸门与时限照常生效。
  2. **采集（P2、P3）**：
     - `Network.responseReceived` 时把 `response.mimeType` 记到条目上。
     - `Network.loadingFinished` 时，`TabLog.requestFinished` 返回该条目；对 Fetch / XHR，`BrowserTab` 按 mime 分流：文本类发一次 `Network.getResponseBody`（`untracked`，时限 5 秒），二进制直接记 `binary`。
     - 回执写回 `TabLog`，按上限分层截断或挤出；失败按原因记 `deferred` 或 `unavailable`。
  3. **弹窗补采（P2）**：
     - 采集命令因弹窗被拒或被中断（错误码 `DIALOG_OPEN`）时，条目记为 `deferred`，`TabLog` 保留它的 requestId。
     - 同进程的弹窗答完后，`BrowserHost.resumeProcess` 已经会逐个通知同进程标签（现在只用来补设设备模拟）。这一步扩展为「弹窗之后的恢复」：补设设备模拟、继续操作收尾、补采 `deferred` 的响应体。
     - 补采时页面若已导航，渲染进程的旧响应体已清，记为 `unavailable`（原因：页面在采集前已离开）。
  4. **读取**：`browser_logs` 读取仍是「只读缓冲、不发 CDP」，页面停住、导航之后照样能读，生命周期与条目一致。
- 动机：只有「请求结束就采、被弹窗挡住就在答完后补采」，能让响应体与日志条目同生共死。按需读取会让 `logs` 失去「停住也能读」的性质（`browser-host.ts:186`），导航后也大多取不到。

**行为账**

- 行为不变：
  - `browser_logs` 不带 `body` 时，条目原有字段不变（Fetch / XHR 条目多一个 `body` 状态）；
  - console 日志不变；
  - 条目容量与淘汰规则（network 1000 条，先进先出）不变；
  - 交接中仍可读日志；
  - 动作的结算、收尾与超时不受采集影响（记账隔离保证）；
  - 弹窗答完后的设备模拟补设与操作收尾不变。
- 有意改变：
  - 渲染进程响应体缓冲从 1 KB 调到单条 1 MB、总量 8 MB；
  - 所有页面的每个文本类 Fetch / XHR 请求，结束后都多一次不记账的 CDP 往返；
  - 宿主主进程每个标签至多多占 2 MB 响应体文本；
  - 重定向链上只有最后一跳有响应体；
  - 交接期间用户登录的接口响应也会被采到（问题清单 4，按「先功能后安全」记入待办）；
  - `BrowserTab.resyncEmulation` 改名为 `resumeAfterDialog`（职责从「补设设备模拟」扩展为「弹窗之后的恢复」，见改动账本）。

**消费方**

- 响应体的读取方只有 `BrowserHost.logs`（`browser-host.ts:186-194`）→ 后端 `tool_handler/browser/logs.py` → `rendering.py`。
- `resyncEmulation` 的调用方只有 `BrowserHost.resumeProcess`（`browser-host.ts:558-561`），随改名一并改。
- `CdpSession.send` 的现有调用方都不传 `untracked`，行为不变。
- 页面结果里的日志计数（`TabLog.digest`）不受影响。

**分阶段**

| 阶段 | 内容 | 验证 |
|---|---|---|
| 1a | `CdpSession` 的 `untracked`；`TabLog` 的 requestId、mime 与响应体槽位、状态表、上限分层；`BrowserTab` 的采集与弹窗补采；缓冲调大；`resyncEmulation` 改名 | `browser-tab-log` 单测（全部状态、上限、挤出、重定向）；新建 `browser-tab-network` 单测：采集触发链、记账隔离（act 期间结束的请求不推迟收尾）、弹窗挡住后补采、补采前已导航 |
| 1b | `browser_logs` 的 `body` 参数与校验、列表行状态、`response_body` 结果、wire 与渲染、工具描述 | 契约样例、后端守卫、渲染测试；引擎 E2E 用本机服务验证 JSON 响应可读、导航后仍可读、「请求返回后立刻 alert」的页面在答完弹窗后可读；**实跑**：Agent 查出本地小应用「保存失败」的原因 |
| 2 | 区域截图，并改正 `ref` 说明里的示例 | 新建 `browser-page-capture` 单测（截区、放大、越界、冻结帧不变）、契约样例、后端渲染测试；**实跑**：Agent 截取并核对本地页面上的一张 Canvas 图表 |
| 3 | 横向滚动、三击、连点与连按 | 后端参数测试（含互斥校验）、宿主契约单测、`browser-pure` 单测与结算矩阵（三击的 clickCount 依次为 1、2、3；连点每次都是 1；中途失败报「做完 k 次」）；引擎 E2E 加宽表格与计数按钮；**实跑**：Agent 测出本地小应用「快速点两次重复下单」 |

- 每阶段独立可交付；被取代的旧常量与注释（「从不取响应体」）在 1a 删除。
- 引擎 E2E 与原生验收的窗口都是 `show: false`，不弹窗。desktop-smoke 会打开真实主窗口，跑之前要征得用户同意，本方案不需要跑它。
- 实跑验收没有自动化入口，由用户在 `npm run electron:dev` 里配合发起。

【链路复用】

- 直接复用：
  - **跨语言契约链**：参数模型 → `scripts/export_browser_tool_schema.py` → `contract/tool-args.schema.json` → `params.ts` 校验，守卫 `tests/test_browser_contract_guard.py`、`tests/unit/browser-contract.test.ts`。
  - **通用输出预算**：超过 20,000 字符就写入 `.cosir/tool-artifacts/` 并给路径。响应体不另做落盘。
  - **弹窗之后的恢复链路**：`BrowserHost.resumeProcess` 逐个通知同进程标签（`browser-host.ts:558-561`）。补采挂在这里，不另建通知。
  - **元素截图的放大逻辑**（`page-capture.ts` 的 `zoom`）：区域截图沿用，截区直接用区域本身、不加边距。
  - **一个动作内循环多个输入单元**（`key_by_key`，`input/keyboard.ts:47-48`）：连点与连按沿用。点击循环本身已支持 N 次（`input/pointer.ts:24-27`），三击只改取值。
  - **现有错误码**：`INVALID_PARAMS`（找不到条目）、`OUT_OF_VIEWPORT`（区域越界）、`SCROLL_END`（横向到头）。不新增错误码。
- 改造后复用：
  - **`CdpSession.send`** 加 `untracked` 选项。原调用方不传，行为不变。
  - **`SCROLL_AREA` 页面函数**（`input/page-functions.ts:14-39`）：参数从「是否向下」改为「轴加方向」，按轴判定 `overflowX` / `overflowY`、`scrollWidth` / `scrollHeight`、`scrollLeft` / `scrollTop`。纵向分支的语义与 `SCROLL_END` 判定不变；边界文案保留 `is already at the` 前缀。
  - **`BrowserTab.resyncEmulation` → `resumeAfterDialog`**：原有两件事（补设设备模拟、继续操作收尾）不变，加上补采。
  - **`TabLog`**：网络条目加 mime 与响应体状态，原有读取路径不变。
- 无可复用：无。

【问题清单】

1. **响应体缓冲 1 KB**。超过 1 KB 的接口即使补上读取也取不到。证据：`browser-tab.ts:37-38、252`。结构层，置信度高。由阶段 1a 解决。
2. **CDP 滚轮挂起的前提可能已过时**。滚动改用 `scrollBy`，理由是「视图不挂窗口时滚轮事件永不返回」（`docs/plan/embedded-browser-p3-discussion.md` §15）。之后 §17 实测「开焦点模拟后合成鼠标事件正常」（`browser-tab.ts:254-256`），两者很可能是同一个根因。局部问题，置信度中，待验证。本次不改滚动机制，只记录。
3. **截图可能被模型服务商缩小**，导致「截图像素 = 视口坐标」不成立。cosir 侧只在最长边超过 8192 时缩放（`image_normalizer.py:78-92`）。契约层，置信度低（未实测）。本次不改，并入待办「标注截图」统一设计。
4. **交接中可读日志，且所有页面都采集**。用户在交接时登录第三方网站，Agent 事后能读到登录接口的响应体（`browser-host.ts:186-194` 无交接检查）。安全层，置信度高。按「先功能后安全」记入 AGENTS.md 待办「安全与权限」的浏览器条目，本次不拦。
5. **一处 docstring 漂移**（P12）。`browser_args/actions/keyboard.py:48` 写「宿主时限按动作数估算」，实际宿主方法总时限固定为 330 秒（`contract/method-deadlines.json`）。阶段 3 改到这个文件时一并修正。

【选定路线】

- 结论：
  - **响应体**：先改造为「请求结束就采、不记账、被弹窗挡住就在答完后补采」，所有页面都采；再给 `browser_logs` 加 `body` 参数，并在列表行标出状态。
  - **区域截图**：扩展现有截图。
  - **横向滚动、三击、连点与连按**：扩展现有动作。
- 被否的候选：
  - **按需读取响应体**（读的时候才发 `Network.getResponseBody`）：导航后基本取不到，页面停在弹窗时会失败，`logs` 也会失去「停住也能读」的性质。
  - **采集走修改操作的记账**（不加 `untracked`）：act 期间结束的请求会拖住收尾，慢时让标签变成「结果未知」（P1）。
  - **弹窗挡住就放弃**：「保存失败」类页面通常拿到响应就 alert，核心场景会大面积采不到（P2）。
  - **为补采另建「弹窗关闭」事件**：`resumeProcess` 已经是这件事的唯一通知点，另建会出现两条恢复路径。
  - **只在本机、局域网与本地文件页面采集**：日常浏览零成本，也绕开了大半安全问题；但用户选择所有页面都采，覆盖调试第三方网站的场景。
  - **新增独立工具 `browser_response_body`**：采集改为请求结束时进行后，读取仍是「只读缓冲」，与 `browser_logs` 是同一职责，用参数就够了。新工具还要多改工具名、Agent 清单、系统提示词的浏览器工具集、前端目录和展示契约。
  - **读正文时把 `limit` 也设成必须为空**：`limit` 不可为 null、默认 50，硬性要求反而让模型每次都得特意传值；改为「不起作用」并在说明里写明。
  - **找不到条目时返回「不可用 + 原因」**：调用者给了一个不存在的 seq，属于参数问题，用 `INVALID_PARAMS` 更准确，也不用为此扩展结果形状。
  - **连点用多个 click 动作表达**：每步之间至少结算 0.5 秒，测不出连点，还占满 10 个动作名额。
  - **只加按键重复、不加鼠标连点**：大多数连点测试测的是鼠标。
  - **用 `click_count` 表达连点**：多击手势的 clickCount 依次递增，浏览器按双击、三击理解，不是 N 次独立点击。
  - **区域截图沿用元素截图的 8px 边距**：区域是 Agent 精确给出的框，加边距会让「截到的就是我给的那块」不成立。
  - **区域越界时裁剪**：裁剪后的图与 Agent 给的坐标对不上，又没有提示；报 `OUT_OF_VIEWPORT` 让 Agent 先滚动，更不容易错。
  - **改回 CDP 滚轮以支持滚动量与只听 wheel 事件的页面**：结算会从「确定没滚就失败」退化为「已派发、结果未知」，并且依赖问题清单 2 的假设，先不做。
  - **用 `Fetch` 域拦截取响应体**：会侵入请求时序，与「少暴露自动化痕迹」的取向冲突（`browser-tab.ts:105`）。

【实现约定】（复查中定下、实施时必须照做的细则）

1. **`repeat` 的缺省（P8）**：
   - 宿主 `PageAction` 类型里 click 与 press 的 `repeat` 是必填的 `number`，不是可选字段；`toAction` 从 wire 原样映射（导出的 schema 已把每个属性标成必填）。
   - 直接构造宿主形状动作的测试替身与验收脚本（单测替身、`browser-native/main.ts`、`browser-engine.spec.ts`）缺了它，`tsc --noEmit` 会报错：根 `tsconfig.json` 包含 `tests/`，只排除 `electron/main` 与 `electron/preload`。
   - 前提是这些构造点都带着宿主类型。引擎 E2E 的 `call` 经 `app.evaluate` 把请求序列化进 main 进程，原先请求类型是 `Record<string, unknown>`，类型检查覆盖不到，实施中因此漏改了三处 press（问题账本 L-3-1）。`call` 的请求已改为宿主方法自己的请求类型（`context`、`view`、`newTab` 由 `call` 补上），宿主内部形状的全部构造点都纳入 `tsc`。
   - 执行器不再做「缺省当 1」的兜底。
2. **「做完 k 次」的语义（P9）**：
   - k 指已经完整做完的次数。第 k+1 次失败时，错误消息末尾附上 ` (stopped after k of N)`。
   - 步骤状态沿用现有规则（`input/actions.ts:41`）：派发过可能生效的命令之后才失败，记为 `unknown`。这包括「第 1 次点开了弹窗、第 2 次被闸门拦下」的情形：结果是 `unknown`，不是 `failed`，同批随后的 `dialog` 动作照常可以不带 id 作答。
3. **横向滚动的提示（P11）**：
   - `SCROLL_END` 的 `status_hint` 从「已到底」改为「已到尽头」；
   - 页面函数的边界文案按方向写成「already at the bottom / top / left edge / right edge」，保留 `is already at the` 前缀。

【改动账本】

- 精简：
  - `browser-tab.ts`：删除 1 KB 常量与「从不取响应体」的注释，换成两个有依据的渲染进程上限，约 −2 行；
  - `rendering.py` 的截图正文：「视口 / 元素」两句合并为按截区说明，约 −3 行；
  - `browser_act` 的 scroll 说明改写而不叠加，约 −1 行；
  - `screenshot.py` 的 `ref` 说明去掉「chart」这个不成立的示例（P7），工具描述同步改，约 ±0 行；
  - `keyboard.py:48` 修正过时的注释，约 ±0 行。
- 新增（预估）：

  | 阶段 | 文件 | 行数 |
  |---|---|---|
  | 1 | 宿主 `cdp-session.ts`（`untracked`） | 约 +4 |
  | 1 | 宿主 `tab-log.ts`（mime、响应体状态、上限分层、补采登记、正文读取） | 约 +110 |
  | 1 | 宿主 `browser-tab.ts`（采集、分流、补采、`resumeAfterDialog`） | 约 +50 / −4 |
  | 1 | 宿主 `browser-host.ts`（`resumeProcess` 调用改名；`logs` 分出正文读取） | 约 +10 / −2 |
  | 1 | 宿主 `params.ts`、`wire.ts`、`contract/wire-samples.json` | 约 +50 |
  | 1 | 后端 `browser_args/logs.py`、`tool_handler/browser/logs.py`、`rendering.py` | 约 +60 |
  | 1 | 测试（新建 `browser-tab-network.test.ts`，扩展 `browser-tab-log` / 契约 / 后端） | 约 +190 |
  | 2 | 后端 `browser_args/screenshot.py`、`tool_handler/browser/screenshot.py`、`rendering.py` | 约 +25 |
  | 2 | 宿主 `params.ts`、`browser-host.ts`、`page-capture.ts` | 约 +25 |
  | 2 | 测试（新建 `browser-page-capture.test.ts`） | 约 +50 |
  | 3 | 后端 `actions/page.py`、`pointer.py`、`keyboard.py`（取值、两个 `repeat` 与互斥校验） | 约 +25 |
  | 3 | 宿主 `params.ts`、`input/page-functions.ts`、`input/pointer.ts`、`input/keyboard.ts` | 约 +45 / −10 |
  | 3 | 后端 `tool_handler/browser/errors.py`（`SCROLL_END` 提示） | 约 ±0 |
  | 3 | 测试 | 约 +80 |

  `browser_act` 的说明每次请求永久多约 150 token（两个 `repeat` 各约 70，三击几乎为零）；`browser_logs` 与 `browser_screenshot` 各多约 80–100 token。

- 净变化：约 +720 / −20 行（含测试约 +320）；导出的 `tool-args.schema.json` 随之再生成。交付后以 `git diff --stat` 对账。
- 目录 or 文件命名变更：
  - 新增测试 `apps/desktop/tests/unit/browser-tab-network.test.ts`：
    - 正向检索：按「浏览器标签的网络采集」找，落在 `browser-tab-*` 一组；
    - 反向读出：一看就知道测的是 BrowserTab 的网络部分。
  - 新增测试 `apps/desktop/tests/unit/browser-page-capture.test.ts`：与被测文件 `page-capture.ts` 同名，正向、反向都成立。
  - 方法改名 `BrowserTab.resyncEmulation` → `resumeAfterDialog`：职责从「补设设备模拟」扩展为「弹窗之后的恢复」，旧名会让读者以为补采不在这里。
  - 源码不新增文件：响应体是网络条目的附属事实，放进 `tab-log.ts`；采集放进已订阅 Network 事件的 `browser-tab.ts`。
- 同步：
  - AGENTS.md「内置浏览器」段落：网络请求归标签日志的那句补上「含请求结束时采集的文本响应体，弹窗挡住时答完后补采」；
  - AGENTS.md 待办「安全与权限 → 浏览器」补一句「所有页面的响应体都被采集，交接中也可读」；
  - `claude-code-tool-mapping.md` 第四节表格对应行；
  - 测试计划文档按本版契约修订（状态表、`response_body` 结果、`INVALID_PARAMS`、区域越界报错、`repeat` 必填、「stopped after k of N」）。

【刻意不做】

- **连点之间的间隔可调**：连点只用于「中间没有停顿」的测试；要模拟人手的节奏，拆成多个动作即可。
- **N 次多击**（`click_count` 与 `repeat` 同时大于 1）：没有场景。校验时拒绝，免得两个参数叠加后语义含糊。
- **指定滚动量**：没有真实需求证据。`scrollBy` 每次约 0.8 屏，配合 `SCROLL_END` 足以翻完列表。
- **只听 wheel 事件的页面**（地图、画布表格）：要改回 CDP 滚轮，见问题清单 2，先实测。
- **截图缩小（scale）**：会打破「截图像素 = 视口坐标」的约定，需先定规则；与问题清单 3、待办「标注截图」一起设计。
- **快照按 ref 读子树、限制深度、字符上限**：ref 只发给可交互元素，main、table、list 这类容器没有 ref，子树价值有限；`find`、`scope=viewport`、`from_line` 已覆盖多数需求。出现失败案例再做，届时考虑「find 带上子孙」开关，而不是给容器发 ref。
- **act 步骤间插入观察**：拆成两次调用即可等价覆盖。步骤间截图要给 act 声明 `returns_images`，而准入门禁按工具整体拒绝，会让不支持图片的模型连 act 都用不了（`tool_access_gate.py:143`）。
- **编辑快捷键**（全选、撤销）：macOS 上要另传编辑命令，跨平台行为不一致，有意不做（`input/modifiers.ts:3-6`）。
- **SSE 正文与 WebSocket**：SSE 列表行标为「流式，无正文」。WebSocket 本来就不进网络日志：宿主只订阅四个 Network 事件，没有 `webSocketCreated`。本次不扩大范围。
- **跨进程 iframe 与 worker 的请求**：本来就不在日志里（子会话不开 Network，`page-frames.ts:22`），不扩大范围。
- **限制采集的并发数**：页面一次性发出大量请求时，采集命令会同时在途。命令不记账、互不阻塞，目前没有证据需要限流。
- **交接中读响应体的限制**：见问题清单 4，并入「安全与权限」整体设计。

---

## 问题处置表

测试计划第五节 P1–P12 在本版的去处：

| 编号 | 问题 | 处置 |
|---|---|---|
| P1 | 后台采集被当前修改操作记账 | `CdpSession.send` 加 `untracked`（功能改造 终态 1） |
| P2 | 弹窗挡住采集 | 记为 `deferred`，借 `resumeProcess` 在答完后补采（终态 3） |
| P3 | 上限口径打架 | 「上限分层」表：渲染进程决定能否取到，宿主决定存多少，后端预算决定给模型多少 |
| P4 | 列表状态不够 | 「响应体状态表」九种状态，写明流式判定、大小口径、文本类 mime |
| P5 | 过滤条件与找不到条目 | 新契约：`limit` 不起作用；找不到报 `INVALID_PARAMS` |
| P6 | 正文结果的形状 | 结果 `kind: "response_body"`，不带翻页字段 |
| P7 | 区域截图规则与说明漂移 | 新契约：不留边、整块须在视口内否则 `OUT_OF_VIEWPORT`；去掉 `ref` 说明里的「chart」 |
| P8 | `repeat` 缺省导致零次 | 实现约定 1：类型必填，由 `tsc` 拦截，不做运行期兜底 |
| P9 | 「第 k 次」语义 | 实现约定 2：k 为已做完次数，状态沿用现有规则 |
| P10 | 证据与事实不符 | 已在上一版更正 |
| P11 | `SCROLL_END` 提示 | 实现约定 3 |
| P12 | docstring 漂移 | 问题清单 5，阶段 3 一并修正 |

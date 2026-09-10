# other-tool-ui-refactor 第二轮独立方案验收

> 状态：历史验收记录，仅保留审计用途。2026-09-10 已确认采用绿地简化方案，当前实现不再
> 使用本文提到的 Web 专用 `kind`、projection、snapshot allowlist 或 renderer。

验收日期：2026-09-08  
验收对象：docs/plan/other-tool-ui-refactor-plan.md、上一轮验收文档、当前 apps/backend / apps/desktop 代码，以及 assistant-ui 官方文档。  
范围约束：本轮只更新本文件；未修改源代码、测试代码、AGENTS.md 或方案文档。

## 1. 结论

结论：**方案 PASS；实现尚未通过，需完成“实现前置门禁”后再做工程验收。**

最新版方案已经对上一轮 F1–F7、R1–R5 给出明确落点、数据不变量、测试断言和失败门禁。当前代码仍有未实施项，但按本轮要求不以实现缺口否定方案本身。

## 2. 代码事实

### 当前已实现

- 单用户本地拓扑已成立：Tauri Rust host 启动并监管本机 FastAPI；FastAPI 以 127.0.0.1 监听，Tauri 动态分配端口、等待 /health、向 WebView 提供地址；关闭时终止进程树，异常退出允许一次自动恢复，之后显示失败并等待人工重试。证据：apps/desktop/src-tauri/src/backend_supervisor.rs、backend_process.rs、backend_readiness.rs、apps/backend/app/__main__.py。
- 权威数据存放在后端 SQLite：主库为 storage/app.sqlite3，日志和 LangGraph checkpoint 也由 Settings 指向本地 SQLite；前端只通过 Assistant Transport 读取/发送状态，不执行工具、不保存对话事实。证据：apps/backend/app/config/settings.py、apps/backend/app/storage/store_engines.py、apps/desktop/README.md。
- ToolObservation 已区分 content（模型通道）、data（客户端展示通道）和 internal_data；ToolObservationBudget 已分开处理模型输出预算和展示数据预算。
- ToolCallCreatedEvent / ToolCallStatusChangedEvent 已由 ConversationEventProjector 投影到 snapshot，再由 Transport 推送给前端；converter 已把状态、presentation、data、error、result 放进工具 part/artifact。
- fake provider 注入脚手架和 Web provider 选择测试已经存在，可覆盖工具层 provider、URL、顺序、部分失败等基础行为。

### 方案要求实施、当前尚未实现

- web_extract.py 当前仍以 data={"web": results} 返回，结果项包含 content 和 metadata；没有 web-extract-status 字段级 UI projection。
- web_search.py 当前仍返回 data={"web": web_results}，没有 web-search-results kind。
- tool_observation_dispatcher.py 当前仍在 completed 事件中执行 result=summary["content"]；正文仍可经 event.result → snapshot.result → converter 进入 UI state。
- conversation_state_snapshot.py 目前只检查 tool data 是 dict，没有 Web discriminated union/allowlist，也没有拒绝 content、metadata、raw response 等字段。
- apps/desktop/lib/assistant/contract.ts 的 TransportToolData 仍是 Record<string, unknown>；converter.ts 是透传映射，不是 Web 脱敏边界。
- tool-part.tsx 没有 Web 专用 route/renderer；thread.aui.tsx 的 GroupedParts 当前只配置 reasoning，未按 presentation.surface 分组工具。
- DetailsTool、diff 和部分 ToolGroup 样式仍有 border / rounded / card 结构；当前没有验证 ghost/no-border 视觉规则。
- 当前单元测试没有 Web renderer、Web kind、禁止字段或 Transport payload 断言。上一轮记录的选定后端测试为 85 passed、8 failed，失败涉及 fake provider 未覆盖默认 Firecrawl 选择；该基线在实现验收前必须修复，不能带失败通过。

## 3. 官方事实

本轮只采用 assistant-ui 官方页面：

| 官方原则 | 对本方案的约束 |
|---|---|
| [Tool UI](https://www.assistant-ui.com/docs/tools/tool-ui) 的 toolkit renderer 接收 args、result、status，可为后端工具提供 render-only UI，并处理运行中、完成、错误态 | Web search/extract 必须拥有专用 renderer；renderer 不应从模型文本自行推导状态。 |
| [Tool call](https://www.assistant-ui.com/elements/tool-call) 将一次调用的 request/result 放入 disclosure，运行中与 settled 状态由 tool-call part 驱动 | 搜索结果默认收起、抽取只显示静默状态行；展开状态是 UI 临时状态。 |
| [Tool group](https://www.assistant-ui.com/elements/tool-group) 只聚合连续 tool calls；官方 ghost 变体无 border/background，outline 有 card 外框 | trace 工具可进入连续工具组，但本方案必须选 ghost 或等价无外框组合。 |
| [Message Part Grouping](https://www.assistant-ui.com/docs/guides/part-grouping) 推荐 MessagePrimitive.GroupedParts 处理相邻 parts；display: standalone 可使工具 UI 脱离折叠 trace | 分组输入必须是 presentation.surface，不能在 Thread 中按工具名硬编码。 |
| [Assistant Transport](https://www.assistant-ui.com/docs/runtimes/custom/assistant-transport) 是状态快照流；后端发送完整 agent state，converter 映射为 UI state，支持 set / append-text 等操作 | 后端必须先生成安全 snapshot；converter 不承担安全过滤，也不能把 UI state 反写成领域事实。 |

## 4. 逐项反馈关闭矩阵

“方案关闭”只判断最新版方案是否已关闭设计验收项；“代码状态”独立记录当前实现，避免混淆。

| 反馈 | 方案是否关闭 | 方案中的落点、不变量、断言和失败门禁 | 当前代码状态 |
|---|---|---|---|
| F1：缺少 Web contract、kind、renderer route | 是 | §5 定义 web-search-results / web-extract-status；§6.1/6.2 定义 renderer 与 kind-first route；前端 route、后端 kind、Transport snapshot 测试；未知 kind 必须安全 fallback，Web 测试失败不得通过 | 未实现；当前 route 只有 delete/diff/terminal/details/unknown。 |
| F2：web_extract 正文/metadata 进入 UI data | 是 | §11.1 要求后端 UI projection + 字段级 allowlist：web_extract 只允许 provider、sites 及明确站点状态字段，丢弃 content、metadata、raw response；snapshot validator 复用第二道 allowlist；测试断言所有 Web data 不含禁止字段 | 未实现；web_extract.py 当前仍把正文和 metadata 放入 data。 |
| F3：summary.content → event.result → snapshot.result → Transport | 是 | §11.2 明确 content 只进 observe/Runtime context；Web extract completed event.result 为 None 或 UI-safe result；dispatcher/projector/snapshot/Transport payload 逐层断言正文不出现 | 未实现；dispatcher 当前仍使用 result=summary["content"]。 |
| F4：缺少 Web renderer 状态测试 | 是 | §6.1、§10、§11.3 覆盖 loading/success/empty/partial/all-failed/provider error/invalid URL/cancelled/truncated；renderer 只渲染安全字段，未知 kind fallback；相关测试失败即阻断 | 未实现；当前无 Web renderer 和专项断言。 |
| F5：工具 trace、standalone、surface 驱动分组未闭环 | 是 | §6.3、§11.4 规定 trace/standalone 映射、GroupedParts/UI adapter 以 presentation.surface 为唯一输入、standalone 不进入 trace、用户 open state 不被 snapshot 重置 | 未实现；Thread 目前只按 reasoning 分组。 |
| F6：边框/card 视觉规则未落地 | 是 | §7 规定根节点无 border/外层 rounded/card，列表用间距或弱内部 divider，diff/terminal 只保留可读性样式；§10 前端测试断言无外层 border/card class；ToolGroup 必须 ghost/等价无框 | 未实现；Details、diff、ToolGroup 仍可见 border/rounded。 |
| F7：fake provider 基线失败 | 是 | §11.5 要求 fake provider 通过与生产一致的注册/注入入口覆盖 Web 工具；Web tool、summary、dispatcher、projector、snapshot contract 全通过；任何 fixture/config 失败均 FAIL | 部分已有注入基础，尚无新 UI/Transport 基线；上一轮记录仍有 8 个失败。 |
| R1：只靠 renderer 不渲染正文不足 | 是 | §11.2 把模型通道与 UI 通道分离，并要求 Transport payload 级负向断言；renderer 的“不渲染”只是第二道防线 | 当前仍有正文透传风险。 |
| R2：预算截断不是字段安全 | 是 | §11.1 要求 projection 先于 DisplayDataBudget，allowlist 负责安全，预算只负责大小；禁止字段丢弃需有诊断日志且不得记录正文 | 当前 DisplayDataBudget 仅递归截断，未做 allowlist。 |
| R3：pending/running/错误时序不稳定 | 是 | §11.3 明确 created/pending、running、completed、partial、all-failed、cancelled、invalid URL/provider error 的 status-only 语义；CreatedEvent.data、状态事件、snapshot.data、artifact.data 保持同一 kind；Transport 序列化测试覆盖全过程 | 当前 CreatedEvent 无 data，pending/running Web 目标未形成安全结构。 |
| R4：宽泛 Record 允许 provider 字段穿透 | 是 | §11.1 要求后端 discriminated union/equivalent Pydantic allowlist，snapshot validator 复用；§6.2 以稳定 kind 路由，未知 kind 只能 fallback，不得将未知 Web result 当正文展示 | 当前后端 snapshot 与前端 contract 仍宽泛。 |
| R5：fake provider 配置问题可能掩盖回归 | 是 | §11.5 将生产选择逻辑、fake 注册入口和失败门禁写死；不得以 fixture/config 问题带失败进入验收 | 当前历史基线仍需修复，不能宣称 Web 回归通过。 |

### 时序专项判定

方案已明确以下不变量：

    created/pending  : 同一 kind，按已校验 args 建站点，site.status=pending
    running          : 同一 kind，site.status=running，不含正文
    completed        : 同一 kind，逐站 success/truncated，不含正文
    partial          : completed + 成功/失败站点并存
    all-failed       : failed + 失败站点（无站点身份时仅总体安全状态）
    cancelled        : cancelled + 已知站点保留安全状态，不含正文
    invalid-url      : failed + invalid_url 状态/错误码，不发起网络请求

因此，方案层的状态覆盖是闭合的；实现时必须把上述语义落实成唯一 JSON 字段名、枚举和值约束，不能只凭文字或 renderer 猜测。

## 5. 剩余风险

以下不是把方案判为 FAIL 的理由，而是实施时必须消除的风险：

1. §11.1 的 projection 文件名使用“例如”表述；实施时应在 apps/backend/app/core/tools/tool_execute/ 选定唯一模块，并让所有 Web observation 经过该入口，禁止 handler/dispatcher 各自复制 allowlist。
2. event.error / snapshot.error 也要限制为短、可展示的错误摘要；不能只过滤 data 而让原始 provider 异常从错误字段进入 UI。
3. web-search-results 的 empty/provider-error 状态、web-extract-status 的站点字段名和长度限制必须在代码 schema 中冻结；不能同时保留多套命名。
4. GroupedParts 访问 artifact 的版本差异必须在 UI 边界用一个确定的 adapter 解决；不能把“暂时 flat rows”当作最终分组实现。
5. 搜索结果链接的点击应只打开已返回 URL，不触发前端重新抓取；建议加入组件/E2E 断言。该项在方案中已有行为原则，但测试断言仍需具体化。

## 6. 实现前置门禁

下列门禁全部满足后，才可进行“实现验收”；任一失败均不得宣布改造完成：

- 后端建立正式 Web UI projection 和字段级 allowlist；web_extract 的 data、event.result、snapshot.result、snapshot.data、Transport payload 均无正文、metadata、raw response。
- web_search / web_extract 的 kind、字段名、状态枚举和错误摘要长度有后端 schema；snapshot validator 与 projection 使用同一安全规则。
- created/pending/running/completed/partial/all-failed/cancelled/invalid-url/provider-error/empty 的状态时序均有测试，且相同调用的 kind 与站点顺序稳定。
- dispatcher 的 completed Web extract result 不再来自 summary.content；模型正文仍能通过 observe 写入 RuntimeContext，且不进入 UI snapshot。
- fake provider 通过生产一致的注册/选择入口；Web tool、summary、dispatcher、projector、snapshot、Transport payload 测试全绿，不允许带 8 个历史失败进入验收。
- 前端新增 Web search/extract renderer；tool-part.tsx 按 kind 路由；converter 只透传后端已治理数据；即使注入意外 content，DOM 也不渲染正文。
- thread.aui.tsx 的 GroupedParts/adapter 以 presentation.surface 分组；trace 默认收起，standalone 不被吞入；open state 不因 snapshot 更新重置。
- renderer、列表、diff、terminal、ToolGroup 满足 quiet/no-border 规则；至少有组件断言，最好补一条浏览器 DOM/E2E 断言。
- Tauri 本地生命周期保持现状：loopback-only、动态端口、health/readiness、关闭时进程树清理、崩溃恢复和失败可见；前端重连后事实从后端 snapshot 恢复，展开态丢失可接受。

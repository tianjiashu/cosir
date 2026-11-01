# apps/desktop 代码与架构精简审查

- 标准：`skills/code-simplification/references/simplification-standard.md` 1.0；项目第零铁律优先。
- 范围：用户指定 `apps/desktop`，624 个现存、受跟踪或未被忽略的文件；必要时追到后端契约对端。
- 审查基准：HEAD `5d1136b6ce5209bd2670399aa662402e5ee15231` 加开始时已有的未提交修改。
- 上一轮：`.simplification/reports/2026-11-01.md`，基准 `60b267518128c70a60236ebe23c1845dcea66f44`。旧计划的结论均须以当前源码重新核对。
- 本次是**只审查**：未修改业务代码、测试、检测器或项目配置，未切分支、暂存或提交。只新增本报告；不更新执行轮的基准记录。
- 范围内先做静态扫描，再沿宿主、传输状态、组件与编辑器三个方向重点深读；不是逐行覆盖全部文件，也不声称穷尽所有问题。

## 结论与推荐顺序

最值得处理的不是缩短组件或合并所有相似写法，而是明确生命周期所有权、消除无意义状态更新、集中重复业务规则。

1. **单独修复三个已复现的生命周期问题**：宿主连接内的请求登记、浏览器创建中的资源归属、HTTP 超时资源释放。它们会改变现有行为，列为 L3，不混进等价精简。
2. **优先实施四项 L2 精简**：终端同步真正 no-op、共享 Agent 配置面板归位、Team 节点 ID 解析规则收敛、剩余共享 query options 收敛。
3. 死导出、恒定参数和必填契约升级后的空分支收益较小，可随后处理。双重超时须先证明取消与日志语义等价。配置文本表单的统一暂缓。

所有下列“处理”均是建议；本轮没有执行变换。

## 项目画像与边界

沿用 `.simplification/project-profile.md`，并以当前 `AGENTS.md`、`apps/desktop/AGENTS.md` 与源码核对：

| 维度 | 本次判断与依据 |
|---|---|
| 产品形态 | 单用户本机桌面 Agent，非发布给未知调用方的库。来源：根 AGENTS 产品形态。 |
| 运行拓扑 | Electron main、React 渲染进程、FastAPI 子进程；HTTP/SSE/IPC 是本机进程边界。来源：根 AGENTS 与 desktop AGENTS。 |
| 信任边界 | IPC/host-link 输入、后端响应、网页、文件和持久化偏好。内部具体类型间的调用不另设防御。来源：根 AGENTS、ipc.ts、HTTP/Transport 入口。 |
| 生命周期 | 绿地，无兼容负担；删除旧路径仍须排除测试、构建入口和跨进程消费者。来源：根 AGENTS。 |
| 外部观察者 | 用户界面、后端协议对端、结构化诊断日志。超时、取消、清理、错误分类与日志均受保护。来源：根 AGENTS 和精简标准 §3。 |
| 质量要求 | 长期可维护性、正确性、可排查性；资源清理和 generation 隔离不能为减少代码而删除。来源：第零铁律及生命周期边界。 |
| 规模与协作 | 本轮 624 个现存范围文件；工作区已有用户修改，因此全范围只读。团队人数不作为本轮路线依据。 |
| 项目规范 | 根/desktop AGENTS；采用具体类型、最近公共归属、不新增备用接口或通用管理框架。 |

## 基线与验证证据

| 检查 | 结果 |
|---|---|
| 渲染进程类型检查 | `tsc --noEmit --incremental false -p .` 通过。 |
| 宿主类型检查 | `tsc --noEmit --incremental false -p electron` 通过。 |
| 单元测试 | `npm run test:unit`：105 文件、665 用例全部通过。第一次受沙箱禁止本机端口监听影响，放宽执行环境后重跑通过。 |
| Lint | `npm run lint`：49 个 `no-undef`，集中在 Node 脚本和桌面 E2E 的 Node 全局量；检查失败，未修改配置。与上一轮记录相比不再有那 1 条未使用变量错误。 |
| 精简检测器 | 通过 `.simplification/scripts/run-all.mjs --scope … --out …` 统一运行：knip 21、tsc-unused 0、eslint-defense 31、catch-swallow 15、dir-names 0，共 67 个原始候选。原始命中不是 67 个已确认问题。 |
| 只读定向复现 | 两个宿主异步交错、终端重复通知、HTTP 超时未释放，主 Agent 审查复现脚本并重新运行，结果见下文。 |
| 构建、前端/桌面 E2E | 本轮未运行；没有代码改动，不据此声明实施验收通过。 |
| 工作区完整性 | 对开始时 624 个范围文件逐个 SHA-256 比较，审查完成时无变化。 |

临时证据目录：`/tmp/cosir-desktop-review-20261101/`，含 `scope.json`、`manifest-before.json`、`status-before.txt`、`candidates.json`、检查日志及两个只读复现脚本。关键结果同时保存在本报告，不依赖临时文件长期存在。

Lint 基线不满足 skill 的完整执行轮前提。本次完成只读审查，不据此进入变换或宣称 G0–G7 验收通过；未来实施前应先明确并修复检查基线。

## L3：结构性缺陷，只报告

### R1：请求身份是连接内唯一，inflight 登记却跨连接

- **现象**：重连后旧请求完成，会删掉新连接同号请求的取消登记；后续取消无法中止新 handler。
- **证据**：`electron/main/host-link/client.ts:34` 的 Map 属于客户端；`:83-94` 断开只 abort/clear，不等待旧 handler；`:127` 新调用按数字 ID 写入；`:140-141` 旧 finally 无条件删除同 ID。后端 `apps/backend/app/tools/host/host_link.py:42-47` 每条连接从 1 重新编号，`:185-196` 新连接立即替换旧连接。
- **可达交错**：旧 id=1 正在 await → 换连接 → 新 id=1 开始 → 旧 handler finally → 新 id=1 收到取消。snapshot/screenshot 的 CDP 等待不消费 handler signal，旧调用可晚于重连结束。
- **复现**：`host-interleavings.cjs` 输出 `oldCallAborted=true`、`newCallAbortedAfterCancel=false`。
- **根因层级/置信度**：连接生命周期与身份契约，**高**。不是单个 cancel 分支写错。
- **两条路线**：删除前比较 controller 身份可堵住该反例；但接收旧 socket 消息、取消、断开、finally 仍共享跨连接 Map。推荐让 socket 与 inflight 归同一连接上下文，各回调捕获所属上下文，不新增基类或工厂。
- **同类点**：连接切换、同 generation 重连、取消查找、旧消息接收、旧 finally、断开释放；未来修复在连接所有者一处处理。
- **影响与验证**：host-link 客户端内部结构；wire 不必改变；有意修复取消行为。补旧调用迟到收尾、新连接复用 ID、旧 socket 帧、断开释放测试。后端连接级 `_pending` 已隔离，不需同样改造。
- **待验证**：真实 Electron 重连时序未跑 E2E；确定性交错已经在当前客户端源码复现。建议优先级 P2。

### R2：浏览器标签创建期间，两份登记不同步

- **现象**：创建中列标签会读到尚不存在的 BrowserTab；创建中删除 Task 后，迟到的创建仍写入 `tabs`，出现已删除 Task 的孤立标签。
- **证据**：`electron/main/browser/browser-host.ts:168-175` 先 `table.add`，await 创建后才 `tabs.set`；`tab-table.ts:31-36` 同时将它设为当前标签；`browser-tab.ts:110-116` 创建有 loadURL 和两次 CDP 等待。`browser-host.ts:92-93`、`:208-209` 假定两表一致，`:99-106` 和 `:226-234` 清理拿不到创建中的对象。
- **复现**：`userOpen` 悬挂创建 → list → `dropTasks` → 创建完成。输出：list 抛 `Cannot read properties of undefined (reading 'summary')`；open 返回 `TAB_NOT_FOUND`；`tabsAfterTaskDropped` 仍有 t1；`createdViewClosed=false`。
- **生产入口**：`electron/main/ipc.ts:47-51` 用户开标签与 `browser/methods.ts:21-22` Task 删除通知可并发；host-link `client.ts:115` 不串行等待 handler。
- **根因层级/置信度**：创建、就绪和销毁的资源所有权未统一，**高**。
- **两条路线**：加可选链只隐藏 list 异常；将 `table.add` 移到 await 后又会失去并发上限预占。推荐一处资源登记拥有标签对象与创建状态；创建中仍计入上限，读取有明确就绪语义，删除覆盖创建中对象并阻止迟到发布，初始化失败由同一所有者释放。
- **同类点**：`manageTabs`、`tabFor`、`dropTasks`、`closeAll`、创建失败及 popup 创建都依赖同一生命周期；未来修复应一起纳入根因边界，不分别打补丁。
- **影响与验证**：BrowserHost/BrowserTab/TabTable 内部契约；有意修复删除后复活与不可用标签访问。补创建中 list、Task 删除、closeAll、初始化失败、并发上限测试；再跑 browser desktop E2E。
- **待验证**：本次替身只控制 BrowserTab 创建的异步完成，没有声称复现真实 Chromium 资源泄漏。源码层孤立登记与未调用 close 已确定。建议优先级 P2。

### R3：requestRaw 创建超时，却未释放

- **现象**：HTTP 已成功返回，定时器仍存在，到期继续 abort 已返回响应所用 signal。
- **证据**：`lib/http/client.ts:39-67` 创建 `withTimeout` 后无 finally/clear；`lib/async/abort-timeout.ts:48-59` 的 clear 还负责移除 owner listener。与 `client.ts:10-12`“requestRaw/requestOk 超时只到响应头”的注释契约不符。
- **复现**：`state-review-probes.cjs` 输出 `pendingAfterSuccess=1`、`signalAbortedAfterSuccess=true`、`abortReason=TimeoutError`。
- **同类生产入口**：`lib/assistant/task-commands.ts:142` 停止 Task、`:171-174` 取消工具、`:210-213` 转后台；它们都给 requestOk 传超时。普通 requestJson 在 `client.ts:93-101` 自行创建并在 finally 清理；图片上传、模型/MCP 测试走后者，不受该遗漏影响。
- **根因层级/置信度**：HTTP 请求边界的资源生命周期，**高**。带 owner signal 的路径还会遗留 listener；当前上述三个生产入口未传 owner signal，不扩大描述为已经发生的监听器累积。
- **两条路线**：让各命令自行清理需要暴露下层实现且多处同步；推荐资源创建者 requestRaw 在完成/失败后对称释放，继续保留网络失败日志。
- **影响与验证**：有意消除晚到 abort，属于修复；补成功、网络失败、主动取消、超时、慢错误响应体的测试。若希望超时覆盖响应体，应另明确消费方契约，不能随手删计时器当等价精简。建议优先级 P2。

## L2：优先精简候选

### S1：终端同步真正无变化时保留原 state（M9）

- `lib/terminal/terminal-workbench-store.ts:80-83` 的 update 永远复制对象；`:97-98` 的 `return {}` 不能跳过这层复制。
- `components/assistant/runtime/assistant-runtime-session.tsx:146-153` 每次 Transport 提交同步终端镜像；`components/terminal/terminal-workbench.tsx:45` 订阅整个 Task 桶。流式文本变化也能触发终端无意义通知和重渲染。
- 复现：同一 sessions 引用再次同步，`sameSessionsReference=true`、`sameTaskReference=false`、`notifications=1`。
- **根因/置信度**：局部更新辅助函数不表达 no-op，**高**；现有 store 的归属合理，不需要重建状态架构。
- **推荐**：在同步边界直接用函数式 set 返回原 state，或让该辅助函数明确支持 no-op。给组件加 memo 只处理症状，新增缓存更不合适。
- **等价与影响**：只减少同引用同步的对象分配/通知；真实 session 更新、选中回落、已阅标记、焦点和自动创建不变。没有外部 wire、数据格式或日志变化。
- **同类排除**：`lib/background-tasks/store.ts:35-36`、`lib/handoff/store.ts:24-26` 已真正跳过同引用更新；null 清空路径也已有 no-op。
- **验证**：新增通知次数/引用保持测试，保留既有 terminal-workbench-store 单测，并运行终端工作台 E2E。预计 E8 减少 1 个高频冗余执行点；不把通知次数当业务信号。

### S2：共享 Agent 配置面板归回功能目录（S1/S2/S6）

- `components/system-configuration-page.tsx:71-83` 定义共享适配契约，`:103-248` 拥有 Agent 列表与编辑流程；`components/workspace-configuration-page.tsx:8` 从另一个页面导入，`:95` 使用。
- **根因/置信度**：页面装配与共享功能混放，**高**。这是上一轮计划 #45 的未完成部分，当前仍成立。
- **推荐**：将共享 Panel、私有 Editor 与关联类型移到已有 `components/configuration/`，两页并列依赖；作用域 API 装配留页面。仅加 re-export 会保留错误归属与跳转。
- **等价与影响**：保持组件挂载身份、会话 useState 位置、查询失效完成后再关闭的顺序、未保存守卫；不改变系统/工作区作用域语义，不重写全部配置 API。
- **验证**：类型检查、构建、系统/工作区创建编辑删除取消与切换路由 E2E；移动与行为修改分离。预计页面间反向依赖 1 条变为 0，功能逻辑归属清楚。

### S3：集中 Team 草稿节点 ID 的唯一解析规则（M1/M9）

- 同类点：`components/agent-team/configuration-form.tsx:30-36`、`:204`；`components/agent-team/graph-model.ts:58-67`、`:129-130`、`:143-147`；`lib/agent-team-editor/model.ts:46`。
- 这些位置都区分不存在、唯一、重复的业务 ID，图投影还逐边重复扫描节点。删除最后一个同名节点才删引用（model.ts:61）是另一条规则，不能机械合并。
- **根因/置信度**：同一领域知识分散表达，**高**；抽取形状还需用行为测试约束。
- **推荐**：在 `lib/agent-team-editor/` 放当前草稿的纯索引/解析函数，在一次图投影内复用；不持久缓存，不增加通用图框架。逐处把 filter 改成 find 会丢失歧义语义，不可采用。
- **等价与影响**：重复/空 ID 草稿仍可编辑，missing/duplicate 端点仍可见，顺序、editorId、END 目标特殊语义不变；不在前端重写后端图校验。
- **验证**：现有 graph-model、editor-session 和 agent-team-session E2E 已覆盖部分歧义场景；须先补表单大纲/dangling 分组特征测试。预计规则的三类消费方依赖同一解析定义；不宣称整个图算法复杂度已降到线性。

### S4：复用已有 queryOptions 收敛剩余共享资源（M1）

- Skills：`components/configuration/skill-configuration-panel.tsx:48-50` 与 `components/composer/skills-selector.tsx:28-31`。
- Model configs：`components/model/model-config-manager.tsx:117-119` 与 `lib/model/catalog.ts:61-64`。
- **根因/置信度**：同一资源 query key + queryFn 各写两处，**高**；当前函数相同，未发现已经取错数据。
- **推荐**：沿用 `lib/query/query-options.ts` 的现有模式，每个资源定义一对查询键和函数；无需另造查询框架。
- **等价与影响**：调用方继续拥有 `enabled: open`、`select: buildModelCatalog`、mutation 与失效策略，保持 signal 传递。toolGroups、agents、fileInfo 已收敛；单消费者查询不为统一风格迁移。
- **验证**：资源请求参数与 signal、缓存失效、Skill 选择器开关、模型配置刷新测试；预计 E2 各从 2 个表达点降至 1 个。

## 其他候选与暂缓项

| 等级/条款 | 位置与证据 | 路线、等价边界与取舍 |
|---|---|---|
| L1 / D6 | `components/assistant-ui/tools/call-row.tsx:47-48` | body 刚经 `?? null`，随后的 `body !== undefined` 恒真，可删除；保留 null/false 的不展开语义。仅为局部小收益，本轮未执行。 |
| L2 / D2 | knip 21 条中的 18 个仅文件内使用的导出、1 个无消费者 E2E_SLOT | 全仓符号搜索与入口核对后，18 个可去 export，不能删内部实现；E2E_SLOT 仅定义可删。按导出边界保守列 L2；无业务行为收益，降低不必要的模块表面积。 |
| L2 / D3 | `electron/main/browser/browser-tab.ts:188` 的 format；生产调用 `browser-host.ts:82,141` 都是 jpeg | 固定 JPEG 并删参数；保留不同 quality、fullPage、devicePixels、CDP 参数与 data URL。类型检查与截图桌面 E2E验证。收益低，不扩展为截图 API 改造。 |
| L2 / D3 | `scripts/cargo-target.mjs:14-19` 的 extraEnvironment；同文件 :42、stage-backend.mjs:87、stage-terminal-worker.mjs:34 全部无参 | 删除未使用扩展参数与 spread；每次读取 process.env、CARGO_TARGET_DIR 覆盖顺序不变。验证环境输出及构建入口。收益低。 |
| L2 / F1 | `components/assistant/runtime/use-runtime-transport.ts:79,138`；`use-runtime-diagnostics.ts:23,41` | workspaceId 在 `runtime-types.ts:11,31` 及所有生产调用链已经必填 number；附件 adapter 的 null 路径及对应日志回落可删除。只处理这条内部链；新对话模型选择的可选 scope 真实存在，应保留。须保留日志字段与附件行为测试。 |
| L3 / F6，等价性待证 | `lib/assistant/canonical-snapshot.ts:46,51-54` → `assistant-snapshot-client.ts:16` → `lib/http/client.ts:93-100` | 同次读取创建两个同期限计时器，事实确定。推荐 HTTP 只负责截止时间，canonical 保留 generation、主动取消收敛和失败日志。不能只删 timeoutMs 传参（会恢复内层默认 30 秒，仍双计时）。现有 canonical 单测 mock 掉 HTTP 链；先验证超时/主动取消的先后顺序、timedOut 日志不变，证明后才转 L2。 |
| 暂缓 / M3 | Markdown hook、workspace fileignore、terminal denylist 的文本草稿机制 | 前两者保存中仍可编辑、按 submitted 清草稿；denylist 禁输且有 patterns 转换。最多共享小型草稿状态机制，不统一完整保存流程。当前代码短且清楚，抽象增加跳转，收益不如 S1–S4。 |

18 个仅文件内使用的导出清单：

- `components/assistant-ui/tools/call-view-registry.ts:16`：CALL_VIEWS。
- `lib/assistant/turn-layout.ts:32-113`：CHANGES_KIND、STANDALONE_ARTIFACT_KINDS、StandaloneArtifactKind、NarrationItem、ReasoningItem、VerbCount、ChangedFileStatus、AttentionItem、AnswerItem、callVerb。
- `components/assistant-ui/tools/views/file-changes-view.tsx:37`：parseFileDiff。
- `components/assistant-ui/turn/reasoning-item.tsx:13`：reasoningPreview。
- `components/assistant-ui/turn/turn.tsx:32`：turnPhaseOf。
- `lib/links/resolve-link.ts:15,18`：WebLink、AnchorLink。
- `components/workspace-references/use-open-workspace-path.ts:13,26`：OpenFileOptions、OpenFileOutcome。

另有 `tests/e2e/e2e-env.ts:12` E2E_SLOT 无消费者。18 个符号中类型被同文件公共类型引用不意味着类型名必须导出；移除 export 不移除这些内部类型。

## 排除的误判与刻意不做

- `tests/desktop-e2e/browser-harness/main.ts` 是 `tests/desktop-e2e/global-setup.mjs:19` 的 esbuild 动态入口，knip 假死；应修检测入口配置，不能删文件。`components/ui/dialog.tsx` 的 DialogTrigger 延续上一轮的生成组件 API 保留决定，本轮不擅自缩减。
- `browser-tab.ts:132` 吞 loadURL promise 的拒绝，是因为同次 waitForLoad 已统一处理加载事件；删 catch 会新增未处理拒绝。popup 导航失败可能真实发生，不作为等价删除。
- supervisor 的异步 spawnError、草稿恢复 effect 的 active 标志可在 await 期间变化；静态类型命中不证明不可达。generation、防旧请求、退出强杀、有限重试各有不同职责。
- JSON.stringify 可能没有字符串结果；未知 wire/第三方输入的默认值、持久化偏好读失败、日志失败不阻断主路径等不按静态类型一概删除。31 条 F1、15 条 F4 未全部变成修改清单。
- Terminal、Handoff、BackgroundTasks、TaskStatus 的镜像均是明确的只读投影，不因存了状态就认定为第二事实源。React Flow 节点还保存拖动/测量状态，不能直接删除交互镜像。
- 不将 Agent 与 Team 编辑会话合成通用会话：身份、历史、布局与失败语义不同。不调整 HTTP/SSE、宿主 IPC 或后端持久化格式。不做全目录搬家、格式化或批量删注释。
- 文档漂移：`requestRaw` 的“仅到响应头”说明与实现不符，已归 R3；`projectCargoEnvironment` 的扩展参数是预留能力，不是现有调用事实。修复/删除时同步注释，本轮只记录。

## 实施约束与收益评价

- 建议将 R1/R2/R3 按修复任务处理，各先定终态、补复现测试，再实施；不要把它们包装成“行为不变”。
- S1–S4 可分别形成独立、可验证的变换。F 类、合并类和生命周期变换在实施与测试后仍需独立复核；本次并行调查不替代实施后的独立复核。
- 开始时已有修改的文件继续只读；本次没有决定任何分支/提交策略。实施前应按用户届时授权及 skill 关卡确认具体范围。
- 本轮生产/测试代码净修改均为 0，未增加依赖。E2/E6/E8 的预期收益写在各候选中；E5 重复率、E7 全范围复杂度未测，不编造数值。
- **评级：审查完成，尚无已实施收益，不进行精简执行评级或 G0–G7 验收。**
- skill 流程建议（不修改 skill）：明确“仅审查”模式，允许保留当前工作区只读证据而不创建分支/快照提交；将方案保存在 plans，避免未执行的审查推进下一轮变更基准。既有检测器新增能力另行变更，不在本轮审查中夹带。

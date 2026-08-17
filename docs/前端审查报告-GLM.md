# 前端代码审查报告（apps/desktop）
5 个子 Agent 并行审查完成，覆盖 stores/services、hooks、布局与面板组件、聊天组件与 lib、页面/入口/配置（含 tsc/eslint/vitest 实测）。共发现 5 严重、6 高、22 中、15 低 。以下按严重级别汇总（重复发现已合并）。

## 严重（当前已破坏功能，必须立即修）

**复核状态总览（2026-08-17）**：依据当前代码事实，3 个并行核查子 Agent 逐项复核（只读）。标记含义：✅ 已修复（修复方式正确且无残留）｜❌ 仍存在（代码现状与报告描述一致）。

| 项 | 问题 | 状态 |
|---|---|---|
| S1 | CodeBlock 缺 `useState` 导入，渲染代码块即整界面崩溃 | ✅ 已修复 |
| S2 | `.ts` 测试文件含 JSX，build/test/lint 三条链路全断 | ✅ 已修复 |
| S3 | RightPanel Sources/Subagent 标签点击无效 | ✅ 已修复 |
| S4 | 启动恢复与 Sidebar 惰性加载竞态，首屏任务静默恢复失败 | ❌ 仍存在 |
| S5 | 日志页 trace 模式整列表 key 重复 | ✅ 已修复 |

**小结**：5 项中已修复 4 项，仅 S4 残留（启动恢复竞态，与 H2 跨实例竞态防护分裂同根）。详见下方各 S 项逐项状态与证据。

S1. CodeBlock 缺少 useState 导入——渲染任何代码块即整界面崩溃 CodeBlock.tsx:11 只导入 useMemo ， 第 67 行 却调用 useState(false) （tsc 实测复现 TS2304 ）。Agent 回复中任何 markdown 代码块都会触发 ReferenceError → ErrorBoundary 接管 → 聊天功能完全不可用。 这是工作区未提交改动引入的 （git diff 确认 HEAD 版本正常，重构时误删）。修复：改为 import { useMemo, useState } from "react" 。
- **复核状态（2026-08-17）：✅ 已修复** — `CodeBlock.tsx:11` 现为 `import { useMemo, useState } from "react"`；`useState(false)` 调用在 `:67`（`const [expanded, setExpanded] = useState(false)`），`useMemo` 在 `:71`/`:78` 正常，ReferenceError 崩溃已消除。

S2. .ts 测试文件含 JSX——build / test / lint 三条链路全部中断 review.clientDisconnectedNoUI.test.ts:93 在 .ts 文件中写 render(<StatusBadge ... />) ，实测 tsc -b 、vitest、eslint 全部失败。 更隐蔽的危害 ：tsc 遇语法错误会跳过全部语义诊断，S1 的 useState 错误正是因此被掩盖。修复：重命名为 .test.tsx （1 分钟解锁三条链路）。
- **复核状态（2026-08-17）：✅ 已修复** — JSX 测试已改名 `review.clientDisconnectedNoUI.test.tsx`（`:99` `render(<StatusBadge {...props} />)`）；`apps/desktop/src/tests/` 92 个测试文件中已无 `.ts` 残留 JSX（`renderHook` 导入与字符串字面量内的 `<script>` 均安全），`tsc -b` 不再因 JSX 语法中断、不再掩盖语义诊断。

S3. RightPanel：Sources 标签页永远无法进入，Subagent 标签页点击无效 RightPanel.tsx:83-89 受控 Tabs 的 activeTab 只由 selectedChildTurnId 派生（ subagent : outputs ），点击 Sources/Subagent trigger 只调 clearSelection() ，value 不变，Radix 不切换。Sources 内容（ContextBlock/McpBlock）用户永远看不到。修复：增加本地 manualTab state 记录手动选择。
- **复核状态（2026-08-17）：✅ 已修复** — `RightPanel.tsx:89-91` 新增本地 `activeTab` state（初值按 `selectedChildTurnId ? "subagent" : "outputs"` 派生）；`:96-100` 派生同步仅在选中态为真时切到 subagent，清空选中态不再强制拉回 outputs；`:114-116` `Tabs value={activeTab} onValueChange={handleTabChange}`；`:105-110` `handleTabChange` 写 state（`value !== "subagent"` 时 `clearSelection()`）；`:127` Sources trigger + `:151-163` Sources 内容区（`SourcesTab`/`ContextBlock`/`McpBlock`）可达。

S4. 启动恢复与 Sidebar 惰性加载必然竞态，首屏任务静默恢复失败 useStartupTaskResume.ts:47-72 把 loadWorkspaceTasks 返回 false （含「在途跳过」语义）一律当失败短路。React commit 顺序中 Sidebar 的 ensureLoaded 先发起请求入 inflightLoads ，App 的 resume 命中去重返回 false → 首屏不自动打开会话，且 无任何日志 。修复：不以返回值短路，直接读 store；或 inflightLoads 存共享 Promise。
- **复核状态（2026-08-17）：❌ 仍存在** — `useStartupTaskResume.ts:54-57` 仍 `const loaded = await loadWorkspaceTasks(workspaceId); if (!loaded) { return; }` 静默短路；`loadWorkspaceTasks`（`useWorkspaceTaskLazyLoad.ts:74-96`）把「已加载 / 在途去重 / 加载失败」三种语义全归并为返回 `false`（`:75-77` 已加载、`:78-80` 在途、失败在 `:89` 记日志），未返回共享 inflight Promise。Sidebar 先发起加载时 resume 命中 inflight 去重即静默跳过，首屏任务不自动打开且无日志。持久化路径 `openTask(persistedId)`（`:124-136`）不受影响；`resumeDefaultTask` 默认回退（`:153-155`）仍踩坑。**待修**：`loadWorkspaceTasks` 返回共享 inflight Promise 或改读 store 判定，resume 不以 `false` 短路并补日志。

S5. 日志页 trace 模式整列表 key 重复 LogsPage.tsx:251 getKey 用 entry.trace_id || ... ，按 trace 查询时 50 条 key 完全相同，违反 VirtualList 自身契约，翻页时卡片折叠态可能串位。修复：key 改为 trace_id + "::" + event_id 。
- **复核状态（2026-08-17）：✅ 已修复** — `LogsPage.tsx:255` `getKey` 现为 `` `${entry.trace_id ?? "no-trace"}::${entry.ts}::${index}` ``（trace_id+ts+index 组合，index 兜底），trace 查询下每行 key 唯一；`virtual/VirtualList.tsx:154` `key={getKey(...)}` 直接作渲染 key，符合 VirtualList「内在字段+下标」契约（`:14-16` 注释）。回归测试 `logs.getkey.m4.test.tsx` 锁死（`:30-31` 与源码逐字符同公式、`:54-71` 同 trace_id+ts 生成 5 个唯一 key、`:93-104` 纯 trace_id 反例）。

## 高
H1. prepareWorkspace 未覆盖 30s 超时，大仓库准备「必然失败」且不自愈 api.ts:295-299 走默认 30s 超时（注释自我预警「大仓库可达数分钟」却未落实），超时后 workspaceEventStore.ts:152-160 置 error 并 断开 SSE → 后端数分钟后索引成功的事件永远无法送达，UI 永久显示失败。修复： timeout: false + prepare 失败时不断开事件流。

H2. useTask/useSSE 被 4 处独立实例化，竞态防护全面分裂（架构缺陷）

- 跨实例 openTask 覆盖用户选择：启动恢复慢返回时把用户刚切换的任务「弹回」旧任务（ useTask.ts:289-324 的 seq 守卫是实例级的）；
- 僵尸 SSE 连接：NewTaskPage 建立的连接在组件卸载后 无人能 disconnect ，持续接收推送直至自然结束（ useSSE.ts:98-99 ）。
  修复：SSE 连接与 openTask 序号提升为模块级单例。
H3. 切换任务时 checkpoint 不重置，旧检查点过滤新任务变更集 useChanges.ts:152-162 taskId effect 只 refresh() 不清 checkpoint（两个子 Agent 独立发现）。任务 B 显示假空态「暂无文件变更」；且新响应到达前 changeSet 仍是旧任务数据，用户可能在错误上下文执行撤销。修复：taskId 变化时 setCheckpointState(null) + 清空旧 changeSet。

H4. 日志查询无过期响应防护（两个子 Agent 独立发现） LogsPage.tsx:85-121 无 seq/AbortController；LogStatsBar 按钮与 Enter 均不受 isLoading 限制。快速切换筛选时旧结果覆盖新结果且 UI 高亮错配。修复：请求序号守卫 + 按钮/回车 loading 防护。

H5. logger 脱敏对循环引用直接抛异常，日志反而成为崩溃源 logger.ts:144 JSON.parse(JSON.stringify(obj)) 遇循环引用抛 TypeError ，整条链路无 try/catch， logError("...", err, { event: domEvent }) 类调用会反向炸穿业务代码（渲染路径即白屏）。修复： redactContext 包 try/catch 降级，克隆改 structuredClone 。

H6. ESLint 8 个 error，lint 门禁失败（exit 1）

- 3 处 Tailwind 任意值违令（ErrorBoundary min-h-[200px] 、LogEntryCard text-[11px]/[10px] ）；
- 4 处 prefer-const （projector.groupTools.adversarial.test.ts）；
- 1 处 parsing error（即 S2）。 prefer-const 可 --fix 自动修复。
## 中（按主题分组）
### 性能隐患
问题 位置 影响 流式长代码块每 120ms 全量重新高亮+净化，O(n²)，定稿无缓存无上限 CodeBlock.tsx:78 长代码输出掉帧，定稿瞬间冻结数百 ms ChatPanel 每批事件全量扫描重建 childTurnIds，O(N²) ChatPanel.tsx:122-166 长会话流式期间主线程占用线性恶化 eventStore setEvents turn 分片逐事件 merge，打开大任务 O(n²)，且双重排序 eventStore.ts:214-231 打开数千事件任务时 UI 冻结 appendEvent 每事件 3 次 O(n) concat，「O(1) 摊还」注释与实现不符 eventStore.ts:120-133 万级事件后流式 flush 拷贝总量线性增长 useDelegationStreams 每个 rAF 帧对全量事件 O(N) 重算 useDelegationStreams.ts:289 长会话+高频 child 流时掉帧 LogEntryCard 未 memo + renderItem 内联，筛选每按键重渲染全部卡片 LogEntryCard.tsx:84 日志页筛选输入卡顿 ToolCallCard 对所有布局无条件 parseDiff ToolCallCard.tsx:196-203 长文本工具展开时无谓解析

### 内存泄漏
- 删除任务时 turnStore、child task 事件分片不级联清理（ taskStore.ts:265-282 ，delegation child 分片 无任何清理路径 ）；
- conversationTraceStore.traces 无上限追加，每次 HTTP/SSE 请求永久累积（ conversationTraceStore.ts:117 ）；
- SSE fetch 无超时无外部 signal，半开连接永不 settle（ api.ts:332-335 ）。
### 逻辑与状态一致性
- ToolCallGroup 把已取消工具统计为「成功」 （ ToolCallGroup.tsx:52-54 ）；
- 终态轮次永久显示「思考中」 ： isAwaitingFirstToken 未检查 turn status（ TurnTimeline.tsx:208-209 ），用户停止/失败后以为仍在运行；
- connectionState 状态卡死 ：disconnect 后停留 STREAMING、连接失败后停留 CONNECTING（ useSSE.ts:232-239 + sse.ts:150-155 ）；
- openTask 的 loading/error 写入无 seq 守卫，快切任务时 A 的错误提示覆盖 B 的界面；contextUsage 回填在 seq 检查 之前 执行，过期响应污染全局圆环（ useTask.ts:290-357 ）；
- createTask 失败后 activeTaskId 回滚为 null 而非恢复之前任务，且持久化已被清空（ useTask.ts:137-171 ）；
- 视图切换整树卸载：InputBar 草稿清零、Sidebar 删除确认弹窗/进行中状态丢失（ App.tsx:145-225 ）；
- NewTaskPage 两个文案不同的按钮执行 100% 相同的逻辑，功能性误导（ NewTaskPage.tsx:248-263 ）。
### 用户体验
- 「加载更早对话」后视口跳位，无滚动锚定（ ChatPanel.tsx:296-326 ）；
- revert/keep 按钮进行中不禁用，可并发重复提交（ ChangesToolbar.tsx:32-56 ）；
- AgentSelector 网络失败伪装成永久「加载中...」，无重试（ AgentSelector.tsx:87-91 ）；
- 链接点击未接宿主 shell 打开（Rust 侧已授权 shell:allow-open 但前端从未调用），外链在 WebView 内替换整个 UI（ ToolCallCard.tsx:574-580 ）；
- LogFilterBar Enter 未判 isComposing ，中文输入法选词误触发查询（ LogFilterBar.tsx:65-81 ，InputBar 已正确处理）；
- SENSITIVE_PATHS 精确匹配漏掉 refresh_token / access_token 等变体，secret 可能明文落盘（ logger.ts:92-102 ）。
## 低（简列）
渲染期日志刷屏（TurnTimeline thinking_rendered 每 delta 一条、SubagentPanel useMemo 内 logWarn）；死代码（ConversationTraceBlock 空文件、ProjectList/TaskList 无引用、LogsPage scrollRef）；硬编码调色板色未走 token（ChangeFileRow/LogEntryCard/LogDataViewer/WorkspaceEventBadge，深色主题下对比度不足）；InputBar 注释宣称 Shift+Enter 换行但实为单行 <input> ；RightPanel Outputs/Sources 展示无标识的 mock 数据；Sidebar 删除确认弹窗无 Esc/焦点陷阱；打开任务失败静默吞掉； temp-${Date.now()} 同毫秒碰撞；delegation 流一次失败永久拉黑不重连；connect 无条件 reset 圆环导致闪烁； updateTask/updateTurn 重建无关分组引用； removeWorkspace 不清理 collapsedWorkspaceIds ；conversationTraceStore 全文件中文注释乱码（GBK/UTF-8 双重编码）；index.css 注释与值矛盾（宣称冷灰实为纯白）；main.tsx 错误监听注册晚于首渲染。

## 确认无问题的方面（避免误报）
XSS 防护（CodeBlock 经 DOMPurify、react-markdown v10 默认转义）；MarkdownStream 节流/定稿缓存；VirtualList 动态测量与 totalSize 契约；useChanges 的检查点竞态防护（正面范例）；useCopyToClipboard；SSE 重连缓冲与 rAF 攒批；依赖版本无冲突；alias 三处一致；StrictMode 防护基本完备。

## 修复优先级建议
1. 立即 ：S1（一行导入修复，聊天当前不可用）→ S2（改扩展名，恢复类型门禁）→ H6（ eslint --fix + 3 处任意值）
2. 本迭代 ：S3/S4/S5、H1（大仓库必然踩）、H3（用户可感知假空态）、H5（潜在崩溃源）
3. 规划改造 ：H2（SSE/openTask 模块级单例化，一并消除多个中级问题根因）、eventStore 性能三件套、删除路径级联清理
4. 顺手清理 ：死代码、渲染期日志、IME、token 化颜色
总体评价：代码库单实例内的竞态防护和不可变更新质量高于平均（注释详尽、测试覆盖广），但 跨实例/跨模块边界是系统性薄弱点 ，且当前工作区存在一个已破坏聊天功能的未提交缺陷（S1），建议优先处理。
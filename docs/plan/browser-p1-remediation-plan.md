# 内置浏览器 P1 缺陷改造方案（复核后）

日期：2026-11-09
状态：**已确认，按阶段实施**（用户确认「推荐 A，全做」；改动前先定验收方案，改动完成后由独立子 Agent 验收）。复核与方案均为只读/文档工作；生产目录 `git status` 中与本方案相关的浏览器文件为空（工作区另有一批与本方案无关的未提交改动，未触碰）。
依据：四个方向的独立复核（`apps/desktop/temp/p1-verify-a|b|c/`，全部只读、临时测试在 gitignore 的 temp 目录）。

---

## 0. 复核结论：P1 到底是真是假

| 原编号 | 复核结论 | 范围变化 | 最终定级 | 证据 |
| --- | --- | --- | --- | --- |
| P1-1 步骤状态把「确定无效果」判成 `unknown` | **真实缺陷（确认）** | 初判 3 例 → 可达 **8 条**误判路径；另确认 1 条「看起来像、实际正确」的反例（`select` no-option-matched 确实已改页面） | **P1** | 75 条动作×失败路径矩阵（`temp/p1-verify-a/matrix.test.ts`：67 通过 / 8 红）；语义基准 `docs/plan/browser-reliability-refactor-plan.md:121`；模型/UI 文案 `rendering.py:147-150`、`browser-page-view.tsx:55` |
| P1-2 `scroll` 在 body 滚动容器页面假 `SCROLL_END` | **真实缺陷（确认）** | 影响面不只 scroll：同布局下 `fullPage` 截图只截一屏（CDP `cssContentSize` 事实源，独立问题） | **P1**（scroll）+ 单列 P2（fullPage） | 真实 Chromium（Electron 44.7.2）13 种布局探针：html hidden/clip + body auto、标准/怪异模式、移动模拟全部复现，带/不带 ref 都不滚；工具文案 `tool_models/browser_args/actions/page.py:32-34` 明确承诺 |
| P1-3 `browser_logs` 游标读不到在途请求迟到结局 | **部分真实，降级** | 机制成立，但「无路可读」不成立：设计明确指定「省略 since 的默认读取」是提示后的读取路径（P3 文档已记录刻意取舍）；缺的是游标路径与文案没把两种口径说清 | **P2**（文案/契约澄清；不做结构性游标改造） | `tab-log.ts:35-43,180-198`、`docs/plan/embedded-browser-p3-discussion.md:601,605`；11 条游标路径矩阵 `temp/p1-verify-b/tab-log-cursor.test.ts`（18 passed=现状事实） |
| P1-4 `agentBusy` 把用户导航算成 Agent 操作 | **真实缺陷（确认）** | 契约回归：原始实现就是 `agentCalls > 0`（9eb228f2），e1b95010 大重构顺带改义，无文档/测试支持 | **真实；按影响定为 P2**（一行修复，纳入本方案） | `browser-host.ts:250` vs `host-contract.ts:130-132`、`browser-host.ts:82-83`；`temp/p1-verify-b/agent-busy.test.ts` 复现 |
| P1-5 观察失败时回执页面事实失真 | **真实缺陷（确认）** | 5a（act 回执用执行前页头）、5b（navigate 兜底 other_tabs=[]）成立；扩出 C-1（dialog 手写 null）、C-2（缺恢复指引）、C-4（wire 无失败分支样例） | **P1/P2**（5a 中、5b 低） | `browser-host.ts:140,154-158,124`；真实 Electron 探针：回执 head=旧 URL、otherTabs=[]、销毁后读取抛错；临时红测 3 failed / 5 passed |

**扩大范围新增的其它事实（不在 P1 改造范围，记入问题清单/刻意不做）**

- 同一网络请求可被连续两次页面结果重复计数（先 500 再 loadingFailed）：`tab-log.ts` `newFailures` 在 digest 清空后再次加入。低概率，随手可修（P3）。
- `browser_logs` 英文工具描述两处不实：页面结果并不携带游标；「since you last looked」的基准其实是上一次页面结果（P2 文案，随 P1-3 一起修）。
- `fullPage` 截图在 body 滚动容器页面只截一屏（P2，独立，见 §刻意不做）。
- 观察失败时 UI 卡片可能完全看不出失败（display_data 无该事实）；本次不前端改造，记入刻意不做。
- act 的 `head` 在 `operate` 前取，初始化中的标签会记 about:blank（实现时把 `lastKnown` 移到 operate 回调内、动作前即可，见 §2）。

---

## 1. 任务类型

- **包 I（P1-1 + P1-5）**：原类型 **缺陷修复 → 功能改造**。根因在契约/结构：执行事实用单个布尔 `effectStarted` 推断「可能已生效」，回执的页面事实在降级路径手写/复用旧值。按第零铁律必须在事实源处解决，不能逐个失败路径打补丁。
- **包 II（P1-2）**：**缺陷修复**，根因层级 = 局部逻辑（页面内滚动目标解析写死）。
- **包 III（P1-4 + P1-3 文案/重复计数）**：**缺陷修复**（P1-4 局部逻辑 + 契约注释；P1-3 契约文案）。
- 三个包相互独立，可分开验证、分开交付；不共享 wire 形状变更。

---

## 2. 现状 → 终态

### 包 I：执行结算与回执事实（P1-1 + P1-5，含 C-1/C-2/C-4；可选 P2-1）

**现状**

1. 步骤结算：`input/actions.ts:40` 只按 `session.effectStarted` 判 `unknown`；`operation.ts:44-53` 对 `effect()` 内**任意**命令（含只读 `DOM.resolveNode`、只读页面函数）置位；`input/page-functions.ts:58-59` 把只读 `element()` 校验包进 `effect()`。设计文档 `:121` 要求 `failed`=尚未派发/确定未生效、`unknown`=可能已有部分作用，两者是两个事实，不是保守程度。模型文案 `rendering.py:147-150`、UI 文案 `browser-page-view.tsx:55` 都消费这个状态。
2. 回执：`browser-host.ts:140` 在 `operate` 前取 `head`，观察失败时 `:157` 直接复用（动作可能已导航）；`navigate` 兜底 `:124` 手写 `otherTabs: []`、`dialog: null`、缺恢复指引；后端 `rendering.py:154-181` 在 observation=null 时用该页头渲染正文；wire 样例没有失败分支（C-4）。
3. 已派发释放失败会重放（P2-1，`operation.ts:106-111,130-135`）——同属包 I 的「执行事实」，建议随包一并（可裁剪）。

**终态**

1. **effect 事实只表示「可能改变页面/输入状态」**：
   - `operation.ts` 的 `effect()` 契约写清：只包可能改变页面或输入状态的命令；只读命令（`DOM.resolveNode`、`DOM.getBoxModel`、`DOM.getLayoutMetrics`、可读页面函数）不得进入 effect。
   - `input/element.ts` 拆出两个具体助手：`callOn`（只读页面函数，不受 effect 标记）与 `mutateOn`（先只读 resolve 节点，再在 `session.effect()` 内执行 `Runtime.callFunctionOn`）。`scroll`/`select`/`type` 的 clear 改用 `mutateOn`；`element()`/`actionable()`/`isConnected` 等只读检查保持 effect 外。
   - 页面函数结果**证明确定无副作用**时（`SCROLL_AREA` 返回「already at bottom」），动作抛带显式标记的错误 `NoEffectError`（`operation.ts` 中的具体错误类型 + `certainNoEffect(error)` 构造器；单实现、不加接口/工厂）。`runActions` 结算：`NoEffectError` 或未置 effect → `failed`，否则 `unknown`。
   - 保留 `actions.ts:45` 的「unknown 后紧接答弹窗」语义（`effectStarted` 仍由真正的 `callFunctionOn`/输入命令置位）。
2. **回执页面事实**：
   - `browser-host.ts` 增加 `headOr(tab, lastKnown)`：标签存活 → 读当前 head；已销毁/读取抛错（销毁与回执同刻竞争）→ 退回 `lastKnown`。`act` 在 `operate` 回调内、动作前取 `lastKnown`（不晚于动作，能抗销毁），观察失败时用 `headOr`。
   - `navigate` 兜底：catch 内 `headOr(tab, lastKnown)` 取当前值；`otherTabs` 与 `result()` 共用私有 `siblingTabIds(tab)`（唯一组装点）；`dialog: tab.dialog`（主进程本地事实）；`notes` 补「read a fresh snapshot before continuing」。`snapshot` 空串、`frames: 0`、`newLogs: 0` 保留（正文只在 >0 时输出，不构成断言）。
   - 降级路径**不推进** `deliver`/`digest`（沿用设计 `:132`：没有页面结果/代次不匹配时不提交基准），下一次成功页面结果补交恰好一次。
3. 契约/文案：
   - wire 形状不变；新增 `contract/wire-samples.json` 的 `act_observation_failed` 样例，`browser-contract.test.ts` 断 wire 输出、`test_browser_contract_guard.py` 渲染失败分支（C-4）。
   - 后端 `act_observation` observation=null 分支：`TAB_NOT_FOUND` 时说明「页头是动作前的页面」；其他码附 `failure_advice(code)`（DIALOG_OPEN 等给出与执行中断一致的下一步）。
4. 可选 P2-1：`Release` 增 `dispatched` 事实；已派发后失败 → 清 `held`、保留 `unsafe`；`drain` 先判 `unsafe` 再决定是否 release。

**结构归属**

| 事实 | 所有者 |
| --- | --- |
| 「effect 内命令是否可能改变页面」 | `BrowserOperation`（effect 事实） |
| 「这一步确定没生效 / 可能生效」 | `runActions`（步骤结算） |
| 页面 head / 兄弟标签 / 弹窗 / 降级结果 | `BrowserHost`（`headOr`、`siblingTabIds`、唯一 result/degraded 构造点） |
| wire / 后端正文 / UI | 只消费值，形状不变 |

### 包 II：滚动目标（P1-2）

**现状**：`input/page-functions.ts:18` 祖先遍历无条件跳过 `document.body`；`:19` 回退到 `document.scrollingElement`（在 `html{overflow:hidden}+body{overflow-y:auto}` 页面上是不可滚的 `<html>`）。两条路径（带 ref 的 `:58-59`、不带 ref 的 `:60`）都命中；`document.scrollingElement` 在怪异模式可为 null。

**终态**：
- **保留现有祖先链判定**（带 ref 从元素向上找滚动容器、不带 ref 从 documentElement 出发；不改变常规布局的结果），只修两处：
  1. 祖先遍历到 `body` 时不再无条件把它换成 page：当 `document.scrollingElement` 实际不可滚、而 `body` 可滚时，目标选 `body`；
  2. 回退链改为唯一规则：`scrollingElement`（若真的可滚）→ `body`（若真的可滚）→ `scrollingElement || documentElement`；最后仍不可滚才报 `SCROLL_END`。`document.scrollingElement` 在怪异模式可为 null，不直接信任。
- 这样 html 正常滚动（`scrollingElement` 可滚 → 仍选 html）、`html,body{height:100%}+body auto`（html 也可滚 → 仍选 html，与现状一致）、div/嵌套容器（祖先链先命中 div，不变）都保持现有行为，只修「document 不可滚 + body 是真实滚容器」。
- 不带 ref 保持「滚页面」契约；错误文案区分「页面不可滚（可传 ref 滚某区域）」与「已到底」，两者都仍是 `SCROLL_END`（不新增错误码，不改后端建议表）。
- 与 P1-1 联动：`SCROLL_END` 由动作抛 `NoEffectError`（确定未滚动），步骤状态为 `failed`。

### 包 III：投影与游标文案（P1-4 + P1-3）

**P1-4**：
- 现状：`browser-host.ts:250` `agentBusy = operation?.state !== "unknown" && (agentCalls > 0 || tab.operation !== null)`，`tab.operation` 与发起方无关；`userNavigate`（`:297-302`）也占槽。前端 `browser-panel.tsx:93-96,141`、`agent-activity-bar.tsx:14` 按「Agent」文案消费。
- 终态：`agentBusy: agentCalls > 0`（回到 9eb228f2 原始契约）；`operation` 投影保留（后端交接消费 `operation`，`handoff.py:78`）；`host-contract.ts` 注释不变（它本来就写 Agent）。前端无需改：用户导航时标签条回到 loading 转圈，无 Run 时不显示 Agent 提示。
- 若产品将来要展示「页面正被任何人操作」，新增独立字段/文案，不改 `agentBusy` 语义。

**P1-3（降级项，只修文案/契约，不做结构改造）**：
- 页面结果 `new_logs` 的提示改为「browser_logs **without since** shows them」；`rendering.py:230-233` 的 since 说明补一句：since 按**条目开始序号**过滤，读过的在途请求之后落定的状态不会因 since 重读出现；要看结局请省略 since 或传更早序号。
- `tool_models/browser_args/logs.py:18-26` 与英文描述删除过度承诺（页面结果不携带游标；「since you last looked」改为「since this tab's last page result」）。
- 同一请求重复计数：`TabLog.update` 只在「从未失败 → 失败」时加入 `newFailures`（或按条目记录已报代数），一行语义修正 + 测试。
- **不做**：把 since 改成变化时间、把 next 滞后到最老未落定项（轮询/SSE 会让游标卡死）、给 new_logs 加游标（与阶段 3 决定冲突）。可选 `in_flight:{count,oldest_seq}` 字段留待需要时单独立项（需 wire/守卫同步）。

---

## 3. 行为账

### 行为不变

- wire 形状与字段名、`ActResult` 状态枚举、`PageHead`/`PageResult` 字段、工具参数模型、错误码集合、方法时限、host-link 协议、IPC 契约。
- `operation` 状态机（running/stopping/unknown）、close/交接/取消语义、`unknown` 隔离与「关闭标签恢复」、`actions.ts:45` 的批内答弹窗、`digest`/`deliver` 的消费点与「降级不推进」规则。
- 已知 `unknown` 正确项：click/hover/drag 的已派发命令失败、`insertText`/`clear` 失败、`select no-option-matched`（实测已改 selected）、`drag` 收尾撤销失败（偏保守但方向安全）。
- 滚动：常规 html 滚动、div 容器、嵌套容器、完全不可滚页的现有结果与错误码。

### 有意改变（用户/模型可见）

1. 8 条「确定没生效」路径：`unknown` → `failed`；模型正文不再出现「Some input may already have taken effect… do not replay」，UI 步骤卡从「结果不确定，请先检查页面」变红色失败 + 短提示。
2. `act` 观察失败回执页头：旧页面 → 当前页面（标签存活时）；标签已销毁时仍返回执行前最后已知值，并照旧以 `TAB_NOT_FOUND` 说明。
3. `navigate` 观察失败兜底：`other_tabs` 由 `[]` → 真实兄弟标签；`dialog` 由 null → 当前弹窗；正文多一句恢复指引。
4. 后端 observation 失败正文：TAB_NOT_FOUND 时说明页头是动作前状态；其他码附下一步建议。
5. 用户自己导航时不再显示「Agent 正在操作这个页面」，恢复 loading 反馈；Agent 操作期间不变。
6. body 滚动容器页面：`scroll` 真正滚动；到真正底部才 `SCROLL_END`；错误文案区分「页面不可滚/已在底部」。
7. `browser_logs` 文案改为准确描述两种读取口径；迟到失败在同一页面结果里不再重复计数。
8. （可选）已派发释放失败不再重放一次 `mouseReleased/keyUp`，标签更快从 `unknown` 收敛（否则只能关标签）。

---

## 4. 消费方（全部调用方/消费方）

| 改动事实 | 宿主 | 后端 | 前端 | 测试/契约 |
| --- | --- | --- | --- | --- |
| `steps[].status` 取值（值变化，枚举不变） | `wire.ts:actWire` 原样透传 | `rendering.py:act_summary`（计数/建议）、`browser_display.py:build_act_steps`（hint） | `browser-page-display.ts`、`browser-page-view.tsx:ActSteps` | `browser-pure.test.ts`、`browser-page-tool.test.ts`、`browser-engine.spec.ts`、backend `test_browser_tools.py` |
| `ActOutcome` 顶层 head（值变化） | `browser-host.ts` | `rendering.py:act_observation`/`header_lines`、`browser_display.py` | `browser-page-view.tsx:BrowserPageMeta`（display_data.url/title） | 新增 `wire-samples` `act_observation_failed` + 两侧守卫 |
| `PageResult.other_tabs/dialog/notes`（值变化） | `browser-host.ts` | `rendering.py:page_text`（other tabs / dialog notice / notes） | 无（display_data 不含这些） | `test_browser_tools.py`、contract guard |
| `agentBusy`（值变化） | `browser-host.ts:state` | 无（后端不消费） | `browser-panel.tsx:95-96,141`、`agent-activity-bar.tsx:14` | `browser-host.test.ts`、e2e fake host |
| `scroll` 行为/错误文案 | `input/page-functions.ts` | 建议表文本不变（`errors.py:95-98` 仍适用） | 无 | `browser-engine.spec.ts`（新增 body-scroller 用例）、native 台账 |
| `browser_logs` 文案/重复计数 | `tab-log.ts`（重复计数） | `rendering.py`、`logs.py`、`tool_models/browser_args/logs.py` | 无 | `browser-tab-log.test.ts`、`test_browser_tools.py`、contract guard |
| （可选）释放不重放 | `operation.ts` | 无 | 无 | `browser-operation.test.ts` |

其它调用方确认：`browser_tabs` 的 `operation` 字段与 `handoff.py:78` 的交接前置检查不受影响；`state()` 的 `operation` 投影保留；`browser-page-view.tsx:17` 注释「地址与标题是调用当时的快照」在修正后仍成立（只是从旧值变成当前值，语义更准确）。

---

## 5. 分阶段实施与验证

> 每阶段独立可交付；先加/改回归测试再改实现；被取代的旧路径在同阶段删除，不保留两套。

### 阶段 0：把缺陷做成回归测试（不改变行为）

- 搬 `temp/p1-verify-a/matrix.test.ts` 的 8 条红测进 `tests/unit/browser-pure.test.ts`（或新 `browser-action-settlement.test.ts`；用「生产顺序」替身，不要简化 mock）；保留 67 条绿测作为正向护栏。
- 搬 `temp/p1-verify-c/repro-receipt-facts.test.ts` 的 P1-5a/5b/C-1 红测进 `tests/unit/browser-host.test.ts`；搬 `temp/p1-verify-b/agent-busy.test.ts` 的用户导航用例。
- 新增 engine 用例（`tests/desktop-e2e/browser-engine.spec.ts`）：body-scroller 页面带/不带 ref 滚动、滚到底、常规页回归；同用例可加 `fullPage` 现状断言（作为独立待办证据）。
- 验收：红测失败、绿测全绿；基线 147 宿主单测 / 76 后端测试不受影响。

### 阶段 1：投影与文案（包 III，低风险）

- P1-4：`agentBusy = agentCalls > 0` + `host-contract`/类注释核对 + host 单测（用户导航 false / Agent 读取 true / unknown 时 false）。
- P1-3：渲染/描述文案、重复计数修正；后端测试与契约样例。
- 验收：`npx vitest run tests/unit/browser-host.test.ts tests/unit/browser-tab-log.test.ts`、后端 `test_browser_tools.py`；用户导航场景不再显示 Agent 提示。

### 阶段 2：执行结算（包 I 前一半，P1-1）

- `operation.ts`：`NoEffectError`/`certainNoEffect` + effect 契约注释；
- `input/element.ts`：拆分只读 `callOn` 与 `mutateOn`；
- `input/page-functions.ts`：`scroll` 的 `element()` 移出 effect、`SCROLL_AREA` 用 `mutateOn`、problem → `NoEffectError`；`select` 加只读 tag 预检、`SELECT_OPTIONS` 用 `mutateOn`；`input/keyboard.ts` 的 clear 用 `mutateOn`；
- `input/actions.ts`：状态结算改为 `NoEffectError || !effectStarted → failed`；保留 `:45` 弹窗续跑。
- 验收：8 条红测转绿；67 条矩阵全绿；`browser-operation.test.ts`、`browser-pure.test.ts`；engine body-scroller 用例断言最终步骤 `failed`（与阶段 3 滚动行为共用）。

### 阶段 3：回执事实（包 I 后一半，P1-5）

- `browser-host.ts`：`headOr`、`siblingTabIds`、act lastKnown 移入 operate 回调、navigate 兜底复用事实（dialog/otherTabs/notes）、（可选）release 不重放；
- wire 样例新增 `act_observation_failed`；`browser-contract.test.ts` 与 `test_browser_contract_guard.py` 覆盖失败分支；
- 后端 `rendering.py` observation 失败文案补 TAB_NOT_FOUND 说明与 `failure_advice`。
- 验收：P1-5a/5b/C-1 红测转绿 + 销毁兜底仍绿 + digest「不丢失、不重复」测试；backend 契约/工具测试全绿；真实 Electron 探针复跑（导航后观察失败 head=新 URL；关闭标签仍返执回）。

### 阶段 4：滚动目标（包 II，P1-2）

- 修 `SCROLL_AREA` 的 body 跳过与回退链（保留祖先链判定，`scrollingElement` 不可滚时用 body，怪异模式回退；区分「页面不可滚/已在底部」文案）；
- engine 用例：body-scroller 带 ref/不带 ref 都滚、滚到底第二次 `SCROLL_END`、常规 html/div/嵌套/不可滚页回归、移动模拟。
- 验收：新增 engine 用例通过；`browser-engine.spec.ts` 全量；`scripts/test-browser-native.mjs` 台账补一组（可选，手工验收）。

每阶段回归命令（在 `apps/desktop`；后端在 `apps/backend`）：

```bash
npx vitest run tests/unit/browser-*.test.ts
.venv/bin/python -m pytest -q tests/test_browser_tools.py tests/test_browser_contract_guard.py
npx playwright test -c playwright.desktop.config.ts tests/desktop-e2e/browser-engine.spec.ts   # 需先 npm run build
```

---

## 6. 链路复用

- **直接复用**：`BrowserOperation.effect()/inputUnit/track/drain`（`operation.ts`）、`CdpSession` 单命令时限与闸门（`cdp-session.ts`）、`element()/actionable()` 检查（`element.ts`）、`runActions` 结算循环（`actions.ts`）、`BrowserHost.head()/result()/tabsOf()` 与 `preserveReceipt` 语义（`browser-host.ts`）、`TabLog` 容量与 digest（`tab-log.ts`）、两套 wire/契约守卫与 `wire-samples.json`、engine E2E 与裸 Electron 探针。
- **改造后复用**：`callOn` 拆成只读与 `mutateOn` 两种用法（不是加参数/标志位换复用：职责不同、调用点明确改为对应助手）；`effect()` 语义收窄后原调用方（pointer/keyboard/page-functions）继续使用，行为更准确；`result()` 的兄弟标签组装抽 `siblingTabIds` 供兜底共用，不再有第二份字面量。
- **无可复用**：无新增依赖、无新进程/服务、无新抽象接口/工厂；`NoEffectError` 是唯一具体错误类型（单实现，按「抽象按需引入」不加基类/接口）。

---

## 7. 问题清单（复核中发现，本次一并处理/或明确不做）

| 编号 | 现象 | 证据 | 根因层级 | 置信度 | 处置 |
| --- | --- | --- | --- | --- | --- |
| P1-1（8 条） | 确定无效果失败被判 unknown | `actions.ts:40`、`operation.ts:49`、`page-functions.ts:58-61`、矩阵 8 红 | 局部逻辑 + 契约表达 | 高 | 包 I 阶段 2 |
| P1-2 | body 滚动容器假 SCROLL_END | `page-functions.ts:18-19`、真实 Chromium 13 布局 | 局部逻辑 | 高 | 包 II 阶段 4 |
| P1-5a/b + C-1/C-2/C-4 | 观察失败回执旧页头 / otherTabs=[] / dialog=null / 缺指引 / 无失败样例 | `browser-host.ts:140,157,124`、`rendering.py:154-181`、`wire-samples.json:30-98` | 局部逻辑 + 契约样例缺口 | 高 | 包 I 阶段 3 |
| P1-4 | agentBusy 把用户导航算 Agent | `browser-host.ts:250`、契约 `host-contract.ts:130-132`、git 9eb228f2 vs e1b95010 | 局部投影回归 | 高 | 包 III 阶段 1 |
| P1-3 | since 游标与失败计数两种口径未说清；同请求可重复计数 | `tab-log.ts:180-198,151-166,205-216`、`rendering.py:225-233`、`logs.py:31-35` | 契约文案 + 局部计数 | 高 | 包 III 阶段 1（文案/计数），不做结构改造 |
| P2-1 | 已派发释放失败被重放 | `operation.ts:106-111,130-135` | 局部状态机 | 高 | 可选随包 I（明确标记可裁剪） |
| fullPage 只截一屏 | body-scroller 布局下整页截图只有视口高 | `page-capture.ts:32-34`、`page-geometry.ts:50`、真实探针 | 局部/CDP 事实源 | 高 | **刻意不做**：与 scroll 不同事实源（`Page.getLayoutMetrics.cssContentSize` 不含内层滚动容器），需单独方案，不混入本单 |
| UI 观察失败不可见 | steps 全 done 时卡片看起来成功 | `browser_display.py`、`tool-result-signals.ts` | 展示契约缺口 | 中 | **刻意不做**：需 display_data 新事实，单独立项 |
| act head 早于等标签可用 | 初始化中标签记 about:blank | `browser-host.ts:140` | 局部时序 | 中 | 实现时顺手：lastKnown 移入 operate 回调（阶段 3） |
| 文档/描述不实 | logs.py 两处、页面结果不带游标 | `logs.py:31-35`、`wire.ts` | 契约文案 | 高 | 包 III 阶段 1 |

---

## 8. 选定路线与被否候选

**选定**

- P1-1：显式动作结算 + effect 边界收窄（`callOn`/`mutateOn` 二分 + `NoEffectError`），不是按错误码/失败路径逐个特判。
- P1-5：页头与兜底字段从事实源取值（`headOr`/`siblingTabIds`/`tab.dialog`），不造第二套页面结果、不改 wire 形状。
- P1-2：重写页面内滚动目标解析，保持既有正常布局行为；不引入 Playwright 或「猜最大滚动容器」。
- P1-4：恢复 `agentBusy = agentCalls > 0`。
- P1-3：只修模型可见文案与重复计数；不改造游标语义。

**被否**

| 候选 | 否决理由 |
| --- | --- |
| P1-1 按 `SCROLL_END/REF_STALE/ACTION_FAILED` 等错误码特判 failed | 同一码可在不同阶段出现（REF_STALE 可能在已生效输入后），会引入新的特例与误判；同类会再漏 |
| P1-1 把页面函数返回 problem 一律降 failed | 实测 `select no option matched` 已改 `option.selected`、`CLEAR_FIELD` 可能已生效；矩阵绿测会破 |
| P1-1 重写动作执行引擎/统一输入队列 | 过度；现有 `effect()`+模块划分可承载，改动面大且风险高 |
| P1-5 删除 act 外层 head / 造空 observation / 降级路径消费 digest | 分别破坏「标签关闭后仍可答」、把「没读到」伪装成「读到空页面」、违反 `:132` 基准提交规则（C 报告 §6.6） |
| P1-5 把 navigate 也拆成 execution/observation 两段 | 会改 wire/后端/前端/样例，属独立功能改造，超出本次缺陷范围 |
| P1-2 用 Playwright Locator/滚动 | 引入第二套页面接管与定位体系，与现有 CDP 栈冲突 |
| P1-2 遍历 DOM 猜「最大滚动容器」 | 选择歧义、与「不带 ref 滚页面」契约不符 |
| P1-3 把 `since` 改成「变化时间」/把 next 滞后到最老在途项 | 破坏游标契约；轮询/SSE 会让 next 永远卡住（B 报告 §修法约束） |
| P1-4 新增「谁在操作」字段 | 当前无 UI 需求；先恢复契约，将来需要再单独立项 |

---

## 9. 改动账本（预估，交付后以 `git diff --stat` 对账）

### 精简（删除/合并）

- `input/page-functions.ts`：删 `scroll` 里对 `element()` 的 effect 包裹、删 `select` 的非 `<select>` 分支在 `SELECT_OPTIONS` 内的重复判断、删 `SCROLL_AREA` 里「body 一律跳过」的回退链（约 −20 行）。
- `browser-host.ts`：删 navigate 兜底手写 `otherTabs: []`/`dialog: null`，删 act 提前 `head` 捕获，合并 `result()`/兜底兄弟标签表达式（约 −20 行）；`state()` 复合条件删掉 `(agentCalls > 0 || tab.operation !== null)` 的第二个条件（−0 净行，表达式简化）。
- `input/element.ts`：`callOn` 内 resolve+调用合并路径拆开，删重复 CDP 参数拼装（约 −5 行）。
- `operation.ts`：释放状态补 `dispatched` 后，删 drain 里「先 release 后判 unsafe」的旧顺序（约 −3 行）。
- 后端/前端无删除（仅文案）。

### 新增

- `operation.ts`：`NoEffectError`/`certainNoEffect` + effect 契约注释 + （可选）Release.dispatched（约 +22 行）。
- `input/element.ts`：`mutateOn` 与可复用的 page-call 拆分（约 +25 行）。
- `input/actions.ts`：结算分支（约 +8 行）。
- `input/page-functions.ts`：滚动目标解析重写、`mutateOn` 调用、`NoEffectError`、文案区分（约 +30 行）。
- `input/keyboard.ts`：clear 改 `mutateOn`（约 +2 行）。
- `browser-host.ts`：`headOr`、`siblingTabIds`、act lastKnown、navigate 兜底、agentBusy 一行（约 +30 行）。
- `tab-log.ts`：重复计数修正（约 +6 行）。
- 后端：`rendering.py` 文案/失败建议（约 +8 行）、`logs.py` 描述（约 +2 行）、`tool_models/browser_args/logs.py` since 描述（约 +3 行）。
- 契约：`wire-samples.json` 新增 `act_observation_failed`（约 +30 行 JSON）。
- 测试：`browser-pure.test.ts`/`browser-operation.test.ts`/`browser-host.test.ts`/`browser-contract.test.ts`/`browser-tab-log.test.ts`、`browser-engine.spec.ts`、backend `test_browser_tools.py`/`test_browser_contract_guard.py`（约 +500 行）。
- 文档：`docs/plan/browser-reliability-refactor-plan.md` 实施记录/已知限制补一行（约 +2 行）；`AGENTS.md` 不改（边界未变）。

**净变化：生产代码约 +130/−70 行；测试约 +500 行。** 最终以 `git diff --stat` 对账；若 P2-1 不做，生产净变化约 −8 行（释放逻辑不扩充）。

### 目录/文件命名变更

- 无新增目录；无文件改名/搬家。
- 只有测试落点：优先扩展既有 `tests/unit/browser-*.test.ts`，不新增测试文件；若矩阵体量需要，新增 `tests/unit/browser-action-settlement.test.ts`（正向检索：browser + action + settlement；反向：一眼知是浏览器动作结算测试）。
- 计划文档：已从 `temp/` 移入 `docs/plan/browser-p1-remediation-plan.md`；`temp/` 副本不再维护。

### 同步

- 测试：上述 unit/engine/backend/contract 两侧守卫。
- 文档：`docs/plan/browser-reliability-refactor-plan.md` 的验收记录/已知限制；`embedded-browser-p3-discussion.md` 若涉及步骤语义不再改（历史记录）。
- 日志：沿用现有 `logDesktopEvent`/backend 日志事件名，不新增业务日志字段。

---

## 10. 刻意不做

1. **P1-3 的结构性游标改造**（entry 版本号/`in_flight` 字段/next 滞后）：设计已明确「省略 since」路径；本次只修文案与重复计数。若将来出现真实失败案例，再单独立项。
2. **`fullPage` 截图在 body-scroller 页面只截一屏**：与 scroll 同族但事实源是 `Page.getLayoutMetrics`，修法不同（需另选内容尺寸来源）；不在本单顺手改，避免混合风险。
3. **观察失败 UI 信号（C-3）**：需要 display_data 新事实 + 前端信号契约，单独立项。
4. **P2-2 fileChooser 不消费、P2-3 URL 边界、overlay-watch 漏 `#root` 浮层、下载备注只进触发标签、面板 Task 归属等上一轮 P2/P3**：不属于本次 P1 目标；本方案不夹带。
5. **新错误码**：滚动「页面不可滚」与「已到底」只改文案，保留 `SCROLL_END`，避免新增码要同步宿主/后端/守卫/建议表。
6. **Playwright/新依赖/新进程/新抽象接口**：不引入。
7. **Windows 与真实站点验证**：本环境只覆盖 macOS；真实 Chromium 探针覆盖布局与 CDP，真实站点风控/登录不在本次。

---

## 11. 实施顺序（已确认）

用户确认「推荐 A，全做」：按 §5 阶段 0→1→2→3→4 全部实施；P2-1（释放不重放）随包 I 一并做。每阶段完成后先跑该阶段验收，再进入下一阶段；任何阶段验收不通过，先修再前进。

---

## 12. 验收方案（改动前锁定）

### 12.1 验收角色与时点

- **改动前**：本节随方案一起锁定；测试用例先写/先跑出「红」，再改实现。
- **改动后**：Lead 自验（跑全部命令、对 diff 做加减法与结构自检）→ **独立子 Agent 验收**（2 路只读：A 路验 P1-1/P1-2，B 路验 P1-3/P1-4/P1-5/P2-1；只读、自己跑测试与探针、独立判断，不采信 Lead 结论）→ Lead 汇总；子 Agent 提出的真实问题必须修复并重跑相关验收。

### 12.2 阶段验收矩阵

| 阶段 | 验收项 | 落点测试（预期） | 命令 |
| --- | --- | --- | --- |
| 0 | 缺陷先变红 | `browser-pure.test.ts` runActions 新增 8 例期望 failed（当前 unknown→红）；`browser-host.test.ts` P1-5a/5b/C-1 期望新语义（当前红）；`browser-host.test.ts` 用户导航 agentBusy=false（当前红）；`browser-engine.spec.ts` body-scroller 用例（当前红，需构建） | `npx vitest run tests/unit/browser-pure.test.ts tests/unit/browser-host.test.ts`；engine 单跑 |
| 1 | P1-4：用户导航不再算 Agent；Agent 操作仍算 | `browser-host.test.ts`：用户 `userNavigate` 期间 `agentBusy=false` 且 `operation="running"`；Agent snapshot 期间 `agentBusy=true`；unknown 时 false | `npx vitest run tests/unit/browser-host.test.ts` |
| 1 | P1-3：文案准确、同一请求不重复计数 | `browser-tab-log.test.ts`：500 后 loadingFailed 只计一次；`test_browser_tools.py`/`test_browser_contract_guard.py`：新文案断言（无 since 提示、游标按开始序号、页面结果不带游标） | `npx vitest run tests/unit/browser-tab-log.test.ts`；后端 pytest |
| 2 | P1-1：8 条确定无效果失败 → failed；unknown 正向项保持 | `browser-pure.test.ts` runActions：8 例绿 + 既有「按下后失败→unknown」「insertText/clear 失败→unknown」「select no-option-matched→unknown」「drag 收尾失败→unknown」全绿；75 条矩阵（搬自 `temp/p1-verify-a/matrix.test.ts`）8 红转绿、67 绿不变 | `npx vitest run tests/unit/browser-pure.test.ts tests/unit/browser-operation.test.ts` |
| 3 | P1-5a：act 观察失败回执页头=当前页面（存活时）；销毁时仍返执回并退回执行前值 | `browser-host.test.ts`：动作改变 url/title 后观察失败 → head 当前值；观察期间销毁 → head=lastKnown、still execution、observationError=TAB_NOT_FOUND；成功路径外层/observation head 同源 | `npx vitest run tests/unit/browser-host.test.ts` |
| 3 | P1-5b/C-1/C-2：navigate 兜底 other_tabs 真实、dialog 取事实、notes 有恢复指引；降级不消费 digest/deliver 且下次补交恰好一次 | `browser-host.test.ts` 新增 4 条；现有「navigate 观察失败不消费基准」保持 | 同上 |
| 3 | 契约：失败分支样例两侧守卫生效 | `wire-samples.json` 新增 `act_observation_failed`；`browser-contract.test.ts` 输出逐字相等；后端 `test_browser_contract_guard.py` 渲染失败分支并断页头/建议 | vitest + pytest |
| 3 | P2-1：已派发释放失败不重放 | `browser-operation.test.ts`：`mouseReleased` 派发后 reject → finish 后只 1 次 release，状态 unknown；既有「未派发、答弹窗后只释放一次」保持 | `npx vitest run tests/unit/browser-operation.test.ts` |
| 4 | P1-2：body 滚动容器页带/不带 ref 都滚、到底才 SCROLL_END；normal 布局回归 | `browser-engine.spec.ts` 新增：body-scroller（html hidden + body auto）带 ref/不带 ref、滚到底第二次 SCROLL_END、html 正常/div/嵌套/不可滚页既有用例全绿；步骤状态 failed（与 P1-1 联动） | `npx playwright test -c playwright.desktop.config.ts tests/desktop-e2e/browser-engine.spec.ts`（需 `npm run build`） |

### 12.3 总体验收门槛（全部满足才算完成）

1. **行为正确**：上表全部阶段验收通过；8 条 red→green；正向护栏（unknown 正确项）不变。
2. **无回归**：`npx vitest run tests/unit/browser-*.test.ts` 全绿；`apps/backend` 的 `test_browser_tools.py`、`test_browser_contract_guard.py`、`test_host_link.py`、`test_handoff_registry.py` 全绿；`browser-engine.spec.ts` 全绿（若环境确实无法构建/运行，必须由子 Agent 复现同一阻塞并记录，才允许以裸 Electron 探针替代并在验收报告标注）。
3. **契约不变**：wire 形状、IPC 契约、工具参数 schema、错误码集合不变（只新增测试样例，不改字段）；`test_browser_contract_guard.py` 的 schema/deadlines/错误码镜像断言通过。
4. **类型与构建**：`apps/desktop` 的 TypeScript 检查（`npx tsc --noEmit` 与 `npx tsc -p electron --noEmit`）通过；后端 `ruff check`（改动文件）通过。
5. **代码加减法**：`git diff --stat` 与 §9 预估对账；删除被取代的旧路径（effect 内只读校验、手写兜底字面量、`tab.operation !== null` 条件、重复的 SELECT 非 select 分支）；不新增并行路径/双实现/预留接口/新 flag；新增代码集中在事实源与测试。
6. **可排查**：不新增无归属日志；错误码/日志事件名沿用既有。

### 12.4 独立子 Agent 验收口径

- **只读**：不修改生产/测试/配置；只在 `apps/desktop/temp/acceptance-a|b/` 写报告与临时探针（gitignore）。
- 必须：读 diff 与验收矩阵；**自己重跑**上表命令；核对 8 条红转绿与正向护栏；抽查 `文件:行` 证据；对代码加减法做独立判断（是否还有旧路径、是否用特例/标志位换复用、是否引入第二事实源）；给逐条 verdict（通过/不通过 + 证据 + 是否已复现）。
- 不可只复述 Lead 结论；发现不一致以自己复跑的为准，并在报告中给出命令与输出。
- 两路分工：A 路 P1-1（结算）+ P1-2（滚动）+ 引擎 E2E；B 路 P1-3/P1-4/P1-5/P2-1 + 契约/后端/类型/加减法。Lead 汇总两路后再交付。

### 12.5 失败处理

- 阶段内验收失败：在当前阶段修实现或测试（测试本身错了就改测试并说明），重跑该阶段全部命令。
- 子 Agent 验收不通过：修复 → 重跑受影响验收 → 请同一子 Agent 复核；未通过不得交付。
- 环境性阻塞（如 engine E2E 构建/显示）：记录具体命令、错误、复现步骤；由子 Agent 独立复现确认后才可替代，并在交付中明确标注「未跑」与替代证据。

---

## 13. 实施记录（2026-11-09）

按 §5 阶段 1→4 全部实施（含 P2-1），改动前后自检如下；独立验收见 §14。

### 13.1 实际改动（`git diff --stat`，仅本方案相关文件）

生产代码：

- `electron/main/browser/operation.ts`：`NoEffectError`/`certainNoEffect`，effect 契约注释；`Release` 记录 `dispatched`，原始命令**拒绝**后清 held + unsafe（不重放），派发前/等待被闸门提前结束时保留 held（弹窗答完仍可释放一次）。
- `electron/main/browser/input/element.ts`：拆 `callOn`（只读）与 `mutateOn`（resolve 在 effect 外、页面函数在 effect 内）；`runOnPage` 共用参数拼装。
- `electron/main/browser/input/actions.ts`：步骤结算改为 `NoEffectError || !effectStarted → failed`。
- `electron/main/browser/input/page-functions.ts`：`scroll` 用 `mutateOn` 且 `SCROLL_END` 抛 `certainNoEffect`；`select` 加只读 `<select>` 预检、`SELECT_OPTIONS` 用 `mutateOn`；`SCROLL_AREA` 修 body 跳过/回退链/怪异模式（视口可滚性用 root 的 scrollHeight/clientHeight + html overflow hidden 判定，不能用 overflowY 谓词），文案区分「页面不可滚」与「已到底」。
- `electron/main/browser/input/keyboard.ts`：clear 改用 `mutateOn`。
- `electron/main/browser/browser-host.ts`：`headOr`/`siblingTabIds`；act `lastKnown` 移入 operate 回调；navigate 兜底取当前页头/兄弟标签/弹窗/恢复指引；`agentBusy = agentCalls > 0`。
- `electron/main/browser/tab-log.ts`：失败计数只在首次成为失败时记一次。
- `electron/main/browser/contract/wire-samples.json`：新增 `act_observation_failed`；`tool-args.schema.json` 随参数描述重新导出。
- 后端：`rendering.py`（观察失败分支的建议/闭标签说明、new_logs 读取指引、游标文案、在途请求提示）、`logs.py` 工具描述、`tool_models/browser_args/logs.py` since 描述。

测试/配置：

- `tests/unit/browser-action-settlement.test.ts` + `browser-action-settlement-fixture.ts`（新增，75 条动作×失败路径矩阵：8 条确定无效果转绿 + 67 条正向/保守护栏）；`browser-host.test.ts` +6（用户导航 agentBusy、act 销毁 TAB_NOT_FOUND、成功 act 页头同源、降级不消费基准）；`browser-operation.test.ts` +2（已派发失败不重放、弹窗暂停期间派发的释放落定后收敛）；`browser-tab-log.test.ts` +1；`browser-contract.test.ts` +1；`browser-engine.spec.ts` +2（body 滚动容器、html/body 都 100% 高）。
- 后端 `test_browser_contract_guard.py` +3 断言/用例。
- `apps/desktop/tsconfig.json` 的 `exclude` 增加 `temp`（gitignore 的临时审查目录不参与类型检查）。

### 13.2 自检结果（Lead 实跑）

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 宿主浏览器单测（含新增矩阵） | `cd apps/desktop && npx vitest run tests/unit/browser-*.test.ts` | **15 files / 232 passed** |
| 前端/测试 TypeScript | `npx tsc --noEmit` | 通过 |
| Electron TypeScript | `npx tsc -p electron --noEmit` | 通过 |
| 构建 | `npm run build` | 通过 |
| 后端浏览器/契约/链路/交接 | `cd apps/backend && .venv/bin/python -m pytest -q tests/test_browser_tools.py tests/test_browser_contract_guard.py tests/test_host_link.py tests/test_handoff_registry.py` | **97 passed** |
| Ruff | 改动后端文件 `ruff check` + `ruff format --check` | 通过 |
| ESLint | 改动 desktop 文件 | 通过 |
| 空白 | `git diff --check` | 通过 |
| 真实 Electron 引擎（本轮可用） | `ELECTRON_DISABLE_SANDBOX=1 env -u ELECTRON_RUN_AS_NODE npx playwright test -c playwright.desktop.config.ts tests/desktop-e2e/browser-engine.spec.ts` | **42 passed / 1 failed**；失败是 `follows the panel size...` 截图像素 625×385 vs 640×400（环境显示差异，与本方案改动无关；其余 42 条含 dialog 顺序、body-scroller/height-only/普通页到底文案全绿） |

### 13.3 真实引擎验收的环境阻塞与替代证据

- 无 `ELECTRON_DISABLE_SANDBOX=1` 时：Electron 启动后，真实 `BrowserHost` 新建标签初始化 `loadURL("about:blank")` 抛 `ERR_FAILED (-2) loading 'about:blank'`，既有用例同样失败（不是本次改动引入）。原因是本机 Electron sandbox/启动环境限制（与 `p1-verify-a` 复核记录一致）。
- 加 `ELECTRON_DISABLE_SANDBOX=1 env -u ELECTRON_RUN_AS_NODE` 后引擎可跑：全量 **42 passed / 1 failed**，唯一失败是面板尺寸截图断言（环境显示差异，见上表）。P1-2 的文案分支（普通视口页/height-only 到底应报「already at the bottom」，只有视口与 body 都不可滚才报「cannot scroll」）已由 engine 用例与裸 Electron 探针共同覆盖。
- 替代证据（裸 Electron，真实 Chromium，函数体从生产源码 `page-functions.ts` 原样提取）：
  `env -u ELECTRON_RUN_AS_NODE node_modules/electron/dist/Electron.app/Contents/MacOS/Electron --no-sandbox --disable-gpu ../../temp/acceptance-evidence/scroll-probe.cjs`
  输出：body-scroller 布局 `problemPage=""`、`problemRef=""`，`body.scrollTop 0 → 294 → 588`；怪异模式同样；html 正常滚动成功；div 容器/不可滚页不误滚并返回区分后的 `SCROLL_END` 文案。

---

## 14. 独立验收记录（2026-11-09）

两路独立子 Agent 验收（只读生产代码；全部报告/探针在 gitignore 的 `apps/desktop/temp/acceptance-a|b/`）。

### 14.1 A 路（P1-1/P1-2/真实引擎/代码加减法）：最终通过

- P1-1：75 条动作结算矩阵已搬入 `tests/unit/browser-action-settlement.test.ts` + fixture（case 名称与复核期矩阵逐条一致，fixture 与生产 `cdp-session.ts` 的 signal→闸门→onDispatch→sendCommand→onCommand 顺序一致）；8 条红转绿，67 条正向护栏保持；无旧路径残留。
- P1-2：body-scroller 与 html/body 都 100% 高的布局都正确滚动；`scroll-end-text-probe.cjs` 复跑后仅剩探针自身的 1 条错误预期（div 非 ref 时页面确实不可滚，文案正确）；裸 Electron 10 组布局 31 断言全 PASS；真实引擎链路 height-only/body-scroller/html 正常三种滚动器都正确。
- 真实引擎全量：**42 passed / 1 failed**；唯一失败 `follows the panel size...` 截图像素 625×385 vs 640×400 为固定环境显示差异（与本方案 diff 无关，两轮一致）。
- 代码加减法：未发现堆代码、并行实现、无谓 flag；生产 diff 净增主要来自 new 分支/注释/格式。

### 14.2 B 路（P1-3/P1-4/P1-5/P2-1/契约/构建）：最终通过

- P1-4：真实 `userNavigate` 在途时 `agentBusy=false / operation=running`；Agent 读取 true；unknown false；`operation` 投影保留。
- P1-5：act 存活读当前 head、销毁退回 lastKnown 且断言 `TAB_NOT_FOUND`；成功 act 外层/observation head 同源；navigate 兜底 otherTabs/dialog/恢复指引；降级不消费 deliver/digest、下一次成功结果恰好补交一次；wire 失败样例 + 两侧守卫生效；后端 TAB_NOT_FOUND 说明/建议正确。
- P2-1：原始 release reject 不重放；派发后仅等待被闸门提前结束、raw 仍 pending 时保留 held 并在落定后收敛；派发前被拒保留一次重试；engine dialog 顺序污染消失。
- P1-3：文案与事实一致；同一请求不再跨页面结果重复计数。
- 契约/后端/类型/构建/lint 全绿（232 / 97 / tsc ×2 / build / ruff / eslint / diff --check）；无新依赖、目录或改名。
- 工作树事件：18:02–18:03 三个后端 browser 生产文件曾被外部并发进程回退到 HEAD；Lead 18:07 重放后，18:07/18:19/18:23/18:24/最终采样 SHA-256 一致（`8b54d50a…` rendering、`7d4f3fad…` logs、`756acfb9…` tool_models/logs），之后未被再回退。

### 14.3 交付状态

- 验收结论：**P1 可以交付**（A/B 两路均最终通过；唯一 engine 失败为既有环境显示差异）。
- 本次改动未提交 git；`docs/plan/browser-p1-remediation-plan.md` 与两个测试文件为新增文件，其余为修改。工作区里另有一批与本任务无关的未提交改动，未被触碰。

# 子 Agent Timeline 展示重构方案（前端）

> 决策日期：2026-08-14
> 决策依据：用户确认 + 第零铁律（以长期稳定迭代为尺，改动聚焦、单一职责）
> 状态：方案已通过独立审查预核（对照真实代码签名修正）

## 一、已确认决策（用户拍板）

1. **主 timeline 的 delegation 行只显示 title**：图标 + 子 Agent 名 + 状态徽章；但**终态 summary/error 文字保留**在行内作为补充信息。
2. **去掉内联展开**：主 timeline 内不再就地渲染 child 完整消息流。
3. **右侧栏 Subagent 面板提升为独立 Tab**：与 Outputs / Sources 平级；点击 delegation 行 → 自动切到该 Tab 并渲染对应子 Agent 的 timeline。
4. **侧边栏同时只看一个 Agent 的 timeline**，点击不同 delegation 行可切换查看不同子 Agent 的实时 timeline（复用现有 `selectedChildTurnId` 机制）。
5. **并发泳道保留（选项 A）**：多个 sibling delegation 并发时，保留左边框泳道 +「并发 x/y」归属徽章（已核对该判定仅依赖 `concurrencyGroupSize`/`concurrencyIndex`，不依赖 childEntries，删内联展开后不受影响）。

## 二、后端是否需要改动

**结论：后端零改动。**（CodeGraph status 确认现有数据流天然支持）

- 子 Agent 实时消息流经 `RuntimeEventBus → SSE → eventStore.eventsByTurnId[turnId]` 按 turn_id 索引订阅；每个子 Agent 是独立 child turn。
- `SubagentPanel` 现已直接读 `eventStore.eventsByTurnId[selectedChildTurnId]` 做流式渲染，机制已存在。
- 要删的是「前端内联透传」机制（见第三节），属纯前端，不触碰后端事件/API/SSE 协议。

## 三、真实代码签名对照（审查修正，避免误删）

| 文件 | 实际签名（删除目标） | 删除 or 保留 |
|------|---------------------|-------------|
| `DelegationTimelineEntry.tsx` | `childEntries?: ReactNode` prop（真实行94，props 块第31-32行）；`useState`/`isOpen`/`hasChildEntries`/`Children` 展开分支（真实行98-99、116-133、187-191）；`Expand delegated child events` 箭头 `aria-label`（真实行126，含 `aria-expanded` 第127行）；组件 docstring（真实第60-86行，其中内联展开兄弟按钮描述在第68-70、77行，"维护本地展开/折叠状态"在第85行；并发指示说明在第73-76行） | **删除** prop + 展开分支 + 箭头；**重写** docstring（仅删内联展开/兄弟按钮/本地展开状态描述，保留并发指示说明） |
| `TurnTimeline.tsx` | `TurnTimelineProps` 真实第40行（含 `getChildEvents`/`childEventsRevision` 两 prop）；内部 `renderChildEntries` useCallback（真实行180-199）；`TimelineEntry` 的 `renderChildEntries?: (childTurnId)=>ReactNode` 形参（真实第282-290行，memo 包裹第282行）；`TimelineEntry` 调用透传（真实行243）；`DelegationTimelineEntry` 实参 `childEntries={delegation.childTurnId ? renderChildEntries?.(delegation.childTurnId) : undefined}`（真实第360行）；泳道左边框 `isConcurrencyLane` 判定（真实行347-348，依赖 `delegation.concurrencyGroupSize` 不依赖 childEntries） | **删除** 两个 prop + 内部回调 + `TimelineEntry` 形参与透传 + 实参；**保留** 泳道 |
| `ChatPanel.tsx` | 函数 `buildDelegationChildEventsRevision`（行309-336）；`getChildEvents` useCallback（行168-170）；`renderTurnItem` 内 `childEventsRevision` 局部变量与 `TurnTimeline` 透传（行173-180）；未用导入清理（行10 附近） | **删除** 函数 + useCallback + 局部变量 + 透传；清理导入 |
| `RightPanel.tsx` | 非受控 `Tabs defaultValue="outputs"`（真实第71行）；`SubagentPanel` 嵌套在 `SourcesTab` 内（真实第110行）；Tab 列表仅 Outputs/Sources 两个 trigger（真实第76-83行） | **改** 为受控 `Tabs value` + 订阅 `selectedChildTurnId`；**移出** SubagentPanel 到独立 TabsContent |
| `SubagentPanel.tsx` | 已支持按 `selectedChildTurnId` 流式渲染；空态（行114-123）；docstring `@returns` 称「属于 SourcesTab 子树」（行60）；内部并发 sibling `tab` 切换读 `delegationStore.selectedChildTurnId` 并用 `selectChildTurn`（第82-85、127-129行） | **改 docstring**（子树→独立 Tab）；逻辑无需改（sibling tab 已受控于 store，与面板级 Tab 正交） |

> 关键修正：原方案误将 `renderChildEntries`（内部 useCallback）与 `childEventsRevision`/`getChildEvents` 并列为 `DelegationTimelineEntry` 的 prop；实际 delegation 的 child 渲染走 `TurnTimelineImpl.renderChildEntries → TimelineEntry.renderChildEntries → DelegationTimelineEntry.childEntries` 三层链路，删链必须覆盖 `TimelineEntry`。

### 跨文件删 prop 配对约束（已用真实文件核实行号）
- `TurnTimeline` 侧删除点（真实 `TurnTimeline.tsx`，已用 CodeGraph 核实）：`TurnTimelineProps` 真实第40行（含 `getChildEvents`/`childEventsRevision` 两 prop 及其类型）、`TurnTimelineImpl` 解构（真实第66行含这两项）、`renderChildEntries` useCallback（真实第180-199行，其依赖数组 `[childEventsRevision, getChildEvents, handleOpenFile]` 第198行）、`TimelineEntry` 形参 `renderChildEntries` 与透传（真实第285、243行）、`DelegationTimelineEntry` 的 `childEntries` 实参（真实第360行 `childEntries={delegation.childTurnId ? renderChildEntries?.(delegation.childTurnId) : undefined}`）。
- `ChatPanel` 侧删除点（真实 `ChatPanel.tsx`）：`getChildEvents` useCallback（第168-170行）、`renderTurnItem` 内 `childEventsRevision` 局部变量（第173行）+ `TurnTimeline` 透传（第179-180行）、`buildDelegationChildEventsRevision` 函数（第309-336行）。
- **配对完成判据**：两侧删除必须在同一提交内完成，落地后 `tsc --noEmit` 零报错为配对完成标准。注意 `TurnTimeline` 删 `getChildEvents`/`childEventsRevision` 解构后，`renderChildEntries` useCallback 的依赖数组（第198行）同步去掉这两项，`handleOpenFile`（第176-178行，依赖 `[]`）保留。
- **`TimelineEntry` 删除范围实证（已核实无悬空）**：`handleOpenFile` useCallback 依赖数组为 `[]`（第178行），完全不依赖 `childEntries`/`getChildEvents`；`TimelineEntry` 的 `memo` 包裹（第282行）与 `onOpenFile` 引用稳定性逻辑（第173-178行）独立。故「仅删 `renderChildEntries` 形参+透传，保留 memo 与 onOpenFile」**安全，无悬空引用**。

### 两层 Tab 正交说明（问题 D 实证）
- `RightPanel` 的受控 Tabs（outputs/sources/subagent）是**面板级标签**；`SubagentPanel` 内部的并发 sibling `tab` 列表（真实 `SubagentPanel.tsx` 第82-85行 `siblings` 派生 + 第127-129行注释）是**并发子 Agent 切换 tab**，二者正交。
- `SubagentPanel` 的 sibling tab 直接读 `delegationStore.selectedChildTurnId` 并经 `selectChildTurn` 切换（source of truth 始终是 `delegationStore`），不维护独立的「面板 Tab 状态」。提升为独立 RightPanel Tab 后，该 sibling tab 行为不变，无需「禁止 SubagentPanel 维护 Tab 状态」。方案不引入双源状态。

## 四、改动清单（文件级，改动聚焦）

| 文件 | 改动类型 | 具体改动 |
|------|---------|---------|
| `DelegationTimelineEntry.tsx` | 改 | ① 删 `childEntries` prop（真实行32）及注释；② 删折叠箭头 `aria-label="Expand delegated child events"`（真实行126，含 `aria-expanded` 第127行）+ 展开态 `useState`/`isOpen`/`hasChildEntries`/`Children` 渲染分支（真实行98-99、116-133、187-191）；③ 整行点击派发（可达性约束）：**保留现有原生 `<button type="button">`（真实行135-167，已包 childAgentId + 主区域，自带 Enter/Space + `aria-current` 选中态）作为唯一键盘可达入口，不得退化为裸 `div+onClick`**；实现方式 = 仅删兄弟折叠箭头（116-133）与其展开分支（187-191），**不动**第135-167行这个原生 button 的 DOM 层级与 onClick（`if (childTurnId) selectChildTurn(childTurnId)` 守卫已在第137-141行，缺失时不触发、不崩）；整行"点击区"视觉上即该 button 覆盖主区域，无需改动外层 `div`（第110-111行）；④ 保留 title（图标+Agent名+状态徽章）+ 终态 `summary`/`error` 文字；⑤ 并发泳道「并发 x/y」徽章保留（真实行103 `showConcurrency` 判定 + 第156-160行渲染，**来自 props，与 DOM 层级/删除分支无关，不受影响**）；⑥ **重写组件 docstring**（真实第60-86行）：仅删「内联展开兄弟按钮（68-70）」与「维护本地展开/折叠状态（85）」描述，**保留并发指示说明（73-76）**，改为「整行按钮派发 + 终态文字 + 并发泳道」；⑦ 清理不再使用的 `Children`/`useState` 导入（第1行；`memo`/`ReactNode` 仍使用保留）。 |
| `TurnTimeline.tsx` | 改 | ① 删 `TurnTimelineProps.getChildEvents`/`childEventsRevision` 两 prop 及类型（真实 `TurnTimelineProps` 第40行内）；② 删 `TurnTimelineImpl` 解构中的这两项（真实第66行）；③ 删内部 `renderChildEntries` useCallback（真实行180-199）；④ 删 `TimelineEntry` 的 `renderChildEntries` 形参（真实第285）与第243行透传（真实行239-244）；**仅删形参+透传，保留 `TimelineEntry` 的 `memo` 包裹（第282行）与 `onOpenFile` useCallback 引用稳定性逻辑**；⑤ 删 `DelegationTimelineEntry` 的 `childEntries` 实参（真实第360行）；⑥ `delegations` 渲染简化为纯 title 行（保留 `isConcurrencyLane` 泳道判定，真实行343-348，依赖 `delegation.concurrencyGroupSize` 不依赖 childEntries）；⑦ 同步更新相关 props/docstring 注释。 |
| `ChatPanel.tsx` | 改 | ① 删函数 `buildDelegationChildEventsRevision`（行309-336）；② 删 `getChildEvents` useCallback（行168-170）；③ 删 `renderTurnItem` 内 `childEventsRevision` 局部变量与 `TurnTimeline` 三 prop 透传（行173-180）；④ 清理未用导入。 |
| `RightPanel.tsx` | 改 | ① `Tabs` 改为受控 `value`（真实第71行 `defaultValue="outputs"` → 改 `value={selectedChildTurnId ?? "outputs"}`），订阅 `useDelegationStore(s=>s.selectedChildTurnId)`（真实已 useDelegationStore 在第74行，复用同一 hook），非空时切到 `subagent`；② 新增 `SubagentTab` TabsTrigger/TabsContent（与 Outputs/Sources 平级，Tab 列表真实第76-108行）；③ 将 `<SubagentPanel />` 从 SourcesTab 内（真实第110行）移出到独立 `subagent` TabsContent；④ 更新组件内注释。**受控 Tab 边界（阻塞项）**：`value` 初值 `selectedChildTurnId ?? "outputs"`（为空时默认停 outputs，避免无选中跳空 Subagent Tab）；`SubagentPanel` 在独立 TabsContent 下切 Tab 不丢选中（选中态来自 `delegationStore` 全局订阅）；为免流式进度在切回时重置，落地时评估 `forceMount`+CSS 隐藏（若切 Tab 导致流式重置则采用）；手动验收须含「切 Tab 不丢子 Agent 流式进度」。 |
| `SubagentPanel.tsx` | 查（仅改 docstring） | 逻辑不变，仅改 docstring（「属于 SourcesTab 子树」→「独立 RightPanel Tab」）。内部并发 sibling `tab` 切换（真实第82-85行 `siblings` 派生 + 第127-129行注释）直接读 `delegationStore.selectedChildTurnId` 并经 `selectChildTurn` 切换，source of truth 始终为 `delegationStore`，**不维护独立面板 Tab 状态**，与 RightPanel 面板级 Tab 正交；提升为独立 Tab 后该 sibling tab 行为不变，无需改逻辑。 |
| `delegationStore.ts` | 不改动 | `selectChildTurn`/`selectedChildTurnId`/`clearSelection` 已具备。 |
| `delegationTimelineEntry.test.tsx` | 改 | 删除 `expands delegated child entries`（行49-65）、键盘焦点回归（行67-86）两用例（依赖 `childEntries` 与展开箭头，将失效）；保留/调整 title+终态文字（行13-47）、并发泳道（行88-127）；不传 `childEntries` prop。 |
| `subagentPanel.test.tsx` | 改 | 保留现有 `DelegationTimelineEntry` 联动断言（aria-label 不变仍可过，行29-71）；**新增**「点击整行 → selectChildTurn 调用」用例（验证键盘可达入口派发）；**新增**「空 childTurnId 点击不触发 selectChildTurn」用例（验证守卫，对应问题6）；确认引用 props 与新签名一致。 |
| `turnTimeline.delegation.test.tsx` | 改 | 删除两用例整体（行37-88 `renders expanded child turn events...`、行90-152 `refreshes child entries when only child event revision changes`，均验证将被移除的内联展开/childEventsRevision 能力）；**替换为 delegation 纯 title 渲染回归用例**（避免删用例后 delegation 渲染无回归保护）：`render(<TurnTimeline turn delegationEvents/>)` 后断言 `expect(screen.queryByLabelText("Expand delegated child events")).toBeNull()`（内联展开 DOM 已移除）且 `expect(screen.getByText("completed")).toBeTruthy()`（终态文案保留）。 |

## 五、单一职责校验

- `DelegationTimelineEntry`：收窄为「主 timeline 单条 delegation 标题行渲染 + 整行点击派发」，不再承担 child 消息流渲染。
- `TurnTimeline`：只按 turnId 渲染主 timeline 各类型 entry；delegation 子流渲染职责下沉到 `SubagentPanel`。
- `RightPanel`：只做 Tab 编排；Subagent Tab 内容委托 `SubagentPanel`。
- 删除的 `getChildEvents`/`childEventsRevision`/`renderChildEntries` 透传链：原属越界职责，删除后主/侧职责边界清晰。

## 六、不重复造轮子校验

- 子 Agent timeline 渲染复用既有 `TurnTimeline`（按 turnId 复用同一套 entry 渲染），不新写第二套渲染器。
- `SubagentPanel` 复用既有 `eventStore.eventsByTurnId` 订阅 + `delegationStore.selectedChildTurnId`，不新建事件通道。

## 七、可排查性

- 纯前端交互改动，无新增后端日志需求。

## 八、验收标准（独立审查 + 测试闭环）

### 审查维度（对照编码规范）
- [ ] 每个文件职责可用一句话（不含「和」）描述。
- [ ] 无跨层/越界职责（主 timeline 不渲染 child 流，child 流只在 Subagent Tab）。
- [ ] 无重复实现（不出现第二套 timeline 渲染；已核实 `SubagentPanel` 复用 `TurnTimeline` 与 `eventStore.eventsByTurnId`）。
- [ ] 函数/docstring 完整且与实现一致（删 prop 后同步重写 `DelegationTimelineEntry`/`SubagentPanel` docstring；重写时**保留并发指示说明**，仅删内联展开描述）。
- [ ] 错误处理完整（整行点击保留 `childTurnId` 守卫，缺失时不触发选中，不崩）。
- [ ] **ARIA 可达性不退化**：整行点击仍保留原生 `<button>` 键盘可达入口（Enter/Space + aria-current），不得退化为裸 div+onClick（第零铁律·改动聚焦，问题6）。
- [ ] 并发泳道保留且渲染正确（已核实依赖 `concurrencyGroupSize`/`concurrencyIndex`，不依赖 childEntries，删内联展开后不受影响）。
- [ ] 终态 summary/error 文字保留在行内。
- [ ] 删除彻底：`TimelineEntry.renderChildEntries`、`TurnTimeline` 两 prop、`ChatPanel` 函数/useCallback/局部变量均无悬空引用；**且 `TurnTimeline` prop 删除与 `ChatPanel` 透传删除成对完成（落地后跑 tsc 验证）**。
- [ ] **受控 Tab 边界明确**：`RightPanel` 受控 `value` 初值 `selectedChildTurnId ?? "outputs"`；切 Tab 不丢子 Agent 流式进度（评估 forceMount）。`SubagentPanel` 内部 sibling tab 与 RightPanel 面板级 Tab 正交，不引入双源状态（source of truth 始终为 `delegationStore`）。

### 测试维度
- [ ] `delegationTimelineEntry.test.tsx`：不传 childEntries；点击整行触发 selectChildTurn；无内联展开 DOM；并发泳道存在。
- [ ] `turnTimeline.delegation.test.tsx`：移除内联展开用例后通过。
- [ ] `subagentPanel.test.tsx`：切换 selectedChildTurnId 渲染不同 Agent；新增整行点击用例。
- [ ] vitest 全绿 + 前端 lint 通过。

### 手动验收（桌面端）
- [ ] 主 timeline delegation 行只显示 title + 终态文字，无内联展开箭头。
- [ ] 点击 delegation 行 → 右侧栏切到 Subagent Tab 并流式渲染该子 Agent timeline。
- [ ] 点击不同 delegation 行 → 侧边栏切换查看不同子 Agent。
- [ ] 并发场景：泳道左边框 +「并发 x/y」徽章保留。

## 九、落地顺序

1. 写方案文档（本文件，已通过预审查修正）。
2. 主 Agent 按第三节签名对照 + 第四节清单逐文件改码（同步重写 docstring、清理导入、删测试失效用例）。
3. 改完启动独立审查子 Agent + 独立测试 Agent，直到「符合」+ 测试通过。
4. 闭环交付。

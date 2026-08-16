# coding-agent 桌面端 UI 重构计划

> 目标：以 `/g/code/deepseek-harness` 的成熟 UI 为**模式字典（pattern dictionary）**，
> 在 `/h/coding-agent` 现有 Tauri + React + Tailwind 技术栈内，**重写而非引入**其 UI，
> 完成一次整体 UI 重构（交互范式 + 界面）。
>
> 核心约束：不引入 harness 的任何 npm 包（其 UI 强耦合 cordis 插件运行时，无法接入
> Tauri + zustand）；不复制其 12px 大圆角 / 彩色化视觉；保持本项目
> `docs/ui-guidelines.md` 的克制基调（灰阶、≤4px 圆角、无阴影、彩色面积 <5%）。
>
> 文档版本：v2（2026-08-15 基于实际代码核对修订）。v1 中若干"问题定性"与代码现状不符，
> 已在 §0 逐条纠正，**修订点以「（修订）」标注**，请勿照搬 v1 的前提。

---

## 0. 诊断：问题不在骨架，在主线（已基于代码核对修订）

当前布局与 harness 同属**三栏范式**（Sidebar | ChatPanel | RightPanel），骨架正确。
但 v1 把"右栏 6+ tab""四条分散时间线""组件大面积违反 token"作为前提，经 2026-08-15
核对源码，**这三点均与现状不符**，见下。

### 0.1 信息架构：不是"tab 太多"，而是"常驻 tab 多为空 / 脱离上下文"（修订）

核对 `components/layout/RightPanel.tsx` 与 `components/right-panel/*` 后，右栏真实状态为：

- **只有 3 个常驻 tab**：`Outputs` / `Sources` / `Subagent`。
- `Changes` **已不在右栏**——迁移到对话区顶部的 `ChangesDrawer`（`layout/ChangesDrawer.tsx`，默认收起）。
- `Context` / `Mcp` **不是 tab**，而是 `Sources` tab 内的两个占位区块（`ContextBlock.tsx` / `McpBlock.tsx`，标注"即将上线"）。
- `Outputs` / `Sources` 当前是 **mock 静态数据**（`RightPanel.tsx` 内 `MOCK_OUTPUTS` / `MOCK_SOURCES`）。
- `Subagent` tab 是**唯一有真实数据**的：由 `delegationStore.selectedChildTurnId` 驱动，点击父时间线的 delegation 行即自动切到该 tab（`RightPanel.tsx:77-89`）。这本身就是 harness「情境化 details」的雏形。
- `ConversationTraceBlock.tsx` 是 **0 字节空文件**，不在任何渲染路径中。

**结论**：真正的问题不是"6 个 tab 过载"，而是"右栏缺乏以「选中」为驱动源的情境化，
且多数区块仍是空壳/mock"。Phase 1 应做的是**把右栏从「固定 tab 架」改为「情境化检视面」**，
而非简单删 tab。SubagentPanel 已证明该模式在本栈可行。

### 0.2 交互：InputBar 仍是裸输入，但已非"白纸"（修订）

核对 `components/layout/InputBar.tsx`：当前是受控 `Input` + 发送/停止按钮，已集成
`AgentSelector`（模型/agent 选择）、`ContextUsageRing`（上下文占用）、`canSend/canStop`
守卫、IME 安全的 Enter 发送。**缺的不是输入框本身，而是**：

- 无 **slash 命令组合框**（`/` 或 `>` 触发命令菜单、焦点不离开输入框）。
- 无 **全局命令面板（Cmd/Ctrl+K）** 与键盘导航。
- 这些叠加在现有 InputBar 之上即可，不必重做输入区。

### 0.3 时间线：所谓"四条"其实已大部分统一（重要修订）

v1 称"时间线散在 4 处：LogsPage / TurnTimeline / DelegationTimeline / trace store"。
核对后实际情况：

- **统一投影管线已存在**：`services/timeline/projector.ts` 把 turn 记录 + runtime event
  投影成 `TurnTimelineEntry` / `TimelineToolItem` / `TimelineDelegationItem`；
  `groupTools.ts` 把连续 tool 段聚合成 `toolGroup`；`eventStore.eventsByTurnId` 按 turn
  分片存储；`layout/TurnTimeline.tsx` 消费投影结果渲染。这是 harness `ui-trajectory`
  的等价物，且已是**权威单一数据源**。
- **父子已打通**：`right-panel/SubagentPanel.tsx` 直接复用 `TurnTimeline` 渲染选中的
  child turn（`SubagentPanel.tsx:17-30` 注释明确"不再经 projectTurnTimeline 全量投影"），
  并复用 `projector.DELEGATION_STATUS_UI` 单一映射。
- **LogsPage 是原始日志查询工具**（`pages/logs/LogsPage.tsx`，按 `trace_id` 查后端日志），
  属**调试/诊断**用途，**不应并入用户向的执行轨迹 spine**。
- **conversationTraceStore / clientTraceStore 是性能遥测**（PerfTrace / langfuse trace），
  **不是执行时间线**，不该出现在用户轨迹视图里。

**结论**：Phase 3 的真实工作量远小于 v1 所述。剩余工作是：(a) 把"父 TurnTimeline +
子 SubagentPanel"两套渲染收敛为**单一递归 TrajectoryView**（支持 delegation→child→
其自身 delegation 任意嵌套），消除 SubagentPanel 这个特例分支；(b) 明确 LogsPage 与
遥测的走向（见 §4.3 / §5 决策点）。

### 0.4 视觉纪律：组件树已基本合规，无需大扫除（重要修订）

v1 称"清掉硬编码 `bg-[#...]` / `rounded-[12px]` / `shadow`"。核对 `components/**/*.tsx`
（Phase 0 审计见 §4.5）结论：

- **未发现任何 `bg-[#...]` / `text-[#...]` / `border-[#...]` 硬编码色值**。
- **未发现 `rounded-[12px]` 之类大圆角**（仅 `scroll-area.tsx:31` 的 `rounded-[inherit]`
  为无害继承）。
- 唯一自标"待收敛"的是 `InputBar.tsx:145` 的 `min-h-[36px]`（注释已写"待收敛到 token"）。
- 全局颜色/圆角/边框均走 `index.css` 令牌 + `tailwind.config.ts`。

**结论**：Phase 0 不是"大扫除"，而是**小修 + 一致性收口 + lint 护栏**：
(1) 收口 2~3 处标注过的任意值；(2) 统一"终态/运行状态 → 徽章 variant"映射
（见 §4.5，当前 `StatusBadge` 与 `DELEGATION_STATUS_UI` 的 variant 集合不一致）；
(3) 加 `tailwind/no-arbitrary-value` 硬性护栏，把现有 `eslint-disable` 注释收敛为白名单。

---

## 1. 重构路线（按性价比排序，前提已修订）

### Phase 0 — 视觉纪律收口（小修 + 一致性，零新功能）

- **目标**：把 v1 夸大的"大扫除"收敛为可控的小修，并堵住回归通道。
- **关键改动**：
  1. 收口 `InputBar.tsx:145` 的 `min-h-[36px]` → 新增 token（如 `tokens.ts` 的
     `Input.minHeight`）或复用现有；`ChangeCheckpointSelect.tsx:40` 的 `w-[160px]`、
     `AgentSelector.tsx:81` 的 `max-w-[80px]` 若属稳定布局尺寸，沉淀为具名 token。
  2. 统一状态→variant 映射：以 `projector.DELEGATION_STATUS_UI` 为事实源，
     `StatusBadge` 的 `FINAL_STATUS_CONFIG` 对齐其 variant 集合（补 `warning` /
     `secondary` 分支），避免两处语义漂移。
  3. 引入 `tailwind/no-arbitrary-value` 规则，仅放行已登记的白名单（如 `max-h-[40vh]`、
     `w-[calc(...)]`），其余报错。
- **从 harness 复刻要点**：无（harness 侧无对应，本阶段纯整改）。
- **关键文件清单**：`components/ui/tokens.ts`、`components/chat/StatusBadge.tsx`、
  `services/timeline/projector.ts`（`DELEGATION_STATUS_UI`）、`InputBar.tsx`、
  `.eslintrc` / 对应 config、`index.css`。
- **验收标准**：
  - 全项目 `components/**` 无 `bg-[#`/`text-[#`/`border-[#` 字面色值。
  - 无 `rounded-[Npx]`（N≥8）字面类；`shadow` 仅出现在 popover/tooltip（见 §3 红线）。
  - `StatusBadge` 与 `DELEGATION_STATUS_UI` variant 字段集合一致，无 `as const` 越界。
  - CI lint 对新增任意值报错（已有 `eslint-disable` 注释列入白名单）。

### Phase 1 — RightPanel 改造成「情境化 details 面」（最大 IA 收益）

- **目标**：右栏从"固定 3-tab 架"改为**单一情境化检视面**——当前选中（对话中的工具卡 /
  委派行 / 变更文件 / 来源项）决定显示内容，无选中时显示常驻摘要。
- **现状核对**：见 §0.1。SubagentPanel 已是情境化范例（由 `delegationStore` 驱动）。
- **关键改动**：
  1. 新增 `selectionStore`（zustand）：`{ kind: 'tool'|'delegation'|'file'|'source'|'none', refId }`。
     对话内 `ToolCallCard` / `DelegationTimelineEntry` / `ChangesDrawer` 文件行 / `SourcesTab`
     点击 → `select(...)`；右栏订阅并渲染对应 surface。
  2. 把现有 `Outputs`/`Sources`/`Subagent` 三 tab 改为"选中驱动"的 surface 注册表，
     不再用 Radix Tabs 强切；`ContextBlock` / `McpBlock` 作为 `source`/`none` surface 的子块。
  3. **关闭时不卸载**：右栏 `Panel` 设 `collapsible collapsedSize={0} minSize={DETAILS_MIN}`，
     内容始终挂载，`width:0` 仅视觉收起（学 harness `DetailsColumn` 的"width 0 keeps mounted"）。
  4. **常驻摘要**：变更数已由 `ChangesDrawer` 触发器承担、上下文用量已由 `InputBar`
     的 `ContextUsageRing` 承担——**因此 v1 设想的"额外 slim rail"大概率冗余**（见 §5 决策点）。
     若确需，仅在右栏折叠态顶部放一条极窄摘要条（变更数 + 上下文%）。
- **从 harness 复刻的实现要点**（伪代码/结构，非完整组件）：
  ```ts
  // 右栏渲染：选中驱动，而非 tab 枚举
  function RightPanel() {
    const sel = useSelectionStore(s => s.selection)
    return (
      <aside data-open={sel.kind !== 'none'}>
        {sel.kind === 'none' && <DetailsSummary />}      // 折叠/无选中时的常驻摘要
        {sel.kind === 'tool'    && <ToolIODetail refId={sel.refId} />}
        {sel.kind === 'delegation' && <SubagentPanel childTurnId={sel.refId} />}
        {sel.kind === 'file'    && <FileDiffDetail refId={sel.refId} />}
        {sel.kind === 'source'  && <SourceDetail refId={sel.refId} />}
      </aside>
    )
  }
  // 复用既有：ToolIODetail 内联 ToolCallCard 的展开态；FileDiffDetail 复用 ChangesPanel 的单文件视图
  ```
- **关键文件清单**：`stores/selectionStore.ts`（新）、`components/layout/RightPanel.tsx`、
  `components/right-panel/SubagentPanel.tsx`、`components/chat/ToolCallCard.tsx`、
  `components/chat/DelegationTimelineEntry.tsx`、`components/layout/ChangesDrawer.tsx`、
  `components/right-panel/OutputsTab.tsx` / `SourcesTab.tsx` / `ContextBlock.tsx` / `McpBlock.tsx`、
  `App.tsx`（右栏 `Panel` 加 `collapsible`）。
- **验收标准**：
  - 点击对话内工具卡 → 右栏显示该工具完整 I/O；点击 delegation 行 → 右栏显示子 agent 时间线。
  - 右栏关闭（width:0）后，重新打开不丢失已加载的子时间线/滚动位置（mounted 验证）。
  - 无选中时右栏呈现常驻摘要，不空白不报错。
  - 现有 SubagentPanel 行为（含并发 sibling tab）完全保留，测试 `subagentPanel.test.tsx` 等不破。

### Phase 2 — Slash 命令组合框 + Cmd+K 全局面板（最大交互收益）

- **目标**：InputBar 升级为 combobox 式 composer；新增全局 overlay 层承载命令面板。
- **现状核对**：见 §0.2。InputBar 已含 AgentSelector / 发送停止 / ContextUsageRing，需**叠加**而非重做。
- **关键改动**：
  1. **命令注册表**（单一数据源，slash 与 Cmd+K 共用）：
     ```ts
     type Command = { id: string; title: string; icon?: LucideIcon;
                      keywords?: string[]; run: (ctx) => void }
     const COMMANDS: Command[] = [ /* 见 §4.2 */ ]
     ```
  2. **Slash 组合框**（复刻 harness `ui-input-trigger/MenuView.tsx` 的 combobox 模式）：
     - InputBar 内：当输入以 `/` 或 `>` 开头且在行首 → 弹出候选菜单（锚定在输入框上方）。
     - **焦点不离开输入框**：菜单项用 `onMouseDown` + `preventDefault` 选取（不抢焦点）；
       高亮项经 `aria-activedescendant` 暴露；`↑/↓` 导航、`Enter` 执行、`Esc` 关菜单。
     - 高度用 `useAnchoredMaxHeight` 思路（按输入框上方可用空间 clamp，上限 ~320px）。
       点击输入框 / 底栏不关闭菜单（harness `MenuView.tsx:54-65` 的 pointerdown 判定）。
  3. **Cmd/Ctrl+K 全局 overlay**（复刻 harness `shell.overlay` 层）：
     - 在 `App.tsx` 根部新增 `OverlayLayer`（`fixed inset-0 z-[...]` Portal，高于三栏）。
     - 面板内模糊搜 `COMMANDS` + 跳任务 / 跳文件 / 切项目（数据源复用 `taskStore` /
       `workspaceStore` / `ChangesPanel`）。
- **从 harness 复刻要点**：`MenuView.tsx` 的 `useSyncExternalStore` 订阅菜单 store、
  `optionId`/`aria-activedescendant`、`mousedown` 选取、`useAnchoredMaxHeight` 高度 clamp；
  `AppFrame.tsx` 的 `shell.overlay` 层定位（见 §4.4 快捷键与 overlay 共存）。
- **关键文件清单**：`components/layout/InputBar.tsx`、`services/commands.ts`（新，注册表）、
  `components/command/SlashMenu.tsx`（新）、`components/command/CommandPalette.tsx`（新）、
  `App.tsx`（OverlayLayer）、`hooks/useHotkeys.ts`（新，见 Phase 4）。
- **验收标准**：
  - 输入 `/` 弹出命令菜单，键盘 `↑/↓/Enter/Esc` 全可操作，焦点始终在输入框。
  - `Cmd/Ctrl+K` 在任何视图/焦点态下都能唤起面板；面板内可搜命令并跳转。
  - 菜单选取后输入框内容/光标正确（如 `/changes` 不残留字面 `/changes`）。
  - 不破坏既有 IME Enter 守卫（`InputBar.handleKeyDown` 的 `isComposing` 判定保留）。

### Phase 3 — 轨迹 spine 统一（收敛父/子渲染为单一递归组件）

- **目标**：把"父 `TurnTimeline` + 子 `SubagentPanel`"两套渲染收敛为**单一递归
  `TrajectoryView`**，支持 delegation→child→其自身 delegation 任意嵌套；明确 LogsPage
  与遥测的走向。
- **现状核对**：见 §0.3。投影管线已统一，缺的是**渲染层递归化**与**范围界定**。
- **关键改动**：
  1. 抽 `TrajectoryView({ turnId })`：内部复用 `eventStore.eventsByTurnId[turnId]` +
     `projectTimelineIncrementally`，遇到 `delegation` 项时递归渲染 `TrajectoryView({ childTurnId })`
     （替代 SubagentPanel 特例）。
     **必须完整保留 SubagentPanel 现有三件关键行为**（已核对 `SubagentPanel.tsx`，不得丢）：
     - (a) **并发 sibling tab**：当选中 child 属并发组（同 parent 下 ≥2 running delegation）时，
       顶部以 tab 列出全部 sibling（`deriveSiblingDelegations` 派生），点击切换选中；
     - (b) **O(N) 派生降频**：sibling/status 两个全量事件扫描用 `useDeferredValue` 包裹
       （`SubagentPanel.tsx:83-84`），避免每帧重跑；child 分片（`childEvents`）刻意不降频以保证实时；
     - (c) **兜底 TurnRecord**：`turnStore` 无该 child 记录时 `createFallbackTurn` 构造兜底
       （status 按 `deriveChildDelegationStatus` 映射，绝不写死 "completed"）。
  2. **下钻落点（已核实）**：对话内 `DelegationTimelineEntry` 点击经
     `delegationStore.selectChildTurn(childTurnId)` 驱动右栏（非对话内联展开）。递归 `TrajectoryView`
     因此**渲染在右栏 `delegation` surface**（沿用该通道），而非 inline 展开——保持与现状一致。
     若未来想改"对话内联展开"（harness 默认），属独立设计决策，见 §5。
  3. `SubagentPanel` 退化为 `TrajectoryView` 在右栏的承载壳（或右栏 `delegation` surface 直接调 `TrajectoryView`）。
  4. **范围界定**（见 §5 决策点）：`LogsPage`（原始日志）**不并入** spine，作为诊断模式从
     Cmd+K `/logs` 进入；`conversationTraceStore` 遥测默认不暴露给用户。
- **从 harness 复刻要点**：`ui-trajectory` 的 `TrajectoryTurn` / `TrajectoryGroupHeader`
  / `TrajectoryTimeline` 的"turn→group→tool"层级与虚拟滚动（`trajectory-virtual-rows.ts`）；
  其中**虚拟滚动**是我们已用 `lib/virtual/VirtualList` 覆盖的，重点复刻层级结构与折叠态。
- **关键文件清单**：`components/trajectory/TrajectoryView.tsx`（新）、
  `components/layout/TurnTimeline.tsx`、`components/right-panel/SubagentPanel.tsx`、
  `services/timeline/projector.ts`、`services/timeline/groupTools.ts`、
  `stores/eventStore.ts`、`pages/logs/LogsPage.tsx`。
- **验收标准**：
  - 选中 delegation → 右栏递归展示该 child 的完整时间线，child 内再嵌 delegation 可继续下钻。
  - 既有 TurnTimeline 视觉/交互（toolGroup 折叠、状态点、流式增量）不变，测试 `turnTimeline.*` 不破。
  - SubagentPanel 的并发 sibling tab、loading 占位、O(N) 派生降频行为完整保留。

### Phase 4 — 布局引擎与键盘化（保留 react-resizable-panels，借鉴 harness 语义）

- **目标**：在现有 `react-resizable-panels` 之上补 harness `computeColumns` 的两点精髓
  （**窄屏自动收 sidebar** + **details 宽度 0 保活**），并全应用加键盘快捷键。
- **现状核对**：`App.tsx` 已用 `react-resizable-panels` 的 `Group/Panel/useDefaultLayout`
  **宽度持久化已具备**（`CHAT_LAYOUT_ID` / `FULL_LAYOUT_ID` 分视图存 localStorage）。
  `PanelDragHandle` 已封装 `Separator` 的拖拽高亮，非"ad-hoc"——v1 称"替换"有误，应为"增强"。
- **关键改动**：
  1. **窄屏自动收起**：新增 `useViewport()`（rAF 节流的 `ResizeObserver`，对齐 harness
     `AppFrame.tsx:111-128`）；`viewport < 1024`（harness `SIDEBAR_AUTO_COLLAPSE`）时
     编程式折叠 sidebar `Panel`（或其 `collapsedSize` 落为 rail）；窗口加宽自动恢复偏好。
  2. **details 宽度 0 保活**：右栏 `Panel` 设 `collapsible collapsedSize={0}`（Phase 1 已定）。
  3. **concession 链**：details 优先收缩/自动关闭、sidebar 不妥协——在 `onResize` 回调里对
     右栏做 clamp（参考 harness `computeColumns` 三步：先保 center≥640，再缩 details，再关 details）。
  4. **键盘化**：`useHotkeys` 注册（见 §4.4），overlay 打开时吞掉底层快捷键、Esc 关闭。
- **从 harness 复刻要点**：`columns.ts` 的纯函数求解（`clampWidth` / 三步 concession 链）、
  `AppFrame.tsx` 的 `narrow` 推导与 `setNarrow`、`DragHandle` 的 pointer-capture + rAF 节流
  （我们已由 `PanelDragHandle` 的 `data-separator` 状态承担，无需重写）。
  - **常量参考（照搬 harness 尺度，落到本项目 px）**：`CENTER_MIN=640`；
    `SIDEBAR_MIN=264 / MAX=420 / DEFAULT=280 / COLLAPSED=56 / AUTO_COLLAPSE=1024`；
    `DETAILS_MIN=300 / MAX=520 / DEFAULT=360`。让步链三步：① 保 center≥640，先缩 details 到 `DETAILS_MIN`；
    ② 仍不够则 details 推到 0（派生，不改偏好值，故窗口加宽自动恢复）；③ sidebar 永不妥协。
  - **关键差异**：harness 用 CSS Grid `gridTemplateColumns` 直接求解；我们用 `react-resizable-panels`
    的 `Panel`，故让步链要落在 `onResize`/`onLayout` 回调里对右栏做 clamp + `collapsedSize={0}`，
    而非自研 grid。语义一致，实现不同。
- **关键文件清单**：`App.tsx`、`hooks/useViewport.ts`（新）、`hooks/useHotkeys.ts`（新）、
  `components/layout/PanelDragHandle.tsx`、`stores/layoutStore.ts`（可选，承偏好）。
- **验收标准**：
  - 视口 < 1024px 时 sidebar 自动收为 rail；拉宽后恢复原宽（持久化不丢）。
  - 右栏可拖到 0 宽且内容不卸载（Phase 1 验收共用）。
  - 全局快捷键可用且不冲突（§4.4），overlay 开启时底层不误触发。

---

## 2. 值得直接复刻的 harness 组件（在本栈重写，标注覆盖度）

| 来自 harness | 落到本项目 | 覆盖度 | 关键注意 |
|---|---|---|---|
| `ui-input-trigger` slash 组合框（`MenuView.tsx`） | `InputBar` + 新 `SlashMenu` | **待做** | 复刻 combobox 模式（焦点不离开、aria-activedescendant、mousedown 选取、高度 clamp）；不引 cordis |
| `shell.overlay` 全局面板 | 新 `OverlayLayer` + `CommandPalette` | **待做** | 根部 fixed Portal；Cmd+K 唤起；不引 harness overlay store |
| `details` 情境化面（`AppFrame.DetailsColumn`） | RightPanel 重构 | **部分**（SubagentPanel 已雏形） | 用 `selectionStore` 驱动；`width:0` 保活 |
| `ui-trajectory` 轨迹层级 | `TrajectoryView`（递归） | **部分**（projector 已统一） | 复刻 turn→group→tool 层级与折叠；虚拟滚动已用 `VirtualList` |
| `StateDot` 四态点（done/warning/ongoing/error） | `components/ui/StateDot.tsx`（新） | **待做** | 仅四态**静态**点；**禁止复刻 harness 的 8 格像素追逐动画**（那是 stagger，违反 §五）；ongoing 用静态环或复用既有 `thinking-dot`；颜色映射见 §3 |
| `TerminalBlock` 卡（gutter 状态点 + banner + 复制） | `TerminalCallCard` | **已覆盖** | 核对 `TerminalCallCard.tsx` 已有 header/banner/CopyButton/**状态 pill**，但**无 gutter 运行态点**（已核实）；具体补强：① 在卡片左侧 gutter 插 `StateDot`（done/warning/ongoing/error），替代当前纯文字状态；② 运行中输出继续复用既有 `TerminalViewer`（xterm），终态复用 `<pre>`；③ 圆角改 4px（见 §3）；不重造卡 |

> 注：harness `TerminalBlock.module.css` 用 `--dsl-terminal-radius: 12px`——**不照抄**，
> 本项目统一 4px（§3 红线）。其 gutter/run-state 结构可借鉴，几何参数按本项目 token。

---

## 3. 红线（不可逾越，已对齐 ui-guidelines.md）

- ❌ 不 `import @deepseek-ai/dsh-*`——cordis 运行时耦合，接不进 Tauri。
- ❌ 不抄 12px 大圆角——TerminalBlock 等几何改为 4px（§七验收：圆角统一 ≤4px）。
- ❌ 不抄入场动画 / stagger——`ui-guidelines.md` §五明令禁止。
- ⚠️ **阴影**：仅"脱离文档流的浮层（popover / tooltip / overlay）"允许极轻阴影以区分层级
  （`ui-guidelines.md` §三第 2 段）；**面板 / 卡片 / 按钮一律无阴影**。v1 的"新建组件禁止
  写 shadow"过绝对，以此条为准。
- ⚠️ **状态语义色**：`success` / `warning` 按 §2.3 用 `emerald-*` / `amber-*` **低饱和浅底**
  表达，**不强制令牌化**；`destructive` 走 `--destructive` 令牌；`ongoing` 见下。
- ❌ 新建组件禁止写 `bg-[#...]` / `text-[#...]` / `border-[#...]` 字面色值；任意值走 lint 白名单（Phase 0）。

### StateDot 四态颜色映射（落地口径）

| 状态 | harness 语义 | 本项目落地 |
|---|---|---|
| done | 绿 | `emerald-*` 低饱和（对齐 `StatusBadge` 的 `success`） |
| warning | 琥珀 | `amber-*` 低饱和（对齐 `DELEGATION_STATUS_UI.warning`） |
| error | 红 | `--destructive` 令牌 |
| ongoing | 蓝（运行环） | **复用 `--primary`**（低饱和石板蓝，不引入新色）；**静态环**（border）或复用既有 `thinking-dot` 三点跳动（§五已允许作"仍在处理"信号），**禁止** harness 那种 8 格像素追逐（stagger，违反 §五） |

> **harness `StateDot.tsx` 复刻红线**：harness 的 `ongoing` 是 `viewBox 0 0 10 10` 里 8 个 `<rect>` 带 `animationDelay` 依次点亮的像素追逐——这是典型的 stagger/装饰性动效，`ui-guidelines.md` §五明令禁止。本项目的 `StateDot` `ongoing` **只做静态表达**，绝不移植该动画。
> ongoing 是否新增 `--info` 低饱和蓝、success/warning 是否正式令牌化，见 §5 决策点。

---

## 4. 待细化项落实（v1 §4 逐条展开）

### 4.1 RightPanel 各 tab 实际数据源与依赖盘点

| Surface | 当前数据源 | 真实度 | 依赖 | Phase 1 处置 |
|---|---|---|---|---|
| Outputs | `RightPanel.MOCK_OUTPUTS`（静态） | mock | 无 | 接 `ChangesPanel`/产物索引或并入 `file` surface；mock 阶段保留 |
| Sources | `RightPanel.MOCK_SOURCES`（静态） | mock | 无 | 接上下文引用索引；点击 → `source` surface |
| Subagent | `delegationStore.selectedChildTurnId` → `eventStore.eventsByTurnId[child]` → `TurnTimeline` | **真实** | delegationStore / eventStore / turnStore / projector | 保留为 `delegation` surface（递归） |
| Context | `ContextBlock` 占位 | 空壳 | 未来 context compaction 指标 | `none`/`source` surface 子块 |
| Mcp | `McpBlock` 占位 | 空壳 | 未来 MCP server 列表 | `none` surface 子块 |
| Changes（已迁出） | `ChangesDrawer` → `ChangesPanel`（真实变更集） | **真实** | useChanges / taskStore / ChangesPanel | 触发器点击文件 → 右栏 `file` diff surface |
| Trace | `ConversationTraceBlock.tsx`（0 字节，未挂载） | 死文件 | — | 删除或留作扩展位 |

> 关键结论：**只有 Subagent 与 Changes 是真实数据**；其余为空壳/mock。Phase 1 不必"砍 6 个
> tab"，而是让右栏在"有真实选中时呈现细节、无选中时呈现摘要"，空壳区块随各自数据源到位再填。

### 4.2 Slash 命令初版清单（与 Cmd+K 共用 `COMMANDS`）

> 全部建立在**已存在能力**之上，不新增后端。初版 8 条，建议先上核心 4 条快速见效（见 §5）。

| 命令 | 标题 | 触发动作 | 数据源/既有入口 |
|---|---|---|---|
| `/clear` | 清空上下文 | 清空当前对话上下文（保留任务） | taskStore / turnStore | ⚠️ 需确认后端是否支持"保留任务清空上下文"，否则降级为"新开任务" |
| `/logs` | 打开日志 | 切到 `LogsPage`（`App` view='logs'） | `pages/logs/LogsPage.tsx` |
| `/changes` | 展开变更 | 展开 `ChangesDrawer` | `layout/ChangesDrawer.tsx` |
| `/model` | 选择 Agent | 打开 `AgentSelector` | `chat/AgentSelector.tsx`（已集成 InputBar） |
| `/task <描述>` | 新建任务 | 等价于发送（createTask） | `hooks/useTask.ts` |
| `/switch <项目>` | 切换工作区 | 切 `activeWorkspaceId` | `Sidebar` `ProjectList` / workspaceStore |
| `/delegations` | 聚焦委派 | 滚动/展开最新 delegation | delegationStore |
| `/help` | 命令帮助 | 列出命令 | 复用 `COMMANDS` |

### 4.3 trajectory 与 `services/timeline/*` 的映射关系

```
runtime event ──► eventStore.eventsByTurnId[turnId]   （按 turn 分片，引用稳定）
        │
        ▼
projector.projectTimelineIncrementally / projectTurnTimeline
        │   产出 TurnTimelineEntry[]（message/tool/thinking/status/delegation）
        ▼
groupTools.groupConsecutiveTools  ──► RenderEntry[]（tool 段聚合成 toolGroup）
        │
        ▼
TurnTimeline（父）  ──►  遇 delegation 项  ──►  TrajectoryView(childTurnId) ［Phase 3 递归化］
        │                                            │
        ▼                                            ▼
   ChatPanel 渲染                              SubagentPanel（当前特例）/ 右栏 delegation surface
```
- **LogsPage（原始日志）不在此链**：它是后端 `fetchLogsByTrace` 查询，调试用途，不进 spine。
- **conversationTraceStore / clientTraceStore（PerfTrace 遥测）不在此链**：非执行时间线。
- 故 v1"合并四条时间线"实际 = (a) 渲染层递归化（Phase 3）；(b) 明确 LogsPage/遥测不并入。

### 4.4 键盘快捷键方案与冲突排查

| 快捷键 | 行为 | 实现 |
|---|---|---|
| `Cmd/Ctrl+K` | 唤起全局命令面板 | `useHotkeys` 监听；overlay 开启时吞底层 |
| `Cmd/Ctrl+B`（或 `` Cmd/Ctrl+` ``） | 切换 sidebar | 调 `Panel` collapse API |
| `Cmd/Ctrl+\` | 切换 RightPanel（details） | 调右栏 `collapsible` |
| `Esc` | 关 overlay / 收起 details（若开） | overlay 内拦截 |
| slash 菜单内 `↑/↓/Enter/Esc` | 导航/执行/关 | combobox 模式（Phase 2） |

**冲突排查结论**：当前全局**无既有快捷键**（仅 `InputBar` 内 `Enter`/`Shift+Enter`，且 IME 安全）。
→ 冲突风险低。但两点需老板核对（§5）：(1) Tauri `tauri.conf.json` 是否已注册全局
accelerator 占用 `Cmd+K` 等；(2) `Cmd+K` 在 InputBar 聚焦时仍需正常唤起（不落入输入）。

### 4.5 组件审计具体清单（Phase 0 依据）

扫描 `components/**/*.tsx` 的任意 Tailwind 值，结论：**基本合规，无需大扫除**。

| 文件:行 | 任意值 | 性质 | 处置 |
|---|---|---|---|
| `InputBar.tsx:145` | `min-h-[36px]` | 作者自标"待收敛到 token" | Phase 0 收口为 token |
| `ChangeCheckpointSelect.tsx:40` | `h-8 w-[160px]` | 固定宽 select | 稳定布局尺寸 → 具名 token 或保留并加白名单 |
| `AgentSelector.tsx:81` | `max-w-[80px]` | 标签截断 | 保留（截断语义），加白名单 |
| `ChangesDrawer.tsx:102` | `max-h-[40vh]` | 视口相对自适应（§2.1 例外） | 已 `eslint-disable`，保留白名单 |
| `RightPanel.tsx:101` | `w-[calc(100%-1rem)]` | 抵消父 margin 的 calc | 已注释，保留白名单 |
| `scroll-area.tsx:31` | `rounded-[inherit]` | 继承，无害 | 保留 |
| `popover.tsx:33` | `shadow-md` | **浮层阴影，§三允许** | 不违规，保留 |

- **零** `bg-[#...]` / `text-[#...]` / `border-[#...]` 硬编码色值。
- **零** `rounded-[12px]` 之类大圆角。
- 一致性缺口（非 token，而是逻辑）：`StatusBadge.FINAL_STATUS_CONFIG` 的 variant 集合
  （success/destructive/outline）与 `projector.DELEGATION_STATUS_UI`（outline/success/
  destructive/warning/secondary）**不一致**——Phase 0 统一。

### 4.6 各阶段验收标准

已内联到 §1 每个 Phase 的「验收标准」小节，此处不再重复罗列，避免漂移。

---

## 5. 待老板拍板的关键决策点（无法自行定论）

1. **右栏"额外 slim rail"是否必要？** 变更数已由 `ChangesDrawer` 触发器承担、上下文用量已由
   `InputBar` 的 `ContextUsageRing` 承担。Phase 1 默认**不新增**独立 rail，仅折叠态保留极窄摘要。
   若老板认为需要常驻"一眼看全局"的摘要条，再立项。
2. **StateDot 配色是否令牌化？** §2.3 当前规定 success/warning 用 `emerald-*`/`amber-*` 浅底、
   **不令牌化**；但 StateDot 跨组件复用，是否值得在 `index.css` 正式加 `--success`/`--warning`/
   `--info`？以及 ongoing 复用 `--primary` 还是新增低饱和 `--info` 蓝？
3. **LogsPage 与遥测走向**：是否认同"原始日志与 PerfTrace 遥测不并入用户轨迹 spine"，
   仅作为诊断模式从 Cmd+K `/logs` 进入？还是老板希望 spine 内嵌"原始日志"切换？
4. **details 下钻落点：右栏 surface 还是对话内联展开？** harness 的 `details` 即其右列
   （Sidebar|Conversation|Details 三栏），与本项目"Sidebar|Chat|RightPanel"**已一一对应**
   （RightPanel = harness details），**不存在"第四列"**。需老板确认的唯一分歧是：delegation 下钻
   继续走**现状的右栏 surface**（`selectChildTurn` 驱动，已核实），还是改成 harness 默认的
   **对话内联展开**（delegation 行就地展开 child 时间线）。Phase 3 默认沿用右栏 surface。
5. **Tauri 全局快捷键冲突**：`Cmd/Ctrl+K` 等是否被 `tauri.conf.json` 的 accelerator 占用？
   需老板/桌面端同学核对后定键位（可能改 `Cmd/Ctrl+J` 等）。
6. **Slash 命令初版范围**：先上核心 4 条（`/changes` `/logs` `/clear` `/help`）快速见效，
   还是一次性上全 8 条？建议分两批。

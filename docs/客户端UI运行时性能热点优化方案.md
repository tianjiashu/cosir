# 客户端 UI 运行时性能优化方案（长期架构视角）

> **定位**：本文是 `docs/客户端流式显示与排版优化技术方案.md`（下称「既有方案」）的**补充与落地细化**，不是替代。
> 既有方案第 3.6 节已规划 Timeline 虚拟滚动（依赖 `@tanstack/react-virtual`）、第 3.1–3.5 节规划排版 token / caret / 代码块折叠 / 思考块流式可见 / 富文本加固。
> 本文聚焦「既有方案落地后、真实代码已具备 turn 级窗口与 SSE rAF 攒批的前提下，**运行时仍存在的细粒度热点**」，并补齐「已装依赖/已沉淀原语未被复用」的遗漏。
> **设计原则（用户明确）**：不怕改动大，以**长期稳定迭代**为目标——优先做结构性、可维护的优化，而非在组件里打补丁；改动面可以大，但必须不破坏既有优化、必须可验证、必须可回归。
> 所有事实均来自 `read_file` / `search_content` 真实代码（2026-08-05 快照），与既有方案撰写时的「现状」存在差异，差异点已在文末第 6 节标注。

---

## 一、当前真实代码基线（与既有方案「现状」的关键差异）

| 能力 | 既有方案 2.1 节描述的「现状」 | 当前真实代码（已落地） | 来源 |
|---|---|---|---|
| turn 列表渲染 | 所有 turn DOM 常驻 | 已做分片窗口：`INITIAL_TURN_COUNT=20` + 「加载更早对话」按需扩展 | `ChatPanel.tsx:41,105,111-112,161-173` |
| turn 级重渲染隔离 | 未提及 | `eventsByTurnId` 按 turn 分片，其它 turn 收事件时数组引用不变，`<TurnTimeline>` memo 精确跳过 | `ChatPanel.tsx:69-71,176-182` |
| 流式滚动 | rAF + 200ms 节流 | 已实现 | `ChatPanel.tsx:117-130` |
| 虚拟滚动能力 | 第 3.6 节规划引入 | **已沉淀为项目自有原语 `lib/virtual/VirtualList.tsx`**，且 `ToolCallCard` 已复用（阈值 50），配套 `tests/VirtualList.test.tsx` 已覆盖 | `lib/virtual/VirtualList.tsx`；`ToolCallCard.tsx:39,391,412-423`；`tests/VirtualList.test.tsx` |
| `@tanstack/react-virtual` 依赖 | 规划引入 | **已装 `^3.14.9`**（无新增依赖即可用） | `apps/desktop/package.json` |
| `ChangesTab` 列表 | 未提及 | 直接 `files.map` 渲染（map 本体 96-105 行），**未复用**已有的 `VirtualList` 原语；外层被 Radix `<ScrollArea>` 包裹 | `ChangesTab.tsx:94-107`；`RightPanel.tsx:138-142` |
| turn 级 Timeline 虚拟滚动 | 规划中 | **未落地**（仍是 `<ScrollArea>` + `map`，未用 `VirtualList`） | `ChatPanel.tsx:135,176-182` |

**结论**：流式框架「骨架优化」已落地（窗口 + memo + 攒批 + 节流滚动），虚拟滚动能力也已沉淀为项目自有通用原语 `VirtualList`。剩余问题是「渲染层细粒度热点」与「已有原语未被复用」——本文从**长期架构视角**给出结构性解法，改动面允许较大，但以「不破坏既有优化 + 可验证」为硬约束。

---

## 二、待解决的热点（按根因优先级，面向长期可维护）

### H1 — 活跃 turn 的 `projectTurnTimeline` 每帧全量重投影（高，根因级）

**事实**：`TurnTimeline` 中 `useMemo(() => projectTurnTimeline([turn], events), [turn, events])`。`events` 来自 `eventsByTurnId[turn_id]`，`eventStore` 每次 flush 调用 `appendEvents` 会**生成一个新数组**（引用变化——这是既有方案为 memo 隔离历史 turn 而设计的，必须保留）。副作用：`useMemo` 的 `events` 依赖每帧变 → 每帧全量重投影。

`projectTurnTimeline`（`services/timeline/projector.ts`）：`events.filter(turn 过滤)` + `projectEntries(events)`（`for` 逐事件 switch 累加）+ 多段正则/行号计算。复杂度 **O(n)**，n 为全 turn 事件数。

**影响**：流式 60s + 中等 token 率即数千事件；多轮对话 / 并发工具调用时 n 线性放大。这是 H2（markdown 每帧重渲染）的根因之一——`AgentMessage` 收到的 `entries` 每帧都是新数组。

**长期架构解法（结构性，非打补丁）**：
- **建立「投影结果稳定引用」契约**：投影层不再每帧从零 `filter + reduce`，而是维护「已投影基线 + 已消费 `event_id` 集合」，每次只对新到达的 delta 事件 `applyDelta` 并复用历史投影结果（`event_id` → 投影片段的稳定 map）。`projectTurnTimeline` 返回的 `entries` 数组引用，只有在 delta 真正改变内容时才变，**与 `events` 数组引用变化解耦**。
- **明确 delta 来源契约**：`eventStore.appendEvents` 当前只暴露「全量 events 数组」。长期正解是让 store 同时暴露「本批新增 events」（`appendEvents` 内部已知 delta），投影层直接消费 delta，无需在组件里 diff。这要求 `eventStore` 增加一个轻量「lastDelta」字段或订阅回调，但**不得改变已有 `eventsByTurnId` 的引用不变语义**（否则历史 turn memo 隔离失效）。
- **配套组件层**：`TurnTimeline` 的 `useMemo` 依赖从 `[turn, events]` 改为「仅当一个真正改变投影结果的 delta 到达才重投影」，避免纯引用变化触发。

**预期收益**：活跃 turn 每帧投影成本从 O(n) 降到 O(delta)；长会话 / 大任务下消除卡顿主因；且投影结果引用稳定后，H2 的 markdown 重渲染同步减少。

**改动面**：`services/timeline/projector.ts`（新增增量投影/缓存层）、`stores/eventStore.ts`（`appendEvents` 暴露 delta 或新增 delta 订阅）、`components/layout/TurnTimeline.tsx`（接入稳定 entries）。属中等偏大改动，但全部在既有分层内（`service/timeline` 投影、`store` 状态、`component` 渲染），不跨层。

---

### H2 — `AgentMessage` 流式期 `react-markdown` 每帧重解析（高）

**事实**：`AgentMessage` 已做好 `useMemo(buildMarkdownComponents, [streaming, lastTag, hasContent, lastLine])`（`CodeBlock` 等组件映射缓存正确）。但 `ReactMarkdown` 的 `children={content}` 每帧随流式增量变化 → 整段 mdast 重新解析。短消息无感，**长输出 / 多消息并发**时每帧重解析所有已生成内容代价明显。

**长期架构解法（结构性）**：
- **区分「流式态」与「定稿态」渲染策略**，抽成独立受控组件（如 `MarkdownStream`），长期可替换、可单测：
  - **定稿态**（非流式）：正常 `react-markdown` 全量解析（一次），可接入 `rehype-highlight` 高亮。
  - **流式态**：采用「**增量文本 append**」而非每帧重 parse 整段——把已定稿的前缀段落用缓存的渲染结果（或 `dangerouslySetInnerHTML` 缓存字符串）固化，仅对「最后一个未闭合段落」做轻量 markdown 解析；配合 caret 独立层（不牵动 markdown 子树）。
  - **兜底**：若增量 append 实现成本过高，先用「content 100ms 节流快照」过渡（节流阈值可配置），`streaming` 结束立即用最终 content 渲染一次，避免末尾残留。
- **不引 Monaco / 不引 @llm-ui**：Monaco **当前尚未引入本项目**（`package.json` 无 monaco 依赖、全仓 0 引用；Diff 展示由已装的 `react-diff-view@^3.3.3` 在 `ToolCallCard` 承担），按 `客户端UI组件.md` 第 89-101 行它属于「未来真正编辑/完整 Diff」的独立大模块，本文不提前引入；`@llm-ui` 会接管 markdown 渲染与代码块插槽，与现有 `AgentMessage`+`CodeBlock`+`FileLink` 三件套耦合过深，**长期维护成本高于收益**，不采用。仅当自实现增量 append 实测仍不达标，才评估 `streamdown`（react-markdown drop-in 流式替代，耦合浅）。

**预期收益**：长消息流式期 markdown 重解析从「每帧全量」降到「仅末段增量 / 100ms 节流」；与 H1 联动后重渲染进一步减少；渲染策略抽离后长期可演进、可单测。

**改动面**：`components/chat/AgentMessage.tsx`（抽 `MarkdownStream` 受控组件 + 流式/定稿双策略）、`components/chat/CodeBlock.tsx`（跟随 H4 统一高亮契约）。属较大改动，但集中在聊天渲染层，不波及 stores/api。

---

### H3 — 长列表统一复用项目自有 `VirtualList` 原语（中，收益明确、风险低）

**事实**：`@tanstack/react-virtual@^3.14.9` 已装，并已沉淀为项目自有原语 `lib/virtual/VirtualList.tsx`（含动态行高 `measureElement`、已被 `ToolCallCard` 复用、有测试覆盖）。但 `ChangesTab.tsx:94-106` 仍 `files.map` 直接渲染，未复用该原语；turn 级 Timeline 虚拟滚动（既有方案 3.6 节）也未落地。

**长期架构解法（结构性，统一规范）**：
- **立项目约定**：「所有超过阈值（建议 50）的列表一律走 `VirtualList` 原语」，杜绝后续再出现 `files.map` / `items.map` 裸渲染。把 `ChangesTab` 与 turn 级 Timeline 都接入 `VirtualList`，统一滚动容器与动态行高处理。
- **前置条件（必做，否则虚拟滚动失效）**：`VirtualList` 契约（`VirtualList.tsx:10-11`）禁止用 Radix `ScrollArea` 作滚动容器（其 Viewport 干扰 `scrollOffset` 测量）。当前 `ChangesTab` 外层是 `RightPanel.tsx:139` 的 `<ScrollArea>`，`ChatPanel` 是 `ChatPanel.tsx:135` 的 `<ScrollArea>`；两处接入前须先改为原生 `overflow-y-auto` 并给出有界高度（与既有方案 3.6 节「`ScrollArea` 兼容性」一致），避免嵌套双滚动。H3 因此是「接入 + 滚动容器改造」，风险按**中**评估。
- `ChangesTab`：用 `VirtualList` 包裹 `files`，替换 `files.map`；`getKey` 用 `file.path`（天然唯一稳定）。注意当前仅 `onToggleSelect` 是 `useCallback`，`onKeep`/`onRevert` 为内联箭头（`ChangesTab.tsx:102-103`，每帧新引用），接入时须一并收敛为 `useCallback`，否则行级 memo 失效、虚拟化收益被抵消。`VirtualList` 已支持动态高度（`measureElement`），选中态变化触发重测属正常。
- turn 级 Timeline：按既有方案 3.6 节，对 `turns` 列表接入 `VirtualList`（与 `ChangesTab` 文件级虚拟滚动互补：turn 级解决「数百 turn DOM 爆炸」，文件级解决「大变更集行爆炸」，同一原语、无冲突）。

**预期收益**：大变更集 / 长会话 DOM 节点从「全量」降到「视口内 + overscan」；统一原语后后续列表零额外心智负担，长期可维护。

**改动面**：`components/right-panel/ChangesTab.tsx`（接入 `VirtualList`）、`components/layout/ChatPanel.tsx`（turn 级虚拟滚动接入）。属明确、低风险改动（复用已验证原语 + 已有测试）。

---

### H4 — `CodeBlock` 长代码每帧 `code.split("\n")` + 统一高亮契约（低）

**事实**：`CodeBlock.tsx` 流式期每次 render 执行 `code.split("\n")`，且当前**无语法高亮**（注释 TODO 换 Monaco，但 Monaco 过重）。

**长期架构解法（结构性）**：
- `useMemo(() => code.split("\n"), [code])`（消除每帧 split）。
- **统一高亮契约（项目级）**：建立 `lib/markdown/highlight.ts` 作为唯一高亮入口，**仅定稿期应用**（流式期不做高亮，避免每帧重高亮）。该契约同时服务 H2 定稿态 markdown 高亮，避免 `CodeBlock` 与 `AgentMessage` 各写一套。
- **高亮实现须单独立项，不属于「零新增依赖」范围**：实测 `rehype-highlight` / `lowlight` / `shiki` **当前均未安装**（`apps/desktop/package.json` 无此依赖、全仓 0 引用）。故本轮**只做零依赖部分**：`useMemo` 去重 + 建立 `lib/markdown/highlight.ts` 契约入口（内部先返回纯文本、不高亮），把接口面固定下来；**真正的语法高亮列为「以后做」**，若确认要上，候选 `rehype-highlight`（+`lowlight`，与 remark/rehype 管线同源、约十几 KB），须按 `AGENTS.md` 依赖纪律显式上报用户并**锁版本**写入 `package.json`，不得由开发 Agent 自行引入。
- **不引 Monaco** 做聊天代码块高亮（见 `客户端UI组件.md` 第 101 行「聊天代码块用轻量高亮」）；Monaco 留给真正编辑/Diff 的独立模块。

**预期收益**：消除每帧 split 微开销；定稿代码获统一高亮，且不影响流式性能；高亮逻辑集中复用，长期可维护。

**改动面**：`components/chat/CodeBlock.tsx`、`lib/markdown/*`（新增高亮契约）。小到中等。

---

## 三、依赖决策（对齐项目约定，零新增依赖）

| 项 | 决策 | 依据 |
|---|---|---|
| 虚拟滚动 | **复用已装 `@tanstack/react-virtual@^3.14.9` + 项目自有 `VirtualList` 原语**，不新增依赖 | 既有方案 3.6 节已立项；`package.json` 已含；`VirtualList` 已沉淀且已测试 |
| 流式 markdown | **自实现「流式/定稿双策略 + 增量 append」**，不引 `@llm-ui`；`streamdown` 仅作兜底评估 | 避免深度耦合现有三件套；长期可演进、可单测；Monaco 仅留给编辑/Diff |
| 语法高亮 | 本轮**只建契约入口 `lib/markdown/highlight.ts`（零依赖、暂不高亮）**；真正高亮列「以后做」，候选 `rehype-highlight`(+lowlight)，**属新增依赖须先上报并锁版本**；不引 Monaco/shiki | 实测 rehype-highlight/lowlight/shiki 均未安装；`客户端UI组件.md` 第 101 行「聊天代码块用轻量高亮」；`AGENTS.md` 依赖纪律 |
| H1 增量投影 | **自实现**（store 暴露 delta + 投影层缓存），不引外部库 | 投影是项目私有领域模型；且须与 `appendEvents` 引用语义共存 |

> 按 `AGENTS.md` 依赖纪律：外部依赖须「通用复杂能力且代码库/已有依赖/标准库均不合适」才引入，且锁版本。本方案**本轮落地范围零新增依赖**（虚拟滚动复用已装 `@tanstack/react-virtual` + 自有 `VirtualList`；流式 markdown 自实现；增量投影自实现）。唯一可能引入依赖的是「语法高亮」，已被明确剥离为「以后做 + 须先上报锁版本」，不在本轮承诺内。

---

## 四、落地清单（结构性，不重复既有方案已列项）

| 序号 | 文件 | 改动 | 对应热点 | 风险 |
|---|---|---|---|---|
| 1 | `services/timeline/projector.ts` | 新增「增量投影 + event_id→片段缓存」，返回稳定引用 `entries` | H1 | 中 |
| 2 | `stores/eventStore.ts` | `appendEvents` 暴露本批 delta（不改 `eventsByTurnId` 引用语义） | H1 | 中 |
| 3 | `components/layout/TurnTimeline.tsx` | 接入稳定 `entries`，`useMemo` 依赖改为「仅投影相关 delta 变化」触发 | H1 | 低 |
| 4 | `components/chat/MarkdownStream.tsx`（新增） | 抽「流式/定稿双策略」受控组件；流式增量 append，定稿全量 + 高亮 | H2 | 中 |
| 5 | `components/chat/AgentMessage.tsx` | 接入 `MarkdownStream`，移除内联 markdown 逻辑 | H2 | 低 |
| 6 | `components/right-panel/ChangesTab.tsx` | 接入 `VirtualList` 原语，替换 `files.map` | H3 | 低 |
| 7 | `components/layout/ChatPanel.tsx` | turn 级 Timeline 接入 `VirtualList`（既有方案 3.6 节） | H3 | 低 |
| 8 | `lib/markdown/highlight.ts`（新增） | 统一定稿高亮契约（`rehype-highlight`） | H4 / H2 | 低 |
| 9 | `components/chat/CodeBlock.tsx` | `code.split("\n)` 包 `useMemo`；接入统一高亮契约 | H4 | 低 |

---

## 五、验证与闭环（对齐既有方案第六节 + `AGENTS.md` 第八节）

1. **类型检查**：`cd apps/desktop && npx tsc --noEmit` 零错误。
2. **单测（必做，因改动面大）**：`npx vitest run` 通过；新增用例覆盖：
   - 增量投影 delta 正确性（同 event_id 投影稳定、乱序/重复 delta 幂等）；
   - `eventStore.appendEvents` 暴露 delta 且不破坏 `eventsByTurnId` 引用语义；
   - `MarkdownStream` 流式/定稿双策略不丢字、定稿后最终渲染正确；
   - `VirtualList` 已在 `tests/VirtualList.test.tsx` 覆盖，`ChangesTab`/`ChatPanel` 接入后补集成用例（仅渲染视口内行）；
   - 统一高亮契约在定稿期产出高亮、流式期不触发。
3. **独立审查 Agent**：静态复查分层 / 单一职责 / docstring / 无死代码 / 是否引入跨层依赖（按 `AGENTS.md` 第八节）。
4. **独立测试 Agent**：独立执行测试覆盖业务与边界（按 `AGENTS.md` 第八节 + 既有方案第六节）。
5. **性能核查**：长会话 / 大变更集 mock，DevTools 确认：活跃 turn 每帧投影成本与 delta 成正比（非与总事件数成正比）；`ChangesTab` / turn 级列表 DOM 节点 ≈ 视口内 + overscan；流式期 markdown 重解析次数显著下降。
6. **不破坏既有优化**：回归确认 `appendEvents` 引用不变语义、`eventsByTurnId` memo 隔离、`INITIAL_TURN_COUNT` 窗口、SSE rAF 攒批仍生效。

---

## 六、重要说明：本文与既有方案「现状」的代码差异

- 既有方案 2.1/2.2 节描述的「所有 turn DOM 常驻、无窗口」**在当前真实代码中已不存在**——`ChatPanel` 已落地分片窗口 + `eventsByTurnId` memo 隔离 + rAF 节流滚动。
- 既有方案 3.6 节规划的虚拟滚动能力**已落地为 `lib/virtual/VirtualList.tsx` 原语**（`ToolCallCard` 在用、`VirtualList.test.tsx` 已覆盖），而非「待引入」。本文据此把 H3 从「引入依赖」修正为「复用自有原语」。
- 因此本文**不重复**既有方案的窗口 / memo / rAF 工作，只处理其之后仍存在的细粒度热点（H1–H4），并把 turn 级虚拟滚动按既有方案 3.6 节一并纳入（用同一 `VirtualList` 原语）。

---

## 七、风险与长期注意事项

- **H1 增量投影**必须保留 `appendEvents` 的引用不变语义，否则破坏历史 turn memo 隔离（回退到「所有 turn 重渲染」）。增量只优化「投影计算触发频率 + 结果引用稳定」，不动事件数组契约。store 暴露 delta 时须双写（全量数组 + delta 列表），不删减既有字段。
- **H2 双策略**节流阈值应可配置；`streaming` 结束后立即用最终 content 渲染一次，避免末尾残留节流快照。增量 append 若引入「未闭合语法误渲染」，须有「末段降级为纯文本」兜底。
- **H3 虚拟滚动**须用 `VirtualList` 已支持的动态高度（`measureElement` + `ResizeObserver`），避免行高估算错误导致滚动跳动；选中态变化触发重测属正常。
- **H4 高亮**仅定稿期应用，流式期保持纯文本 + 折叠（`CodeBlock` 已有阈值折叠），避免每帧重高亮。
- **改动面较大**，必须走 `AGENTS.md` 第八节完整闭环（类型检查 + 单测 + 独立审查 Agent + 独立测试 Agent），且单测覆盖 H1/H2 的核心不变量，保证后续迭代可回归。

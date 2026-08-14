# 并发子 Agent UI 显示 — 实施计划

> 状态：待独立审查闭环
> 日期：2026-08-13
> 设计依据：第零铁律（以长期稳定迭代为尺，敢改、敢引依赖，但绝不重复造轮子）、现有代码事实、已落地约定（AGENTS.md / rules/）

---

## 0. 代码事实基线（已通过 CodeGraph 探查确认）

后端与前端**已有完整的委派底座**，本计划不做推翻，只在既有契约上补齐 UI 能力。

### 后端（已存在，无需新建数据流）
- `core/delegation/delegation_executor.py`：`DelegationExecutor.execute` 调用
  `delegation_storage.try_create_pending(..., max_concurrency=Settings.DELEGATION_MAX_CONCURRENCY, ...)`，
  并发额度由 storage 层在同一事务内原子裁决 → 后端**已为并发委派预留语义**。
- 每个 child 是独立 child turn，带 `delegation_id` / `parent_turn_id` / `child_turn_id`
  （见 `models/payload/delegation_child_started_payload.py` 的 `DelegationChildStartedPayload`，
  status=running + delegation 三元组）。
- 事件经 `RuntimeEventService` 持久化并 SSE 广播；child turn 作为独立 turn，其 runtime events
  可经 `GET /turns/{turn_id}/stream`（turn 级 SSE）或 task 级事件查询获取。
- 已有 delegation 生命周期事件类型：`delegation_child_started` / `delegation_finished`
  / `delegation_failed` / `delegation_cancelled`（见 `models/enums/event_type.py`；
  前端 `projector.ts` 的 `DELEGATION_EVENTS` 与 `useDelegationStreams.ts` 均统一用 `delegation_finished`，
  **无 `delegation_completed`**）。

### 前端（已存在，缺口明确）
- `services/timeline/projector.ts`：已有 `delegationById: Map<delegationId, index>`，
  按 `delegation_id` 合并生命周期事件 → **天然支持同一 parent turn 下多个 delegation 分行**。
  增量投影已处理 `delegation_child_started`/`delegation_finished`/`delegation_failed`/`delegation_cancelled`。
- `components/chat/DelegationTimelineEntry.tsx`：已能渲染单个子 Agent 行
  （状态徽章、child turn id、可展开的 `childEntries`），但 `childEntries` 当前是 ReactNode，
  **未接到后端 child turn 事件流**。
- `hooks/useDelegationStreams.ts` + `services/delegationStream`：`DelegationStreamConnection`
  已从 task 级事件流识别带 `child_turn_id` 的 delegation，并为**每个 child turn 建立独立
  subscribe-only SSE 连接**，把 child events 合并进全局 `eventStore`。
  → **child turn 事件在前端已可获取**。
- `stores/eventStore.ts`：全局事件存储，child events 与 parent events 同存；
  `projectTurnTimeline(turns: TurnRecord[], events)`（`projector.ts:510`，首参为 `TurnRecord[]`）
  可按 `turn_id` 投影任意 turn 的 timeline；`eventStore.eventsByTurnId` 已按 `turn_id` 分片 child 事件。
- `components/right-panel/SubagentBlock.tsx`：**目前是 MOCK 数据**，未接真实 child turn 事件流。

### 真实缺口（本计划要解决的）
1. **并发可视化语义缺失**：多条 delegation 在 timeline 里线性堆叠为普通行，
   无"并发展示"语义（无分组、无并发进度指示、无左侧连通泳道）。
2. **点击侧边栏链路缺失**：`DelegationTimelineEntry` 行不可点击；`SubagentBlock` 是 MOCK，
   没有"点击 timeline delegation → 侧边栏展示该 child turn 完整 timeline"的接线。
   （注：child turn 事件流已由 `useDelegationStreams` 接入 `eventStore`，缺失的只是"渲染接线"。）

---

## 1. 设计原则（对齐第零铁律与项目规范）

- **复用优先，不造轮子**：child turn 的独立事件流、SSE 订阅、timeline 投影器、状态徽章组件
  全部已存在。侧边栏渲染直接复用 `eventStore.eventsByTurnId` 分片 + `projectTurnTimeline([turnRecord], events)`
  + 现有事件源，不新建数据流、不新建 projection hook、不新建投影器。
- **不引入新后端事件类型**：并发关系完全可由现有 `delegation_child_started` / `delegation_finished`
  等事件在前端推导（同 `parent_turn_id` 下多个 running 即并发组）。
- **单一职责 / 目录清晰**：新增一个 `delegationStore`（委派 UI 选中态）、一个 `SubagentPanel`
  （侧边栏真实渲染）；投影器只加"并发组识别"纯函数与累积字段，不新增独立 hook。
- **最小、向后兼容的后端扩展（仅当需要更精确并发组语义时）**：
  在 `DelegationChildStartedPayload` 加可选 `concurrency_group_id`
  （由 `DelegationExecutor` 在 `try_create_pending` 时按当前并发集合生成）。
  若前端仅凭现有事件即可推导并发组，则**后端零改动**——首选此路径。

---

## 2. 实施步骤

### 步骤 A：侧边栏展示单个子 Agent（价值独立，先行）
**目标**：点击 timeline 上的 delegation 行 → 侧边栏展示该 child turn 的完整 timeline。

A1. 新增 `stores/delegationStore.ts`（zustand，与 `turnStore`/`eventStore` 同层）
- 状态：`selectedChildTurnId: string | null`。
- 动作：`selectChildTurn(childTurnId)` / `clearSelection()`。
- 职责单一：仅承载"当前选中的委派子 Agent"UI 状态。

A2. 改造 `components/chat/DelegationTimelineEntry.tsx`
- 行整体可点击（`onClick` → `delegationStore.selectChildTurn(childTurnId)`），
  带键盘可达性（`role="button"` / `tabIndex` / `onKeyDown`）。
- 选中态视觉高亮（读取 `delegationStore.selectedChildTurnId`）。
- 删除原内联 `childEntries` MOCK 展开逻辑（改走侧边栏，避免主 timeline 变重、避免 child 自身
  工具调用嵌套内联失控）。

A3. 新增 `components/right-panel/SubagentPanel.tsx`（替换 MOCK `SubagentBlock` 的位置或作为独立右侧 tab）
- 订阅 `delegationStore.selectedChildTurnId`。
- 通过 `useDelegationStreams` 已建立的 child 事件（`eventStore` 内该 `childTurnId` 的事件），
  **按真实签名**复用现有投影器：先取 `eventStore.eventsByTurnId[childTurnId]`（`eventStore.ts:37,96,216-220`
  已按 `turn_id` 分片的 child 事件），再构造单元素 `TurnRecord[]`
  （`[{ turn_id: childTurnId, input_text: "", response_text: "" }]`；
  若 `eventStore` 或 `turnStore` 已有该 turn 记录则优先复用真实 `TurnRecord`，避免捏造），
  调用 `projectTurnTimeline([turnRecord], childEvents)`（`projector.ts:510`，首参为 `TurnRecord[]` 而非 turnId），
  复用 `TurnTimelineImpl` 渲染组件。
- 空态：未选中时显示提示；选中但事件未到时显示 loading。
- 顶部显示子 Agent 元信息（child turn id、delegation 状态徽章）。

A4. ~~新增 `hooks/useChildTurnTimeline.ts`~~ **已删除**：审查指出 `eventStore.eventsByTurnId` 已提供
  按 turn_id 取 child 事件的现成分片（`eventStore.ts:37,96,216-220`），再叠加一层"turnId→投影"的
  hook 属重复造轮子、职责与 `eventStore` 分片重叠。A3 直接在 `SubagentPanel` 内复用该分片 +
  `projectTurnTimeline([turnRecord], events)` 即可，不新增独立 hook。

### 步骤 B：并发组可视化（在 A 基础上增量）
**目标**：timeline 内识别同 parent 下多个并发 delegation，给出并发展示语义；
侧边栏支持并发多子 Agent 切换。

B1. `services/timeline/projector.ts` 增量投影内识别并发组（纯前端推导，零新事件）
- **新增投影累积字段** `concurrencyByParentTurn: Map<string, Set<string>>`（`TimelineProjectorState`，
  与现有 `delegationById` 同层），记录每个 `parent_turn_id` 下当前处于 running 的 delegation_id 集合。
- 在 `projectTimelineIncrementally` 的 delegation 分支中维护该集合：
  - `delegation_child_started` → 将 `delegation_id` 加入 `concurrencyByParentTurn[parentTurnId]`；
  - `delegation_finished` / `delegation_failed` / `delegation_cancelled` → 从集合中移除。
- 派生逻辑：当某 `parentTurnId` 对应集合中 running 规模 ≥ 2 时，该集合即一个并发批次；
  给每个 member delegation 的 `TimelineDelegationItem` 附加 `concurrencyGroupSize`（集合大小）
  与 `concurrencyIndex`（在集合中的序号，按 started 到达顺序稳定）。
- **幂等保证**：`processedEventIds` 已覆盖去重，乱序/回放时集合增删仍由事件类型决定，判定不漂移。
- 不修改现有 `delegationById` 合并逻辑，仅在其上叠加并发组标注；
  新增字段与派生纯函数须带完整中文 docstring（参数/返回/副作用），随实现同步更新。

B2. `components/chat/DelegationTimelineEntry.tsx` 渲染并发指示
- 行内显示并发进度，例如 `2 / 3 并发运行中`；同一并发组的行用左侧连通竖线（泳道）视觉归组
  （复用现有 `border-l` 思路，扩为并发组容器包裹）。
- `TimelineDelegationItem` 类型新增可选并发字段（`concurrencyIndex?` / `concurrencyGroupSize?`），
  保持向后兼容。

B3. `components/right-panel/SubagentPanel.tsx` 支持并发切换
- 顶部 tab 列出当前所有并发 child（各自状态徽章），点击切换 `selectedChildTurnId`，
  下方渲染对应 child turn timeline。把"并发"与"侧边栏"两诉求在侧边栏内自然汇合。

### 步骤 C：后端最小扩展（条件项，仅在 A/B 仅靠现有事件无法精确判定并发组时才做）
- `models/payload/delegation_child_started_payload.py`：`DelegationChildStartedPayload` 加可选
  `concurrency_group_id: str | None = None`，docstring 同步。
- `core/delegation/delegation_executor.py`：在 `try_create_pending` 返回时，按当前并发集合
  生成/填充 `concurrency_group_id`。
- 生成脚本 `scripts/generate_runtime_event_ts.py` 重新生成 `apps/shared/ts/events.ts`，
  保持前后端协议一致（勿手改 ts）。
- 若 A/B 阶段前端已能仅凭现有事件推导并发组，**跳过 C**。

---

## 3. 目录 / 职责落点

```
apps/desktop/src/
  stores/delegationStore.ts            # 新增：选中态（单一职责：委派 UI 状态）
  services/timeline/
    projector.ts                       # 改动：新增 concurrencyByParentTurn 累积字段 + 并发组派生（纯函数，零新事件）
  components/chat/
    DelegationTimelineEntry.tsx        # 改动：行可点击、渲染并发指示、删除 MOCK 内联展开
  components/right-panel/
    SubagentPanel.tsx                  # 新增（替换 MOCK SubagentBlock）：复用 eventStore.eventsByTurnId + projectTurnTimeline 渲染 child turn timeline + 并发切换
```

后端改动范围：**仅 C 为条件项**；A/B 阶段后端零改动。

---

## 4. 验收 / 闭环要求（对齐 rules/ 开发-审查-测试闭环）

- **独立审查 Agent**：对照 AGENTS.md 与 rules/ 逐条检查（单一职责、目录结构、复用、防膨胀、
  docstring、分层、禁止行为），输出「符合 / 不符合」+ 文件/行号问题列表。主 Agent 不得自判完成。
- **独立测试 Agent**：前端为 React/TS，需补充：
  - `projector.ts` 并发组识别的单元测试（≥2 并发、并发完成、混合串行+并发）。
  - `delegationStore` 选中态单测。
  - `SubagentPanel` / `DelegationTimelineEntry` 点击 → 选中 → 渲染的组件/集成测试
    （用已入 `eventStore` 的 mock child 事件验证复用 `projectTurnTimeline` 正确）。
  - 若做 C：`delegation_child_started_payload` 与生成脚本单测。
- 审查 + 测试均通过后，方可宣布完成；有问题修复后重新跑闭环。
- 新增投影累积字段（`concurrencyByParentTurn`）、并发组派生纯函数、`TimelineDelegationItem`
  并发可选字段，须带完整中文 docstring（参数/返回/异常/副作用），随实现同步更新。
- `SubagentPanel` / `delegationStore` 等新增前端模块须带组件/函数级 docstring 与必要类型注解。

---

## 5. 风险与取舍

- **内联展开 vs 侧边栏**：选侧边栏而非主 timeline 内联展开，因 child 自身含工具调用嵌套，
  内联会让主 timeline 失控、破坏"不看代码只看目录知职责"的清晰度。
- **并发组判定在前端推导**：避免新增后端事件类型与协议耦合；仅当精度不足才走 C 的最小扩展，
  且 C 向后兼容（字段可选）。
- **C 为正确性兜底优先**：若 A/B 上线后并发组出现错判（如快速完成又新建导致前端近似推导失准），
  应优先走 C（后端 `DelegationChildStartedPayload` 补 `concurrency_group_id`，由
  `DelegationExecutor.try_create_pending` 在同一事务内原子裁决时填充），而非在前端打补丁——
  与"正确性优先于默认偏好（零改动）"一致。
- **复用 `useDelegationStreams` 已建连接**：不重复建 SSE，符合"能复用就不手写"。

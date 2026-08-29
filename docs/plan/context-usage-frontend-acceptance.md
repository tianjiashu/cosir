# 上下文占用圆环前端修复 —— 验收目标

> 状态：开发已完成，待独立审查 Agent 验收（2026-08-28）。
> 开发自测：5 个新测试文件 26/26 通过；变异验证（源码回退到旧实现）25/26 失败；改动文件 tsc / lint 零新增错误。
> 配套方案：`context-usage-main-agent-concurrency-fix-plan.md` §4（前端部分）。
> 后端结论：§3 的 5 项中 4 项已落地（提交 `6e18766`），仅 §3.5 窗口软上限默认值双轨未修；后端不需要为本次前端修复再改动。

---

## 一、任务目标（结果态，不是改动态）

**桌面端上下文占用圆环在任何 task 并发、切换、删除时序下，都只显示当前 active task 的、尽量新的上下文占用；不存在任何一条可复现的时序能让圆环显示错误 task 的数据或被意外清零。**

本任务的完成判据是「上述结果态成立」，**不是「改完了下面这几个文件」**。审查 Agent 的判定对象是最终代码的行为与不变量，不是 diff 是否落地。

### 关键事实（决定了验收面是有限的、可穷尽的）

- usage 状态的**写入点只有 2 处**：`hooks/useSSE.ts` 的 flush 循环、`hooks/useTask.ts` 的 `openTask()`。
- usage 状态的**读取点只有 1 处**：`components/chat/ContextUsageRing.tsx`，且其唯一消费者是 `components/layout/InputBar.tsx:284`。
- `useSSE()` 在生产代码中只被 `useTask.ts:97` 实例化一次：单一连接池、单一 `pendingEventsRef` 攒批缓冲。
- `useDelegationStreams` 不写 usage store（委派子流与圆环无关，后端亦不为其 emit `context_usage`）。

---

## 二、缺陷基线（验收时若仍可复现 → 不合格）

下列每条都标注了当前代码中的确切位置，审查 Agent 应逐条确认已消除。

| 编号 | 缺陷 | 基线位置 | 可复现时序 |
|---|---|---|---|
| D1 | 后台 task 的 `context_usage` 覆盖当前圆环 | `useSSE.ts:190-192`（未读 `event.task_id`） | A 活跃，B 后台运行 → 圆环显示 B 的占用 |
| D2 | 任意 turn 建连时全局清零当前圆环 | `useSSE.ts:222-223`（无条件 `reset()`） | A 正在流式时 B 新建 turn → A 圆环瞬间归零 |
| D3 | `openTask()` 迟到响应覆盖当前圆环 | `useTask.ts:378-388` 位于 `:393` 的 `seq` 守卫之前 | 快速打开 A 再打开 B，A 的迟到响应覆盖 B 的圆环 |
| D4 | store 无 task 归属概念 | `contextUsageStore.ts:18-25`（全局三字段） | 切回旧 task 时显示的是「最后收到事件的那个 task」的值 |
| D5 | 删除 task 后 usage 无清理路径 | `taskStore.removeTask` / `clearWorkspaceTasks` 未触及 usage store | 删除任务后 `usageByTaskId` 残留条目，无界增长 |
| D6 | 运行中任务被旧持久化值回拨 | `useTask.ts:381` 无条件回填 | A 正在流式时 `useStartupTaskResume` 回调 `openTask(A)` → 圆环被稍旧的 DB 值往回拨 |

---

## 三、验收标准（AC）

每条给出「审查 Agent 如何独立验证」。全部满足才算通过。

### AC1 — store 形状为 task 维度，键为 `number`

`stores/contextUsageStore.ts` 状态为 `usageByTaskId: Record<number, TaskUsageEntry>`，
`TaskUsageEntry = { usedTokens: number; totalTokens: number; updatedAt: string | null }`。

- **验证**：键类型必须是 `number`，与 `TaskRecord.task_id`、`tasksById`、`drafts: Record<number, string>` 一致。出现任何 `Record<string, …>` 或 `String(taskId)` 作为 store 键的写法 → **不合格**（方案 §4.1 原文的 `Record<string, …>` 是错的，与项目 idUnify 约定冲突）。
- 动作集合：`setUsage(taskId, payload, updatedAt)` / `resetTask(taskId)` / `clearAll()`。store **只承载状态与动作，不做派生**；派生选择器不得作为 store action。

### AC2 — 写入按 `event.task_id` 归属

`useSSE` flush 循环写入 `setUsage(event.task_id, …)`。

- **验证**：写入路径不存在对「当前 active task」的任何读取（含 `useTaskStore.getState().activeTaskId`）。把 activeTaskId 捕获进 `useSSE.connect` 闭包属于易腐写法（用户切 task 后闭包立即过期），**不合格**。

### AC3 — 不存在全局 reset 副作用

- **验证**：`useSSE.ts` 中不再出现对 usage store 的 `reset()` / `clearAll()` 调用。任意 `connect()` 不得清空任何 task 的已缓存 usage。
- 新建 task 在 `usageByTaskId` 中本就没有条目，缺省即 0/0，不需要任何 reset。

### AC4 — 读取按 activeTaskId 派生

`ContextUsageRing` 从 `useTaskStore.activeTaskId` 取当前任务，再读 `usageByTaskId[activeTaskId]`。

- **验证**：`activeTaskId` 为 `null`、或该 task 无条目时，圆环显示 0/0 且**不得**回退到「全局最新值」或任何其它 task 的值。

### AC5 — zustand 引用稳定，无重渲染风暴

- **验证**：selector 内**不得**新建对象作为缺省值（如 `?? { usedTokens: 0, totalTokens: 0 }`），必须使用模块级冻结常量 `EMPTY_USAGE`。否则每次 store 变更返回新引用，击穿 zustand 的引用相等比较 → 无限重渲染。这是本次改造最易踩的坑，必须逐行确认。
- 同一 store 变更下，非 active task 的 usage 更新**不得**触发圆环组件重渲染。

### AC6 — 生命周期清理覆盖三条删除路径

- 单任务删除（`taskStore.removeTask`）→ `resetTask(taskId)`
- 工作区任务清空（`clearWorkspaceTasks` 的 `removedTaskIds` 循环）→ 逐条 `resetTask`
- 全量清空路径 → `clearAll`

- **验证**：落位与同文件既有的 `useEventStore.getState().invalidateTask(taskId)` 范式并排（`removeTask` 约 :760、`clearWorkspaceTasks` 约 :941-943）。
- `contextUsageStore` 必须是 leaf（仅依赖 zustand 与 `@shared/events`），taskStore 反向引用它**不得**产生循环依赖。

### AC7 — `openTask()` 回填带单调守卫

- **验证**：回填写入 `usageByTaskId[taskId]`（而非全局），使其在 `seq` 竞态判断之前或之后都无害——旧响应最多刷新它自己的缓存，结构上不可能污染当前 UI。
- 回填前比较时间戳：本地条目已存在且 `entry.updatedAt > task.updated_at` 时跳过，防止 D6。
- `context_usage_used` 或 `context_window_total` 任一为 `null` 时走 `resetTask(taskId)`（不是全局 reset）。

### AC8 — 单批次内多 task 事件交错时各 task 独立取最后一条

`pendingEventsRef` 是跨连接共享的攒批缓冲，一个 flush 批次内可能交错不同 task 的事件。

- **验证**：由于数组顺序对每个 task 各自单调，按 `event.task_id` 分键写入后，每个 task 得到的必须是它自己在本批次中的**最后**一条。审查时需确认没有「整批取最后一条覆盖全局」的残留逻辑。

### AC9 — 类型完整，无 `any` / 无 string 中间键

- **验证**：`ContextUsagePayload` 来自 `@shared/events`，不得新增本地重复定义。不得出现 `as any`、`String(taskId)` 作 store 键。

### AC10 — 测试具备真实回归能力（变异可证伪）

新增 5 个 vitest（`src/tests/`）：

1. `contextUsageStore.taskIsolation.test.ts` —— A/B 分别 setUsage 互不覆盖；键为 number。
2. `useSSE.contextUsageIsolation.test.tsx` —— 后台 task B 的 `context_usage` 到达后，active task A 的圆环仍是 A 的值（**对应 D1，核心回归**）。
3. `useTask.openTask.contextUsageRace.test.ts` —— 快速打开 A 再打开 B，A 的迟到响应不让 B 的圆环显示 A 的值（**对应 D3，核心回归**）。
4. `ContextUsageRing.activeTask.test.tsx` —— `activeTaskId` 切换时圆环读对应 task 值；无条目时 0/0。
5. `taskDelete.contextUsageCleanup.test.ts` —— 删除 task 后清理该 task 缓存。

- **验证（关键）**：第 1–3 项必须**对旧实现可证伪**——即把被测代码回退到全局单值 + 无条件 reset + 回填在守卫前，这些测试必须失败。**通过但改回旧代码仍通过的测试视为无效，该项判不合格。**
- 全部 5 项在 CI 下通过，且不依赖既有 100+ 红色 tsc 错误的修复。

---

## 四、反验收：这些**不算**任务完成

明确排除，防止「改了代码就算完成」：

- ❌ 「4 个文件都改了、diff 与方案一致」—— 若 D1–D6 任一仍可复现，仍判不合格。
- ❌ 「全局单值保留，只在写入处加 `if (taskId === activeTaskId)` 过滤」—— 这会把 activeTaskId 捕获进 `connect` 闭包，切 task 后过期；且它丢弃后台 task 数据，与 codebase 既有的 task 键化约定（`tasksById` / `drafts` / `eventsByTaskId` / `turnsByTask`）相悖。
- ❌ 「测试数量够了」—— 测试必须对旧实现可证伪（见 AC10）。
- ❌ 顺手重构 `ContextUsageRing` 的视觉表现、颜色分级、格式化逻辑 —— 不在本次范围。
- ❌ 修复既有 100+ tsc 红色错误 —— 只要求本次相关文件零新增错误。

---

## 五、本次明确不做（非缺陷，或需另行拍板）

| 项 | 说明 |
|---|---|
| 无数据时的 0/0 展示 | 无 active task 或该 task 无 usage 时显示 `0.0% · 0 / 0 上下文`，**维持现状，不算缺陷**。验收只看「是否显示了别的 task 的数据」。 |
| 委派子 Agent usage | 后端 `main_agent_only=True` 已不为其 emit，前端 `useDelegationStreams` 也不写 usage store。无需处理。 |
| 新建 task 预填 `totalTokens` | 需先定「无 turn 时是否显示圆环」的产品口径。以后做。 |
| `Settings.CONTEXT_WINDOW_TOKENS` 双轨 | 后端遗留项（类字段 200000 vs `Settings.load()` 默认 "0"），需你拍板口径，本次不动。 |

### 原本列为残留、后经确认纳入本次范围

**删除「运行中」的 task 必须断开其 SSE 连接。**
原 `Sidebar.handleConfirmDeleteTask` 只调 `removeTask`，未断开该 task 的流；残留流继续投递事件会在 `usageByTaskId` / `eventStore` / `turnStore` 中重建已删 task 的条目。用户已确认随 usage 一并修复。

修复过程中发现的**阻断性前提**：连接池若放在 `useSSE` 的 `useRef` 里是无效的——`useTask()` 在 InputBar / Sidebar / NewTaskPage / useStartupTaskResume 四处各自实例化，每个实例持有彼此隔离的空池，InputBar 建立的流 Sidebar 永远看不到，断流会静默退化为空操作。故连接池必须抽为模块级单例 `services/sseConnectionPool.ts`，删除事务收口在 `useTask.deleteTask`。

### AC11 — 删除任务的事务完整性

`useTask.deleteTask(taskId)` 是删除的唯一入口，内部三步：先断流 → 再调后端 `DELETE /tasks/{id}` → 最后清本地缓存。

- **验证**：断流必须在本地缓存清理**之前**（否则删除期间到达的事件会在清理之后又写回条目）。`Sidebar` 不得再直接导入 `deleteTask` API 或调用 `taskStore.removeTask`。
- **验证（核心）**：连接池必须是模块级单例。连接由**另一个** `useSSE` 实例建立时，删除方仍必须能断掉它——这是 `sseConnectionPool` 存在的唯一理由，也是本 AC 最容易被「看起来改对了」蒙混的点。
- 断流失败（如后端删除抛错）不改变本地缓存，由调用方决定是否展示错误。

---

## 六、审查 Agent 判定输出格式

```text
结论：通过 / 不通过

逐条判定：
  AC1 … AC10：通过 / 不通过 + 依据（文件:行）

缺陷基线复核：
  D1 … D6：已消除 / 仍可复现 + 依据（文件:行）

反验收检查：
  是否存在「以改动量替代结果态」的情形：是 / 否

不通过项清单（若有）：
  [文件名:行号] 问题描述｜违反 AC 编号｜修复建议
```

首行必须是「通过」或「不通过」。不通过时逐条列出文件名、行号、问题描述、违反条款与修复建议；不提修复建议的判定无效。

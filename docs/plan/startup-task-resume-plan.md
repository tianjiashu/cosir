# 客户端启动恢复任务方案

## 背景

当前客户端启动后会读取持久化的 `coding-agent.activeTaskId`，但实际恢复逻辑绑定在
`App.tsx` 的当前 `activeWorkspaceId` 任务列表加载流程里：

1. 启动加载 workspace 列表。
2. `workspaceStore.setWorkspaces()` 将第一个 workspace 设为 `activeWorkspaceId`。
3. `App.tsx` 只调用 `loadWorkspaceTasks(activeWorkspaceId)`。
4. 如果持久化 task 不在该 workspace 的任务列表中，就回退打开该 workspace 的首个 task。

这会导致一种启动异常：上次打开的 task 属于其他 workspace 时，首屏没有真正执行
`openTask(taskId)` 的完整回填路径，用户需要手动点击左侧 task 后，turn/events 才被拉取并展示完整内容。

## 根因

根因不是 ChatPanel 渲染问题，而是职责边界错误：

- “恢复上次活跃 task”是应用启动会话恢复能力。
- “按 workspace 懒加载 task list”是 Sidebar 数据展示能力。

当前实现把前者依赖在后者之上，导致启动恢复只能看见当前 workspace 的任务列表。task 本身已有全局唯一
`task_id`，后端也已有 `GET /tasks/{task_id}`，启动恢复不应通过 workspace list 间接查找 task。

## 设计目标

- 启动时优先恢复上次活跃 task，无论它属于哪个 workspace。
- 启动恢复以 task 详情接口为事实源，不依赖 workspace task list 是否已加载。
- Sidebar 的 workspace task list 仍保持懒加载，不为了恢复 task 扫描所有 workspace。
- 恢复失败路径清晰：持久化 task 不存在时清理脏状态并降级。
- `App.tsx` 保持装配职责，不继续堆叠启动恢复分支。

## 推荐方案

新增一个启动恢复编排 hook：

```text
apps/desktop/src/hooks/useStartupTaskResume.ts
```

它的唯一职责是：在 workspace 列表可用后，恢复客户端首屏 task。

### 启动恢复流程

1. 读取 `loadPersistedActiveTaskId()`。
2. 如果存在持久化 task：
   - 调用 `openTask(taskId)` 完成「选中 + 拉取 task/turns + 回填 events」的完整恢复（`openTask` 内部已
     `api.getTask` + `api.listTaskTurns`，无需前置探测）。
   - `openTask` 成功后，从 `taskStore` 的 `tasksById[taskId]` 拿到 `task.workspace_id`（`openTask` 已把
     task 实体写入缓存，见 `useTask.ts` 第 304-308 行），将 `workspaceStore.activeWorkspaceId` 对齐到它。
   - 后台触发 `loadWorkspaceTasks(task.workspace_id)`，让 Sidebar 列表与中央会话对齐（失败不影响中央会话，见「错误处理」）。
3. 如果恢复失败（`openTask` 失败）：
   - 区分 404 与网络错误：404 表示 task 已不存在 → 清理持久化 active task（经 `setActiveTask(null)` 单点出口），回退默认 workspace 首条 task；网络错误 → 不清理持久化，保留下次启动重试机会，本启动降级到默认 task 或空态。
4. 如果没有持久化 task：
   - 加载默认 workspace 的 task list。
   - 打开默认 workspace 的首条 task。
5. 如果 workspace 列表为空：
   - 不恢复 task，保留当前空 workspace 引导。

> 关于 `api.getTask` 与 workspace 对齐：`openTask` 内部已 `Promise.all([api.getTask, api.listTaskTurns])`
> （`useTask.ts` 第 300-303 行）并自行把 task 实体写入 `tasksById`（第 304-308 行），因此恢复 hook
> **绝不前置再调一次 `api.getTask`**。`task.workspace_id` 在 `openTask` 成功后直接从
> `useTaskStore.getState().tasksById[taskId].workspace_id` 读取（零额外请求），再对齐
> `activeWorkspaceId`。这样同一 task 全程只发一次 `GET /tasks/{task_id}`（由 openTask 发出）。
> 404/失败的判定完全由 `openTask` 的 try/catch 承担（`useTask.ts` 第 342-346 行），恢复 hook 据此区分 404 与网络错误。

### 建议调整的接口

`useTask.openTask` 当前签名是：

```ts
openTask(taskId: string, forceRefresh?: boolean): Promise<void>
```

**保持现有签名不变**，不引入 `reason`/options 抽象。理由：

- 业务代码中 `openTask` 的所有调用点（`App.tsx` 恢复分支、`Sidebar.tsx` 点击 task）均不传 `forceRefresh`，
  默认 false，即不存在任何 `forceRefresh: true` 的业务调用。冷启动首次打开时 `eventStore` 缓存本就为空，
  `openTask` 会走网络回填（`useTask.ts` 第 329-331 行「缓存为空即回填」），因此恢复路径**无需**
  `forceRefresh: true` 即可正确回填，不存在「缓存命中导致回填被跳过」的问题。
- `reason: "user" | "startup" | "refresh"` 枚举当前零消费，属于「为未来可能而引入」的推测性 API，违反
  防膨胀原则。恢复来源语义已由 `App.tsx` 现有日志的 `from_persisted` 字段表达，无需额外枚举。
- 若未来确有「按打开来源做日志/打点/缓存」的需求，届时再以真实消费方为驱动引入，不在本方案预置。

## 模块边界

### `App.tsx`

只保留顶层装配：

- 加载 workspace list。
- 调用 `useStartupTaskResume(...)`。
- 负责布局渲染。

不再直接编排“找 task、回退 task、打开 task”的细节。

**必须删除现有恢复分支**：`App.tsx` 第 148-190 行的 `loadTasks` effect 及配套的 `resumeAttempted` ref、
`openTaskRef`、`persistedId` 快照逻辑一并移除（Sidebar 懒加载已由 `useWorkspaceTaskLazyLoad` 的
`ensureLoaded` 独立承担，不依赖 App 的 `loadTasks` effect 补拉）。若不删除，新 hook 与旧 effect
会形成「两套去重机制并存、互相看不见对方」的**双重恢复**。

### `useStartupTaskResume.ts`

负责启动恢复完整业务流：

- 去重，确保启动生命周期只恢复一次（hook 内部用 ref，作为**唯一**恢复去重点；触发条件以
  「workspace 列表已非空」为准，而非 `activeWorkspaceId` 变化）。
- 读取持久化 task。
- 协调 `taskStore`、`workspaceStore`、`loadWorkspaceTasks`、`openTask`。
- 处理降级和日志。

触发与时序约束：

- **触发条件**：以 `workspaces` 列表从「空 → 非空」作为恢复触发信号，而不是 `activeWorkspaceId`。
  因为 `workspaceStore.setWorkspaces` 会把第一个 workspace 赋给 `activeWorkspaceId`（`workspaceStore.ts`
  第 50-55 行），若仍以 `activeWorkspaceId` 为触发，对齐 `activeWorkspaceId` 到持久化 task 所属
  workspace 时会**二次触发**自身，造成重复恢复。
- **与 delegation 流的衔接**：`useDelegationStreams(activeTaskId)` 依赖 `[taskId]`（
  `useDelegationStreams.ts`）。注意在「持久化 taskId 与恢复 taskId 相同」的最常见场景下，恢复时
  `setActiveTask` 后 `activeTaskId` 字符串值不变，`[taskId]` effect 不会因此重建；真正驱动 delegation
  重新推导的是 events 回填导致的 `taskEvents` 变化（该 hook 的第二个 effect 依赖含 `taskEvents`）。
  这是预期行为，无需额外处理，但实现者应知悉触发归因是 `taskEvents` 而非 `activeTaskId`，不在 hook
  内重复订阅 delegation 流。
- **与 Sidebar 懒加载的衔接**：`loadWorkspaceTasks(task.workspace_id)` 复用模块级 `inflightLoads`
  去重（`useWorkspaceTaskLazyLoad.ts`），与 Sidebar 的 `ensureLoaded` 不会重复请求同一 workspace。

### `useWorkspaceTaskLazyLoad.ts`

继续只负责 workspace task list 的懒加载：

- 不承担 active task 恢复。
- 不隐式切换中央会话。
- 不扫描所有 workspace 来寻找持久化 task。

### `taskStore.ts`

**不新增 action**。持久化清理沿用现有单点出口：

- `setActiveTask(null)` 已经 `applyActiveTask` → `persistActiveTaskId(null)` 清空 localStorage
  （`taskStore.ts` 第 296-299、94-105 行），`setActiveTask(null)` 的语义就是「清空活跃任务」，是
  直接、正确的单点入口，不存在「间接表达」问题。
- 恢复失败路径直接调 `setActiveTask(null)` 即可完成「清理持久化脏值」，无需新增
  `clearPersistedActiveTask()` 这类与统一出口并行的冗余 action（避免重复造轮子）。
- 若确需语义更明确的封装，在 `useStartupTaskResume` 内部封装为私有函数，不必暴露到 store。

## 错误处理

- `openTask(persistedTaskId)` 失败且为 404：task 已不存在 → 记录 info/warn，调 `setActiveTask(null)` 清理持久化 ID，回退默认 task。
- `openTask` 失败且为网络错误：不清理持久化 ID，只记录 error，并允许下次启动重试；当前启动降级到默认 task 或空态。
- `openTask` 的 events 回填失败（非致命）：`openTask` 内部已保留骨架并记录 `eventsError`（`useTask.ts` 第 333-340 行），恢复 hook 不额外处理，用户手动点击 task 或刷新时可重试。
- `loadWorkspaceTasks(task.workspace_id)` 失败：不影响中央会话恢复，只影响 Sidebar 分组展示，并沿用现有 failed workspace 状态。

> 404 与网络错误的区分：`api.getTask` 抛出的 `ServiceError` 携带 `statusCode`（`api.ts` 的 `buildError`），
> 恢复 hook 在 catch 中按 `err.statusCode === 404` 区分，其余按网络/服务错误处理。

## 测试计划

新增/调整 Vitest 覆盖以下场景：

- 有持久化 task，且 task 属于第一个 workspace：启动打开该 task，并回填 events。
- 有持久化 task，且 task 属于非第一个 workspace：启动仍打开该 task，不回退首个 workspace task。
- 持久化 task 404：清理持久化 ID（`setActiveTask(null)` 被调用），并回退默认 workspace 首条 task。
- 持久化 task 网络错误：不清理持久化 ID，记录错误，并降级展示默认 task 或空态。
- 没有持久化 task：保持默认 workspace 首条 task 的现有行为。
- workspace 列表为空：不调用 `openTask`，展示 workspace 引导。
- **无重复请求**：恢复启动时 `api.getTask` 全程仅被调用一次（由 `openTask` 内部发出），不得出现
  「前置探测 + openTask 内部再拉一次」的双重 `GET /tasks/{id}`（mock 断言 `api.getTask` 调用次数 === 1）。
- **无双重恢复**：新 hook 与 App 层互斥——迁移后 App 不再有恢复分支，恢复仅由 `useStartupTaskResume`
  触发一次（集成断言 `openTask` 仅被调用一次）。

## 迁移步骤

1. 为当前问题补 App 或 hook 级失败测试，先复现“持久化 task 在非首个 workspace 时启动打开错误 task”。
2. 新增 `useStartupTaskResume.ts`，把启动恢复编排从 `App.tsx` 抽出。
3. 调整 `App.tsx`：删除现有 `loadTasks` effect 的恢复分支（`resumeAttempted`/`openTaskRef`/`persistedId`
   快照），只保留 workspace list 加载与 `useStartupTaskResume(...)` 装配。
4. 跑桌面端相关测试，重点覆盖 `useTask.openTask`、`useWorkspaceTaskLazyLoad`、启动恢复新增测试
   （含「无重复请求」「无双重恢复」两条防护性断言）。

> 说明：本方案**不改** `openTask` 签名，**不新增** `taskStore` action，因此原「迁移 options 对象 /
> 新增 clearPersistedActiveTask」两步已取消。

## 非目标

- 不改变后端 task/turn/events API。
- 不改变 Sidebar 的懒加载策略。
- 不做跨 workspace 全量扫描。
- 不引入新的持久化格式迁移；当前只使用已有 `coding-agent.activeTaskId`。


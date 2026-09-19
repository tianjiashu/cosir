# 前端后端生命周期单一控制入口技术方案

日期：2026-09-19

状态：已完成独立复审，方案已按复审意见修订；本文件只描述方案，不代表代码已修改。

## 1. 结论

当前后端生命周期的真正所有者已经是 Tauri Rust `BackendSupervisor`，问题不在于
Rust 侧缺少 supervisor，而在于前端又出现了一套生命周期控制入口：

- `App.tsx` 通过 `initializeBackendRuntime` / `restartBackendRuntime` 负责启动门禁；
- `BackendStatusBanner` 自己调用 `backend_status`、`backend_runtime_config`、
  `restart_backend`，并直接调用 `setBackendBaseUrl`；
- `runtime-config.ts` 同时保存 runtime 地址、generation，并被 Assistant runtime
  订阅。

修复目标是把前端所有 Tauri 生命周期 IPC、状态轮询、generation 更新和重启操作集中在
`apps/desktop/src/runtime-config.ts` 的单一 runtime controller 中。React 组件只读取
状态并发出“请求重启”的意图，不再直接调用 Tauri command 或修改 runtime config。

本方案不修改后端业务协议、不引入新进程、不引入依赖，也不把 Assistant UI runtime
当成后端生命周期 supervisor。

## 2. 运行边界与事实所有权

```text
Tauri Rust BackendSupervisor
  ├─ 启动、停止、有限崩溃恢复、动态端口、进程树清理
  └─ backend_status / backend_runtime_config / restart_backend

React runtime-config controller
  ├─ 唯一前端 IPC facade
  ├─ 启动门禁、状态轮询、重启请求、generation 投影
  └─ 向 React 暴露独立的 status/runtime snapshot 和订阅

React UI
  ├─ BackendStatusBanner：显示状态、请求 retry
  └─ AssistantRuntimeSession：只消费 backend URL/generation，负责 Transport attach/reconcile
```

- 后端进程、后端状态和进程 generation 的权威事实在 Tauri supervisor。
- 前端 runtime snapshot 是 supervisor 状态的 UI/Transport 投影，不是第二套后端状态机。
- Assistant UI 的 runtime state 仍只负责当前任务的渲染与 Transport 控制，不负责启动、
  停止或重启后端。
- `AssistantRuntimeProvider` 继续位于当前 task session 的 Assistant runtime 子树中；
  不因为本次修复把它提升为全局桌面 provider，也不通过 status Banner 重建它。

## 3. 当前代码事实

### 3.1 已有的 Tauri supervisor 足够承担生命周期

`backend_supervisor.rs` 已经具备：

- `lifecycle_gate` 串行化生命周期操作；
- `lifecycle_generation` 防止旧进程/旧线程覆盖新状态；
- 有限 crash recovery budget；
- 动态 loopback 端口和 readiness 等待；
- `restart_backend` 的 stop → prepare_start → start 流程；
- 退出时由 `RunEvent::Exit` 调用 `BackendSupervisor::stop()`。

因此本次不需要在 React 侧复制这些机制。

### 3.2 当前重复入口

| 入口 | 当前职责 | 问题 |
|---|---|---|
| `src/App.tsx` | 首次启动、失败后重启、boot gate | 是合理的页面启动入口，但目前只覆盖启动阶段 |
| `src/runtime-config.ts` | 地址、generation、启动轮询、重启 facade | 已接近 controller，应成为唯一前端生命周期入口 |
| `components/backend-status-banner.tsx` | 状态轮询、读取地址、重启、修改 runtime snapshot | 越过 facade，形成第二套生命周期控制 |
| `assistant/runtime/*` | 根据 generation 变化恢复 Assistant Transport | 不应承担后端启动/停止；该职责应保留 |

### 3.3 Assistant UI 约束

官方文档：

- [AssistantRuntimeProvider](https://www.assistant-ui.com/docs/api-reference/context-providers/assistant-runtime-provider)
- [Assistant Transport](https://www.assistant-ui.com/docs/api-reference/transport/assistant-transport)
- [Resuming a run](https://www.assistant-ui.com/docs/runtimes/custom/local-runtime#resuming-a-run)

官方 Transport runtime 支持 `initialState`、`api`、`resumeApi`、`headers`、`body`、
`onError` 等装配项；官方语义中的 `resumeRun` 是重新连接活动中的 stream。项目现有
`useRuntimeRecovery` 已把“后端 generation 变化后读取 canonical snapshot，并对 active
Run attach”作为恢复逻辑；“attach 而非 business resume”是本项目 `/assistant/attach`
协议的约定，不是 assistant-ui 对所有 adapter 的通用保证。

因此：

1. 后端状态变为 `starting/failed/stopped` 时，前端只显示生命周期状态，不自动取消或
   重放业务 Run。
2. 后端重启成功且 generation 改变时，由现有 recovery 逻辑读取 canonical snapshot，
   决定 import terminal state 或重新 attach active Run。
3. `BackendStatusBanner` 不直接调用 `aui.thread.resumeRun()`，避免把后端重启和
   Assistant UI 的 Transport resume 语义混为一谈。`resumeRun` 只有在本项目已有
   attach endpoint 和 recovery 规则下，才可解释为重新订阅已有 Run；它不负责启动、
   停止或重启后端。

## 4. 推荐设计

### 4.1 将 `runtime-config.ts` 收口为单一前端 controller

不新增独立服务文件，优先在现有 `src/runtime-config.ts` 中完成收口，减少跨文件
生命周期逻辑。

建议将当前散落的逻辑整理为以下内部原语：

```ts
readSupervisorStatus(): Promise<BackendStatus>
readSupervisorConfig(): Promise<BackendRuntimeConfig>
applySupervisorConfig(config: BackendRuntimeConfig): void
refreshBackendRuntime(): Promise<void>
startBackendRuntimeMonitor(): () => void
restartBackendRuntime(): Promise<BackendRuntimeConfig>
```

其中：

- `readSupervisorStatus` 和 `readSupervisorConfig` 是唯一 Tauri 查询封装；
- `applySupervisorConfig` 是唯一更新 `backendBaseUrl` 与 generation 的入口；
- `refreshBackendRuntime` 统一 Banner 轮询与启动后的状态同步；
- `restartBackendRuntime` 是唯一前端重启入口；
- `startBackendRuntimeMonitor` 必须幂等，重复调用不得启动多个 interval。

`initializeBackendRuntime` 保留为启动门禁，但改为复用上述查询和 apply 原语，不再复制
状态读取/配置读取逻辑。`initializeBackendRuntime`、`refreshBackendRuntime` 和
`restartBackendRuntime` 必须共用 controller 内部的 operation epoch/token；异步结果只有
在 token 仍然有效时才能写入 projection。不能只对 monitor 响应做过期保护。

生命周期操作的并发规则如下：

- 新的 initialize/restart 操作开始时，使旧 operation token 失效；旧的 Tauri Promise
  不需要强行取消，但其结果和 finally 都不得再发布状态或配置；
- restart 请求采用 controller 级 single-flight，同一时刻多个 retry 复用同一个
  restart Promise，不向 Rust 重复发出 `restart_backend`；
- refresh 也采用 single-flight；restart 开始时使正在进行的 refresh 结果失效，restart
  成功后由同一份返回配置原子更新 status/runtime 两个 projection；
- monitor 的 status 查询和 ready 后的 config 查询必须属于同一个 refresh token；
  `starting/failed/stopped` 时不强行读取 config；
- App 启动门禁与 Banner retry 都只能调用 controller facade，不能各自维护锁、epoch
  或轮询。

### 4.2 分离“连接配置投影”和“状态展示投影”

为避免每 2 秒的 `starting/ready/failed` 状态刷新导致 Assistant Transport runtime
被误认为地址或 generation 发生变化，建议在同一个 controller 内提供两个只读投影，
以及两套独立订阅集合：

- `BackendRuntimeSnapshot`：`backendBaseUrl`、`generation`；供 Assistant runtime 和
  Workbench transport 使用；只有地址或 generation 变化时更新；
- `BackendStatusSnapshot`：`BackendStatus | null`；供启动页和 Banner 显示；状态变化
  不修改 URL/generation。
- `subscribeBackendRuntime` 和 `subscribeBackendStatus` 必须是两套 listener 集合；
  status listener 的通知不得触发 runtime listener，反之亦然。

两者仍由同一个 controller 更新，不形成两个生命周期所有者。这样可以保持
`AssistantRuntimeProvider` 稳定，只在真实 backend generation 变化时触发已有 recovery
路径。`AssistantRuntimeSession` 和 `WorkbenchAgentRunSurface` 只能订阅 runtime
projection；`BackendStatusBanner` 只订阅 status projection。

### 4.3 统一轮询与并发规则

controller 需要满足：

1. 同一时刻最多一个 `refreshBackendRuntime` 请求链；
2. monitor 启动幂等，清理时只清理自身 interval；
3. initialize、refresh、restart 共用 operation epoch；新操作开始后，所有旧 status、
   config、finally 回调均只能被丢弃；
4. 只有 `applySupervisorConfig` 可以更新 URL/generation；restart 成功时必须以同一份
   supervisor config 原子更新 runtime projection 和对应 ready status，避免短暂出现新
   generation + 旧 status；
5. supervisor 返回旧 generation 的配置时丢弃；相同 generation 但不同 URL 视为异常
   响应，记录诊断日志并保留当前 runtime，不允许同一 generation 无理由切换端口；
6. restart 失败时 status 收敛到 `failed`/`stopped`，runtime projection 标记为不可用，
   旧 URL 不得继续被视为可用；Assistant runtime 不因失败状态自动调用 `resumeRun`，
   只能等待下一次显式 retry；
7. monitor 查询失败不能伪造业务 Run 终态，只更新可展示的后端不可用状态；不能把
   transport 断开转换为 business cancel 或 business resume。

保留当前 2 秒状态检查频率即可，不新增 websocket、SSE 或后台服务。

### 4.4 `App.tsx` 只负责启动门禁和 monitor 生命周期

调整为：

1. 首次 mount 调用 `initializeBackendRuntime()`；
2. 成功后调用 `startBackendRuntimeMonitor()`；
3. effect cleanup 调用 monitor 返回的 disposer；
4. 启动失败继续显示 `BackendStartupScreen`/失败重试页；
5. 后续运行期 crash 或 stopped 状态由 controller 更新，Banner 显示并提供 retry。

`App.tsx` 不直接调用 `invoke`，也不读取或写入 Tauri status 细节。

### 4.5 `BackendStatusBanner` 改为纯订阅组件

Banner 只做三件事：

- 使用 `useSyncExternalStore` 订阅 `BackendStatusSnapshot`；
- 根据状态渲染 starting/failed/stopped；
- 点击 retry 时调用 `restartBackendRuntime()`。

删除其内部的：

- `invoke("backend_status")`；
- `invoke("backend_runtime_config")`；
- `invoke("restart_backend")`；
- `setBackendBaseUrl`；
- interval polling。

`retrying` 可以保留为纯 UI 防重复点击状态；它不是生命周期事实。

### 4.6 Assistant runtime 保持现有恢复语义

不修改 `AssistantRuntimeProvider` 的层级和 assistant-ui 的 Transport 装配方式。

只确认以下边界：

- `AssistantRuntimeSession` 和 `WorkbenchAgentRunSurface` 都订阅
  `BackendRuntimeSnapshot`，只关注 URL/generation；
- status-only 更新不调用 `importExternalState`、`resumeRun` 或重建 provider；
- generation 改变后继续由 `useRuntimeRecovery` 执行 `/assistant/state` canonical
  snapshot reconcile；
- runtime 从 unavailable 恢复为 available 时，即使 generation 未变化，也由
  `useRuntimeRecovery` 执行一次 canonical reconcile；这用于处理查询抖动，不把
  availability 变化误当成 business resume；
- Workbench 的 `useAssistantTransportRuntime`、`AssistantRuntimeProvider` 和
  `AttachBridge` 遵守同一规则；status-only 更新不得触发重复 attach；generation 改变后
  是否 attach 必须沿用主 Assistant runtime 的 active Run 判定；
- `RuntimeControlBridge`、Workbench `AttachBridge` 和 recovery resume 都必须受
  `BackendRuntimeSnapshot.available` 门控；重启中或失败时不得向不可用地址执行 attach，
  恢复后最多执行一次 attach/reconcile；
- active Run 只重新 attach，不能因为前端重新挂载而隐式重放；
- terminal Run 只在项目 TransportState 契约下，将经过校验的 canonical snapshot 通过
  `importExternalState` 导入，不把它表述为 assistant-ui 官方定义的后端 canonical API，
  也不从 assistant-ui 本地 state 推断后端事实。

## 5. 变更范围

### 必改文件

- `apps/desktop/src/runtime-config.ts`
  - 收口 Tauri 查询、轮询、配置 apply、重启和 status/runtime 两类 snapshot；
  - 提供幂等 monitor 和 stale response fencing。
- `apps/desktop/src/App.tsx`
  - 启动成功后启动 monitor，卸载时清理；
  - 保留 boot gate，不再承载额外 lifecycle 细节。
- `apps/desktop/components/backend-status-banner.tsx`
  - 改为只读订阅 + retry intent。

### 重点验证文件

- `apps/desktop/components/assistant/runtime/assistant-runtime-session.tsx`
- `apps/desktop/components/assistant/runtime/assistant-runtime-bridges.tsx`
- `apps/desktop/components/assistant/runtime/runtime-types.ts`
- `apps/desktop/components/assistant/runtime/use-runtime-recovery.ts`
- `apps/desktop/components/workbench-agent-run-surface.tsx`
- `apps/desktop/src-tauri/src/backend_supervisor.rs`

这些文件不改变 Assistant Transport endpoint 或业务 resume 语义；其中 Assistant runtime
桥接、recovery 和 Workbench 只增加生命周期 availability 门控，确保重启中不 attach，
以及同 generation availability flap 恢复后只做一次 canonical reconcile。

### 明确不改

- 不改 FastAPI Assistant Transport endpoint；
- 不改 Tauri command 名称和 Rust supervisor 状态机；
- 不在 React 中新增后端 stop/start/restart 逻辑；
- 不通过 assistant-ui 的 `cancelRun` 或 `resumeRun` 替代后端 supervisor 操作；
- 不引入全局状态库、事件总线、远程服务或新依赖。

## 6. 测试与验收

### 6.1 runtime-config 单元测试

补充或调整以下测试：

- monitor 重复启动只产生一个 interval；
- monitor cleanup 后不再更新 snapshot；
- status-only 更新只通知 status listener，不通知 runtime listener；runtime-only 更新只
  通知 runtime listener，不通知 Banner status listener；
- status-only 变化不增加 backend generation；
- backend 重启成功时 URL/generation/status 一次性收敛；
- restart 期间返回的旧 monitor 响应不会覆盖新 generation；
- 旧 `initializeBackendRuntime` 响应不能覆盖新 restart 的 status/runtime projection；
- cleanup 后已完成的旧请求不能发布 snapshot；
- 同时触发 App retry 和 Banner retry 时只发出一个 `restart_backend`，或明确返回已有
  restart Promise；
- stale generation 不得回退 URL/generation；
- 相同 generation + 不同 URL 的响应被拒绝并记录诊断日志；
- restart 失败后 status 为 failed/stopped，且不伪造 active Run 状态；
- 浏览器开发模式不调用 Tauri command。

### 6.2 组件测试

为 `BackendStatusBanner` 增加测试，确认：

- 组件不直接 import/call `@tauri-apps/api/core`；
- starting/ready/failed/stopped 显示正确；
- retry 只调用 `restartBackendRuntime`；
- retry 期间不会重复提交。

### 6.3 桌面生命周期验收

至少验证：

1. 首次启动：窗口可显示，backend ready 后应用进入 ready；
2. 正常运行期间 status 轮询不会重新挂载 Assistant runtime；
3. backend 崩溃一次：Tauri supervisor 有限恢复，前端按 generation 重新读取 snapshot；
4. backend 连续崩溃：进入 failed，不能无限重启；
5. 用户点击 Banner retry：只经过 runtime-config 的 restart facade；
6. 后端重启复用同一端口：generation 变化仍能触发 Assistant reconcile；
7. active Run 在后端重启后只 attach，不隐式重放；
8. `WorkbenchAgentRunSurface` 在 status-only 更新时不重复 attach，在 generation 更新时
   遵守与主 Assistant runtime 一致的 recovery 规则；
9. React StrictMode 或重复 mount/unmount 下 monitor 仍只有一个实例；
10. 应用退出：仍由 Tauri `RunEvent::Exit` 停止 backend。

## 7. 实施顺序

1. 先在 `runtime-config.ts` 抽取查询/apply 原语和 status/runtime 两类 snapshot；
2. 加入 monitor 单例、操作 epoch 和单飞 refresh；
3. 改 `App.tsx` 启动/清理 monitor；
4. 将 `BackendStatusBanner` 改为订阅式组件；
5. 运行 TypeScript、业务前端 ESLint、runtime-config/Banner 单元测试；
6. 最后做 Tauri backend crash/restart 和 Assistant attach 验收；
7. 由独立 Agent 依据当前代码与官方 Assistant UI 文档复审，确认没有重复 lifecycle
   owner、错误的 `resumeRun` 语义或 provider 重挂载问题。

## 8. 风险与回滚

- 风险：status snapshot 更新触发不必要的 Assistant runtime 重渲染。通过独立的
  runtime/status projection、独立 listener 集合和保持 URL/generation 对象稳定规避。
- 风险：monitor 或旧 initialize 响应晚于 restart 返回。通过共享 operation epoch、
  single-flight 和 generation 单调校验丢弃旧响应。
- 风险：Banner 从直接查询改为订阅后，启动页与运行期状态显示时序变化。通过
  `initializeBackendRuntime` 先完成启动门禁，再启动 monitor，保持当前用户可见流程。
- 回滚：核心实现只涉及 `runtime-config.ts`、`App.tsx`、`BackendStatusBanner` 三个前端
  文件；测试和必要的 runtime 消费者验证文件另计。不涉及 SQLite、FastAPI、Tauri
  supervisor 数据或协议回滚。

## 9. 复审结果

独立子 Agent 已基于当前代码完成交叉审查，并使用官方 assistant-ui 文档核对
`AssistantRuntimeProvider`、`useAssistantTransportRuntime`、`resumeApi`、`resumeRun` 和
`initialState` 语义。复审结论为“方案方向正确，但必须修订后实施”，未发现需要修改 Rust
supervisor 或 Assistant Transport 后端协议的 P0 问题。

已纳入本方案的关键修订：

- Tauri supervisor 仍是 backend 生命周期唯一事实所有者；
- React 不再直接 invoke backend lifecycle commands；
- Assistant UI provider/runtime 层级和 `useAssistantTransportRuntime` 语义没有被改变；
- `resumeApi`/`resumeRun` 的“attach 已有 Run”解释明确限定为本项目 endpoint 契约，
  不冒充 assistant-ui 的通用保证；
- initialize、monitor、restart 共用 operation epoch，旧异步响应不能覆盖新状态；
- status/runtime 使用独立 listener 集合，避免 status-only 触发 Assistant 或 Workbench
  provider 重建；
- Workbench runtime、`AttachBridge` 和重复 attach 风险纳入验证；
- 相同 generation + 不同 URL、restart 失败、StrictMode cleanup 等边界已纳入设计和测试。

官方参考：

- [AssistantRuntimeProvider](https://www.assistant-ui.com/docs/api-reference/context-providers/assistant-runtime-provider)
- [Assistant Transport](https://www.assistant-ui.com/docs/runtimes/custom/assistant-transport)
- [Resumable streams](https://www.assistant-ui.com/docs/guides/resumable-streams)

实施前的最终门槛仍是：通过现有 TypeScript、前端 lint、runtime-config/Banner 单元测试，
并完成 Tauri backend crash/restart、主 Assistant runtime 与 Workbench attach 验收。

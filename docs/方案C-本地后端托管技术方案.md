# 方案 C：本地后端托管技术方案

本文档用于规划 `coding-agent` 在第二阶段采用“方案 C”完成本地 Python 后端托管的技术方案。

方案 C 的定义是：

- **开发期**：继续使用项目内 `apps/backend/.venv` 作为 Python 运行时。
- **架构上**：从现在开始就按“桌面端托管本地后端进程”的正式形态设计。
- **后续打包期**：允许把 `.venv Python` 替换成 bundled Python runtime，但不要求本阶段一次完成。

本文档不展开详细代码实现，而是说明：

- 应把能力写到哪些文件
- 每个文件承担什么职责
- 为什么这样拆分更符合当前项目规范

## 一、方案选择结论

当前推荐采用方案 C，而不是直接采用“系统 Python”或“一开始就做完整 bundled runtime”的原因如下：

### 1. 兼顾落地速度与未来演进

- 直接使用项目内 `.venv`，开发期可以最快完成后端托管闭环。
- 同时托管逻辑不应依赖 `.venv` 这个事实，而应依赖“后端运行时定位器”。
- 这样后续切换到 bundled Python 时，只需要替换运行时解析层，不需要推翻托管架构。

### 2. 最符合当前项目阶段

当前项目的第一矛盾不是“怎么打包 Python 最优雅”，而是：

- 后端是不是由桌面端真实托管
- 用户是否还要手动照顾后端
- 后端异常能否被桌面端感知和处理

方案 C 先解决这个核心问题。

### 3. 更符合开发规范

根据 `rules/Agent代码开发规范.md` 与 `rules/Agent客户端代码开发规范.md`：

- 不能把“进程启动”“状态机”“前端桥接”“日志查看”堆进一个文件
- 必须按职责拆分
- 必须保留可扩展点

方案 C 更适合按分层和单一职责逐步建设。

## 二、目标与非目标

## 目标

第二阶段本地后端托管能力的目标是：

- 桌面应用启动时可自动拉起本地 Python 后端
- 桌面应用能探测后端健康状态和配置摘要
- 桌面应用能停止、重启、查看后端状态
- 后端异常退出时，桌面应用能感知并提示
- 后端启动失败时，桌面应用能提供结构化错误和日志入口
- 未来可以从 `.venv Python` 切换到 bundled Python，而不改前端调用协议

## 非目标

本方案当前不覆盖：

- 完整 Python 打包与安装器策略
- 完整 sandbox
- 多后端实例并行运行
- 远程后端部署
- 复杂运维面板

## 三、总体分层建议

建议把本地后端托管拆成四层：

```text
前端 UI / Zustand / Hook
    ↓
Tauri IPC Command 层
    ↓
Rust 托管服务层
    ↓
Python 后端进程
```

进一步拆开后：

```text
React 组件层
  └─ 只展示后端状态和错误

前端 Hook / Service 层
  └─ 只调用 Tauri command，不自己管理子进程

Tauri Command 层
  └─ 只做参数入口、返回结构化结果

Rust 托管服务层
  ├─ 后端运行时定位
  ├─ 进程启动与停止
  ├─ 健康检查与状态机
  ├─ 日志路径与错误摘要
  └─ 生命周期协调
```

这样拆的原因：

- React 组件不应知道 Python 如何启动
- Tauri command 不应承载复杂状态机
- Rust 托管服务层不应混入 UI 语义
- 后续 bundled runtime 替换时，只需要改“运行时定位”和“启动参数”层

## 四、目录与文件落点建议

## 4.1 Tauri Rust 侧

当前 `apps/desktop/src-tauri/src/` 只有：

- `main.rs`
- `lib.rs`
- `commands.rs`

这不足以承载正式的本地后端托管。建议拆分为：

```text
apps/desktop/src-tauri/src/
  main.rs
  lib.rs
  commands/
    mod.rs
    logging.rs
    backend.rs
  backend/
    mod.rs
    types.rs
    runtime_locator.rs
    process_launcher.rs
    health_checker.rs
    supervisor.rs
```

### `main.rs`

职责：

- 保持极薄入口
- 只调用库入口 `run()`

理由：

- 已经符合单一职责，不应增加新逻辑

### `lib.rs`

职责：

- 装配 Tauri Builder
- 注册 command 模块
- 初始化 backend supervisor 状态

理由：

- `lib.rs` 应是应用装配入口，而不是后端托管实现文件
- 新能力应以模块注入方式接入，不继续把逻辑堆在这里

### `commands/mod.rs`

职责：

- 统一导出 command 模块

理由：

- `commands.rs` 目前职责会越来越宽
- 拆成目录后，能让“日志命令”和“后端托管命令”各自独立

### `commands/logging.rs`

职责：

- 保留前端日志写入命令
- 不承担后端托管职责

理由：

- 日志写入和进程托管是完全不同的变更理由
- 符合单一职责规范

### `commands/backend.rs`

职责：

- 暴露给前端的后端托管命令入口

建议包含的命令：

- `backend_start`
- `backend_stop`
- `backend_restart`
- `backend_status`
- `backend_logs_tail`

理由：

- command 层只做 IPC 边界
- 参数解析、返回结构和错误映射留在这里
- 不能把进程管理细节直接写进 command

### `backend/types.rs`

职责：

- 定义 Rust 侧后端托管相关类型

建议包含：

- `BackendStatus`
- `BackendHealthSnapshot`
- `BackendLaunchConfig`
- `BackendStartResult`
- `BackendErrorSummary`

理由：

- 状态枚举、健康摘要、错误摘要未来会被 command、supervisor、前端共同依赖
- 提前独立类型文件，避免 supervisor 和 command 互相复制结构

### `backend/runtime_locator.rs`

职责：

- 解析本地 Python 后端运行时位置
- 解析工作目录、`.venv/bin/python`、`python.exe`
- 解析 `apps/backend/.env.local`、`.env` 等约定路径

理由：

- 方案 C 的核心就是“开发期用 `.venv`，后续允许替换”
- 所以“运行时定位”必须独立，不能写死在启动器里
- 后续切换 bundled runtime 时，优先改这个文件

### `backend/process_launcher.rs`

职责：

- 组装启动命令
- 设置环境变量
- spawn Python 后端进程
- 返回子进程句柄、pid、port、启动时间等

理由：

- 这是纯进程启动职责
- 不应混入健康轮询、重启策略、前端状态

### `backend/health_checker.rs`

职责：

- 请求 `GET /health`
- 判断后端是否真正可用
- 判断 provider / model / has_api_key / base_url 是否符合预期

理由：

- “端口上有进程”不等于“后端已准备好”
- 需要把进程级健康和服务级健康分开建模

### `backend/supervisor.rs`

职责：

- 维护后端托管状态机
- 协调 launcher 与 health checker
- 监听进程退出
- 提供 start / stop / restart / status

状态建议：

- `stopped`
- `starting`
- `running`
- `stopping`
- `failed`
- `restarting`

理由：

- 这是整套方案的核心
- 但它应该是协调层，不直接承担 command、路径解析、HTTP 检查等细节职责

### `backend/mod.rs`

职责：

- 汇总导出 backend 托管模块

理由：

- 保持 `lib.rs` 装配时引用清晰

## 4.2 前端 TypeScript 侧

建议在 `apps/desktop/src/` 里这样落点：

```text
apps/desktop/src/
  services/
    backend.ts
  hooks/
    useBackend.ts
  stores/
    backendStore.ts
  components/
    layout/
      TopBar.tsx
    right-panel/
      BackendStatusBlock.tsx
```

如果你希望更保守，也可以先不加 `BackendStatusBlock.tsx`，先把状态接到 `TopBar`。但从长期看，后端状态会越来越丰富，最好独立出来。

### `services/backend.ts`

职责：

- 封装前端对 Tauri backend commands 的调用

建议包含：

- `startBackend()`
- `stopBackend()`
- `restartBackend()`
- `getBackendStatus()`
- `getBackendLogsTail()`

理由：

- 当前 `services/api.ts` 是 HTTP 到 Python 后端的封装
- 后端托管是前端到 Tauri Rust 的封装，不应混进 `api.ts`
- 否则会把“业务 API”和“桌面基础设施控制”混在一起

### `hooks/useBackend.ts`

职责：

- 负责组合后端托管相关前端行为
- 驱动状态拉取、重试、UI 行为

建议承载：

- 初次挂载时自动探测
- 必要时触发自动启动
- 暴露 `start/stop/restart/refresh`
- 暴露结构化状态与错误摘要

理由：

- 当前这个 hook 已经是后端状态入口
- 适合作为前端状态行为聚合层
- 但不应直接调用 HTTP `/health` 以外的复杂逻辑，应通过 `services/backend.ts`

### `stores/backendStore.ts`

职责：

- 存储后端托管状态

建议状态：

- `status`
- `healthSnapshot`
- `lastError`
- `lastStartedAt`
- `pid`
- `port`
- `isExpectedProvider`

理由：

- 后端状态已经不再只是一个 hook 局部状态
- 右侧面板、顶部状态栏、错误提示都可能依赖
- 按客户端开发规范，领域状态应进入 store，而不是散在多个组件里

### `components/layout/TopBar.tsx`

职责：

- 展示最小后端运行状态信号

建议只展示：

- `启动中`
- `运行中`
- `异常`

理由：

- TopBar 适合承载全局状态信号
- 但不要在这里展示大量诊断细节

### `components/right-panel/BackendStatusBlock.tsx`

职责：

- 展示后端状态详细信息

建议展示：

- provider
- model
- API key 是否存在
- 当前端口
- 最近错误摘要
- 查看日志入口
- 手动重启入口

理由：

- 这类信息超出 TopBar 的信息密度
- 应放进右侧信息面板，符合当前产品布局

## 4.3 Shared 类型层

建议在 `packages/shared/ts/` 新增：

```text
packages/shared/ts/
  backend.ts
```

### `packages/shared/ts/backend.ts`

职责：

- 定义前端与 Tauri command 共享的后端托管状态类型

建议包含：

- `BackendStatus`
- `BackendHealthSnapshot`
- `BackendErrorSummary`
- `BackendStatusResponse`

理由：

- 当前 `packages/shared/ts/api.ts` 主要面向 Python HTTP API
- 本地桌面托管状态属于另一类跨端契约
- 单独建文件更符合单一职责，也避免污染 API 路径定义文件

## 4.4 文档与运行配置

建议补充以下文档或配置：

```text
apps/backend/.env.example
docs/tech-stack.md
docs/desktop-client-development-plan.md
docs/第二阶段迭代路线图.md
```

### `apps/backend/.env.example`

职责：

- 明确后端本地运行所需配置项

理由：

- 当前本地 env 已在使用
- 需要把可复制的配置模板固定下来

### `docs/tech-stack.md`

建议补充：

- 说明第二阶段开发期由 Tauri 托管 `.venv Python`
- bundled runtime 作为后续演进项

理由：

- 技术栈文档应记录实际运行事实

### `docs/desktop-client-development-plan.md`

建议补充：

- 后端托管状态流
- 前端与 Tauri command 的交互边界

理由：

- 当前客户端开发计划更偏聊天主链路
- 需要把“本地后端托管”升级成客户端正式能力

## 五、状态机建议

建议后端托管状态机如下：

```text
stopped
  -> starting
  -> running
  -> failed

running
  -> stopping
  -> restarting
  -> failed

failed
  -> starting

stopping
  -> stopped

restarting
  -> starting
```

必须区分两种失败：

- **启动失败**
  - Python 不存在
  - `.venv` 缺失
  - 端口被占用
  - env 配置错误

- **运行后失败**
  - 子进程异常退出
  - `/health` 超时
  - 返回 provider 配置不符合预期

这样拆的理由：

- 用户提示文案不同
- 日志定位方式不同
- 是否允许自动重试也不同

## 六、命令设计建议

建议前端只依赖以下 Tauri commands：

- `backend_start`
- `backend_stop`
- `backend_restart`
- `backend_status`
- `backend_logs_tail`

不建议一开始暴露更多命令，例如：

- `backend_spawn_python`
- `backend_ping_port`
- `backend_check_env`

理由：

- 前端不应了解过多基础设施细节
- 这些细节应封装在 Rust supervisor 内部

## 七、日志设计建议

按照开发规范，本方案必须有明确日志路径和诊断边界。

建议日志分为两类：

### 1. 桌面端日志

- `desktop.log`
- 记录前端日志、Tauri command 错误、托管状态切换

### 2. 后端日志

- `app.log`
- `backend.log`
- 记录 Python 后端运行和 API 异常

Rust 托管层建议额外记录：

- 启动命令摘要
- 使用的 Python 路径
- 工作目录
- 端口
- health check 结果
- 启动耗时
- 异常退出码

禁止记录：

- API Key 原文
- 完整 env 内容

## 八、为什么不建议直接把能力堆进现有文件

以下做法不建议采用：

### 1. 把所有 Rust 逻辑都继续堆进 `commands.rs`

问题：

- `commands.rs` 会同时承担日志写入、命令入口、进程管理、状态机、健康检查
- 明显违反单一职责

### 2. 把后端托管逻辑写进 `useBackend.ts`

问题：

- React hook 不应承担本地子进程生命周期管理
- 这会把基础设施控制和 UI 状态搅在一起

### 3. 把后端托管协议塞进 `services/api.ts`

问题：

- `api.ts` 面向 Python HTTP API
- Tauri command 是桌面基础设施接口，不是业务 API

### 4. 让前端组件直接 `invoke()`

问题：

- 违反客户端分层规范
- 状态、错误、日志会分散到多个组件

## 九、建议实施顺序

建议按下面顺序推进：

1. Rust 侧先完成 `runtime_locator`、`process_launcher`、`types`
2. 再完成 `health_checker`
3. 再完成 `supervisor`
4. 再暴露 `commands/backend.rs`
5. 前端新增 `services/backend.ts`
6. 重构 `useBackend.ts`
7. 新增 `backendStore.ts`
8. 最后把状态接入 `TopBar` 和右侧面板

这样推进的理由：

- 先把底层事实做稳，再暴露给前端
- 避免 UI 先行，结果后端托管协议反复改动

## 十、最终结论

方案 C 的关键不是“继续用 `.venv`”，而是：

- 用 `.venv` 作为当前运行时事实
- 把后端托管做成正式能力
- 把“运行时定位”“进程启动”“健康检查”“状态机”“前端桥接”严格拆层

按当前项目规范，最合理的落点是：

- Rust 侧新增 `backend/` 目录承载托管核心能力
- `commands/` 目录只保留 IPC 边界
- 前端新增 `services/backend.ts`、`stores/backendStore.ts`
- `useBackend.ts` 只做前端行为聚合
- shared 层新增 `packages/shared/ts/backend.ts`

这是当前阶段最符合单一职责、分层架构、日志可排查和后续 bundled runtime 演进要求的写法。

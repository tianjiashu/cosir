# Cosir 桌面窗口与托盘生命周期技术方案

**状态**：待实现方案  
**日期**：2026-09-09  
**适用范围**：`apps/desktop` Tauri 2 桌面应用（Windows、macOS）

## 1. 目标与非目标

### 1.1 目标

应用启动后：

1. 点击主窗口关闭按钮（Windows 的 `X`、macOS 的红色关闭按钮）只隐藏主窗口，不退出 Cosir。
2. 隐藏期间，Tauri 宿主、本机 FastAPI 后端、LangGraph Agent 和正在运行的工具继续按现有生命周期运行。
3. Windows 在通知区域提供 Cosir 图标；macOS 提供状态栏图标，并保留 macOS 的 Dock 入口。
4. 点击托盘/状态栏图标可以恢复并聚焦主窗口。
5. 右键托盘图标显示菜单，菜单中的“退出 Cosir”才真正退出应用并停止后端进程树。
6. 已有单实例行为继续成立：二次启动不创建第二个实例，而是恢复已有主窗口。
7. macOS Dock 点击应用但当前没有可见窗口时，恢复已有主窗口。

### 1.2 非目标

- 不把托盘逻辑放到 React、FastAPI 或 Agent Runtime。
- 不新增认证、多用户、后台服务、远程队列或云端控制面。
- 不改变 Assistant Transport、Conversation snapshot、SQLite schema 或 Agent run 语义。
- 不在本任务中实现开机自启、系统通知、托盘图标动态状态或可配置关闭策略。
- 不声称“退出应用后 Agent 自动续跑”；当前项目的真实退出会停止后端进程，正在执行的 run 会被中断。

## 2. 代码事实与运行边界

### 2.1 当前拓扑（代码事实）

```text
Tauri Rust 宿主进程
  ├─ 创建窗口、单实例和应用退出生命周期
  └─ BackendSupervisor
       └─ 本机 FastAPI 子进程（127.0.0.1 + 动态端口）
            ├─ LangGraph Agent Runtime
            ├─ 工具/CodeGraph 子进程
            └─ SQLite conversation/task 数据

Tauri WebView / React renderer
  └─ 只负责渲染 Assistant Transport snapshot 和发起业务请求
```

当前 Tauri 装配、单实例回调、启动和退出处理位于
`apps/desktop/src-tauri/src/lib.rs`；后端子进程的启动、探活、崩溃恢复和停止位于
`apps/desktop/src-tauri/src/backend_supervisor.rs` 与
`apps/desktop/src-tauri/src/backend_process.rs`。

当前代码**尚未**创建 tray、尚未拦截 `CloseRequested`，也尚未提供托盘退出菜单；下面的 tray 和窗口隐藏行为都是本方案的目标状态，不是现状能力。

当前项目已使用 Tauri 2、单实例插件和 Windows Job Object；项目清单已经将“关窗不退出”列为 A2.4，原因是长时间 run 不应因窗口关闭而中断。参见
[`docs/本地应用能力清单.md`](../本地应用能力清单.md)。

### 2.2 目标拓扑

```text
Tauri Rust 宿主进程
  ├─ TrayIconBuilder / tray menu / tray event
  ├─ main window CloseRequested → hide
  ├─ single-instance / macOS Reopen → restore
  └─ BackendSupervisor
       └─ 本机 FastAPI 子进程（127.0.0.1 + 动态端口）
            ├─ LangGraph Agent Runtime
            ├─ 工具/CodeGraph 子进程
            └─ SQLite conversation/task 数据

Tauri WebView / React renderer
  └─ 只负责渲染 Assistant Transport snapshot 和发起业务请求
```

### 2.3 所有权与边界

| 问题 | 决策 |
|---|---|
| 行为由哪个进程拥有 | Tauri Rust 宿主进程 |
| 是否跨进程 | 基础功能不跨进程；托盘和窗口事件均在 Tauri 进程处理 |
| 权威状态在哪里 | 窗口可见性由操作系统/Tauri 持有；后端 run、对话和文件事实仍由 FastAPI/SQLite 持有 |
| React 的职责 | 不监听并模拟窗口生命周期，不保存“应用是否退出”的业务状态 |
| FastAPI 的职责 | 不感知窗口隐藏；继续提供 Agent、snapshot、取消和恢复连接能力 |
| 网络要求 | 不新增网络；后端继续只监听 `127.0.0.1` |

## 3. 用户交互契约

### 3.1 窗口关闭

主窗口收到 `WindowEvent::CloseRequested` 时：

1. 调用 `CloseRequestApi::prevent_close()`，阻止窗口销毁。
2. 调用 `window.hide()`，不调用 `window.close()`。
3. 不调用 `BackendSupervisor::stop()`。
4. 记录一条不包含密钥和用户内容的桌面生命周期日志，例如 `main_window_hidden`。

Windows 使用 `hide()` 使窗口从任务栏消失并由通知区域图标提供入口；不能只调用
`minimize()`，否则窗口仍可能保留任务栏按钮。

macOS 同样使用 `hide()`，不把普通窗口伪装成独立的后台服务。macOS 的 Dock 图标默认保留，状态栏图标提供额外的恢复入口。

### 3.2 恢复窗口

恢复操作统一复用一个 Tauri 宿主方法，避免单实例、托盘和 macOS Dock 各自实现一套逻辑：

```text
unminimize → show → set_focus
```

恢复方法必须对窗口已经可见、已经隐藏、处于最小化状态和恢复失败保持幂等；窗口恢复失败只能记录可诊断日志，不能停止后端。

触发来源：

- Windows/macOS 托盘图标左键释放。
- Tauri single-instance 回调。
- macOS `RunEvent::Reopen { has_visible_windows: false }`。

### 3.3 托盘菜单

托盘菜单保持最小化，只提供：

1. `打开 Cosir`：恢复并聚焦主窗口。
2. 分隔线。
3. `退出 Cosir`：真正退出应用。

托盘菜单不添加“关闭窗口”这种会与“退出应用”混淆的文案。Windows 官方将该区域称为 notification area；项目文案可以在中文界面中使用“通知区域/托盘”，但语义必须明确区分“隐藏窗口”和“退出应用”。

推荐关闭左键自动弹菜单：

- 左键：直接恢复窗口。
- 右键：显示菜单。

Tauri tray API 默认可能在左右键都显示菜单，需要显式关闭 left-click menu 行为。

### 3.4 真正退出

“退出 Cosir”菜单项调用 `AppHandle::exit(0)`，进入既有应用退出生命周期。退出处理必须满足：

```text
退出请求
  → 仅执行一次关闭流程
  → BackendSupervisor 停止后端进程树
  → Windows Job Object / 平台对应的子进程清理机制生效
  → Tauri event loop 退出
```

当前 `lib.rs` 同时在 `RunEvent::ExitRequested` 和 `RunEvent::Exit` 调用 `stop()`。实现时应保证 `stop()` 幂等，或收敛到一个明确的关闭时点，避免重复推进 generation、重复日志和重复清理。

退出正在运行的 Agent 时，当前契约是中断 run，而不是后台继续执行或自动续跑。第一版可以直接退出；若后续增加确认框，确认逻辑必须基于后端真实 active-run 状态，不得由 React 本地猜测。

### 3.5 平台差异

#### Windows

- X/Alt+F4：隐藏窗口。
- 通知区域左键：打开并聚焦。
- 通知区域右键：显示菜单。
- 菜单“退出 Cosir”：真正退出。

Windows 官方指南建议 notification area 主要用于长期运行且有后台价值的应用，并要求提供明确退出入口；Cosir 的长时间 Agent run 满足这一使用场景，但未来仍应考虑提供关闭策略设置。

#### macOS

- 红色关闭按钮：隐藏当前窗口，应用仍存在。
- 状态栏图标左键：恢复窗口。
- 状态栏图标菜单：提供打开和退出。
- Cmd+Q / 系统应用菜单 Quit：保持真正退出，不改成隐藏。
- Dock 点击且没有可见窗口：处理 `RunEvent::Reopen` 并恢复窗口。
- 状态栏图标使用 macOS template 图标，不直接使用彩色应用图标作为最终资源。

## 4. Tauri 实现设计

### 4.1 依赖与能力

在 `apps/desktop/src-tauri/Cargo.toml` 启用 Tauri 的 `tray-icon` feature，优先使用 Tauri 2 内置：

- `tauri::tray::TrayIconBuilder`
- `tauri::menu::{Menu, MenuItem}`
- `tauri::tray::{TrayIconEvent, MouseButton, MouseButtonState}`
- `WindowEvent::CloseRequested`
- `CloseRequestApi::prevent_close()`
- macOS `RunEvent::Reopen`

不引入第三方 systray crate。官方文档已覆盖 tray 创建、菜单、左右键事件和窗口恢复。

### 4.2 模块边界

建议将桌面生命周期从 `lib.rs` 的装配函数中抽出，但保持结构简单：

```text
apps/desktop/src-tauri/src/
├─ desktop_lifecycle.rs  # close/hide/restore/quit 的宿主策略
├─ tray.rs               # tray icon、菜单和 tray 事件
├─ lib.rs                # Builder 装配、backend supervisor 启停
└─ backend_supervisor.rs # 后端进程事实和停止幂等性
```

如果实现规模很小，可以先放入 `lib.rs`，但必须保持“托盘/窗口控制”和“后端进程控制”两个职责分区，不能把 tray 事件写入 React 或 backend supervisor 的领域逻辑。

### 4.3 关闭意图防抖

实现应能区分：

- 用户点击窗口 X：隐藏并阻止关闭。
- 托盘菜单主动退出：允许应用退出。
- 系统/平台退出：允许应用退出。

推荐在 Tauri 宿主维护一个只用于窗口生命周期的进程内标记，例如 `quit_requested`；该标记不是业务数据，不写 SQLite，也不暴露给 FastAPI。若 `app.exit(0)` 已绕过窗口 `CloseRequested`，仍需用测试确认；不能仅凭假设省略退出路径验证。

### 4.4 初始化顺序

建议顺序：

1. 创建 `BackendSupervisor` state。
2. 在 `setup` 中创建 tray 和菜单。
3. 为 `main` 窗口注册 `CloseRequested` 处理。
4. 启动后端 supervisor。
5. 显示主窗口。

托盘初始化失败属于桌面装配失败，应返回 setup 错误并让应用明确失败；不能静默启动一个没有恢复入口的后台进程。

### 4.5 图标资源

当前 `apps/desktop/src-tauri/icons/` 只有 `icon.ico`。实现前需要核对其是否包含适合通知区域的多尺寸图像，并补充 macOS 状态栏所需的透明 template PNG。

- Windows：至少覆盖高 DPI 下的通知区域尺寸，使用应用图标或专用 tray 图标。
- macOS：透明背景、单色 template 资源，构建 tray 时设置 template 语义。
- 不在日志中输出资源路径之外的敏感数据。

### 4.6 发行资源与后端运行时

tray 功能不能绕过 release 后端资源的装配。实现发行包时必须同时核对：

- `tauri.conf.json` 的 `bundle.resources` 或等价资源映射是否包含 `backend` 与 `backend-runtime`。
- 安装后的实际资源路径是否符合 `backend_runtime.rs` 的 release 查找逻辑。
- Python/解释器、uv cache 或其他 release 启动所需文件是否真实存在。
- CodeGraph 开启时所需的 Node/server 资源是否随包提供。
- 安装后的应用能启动后端、通过 `/health`，并能创建 tray；不能只验证窗口静态资源加载。

## 5. 生命周期与故障处理

### 5.1 启动

- Tauri 启动时创建 tray 和主窗口。
- 后端继续由已有 `BackendSupervisor` 动态分配 loopback 端口、启动并等待 `/health`。
- 前端继续通过 Tauri runtime config 获取后端地址。
- tray 初始化不依赖后端 ready；即使后端启动失败，用户仍可打开窗口查看失败态并重试。

### 5.2 隐藏期间

- WebView 可以继续存在，React 不需要卸载。
- FastAPI、LangGraph、工具和 checkpoint 继续使用现有机制。
- SSE/Assistant Transport 断开与窗口隐藏不是同一事件；隐藏不能触发取消请求。
- 用户从 tray 恢复时，前端不重建第二套对话事实，只继续使用已有 snapshot/runtime。

### 5.3 后端崩溃

托盘功能不改变现有 supervisor 策略：后端退出由 supervisor 感知，并按已有次数上限执行自动恢复；恢复失败时，窗口恢复后由前端展示失败态。隐藏窗口期间不得因为没有可见窗口而停止监控。

### 5.4 Tauri 进程崩溃或系统终止

这是进程级故障，不属于托盘状态机。Windows 继续依赖已有 Job Object；macOS/其他平台继续按现有子进程生命周期事实验证是否会留下孤儿进程。实现本功能时不得把“窗口隐藏”误当成“进程存活保证”。

macOS 的实现应优先采用 POSIX process group（例如后端启动时建立独立 process group，退出时先进行 graceful shutdown，再对该 group 执行 `killpg` fallback），或提供经过实机验证的等价机制。验收时在发出退出后等待不超过 5 秒，再采集 Tauri、FastAPI、CodeGraph 和工具进程快照；5 秒后仍有归属于 Cosir 的子进程即判定 C-09/L-02 失败。

### 5.5 真退出

退出流程必须保证：

- 不启动新的后端恢复。
- 不因为 supervisor monitor 线程竞态而重新拉起后端。
- 后端端口释放。
- Windows 后端及其工具子进程被 Job Object/清理流程收回。
- 已落库的 snapshot 不被前端删除或覆盖。

## 6. 安全与维护性约束

- 后端继续绑定 `127.0.0.1`，托盘不暴露任何新 HTTP 入口。
- 托盘菜单只调用 Tauri 宿主行为，不把菜单命令转发成可被网页内容伪造的任意 command。
- 日志只记录稳定英文 `event`、中文 `msg` 和安全的结构化 `data`，不记录 API key、完整 prompt 或工具原始输出。
- tray、窗口和后端退出操作必须可重复调用，失败时保留诊断日志。
- 不把窗口可见性写入 conversation/task snapshot；它不是 Agent 事实。
- 不添加认证、多租户、Redis、Postgres、远程 supervisor 或云端任务队列。

## 7. 验证策略

实现完成前，必须使用独立验收文档执行验证：

[`desktop-tray-lifecycle-acceptance.md`](desktop-tray-lifecycle-acceptance.md)

最低验证范围：

- Rust 编译和 Tauri 开发启动。
- Windows X→隐藏、tray→恢复、tray→退出。
- macOS 红色关闭→隐藏、状态栏/Dock→恢复、Cmd+Q→退出。
- 隐藏期间 Agent run 继续执行。
- 二次启动恢复已有实例，不产生第二个后端。
- 后端崩溃/恢复和应用真正退出时无孤儿进程。
- 若当前无法构建发行包，必须明确记录为未通过，不能用开发模式代替打包验收。

## 8. 完成定义

本技术任务只有在以下条件同时满足时才算完成：

1. 代码按本方案实现，且没有把生命周期责任扩散到 React/FastAPI。
2. 独立子 Agent 基于实际代码和验收文档完成审查。
3. 验收文档中的 P0 项全部通过，并有可复核证据。
4. Windows 和 macOS 的平台特有项均已验证，或明确标记为阻塞项并得到用户确认。
5. 失败、退出、恢复、单实例和后端子进程清理没有未解释的行为。

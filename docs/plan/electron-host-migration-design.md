# Cosir 桌面宿主迁移到 Electron 技术方案

**状态**：P0 验证已通过（macOS），待评审
**日期**：2026-10-30
**适用范围**：`apps/desktop`（宿主、preload、前端宿主适配层、打包、桌面 E2E），后端只涉及父进程存活契约与 CORS 来源两处

## 1. 结论

把桌面宿主从 Tauri 2（Rust + 系统 WebView）换成 Electron（Node main 进程 + 自带 Chromium）。
驱动力是**内置、可被 Agent 操控的浏览器**：它需要宿主里有一个支持 CDP 的 Chromium，而 Tauri 在 macOS 上用的
WKWebView 不支持 CDP，Tauri 的 CEF 运行时目前只有社区维护的 alpha 版本（`tauri-runtime-cef`，依赖 Tauri 3 alpha，
官方 `feat/cef` 分支未发布）。Claude 桌面应用与 Codex（ChatGPT 桌面应用）都采用同一种做法：Electron 宿主用
`WebContentsView` 嵌入页面，用 `webContents.debugger` 发 CDP 命令操控，再经本机桥接交给另一个进程里的 Agent 调用
（依据见附录 A）。

迁移的影响面集中在宿主一层：

- **要重写的**：`apps/desktop/src-tauri/` 约 2,400 行 Rust、前端 8 个直接使用 Tauri API 的文件、6 处拖动区标记、
  开发与打包脚本、桌面 E2E。
- **不动的**：Python 后端的业务代码、React 业务组件、Assistant Transport、SQLite、终端 worker（独立的 Rust 程序，
  由后端拉起）。
- **后端只有两处改动**：新增「宿主管道断开就退出」的存活契约，替代 Windows Job Object（见 5.3）；CORS 白名单加入
  `app://cosir` 来源，删除 Tauri 的三个来源（见 5.5，P0 验证中发现）。
- **借迁移一并完成的结构化改造**（5.7）：附件的按钮、粘贴、拖放三个入口合成一条「`File` + 按需取路径」的链路，
  拖放普通文件从此可用；后端状态从轮询改为宿主推送。同时删除只为 WKWebView 存在的 `webkit` 测试项目（5.8）。

迁移分四个阶段（第 10 节）。P1 在功能对等之外只包含上面两项结构化改造，浏览器能力在 P3 落地，详细设计另写文档；本文第 6 节只确定
它对宿主架构的约束，确保 P1 的结构不需要返工。

## 2. 目标与非目标

### 2.1 目标

1. 宿主的现有职责在 Electron 中全部对等实现：后端进程生命周期、启动门禁、有限次崩溃恢复、托盘与关闭隐藏、
   单实例、退出前确认、桌面日志、附件选择与读取、窗口外框。
2. 前端与宿主的控制面保持 AGENTS.md 规定的那几项能力，名称一一对应；React 业务组件只经过一个宿主适配层访问宿主。
3. 宿主结构为内置浏览器预留位置：浏览器由 main 进程持有，后端经本机管道调用（第 6 节）。
4. 删除 Tauri 相关的全部代码、依赖、配置、脚本和测试，不保留两套宿主并存。

### 2.2 非目标

- 不把后端改写成 Node，也不把任何业务逻辑放进 main 进程。
- 不改 Assistant Transport、snapshot、Run 语义和数据库结构。
- P1 不新增能力：不做自动更新、开机自启、系统通知，也不做浏览器功能。拖放普通文件可用是 5.7 A 结构化改造的
  自然结果，不是额外功能。
- 不兼容旧的 Tauri 安装与数据（绿地项目）；`~/.cosir` 下的数据目录与日志格式保持不变，所以也不需要迁移。

## 3. 代码事实

### 3.1 当前拓扑

```text
Tauri Rust 主进程（apps/desktop/src-tauri）
├─ 主窗口（WKWebView / WebView2）── React 前端
├─ 托盘、单实例、macOS 菜单、退出确认
└─ BackendSupervisor
     └─ Python/FastAPI 子进程（127.0.0.1 + 动态端口）
          ├─ 工具子进程
          └─ terminal-worker（Rust，PTY）
```

### 3.2 Rust 宿主模块清单

| 模块 | 行数 | 职责 |
|---|---|---|
| `backend_supervisor.rs` | 767 | 状态机（`starting` / `ready` / `failed` / `stopped`）、generation 防止旧线程覆盖、动态端口、90 秒就绪超时、崩溃恢复最多 1 次、健康检查失败重试；IPC 命令 `backend_status`、`backend_runtime_config`、`restart_backend`、`write_frontend_log` |
| `backend_runtime.rs` | 279 | 解析后端启动方式：开发模式用 uv 同步环境并解析出解释器（uv 不作为被监督进程，避免后端变成孙进程后泄漏）；`COSIR_BACKEND_DIR`、`COSIR_BACKEND_PYTHON`；打包版用 PyInstaller 产物 |
| `backend_process.rs` | 276 | spawn 后端并注入 `CODING_AGENT_PORT`、`CODING_AGENT_RELOAD`、`CODING_AGENT_BOOT_STATE_FILE`、`CODING_AGENT_DATA_DIR`、`CODING_AGENT_TERMINAL_WORKER`，补虚拟环境变量；**Windows Job Object `KILL_ON_JOB_CLOSE`**；停止时 Windows 用 `taskkill /T`，其他平台直接 kill |
| `backend_readiness.rs`、`backend_http.rs` | 123 | 等待 bootstate 文件与 `/health`，极简 HTTP GET |
| `exit_confirmation.rs` | 175 | 退出前查询 `/terminal/sessions/active-count`，有运行中终端就弹原生确认框；查询失败或超时直接放行 |
| `app_menu.rs` | 58 | macOS 专用：把默认菜单里的退出项换成自定义项，让 Cmd+Q 也能走退出确认 |
| `tray.rs` | 76 | 托盘、显示主窗口、「退出并停止运行」 |
| `desktop_log.rs`、`log_paths.rs`、`data_paths.rs` | 207 | 按日期和大小分片的 JSONL 日志；数据根在 macOS / Windows 是用户主目录，其他平台是应用数据目录，后端负责追加 `.cosir` |
| `file_access.rs` | 37 | `read_selected_attachment_file`、`resolve_selected_attachment_path` |
| `webview_diagnostics.rs` | 204 | 仅 Windows：WebView2 进程失败诊断 |
| `lib.rs` | 149 | 装配：关闭窗口时隐藏到托盘、单实例回调、macOS Dock 点击恢复窗口、桌面 E2E 专用命令 |

### 3.3 前端对 Tauri 的依赖

| 文件 | 用到的 API |
|---|---|
| `src/runtime-config.ts` | `invoke`（`backend_status`、`backend_runtime_config`、`restart_backend`），`__TAURI_INTERNALS__` 检测 |
| `lib/logging/frontend-log.ts` | `invoke("write_frontend_log")` |
| `lib/platform.ts` | `isTauri` |
| `components/composer/attachment-picker.tsx` | `invoke`（读取与解析附件），`plugin-dialog` 的 `open` |
| `components/composer/workspace-picker.tsx`、`components/configuration/skill-configuration-panel.tsx` | `plugin-dialog` 的 `open` |
| `components/layout/window-controls.tsx` | `getCurrentWindow`（Windows 自绘的窗口按钮） |
| 6 个文件 | `data-tauri-drag-region` 拖动区 |

### 3.4 构建、打包与测试

- 开发：`scripts/run-tauri-dev.mjs` 先构建 terminal-worker，再启动 Tauri；Tauri 的 `beforeDevCommand` 启动 Vite（3000 端口）。
- 打包：`build:bundle` 依次构建前端、`stage:terminal-worker`、`stage:backend`（PyInstaller）；Tauri bundle 把
  `target/resources/{backend,terminal-worker}` 打进安装包；`stage-portable.mjs` 生成便携版。
- 桌面 E2E：`tauri-plugin-wdio` 加 WDIO（`tests/tauri-e2e/desktop-smoke.spec.mjs`），由 `desktop-e2e` feature 启用。
- CI：`.github/workflows/terminal-worker-bundle.yml` 里有 `cargo install tauri-cli` 和 `npm run tauri:build`。

## 4. 目标拓扑与事实所有权

```text
Electron main 进程（apps/desktop/electron/main）
├─ 主窗口 BrowserWindow ── preload（contextBridge）── React 前端
├─ 托盘、单实例、退出确认、桌面日志
├─ BackendSupervisor
│    └─ Python/FastAPI 子进程（stdin 管道 = 宿主存活信号）
│         ├─ 工具子进程
│         └─ terminal-worker
└─ BrowserHost（P3）── 每个 Task 的 WebContentsView + CDP
      ▲ 本机管道 JSON-RPC
      └─ Python 后端的 browser_* 工具
```

所有权调整：

| 事实 / 职责 | 迁移前 | 迁移后 |
|---|---|---|
| 后端进程生命周期唯一所有者 | Tauri Rust 主进程 | Electron main 进程 |
| 宿主崩溃时清理后端 | Windows 用 Job Object；macOS 没有兜底 | 后端检测到 stdin 读到 EOF 后自行退出（两个平台都覆盖） |
| 前端到宿主的控制面 | Tauri IPC | preload 暴露的 `window.cosirHost`，渲染进程里只经过 `lib/desktop-host/` |
| 浏览器标签页实例（P3） | 无 | main 进程持有，属于进程内协调状态；Task 归属和回收时机由后端决定 |

AGENTS.md 里「产品形态与运行拓扑」「进程生命周期边界」「Tauri IPC」「代码目录边界」「日志打印与排查」几节在 P1
完成时同步重写。

## 5. 宿主设计

### 5.1 目录与构建

```text
apps/desktop/
├─ electron/
│  ├─ main/
│  │  ├─ app.ts                 装配：单实例、窗口、托盘、IPC、退出流程
│  │  ├─ window.ts              主窗口创建、关闭隐藏、外框参数、导航限制
│  │  ├─ tray.ts
│  │  ├─ exit-confirmation.ts
│  │  ├─ ipc.ts                 ipcMain.handle 注册，入参校验只在这一层做
│  │  ├─ file-access.ts
│  │  ├─ paths.ts               数据根、.cosir、logs、runtime
│  │  ├─ desktop-log.ts
│  │  └─ backend/
│  │     ├─ supervisor.ts       状态机、generation、崩溃恢复
│  │     ├─ runtime.ts          解析启动方式（uv、显式解释器、打包产物）
│  │     ├─ process.ts          spawn、环境变量、停止进程树
│  │     └─ readiness.ts        bootstate 文件 + /health
│  ├─ preload/index.ts          contextBridge 暴露 window.cosirHost
│  └─ shared/host-contract.ts   IPC 通道名与载荷类型（main、preload、renderer 共用）
├─ lib/desktop-host/            渲染进程的唯一宿主出口
└─ src/、components/、lib/      React 前端（基本不动）
```

- **渲染进程**：继续用现有的 Vite 配置和 3000 端口，不变。
- **main 与 preload**：用 esbuild（Vite 已经依赖它）编译成 CJS，输出到 `target/electron/`。开发模式下 watch 重新编译
  后重启 Electron。
- 不采用 electron-vite：它要求把渲染进程的 Vite 配置并进它的配置文件，迁移成本高，收益只是少写一个构建脚本。
- Electron 在实施时锁定当时稳定版的精确版本，最低要求 30（`WebContentsView` 从 30 开始提供）。

### 5.2 宿主控制面（IPC 契约）

preload 只暴露下面这些方法，不暴露 `ipcRenderer` 本身：

| Tauri 命令 / API | `window.cosirHost` | main 侧实现 |
|---|---|---|
| `backend_status` | `backend.status()` | supervisor 状态 |
| `backend_runtime_config` | `backend.runtimeConfig()` | `{ backendBaseUrl, generation, status }`，结构不变 |
| `restart_backend` | `backend.restart()` | 重置崩溃恢复次数后重启 |
| （前端目前靠轮询） | `backend.onStatusChanged(cb)` | 状态变化时推送完整快照，取代前端轮询（5.7 B） |
| `write_frontend_log` | `log.write(entry)` | 按 `FrontendLogEntry` 字段做白名单校验后写入 frontend 日志 |
| `read_selected_attachment_file`、`resolve_selected_attachment_path` | 删除，改为 `files.pathOf(file)` | 在 preload 里调用 `webUtils.getPathForFile`，不经过 main，也不传文件内容（5.7 A） |
| `plugin-dialog` 的 `open` | `dialog.openDirectory(options)` | `dialog.showOpenDialog`，只用于选目录（工作区、附件文件夹、Skill）；选文件改用 `<input type="file">`（5.7 A） |
| `getCurrentWindow` | 删除 | Windows 改用系统原生的窗口按钮（5.4） |
| `isTauri` / `__TAURI__` | `window.cosirHost !== undefined` | 由 `lib/platform.ts` 统一判断 |

用户授权边界：渲染进程只能对自己手里的 `File` 对象取路径，而 `File` 只能来自用户的选择、粘贴或拖放，所以
`files.pathOf` 无法用来探测任意路径；目录路径只来自原生对话框。渲染进程拿到路径后**只把它作为附件描述交给后端**，
不自己读取工作区文件（AGENTS.md 的前端边界不变）。

渲染进程侧，`lib/desktop-host/` 是唯一出口，`runtime-config.ts`、`frontend-log.ts`、附件、工作区和 Skill 的选择器
都改为从这里引用。浏览器开发模式（不在 Electron 里运行）下没有 `cosirHost`，沿用现在的约定：`VITE_BACKEND_URL`，默认 `http://127.0.0.1:8000`。

### 5.3 后端进程生命周期

`supervisor.ts` 一比一移植 Rust 的状态机，现有 Rust 单测覆盖的规则改写成 Vitest 用例：

- 状态 `starting` / `ready` / `failed` / `stopped`，以及 generation 比较；
- 90 秒就绪超时，崩溃恢复最多 1 次，恢复次数用完后等用户手动重启；
- 健康检查失败时的重试只推进一次 generation，不会重新打开已经关闭的生命周期。

`runtime.ts` 和 `process.ts` 的规则不变：

- uv 只负责解析解释器和同步环境，后端始终是宿主的**直接子进程**；
- 环境变量（`CODING_AGENT_*`、`VIRTUAL_ENV`、`PATH`、`UV_CACHE_DIR`）的注入规则不变；
- 后端的 stdout 和 stderr 写入 `backend-console` 日志。

**宿主存活契约（替代 Job Object）**：

- 宿主以 `stdio: ["pipe", "pipe", "pipe"]` 拉起后端，stdin 一直不写入，也不关闭。
- 宿主传入环境变量 `CODING_AGENT_PARENT_PIPE=stdin`；后端在 lifespan 启动时开一个守护线程阻塞读 stdin，读到 EOF
  就记一条 `parent_host_lost` 日志，然后按正常关闭流程退出（复用 lifespan 的关闭逻辑，而不是直接 `os._exit`）。
- 宿主无论以什么方式消失（正常退出、崩溃、被强杀），操作系统都会关闭管道，后端都能收到信号。所以这一条能同时
  覆盖 Windows 和 macOS；而现在 macOS 并没有这层兜底。
- 绕过宿主直接跑后端（开发调试）时不设这个变量，也就不启用检测。

**正常停止**：

- POSIX：spawn 时 `detached: true` 让后端成为进程组长，停止时先给进程组发 SIGTERM，等 3 秒后发 SIGKILL。
- P0 实测：只要前端连着 `/task-status/stream` 这类长连接，uvicorn 收到 SIGTERM 后会卡在等待流式响应结束，
  5 秒内退不出，最终只能 SIGKILL。现在的 Rust 宿主在 macOS 上本来就是直接 SIGKILL，所以 lifespan 的关闭清理
  目前并不执行，这是现状就有的问题。P1 保持「SIGTERM + 短超时 + SIGKILL」，不阻塞退出；让长连接在关闭时
  主动结束、从而使优雅关闭真正生效，作为后端的独立跟进项（可以设置 uvicorn 的 `timeout_graceful_shutdown`，
  或让流式端点在 lifespan 关闭时收尾）。
- Windows：`taskkill /PID <pid> /T`，超时后加 `/F`（和现在一致）。
- 工具子进程和终端 worker 仍由后端自己的关闭流程负责清理，宿主只兜底。

### 5.4 窗口、托盘与退出

| 行为 | Electron 实现 |
|---|---|
| 窗口先显示，后端在后台启动 | 先 `BrowserWindow.show()`，再启动 supervisor；前端通过启动门禁显示后端状态（和现在一样） |
| 窗口外框 | macOS：`titleBarStyle: "hiddenInset"`，`trafficLightPosition: { x: 20, y: 22 }`；Windows：`titleBarStyle: "hidden"` 加 `titleBarOverlay`（系统原生按钮，颜色跟随主题）。**删除 `WindowControls` 组件和 `tauri.windows.conf.json`** |
| 拖动区 | `data-tauri-drag-region` 改为 `.app-drag-region`（`-webkit-app-region: drag`），里面的可点击元素加 `no-drag`。左右安全区变量（`globals.css`）仍然只由 `lib/platform.ts` 计算 |
| 关闭窗口隐藏到托盘 | `close` 事件里 `preventDefault()` 后 `hide()`；但处于真正退出流程时放行 |
| 托盘 | `Tray` 加菜单「显示 Cosir」「退出并停止运行」 |
| 单实例 | `app.requestSingleInstanceLock()`，第二个实例触发 `second-instance` 时恢复主窗口 |
| macOS Dock 点击 | `app.on("activate")` 恢复主窗口 |
| 退出确认 | `before-quit` 里先 `preventDefault()`，查询 `/terminal/sessions/active-count`（短超时），有运行中终端就弹 `dialog.showMessageBox`，确认后打标记再 `app.quit()`。Cmd+Q、托盘退出和菜单退出都会触发 `before-quit`，**不再需要 `app_menu.rs` 那种变通写法**。查询失败、超时或后端未就绪时直接放行 |
| 最终清理 | `will-quit` 里同步停止 supervisor |

### 5.5 安全基线

- 主窗口：`contextIsolation: true`、`sandbox: true`、`nodeIntegration: false`，只加载自己的 preload。
- 导航限制：`will-navigate` 只允许应用自身的地址（开发时是 Vite 地址，打包后是 `file://` 或自定义协议）；
  `setWindowOpenHandler` 一律拒绝新窗口，外链交给 `shell.openExternal`。
- 权限：`session.setPermissionRequestHandler` 默认拒绝。
- 打包版加载前端用自定义协议 `app://`（`protocol.handle`），不直接用 `file://`，以免相对路径和 CSP 出问题。
  页面来源因此是 `app://cosir`，后端 CORS 白名单必须加上它（P0 中前端请求后端被 CORS 拦下，才发现这一点）；
  开发模式仍从 Vite（`http://127.0.0.1:3000`）加载，已经在白名单里。
- 每个 IPC 入口都校验入参（`ipc.ts` 是信任边界），main 侧其他模块直接信任 `ipc.ts` 的结果，不重复校验。

### 5.6 日志与诊断

- `desktop-YYYY-MM-DD.log`、`backend-console-*.log`、`frontend-*.log` 的路径、JSONL 字段、按大小分片的规则都不变，
  `desktop-log.ts` 照搬 Rust 的轮转逻辑。
- 新增两个平台都生效的诊断事件：`render-process-gone`、`child-process-gone`、`unresponsive`。它们取代 Windows
  专用的 `webview_diagnostics.rs`。
- 日志写失败不影响宿主运行，这一点和现在一样。

### 5.7 借迁移完成的结构化改造（P1 一并完成）

这两项都是「换宿主时顺手做比以后单独做便宜」的结构化改造：宿主这一层反正要整体重写，相关调用方也都要改一遍。

**A. 附件三个入口合成一条链路（收益最大）**

现状（代码事实）：WebView 里的 `File` 对象拿不到本机路径，附件因此分成三套：

- 附件按钮：Tauri 原生对话框拿路径，经 `resolve_selected_attachment_path` 规范化；图片再经
  `read_selected_attachment_file` 把字节以 `number[]` JSON 数组传回前端（`attachment-picker.tsx`），一张 5MB 的
  图片会膨胀成几十 MB 的 JSON。
- 粘贴、拖放：拿不到路径，普通文件只能拒绝，提示「请使用附件按钮选择文件」
  （`composer-external-files.ts` 的 `local-path-required`）。按 Tauri 文档，Windows 上默认还会拦截 HTML5 拖放，
  所以从资源管理器拖文件进来在真实应用里基本不可用（根据代码和文档推断，未在 Tauri 应用里实测）。
- 路径对应关系靠登记：`local-attachment-registry.ts` 用 `WeakMap<File, metadata>` 把选择器拿到的路径挂到
  `File` 上，后续环节才能查到。

改造后：

- 三个入口都只产出 `File`：附件按钮用 `<input type="file" multiple>`（Electron 中打开的就是系统原生对话框，
  返回可读的 `File`），粘贴和拖放本来就是 `File`；需要路径时统一调用 `files.pathOf(file)`。
- 从 Finder / 资源管理器拖进来的普通文件、文件夹、图片都能接纳。
- 删除：两个文件读取 IPC、`registerLocalAttachment` / `getLocalAttachment` 那层 `WeakMap` 登记、
  `local-path-required` 拒绝分支、附件选择器里的逐个路径解析循环。按 id 查元数据的表保留（草稿里的 token 靠它）。
- 取舍（已确认）：`<input type="file">` 不能指定初始目录，选文件时不再默认打开工作区根目录。换来的是三个入口一条链路、
  不再经 IPC 传字节，判断值得；选目录仍用 `dialog.openDirectory`，保留初始目录。

**B. 后端状态从轮询改成推送**

现状：`src/runtime-config.ts`（432 行）启动时每 250ms、之后每 2 秒轮询一次宿主，再用操作版本号（epoch）
防止过期结果覆盖新结果。

改造后：main 进程在状态或 generation 变化时，经 `backend.onStatusChanged` 推送一份完整快照
（`{ backendBaseUrl, generation, status }`）；渲染进程启动时 `runtimeConfig()` 取一次初值，之后只订阅。
删除轮询循环与大部分 epoch 竞态处理；generation 比较保留，这是防止旧进程状态覆盖的唯一判据。
说明：Tauri 也支持事件推送，这一项并不依赖 Electron，只是宿主整体重写时一并完成成本最低。

### 5.8 只为 WKWebView 存在的代码

| 位置 | 为什么存在 | 处理 |
|---|---|---|
| `playwright.config.ts` 的 `webkit` 项目（17 个 spec 再跑一遍） | 模拟 macOS 的 WKWebView | **P1 删除**。E2E 只跑 Chromium，与真实运行内核一致 |
| `lib/keyboard/ime.ts` 的三重判断，以及调用方维护的组字标记（如 `composer-editor.tsx` 的 `isComposingRef`） | WKWebView 上屏时会先触发 `compositionend`，再送出 `isComposing=false` 的回车 | P1 之后实测 Chromium 中文输入法，确认后只保留 `isComposing` 与 keyCode 229，删调用方的组字标记 |
| `app/markdown.css` 的 Mermaid 全屏尺寸修正、`globals.css` 不用 `mask-image` 的约束 | WebKit 绘制与滚动性能问题 | P1 之后在 Chromium 下实测，不再需要就删除并更新注释 |
| `lib/assistant/message-height-estimate.ts` | 注释以 WKWebView 惯性滚动为例 | **保留**：估算准确对任何内核的虚拟列表都有益，只更新注释 |

原则：只有实测确认不再需要才删除，不凭推测删兼容代码。

### 5.9 迁移后可以加强的能力（P1 之后，各自单独评估）

| 能力 | 现状 | Electron 做法 |
|---|---|---|
| 外链统一用系统浏览器打开 | Markdown 的 `target="_blank"`、终端的 `window.open` 没有统一入口；项目未装 Tauri opener 插件，推测点击多半无反应（未实测） | `setWindowOpenHandler` + `shell.openExternal`，一处接管（这条随 5.5 的导航限制在 P1 就会生效） |
| 系统通知 | 只有应用内 toast；窗口隐藏到托盘后，Run 完成或等待审批时用户不知道 | `Notification`，在 Run 落定、等待用户决定时提醒；属于产品新功能，单独设计 |
| 渲染进程崩溃自动恢复 | 只有 Windows 有 WebView2 诊断，没有恢复 | `render-process-gone` 后有限次 `reload()`，两个平台一致 |
| 真实桌面 E2E | WDIO 一套、Playwright 一套 | Playwright `_electron.launch`，与现有 E2E 共用 fixture |
| 内置浏览器 | 无 | 第 6 节，P3 |

### 5.10 换宿主后要坚持的边界

- **不把业务逻辑放进 main 进程**：main 只管进程、窗口、文件授权、浏览器视图。
- **能拿到本机路径不等于前端可以读文件**：路径只作为附件描述交给后端，由后端读取。
- **安全要自己守**：Tauri 默认能力最小、按需开放；Electron 的 main 进程拥有完整 Node 能力，preload 每多暴露一个
  方法都是一次授权。新增宿主能力必须同时更新 `shared/host-contract.ts` 与 `ipc.ts` 的入参校验。

## 6. 内置浏览器对宿主的约束（P3 详细设计另文）

本节只确定 P1 必须预留的结构：

1. **BrowserHost 放在 main 进程**，每个标签页是一个 `WebContentsView`，挂在主窗口上，由渲染进程报告占位区域，
   main 进程调用 `setBounds` 对齐。
2. **会话隔离**：每个工作区一个持久化分区（`persist:browser-<workspaceId>`），和主窗口的分区完全分开；浏览器视图
   不加载 preload，不开放 Node 能力。
3. **Agent 怎么调用**：后端的 `browser_*` 工具经本机管道发 JSON-RPC。管道路径由宿主在 spawn 时通过环境变量
   `COSIR_HOST_PIPE` 传入（POSIX 用 Unix socket，Windows 用命名管道），不监听 TCP 端口。选择原生工具而不是 MCP，
   是因为 Task 归属、取消、超时、`display_data.kind` 和 HITL 都是后端原生工具已有的能力。
4. **操控与观察**：用 `webContents.debugger` 发 CDP 命令，方法有白名单；给模型的观察以
   `Accessibility.getFullAXTree` 生成的带 uid 快照为主，截图为辅。
5. **生命周期**：标签页属于进程内协调状态，不跨重启恢复；删除 Task 时后端通过管道通知宿主关闭对应标签页；
   宿主重启后所有标签页消失，后端按「旧终端失效」同样的方式处理。
6. **弹层遮挡**：`WebContentsView` 盖在 DOM 上方，React 的弹层画不到它上面。方案是弹层打开时先隐藏视图，用最后一帧
   截图占位。P0 实测：截图 10～15ms，切换到占位 25～28ms，恢复 1ms，肉眼察觉不到；`capturePage` 偶发
   `UnknownVizError`（6 次自测、33 次截图里出现 1 次），失败时退回空白占位，不能阻塞弹层打开。

P1 在 `host-contract.ts` 和 supervisor 的环境变量注入处为 3 留好接入点，但不实现管道服务端。

## 7. 打包与发布

- 采用 **electron-builder**：
  - `extraResources` 打入 `target/resources/backend`（PyInstaller 产物）和 `target/resources/terminal-worker`，
    路径通过 `process.resourcesPath` 解析，替代 Tauri 的 `app-resources/`；
  - macOS 出 `dmg` 和 `zip`，启用 hardened runtime 并配置 entitlements（Electron 需要的 JIT 相关权限），之后公证；
  - Windows 出 `nsis` 安装包和 `portable` 便携版，**删除 `stage-portable.mjs`**。
- `build:bundle` 的前置步骤（前端、terminal-worker、PyInstaller）保留，最后一步从 `tauri build` 换成
  `electron-builder`。
- CI：`terminal-worker-bundle.yml` 里的 `cargo install tauri-cli` 和 `npm run tauri:build` 换成 electron-builder；
  Rust 工具链继续保留，因为终端 worker 需要它。
- 体积预期：安装包大约增加 100MB（Chromium），运行时内存高于系统 WebView。这是换来统一内核和 CDP 能力的已知代价。

## 8. 测试策略

| 层级 | 内容 |
|---|---|
| main 进程单测（Vitest，node 环境） | 移植 Rust 单测：崩溃恢复次数有限、generation 比较、健康检查重试只推进一次、解释器解析规则、环境变量注入、日志分片 |
| 后端单测 | stdin 读到 EOF 后走正常关闭流程；不设 `CODING_AGENT_PARENT_PIPE` 时不启用检测 |
| 渲染进程 | 现有 Vitest 与 Playwright（Vite + 内存测试服务）保留，**删除 `webkit` 项目**；`lib/desktop-host/` 补缺少 `cosirHost` 时的 fallback 用例；附件改造补「粘贴 / 拖放普通文件被接纳」用例，删除 `local-path-required` 相关断言；状态推送补订阅与 generation 过期的用例 |
| 桌面 E2E | 用 Playwright 的 `_electron.launch` 改写 `desktop-smoke`：启动后进入 ready、关闭窗口隐藏到托盘、单实例、退出时清理后端进程。**删除 WDIO、`wdio.tauri.conf.mjs`、`tauri-plugin-wdio` 和 `desktop-e2e` feature** |
| 手工验收 | macOS 和 Windows 各跑一遍：托盘、Cmd+Q 和托盘退出的确认、强杀宿主后后端在数秒内退出、打包版能找到后端和 terminal-worker |

## 9. 变更范围

**新增**：`apps/desktop/electron/`（main、preload、shared）、`apps/desktop/lib/desktop-host/`、electron-builder
配置、main 与 preload 的构建和开发脚本、`tests/electron-e2e/`。

**修改**：

- 前端：3.3 节列出的 8 个文件、6 个拖动区文件、`globals.css`（拖动区样式）；附件链路
  （`composer-external-files.ts`、`assistant-composer-attachments.ts`、`local-attachment-registry.ts`、
  `new-conversation.tsx`）；`src/runtime-config.ts` 改为推送订阅；
- 后端：lifespan 加 stdin 存活检测；`app.py` 的 CORS 白名单加 `app://cosir`、删 Tauri 来源；
- 构建：`package.json`（依赖和脚本）、`scripts/stage-*.mjs`（资源路径）、`playwright.config.ts`、CI 工作流；
- 文档：AGENTS.md 的相关章节、`apps/desktop/AGENTS.md`、README。

**删除**：`apps/desktop/src-tauri/` 整个目录、`@tauri-apps/*` 依赖、`components/layout/window-controls.tsx`、
`scripts/run-tauri-*.mjs`、`scripts/stage-portable.mjs`、`wdio.tauri.conf.mjs`、`tests/tauri-e2e/`、Playwright 的
`webkit` 项目、附件的 `WeakMap` 路径登记与 `local-path-required` 分支、`runtime-config.ts` 的轮询循环；根 Cargo
workspace 里的 desktop 成员也移除，只保留 terminal-worker。

**明确不改**：后端业务代码、Assistant Transport、React 业务组件、终端 worker、`~/.cosir` 下的目录结构和日志格式。

## 10. 实施阶段

| 阶段 | 内容 | 完成标准 |
|---|---|---|
| **P0 技术验证**（独立分支，可以随时放弃） | 最小 Electron 壳：加载 Vite 前端、开发模式拉起后端、`window.cosirHost` 跑通 `runtimeConfig`；嵌入一个 `WebContentsView`，用 CDP 跑通「打开网页、无障碍树快照、按 uid 点击」；Python 经 Unix socket 或命名管道调用一次；验证弹层遮挡方案；验证 stdin EOF 检测 | 四个验证点都有可行结论；任何一个不可行就回到本文第 1 节重新评估 |
| **P1 宿主替换** | 第 5 节全部内容（含 5.7 的两项结构化改造与删除 `webkit` 项目）；前端改走 `lib/desktop-host/`；后端存活契约；Electron 版桌面 E2E；删除 Tauri；重写 AGENTS.md 相关章节 | 现有单测、E2E 和新的桌面冒烟测试全部通过；第 8 节手工验收在 macOS 上通过 |
| **P2 打包与发布** | electron-builder、签名与公证、Windows 安装包和便携版、CI | 两个平台都能出可安装的包，打包版通过第 8 节手工验收 |
| **P3 浏览器能力** | 另写详细设计：BrowserHost、管道服务端、后端 `browser_*` 工具、工作台浏览器面板、展示数据契约、安全策略 | Agent 能在内置浏览器里完成一个真实任务，用户可以直接接管 |

P1 和 P2 可以合并成一个 PR 落地，也可以分开；P1 合入后主干上就只有 Electron 宿主。5.8 的实测清理与 5.9 的加强项
在 P1 之后按需逐项进行，不阻塞 P2、P3。

## 11. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| stdin EOF 检测在某些启动方式下不可靠（比如被其他父进程包了一层） | 宿主崩溃后后端泄漏 | 宿主始终直接 spawn 后端（保持现有约束）；P0 在两个平台上分别验证强杀宿主的情况 |
| 弹层遮挡浏览器视图 | P3 体验变差 | P0 验证截图占位方案；不行就改用 `<webview>`，并接受它的稳定性风险 |
| 签名和公证流程变化 | P2 延期 | P2 独立成阶段，不阻塞 P1 合入 |
| 安装包体积和内存上涨 | 资源占用 | 已知代价；P2 记录实际数据，作为后续优化的基线 |
| preload 暴露面扩大导致渲染进程越权 | 渲染进程借 IPC 读写任意文件 | 坚持 5.5 与 5.10：沙箱、白名单方法、入参只在 `ipc.ts` 校验；附件不提供「按路径读文件」接口 |
| 选文件不再默认打开工作区目录 | 体验小幅变化 | 已知取舍（5.7 A）；如反馈强烈，再给 `<input>` 前加一个「从工作区选择」的入口 |
| 迁移期间宿主行为回归 | 托盘、退出、恢复等细节出错 | 先把 Rust 单测移植成 Vitest 再改实现；第 8 节手工验收清单逐项核对 |

回滚：P1 合入前都在分支上进行，可以直接放弃；合入后按绿地项目原则不保留 Tauri，回滚就是 revert 对应提交。

## 12. P0 验证结果（2026-10-30，macOS，Electron 44.7.2）

验证代码是用完即弃的，放在会话临时目录里，没有进入仓库。后端用的是仓库当前代码的副本（只在副本里给 CORS
白名单加了 `app://cosir`），数据目录用临时目录，没有碰 `~/.cosir`。连续跑了 3 次自测，结果一致。

| 验证点 | 结果 | 数据 |
|---|---|---|
| V1 Electron 拉起后端、用 `app://` 加载真实前端、`window.cosirHost` | 通过 | 窗口 0.4～0.8s 显示，后端 1.0～1.3s 就绪；`cosirHost.backend.runtimeConfig()` 返回正确配置；页面里没有 `require` / `process`（隔离生效）。**发现**：CORS 白名单缺 `app://cosir`（见 5.5）；没有启动门禁时，前端首个请求会早于后端就绪，P1 的 `cosirHost` 门禁负责解决 |
| V2 `WebContentsView` + CDP | 通过 | 无障碍树快照 11～13ms，按 uid 点击 4ms（含滚动到可见）；`Input.insertText` 输入中文正常；外网页面（example.com）可以打开并生成快照 |
| V3 Python 经 Unix socket 调用宿主 | 通过 | 打开网页、快照、点击、取值四次调用总计约 17ms（单次 0.3～9ms），整个 Python 进程往返 30～41ms |
| V4 弹层遮挡的截图占位方案 | 通过，需要兜底 | 见第 6 节第 6 点 |
| V5 宿主被 SIGKILL 后子进程靠 stdin EOF 退出 | 通过 | 子进程 119ms 内退出，没有残留进程 |
| 附带：停止后端 | 发现现状问题 | 长连接让 SIGTERM 优雅关闭超过 5 秒，见 5.3 |

**没有覆盖的部分**：Windows（命名管道、`taskkill`、`titleBarOverlay`）没有验证，放到 P1 一开始在 Windows 上补测；
打包后的签名和公证留给 P2。

**结论**：四个关键假设都成立，没有触发「回到第 1 节重新评估」的条件，可以进入 P1。

## 13. 待决问题

1. 打包工具：本文推荐 electron-builder，另一个选项是 Electron Forge。
2. 浏览器分区粒度：按工作区还是按 Task。本文倾向按工作区，这样同一工作区里的登录状态能共用，留到 P3 定稿。

## 附录 A：Claude 与 Codex 的实现依据

以下内容来自本机安装包（`/Applications/Claude.app`、`/Applications/ChatGPT.app`）的只读检查，以及公开资料：

- **Claude 桌面应用**：安装包带 `Electron Framework.framework`；代码中用 `WebContentsView` 嵌入页面，用
  `session.fromPartition(...)` 隔离存储；`CDPTools` 类经 `webContents.debugger.sendCommand` 发
  `Input.dispatchMouseEvent`、`Input.dispatchKeyEvent`、`Page.captureScreenshot`，用
  `Accessibility.getFullAXTree` 生成带 uid 的快照，允许调用的 CDP 方法有白名单；浏览器以 MCP 工具的形式提供给
  Agent 进程。
- **Codex（ChatGPT 桌面应用）**：打包信息写着 `runtimeName: "owl"`，内核目录是
  `Codex Framework.framework/Versions/156.0.8078.48`（Chromium 版本号）；代码中同样使用 `WebContentsView` 和
  `webContents.debugger.attach("1.3")`，有 `browserPane`、`inAppBrowserUse`、`externalBrowserUse` 等功能开关，
  以及权限、导航限制、标签页存活时长等配置，标签页按对话管理；Agent 进程通过环境变量
  `CODEX_APP_TOOLS_PIPE_PATH` 拿到本机管道调用宿主工具。
- **Atlas**：OpenAI 工程博客介绍，Atlas 把 Chromium 放在独立进程中运行，画面通过图层共享显示到 Swift 应用的
  窗口里，输入由应用转发（[Building ChatGPT Atlas](https://openai.com/index/building-chatgpt-atlas)）。
- **Tauri CEF 运行时**：社区维护的 `tauri-runtime-cef` 仍是 alpha 版本，依赖 Tauri 3 alpha，从未发布的 `feat/cef`
  分支抽出（[docs.rs](https://docs.rs/crate/tauri-runtime-cef/3.0.0-alpha.5)、
  [GitHub](https://github.com/SableClient/tauri-runtime-cef)）。

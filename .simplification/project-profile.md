# 项目画像

> 首轮建立于 2026-10-31（标准 1.0）。事实主要来自根目录 `AGENTS.md`，其余来自代码与配置。
> 检测器不在这里登记，见 `scripts/README.md`。

## 画像

| 维度 | 结论 | 来源 |
|---|---|---|
| 产品形态 | 单用户、本机运行的桌面 Agent 应用（Electron 宿主 + React 渲染进程 + Python/FastAPI 后端子进程）。不是库，没有对外发布的公开 API | AGENTS.md「产品形态与运行拓扑」 |
| 运行拓扑 | 单机多进程：Electron main（窗口、托盘、宿主 IPC、后端生命周期）、渲染进程（React，经 preload 的 `window.cosirHost` 访问宿主）、后端子进程（localhost HTTP/SSE/WebSocket）、按需工具子进程。不涉及多租户、鉴权、分布式 | AGENTS.md |
| 跨进程契约 | 渲染进程 ↔ 宿主：`electron/shared/host-contract.ts`（入参只在 `electron/main/ipc.ts` 校验）；渲染进程 ↔ 后端：HTTP 领域 API、Assistant Transport snapshot、`/task-status/stream`、终端 WebSocket；后端 → 宿主：宿主工具链路（`electron/main/host-link/`）；工具展示契约 `display_data.kind` ↔ 前端 renderer 注册表。这些两端都在本仓，删改必须两端同查 | AGENTS.md「进程间接口边界」「工具 UI 渲染边界」 |
| 信任边界 | 进程外输入：用户输入、后端 HTTP/SSE 响应（渲染进程视角）、宿主 IPC 入参（main 视角，只在 `ipc.ts` 校验）、文件系统、网页内容（内置浏览器）、后端子进程输出。内部模块之间不是信任边界 | AGENTS.md；代码 |
| 生命周期阶段 | 绿地项目：不需要代码兼容和运行兼容，数据库、日志、配置可破坏性变更；兼容分支与旧路径可以直接删除 | AGENTS.md 顶部「重要提示」 |
| 外部观察者 | 无外部团队或第三方消费者。可观察面是：用户界面行为、结构化日志（`frontendLog` 落盘到 `.cosir/logs/`，属于受保护的可观测性）、跨进程契约的对端 | AGENTS.md「日志打印与排查」 |
| 质量要求 | 可排查日志为铁律；进程生命周期（后端启停、退出清理、崩溃恢复有限且串行）为关键失败安全路径；渲染进程开启 contextIsolation 与 sandbox；不为低概率事件叠多层防御 | AGENTS.md |
| 规模与协作 | 前端约 400 个生产 TS/TSX 文件；提交历史中 2 位作者（以 tianjiashu 为主），按单人主导开发看待，合并与抽象的协作成本低 | git log；代码统计 |
| 项目规范 | 根 `AGENTS.md`（第零铁律、五条铁律、禁止 `getattr` 式能力探测、抽象按需引入、不层层防御校验、日志规范、docstring 约定）；`rules/Agent代码开发规范.md`；前端 ESLint 自定义规则（禁止原始色板、任意字号、直接用 `error.message` 作界面文案） | 文件 |

## 工具链（`apps/desktop`）

在 `apps/desktop` 目录下执行：

| 用途 | 命令 | 最近验证状态（2026-11-01 第二轮） |
|---|---|---|
| 类型检查（渲染进程） | `npx tsc --noEmit -p .` | 通过 |
| 类型检查（宿主） | `npx tsc --noEmit -p electron` | 通过 |
| Lint | `npm run lint` | 通过：已为 Node `.mjs` 配置 `globals.node`，保留 `no-undef` 规则 |
| 单元测试 | `npm run test:unit` | 通过：109 个文件、692 个用例 |
| 端到端测试 | `npm run test:e2e`；桌面 E2E `npm run test:e2e:desktop`（需构建） | 前端 145 通过、2 失败、9 未运行；真实 Electron 浏览器引擎 15 通过。完整桌面冒烟被基线后端 `DeferredEntry` 导入错误阻断；详见第二轮报告 |
| 构建 | `npm run build` | 通过；存在既有 CSS `::highlight` 识别与 bundle 大小提示 |

后端（`apps/backend`）的工具链待后端纳入精简范围时补充。

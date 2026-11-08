# 路径语义审计：apps/desktop

> 状态：批 1–4 已提交；批 5（D3）已执行未提交：`lib/assistant/` 拆为 `transport/`、`composer/`、`tools/`、`turn/` 四个子目录（33 个文件），`contract`、`transport-message-converter`、`task-commands`、`run-launch`、`command-request-store`、`assistant-performance-probe`、`attachments/` 留在根。`composer-action-policy` 落为 `composer/action-policy.ts`。后端 `test_tool_registry_display_guard.py` 读取前端 `tools/tool-catalog.ts` 的路径已同步。批 4 实际落点：`task-rail` → `conversation-aside`（含 `lib/`、标识符 `TaskRail*` → `ConversationAside*`、`RailCard*` → `AsideCard*`、`data-task-rail` 等属性与日志事件名；界面文案「会话侧栏」未改）；`assistant-ui/elements` → `conversation/thread`，`assistant-ui/{tools,turn}` → `conversation/{tools,turn}`，`assistant` → `conversation-runtime`。`conversation-runtime/` 内 `assistant-*` 文件名与 `runtime/` 子目录保留，是遗留的命名不一致，未纳入本批。下文清单保留审计时的原始路径，对照时以第 6 节批次表为准。
> 执行偏差：A2 中的 `electron/main/browser/input/session.ts` 在审计后已被他人提交删除（`PageSession` 并入 `operation.ts`），该项取消；其余项均按清单执行。`tests/unit` 中随被测模块同步改名的有 `transport-message-converter`、`composer-action-policy`、`http-error`、`markdown-editor-keymap`、`terminal-output-viewport` 五个测试文件。
> 审计过程本身未改任何源码、配置或 AGENTS.md。
> 范围：用户限定为 `apps/desktop`。含 `components/`（298 个文件）、`lib/`（146）、`electron/`（73）、`hooks/`（4）、`app/`（2）、`src/`（3），共 526 个源文件；`tests/`、`scripts/` 只用来统计引用，不单独审。后端与 `apps/terminal-worker` 不在本次范围。
> 标准：只看完整路径就能说出文件做什么、属于哪层，也能猜到新功能该放哪。

## 0. 结论

1. **desktop 里没有拼写错误。** 文件名、目录名以及标识符中查不到 `backgraound`、`avaliable` 一类错误。已知的两处拼写错误都在后端，不在本次范围。
2. **没有 `utils/`、`helpers/`、`common/` 这类整目录泛名。** 泛名只剩零星文件：`lib/utils.ts`（shadcn 约定，列为例外）、`display-utils.ts`、`types.ts` 系列、`session.ts`、`store.ts`、`methods.ts`。
3. **最该改的是同名异义和职责重叠，共四类：**
   - 5 个 `store.ts`、3 个 `session.ts`、2 个 `model-selector.tsx`：完整路径之外，文件名本身不指向任何事物。
   - `tools/details/` 与 `tools/views/` 是两个职责相同的目录，而且 `details/detail-view.tsx` 不是视图。
   - `components/` 根目录躺着 6 个页面或域文件，各有对应的域目录却没放进去。
   - `lib/assistant/` 有 45 个平铺文件，混着传输、输入框、工具、回合展示四个主题。
4. **AGENTS.md 在 desktop 范围内有一处漂移：** 写 `app/（根组件与全局样式）`，实际 `app/` 只有两个 CSS，根组件在 `src/App.tsx`（见第 5 节）。其余 desktop 路径逐条核对都存在。你给的另外三处漂移（`mult_agent/` 等）都在后端，不在本次范围。

引用数的口径：**引用文件数 = 静态 import 该文件的文件数，含 `tests/`**，由脚本解析 `@/` 别名与相对路径、`import()`、`vi.mock` 得到。字符串路径引用（`knip.jsonc`、`scripts/*.mjs`、`components.json`、AGENTS.md、`docs/plan/*`）不在内，改名时要另行 `grep`，各条已标出已知的几处。`tests/unit` 有 133 个文件，多数以被测模块命名（如 `converter.test.ts`），改名时测试文件同步改名。

## 1. 例外（语言、框架、工具约定，不改）

| 路径 | 约定来源 |
|---|---|
| `electron/preload/index.ts`、`lib/desktop-host/index.ts` | 入口 / 桶文件 `index.ts` |
| `electron/main/app.ts` | 主进程构建入口，`build-electron.mjs` 与 `knip.jsonc` 按路径引用；输出为 `main.cjs`。可选改 `main.ts`（P3-1），不属于约定例外之外的必改项 |
| `src/main.tsx`、`src/App.tsx`、`src/router.tsx` | Vite / React 入口 |
| `components/ui/*`、`lib/utils.ts`（93 个引用） | shadcn 在 `components.json` 里固定了 `@/components/ui` 与 `@/lib/utils` 两个别名 |
| `components/assistant-ui/elements/*.aui.tsx` | assistant-ui 注册表的 `.aui.tsx` 命名约定（运行时连接版） |
| `lib/assistant/assistant-external-state.d.ts`、`lib/workspace-files/pdf.d.ts` | 环境类型声明 `.d.ts` |
| `*.test.ts`、`*.spec.ts`、`*.json` 契约样例 | 测试与数据文件 |
| `lib/terminal/terminal-*.ts`、`components/terminal/terminal-*.tsx`、`components/browser/browser-*.tsx`、`components/composer/composer-*.tsx` 的「目录名 + 前缀」 | 不改，理由见第 6 节 |

## 2. 清单

优先级：**P1** = 路径给出错误或矛盾的语义，或同一职责散在两处；**P2** = 泛名或同名异义，改了立刻变好找；**P3** = 可选。「影响」列是引用文件数，括号内是其中测试文件数。

### 2.1 同名异义（全仓重名）

| # | 当前路径 | 问题 | 建议路径 | 影响 | 优先级 |
|---|---|---|---|---|---|
| A1 | `lib/background-tasks/store.ts` | 5 个文件都叫 `store.ts`，文件名不说是什么的 store；而同仓已有 9 个 `*-store.ts`（`dock-store`、`panel-store`、`host-state-store`、`confirm-store`、`task-status-store`、`command-request-store`、`disclosure-store`、`transport-frame-store`、`terminal-workbench-store`），这 5 个是漏网的 | `lib/background-tasks/background-tasks-store.ts` | 10 (2) | P1 |
| | `lib/handoff/store.ts` | 同上 | `lib/handoff/handoff-store.ts` | 8 (2) | P1 |
| | `lib/task-rail/store.ts` | 同上；若采纳 D2 则随目录改 | `lib/task-rail/task-rail-store.ts` | 9 (2) | P1 |
| | `lib/workbench/store.ts` | 同上 | `lib/workbench/workbench-store.ts` | 6 (1) | P1 |
| | `lib/workspace-files/store.ts` | 同上 | `lib/workspace-files/workspace-files-store.ts` | 14 (2) | P1 |
| A2 | `lib/agent-editor/session.ts` | 3 个 `session.ts`，含义各不相同（编辑器会话、编辑器会话、`BrowserTab` 交给动作模块的页面能力） | `lib/agent-editor/agent-editor-session.ts` | 6 (1) | P2 |
| | `lib/agent-team-editor/session.ts` | 同上 | `lib/agent-team-editor/agent-team-editor-session.ts` | 10 (1) | P2 |
| | `electron/main/browser/input/session.ts` | 实际定义 `PageSession` 类型，又与 `cdp-session.ts`、`browser-session.ts` 并存，三个「session」指三件事 | `electron/main/browser/input/page-session.ts` | 10 (1) | P2 |
| A3 | `components/assistant-ui/model-selector.tsx` | 与 `components/model/model-selector.tsx` 同名。前者是接 Run 配置锁定与偏好存储的输入框选择器（只被 `composer-controls.tsx` 用），后者是带 `ModelSelector.Root/Content/…` 的通用选择器组件 | `components/composer/composer-model-selector.tsx`（`components/model/model-selector.tsx` 保留） | 1 | P1 |
| | `components/assistant-ui/model-config-panel.tsx` | 实际是承载 `ModelConfigManager` 的对话框，却放在 assistant-ui 根目录，名为 panel | `components/model/model-config-dialog.tsx` | 1 | P2 |
| A4 | `lib/browser/tab-label.ts` | 2 个 `tab-label.ts`，一个是浏览器标签，一个是文件阅读标签 | `lib/browser/browser-tab-label.ts` | 4 (1) | P3 |
| | `lib/workspace-files/tab-label.ts` | 同上 | `lib/workspace-files/file-tab-label.ts` | 2 (1) | P3 |
| A5 | `components/markdown-editor/shortcuts.ts` | 与 `lib/keyboard/shortcuts.ts`（全局快捷键，10 个引用）同名；内容是 Markdown 编辑器的格式化键位与回车续写 | `components/markdown-editor/markdown-editor-keymap.ts` | 2 (1) | P3 |
| A6 | `electron/main/host-link/methods.ts` | 与 `browser/methods.ts` 同名；前者是方法注册表与 `HostMethodError`，后者是注册浏览器方法 | `electron/main/host-link/method-registry.ts`；`electron/main/browser/browser-methods.ts` | 15 (4)；3 (2) | P2 |
| A7 | `electron/main/host-link/client.ts` | 与 `lib/http/client.ts` 同名（跨进程，不会同处一个包，但搜索时混） | `electron/main/host-link/host-link-client.ts` | 2 (1) | P3 |
| A8 | `lib/agent-team-editor/outline.ts` | 与 `electron/main/browser/snapshot/outline.ts` 同名；内容是 Team 图的大纲排序 | `lib/agent-team-editor/team-outline.ts` | 2 (1) | P3 |
| A9 | `lib/http/errors.ts` | 与 `electron/main/browser/errors.ts` 同名；且 `lib/errors/` 下另有两个错误文件，错误概念散在三处。本文件实际是 `HttpError` 与 `parseHttpError` | `lib/http/http-error.ts`（`lib/errors/` 两个文件名已清楚，不动） | 21 (7) | P3 |

### 2.2 泛名与语义不符

| # | 当前路径 | 问题 | 建议路径 | 影响 | 优先级 |
|---|---|---|---|---|---|
| B1 | `lib/assistant/conversation-actions.ts` | 名字像「对话命令」，实际是输入框动作判定（`ComposerAction`、`deriveComposerAction`）与消息可编辑性判断；真正的命令在 `task-commands.ts`，两者易混 | `lib/assistant/composer-action-policy.ts`（若采纳 D3，落 `composer/action-policy.ts`） | 2 (1) | P2 |
| B2 | `lib/assistant/converter.ts` | 「谁转什么」不明。实际是 Transport 消息 → assistant-ui `ThreadMessage` 的转换，另夹带编辑会话标记、乐观用户命令。被 17 个文件引用 | `lib/assistant/transport-message-converter.ts` | 17 (8) | P2 |
| B3 | `components/assistant-ui/tools/display-utils.ts` | 泛名 `utils`；内容是对 `display_data` 的容错读取（`asRecord`、`readArray`、`readString`…） | `components/assistant-ui/tools/display-data-readers.ts` | 12 (0) | P2 |
| B4 | `components/assistant-ui/tools/artifact-card.ts` | 只导出 `ArtifactCardProps` 类型，名字像组件 | `components/assistant-ui/tools/artifact-card-props.ts` | 4 (0) | P3 |
| B5 | `lib/model/selection-constants.ts` | `constants` 泛名；内容只有 `REASONING_EFFORT_VALUES` | `lib/model/reasoning-effort.ts` | 2 (0) | P2 |
| B6 | `lib/api/tools.ts` | 只有 `getToolGroups`、`ToolGroupCatalog`，「tools」会被理解成工具执行 | `lib/api/tool-groups.ts` | 10 (0) | P2 |
| B7 | `lib/trace.ts` | 只生成 trace id，且在 `lib/` 根 | `lib/logging/trace-id.ts`（与 `frontend-log.ts` 同目录，同属可排查日志） | 21 (0) | P3 |
| B8 | `components/workbench/surface.tsx`、`lib/workbench/context.tsx`、`lib/workbench/types.ts` | `surface` / `context` / `types` 离开目录就不知所指 | `workbench-surface.tsx` / `workbench-context.tsx` / `workbench-tab-types.ts` | 1；8 (2)；7 | P2 |
| B9 | `lib/markdown-document.ts` | 内容是「Markdown 配置文档契约」（只被三个配置页用），名字却像通用 Markdown 工具 | `lib/configuration-document.ts` | 3 (0) | P2 |
| B10 | `lib/workspace-preferences.ts` | 名为 preferences，实际只读写「最近工作区 id」，且只被 `use-workspace-catalog.ts` 用 | `lib/last-workspace.ts` | 1 | P2 |
| B11 | `lib/agent-team-status.ts` | `lib/` 根文件，内容属 Team 编辑器 | `lib/agent-team-editor/team-status.ts` | 4 | P2 |
| B12 | `electron/main/backend/runtime.ts` | `runtime` 泛名；实际是「后端启动方式的解析」（`BackendRuntime`、`resolveBackendRuntime`） | `electron/main/backend/backend-runtime.ts` | 3 (1) | P2 |
| B13 | `electron/main/browser/browser-session.ts` | 内容是配置 Electron 持久分区、下载与权限；与 `cdp-session.ts` 的「session」不是一回事 | `electron/main/browser/browser-partition.ts` | 6 (3) | P3 |
| B14 | `components/assistant-ui/tools/terminal-output-view.ts` | 非 React，持有只读 xterm 实例与游标；`components/terminal/terminal-view.ts` 则是交互会话视图，同形异义 | `components/assistant-ui/tools/terminal-output/output-viewport.ts`（见 C2） | 2 (1) | P2 |
| B15 | `components/assistant-ui/tools/call-status.tsx` | 与 `lib/assistant/call-status.ts` 同名；前者渲染状态，后者推导状态。前者只被 1 个文件引用 | `components/assistant-ui/tools/call-status-badge.tsx` | 1 | P3 |

### 2.3 放错位置 / 职责重叠

| # | 当前路径 | 问题 | 建议路径 | 影响 | 优先级 |
|---|---|---|---|---|---|
| C1 | `components/assistant-ui/tools/details/`（7 个文件）与 `tools/views/`（6 个文件） | `call-view-registry.ts` 同时从两个目录取 `xxxView: CallView`，两个目录职责相同，新增一种视图无从判断放哪边。其中 `details/detail-view.tsx` 实际是展开体共用的外壳零件（`DetailSurface`、`DETAIL_ROW_CLASS`…），不是视图 | 合并为 `tools/call-views/`；`detail-view.tsx` → `call-views/detail-parts.tsx`；其余文件名不变 | 目录外引用：`details/` 7 个文件，`views/` 4 个文件 | P1 |
| C2 | `components/assistant-ui/tools/terminal-output-{preview,reader,reconciler,text,view}.ts(x)`、`terminal-viewport.tsx`、`terminal-write-scheduler.ts`（7 个） | 7 个同主题文件平铺在 42 个文件的 `tools/` 里；只被 `views/terminal-result-view.tsx` 间接使用 | 移入 `tools/terminal-output/`，`terminal-output-` 前缀在目录内去掉：`preview.tsx`、`reader.tsx`、`reconciler.ts`、`text.ts`、`output-viewport.ts`（原 `terminal-output-view.ts`）、`viewport.tsx`、`write-scheduler.ts` | 目录外引用 1 个文件 | P2 |
| C3 | `tools/agent-team-{configuration-tool,display,execution-card,execution-pipeline,tool}.ts(x)`、`tools/agent-configuration-draft-{display,tool}.ts(x)`、`tools/draft-save-status.tsx` | 8 个与 Agent / Team 配置草稿、Team 执行有关的文件平铺在 `tools/` | `tools/agent-team/`（执行卡片与审批：4 个）、`tools/configuration-draft/`（草稿工具与保存状态：4 个） | 目录外引用约 8 个文件 | P2 |
| C4 | `components/context-observer-page.tsx` | 页面文件躺在 `components/` 根，旁边就是 `context-observer/` | `components/context-observer/context-observer-page.tsx` | 1 | P1 |
| | `components/system-configuration-page.tsx`、`workspace-configuration-page.tsx`、`environment-configuration-form.tsx` | 同上，旁边就是 `configuration/` | `components/configuration/system-configuration-page.tsx` 等 | 1；1；2 | P1 |
| | `components/task-page.tsx`、`components/task-workspace-layout.tsx` | 任务页与它的布局边界在 `components/` 根，没有归属目录 | `components/task/task-page.tsx`、`components/task/task-workspace-layout.tsx` | 1；1 | P1 |
| C5 | `components/markdown-text.tsx`（2）、`static-markdown.tsx`（5）、`markdown-rendering-config.ts`（4） | Markdown 渲染散在 `components/` 根，旁边是 `markdown-editor/` | 新建 `components/markdown/` 放入这三个 | 合计 11 | P2 |
| | `components/code-editor-theme.ts` | 被 `markdown-editor` 与 `text-viewer` 共用 | `components/code-editor/code-editor-theme.ts` | 2 | P3 |
| C6 | `components/cosir-mark.tsx` | 只被 `backend/startup-screen.tsx` 使用 | `components/backend/cosir-mark.tsx` | 1 | P3 |
| | `components/root-error-boundary.tsx` | 四个错误边界里三个已在 `render-isolation/` 或各自域内，这个在根 | `components/render-isolation/root-error-boundary.tsx` | 1 | P3 |
| C7 | `components/files/files-panel.tsx`（独立目录里只有 1 个文件） | 与 `browser/browser-panel.tsx`、`workbench/workbench.tsx` 平行，但「files」与「workspace-files」两个目录同义 | `components/workspace-files/workspace-files-panel.tsx`，删除 `components/files/`（AGENTS.md 同步） | 1 | P3 |
| C8 | `app/globals.css`、`app/markdown.css` | `app/` 里只有两个 CSS，目录名暗示根组件 | `styles/globals.css`、`styles/markdown.css`；注意 `components.json` 的 `tailwind.css`（值为 `app/globals.css`）、`src/main.tsx`、`eslint.config.mjs` 注释、`tests/e2e/markdown-blocks.spec.ts` 注释按字符串引用 | 1；1 | P3 |

### 2.4 目录级命名（需要你决定，见第 4 节）

| # | 当前路径 | 问题 | 建议路径 | 影响 | 优先级 |
|---|---|---|---|---|---|
| D1 | `components/assistant-ui/`（70 个文件）与 `components/assistant/`（21 个文件） | 两个目录仅差 `-ui`，看路径无法说出分工：前者是对话渲染（`elements`、`tools`、`turn`），后者是 Assistant runtime 与传输会话接线。`assistant-ui` 还是第三方库名，`elements/` 里实际多是应用自有组件（`queued-messages`、`conversation-aside`、虚拟列表） | 方案一（推荐）：`components/conversation/{thread,tools,turn}`（原 assistant-ui）与 `components/conversation-runtime/`（原 assistant）。方案二：只改 `assistant` → `assistant-runtime`，不碰 `assistant-ui` | 目录外引用：assistant-ui 26 个文件，assistant 9 个文件；AGENTS.md 多处提到路径 | P2 |
| D2 | `components/task-rail/`（6）、`lib/task-rail/`（2 个文件） | 代码里叫 `TaskRail`，对话视口里叫 `ConversationAside`，AGENTS.md 叫「会话侧栏」，左边栏叫 `app-sidebar`：同一个位置「侧栏」两个意思 | `components/conversation-aside/`、`lib/conversation-aside/`，内部 `task-rail.tsx` → `conversation-aside-panel.tsx`、`use-task-rail.ts` → `use-conversation-aside.ts`；`cards.tsx`（2 个引用，泛名）→ `aside-card-registry.tsx`；`rail-card.tsx` → `aside-card.tsx` | 目录外引用：components 2 个文件；lib 另计；AGENTS.md 与 e2e 的 `task-rail.spec.ts` 同步 | P2 |
| D3 | `lib/assistant/`（45 个文件，被 147 个文件引用，其中 `contract.ts` 一个就有 86 个） | 平铺混了四个主题，看路径找不到「传输」「输入框」「工具」「回合展示」各在哪 | 子目录：`transport/`（assistant-snapshot-client、canonical-snapshot、snapshot-validation、task-transport-session、transport-frame-store、transport-history-context、transport-state-operations、use-task-assistant-transport-runtime、assistant-external-state.d.ts）、`composer/`（composer-drafts、edit-composer-draft、editable-user-document、prepare-user-command、skill-mention-token、team-approval-draft、submit-new-conversation-message、markdown-literals、conversation-actions→action-policy）、`tools/`（tool-catalog、tool-artifact、tool-call-index、tool-result-signals、call-status、file-changes）、`turn/`（turn-layout、turn-heading、current-activity、run-duration、message-height-cache、message-height-estimate、disclosure-store、image-stack-policy、user-message-segments）。`contract.ts`、`converter.ts`、`task-commands.ts`、`run-launch.ts`、`command-request-store.ts`、`assistant-performance-probe.ts` 留在根（跨主题）。`attachments/` 已是子目录 | 147 | P2 |

## 3. AGENTS.md 与路径的漂移（只记录，不改）

| 位置 | 写法 | 事实 | 处理 |
|---|---|---|---|
| 根 `AGENTS.md`「代码目录边界」 | `apps/desktop/`：`src/`（React 入口）、`app/`（根组件与全局样式） | `app/` 只有 `globals.css`、`markdown.css`；根组件是 `src/App.tsx` | 你决定是改文字，还是随 C8 把 `app/` 改成 `styles/` |
| 根 `AGENTS.md` | `components/files/` 为文件面板入口 | 目录存在（1 个文件） | 采纳 C7 时同步 |
| 根 `AGENTS.md` | `components/task-rail/`、`lib/task-rail/` 为会话侧栏 | 目录存在，但代码内部叫 `ConversationAside` | 采纳 D2 时同步 |
| 你提供的三处（`mult_agent/`、`core/tool/derivation`、`core/tool/task_tool_scope`） | | 在后端，不在本次范围 | 未核实 |

脚本核对：AGENTS.md 与 `apps/desktop/AGENTS.md` 里出现的 desktop 路径（带扩展名或反引号目录），当前全部存在，没有悬空引用。

## 4. 需要你决定的

1. **D1：`assistant` / `assistant-ui` 怎么分？** 方案一语义最清楚，但路径改动最大（目录外有 35 个引用文件，两目录有重叠时略少，另加 AGENTS.md 多处）。方案二只改一个目录名。我的推荐是方案一，理由：`assistant-ui` 是库名，让它占着一个应用目录会长期误导；但需要你确认「conversation」这个词作为全仓统一词。
2. **D2：`task-rail` 要不要改成 `conversation-aside`？** 代码里的两个叫法已经分裂，建议统一为 `ConversationAside`；代价是 AGENTS.md 里「会话侧栏」「TaskRail」的说法同步改。
3. **D3：`lib/assistant/` 要不要分子目录？** 引用面最大（147 个文件），但是纯路径变化、可由脚本改写，不改行为。

其余 A、B、C 项没有语义分歧，可直接按批次执行。

## 5. 刻意不改的

- **目录名 + 文件名前缀重复**（`terminal/terminal-view.ts`、`browser/browser-panel.tsx`、`composer/composer-surface.tsx`、`lib/terminal/terminal-api.ts` 等）：违反「目录表达域、文件名表达这件事」的纯粹形式，但前缀让编辑器标签页、搜索结果、`git diff` 里仅凭文件名就能辨认，且全仓一致。去掉前缀会制造更多 `session.ts` / `store.ts` 式的重名，反而更差。
- **偏好存储文件的后缀不统一**（`favorite-prompt-storage`、`pinned-task-storage`、`layout/preferences`、`theme-preference`、`model/selection-storage`）：每个都能看出是 localStorage 偏好，改后缀收益小；B10 的 `workspace-preferences` 因名不副实而单列。
- **`lib/api/configuration.ts`**（20 个引用，混着 Agent、全局指令、环境、忽略规则）：整体是「配置」域，名字不误导，仅内部可拆，本次不动。
- **`components/ui/` 里的非 shadcn 组件**（`panel-dock`、`find-bar`、`state-panel`…）：目录由 shadcn 别名固定，不拆。
- **`components/backend/use-decoded-text.ts`**：只被同目录 `startup-screen.tsx` 使用，位置合理。

## 6. 分批与验证

每批一个独立提交，批内只做路径变更（含 import 改写、测试文件改名、字符串引用、AGENTS.md 与 `docs/plan/*` 的路径文字），不夹带行为改动。用脚本改写 import，避免手工漏改。

| 批 | 内容 | 规模 | 验证 |
|---|---|---|---|
| 1 | **同目录改名**：A1–A9、B1–B13、B15 中不跨目录的项；先做 A1（5 个 store）与 A3（model-selector），收益最大 | 约 30 个文件改名，约 200 处 import | `npx tsc --noEmit`；`npx tsc -p electron`（`tsconfig.json` 把 `electron/main`、`electron/preload` 排除了，必须单独跑）；`npm run lint`；`npm run knip`（抓悬空与未用）；`npm run test:unit`；`node scripts/build-electron.mjs`（打包入口路径） |
| 2 | **跨目录搬家（小）**：C4–C8、B4/B7/B9–B11 | 约 15 个文件，各 1–4 个引用 | 同批 1，另加 `npx playwright test`（Vite + 内存服务，无头；不跑 `test:e2e:desktop`，会弹窗）；`grep` 核对 `components.json`、`knip.jsonc`、`eslint.config.mjs` 里的路径字符串 |
| 3 | **tools 目录整理**：C1、C2、C3、B3、B14 | 约 35 个文件，目录外引用约 30 个文件 | 同批 2；重点跑与工具卡片相关的 e2e：`tool-intent-activity`、`hidden-tool-details`、`terminal-output`、`background-surfaces`、`agent-team-*` |
| 4 | **需决定的目录改名**：D2，然后 D1 | D2 约 12 个文件；D1 约 91 个文件 | 同批 2，另跑完整 `npx playwright test`；同步 AGENTS.md 里的路径 |
| 5 | **`lib/assistant/` 分子目录**：D3 | 45 个文件，147 个引用文件 | 同批 4；脚本改写后先 `tsc` 再 `knip`；建议此批独立、前后工作区保持干净 |

提交顺序建议：1 → 2 → 3 → 4 → 5，每批之间工作区必须干净（当前 `apps/desktop` 下有 30 余个已修改文件未提交，这些文件有不少在上面的移动范围内，如 `call-row.tsx`、`call-view-registry.ts`、`detail-view.tsx`、`markdown-text.tsx`、`task-rail/background-task-row.tsx`、`lib/assistant/disclosure-store.ts`）。改名前先等这批改动提交。

每批完成后补一条：AGENTS.md 的目录说明、`docs/plan/` 里引用了旧路径的文档（用 `grep` 对着旧路径全量查一次）。

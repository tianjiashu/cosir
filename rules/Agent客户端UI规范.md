# Agent 客户端 UI 规范（设计令牌 / 样式纪律 / 自适应）

> 本规范是 `rules/Agent客户端代码开发规范.md` 的 **UI 专项派生文档**，专用于 `apps/desktop/`（Tauri 2 + React + TypeScript + Vite）的前端样式与界面一致性。
> 当本文件与 `rules/Agent客户端代码开发规范.md` 冲突时，以「设计令牌单一事实来源、反魔法值、自适应防溢出」优先；未覆盖的部分一律回退到客户端通用规范与后端规范。
> 关联文档：`rules/Agent客户端代码开发规范.md`、`rules/Agent代码开发规范.md`、`docs/ui-guidelines.md`、`docs/desktop-client-development-plan.md`、`AGENTS.md`。
>
> **为何独立成篇**：客户端特有的**设计令牌、样式一致性、自适应布局**是独立且高频的质量盲区，脱胎于以「正确性、日志、分层」为核心的通用规则，直接套用会产生盲区。本文件源自真实审查中反复出现的裸写 `text-[10px]`、`max-w-3xl` 散落、间距不一致、容器溢出等缺陷，必须作为独立条款强制执行。

---

## 一、UI 生态选型（强制）

写任何前端 UI 前，必须优先使用项目既定生态，禁止重复造轮子：

- **UI 组件库**：shadcn/ui + Radix UI（可访问性原语、分隔线、滚动区等一律走 Radix，禁止手写）。
- **样式**：Tailwind CSS（具名 token 在 `tailwind.config.ts`，语义 token 在 `components/ui/tokens.ts`）。
- **图标**：lucide-react。
- **状态**：Zustand。
- **通信**：HTTP 用原生 `fetch`（封装于 `services/api.ts`）；SSE 用 `fetch + ReadableStream`（封装于 `services/sse.ts`）。
- **校验**：zod。
- **复杂 UI**：表格用 TanStack Table、长列表用 TanStack Virtual、命令面板用 cmdk、代码展示用 Monaco、diff 展示用 react-diff-view。
- **禁止**引入与既定体系冲突的 UI 库（如 Ant Design / MUI 作为主体系被 `docs/ui-guidelines.md` 明确禁止）。新引入依赖必须锁定版本并评估是否值得。

---

## 二、设计令牌与样式纪律（核心，强制）

**设计令牌是样式的单一事实来源。本节约等于「前端的不重复造轮子」——禁止在组件里裸写绕过令牌的魔法值。**

### 2.1 令牌分层与位置

- **Tailwind 令牌**：尺寸/容器宽度/间距基准等经 `tailwind.config.ts` 的 `theme.extend` 声明具名 token（如 `maxWidth.content`、`maxWidth.bubble`），提交前由 lint 校验（`eslint-plugin-tailwindcss` 的 `no-arbitrary-value` + `settings.tailwindcss.callees: ["cn","cva"]`），禁止散写 `max-w-[85%]`/`max-w-3xl` 等任意值（除非该值是一次性、全局唯一且不具复用语义的极个别例外，且需注释理由）。
- **组件级语义令牌**：跨组件复用的细粒度视觉变量（统一的 caption 字号、代码块底座样式、1px 细线、折叠上限等）沉淀到 `apps/desktop/src/components/ui/tokens.ts` 单一文件，导出语义化常量（如 `Caption.xs`、`Caption.mono`、`Separator.horizontal`、`ScrollAreaPadding.all`、`Panel.codeBlockMaxHeight`、`CodeLine.minHeight`），供多文件 `import` 复用。该文件是 leaf，不反向依赖上层。
- **共享设计语义**：颜色语义、z-index、断点等全局约定，统一走 Tailwind 主题与 `@shared`；禁止在单文件内用 `style={{}}` 写死与主题冲突的裸值。

### 2.2 反魔法值（硬约束）

- **禁止裸写魔法字号**：`text-[10px]`、`text-[11px]`、`text-[13px]` 等任意像素字号一律改为引用 `tokens.ts` 的语义令牌（如 `cn(Caption.xs, "font-medium ...")`），修饰（字重、颜色）保留在 class 字符串，令牌只描述字号语义，保持原子化。
- **禁止裸写魔法间距/宽度**：`p-[7px]`、`gap-[3px]`、`max-h-[480px]`、`h-[1px]`、`w-[1px]`、`p-[1px]` 等任意值，优先用 Tailwind 标准间距档（`p-2`/`gap-1`/`space-y-3`）或具名 token；标准档无法表达时才在 `tokens.ts` 增补语义 token，调用处经白名单引用。
- **令牌只声明语义，不绑定修饰**：避免在 token 内塞满 `font-medium text-muted-foreground`，否则复用方无法覆盖；修饰交给调用处组合，符合「最小惊讶 + 单一职责」。
- **lint 护栏**：`eslint.config.js` 必须启用 `tailwind/no-arbitrary-value: "error"` 并配置 `settings.tailwindcss.callees: ["cn","cva"]`——否则对全项目最主流的 `className={cn(...)}` 写法完全漏检（假绿灯）。令牌定义文件（`tokens.ts`/`messageTypography.ts`）列入白名单，调用点引用 token 即合规，无需再加 `eslint-disable`。

### 2.3 自适应与防溢出（硬约束）

- **响应式容器**：聊天/内容区主宽度统一走 `max-w-content`（如 `48rem`），气泡走 `max-w-bubble`（如 `85%`）；禁止同一语义宽度在多处各自硬编码。
- **防溢出**：任何可能在一行排不下多个元素的容器（工具栏、头部、状态行）必须 `flex-wrap` 或明确处理溢出（省略号/`truncate`/横向滚动）；右栏/侧栏等定宽区域内部元素用 `min-w-0` 允许收缩，避免把父容器撑破。
- **InputBar 解耦**：输入区按钮组与文本框应是同一 `flex` 容器的兄弟节点（按钮组 `flex shrink-0`、文本框 `flex-1 min-w-0`），禁止用 `absolute` 定位 + 容器 `pr-N` 补偿这种脆弱写法——一改字号/内边距就错位。
- **层级间距一致性**：同层级列表项间距在全局保持一致（如 turn 之间 `space-y-6`、turn 内部条目 `space-y-3`），新增列表项时复用既有间距档，不随手写新值。

### 2.4 验证动作（提交前必做）

- 改样式前先 `search_content` 同类魔法值是否已在 `tokens.ts` 有对应令牌；有则复用，无则在 `tokens.ts` 增补并迁移全部调用点（Rule of Three：出现 3 次以上再抽，但字号类语义强相关的 2 处也应提前收敛）。
- 提交前 `tsc --noEmit` + `lint` 必须通过；含样式改动时，额外人工核对：是否有散落 `text-[Npx]`、是否有未走 token 的 `max-w-[N%]`/`max-w-3xl`、定宽容器内是否有 `min-w-0`/`flex-wrap`。
- 自测窄视口（如侧栏 200px、窗口缩小）下是否出现横向滚动条或溢出裁切。

---

## 三、审查 Agent 的 UI 专项（必须纳入，不可放过）

UI 专项（设计令牌、魔法值、间距一致性、自适应）必须纳入独立审查 Agent 职责，不可因「通用规范未强调」而放过：

- **反魔法值**：全局 `search_content` 改动文件及关联组件，确认无裸写 `text-[Npx]`、无散写 `max-w-3xl`/`max-w-[N%]`、无裸写 `p-[Npx]`/`gap-[Npx]`/`h-[Npx]`/`w-[Npx]`；魔法值必须走 `tailwind.config.ts` 具名 token 或 `components/ui/tokens.ts` 语义令牌。
- **间距一致性**：同层级列表项间距是否复用全局档位，未随手写新值。
- **自适应防溢出**：定宽容器（侧栏/右栏/工具栏/头部）是否带 `min-w-0`/`flex-wrap`；InputBar 是否用 flex 解耦而非 `absolute`+`pr-N` 补偿。
- **lint 护栏有效性**：确认 `no-arbitrary-value` 配置了 `callees: ["cn","cva"]`，且 `eslint .` 对 `cn(...)` 内的任意值真实报错（用探针文件实测，不凭声明）。
- **区分"遗漏"与"已知结构性缺口"**：审查结论须说明哪些是本规范要求的硬约束违反（必须改），哪些是项目既有未收敛项（记录为已知缺口，不强行在本次扩散修改）。
- **记录验证动作**：审查 Agent 必须记录搜索了哪些魔法值模式、读了几处关联文件、`tsc`/`lint` 结果，避免「凭印象通过」。

---

## 四、禁止行为（UI 专属）

| 禁止 | 原因 |
|------|------|
| **裸写魔法字号**（如 `text-[10px]`/`text-[11px]`） | 设计令牌未走 token，全局改字号需逐文件搜改 |
| **裸写魔法间距/宽度**（如 `p-[7px]`/`max-h-[480px]`/`h-[1px]`/`p-[1px]`） | 间距/细线语义不一致，复用与维护成本陡增 |
| **散写容器宽度**（如 `max-w-3xl`/`max-w-[85%]`） | 同一语义宽度多处硬编码，后续难统一收敛 |
| **定宽容器内无 `min-w-0`/`flex-wrap`** | 子元素撑破容器导致横向溢出 |
| **InputBar 用 `absolute` + `pr-N` 补偿定位** | 一改字号/内边距就错位，脆弱难维护 |
| **引入 Ant Design / MUI 作为主 UI 体系** | 与 `docs/ui-guidelines.md` 决议冲突 |
| **手写 shadcn/Radix/TanStack/zod 已有功能** | 重复造轮子 |
| **在 `tokens.ts` 之外散落任意值且不加理由** | 绕过令牌单一事实来源 |
| **`no-arbitrary-value` 不配 `callees` 就宣布 lint 通过** | `cn()` 写法漏检，假绿灯 |

---

> **设计令牌是样式的单一事实来源，裸写魔法值（`text-[Npx]`/`max-w-[N%]`）即重复造轮子。**
> **自适应是硬约束：定宽容器必 `min-w-0`/`flex-wrap`，主宽度走 token，不裸写百分比。**
> **lint 护栏必须对 `cn()` 生效：配 `callees: ["cn","cva"]`，否则假绿灯。**
> **样式改动必须走 token，提交前 tsc + lint + 窄视口自测三件套。**

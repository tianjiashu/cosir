# 前端自研逻辑 → 开源/标准库替代 改造计划

> **文档性质**：改造方案（待实施，先经独立审查 Agent 闭环）
> **关联报告**：`docs/frontend-opensource-replacement-report.md`
> **日期**：2026-08-17
> **范围**：仅收录经审查后确认「认可」的替换项；SSE 连接管理（报告 2.2）已独立重构（见 `docs/sse-connection-refactor-plan.md`），不在本计划。

---

## 0. 取舍总览

本报告只实施**已被确认认可**的条目，并按「用户价值 / 风险 / 依赖必要性」排序：

| 条目 | 来源 | 认可结论 | 依赖 | 优先级 |
|------|------|----------|------|--------|
| 语法高亮空实现 | 报告 2.1 | 强烈认可（真实缺陷） | `lowlight` + `hast-util-to-html` + `dompurify` | 高 |
| 错误边界 Class→函数式 | 报告 2.4 | 认可 | `react-error-boundary` | 高 |
| 日志脱敏 | 报告 3.1 | 认可 | `fast-redact`（`serialize:false` 模式） | 中 |
| case 转换 | 报告 3.2 | 认可 | `change-case` | 中 |
| 剪贴板复制 | 报告 3.4 | 认可（**仅内部 hook**，不引外部库） | 内部 `useCopyToClipboard` hook | 中 |
| HTTP 客户端 | 报告 2.3 | 认可「问题」，批准引入 `ky`（收益/风险比已拍板） | `ky`（v2.0.2，浏览器运行时） | 高（Phase 3） |
| Trace ID | 报告 3.3 | 弱认可，顺手做 | `nanoid`（待定） | 低 |

**不实施 / 已处理**：
- 2.2 SSE 换 `@microsoft/fetch-event-source`：**不采纳**（语义冲突 + 死库），已由 `sseConnectionBase.ts` 内部抽象解决。
- 4.x 低优先级（mergeRefs / PerfTrace / 节流 / rAF 攒批）：保持现状或仅抽内部 hook，不引外部库。

---

## 1. 语法高亮（报告 2.1）

### 1.1 现状事实
- `src/lib/markdown/highlight.ts`：`highlightCode` 为 passthrough 空实现，始终返回 `null`。
- `CodeBlock.tsx` 中 `dangerouslySetInnerHTML` 高亮分支因 `highlightCode` 返回 `null` 永不被触发，所有代码以纯文本渲染。
- 已预留 `HighlightFn` 契约接口。

### 1.2 目标方案
- 引入 `lowlight`（latest 3.3.0，MIT，wooorm 维护）+ `hast-util-to-html`（序列化 hast 树为 HTML）+ `dompurify`（净化）。
- API 事实（已核实 lowlight 官方 README）：
  - `import { common, createLowlight } from 'lowlight'`（common 从 lowlight 导出，非 highlight.js）。
  - `const lowlight = createLowlight(common)`。
  - `const tree = lowlight.highlight(language, code)` → 返回 **hast Root 树**（非字符串）。
  - `import { toHtml } from 'hast-util-to-html'; const html = toHtml(tree)` → 带 `hljs-*` class 的 HTML。
- 实现 `highlightCode: HighlightFn`：按语言调用 `lowlight.highlight(language, code)` → `toHtml(tree)` → `DOMPurify.sanitize(html)`；语言不支持/抛错时回退 `null`（纯文本）。
- `CodeBlock.tsx` 渲染高亮 HTML 时，必须渲染**已净化的** `dangerouslySetInnerHTML`。
- 保留 `HighlightFn` 契约，替换零侵入。

### 1.3 验收
- 代码块出现语法高亮；未知/不支持语言回退纯文本。
- 高亮 HTML 经 DOMPurify 净化，无 XSS 直插。
- `HighlightFn` 契约不变，替换零侵入。
- 主题 CSS（hljs-xxx）由现有样式或引入 highlight.js 主题提供，lowlight 不自带 CSS。

---

## 2. 错误边界（报告 2.4）

### 2.1 现状事实
- `src/components/ErrorBoundary.tsx`：React Class Component，仅 `getDerivedStateFromError` + `componentDidCatch(logError)` + 显示错误。无重试能力。

### 2.2 目标方案
- 引入 `react-error-boundary`。
- 用 `<ErrorBoundary FallbackComponent={ErrorFallback} onReset={...} resetKeys={[key]}>` 包裹根 App / 关键边界。
- `ErrorFallback` 提供 `resetErrorBoundary` 重试按钮。
- 删除原 Class Component（或保留为薄壳兼容层，若无其他引用则删除）。

### 2.3 验收
- 函数式写法，符合项目风格。
- 用户可点击重试（`resetErrorBoundary`）。
- 原 `logError` 行为保留（在 `ErrorFallback` / `onError` 中调用）。

---

## 3. 日志脱敏（报告 3.1）

### 3.1 现状事实
- `src/lib/logger.ts`：自研 `redactContext()` 硬编码 12 个敏感键、递归遍历整个 context、`toLowerCase()` 精确匹配、不支持路径匹配（如 `headers.authorization`）。

### 3.2 目标方案
- 引入 `fast-redact`（latest 3.5.0，MIT，matteo.collina/davidmarkclements 维护，pino 同款）。
- API 事实（已核实 GitHub README）：
  - `const redact = fastRedact({ paths, censor })` 默认返回**序列化 JSON 字符串**（默认 `JSON.stringify`）。
  - `paths` 支持点/括号表示法、数组索引、**通配符**（`a.b.*`、`*.authorization`、`a[*].c`），正好解决现有"只做精确 `toLowerCase()` 匹配、不支持路径"缺陷。
  - 默认会**变异原对象再还原**；设 `serialize: false` 时返回变异后的原对象（`=== obj`）。
- **关键约束**：现有 `redactContext` 返回**对象**（`Record<string,unknown>`，被 `logInfo/logWarn/logError` 直接读字段写入 `LogEntry`）。若用默认模式会返回字符串、破坏日志结构。因此必须用 `serialize: false`：
  ```ts
  const redact = fastRedact({ paths: SENSITIVE_PATHS, censor: "[REDACTED]", serialize: false });
  const safe = redact(structuredClone(merged)); // 不改原对象
  ```
  返回类型保持 `Record<string, unknown>`，调用方零改动。
- `paths` 配置化（覆盖现有 12 个键 + 路径形式如 `*.authorization`、`password`、`token`、`secret`），新增敏感键只改配置不动源码。
- 保留 `normalizeContextKeys` / `toSnakeCase`（§4 会被替换）等其它逻辑不受影响。

### 3.3 验收
- 敏感键与路径（如 `headers.authorization`、`a.b.*`）均被脱敏。
- 非敏感字段原样保留；返回类型仍为对象，日志结构不回归。
- 配置化：新增敏感键只改 paths 列表。

---

## 4. case 转换（报告 3.2）

### 4.1 现状事实
- `src/lib/logger.ts`（第 165-172 行）：自研 `toSnakeCase()` 两条正则，边界覆盖不全（`XMLParser`→`xml_parser`、数字后接大写等）。

### 4.2 目标方案
- 引入 `change-case` 的 `snakeCase()`。
- 替换 `toSnakeCase` 调用点，删除自研实现。
- 验证边界用例：`XMLParser`→`xml_parser`、`version2Update`→`version_2_update`。

### 4.3 验收
- 边界用例正确；现有调用方行为不回归。

---

## 5. 剪贴板复制（报告 3.4）

### 5.1 现状事实
- `CodeBlock.tsx` / `TerminalCallCard.tsx` / `StatusBadge.tsx` / `ToolCallCard.tsx`：4 处同构 `navigator.clipboard.writeText + useState(copied) + setTimeout(reset, 2000)`。

### 5.2 目标方案
- **仅抽内部 `useCopyToClipboard` hook**（放 `src/hooks/useCopyToClipboard.ts`），统一 `copied` 状态与 2000ms 重置行为。
- **不引入外部库**：已核实 `use-copy-to-clipboard@1.0.7` 最后发布内部时间戳 **2020-06-09**（停更 6 年），依赖 `styled-components@5` + `react@16`，拉重依赖且维护停滞，违反"评估长期可维护性"底线，排除。
- 4 处组件（CodeBlock / TerminalCallCard / StatusBadge / ToolCallCard，已确认均有同构 `navigator.clipboard.writeText` + `setCopied(true)` + `setTimeout(()=>setCopied(false),2000)` 样板）统一消费该 hook，删除重复样板。

### 5.3 验收
- 4 处复制行为一致（反馈时长、文案）。
- 无重复 `setTimeout(reset, 2000)` 样板；无新增外部依赖。

---

## 6. HTTP 客户端（报告 2.3，已批准：引入 ky）

### 6.1 认可的问题
- `src/services/api.ts` 手写 `post/get/del` 封装原生 fetch，无超时（长请求可能无限挂起）、无重试、无统一拦截。

### 6.2 决策（2026-08-17 用户拍板：引入 ky）
- 引入 `ky@^2.0.2`（latest 稳定版，sindresorhus 维护，无依赖，浏览器运行时直接用全局 `fetch`，**不要求 Node 22**——Node 22 限制仅作用于 Node 端运行）。
- SSE 端点**继续用原生 fetch 不动**：`connectWorkspaceEventStream`（`api.ts`）与 `SSEConnectionBase`（`sseConnectionBase.ts`/`sse.ts`/`delegationStream.ts`）均消费 `ReadableStream`，ky 不消费流，明确排除。
- `ky.HTTPError` 必须适配到现有 `ServiceError`：在 `ky.create` 的 `hooks.beforeError` 中把 `HTTPError` 转成 `ServiceError`（保留 `statusCode` + 解析 `body.detail`），上层业务代码零改动。

### 6.3 目标方案
- 新增 `src/services/httpClient.ts`：用 `ky.create` 建立单一配置实例 `apiClient`，固化：
  - `prefixUrl: ""`（开发走 Vite 代理，与生产同源）。
  - `timeout: 30000`（每轮尝试 30s，覆盖长请求如 workspace prepare 数分钟需更长或 `totalTimeout`，但普通 API 30s 合理；prepareWorkspace 等个别长请求可 override `timeout:false`）。
  - `retry`: 默认对幂等方法（GET/HEAD/OPTIONS/PUT/DELETE）失败重试 `limit: 2`；POST 不重试（非幂等）。
  - `hooks.beforeError`: 将 `HTTPError` → `ServiceError`（`statusCode` + `body.detail`），非 HTTP 错误（网络/超时）也归一为 `ServiceError`。
  - `throwHttpErrors: true`（默认，非 2xx 抛错由 beforeError 接管转换）。
- 改写 `api.ts` 的 `post/get/del`：内部改用 `apiClient.post(...).json()` / `.get(...).json()` / `.delete(...)`，移除手写的 `response.ok` 判定与 `buildError` 中重复的状态码提取（状态码已由 `beforeError` 统一写入 `ServiceError`）；但保留 `buildError` 对 `body.detail` 的解析能力在 `beforeError` 复用，避免重复逻辑。
- **不改动**：`connectWorkspaceEventStream`（SSE，原生 fetch）、`SSEConnectionBase` 及其子类、`tracePropagation` 调用（trace header 由 `api.ts` 在调用 `apiClient` 前注入，ky 透传 headers）。
- 现有 `recordBackendTrace` / `recordConversationTrace` / 日志（`logError`/`logWarn`）行为全部保留，错误路径经 `beforeError` 统一写 `ServiceError` 后由 `api.ts` 的 `catch` 继续 `logError`（带 module/path/status_code 上下文）。

### 6.4 验收
- 普通请求（post/get/del）走 ky，获得超时 + 幂等重试 + 统一错误归一。
- 非 2xx 错误表现为 `ServiceError`（含 `statusCode` 与 `detail`），上层 `useTask`/`useStartupTaskResume` 等 catch 逻辑零回归。
- SSE 端点行为完全不变（独立测试覆盖：SSE 仍用原生 fetch，未触碰）。
- 错误日志带 module/path/status_code 上下文，可排查（不退化）。

---

## 7. 实施顺序与闭环

1. **Phase 1（用户体验，低风险）**：§1 语法高亮、§2 错误边界。
2. **Phase 2（工具函数，低风险）**：§3 日志脱敏、§4 case 转换、§5 剪贴板 hook。
3. **Phase 3（已批准）**：§6 HTTP 客户端（引入 ky）。
4. 每项均走「开发子 Agent（TDD）→ 独立审查 Agent → 独立测试 Agent」闭环，直到审查 + 测试均通过。

---

## 8. 依赖新增清单（待实施时锁定版本）

| 依赖 | 用途 | 状态 |
|------|------|------|
| `lowlight` | 语法高亮（返回 hast 树） | 引入 @3.3.0，锁版本 |
| `hast-util-to-html` | hast 树 → HTML 字符串 | 引入，锁版本 |
| `dompurify` | 高亮 HTML 净化 | 引入，锁版本 |
| `react-error-boundary` | 错误边界 | 引入 @6.1.3，锁版本 |
| `fast-redact` | 日志脱敏（`serialize:false` 模式） | 引入 @3.5.0，锁版本 |
| `change-case` | case 转换（`snakeCase`） | 引入 @5.4.4，锁版本 |
| 内部 `useCopyToClipboard` hook | 剪贴板（**不引外部库**） | 新增源码，无依赖 |
| `ky` | HTTP 客户端（普通请求，SSE 排除） | 引入 @^2.0.2，锁版本 |

---

## 9. 实施状态（2026-08-17）

### 9.1 已落地（Phase 1 + Phase 2 全部 5 项）
- §1 语法高亮：`src/lib/markdown/highlight.ts` —— lowlight + hast-util-to-html + DOMPurify 替代首版 passthrough 空实现。
- §2 错误边界：`src/components/ErrorBoundary.tsx` —— react-error-boundary 函数式组件替代 Class Component，提供重试 + logError 记录。
- §3 日志脱敏：`src/lib/logger.ts` —— fast-redact（serialize:false + 自研 redactRecursive 递归）替代自研 redactValue。
- §4 case 转换：`src/lib/logger.ts` —— change-case snakeCase 替代自研 toSnakeCase 正则。
- §5 剪贴板：`src/hooks/useCopyToClipboard.ts`（内部 hook，无外部依赖），替换 CodeBlock / TerminalCallCard / StatusBadge / ToolCallCard 四处散落样板。

### 9.2 闭环结论（独立审查 + 独立测试）
- **独立审查 Agent**：复审结论「符合」（5 项修复到位，无死代码 / 冗余 import / 空 catch / 裸 console 日志）。
- **独立测试 Agent**：70 测试全过（logger / highlight / useCopyToClipboard×2 / ErrorBoundary×2 + 测试 Agent 补充的 boundary×5），失败路径经 logWarn/logError 记录带 module 上下文，可排查。
- 开发过程中踩坑并修正：
  1. `fast-redact` 的 `deep:true` 单例复用有数组脱敏失效 bug → 改为非 deep + 自研递归遍历。
  2. `lowlight.highlight(language, code)` 参数顺序（语言名在前），返回 hast 树需 `toHtml` 序列化。
  3. DOMPurify 在 node 测试环境无 window → 加 `purify` 降级透传（生产走浏览器净化）。
  4. hook 初版裸 `console.warn` + 调用方 `.catch` 死代码 → 改为 `logWarn` 统一出口 + `copy` 返回 boolean。

### 9.3 待办
- §6 HTTP 客户端（ky）：**已批准，进入实施**（见下方实施状态补充）。
- tsc 全局仅剩 1 个**预存在历史错误** `src/tests/review.clientDisconnectedNoUI.test.ts`（.ts 文件误用 JSX 语法），非本次改动，按规范不越界修。

### 9.4 §6 ky 实施状态（2026-08-17，已闭环）
- **调研结论**：ky v2.0.2（latest）浏览器运行时直接用全局 fetch，不要求 Node 22（该限制仅作用于 Node 端运行）；SSE 走自研原生 fetch 不变。⚠️ 关键事实：ky v2 的 `HTTPError` 已**预解析响应体到 `error.data`**，`error.response.json()` 不可用（body 已被消费），归一逻辑须读 `error.data?.detail` 而非 `response.clone().json()`。
- **实施范围**：
  - 新增 `src/services/httpClient.ts`：`ky.create` 单一实例 `apiClient`，`prefixUrl:""` / `timeout:30000` / `retry.methods` 显式排除 POST（仅 get/head/options/put/delete，`limit:2`）/ `throwHttpErrors:true`；`hooks.beforeError` 经抽取出的纯函数 `normalizeToServiceError` 把任意 ky 错误归一为 `ServiceError`（HTTP 错误取 `error.response.status` + `error.data.detail`；非 HTTP 错误 `statusCode:0`）。
  - 改写 `src/services/api.ts` 的 `post/get/del` 走 `apiClient`；删除旧 `buildError`（已无引用）；保留 `recordBackendTrace` / `buildTraceHeaders` 注入 / `logError` 带 module/path/status_code 上下文；SSE 的 `connectWorkspaceEventStream` 保持原生 fetch 不动；修正模块顶部过时注释。
  - `package.json` 增 `"ky":"^2.0.2"` 并实际安装。
- **闭环结论**：
  - 独立审查 Agent：「符合」（9 项核查全过；建议项已修正 api.ts 顶部过时注释）。
  - 独立测试 Agent：「通过」——新增 `httpClient.test.ts`(14) + `apiKy.test.ts`(24) = 38 用例；回归 SSE 测试 20 用例全绿。httpClient.ts 覆盖率 100%/100%/100%，api.ts 89.38%/81.81%/100%。变异检查（retry.methods 注入 post、statusCode 误取 0）均按预期变红后还原。
  - 遗留：工作区预存在 tsc 错误 `src/tests/review.clientDisconnectedNoUI.test.ts`（.ts 误用 JSX，非本次改动，不越界修）；`coverage-tmp/` 为 v8 临时产物，可手动清理。

---


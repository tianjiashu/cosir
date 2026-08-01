# 客户端流式显示与排版优化（A 组：排版 / 流式观感）设计规格

> 本 spec 是「客户端流式显示与排版优化」方案的 **A 组** 部分。A 组聚焦前端排版统一与流式观感，零新依赖。
> B 组（timeline 虚拟滚动 + `@tanstack/react-virtual` + 展开态持久化）为独立 spec，不在本文件范围。
> 上游决策来源：`docs/客户端流式显示与排版优化技术方案.md`；本 spec 经 brainstorming 流程与用户逐节确认。

---

## 一、范围与目标

### 1.1 范围边界

| 序号 | 文件 | 改动 |
|---|---|---|
| 1 | `src/components/chat/messageTypography.ts`（新增） | 定义 `MessageTypography` 排版 token 单一事实来源 |
| 2 | `AgentMessage.tsx` | 复用 token；新增 `streaming` prop；末节点探测渲染 caret；向 `CodeBlock` 透传 `streaming` |
| 3 | `ThinkingBlock.tsx` | 新增 `streaming` prop；流式期直接渲染累积内容 + caret，不显示折叠栏；结束后恢复折叠交互 |
| 4 | `CodeBlock.tsx` | 新增 `streaming` prop；超阈值折叠 + 「展开完整代码」；行内 `break-words` |
| 5 | `UserMessage.tsx` | 复用 `MessageTypography.body` |
| 6 | `services/timeline/projector.ts` | `TurnTimelineEntry` 增加 `streaming?: boolean`；pending 块实时投影为 `streaming: true` |
| 7 | `components/layout/TurnTimeline.tsx` | 把 `entry.streaming` 透传给 `AgentMessage` / `ThinkingBlock` |
| 8 | 测试 | 组件级测试 + `timelineProjector.test.ts` 扩展断言 |

**明确不在 A 组**：`ChatPanel` 虚拟滚动改造、引入 `@tanstack/react-virtual`、store 展开态持久化（均归 B 组）。A 组不依赖 B 组，可独立合并与验证。

### 1.2 目标

1. 统一排版 token，消除各消息组件各自写 `text-sm leading-relaxed` 的漂移。
2. 流式正文渲染稳定闪烁 caret，提升「实时生成」感知（对齐 Codex 风格）。
3. 流式期视觉稳定：容器 `min-h`/`min-w` 防塌缩；超长代码块可折叠。
4. 思考块流式期实时可见（累积内容 + caret），不再只在完成时整块出现。
5. 富文本排版加固：行内代码 / 文件路径 `break-words` 防撑破。

### 1.3 设计取舍（已与用户确认）

- **拆两批**：A 组（排版/流式，零依赖，低风险）先、B 组（虚拟滚动，引入新依赖 + 改渲染骨架）后，各自独立 spec + 计划 + 闭环。
- **消息块级 streaming 信号**：caret / 思考块流式可见依赖「这条消息块是否正在生成」，而非 turn 级 running（否则正文生成时思考块也会闪 caret）。
- **projector 增强承载 streaming 信号**：给 `TurnTimelineEntry` 加 `streaming?: boolean`，projector 投影 pending 块为 `streaming: true`；组件只读字段，符合「projector 是投影单一事实来源」分层。不在组件侧推断。
- **caret 末节点探测（纯 React）**：caret 精确注入 markdown 最后一个叶子节点末尾（方案 B），用 `markdownComponents` 块级标签自定义渲染 + `isLastLeaf` 判断实现，保持声明式、可测试；不用渲染后 DOM 注入。
- **thinking 流式期不显示折叠栏**：流式期直接展开累积内容 + caret，避免流式期反复切换折叠态抖动；结束后恢复「默认折叠、点击展开」。
- **流式期 thinking / assistant 的 `expanded` 折叠态用本地 `useState`**：A 组无虚拟滚动回收，无丢失风险，不提前引入 store 持久化（归 B 组配套）。
- **零新依赖**：caret / 折叠 / token 全手写，基于既有 `tailwindcss` + `lucide-react` + shadcn 原语。

---

## 二、排版 token 与 caret 规格

### 2.1 `messageTypography.ts`

新增 `src/components/chat/messageTypography.ts`：

```ts
// 消息排版 token：所有消息组件复用，禁止各写一套 text-sm leading-relaxed
export const MessageTypography = {
  body: "text-[13px] leading-7", // 正文：介于 sm(14) 与 xs(12) 之间，行距 28px 透气
  secondary: "text-xs leading-5", // 思考块 / 代码块 / 元信息：稳定次要层级
  code: "text-xs leading-5 font-mono", // 等宽代码
  caret: "inline-block w-[1px] h-[1em] align-text-bottom animate-pulse bg-foreground/70", // 流式光标
} as const;
```

- 正文 `body` `text-[13px] leading-7`：比当前 `text-sm`(14px) 略小、行距明确。
- 次要 `secondary` `text-xs leading-5`：思考块 / 代码块 / 元信息统一层级。
- caret 纯 CSS `animate-pulse`，零依赖，`aria-hidden`。

### 2.2 caret 渲染规则（`AgentMessage`）

- 新增可选 prop `streaming?: boolean`。
- 渲染策略（末节点探测，纯 React）：
  - `AgentMessage` 预先解析 `content`，确定「最后一个非空块级元素」的标签（`p` / `pre` / `ul` / `ol` / `blockquote` / `h1`-`h6`）。
  - 在 `markdownComponents` 的对应块级自定义组件里接收 `isLastLeaf`；当 `streaming === true && content.trim().length > 0 && isLastLeaf` 时，在该元素末尾追加 `<span className={MessageTypography.caret} aria-hidden />`。
  - 非末块、或非 streaming、或内容为空时不渲染 caret。
- 正文容器（`ReactMarkdown` 外层 div）加 `min-h-[1.5em]`（首字到达前防塌缩）+ `min-w-0`（防 inline 撑破 flex）。
- 正文容器类名由 `text-sm leading-relaxed` 改为 `MessageTypography.body`。

### 2.3 可访问性

- caret 加 `aria-hidden`，不参与屏幕阅读器朗读。
- `prefers-reduced-motion` 下 caret 不闪烁（CSS media query 关闭 `animate-pulse`，作为后续细节在落地时处理，不阻塞主流程）。

---

## 三、projector streaming 投影语义

### 3.1 类型扩展

`TurnTimelineEntry` 扩展：

```ts
| { kind: "assistant"; eventId: string; content: string; streaming?: boolean }
| { kind: "thinking"; eventId: string; content: string; streaming?: boolean }
| { kind: "tool"; item: TimelineToolItem }
| { kind: "status"; eventId: string; eventType: RuntimeEvent["event_type"]; payload: RuntimeEvent["payload"] };
```

### 3.2 投影逻辑（`projectEntries`）

- **已 flush 的历史块**：`streaming` 省略或为 `false`。
- **当前正在累积的 pending 块**：在产出 entries 时，把仍 pending 的 `pendingDelta` / `pendingThinking` 作为**末尾一条** `streaming: true` 的 entry 输出，其 `content` 为当前累积文本。
- 具体做法：
  - 循环内遇到中断事件（工具、状态、未识别事件）时，先 flush 已完成块（`streaming` 假）。
  - 循环结束后，若仍有 `pendingDelta` / `pendingThinking` 未因中断 flush，则作为 `streaming: true` 的末尾 entry 加入（**不清除 pending**，保证下次重投影时仍从累积态继续——配合 `TurnTimeline` memo，活跃 turn 重投影、历史 turn 跳过）。
- **结束信号边界**：`run_finished` / `run_failed` / `run_cancelled` 到达时，pending 块应被 flush 为 `streaming: false`，不产生 `streaming: true` 的悬空块。
- `final_response` 与现有 `hasDeltaStreamed` 去重逻辑保持不变；若投影为 assistant 条目，其 `streaming` 为 false（终态）。

### 3.3 `TurnTimeline.tsx` 透传

- `entry.kind === "assistant"` → `<AgentMessage content={entry.content} streaming={entry.streaming} />`
- `entry.kind === "thinking"` → `<ThinkingBlock content={entry.content} streaming={entry.streaming} />`

---

## 四、思考块与代码块流式行为

### 4.1 `ThinkingBlock`

- 新增 `streaming?: boolean`。
- `streaming === true`：直接渲染累积 `content`（用 `MessageTypography.secondary`）+ caret，**不渲染折叠标题栏**（不渲染「深度思考」按钮），避免流式期反复切换折叠态抖动。
- `streaming === false`（含非流式历史 / 无 streaming 字段）：维持现有「默认折叠、点击展开」交互（本地 `useState(expanded)`）。
- 空内容防御（`content.trim().length === 0` 返回 null）保持现状。
- caret 渲染规则同 2.2（thinking 块为单一文本块，caret 置于文本末尾；thinking 非 markdown，直接在末尾 span 追加）。

### 4.2 `CodeBlock`

- 新增 `streaming?: boolean`。
- 折叠阈值常量（模块级，可配）：`CODE_FOLD_LINES = 12`、`CODE_FOLD_CHARS = 600`。
- `streaming === true` 且（行数 `> CODE_FOLD_LINES` 或字符 `> CODE_FOLD_CHARS`）：默认 `max-h-48 overflow-hidden` + 底部渐变遮罩 + 「展开完整代码」按钮；点击展开。
- 非流式期（`streaming !== true`）：保持原有渲染（或既有截断逻辑），不强制折叠。
- `<pre>` 内行内代码 / 路径加 `break-words`（路径可用 `break-all`），防超长 token 撑破布局。

---

## 五、UserMessage 与富文本加固

- `UserMessage` 气泡内文字复用 `MessageTypography.body`（与 Assistant 正文同字号行距，避免对比突兀）。
- 行内代码（markdown `code`）、`FileLink` 路径统一 `break-words`。
- `AgentMessage` 正文容器统一 `MessageTypography.body` 替代散落 `text-sm leading-relaxed`。

---

## 六、测试规格

### 6.1 `timelineProjector.test.ts`（扩展，核心）

- 进行中块 streaming 投影：含连续 `model_output_delta` 的 events → 末尾 assistant entry `streaming === true`、content 为累积文本。
- 中断后 flush 的块 streaming 假：`delta` 后跟 `tool_call_started` → assistant 块 `streaming` 为假/undefined；thinking 同理。
- 结束信号无悬空：delta 后跟 `run_finished` → assistant 块 `streaming` 假，无残留 `streaming: true` entry。
- 思考/回答交错：thinking delta → assistant delta 序列，各自 streaming 标志正确。

### 6.2 `agentMessage.test.tsx`（新增）

- `streaming=true && content 非空` → 渲染 caret span（`aria-hidden` + `animate-pulse`）；`streaming=false` → 不渲染。
- 末节点探测：构造「以段落结尾」「以代码块结尾」两种 markdown，断言 caret 位于末块内（而非块外容器）。
- 容器含 `min-h-[1.5em]` / `min-w-0` 类。

### 6.3 `thinkingBlock.test.tsx`（新增）

- `streaming=true` → 渲染累积 content + caret，且无「深度思考」折叠按钮。
- `streaming=false` → 维持折叠交互（默认不展开，点击展开）。

### 6.4 `codeBlock.test.tsx`（新增）

- `streaming=true` + 超阈值代码 → 渲染「展开完整代码」按钮 + 折叠容器；同内容 `streaming=false` → 不折叠。
- 行内超长路径含 `break-words` 类。

---

## 七、验证与闭环

1. **类型检查**：`cd apps/desktop && npx tsc --noEmit` 零错误（A 组零新依赖，仅类型）。
2. **测试**：`npx vitest run` 全量通过（含第六章节新增/扩展用例）。
3. **独立审查 Agent**：静态复查分层 / 单一职责 / 无死代码 / docstring。
4. **独立测试 Agent**：独立执行测试覆盖业务与边界场景，输出通过/不通过结论。
5. **视觉自查（手动）**：运行桌面端，确认流式正文 caret 闪烁、思考块实时可见、代码块流式期可折叠、字号行距统一。

---

## 八、风险与注意事项

- **caret 与 SSE 增量**：caret 是纯 CSS 视觉锚点，不参与文本渲染，不会与流式增量冲突。
- **`min-h` 防塌缩**：仅在容器无内容时生效，有内容后由内容撑开，不影响正常排版。
- **代码块折叠阈值**：阈值（行数 / 字符数）为模块常量初值，避免误伤短代码；非流式期保持原样。
- **projector pending 不清除**：循环结束后把 pending 块作为 `streaming: true` 末尾 entry 时，不清除 pending，确保下次重投影从累积态继续；需确认与 `TurnTimeline` memo（活跃 turn 重投影、历史 turn 跳过）协同正确，无重复/丢失。
- **A/B 组解耦**：A 组不动 `ChatPanel` 渲染骨架、不引新依赖、不碰 store 展开态；B 组的虚拟滚动无需等待 A 组，但 B 组落地时 A 组的 `streaming` 信号已是稳定契约。
- **零新依赖**：本期所有优化均手写，不引入任何 npm 包。

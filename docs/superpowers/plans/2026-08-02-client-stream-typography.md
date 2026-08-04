# 客户端流式显示与排版优化（A 组）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不引入任何新依赖的前提下，统一客户端消息排版 token、为流式正文/思考块渲染稳定 caret、流式期代码块可折叠、思考块流式实时可见。

**Architecture:** 流式「块级 streaming 信号」由 `services/timeline/projector.ts` 投影为 `TurnTimelineEntry.streaming?: boolean`（pending 块为 true），经 `TurnTimeline` 透传给 `AgentMessage`/`ThinkingBlock`；caret 用 react-markdown 块级组件自定义渲染 + `isLastLeaf` 末节点探测注入；排版统一走新增 `messageTypography.ts` 常量。A 组不触碰 `ChatPanel` 渲染骨架、不引 `@tanstack/react-virtual`、不动 store 展开态（归 B 组）。

**Tech Stack:** React + TypeScript + Tailwind CSS（v3，字符级 class 如 `text-[13px]`）+ vitest + @testing-library/react + jsdom。零新依赖。

## Global Constraints

- 零新依赖：caret / 折叠 / token 全手写，基于既有 `tailwindcss` + `lucide-react` + shadcn 原语。
- 不引入任何 npm 包（A 组）。
- 排版 token 单一事实来源：`src/components/chat/messageTypography.ts`，禁止各组件再写 `text-sm leading-relaxed`。
- caret 加 `aria-hidden`，不参与屏幕阅读器朗读。
- 代码块折叠阈值：`CODE_FOLD_LINES = 12`、`CODE_FOLD_CHARS = 600`（模块常量，可配）。
- projector 循环结束后把 pending 块作为 `streaming: true` 末尾 entry 时，**不清除 pending**，保证下次重投影从累积态继续；结束信号（`run_finished`/`run_failed`/`run_cancelled`）到达时 flush 为 `streaming: false`。
- 每个任务结束独立可测，频繁 commit；改动遵循项目 docstring 规范（中文四段式）。

---

## 文件结构

| 文件 | 责任 | 操作 |
|---|---|---|
| `src/components/chat/messageTypography.ts` | 排版 token 单一事实来源 | 新建 |
| `src/services/timeline/projector.ts` | 投影 `TurnTimelineEntry`，新增 `streaming?: boolean` 语义 | 修改 |
| `src/components/layout/TurnTimeline.tsx` | 把 `entry.streaming` 透传 `AgentMessage`/`ThinkingBlock` | 修改 |
| `src/components/chat/AgentMessage.tsx` | 复用 token；`streaming` prop；末节点探测 caret | 修改 |
| `src/components/chat/ThinkingBlock.tsx` | `streaming` prop；流式期直接展开内容+caret | 修改 |
| `src/components/chat/CodeBlock.tsx` | `streaming` prop；超阈值折叠 | 修改 |
| `src/components/chat/UserMessage.tsx` | 复用 `MessageTypography.body` | 修改 |
| `src/tests/timelineProjector.test.ts` | 扩展 streaming 投影断言 | 修改 |
| `src/tests/agentMessage.test.tsx` | 新增：caret 渲染 + 末节点探测 | 新建 |
| `src/tests/thinkingBlock.test.tsx` | 新增：流式/非流式行为 | 新建 |
| `src/tests/codeBlock.test.tsx` | 新增：流式折叠 + break-words | 新建 |

---

### Task 1: 新增 MessageTypography 排版 token

**Files:**
- Create: `src/components/chat/messageTypography.ts`

**Interfaces:**
- Produces: `MessageTypography` 常量对象（`body`/`secondary`/`code`/`caret` 四个字符串字段），后续任务直接 `import { MessageTypography }`。

- [ ] **Step 1: 创建 token 文件**

```ts
// 消息排版 token：所有消息组件复用，禁止各写一套 text-sm leading-relaxed
export const MessageTypography = {
  body: "text-[13px] leading-7", // 正文：介于 sm(14) 与 xs(12) 之间，行距 28px 透气
  secondary: "text-xs leading-5", // 思考块 / 代码块 / 元信息：稳定次要层级
  code: "text-xs leading-5 font-mono", // 等宽代码
  caret: "inline-block w-[1px] h-[1em] align-text-bottom animate-pulse bg-foreground/70", // 流式光标
} as const;
```

- [ ] **Step 2: 类型检查确认无错**

Run: `cd apps/desktop && npx tsc --noEmit`
Expected: 零错误（纯导出常量，无消费方亦合法）。

- [ ] **Step 3: Commit**

```bash
git add apps/desktop/src/components/chat/messageTypography.ts
git commit -m "feat(chat): 新增 MessageTypography 排版 token 单一事实来源"
```

---

### Task 2: projector 增加 streaming 字段与投影语义

**Files:**
- Modify: `src/services/timeline/projector.ts`（`TurnTimelineEntry` 类型 + `projectEntries` 逻辑）
- Test: `src/tests/timelineProjector.test.ts`

**Interfaces:**
- Consumes: 现有 `projectTurnTimeline` / `projectEntries` 签名不变。
- Produces: `TurnTimelineEntry` 的 `assistant` / `thinking` 变体新增 `streaming?: boolean`；`TurnTimeline`（Task 3）与组件（Task 4/5）读取该字段。

- [ ] **Step 1: 写失败测试（扩展 timelineProjector.test.ts）**

在现有测试文件末尾追加（沿用现有 import 与 `RuntimeEvent` 构造方式；若现有测试用 helper 构造 event，复用之）：

```ts
describe("streaming 投影", () => {
  it("进行中 assistant 块投影为 streaming:true", () => {
    const events = [
      { event_type: "model_output_delta", event_id: "d1", turn_id: "t1", payload: { text: "Hello" } },
      { event_type: "model_output_delta", event_id: "d2", turn_id: "t1", payload: { text: " World" } },
    ] as unknown as RuntimeEvent[];
    const [item] = projectTurnTimeline([{ turn_id: "t1", task_id: "k", input_text: "x", status: "running", end_reason: null, response_text: null, created_at: "", updated_at: "" } as TurnRecord], events);
    const last = item.entries[item.entries.length - 1];
    expect(last.kind).toBe("assistant");
    if (last.kind === "assistant") {
      expect(last.streaming).toBe(true);
      expect(last.content).toBe("Hello World");
    }
  });

  it("delta 被工具事件中断后 flush 的块 streaming 为假", () => {
    const events = [
      { event_type: "model_output_delta", event_id: "d1", turn_id: "t1", payload: { text: "Hi" } },
      { event_type: "tool_call_started", event_id: "tc1", turn_id: "t1", payload: { tool_name: "read_file", tool_call_id: "c1", arguments: {} } },
    ] as unknown as RuntimeEvent[];
    const [item] = projectTurnTimeline([{ turn_id: "t1", task_id: "k", input_text: "x", status: "running", end_reason: null, response_text: null, created_at: "", updated_at: "" } as TurnRecord], events);
    const assistant = item.entries.find((e) => e.kind === "assistant");
    expect(assistant?.kind).toBe("assistant");
    if (assistant?.kind === "assistant") expect(assistant.streaming).not.toBe(true);
  });

  it("run_finished 后无悬空 streaming 块", () => {
    const events = [
      { event_type: "model_output_delta", event_id: "d1", turn_id: "t1", payload: { text: "Done" } },
      { event_type: "run_finished", event_id: "rf", turn_id: "t1", payload: {} },
    ] as unknown as RuntimeEvent[];
    const [item] = projectTurnTimeline([{ turn_id: "t1", task_id: "k", input_text: "x", status: "running", end_reason: null, response_text: null, created_at: "", updated_at: "" } as TurnRecord], events);
    const hasStreaming = item.entries.some((e) => (e.kind === "assistant" || e.kind === "thinking") && e.streaming === true);
    expect(hasStreaming).toBe(false);
  });

  it("思考/回答交错时各自 streaming 标志正确", () => {
    const events = [
      { event_type: "model_thinking_delta", event_id: "th1", turn_id: "t1", payload: { text: "想" } },
      { event_type: "model_output_delta", event_id: "d1", turn_id: "t1", payload: { text: "答" } },
    ] as unknown as RuntimeEvent[];
    const [item] = projectTurnTimeline([{ turn_id: "t1", task_id: "k", input_text: "x", status: "running", end_reason: null, response_text: null, created_at: "", updated_at: "" } as TurnRecord], events);
    const thinking = item.entries.find((e) => e.kind === "thinking");
    const assistant = item.entries.find((e) => e.kind === "assistant");
    if (thinking?.kind === "thinking") expect(thinking.streaming).not.toBe(true); // 思考已 flush
    if (assistant?.kind === "assistant") expect(assistant.streaming).toBe(true); // 回答仍进行
  });
});
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd apps/desktop && npx vitest run src/tests/timelineProjector.test.ts`
Expected: 新增用例 FAIL（类型错误：`streaming` 不存在 / 断言 `streaming` 为 true 失败）。

- [ ] **Step 3: 扩展类型与投影逻辑**

修改 `TurnTimelineEntry`（约 67-72 行）：

```ts
export type TurnTimelineEntry =
  | { kind: "assistant"; eventId: string; content: string; streaming?: boolean }
  | { kind: "thinking"; eventId: string; content: string; streaming?: boolean }
  | { kind: "tool"; item: TimelineToolItem }
  | { kind: "status"; eventId: string; eventType: RuntimeEvent["event_type"]; payload: RuntimeEvent["payload"] };
```

修改 `projectEntries`：
- 在 `flushPending` 中，已 flush 的块不设置 `streaming`（保持默认 undefined/false）。
- 循环结束后（现有 `flushPending()` 之后），新增：若 `pendingDelta` 仍非空，push `{ kind: "assistant", eventId: pendingDelta.eventId, content: pendingDelta.content, streaming: true }`（**不清除 `pendingDelta`**）；若 `pendingThinking` 仍非空且 `trim().length > 0`，push `{ kind: "thinking", eventId: pendingThinking.eventId, content: pendingThinking.content, streaming: true }`（不清除）。
- 在 `run_finished`/`run_failed`/`run_cancelled` 分支内，先调用一次 flush（让 pending 块以 `streaming` 假落地），再 push status 条目。注意：此时 flush 出的块不带 `streaming`，符合「结束信号无悬空 streaming 块」。

> 实现要点：把原末尾 `flushPending()` 替换为「flush 已完成块（streaming 假）+ 输出进行中块（streaming 真）」两步。可新增内部函数 `flushPendingFinal()` 区分两者。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd apps/desktop && npx vitest run src/tests/timelineProjector.test.ts`
Expected: 全部 PASS（含原有用例 + 新增 4 例）。

- [ ] **Step 5: Commit**

```bash
git add apps/desktop/src/services/timeline/projector.ts apps/desktop/src/tests/timelineProjector.test.ts
git commit -m "feat(timeline): projector 投影块级 streaming 信号"
```

---

### Task 3: TurnTimeline 透传 streaming

**Files:**
- Modify: `src/components/layout/TurnTimeline.tsx`（约 79-99 行）

**Interfaces:**
- Consumes: `entry.streaming`（Task 2 产出）。
- Produces: `AgentMessage` 接收 `streaming` prop（Task 4 实现该 prop）；`ThinkingBlock` 接收 `streaming` prop（Task 5 实现）。

- [ ] **Step 1: 透传 assistant 与 thinking 的 streaming**

修改 `entry.kind === "thinking"` 分支（约 81 行）：

```tsx
<ThinkingBlock content={entry.content} streaming={entry.streaming} />
```

修改 `entry.kind === "assistant"` 分支（约 97 行）：

```tsx
<AgentMessage content={entry.content} streaming={entry.streaming} />
```

- [ ] **Step 2: 类型检查确认无错**

Run: `cd apps/desktop && npx tsc --noEmit`
Expected: 当前 `AgentMessage`/`ThinkingBlock` 尚未声明 `streaming` prop，会报类型错误——属预期。该错误在 Task 4/5 实现 prop 后消除。若希望本任务独立可编译，可临时给两组件加 `streaming?: boolean` 可选 prop（空实现），但更干净的做法是 Task 4/5 紧随其后一并消除。此处仅确认修改结构正确。

- [ ] **Step 3: Commit（与 Task 4/5 合并 commit 亦可，但本任务先单独提交透传骨架）**

```bash
git add apps/desktop/src/components/layout/TurnTimeline.tsx
git commit -m "feat(timeline): TurnTimeline 透传 entry.streaming 给消息组件"
```

---

### Task 4: AgentMessage streaming caret（末节点探测）

**Files:**
- Modify: `src/components/chat/AgentMessage.tsx`
- Create: `src/tests/agentMessage.test.tsx`

**Interfaces:**
- Consumes: `MessageTypography`（Task 1）；`streaming?: boolean` prop（来自 TurnTimeline / Task 3）。
- Produces: 渲染 caret span（`MessageTypography.caret` + `aria-hidden`），仅当 `streaming===true && content.trim().length>0 && 为最后一个叶子节点`。

- [ ] **Step 1: 写失败测试**

```tsx
import { render, screen } from "@testing-library/react";
import { AgentMessage } from "@/components/chat/AgentMessage";

describe("AgentMessage streaming caret", () => {
  it("streaming=true 且内容非空时渲染 caret", () => {
    const { container } = render(<AgentMessage content="hello world" streaming={true} />);
    const caret = container.querySelector('[aria-hidden="true"].animate-pulse');
    expect(caret).not.toBeNull();
  });

  it("streaming=false 时不渲染 caret", () => {
    const { container } = render(<AgentMessage content="hello world" streaming={false} />);
    const caret = container.querySelector('[aria-hidden="true"].animate-pulse');
    expect(caret).toBeNull();
  });

  it("caret 位于以段落结尾 markdown 的末块内", () => {
    const { container } = render(<AgentMessage content={"第一行\n\n第二行"} streaming={true} />);
    const caret = container.querySelector('[aria-hidden="true"].animate-pulse');
    expect(caret).not.toBeNull();
    // 末块应为最后一个 <p>
    const lastP = container.querySelectorAll("p");
    expect(lastP.length).toBeGreaterThan(0);
    expect(lastP[lastP.length - 1].contains(caret)).toBe(true);
  });

  it("caret 位于以代码块结尾 markdown 的末块内", () => {
    const md = "说明文字\n\n```ts\nconst a = 1;\n```";
    const { container } = render(<AgentMessage content={md} streaming={true} />);
    const caret = container.querySelector('[aria-hidden="true"].animate-pulse');
    expect(caret).not.toBeNull();
    const pre = container.querySelector("pre");
    expect(pre).not.toBeNull();
    expect(pre!.contains(caret)).toBe(true);
  });

  it("正文容器含 min-h / min-w 防塌缩类", () => {
    const { container } = render(<AgentMessage content="hi" streaming={true} />);
    const body = container.querySelector(".min-h-\\[1\\.5em\\]");
    const mw = container.querySelector(".min-w-0");
    expect(body).not.toBeNull();
    expect(mw).not.toBeNull();
  });
});
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd apps/desktop && npx vitest run src/tests/agentMessage.test.tsx`
Expected: FAIL（`AgentMessage` 无 `streaming` prop / caret 未渲染）。

- [ ] **Step 3: 实现末节点探测 + caret**

修改 `AgentMessage.tsx`：

1. 新增 prop：

```ts
interface AgentMessageProps {
  content: string;
  className?: string;
  /** 是否正在流式生成中（来自 projector 的块级 streaming 信号）。 */
  streaming?: boolean;
}
```

2. 预先算「最后一个块级标签」：用轻量正则从 `content` 提取末尾块级元素类型（匹配最后出现的 `\`\`\`lang` 围栏 → `pre`；否则 `p`）。实现 `lastBlockTag(content): "pre" | "p"`（列表/引用/标题在本期简化为归入 `p` 处理，caret 落在最近块级容器即可；如后续需要可扩展）。

3. 改造 `markdownComponents`，对 `p` 与 `code` 覆盖加 `isLastLeaf` 判断：

```tsx
const lastTag = lastBlockTag(content);
const markdownComponents: Components = {
  code({ className, children, ...props }) {
    const match = /language-(\w+)/.exec(className || "");
    const language = match ? match[1] : undefined;
    const code = String(children).replace(/\n$/, "");
    if (!language && FILE_PATH_PATTERN.test(code)) return <FileLink path={code} />;
    if (language || code.includes("\n")) {
      const isLast = lastTag === "pre";
      return (
        <CodeBlock
          code={code}
          language={language}
          streaming={streaming}
          isLastLeaf={isLast}
        />
      );
    }
    return <code className={cn("rounded bg-muted px-1 py-0.5 font-mono text-xs", className)} {...props}>{children}</code>;
  },
  p({ children }) {
    const text = typeof children === "string" ? children : "";
    if (!text.trim()) return null;
    const isLast = lastTag === "p";
    return (
      <p className="whitespace-pre-wrap">
        {children}
        {streaming && isLast && content.trim().length > 0 ? (
          <span className={cn(MessageTypography.caret, "ml-0.5")} aria-hidden />
        ) : null}
      </p>
    );
  },
};
```

4. 正文容器类名改为 `MessageTypography.body`，并加 `min-h-[1.5em] min-w-0`：

```tsx
<div className={cn("max-w-[85%] space-y-2 rounded-lg rounded-bl-sm bg-muted/50 px-4 py-2.5", MessageTypography.body, "min-h-[1.5em] min-w-0", className)}>
```

> 注意：`CodeBlock` 的 `isLastLeaf` + `streaming`（Task 6 实现）负责在代码块末块末尾追加 caret；`p` 分支在本任务内联处理 caret。若 `lastTag === "pre"`，则 `p` 分支 `isLast` 为 false，caret 由 CodeBlock 渲染。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd apps/desktop && npx vitest run src/tests/agentMessage.test.tsx`
Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add apps/desktop/src/components/chat/AgentMessage.tsx apps/desktop/src/tests/agentMessage.test.tsx
git commit -m "feat(chat): AgentMessage 末节点探测渲染流式 caret"
```

---

### Task 5: ThinkingBlock streaming 行为

**Files:**
- Modify: `src/components/chat/ThinkingBlock.tsx`
- Create: `src/tests/thinkingBlock.test.tsx`

**Interfaces:**
- Consumes: `MessageTypography`（Task 1）；`streaming?: boolean` prop（Task 3 透传）。
- Produces: 无（仅消费）。

- [ ] **Step 1: 写失败测试**

```tsx
import { render, screen, fireEvent } from "@testing-library/react";
import { ThinkingBlock } from "@/components/chat/ThinkingBlock";

describe("ThinkingBlock streaming", () => {
  it("streaming=true 时直接渲染累积内容 + caret，不显示折叠栏", () => {
    const { container, queryByText } = render(<ThinkingBlock content="正在思考..." streaming={true} />);
    expect(queryByText("深度思考")).toBeNull(); // 折叠栏按钮不渲染
    expect(container.textContent).toContain("正在思考...");
    expect(container.querySelector('[aria-hidden="true"].animate-pulse')).not.toBeNull();
  });

  it("streaming=false 时维持折叠交互（默认不展开）", () => {
    const { queryByText, getByText } = render(<ThinkingBlock content="完整思考" streaming={false} />);
    expect(queryByText("完整思考")).toBeNull(); // 默认折叠，内容不可见
    fireEvent.click(getByText("深度思考")); // 点击展开
    expect(queryByText("完整思考")).not.toBeNull();
  });
});
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd apps/desktop && npx vitest run src/tests/thinkingBlock.test.tsx`
Expected: FAIL（`ThinkingBlock` 无 `streaming` prop / 折叠栏始终渲染）。

- [ ] **Step 3: 实现 streaming 分支**

修改 `ThinkingBlock.tsx`：

1. 新增 prop：

```ts
interface ThinkingBlockProps {
  content: string;
  className?: string;
  /** 是否正在流式生成中；true 时直接展开累积内容 + caret，不显示折叠栏。 */
  streaming?: boolean;
}
```

2. 组件逻辑：

```tsx
export function ThinkingBlock({ content, className, streaming }: ThinkingBlockProps) {
  const [expanded, setExpanded] = useState(false);

  if (!content || content.trim().length === 0) return null;

  // 流式期：直接展开累积内容 + caret，不显示折叠栏，避免抖动
  if (streaming) {
    return (
      <div className={cn("w-full", className)}>
        <div className={cn("mt-2 rounded-lg border border-muted bg-muted/50 px-3 py-2.5 whitespace-pre-wrap", MessageTypography.secondary, "text-muted-foreground")}>
          {content}
          <span className={cn(MessageTypography.caret, "ml-0.5")} aria-hidden />
        </div>
      </div>
    );
  }

  // 非流式：维持原有折叠交互
  return (
    <div className={cn("w-full", className)}>
      <button type="button" onClick={() => setExpanded((p) => !p)} className="flex cursor-pointer items-center gap-1 text-sm text-muted-foreground hover:text-foreground transition-colors">
        <ChevronRight className={cn("h-4 w-4 transition-transform duration-200", expanded && "rotate-90")} />
        <span>深度思考</span>
      </button>
      {expanded ? (
        <div className="mt-2 rounded-lg border border-muted bg-muted/50 px-3 py-2.5 text-xs leading-relaxed text-muted-foreground">
          <div className="whitespace-pre-wrap">{content}</div>
        </div>
      ) : null}
    </div>
  );
}
```

> 保留原 `useEffect` 调试日志（如有）可按项目约定精简，不强制删除；本任务以行为正确为准。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd apps/desktop && npx vitest run src/tests/thinkingBlock.test.tsx`
Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add apps/desktop/src/components/chat/ThinkingBlock.tsx apps/desktop/src/tests/thinkingBlock.test.tsx
git commit -m "feat(chat): ThinkingBlock 流式期直接展开内容 + caret"
```

---

### Task 6: CodeBlock streaming 折叠

**Files:**
- Modify: `src/components/chat/CodeBlock.tsx`
- Create: `src/tests/codeBlock.test.tsx`

**Interfaces:**
- Consumes: `MessageTypography`（Task 1）；`streaming?: boolean` + `isLastLeaf?: boolean`（Task 4 传入）。
- Produces: 超阈值时渲染「展开完整代码」按钮 + 折叠容器；行内 `break-words`。

- [ ] **Step 1: 写失败测试**

```tsx
import { render, screen, fireEvent } from "@testing-library/react";
import { CodeBlock } from "@/components/chat/CodeBlock";

const LONG = Array.from({ length: 20 }, (_, i) => `line ${i}`).join("\n"); // 20 行 > 12

describe("CodeBlock streaming 折叠", () => {
  it("streaming=true 且超阈值时渲染展开按钮并折叠", () => {
    const { container } = render(<CodeBlock code={LONG} language="ts" streaming={true} isLastLeaf={true} />);
    expect(screen.getByText("展开完整代码")).not.toBeNull();
    expect(container.querySelector(".max-h-48")).not.toBeNull();
  });

  it("同内容 streaming=false 时不折叠", () => {
    render(<CodeBlock code={LONG} language="ts" streaming={false} isLastLeaf={true} />);
    expect(screen.queryByText("展开完整代码")).toBeNull();
  });

  it("行内超长路径含 break-words", () => {
    const { container } = render(<CodeBlock code={"a".repeat(200)} language="ts" streaming={false} />);
    expect(container.querySelector(".break-words")).not.toBeNull();
  });

  it("streaming=true 且末块时在末尾渲染 caret", () => {
    const { container } = render(<CodeBlock code={LONG} language="ts" streaming={true} isLastLeaf={true} />);
    expect(container.querySelector('[aria-hidden="true"].animate-pulse')).not.toBeNull();
  });
});
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd apps/desktop && npx vitest run src/tests/codeBlock.test.tsx`
Expected: FAIL（`CodeBlock` 无 `streaming`/`isLastLeaf` prop / 按钮未渲染）。

- [ ] **Step 3: 实现折叠 + caret**

修改 `CodeBlock.tsx`：

1. 新增 props 与常量：

```ts
const CODE_FOLD_LINES = 12;
const CODE_FOLD_CHARS = 600;

interface CodeBlockProps {
  code: string;
  language?: string;
  className?: string;
  /** 是否正在流式生成中（来自 projector 块级信号）。 */
  streaming?: boolean;
  /** 是否为 markdown 最后一个叶子节点（决定是否在末尾渲染 caret）。 */
  isLastLeaf?: boolean;
}
```

2. 组件内：

```tsx
export function CodeBlock({ code, language, className, streaming, isLastLeaf }: CodeBlockProps) {
  const [copied, setCopied] = useState(false);
  const [expanded, setExpanded] = useState(false);

  const lineCount = code.split("\n").length;
  const shouldFold = streaming === true && (lineCount > CODE_FOLD_LINES || code.length > CODE_FOLD_CHARS);
  const folded = shouldFold && !expanded;

  // handleCopy 保持原样 ...

  return (
    <div className={cn("group relative overflow-hidden rounded-md border border-border bg-slate-950 text-sm", className)}>
      <div className="flex items-center justify-between border-b border-border/20 bg-slate-900 px-3 py-1.5">
        <span className="text-xs text-slate-400">{language ?? "text"}</span>
        <Button variant="ghost" size="icon" className="h-6 w-6 opacity-0 transition-opacity group-hover:opacity-100" onClick={handleCopy}>
          {copied ? <Check className="h-3.5 w-3.5 text-emerald-400" /> : <Copy className="h-3.5 w-3.5 text-slate-400" />}
        </Button>
      </div>

      <pre className={cn("overflow-x-auto p-3", folded && "max-h-48")}>
        <code className={cn("font-mono text-xs leading-relaxed text-slate-300 break-words", MessageTypography.code)}>{code}</code>
        {streaming && isLastLeaf ? <span className={cn(MessageTypography.caret, "ml-0.5 align-text-bottom")} aria-hidden /> : null}
      </pre>

      {folded ? (
        <button type="button" onClick={() => setExpanded(true)} className="w-full border-t border-border/20 bg-slate-900 py-1.5 text-xs text-slate-400 hover:text-slate-200">
          展开完整代码
        </button>
      ) : null}
    </div>
  );
}
```

> `break-words` 始终作用于 `<code>`（既防撑破又无害）；折叠仅流式期触发。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd apps/desktop && npx vitest run src/tests/codeBlock.test.tsx`
Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add apps/desktop/src/components/chat/CodeBlock.tsx apps/desktop/src/tests/codeBlock.test.tsx
git commit -m "feat(chat): CodeBlock 流式期超阈值折叠 + 末块 caret"
```

---

### Task 7: UserMessage 复用排版 token

**Files:**
- Modify: `src/components/chat/UserMessage.tsx`

**Interfaces:**
- Consumes: `MessageTypography`（Task 1）。
- Produces: 无。

- [ ] **Step 1: 复用 body token**

修改 `UserMessage.tsx` 气泡内层 div（约 33 行）：

```tsx
import { MessageTypography } from "./messageTypography";

// ...
<div className={cn("max-w-[85%] rounded-lg rounded-br-sm bg-primary px-4 py-2.5 text-primary-foreground shadow-sm", MessageTypography.body)}>
  <p className="whitespace-pre-wrap">{trimmed}</p>
</div>
```

- [ ] **Step 2: 类型检查确认无错**

Run: `cd apps/desktop && npx tsc --noEmit`
Expected: 零错误。

- [ ] **Step 3: Commit**

```bash
git add apps/desktop/src/components/chat/UserMessage.tsx
git commit -m "feat(chat): UserMessage 复用 MessageTypography.body 排版 token"
```

---

### Task 8: 全量验证与闭环

**Files:** 无新增；验证现有改动。

- [ ] **Step 1: 类型检查全量**

Run: `cd apps/desktop && npx tsc --noEmit`
Expected: 零错误。

- [ ] **Step 2: 测试全量**

Run: `cd apps/desktop && npx vitest run`
Expected: 全部 PASS（含 Task 2/4/5/6 新增用例与原有用例）。

- [ ] **Step 3: 独立审查 Agent**

按 `AGENTS.md` 第八节，调用独立审查 Agent 静态复查：分层（projector 投影 / 组件消费边界）、单一职责、无死代码、docstring 四段式、零新依赖。
Expected: 输出通过/不通过结论，无必改项。

- [ ] **Step 4: 独立测试 Agent**

调用独立测试 Agent 独立执行测试，覆盖业务与边界场景（streaming 三态、折叠阈值边界、末节点探测两类 markdown）。
Expected: 输出通过/不通过结论。

- [ ] **Step 5: 视觉自查（手动，非阻塞）**

Run: 启动桌面端，发送指令观察流式正文 caret 闪烁、思考块实时可见、代码块流式期可折叠、字号行距统一。
Expected: 观感符合 spec 目标。

---

## 自检摘要（writing-plans 自审）

- **Spec 覆盖**：§1 范围 8 项 → Task 1-7 一一对应；§2 token/caret → Task 1+4；§3 projector streaming → Task 2+3；§4 thinking/code → Task 5+6；§5 UserMessage → Task 7；§6 测试 → 各 Task 测试步 + Task 8；§7 验证闭环 → Task 8。
- **占位符扫描**：无 TBD/TODO/「类似 Task N」；每个代码步含实际代码。
- **类型一致性**：`streaming?: boolean` 在 projector 类型、TurnTimeline 透传、AgentMessage/ThinkingBlock/CodeBlock props 全程一致；`isLastLeaf?: boolean` 在 AgentMessage→CodeBlock 签名一致；`MessageTypography` 字段名（body/secondary/code/caret）跨任务一致。
- **遗漏检查**：`prefers-reduced-motion` caret 降级在 spec §2.3 标注为「后续细节」，计划未强制作任务（不阻塞主流程），符合 spec 表述。

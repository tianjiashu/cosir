/**
 * Agent 输出消息组件。
 *
 * 使用 react-markdown 渲染 Markdown 内容，代码块和文件路径分别委托给
 * CodeBlock / FileLink 组件做富展示，其余 Markdown（标题、列表、表格、
 * 加粗/斜体/链接等）由 react-markdown + remark-gfm 原生渲染。
 *
 * 流式期（`streaming`）在「内容的最后一个块级节点」内部渲染光标，
 * 使光标随文字自然流动，而非固定悬挂在消息末尾。
 *
 * @module components/chat/AgentMessage
 */

import { useMemo } from "react";
import type { Components } from "react-markdown";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { cn } from "@/lib/utils";
import { CodeBlock } from "./CodeBlock";
import { FileLink } from "./FileLink";
import { MessageTypography } from "./messageTypography";
import { StreamingCaret } from "./StreamingCaret";

/** Agent 消息组件属性。 */
interface AgentMessageProps {
  /** 消息文本内容（Markdown 格式）。 */
  content: string;
  /** 是否正在流式生成中（来自 projector 的块级 streaming 信号）。 */
  streaming?: boolean;
  /** 可选的额外 CSS 类名。 */
  className?: string;
}

/**
 * 内联文件路径匹配正则，用于在 react-markdown 的 `code`（行内代码）节点中
 * 识别文件路径并替换为 FileLink 组件。
 *
 * 匹配 `` `docs/xxx.md` `` 或 `` `src/xxx.ts` `` 等格式。
 */
const FILE_PATH_PATTERN = /^([a-zA-Z0-9_/.-]+\.(?:md|ts|tsx|py|json|yaml|yml|toml|css|html))$/;

/** 内容以「围栏代码块」结尾的匹配：允许结尾残留空白。 */
const TRAILING_FENCED_CODE_PATTERN = /```[^\n]*\n[\s\S]*?```\s*$/;

/**
 * 探测 Markdown 内容的最后一个块级节点类型。
 *
 * 目的:
 *   决定流式光标应落在哪个块容器内。仅区分「代码块」与「其它块」两类，
 *   因为只有代码块的渲染容器（CodeBlock/<pre>）与段落差异足够大，
 *   需要分别注入光标；列表 / 引用 / 标题当前统一按段落语义处理。
 *
 * 参数:
 *   content - 完整的 Markdown 文本。
 *
 * 返回:
 *   "pre" 表示内容以围栏代码块结尾；否则返回 "p"。
 *
 * 异常:
 *   不抛出异常。
 *
 * 副作用:
 *   无（纯函数）。
 */
export function lastBlockTag(content: string): "pre" | "p" {
  return TRAILING_FENCED_CODE_PATTERN.test(content) ? "pre" : "p";
}

/**
 * 计算内容中最后一个块级节点的起始行号（1-based）。
 *
 * 目的:
 *   react-markdown 会为每个 mdast 节点带上源码 position，据此判断
 *   「当前渲染的段落是不是最后一个段落」，从而只给末段落挂光标。
 *   以「最后一个非空行所属的连续文本块的首行」作为末块起始行。
 *
 * 参数:
 *   content - 完整的 Markdown 文本。
 *
 * 返回:
 *   末块起始行号；内容为空时返回 1。
 *
 * 异常:
 *   不抛出异常。
 *
 * 副作用:
 *   无（纯函数）。
 */
function lastBlockStartLine(content: string): number {
  const lines = content.split("\n");
  let end = lines.length - 1;
  while (end >= 0 && lines[end].trim() === "") end -= 1;
  if (end < 0) return 1;
  let start = end;
  while (start > 0 && lines[start - 1].trim() !== "") start -= 1;
  return start + 1;
}

/**
 * 构建 react-markdown 的自定义组件映射。
 *
 * 目的:
 *   把「是否流式」「末块类型」这两个随消息变化的状态注入到 markdown 渲染中，
 *   使光标能被渲染进最后一个块级节点内部。
 *
 * 参数:
 *   streaming - 是否处于流式生成中。
 *   lastTag - `lastBlockTag` 探测出的末块类型。
 *   hasContent - 内容去空白后是否非空（空内容不渲染光标）。
 *   lastLine - `lastBlockStartLine` 算出的末块起始行号。
 *
 * 返回:
 *   react-markdown 的 `Components` 映射对象。
 *
 * 异常:
 *   不抛出异常。
 *
 * 副作用:
 *   无（返回的组件本身为纯渲染）。
 */
function buildMarkdownComponents(
  streaming: boolean,
  lastTag: "pre" | "p",
  hasContent: boolean,
  lastLine: number,
): Components {
  const caretInCode = streaming && hasContent && lastTag === "pre";
  const caretInParagraph = streaming && hasContent && lastTag === "p";

  return {
    /** 代码块 → CodeBlock 富展示。 */
    code({ className, children, ...props }) {
      const match = /language-(\w+)/.exec(className || "");
      const language = match ? match[1] : undefined;
      const code = String(children).replace(/\n$/, "");

      // 行内代码且匹配文件路径 → FileLink
      if (!language && FILE_PATH_PATTERN.test(code)) {
        return <FileLink path={code} />;
      }

      // 代码块（有语言标记或多行）→ CodeBlock
      if (language || code.includes("\n")) {
        return <CodeBlock code={code} language={language} streaming={streaming} isLastLeaf={caretInCode} />;
      }

      // 普通行内代码 → <code> 标签
      return (
        <code className={cn("rounded bg-muted px-1 py-0.5 font-mono text-xs", className)} {...props}>
          {children}
        </code>
      );
    },
    /** 段落：去除末尾空 <p> 包裹纯空白的情况；流式末段落追加光标。 */
    p({ children, node }) {
      const text = typeof children === "string" ? children : "";
      if (typeof children === "string" && !text.trim()) return null;
      // 仅「源码行号等于末块起始行」的段落挂 caret，避免多段落时每段都挂。
      const isLast = node?.position?.start.line === lastLine;
      return (
        <p className="whitespace-pre-wrap">
          {children}
          <StreamingCaret show={caretInParagraph && isLast} />
        </p>
      );
    },
  };
}

/**
 * AgentMessage Agent 输出消息组件。
 *
 * 目的:
 *   将消息内容通过 react-markdown 完整渲染为 Markdown，其中：
 *   - 代码块（```lang ... ```）：使用 CodeBlock 组件
 *   - 文件路径内联代码（`path/to/file.ext`）：使用 FileLink 组件
 *   - 其余 Markdown 元素（标题/列表/表格/加粗/链接等）：由 remark-gfm 原生支持
 *   流式期在最后一个块级节点内部渲染光标。
 *
 * 参数:
 *   content - Markdown 消息内容。
 *   streaming - 是否正在流式生成中。
 *   className - 额外 CSS 类名。
 *
 * 返回:
 *   消息气泡的 React 元素。
 *
 * 异常:
 *   不抛出异常。
 *
 * 副作用:
 *   无。
 */
export function AgentMessage({ content, streaming = false, className }: AgentMessageProps) {
  const hasContent = content.trim().length > 0;
  const lastTag = useMemo(() => lastBlockTag(content), [content]);
  const lastLine = useMemo(() => lastBlockStartLine(content), [content]);
  const components = useMemo(
    () => buildMarkdownComponents(streaming, lastTag, hasContent, lastLine),
    [streaming, lastTag, hasContent, lastLine],
  );

  return (
    <div className={cn("flex justify-start", className)}>
      <div
        className={cn(
          "max-w-[85%] space-y-2 rounded-lg rounded-bl-sm bg-muted/50 px-4 py-2.5",
          MessageTypography.body,
          "min-h-[1.5em] min-w-0",
        )}
      >
        <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
          {content}
        </ReactMarkdown>
      </div>
    </div>
  );
}

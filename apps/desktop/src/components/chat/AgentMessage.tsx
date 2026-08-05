/**
 * Agent 输出消息组件。
 *
 * 使用 react-markdown 渲染 Markdown 内容，代码块和文件路径分别委托给
 * CodeBlock / FileLink 组件做富展示，其余 Markdown（标题、列表、表格、
 * 加粗/斜体/链接等）由 react-markdown + remark-gfm 原生渲染。
 *
 * 流式期（`streaming`）统一由 MarkdownStream 的 caret 层渲染光标，
 * 使光标随文字自然流动，单一光标来源避免与代码块内部光标重复。
 *
 * @module components/chat/AgentMessage
 */

import { memo, useMemo } from "react";
import type { Components } from "react-markdown";

import { cn } from "@/lib/utils";
import { CodeLine } from "@/components/ui/tokens";
import { CodeBlock } from "./CodeBlock";
import { FileLink } from "./FileLink";
import { MessageTypography } from "./messageTypography";
import { MarkdownStream } from "./MarkdownStream";
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

/**
 * 构建 react-markdown 的自定义组件映射。
 *
 * 目的:
 *   把「是否流式」状态注入到 markdown 渲染中，使代码块能感知流式态以调整渲染行为。
 *   流式光标统一由 MarkdownStream 的 caret 层承担（单一光标来源），不在此处注入。
 *
 * 参数:
 *   streaming - 是否处于流式生成中。
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
function buildMarkdownComponents(streaming: boolean): Components {
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

      // 代码块（有语言标记或多行）→ CodeBlock。
      // 流式光标统一由 MarkdownStream 的 caret 层承担（单一光标来源），
      // 故此处 isLastLeaf 恒传 false，避免与外层 caret 形成双光标。
      if (language || code.includes("\n")) {
        return <CodeBlock code={code} language={language} streaming={streaming} isLastLeaf={false} />;
      }

      // 普通行内代码 → <code> 标签
      return (
        <code className={cn("rounded bg-muted px-1 py-0.5 font-mono text-xs", className)} {...props}>
          {children}
        </code>
      );
    },
    /** 段落：去除末尾空 <p> 包裹纯空白的情况。 */
    p({ children }) {
      const text = typeof children === "string" ? children : "";
      if (typeof children === "string" && !text.trim()) return null;
      return <p className="whitespace-pre-wrap">{children}</p>;
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
export const AgentMessage = memo(function AgentMessage({ content, streaming = false, className }: AgentMessageProps) {
  const hasContent = content.trim().length > 0;
  const components = useMemo(() => buildMarkdownComponents(streaming), [streaming]);

  return (
    <div className={cn("flex min-w-0 justify-start", className)}>
      <div
        className={cn(
          "max-w-bubble min-w-0 space-y-2 rounded-lg rounded-bl-sm bg-muted/50 px-4 py-2.5",
          MessageTypography.body,
          CodeLine.minHeight,
        )}
      >
        <MarkdownStream
          content={content}
          streaming={streaming}
          components={components}
          caret={<StreamingCaret show={streaming && hasContent} />}
        />
      </div>
    </div>
  );
});

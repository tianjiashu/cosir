/**
 * Agent 输出消息组件。
 *
 * 使用 react-markdown 渲染 Markdown 内容，代码块和文件路径分别委托给
 * CodeBlock / FileLink 组件做富展示，其余 Markdown（标题、列表、表格、
 * 加粗/斜体/链接等）由 react-markdown + remark-gfm 原生渲染。
 *
 * @module components/chat/AgentMessage
 */

import type { Components } from "react-markdown";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { cn } from "@/lib/utils";
import { CodeBlock } from "./CodeBlock";
import { FileLink } from "./FileLink";

/** Agent 消息组件属性。 */
interface AgentMessageProps {
  /** 消息文本内容（Markdown 格式）。 */
  content: string;
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

/** react-markdown 自定义组件映射：覆盖默认渲染行为。 */
const markdownComponents: Components = {
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
      return <CodeBlock code={code} language={language} />;
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
    if (!text.trim()) return null;
    return <p className="whitespace-pre-wrap">{children}</p>;
  },
};

/**
 * AgentMessage Agent 输出消息组件。
 *
 * 将消息内容通过 react-markdown 完整渲染为 Markdown，其中：
 * - 代码块（```lang ... ```）：使用 CodeBlock 组件
 * - 文件路径内联代码（`path/to/file.ext`）：使用 FileLink 组件
 * - 其余 Markdown 元素（标题/列表/表格/加粗/链接等）：由 remark-gfm 原生支持
 */
export function AgentMessage({ content, className }: AgentMessageProps) {
  return (
    <div className={cn("flex justify-start", className)}>
      <div className="max-w-[85%] space-y-2 rounded-lg rounded-bl-sm bg-muted/50 px-4 py-2.5 text-sm leading-relaxed">
        <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
          {content}
        </ReactMarkdown>
      </div>
    </div>
  );
}

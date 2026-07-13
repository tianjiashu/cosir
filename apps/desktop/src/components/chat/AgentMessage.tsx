/**
 * Agent 输出消息组件。
 *
 * 展示 Agent 的文本输出、代码块和文件链接，
 * 使用浅灰内容块区分不同类型的内容片段。
 *
 * @module components/chat/AgentMessage
 */

import { cn } from "@/lib/utils";
import { CodeBlock } from "./CodeBlock";
import { FileLink } from "./FileLink";

/** Agent 消息组件属性。 */
interface AgentMessageProps {
  /** 消息文本内容（支持内联 Markdown 代码块和文件路径）。 */
  content: string;
  /** 可选的额外 CSS 类名。 */
  className?: string;
}

/** 简单的内联文件路径匹配正则（匹配 `docs/xxx.md` 或 `src/xxx.ts` 等格式）。 */
const FILE_PATH_PATTERN = /`([a-zA-Z0-9_/.-]+\.(?:md|ts|tsx|py|json|yaml|yml|toml|css|html))`/g;

/** 简单的代码块匹配正则（匹配 ```code``` 格式）。 */
const CODE_BLOCK_PATTERN = /```(\w*)\n([\s\S]*?)```/g;

/**
 * AgentMessage Agent 输出消息组件。
 *
 * 将消息内容拆分为纯文本、代码块和文件链接三部分分别渲染：
 * - 纯文本：直接展示
 * - 代码块：使用 CodeBlock 组件
 * - 文件路径：使用 FileLink 组件
 */
export function AgentMessage({ content, className }: AgentMessageProps) {
  // 第一版：简单渲染，后续接入完整 Markdown 解析器
  const parts: Array<{ type: "text" | "code" | "file"; value: string; lang?: string }> = [];

  let lastIndex = 0;
  let match: RegExpExecArray | null;

  // 收集所有代码块和文件链接的位置
  const segments: Array<{ start: number; end: number; type: "code" | "file"; lang?: string; value: string }> = [];

  // 匹配代码块
  const codeRegex = new RegExp(CODE_BLOCK_PATTERN.source, "g");
  while ((match = codeRegex.exec(content)) !== null) {
    segments.push({
      start: match.index,
      end: match.index + match[0].length,
      type: "code",
      lang: match[1] || undefined,
      value: match[2],
    });
  }

  // 匹配文件路径
  const fileRegex = new RegExp(FILE_PATH_PATTERN.source, "g");
  while ((match = fileRegex.exec(content)) !== null) {
    // 跳过已被代码块覆盖的范围
    const isInsideCode = segments.some(
      (s) => s.type === "code" && match!.index >= s.start && match!.index < s.end,
    );
    if (!isInsideCode) {
      segments.push({
        start: match.index,
        end: match.index + match[0].length,
        type: "file",
        value: match[1],
      });
    }
  }

  // 按位置排序并构建 parts 数组
  segments.sort((a, b) => a.start - b.start);

  segments.forEach((seg) => {
    if (seg.start > lastIndex) {
      parts.push({ type: "text", value: content.slice(lastIndex, seg.start) });
    }
    parts.push(seg);
    lastIndex = seg.end;
  });

  if (lastIndex < content.length) {
    parts.push({ type: "text", value: content.slice(lastIndex) });
  }

  return (
    <div className={cn("flex justify-start", className)}>
      <div className="max-w-[85%] space-y-2 rounded-lg rounded-bl-sm bg-muted/50 px-4 py-2.5 text-sm leading-relaxed">
        {parts.map((part, i) => {
          switch (part.type) {
            case "code":
              return <CodeBlock key={i} code={part.value} language={part.lang} />;
            case "file":
              return <FileLink key={i} path={part.value} />;
            default:
              return (
                <p key={i} className="whitespace-pre-wrap">
                  {part.value}
                </p>
              );
          }
        })}
      </div>
    </div>
  );
}

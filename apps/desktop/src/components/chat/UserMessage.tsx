/**
 * 用户消息气泡组件。
 *
 * 展示用户发送的消息内容，
 * 靠右对齐或使用明确的气泡样式与 Agent 输出区分。
 *
 * @module components/chat/UserMessage
 */

import { cn } from "@/lib/utils";

/** 用户消息组件属性。 */
interface UserMessageProps {
  /** 消息文本内容。 */
  content: string;
  /** 可选的额外 CSS 类名。 */
  className?: string;
}

/**
 * UserMessage 用户消息气泡。
 *
 * 使用浅色背景 + 右侧边距区分用户消息与 Agent 输出，
 * 对齐 Codex 桌面客户端的消息样式方向。
 */
export function UserMessage({ content, className }: UserMessageProps) {
  return (
    <div className={cn("flex justify-end", className)}>
      <div className="max-w-[85%] rounded-lg rounded-br-sm bg-primary/10 px-4 py-2.5 text-sm leading-relaxed">
        <p className="whitespace-pre-wrap">{content}</p>
      </div>
    </div>
  );
}

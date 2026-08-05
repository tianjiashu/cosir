/**
 * 用户消息气泡组件。
 *
 * 展示用户发送的消息内容，
 * 靠右对齐或使用明确的气泡样式与 Agent 输出区分。
 *
 * @module components/chat/UserMessage
 */

import { memo } from "react";
import { cn } from "@/lib/utils";

import { MessageTypography } from "./messageTypography";

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
 * 使用主色背景 + 前景色文字，靠右对齐，与 Agent 输出形成明显区分；
 * 对齐 Codex 桌面客户端的消息样式方向。
 */
export const UserMessage = memo(function UserMessage({ content, className }: UserMessageProps) {
  const trimmed = content.trim();
  if (trimmed.length === 0) {
    return null;
  }
  return (
    <div className={cn("flex min-w-0 justify-end", className)}>
      <div
        className={cn(
          "max-w-bubble min-w-0 rounded-lg rounded-br-sm bg-primary px-4 py-2.5 text-primary-foreground shadow-sm",
          MessageTypography.body,
        )}
      >
        <p className="whitespace-pre-wrap">{trimmed}</p>
      </div>
    </div>
  );
});

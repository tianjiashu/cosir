import { MessagePrimitive } from "@assistant-ui/react";
import { BotIcon } from "lucide-react";
import type { FC } from "react";

import { useOpenTask } from "@/components/links/use-open-task";
import { MarkdownText } from "@/components/markdown-text";
import type { TransportMessageSource } from "@/lib/assistant/contract";

/**
 * 另一个 Agent 交给本 Task 的消息（委派、追加消息、Team 节点输入）：不是用户说的话。
 *
 * 靠左、不画成用户气泡，标出发送方并可跳到来源；不可编辑重跑（后端同样拒绝）。正文是纯文本，按
 * Markdown 排版，不解析用户输入里的附件胶囊。
 */
export const AgentInboundMessage: FC<{ source: TransportMessageSource }> = ({ source }) => {
  const openTask = useOpenTask();
  return (
    <MessagePrimitive.Root data-role="agent-message" className="flex flex-col items-start px-2">
      <div className="flex max-w-[85%] flex-col items-start gap-1.5">
        <button
          type="button"
          className="text-muted-foreground hover:text-foreground flex items-center gap-1 text-xs"
          onClick={() => void openTask(source.taskId, source.name)}
        >
          <BotIcon className="size-3.5" aria-hidden />
          <span>来自 {source.name}</span>
        </button>
        <div className="bg-surface text-foreground rounded-2xl px-4 py-2.5 text-sm leading-relaxed wrap-break-word">
          <MessagePrimitive.Parts>{({ part }) => (
            part.type === "text" ? <MarkdownText status={part.status} /> : null
          )}</MessagePrimitive.Parts>
        </div>
      </div>
    </MessagePrimitive.Root>
  );
};

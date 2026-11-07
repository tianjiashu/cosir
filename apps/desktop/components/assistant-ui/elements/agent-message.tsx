import { MessagePrimitive, useAuiState, useScrollLock } from "@assistant-ui/react";
import { BotIcon } from "lucide-react";
import { useMemo, useRef } from "react";
import { useOpenTask } from "@/components/links/use-open-task";
import { StaticMarkdown } from "@/components/static-markdown";
import { CopyIconButton } from "@/components/ui/copy-icon-button";
import { DisclosureChevron } from "@/components/ui/disclosure-chevron";
import type { TransportMessageSource } from "@/lib/assistant/contract";
import { agentKey, useDisclosure } from "@/lib/assistant/disclosure-store";
import { messagePreview } from "@/lib/assistant/message-preview";

/** 开启回合的 Agent 消息：独立成行，正文就是这条 user 消息的文本。 */
export function AgentInboundMessage({ source, taskId }: { source: TransportMessageSource; taskId: number }) {
  const id = useAuiState((state) => state.message.id);
  const parts = useAuiState((state) => state.message.parts);
  const text = useMemo(() => parts.map((part) => part.type === "text" ? part.text : "").filter(Boolean).join("\n\n"), [parts]);
  return <MessagePrimitive.Root data-role="agent-message" className="px-2">
    <AgentMessageRow id={id} text={text} source={source} taskId={taskId} />
  </MessagePrimitive.Root>;
}

/** Source-tagged input: quiet by default, readable and navigable without pretending to be a human message. */
export function AgentMessageRow({ id, text, source, taskId }: { id: string; text: string; source: TransportMessageSource; taskId: number }) {
  const openTask = useOpenTask();
  const preview = useMemo(() => messagePreview(text), [text]);
  const [open, setOpen] = useDisclosure(taskId, agentKey(id), false);
  const ref = useRef<HTMLDivElement>(null);
  const lock = useScrollLock(ref, 0);
  return <div ref={ref} data-slot="agent-message" className="flex min-w-0 flex-col gap-3">
    <button type="button" data-slot="agent-message-toggle" aria-expanded={open}
      onClick={() => { lock(); setOpen(!open); }} className="text-muted-foreground hover:text-foreground flex min-w-0 items-start gap-1.5 text-left text-2sm leading-6">
      <BotIcon className="mt-1 size-3.5 shrink-0" aria-hidden />
      <span className="max-w-[40%] shrink-0 truncate" title={source.name}>来自 {source.name}</span>
      <span className="min-w-0 flex-1 line-clamp-2 sm:line-clamp-1">· {preview || "查看消息"}</span>
      <DisclosureChevron open={open} className="mt-1" />
    </button>
    {open && <div data-slot="agent-message-body" className="min-w-0 pl-5 text-chat leading-7 wrap-break-word">
      <div className="aui-md !space-y-3 leading-6"><StaticMarkdown>{text}</StaticMarkdown></div>
      <div className="text-muted-foreground mt-2 flex items-center gap-2 text-xs">
        <CopyIconButton label="复制消息原文" getText={() => text} logEvent="agent_message_copy_failed" logData={{ taskId, messageId: id }} />
        <button type="button" className="hover:text-foreground" onClick={() => void openTask(source.taskId, source.name)}>打开来源任务</button>
      </div>
    </div>}
  </div>;
}

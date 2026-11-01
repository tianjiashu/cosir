import { useRef } from "react";
import { BrainIcon } from "lucide-react";
import { MessagePrimitive, useScrollLock } from "@assistant-ui/react";

import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { reasoningKey, useDisclosure } from "@/lib/assistant/disclosure-store";
import type { TransportReasoningPart } from "@/lib/assistant/contract";
import { cn } from "@/lib/utils";
import { REASONING_PART_COMPONENTS } from "./markdown-part";
import { DISCLOSURE_ANIMATION_MS, DISCLOSURE_CONTENT_CLASS } from "./turn-tokens";

/** 首句：到第一个句末标点或换行为止；流式时取最新一行，让用户看到思考在推进。 */
export function reasoningPreview(text: string, streaming: boolean): string {
  const trimmed = text.trim();
  if (!trimmed) return "";
  if (streaming) {
    const lines = trimmed.split("\n").map((line) => line.trim()).filter(Boolean);
    return lines[lines.length - 1] ?? "";
  }
  const match = /^[^。！？!?\n]*[。！？!?]?/.exec(trimmed);
  return (match?.[0] ?? trimmed).trim() || trimmed.split("\n")[0];
}

/**
 * 思考行：一行 12.5px 灰字「思考 · 首句」，流式时「思考中 · 最新一行」闪烁；点开展开 Markdown 全文
 * （经 `PartByIndex` 渲染，保留 assistant-ui 的流式作用域）。开合记在 disclosure store，默认收起。
 */
export function ReasoningItem({ taskId, messageId, index, part }: { taskId: number; messageId: string; index: number; part: TransportReasoningPart }) {
  const streaming = part.status === "running";
  const [open, setOpen] = useDisclosure(taskId, reasoningKey(messageId, index), false);
  const collapsibleRef = useRef<HTMLDivElement>(null);
  const lockScroll = useScrollLock(collapsibleRef, DISCLOSURE_ANIMATION_MS);
  const preview = reasoningPreview(part.text, streaming);
  return (
    <Collapsible
      ref={collapsibleRef}
      open={open}
      onOpenChange={(next) => {
        lockScroll();
        setOpen(next);
      }}
      style={{ "--animation-duration": `${DISCLOSURE_ANIMATION_MS}ms` } as React.CSSProperties}
      data-slot="reasoning-item"
    >
      <CollapsibleTrigger className="text-muted-foreground hover:bg-muted -mx-1.5 flex min-h-[26px] w-full min-w-0 items-center gap-2 rounded-md px-1.5 py-0.5 text-left text-xs leading-5">
        <BrainIcon className="size-3.5 shrink-0" aria-hidden="true" />
        <span className={cn("min-w-0 flex-1 truncate", streaming && "shimmer motion-reduce:animate-none")}>
          {streaming ? "思考中" : "思考"}{preview && <> · {preview}</>}
        </span>
      </CollapsibleTrigger>
      <CollapsibleContent className={DISCLOSURE_CONTENT_CLASS}>
        <div className="text-muted-foreground mt-1 mb-1.5 ml-[22px] text-sm" aria-busy={streaming}>
          <MessagePrimitive.PartByIndex index={index} components={REASONING_PART_COMPONENTS} />
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}

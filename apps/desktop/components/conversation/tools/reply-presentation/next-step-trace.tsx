import { useRef } from "react";
import { useScrollLock } from "@assistant-ui/react";

import { DisclosureChevron } from "@/components/ui/disclosure-chevron";
import type { NextStepOffer } from "@/lib/assistant/tools/next-step-offer";
import { nextStepKey, useDisclosure } from "@/lib/assistant/turn/disclosure-store";

/**
 * 过期的下一步建议：之后有了新消息、Run 停止或失败、或是分叉继承来的回复，建议不再可点，原地留一行问题，
 * 展开看当时的回顾与选项。选项只读：后端不知道之后那条消息是不是按钮发的，所以不标「选了哪个」。
 * 撤回让这条回复重新成为最后一条时，回合组件自然换回可点的卡片。
 */
export function NextStepTrace({ offer, toolCallId, sessionId }: { offer: NextStepOffer; toolCallId: string; sessionId: number }) {
  const [open, setOpen] = useDisclosure(sessionId, nextStepKey(toolCallId), false);
  const ref = useRef<HTMLDivElement>(null);
  const lock = useScrollLock(ref, 0);
  return <div ref={ref} data-slot="next-step-trace" className="flex min-w-0 flex-col gap-2">
    <button type="button" data-slot="next-step-trace-toggle" aria-expanded={open}
      onClick={() => { lock(); setOpen(!open); }} className="text-muted-foreground hover:text-foreground flex min-w-0 items-center gap-1.5 text-left text-2sm leading-6">
      <span className="min-w-0 truncate" title={offer.ask}>{offer.ask}</span>
      <DisclosureChevron open={open} />
    </button>
    {open && <div data-slot="next-step-trace-body" className="text-muted-foreground flex min-w-0 flex-col gap-2 text-2sm leading-6">
      <p className="whitespace-pre-wrap">{offer.recap}</p>
      <ul className="list-disc pl-5">{offer.replies.map((reply) => <li key={reply}>{reply}</li>)}</ul>
    </div>}
  </div>;
}

import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * 等待用户卡的外框：对话流里唯一带边框、唯一用信息蓝的容器（设计 §5.8：需要人动作、可能停留很久，
 * 深浅主题都要第一眼看到）。等你操作的调用就地画成这张卡（内容由 kind 视图的 `awaiting` 给出，见
 * `call-view.ts`），决定之后原地变回一行调用。
 */
export function AwaitingFrame({ toolCallId, title, className, children }: { toolCallId: string; title: string; className?: string; children: ReactNode }) {
  return (
    <section
      data-slot="awaiting-card"
      data-tool-call-id={toolCallId}
      aria-label={`等你操作：${title}`}
      className={cn(
        "min-w-0 rounded-(--radius) px-3 py-2.5 text-2sm",
        "border border-[color-mix(in_oklab,var(--info)_38%,transparent)] bg-[color-mix(in_oklab,var(--info)_6%,transparent)]",
        className,
      )}
    >
      {children}
    </section>
  );
}

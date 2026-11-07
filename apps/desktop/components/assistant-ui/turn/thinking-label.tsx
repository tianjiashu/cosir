import type { ComponentProps } from "react";

import type { ActivityFeedback } from "@/lib/assistant/current-activity";
import { cn } from "@/lib/utils";

/**
 * Shared quiet label for waiting output and real reasoning; motion never changes layout.
 * 字号继承所在行，否则状态结束时文字会跳一档。高光带要窄于短标签（「正在思考」约 56px）才看得出在扫。
 */
export function ThinkingLabel({ active, className, ...props }: ComponentProps<"span"> & { active: boolean; children: string }) {
  return <span {...props} data-thinking-active={active} className={cn(
    "text-muted-foreground inline-block",
    active && "motion-safe:shimmer [--shimmer-duration:1800] [--shimmer-repeat-delay:0] [--shimmer-spread:2.5em] [--shimmer-angle:0deg] [--shimmer-color:var(--foreground)]",
    className,
  )} />;
}

/** 回合当前活动：读屏播报与定位都认这一个槽位，同一时刻只有一个所有者。 */
export function ActivityStatus({ feedback, className }: { feedback: ActivityFeedback; className?: string }) {
  return <span data-slot="current-activity" role="status" aria-live="polite" className={cn("min-w-0 [overflow-wrap:anywhere]", className)}>
    <ThinkingLabel active={feedback.animated}>{feedback.label}</ThinkingLabel>
  </span>;
}

import type { ComponentProps } from "react";

import type { ActivityFeedback } from "@/lib/assistant/turn/current-activity";
import { cn } from "@/lib/utils";

/**
 * 「还在运行」的唯一视觉语言：文字上扫过一道高光，调用行标题与活动行共用；动效不改变版面。
 * 字号继承所在行，否则状态结束时文字会跳一档。高光带要窄于短标签（「正在思考」约 56px）才看得出在扫。
 */
export function ThinkingLabel({ active, className, ...props }: ComponentProps<"span"> & { active: boolean; children: string }) {
  return <span {...props} data-thinking-active={active} className={cn(
    "text-muted-foreground inline-block",
    active && "motion-safe:shimmer [--shimmer-duration:1800] [--shimmer-repeat-delay:0] [--shimmer-spread:2.5em] [--shimmer-angle:0deg] [--shimmer-color:var(--foreground)]",
    className,
  )} />;
}

/** 活动行的版面：回复出现前（线程级）、回合内的活动段与停止行同一行高，交接时不跳。 */
export const CURRENT_ACTIVITY_SLOT = "flex min-h-8 items-center text-2sm";

/**
 * 回合当前活动：运行中文案扫光，挂起等待与停止静止。读屏播报与定位都认这一个元素，同一时刻只有一个所有者
 * （运行中的活动段、回复出现前的线程级行，或停止行）。
 */
export function ActivityStatus({ feedback, className }: { feedback: ActivityFeedback; className?: string }) {
  return <span data-slot="current-activity" role="status" aria-live="polite" className={cn("flex min-w-0", className)}>
    <ThinkingLabel data-slot="current-activity-label" active={feedback.animated} className="min-w-0 truncate" title={feedback.label}>{feedback.label}</ThinkingLabel>
  </span>;
}

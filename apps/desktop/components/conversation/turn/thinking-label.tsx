import type { ComponentProps } from "react";

import type { ActivityFeedback } from "@/lib/assistant/turn/current-activity";
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

/** 活动行的版面：回复出现前（线程级）、回合内的活动段与停止行同一行高，交接时不跳。 */
export const CURRENT_ACTIVITY_SLOT = "flex min-h-8 items-center text-2sm";

/**
 * 活动行左侧的三个点：运行中依次明暗，表示「还在工作」；挂起等待时静止。动效只改透明度、不占额外宽度，
 * 减少动画偏好下不动（`motion-safe`）。不用转圈图标：回合里只有这一种「活着」的标识，避免和调用行的扫光
 * 并存两套语言。
 */
export function ActivityDots({ active }: { active: boolean }) {
  return <span data-slot="activity-dots" data-active={active} aria-hidden="true" className="text-muted-foreground inline-flex shrink-0 items-center gap-[3px]">
    {[0, 1, 2].map((i) => <span key={i} className={cn("size-1 rounded-full bg-current", active ? "motion-safe:animate-pulse" : "opacity-60")} style={active ? { animationDelay: `${i * 200}ms` } : undefined} />)}
  </span>;
}

/**
 * 回合当前活动：动画点 · 文案（Claude 式，文字本身不扫光）。读屏播报与定位都认这一个元素，同一时刻只有
 * 一个所有者（运行中的活动段、回复出现前的线程级行，或停止行）。
 */
export function ActivityStatus({ feedback, className }: { feedback: ActivityFeedback; className?: string }) {
  return <span data-slot="current-activity" role="status" aria-live="polite" className={cn("inline-flex min-w-0 items-center gap-2", className)}>
    <ActivityDots active={feedback.animated} />
    <span data-slot="current-activity-label" className="text-muted-foreground min-w-0 truncate" title={feedback.label}>{feedback.label}</span>
  </span>;
}

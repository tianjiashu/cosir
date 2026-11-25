import { useLayoutEffect, useRef, useState, type ComponentProps, type CSSProperties } from "react";

import type { ActivityFeedback } from "@/lib/assistant/turn/current-activity";
import { cn } from "@/lib/utils";
import { ToolIcon } from "../tools/tool-icons";

/**
 * 「还在运行」的唯一视觉语言：文字上扫过一道高光，调用行标题与活动行共用；动效不改变版面。
 * 字号继承所在行，否则状态结束时文字会跳一档。高光带要窄于短标签（「正在思考」约 56px）才看得出在扫。
 *
 * 匀速：tw-shimmer 的轨道宽度缺省按 200px 算时长、按元素宽度算行程，长短标签扫得快慢不一；固定轨道又会让
 * 宽于轨道的长标题扫不到尾。所以这里量出标签自身宽度作为轨道，时长由 `--shimmer-speed` 推导，任何宽度速度一致；
 * 每遍之后停一段（`--shimmer-repeat-delay`）再来，高光取半透明前景色、带稍宽，读作一层光而不是一个亮点。
 */
export function ThinkingLabel({ active, className, style, ...props }: ComponentProps<"span"> & { active: boolean; children: string }) {
  const ref = useRef<HTMLSpanElement>(null);
  const [trackWidth, setTrackWidth] = useState<number>();
  useLayoutEffect(() => {
    const element = ref.current;
    if (!active || !element) return;
    const measure = () => setTrackWidth(Math.round(element.getBoundingClientRect().width));
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [active]);
  return <span {...props} ref={ref} data-thinking-active={active}
    style={active && trackWidth ? { ...style, "--shimmer-track-width": `${trackWidth}px` } as CSSProperties : style}
    className={cn(
      "text-muted-foreground inline-block",
      active && "motion-safe:shimmer [--shimmer-speed:80] [--shimmer-repeat-delay:1000] [--shimmer-spread:3.5em] [--shimmer-angle:0deg] [--shimmer-color:color-mix(in_oklch,var(--foreground)_70%,transparent)]",
      className,
    )} />;
}

/** 活动行的版面：回复出现前（线程级）、回合内的活动段与停止行同一行高，交接时不跳。 */
export const CURRENT_ACTIVITY_SLOT = "flex min-h-6 items-center text-2sm";

/**
 * 活动行行首的图标：段头的汇总态与 `ActivityStatus` 共用这一格，状态在「思考 / 执行 / 汇总」之间切换时文字不横移。
 * 与调用行图标同尺寸，展开后段头图标与下方竖线、调用行图标对齐。
 */
export function ActivityIcon({ name }: { name: string | undefined }) {
  return <ToolIcon data-slot="activity-icon" name={name} className="size-3.5" aria-hidden="true" />;
}

/**
 * 回合当前活动：运行中文案扫光，挂起等待与停止静止。读屏播报与定位都认这一个元素，同一时刻只有一个所有者
 * （运行中的活动段、回复出现前的线程级行，或停止行）；三处同一个图标槽，交接时不跳。
 */
export function ActivityStatus({ feedback, className }: { feedback: ActivityFeedback; className?: string }) {
  return <span data-slot="current-activity" role="status" aria-live="polite" className={cn("text-muted-foreground flex min-w-0 items-center gap-1.5", className)}>
    <ActivityIcon name={feedback.icon} />
    <ThinkingLabel data-slot="current-activity-label" active={feedback.animated} className="min-w-0 truncate" title={feedback.label}>{feedback.label}</ThinkingLabel>
  </span>;
}

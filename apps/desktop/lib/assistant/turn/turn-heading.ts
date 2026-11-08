import type { TransportOutcome } from "../contract";
import { formatDuration } from "./run-duration";
import type { TurnLayout } from "./turn-layout";

/** 28px content + 8px bottom padding + 1px divider; shared with the virtual row estimate. */
export const TURN_HEADING_HEIGHT = 37;
export type TurnHeadingPresentation = { label: string; expandable: boolean; failed: boolean };

/** One heading for both presentation and height estimation. Child/tool outcomes never replace the Run outcome. */
export function projectTurnHeading(layout: TurnLayout, outcome: TransportOutcome | null, elapsedMs: number | null = null): TurnHeadingPresentation | null {
  if (layout.intro) return null;
  const expandable = layout.process.items.length > 0;
  const status = outcome?.endReason === "runtime_restarted" ? "运行已中断 · 后端已重启"
    : outcome?.status === "cancelled" ? "已停止" : outcome?.status === "failed" ? "运行失败"
      : null;
  // 完成后的耗时只修饰真实过程；异常状态和进行中的反馈仍独立保留。
  if (outcome?.status === "completed" && !expandable && !status) return null;
  const durationMs = outcome?.durationMs ?? elapsedMs;
  const duration = durationMs === null ? null : `用时 ${formatDuration(durationMs)}`;
  const label = [status, duration].filter(Boolean).join(" · ") || (expandable ? "查看过程" : null);
  return label === null ? null : { label, expandable, failed: outcome?.status === "failed" };
}

import type { TransportOutcome } from "../contract";
import { formatDuration } from "@/lib/format/duration";
import type { TurnLayout } from "./turn-layout";

/** 28px content + 8px bottom padding + 1px divider; shared with the virtual row estimate. */
export const TURN_HEADING_HEIGHT = 37;
export type TurnHeadingPresentation = { label: string; expandable: boolean; failed: boolean };

/**
 * One heading for both presentation and height estimation. Child/tool outcomes never replace the Run outcome.
 * 标题只在有可收起的过程或 Run 异常结局时存在，运行中、挂起、完成同一条规则；耗时只修饰标题，不单独
 * 构成标题。只有正文时正文本身就是反馈（与 `currentActivity` 同一原则），等待由当前活动表达。
 */
export function projectTurnHeading(layout: TurnLayout, outcome: TransportOutcome | null, elapsedMs: number | null = null): TurnHeadingPresentation | null {
  if (layout.intro) return null;
  const expandable = layout.process.items.length > 0;
  const status = outcome?.endReason === "runtime_restarted" ? "运行已中断 · 后端已重启"
    : outcome?.status === "cancelled" ? "已停止" : outcome?.status === "failed" ? "运行失败"
      : null;
  if (!expandable && !status) return null;
  const durationMs = outcome?.durationMs ?? elapsedMs;
  const duration = durationMs === null ? null : `用时 ${formatDuration(durationMs)}`;
  // 有异常状态时标签非空；否则必有过程，没有耗时（历史回复）就写「查看过程」。
  const label = [status, duration].filter(Boolean).join(" · ") || "查看过程";
  return { label, expandable, failed: outcome?.status === "failed" };
}

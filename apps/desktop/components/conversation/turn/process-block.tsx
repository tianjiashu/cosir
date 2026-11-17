import { memo, useRef } from "react";
import { useScrollLock } from "@assistant-ui/react";
import { DisclosureChevron } from "@/components/ui/disclosure-chevron";
import { activityKey, useDisclosure } from "@/lib/assistant/turn/disclosure-store";
import type { ActivityFeedback } from "@/lib/assistant/turn/current-activity";
import { segmentStartedAt } from "@/lib/assistant/turn/segment-clock";
import { describeCallProblems, describeVerbCounts, type ActivityGroup, type CallItem } from "@/lib/assistant/turn/turn-layout";
import { CallElapsed } from "../tools/call-elapsed";
import { ActivityStatus, CURRENT_ACTIVITY_SLOT } from "./thinking-label";

/**
 * 活动段这一行（Claude 式）：运行时是「动画点 · 当前状态 · 这一段的连续计时 · 箭头」，思考与工具调用共用一个
 * 计时（起点见 `segment-clock`，跑过 3 秒才出现，与调用行同一门槛）；段结束原地换成汇总（计数与问题）。还没有
 * 调用的段只有状态、没有箭头，也不是按钮——没有可展开的东西。展开才挂载普通调用，用户选择保持稳定。
 *
 * `feedback` 覆盖段自己的状态，只用于「正在停止…」：停止是界面暂态，布局层拿不到。
 */
export const ProcessBlock = memo(function ProcessBlock({ sessionId, messageId, group, feedback, renderCall, visible = true }: {
  sessionId: number; messageId: string; group: ActivityGroup; feedback?: ActivityFeedback; visible?: boolean; renderCall: (item: CallItem, visible: boolean) => React.ReactNode;
}) {
  const [open, setOpen] = useDisclosure(sessionId, activityKey(messageId, group.key), false);
  const ref = useRef<HTMLDivElement>(null);
  const lockScroll = useScrollLock(ref, 0);
  const live = group.live && (feedback ?? group.live);
  if (!group.calls.length && !live) return null;
  // 计时只在运行中走；挂起等待（静止的点）不算这一段在工作。
  const startedAt = live?.animated ? segmentStartedAt(sessionId, messageId, group.key, earliestStart(group.calls)) : undefined;
  const status = live && <><ActivityStatus feedback={live} /><CallElapsed startedAt={startedAt} /></>;
  if (!group.calls.length) return <div data-slot="activity-group" className={CURRENT_ACTIVITY_SLOT}><span className="flex min-w-0 items-center gap-1.5">{status}</span></div>;
  const summary = group.summary;
  const counts = describeVerbCounts(summary);
  const problems = describeCallProblems(summary);
  return (
    <div ref={ref}
      data-slot="activity-group" className="flex min-w-0 flex-col gap-1">
      <button type="button" data-slot="activity-head" aria-expanded={open} title={open ? "收起工具记录" : "展开工具记录与操作"} onClick={() => { lockScroll(); setOpen(!open); }}
        className="group/disclosure text-muted-foreground hover:text-foreground flex min-w-0 min-h-8 items-center gap-1.5 text-left text-2sm">
        {status || <span>{counts || "执行记录"}</span>}
        {problems && <span className={summary.failedCount > 0 ? "text-destructive" : "text-muted-foreground"}>· {problems}</span>}
        <DisclosureChevron open={open} reveal />
      </button>
      {group.calls.map((item) => <div key={item.part.toolCallId} hidden={!open} className="min-w-0 pl-5">
        {renderCall(item, visible && open)}
      </div>)}
    </div>
  );
});

/** 段内已开跑调用的最早起点：运行中刷新页面后，计时至少从这里算起。 */
function earliestStart(calls: readonly CallItem[]): string | undefined {
  return calls.reduce<string | undefined>((min, { part }) => part.startedAt && (!min || part.startedAt < min) ? part.startedAt : min, undefined);
}

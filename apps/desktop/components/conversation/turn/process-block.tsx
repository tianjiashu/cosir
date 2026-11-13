import { memo, useRef } from "react";
import { useScrollLock } from "@assistant-ui/react";
import { DisclosureChevron } from "@/components/ui/disclosure-chevron";
import { activityKey, useDisclosure } from "@/lib/assistant/turn/disclosure-store";
import { describeCallProblems, type ActivityGroup, type CallItem } from "@/lib/assistant/turn/turn-layout";
import type { ActivityFeedback } from "@/lib/assistant/turn/current-activity";
import { ActivityStatus } from "./thinking-label";

/** 连续工具区间；摘要承担当前活动，展开才挂载普通调用，用户选择保持稳定。 */
export const ProcessBlock = memo(function ProcessBlock({ taskId, messageId, group, feedback, renderCall, visible = true }: {
  taskId: number; messageId: string; group: ActivityGroup; feedback?: ActivityFeedback; visible?: boolean; renderCall: (item: CallItem, visible: boolean) => React.ReactNode;
}) {
  const [open, setOpen] = useDisclosure(taskId, activityKey(messageId, group.key), false);
  const ref = useRef<HTMLDivElement>(null);
  const lockScroll = useScrollLock(ref, 0);
  if (!group.calls.length) return feedback ? <ActivityStatus feedback={feedback} className="flex min-h-8 items-center text-2sm" /> : null;
  const summary = group.summary;
  // 种类多时只列最多的三类，否则标题折成好几行，失败提示被挤走。
  const verbs = [...summary.verbCounts].sort((left, right) => right.count - left.count);
  const counts = verbs.slice(0, 3).map(({ label, count }) => `${label} ${count} 次`).join(" · ") + (verbs.length > 3 ? ` 等 ${verbs.length} 类` : "");
  const problems = describeCallProblems(summary);
  return (
    <div ref={ref}
      data-slot="activity-group" className="flex min-w-0 flex-col gap-1">
      <button type="button" data-slot="activity-head" aria-expanded={open} title={open ? "收起工具记录" : "展开工具记录与操作"} onClick={() => { lockScroll(); setOpen(!open); }}
        className="group/disclosure text-muted-foreground hover:text-foreground flex min-w-0 min-h-8 items-center gap-1.5 text-left text-2sm">
        {feedback ? <ActivityStatus feedback={feedback} /> : <span>{counts || "执行记录"}</span>}
        {problems && <span className={summary.failedCount > 0 ? "text-destructive" : "text-muted-foreground"}>· {problems}</span>}
        <DisclosureChevron open={open} reveal />
      </button>
      {group.calls.map((item) => <div key={item.part.toolCallId} hidden={!open} className="min-w-0 pl-5">
        {renderCall(item, visible && open)}
      </div>)}
    </div>
  );
});

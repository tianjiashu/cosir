import { memo, useMemo, useRef } from "react";
import { useScrollLock } from "@assistant-ui/react";
import { DisclosureChevron } from "@/components/ui/disclosure-chevron";
import { activityKey, useDisclosure } from "@/lib/assistant/turn/disclosure-store";
import type { ActivityFeedback } from "@/lib/assistant/turn/current-activity";
import { describeCallProblems, describeVerbCounts, type ActivityGroup, type CallItem } from "@/lib/assistant/turn/turn-layout";
import { CallElapsed } from "../tools/call-elapsed";
import { ActivityStatus, CURRENT_ACTIVITY_SLOT } from "./thinking-label";
import { useAttachmentWorkspaceId } from "@/components/conversation/thread/attachment-context";
import { ImageStrip } from "@/components/image/image-strip";
import { attachmentContentUrl } from "@/lib/assistant/attachments/image-src";
import type { CallImage } from "@/lib/assistant/tools/call-images";
import { cn } from "@/lib/utils";
import { DiffStat } from "../tools/call-views/file-changes-view";

/**
 * 活动段这一行：运行时是「扫光的当前状态 · 这个状态的计时 · 改动量 · 箭头」，思考计这一次思考、执行计那次
 * 调用，换状态从头算（起点见 `ActivityFeedback.since`，跑过 3 秒才出现，与调用行同一门槛）；段结束原地换成
 * 汇总（计数、改动量与问题）。这一段产出的图片缩略图并列在行右侧。还没有调用的段只有状态、没有箭头，也不是
 * 按钮——没有可展开的东西。展开才挂载普通调用，用户选择保持稳定。
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
  const status = live && <><ActivityStatus feedback={live} /><CallElapsed startedAt={live.since} /></>;
  if (!group.calls.length) return <div data-slot="activity-group" className={CURRENT_ACTIVITY_SLOT}><span className="flex min-w-0 items-center gap-1.5">{status}</span></div>;
  const summary = group.summary;
  const counts = describeVerbCounts(summary);
  const problems = describeCallProblems(summary);
  return (
    <div ref={ref}
      data-slot="activity-group" className="flex min-w-0 flex-col gap-1">
      {/* 按钮与缩略图并列而不是嵌套：缩略图自己能点开大图，按钮里不能再有按钮。 */}
      <div data-slot="activity-row" className="@container flex min-h-8 min-w-0 items-center gap-2">
        <button type="button" data-slot="activity-head" aria-expanded={open} title={open ? "收起工具记录" : "展开工具记录与操作"} onClick={() => { lockScroll(); setOpen(!open); }}
          className="group/disclosure text-muted-foreground hover:text-foreground flex min-w-0 min-h-8 flex-1 items-center gap-1.5 text-left text-2sm">
          {/* 窄时谁先让位：类别计数先截断，改动量与问题不收缩（问题永远可见）。 */}
          {status || <span data-slot="activity-counts" className="min-w-0 truncate" title={counts || undefined}>{counts || "执行记录"}</span>}
          <DiffStat insertions={summary.diff.insertions} deletions={summary.diff.deletions} />
          {problems && <span className={cn("shrink-0", summary.failedCount > 0 ? "text-destructive" : "text-muted-foreground")}>· {problems}</span>}
          <DisclosureChevron open={open} reveal />
        </button>
        {summary.images.length > 0 && <ActivityImages images={summary.images} />}
      </div>
      {group.calls.map((item) => <div key={item.part.toolCallId} hidden={!open} className="min-w-0 pl-5">
        {renderCall(item, visible && open)}
      </div>)}
    </div>
  );
});

/** 这一段产出的图片：最近 3 张，更早的收成「+N」，点开在整段的图里翻看。容器窄到放不下时整组让位（只剩按钮）。 */
function ActivityImages({ images }: { images: readonly CallImage[] }) {
  const workspaceId = useAttachmentWorkspaceId();
  const pictures = useMemo(() => images.map((image) => ({ src: attachmentContentUrl(image.assetId, workspaceId), alt: image.alt, title: image.alt })), [images, workspaceId]);
  return <div data-slot="activity-images" className="hidden shrink-0 @md:block"><ImageStrip images={pictures} label="这一段的图片" size="inline" limit={3} /></div>;
}

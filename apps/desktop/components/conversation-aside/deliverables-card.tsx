import { useState } from "react";

import { DeliverableBadge, describeDeliverable } from "@/components/conversation/tools/reply-presentation/deliverable-label";
import { useOpenLink } from "@/components/links/use-open-link";
import { MiddleTruncate } from "@/components/ui/middle-truncate";
import type { TransportDeliverable } from "@/lib/assistant/contract";
import { useSessionDeliverables } from "@/lib/deliverables/deliverables-store";

/** 默认列出的条数；更多的收进「显示更多」（对标的「输出内容」同样是五条）。 */
const VISIBLE_COUNT = 5;

/**
 * 「产出」卡（会话侧栏）：本会话交付过的全部东西，条目只读 snapshot 顶层 `deliverables`（后端从完整历史投影，按 key
 * 去重、最近交付的在前），长对话里早期的交付物也在。每行是角标加名称（中间截断、不显示大小），点击与对话里点胶囊
 * 去同一个地方（`resolveDeliverable`）。不探测文件是否还在：清单常驻、会积累几十条，逐条读文件信息就是几十个并发
 * 请求；点击时由打开链路确认。有过交付物才出现。
 */

export function useDeliverablesHasContent(sessionId: number | null): boolean {
  return useSessionDeliverables(sessionId).length > 0;
}

/** 折叠时标题行右侧：条数。 */
export function DeliverablesSummary({ sessionId }: { sessionId: number }) {
  return <>{useSessionDeliverables(sessionId).length}</>;
}

function DeliverableRow({ deliverable, sessionId }: { deliverable: TransportDeliverable; sessionId: number }) {
  const links = useOpenLink("tool", sessionId);
  const { name, path, badge } = describeDeliverable(deliverable);
  const target = links.resolveDeliverable(deliverable);
  return (
    <li>
      <button
        type="button"
        data-slot="aside-deliverable"
        disabled={!target}
        title={deliverable.caption ? `${deliverable.caption}\n${path}` : path}
        onClick={() => target && links.open(target)}
        className="hover:bg-muted focus-visible:ring-ring flex w-full min-w-0 items-center gap-2 rounded-md px-1.5 py-1 text-left text-sm outline-none focus-visible:ring-2 disabled:cursor-default disabled:hover:bg-transparent"
      >
        <DeliverableBadge>{badge}</DeliverableBadge>
        <MiddleTruncate text={name} className="min-w-0 flex-1" />
      </button>
    </li>
  );
}

export function DeliverablesBody({ sessionId }: { sessionId: number }) {
  const entries = useSessionDeliverables(sessionId);
  const [expanded, setExpanded] = useState(false);
  const visible = expanded ? entries : entries.slice(0, VISIBLE_COUNT);
  return (
    <>
      <ul className="flex flex-col gap-0.5">
        {visible.map((deliverable) => <DeliverableRow key={deliverable.key} deliverable={deliverable} sessionId={sessionId} />)}
      </ul>
      {entries.length > VISIBLE_COUNT && (
        <button
          type="button"
          className="text-muted-foreground hover:text-foreground mt-1 rounded-sm px-1.5 py-1 text-xs outline-none"
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? "收起" : `显示更多（${entries.length - VISIBLE_COUNT}）`}
        </button>
      )}
    </>
  );
}

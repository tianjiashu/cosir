import { useSortable } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { GripVerticalIcon } from "lucide-react";

import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { DisclosureChevron } from "@/components/ui/disclosure-chevron";
import { REVEAL_ON_DISCLOSURE_HOVER } from "@/components/ui/reveal-on-hover";
import { useConversationAsideStore, type AsideCardId } from "@/lib/conversation-aside/conversation-aside-store";
import { cn } from "@/lib/utils";

import { CONVERSATION_ASIDE_CARDS } from "./aside-card-registry";

/**
 * 侧栏里一张卡片的外壳：标题行（折叠开关 + 摘要 + 卡片级操作）、拖拽手柄与正文，所有卡片共用。
 *
 * 只有手柄能拖（整张卡可拖会让点标题折叠时误拖）；手柄与折叠箭头平时隐藏，悬停标题行或键盘聚焦时出现，
 * 键盘排序由 dnd-kit 提供（空格拿起、方向键移动、空格放下）。折叠状态是全局偏好，见 `useConversationAsideStore`。
 */
export function AsideCard({ id, sessionId }: { id: AsideCardId; sessionId: number }) {
  const { title, useHasContent, Summary, Actions, Body } = CONVERSATION_ASIDE_CARDS[id];
  const hasContent = useHasContent(sessionId);
  const collapsed = useConversationAsideStore((state) => state.collapsed[id] ?? false);
  const setCollapsed = useConversationAsideStore((state) => state.setCollapsed);
  const { attributes, listeners, setNodeRef, setActivatorNodeRef, transform, transition, isDragging } = useSortable({ id });
  if (!hasContent) return null;

  return (
    <section
      ref={setNodeRef}
      style={{ transform: CSS.Transform.toString(transform), transition }}
      aria-label={title}
      data-aside-card={id}
      className={cn("bg-card rounded-xl border p-3 text-sm shadow-xs", isDragging && "relative z-10 shadow-panel")}
    >
      <Collapsible open={!collapsed} onOpenChange={(open) => setCollapsed(id, !open)}>
        <div className="group/disclosure flex items-center gap-1">
          <CollapsibleTrigger className="flex min-w-0 flex-1 items-center gap-1.5 font-medium outline-none focus-visible:underline">
            {title}
            <DisclosureChevron open={!collapsed} reveal />
          </CollapsibleTrigger>
          {collapsed && <span className="text-muted-foreground shrink-0 text-xs tabular-nums"><Summary sessionId={sessionId} /></span>}
          {Actions && <Actions sessionId={sessionId} />}
          <button
            ref={setActivatorNodeRef}
            type="button"
            aria-label={`拖动排序：${title}`}
            className={cn("text-muted-foreground hover:text-foreground focus-visible:ring-ring/50 -mr-1 flex size-6 shrink-0 cursor-grab items-center justify-center rounded-sm outline-none focus-visible:ring-2 active:cursor-grabbing", REVEAL_ON_DISCLOSURE_HOVER)}
            {...attributes}
            {...listeners}
          >
            <GripVerticalIcon className="size-3.5" aria-hidden="true" />
          </button>
        </div>
        <CollapsibleContent className="mt-2">
          <Body sessionId={sessionId} />
        </CollapsibleContent>
      </Collapsible>
    </section>
  );
}

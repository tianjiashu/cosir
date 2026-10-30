import { useSortable } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { ChevronDownIcon, GripVerticalIcon } from "lucide-react";

import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { useTaskRailStore, type RailCardId } from "@/lib/task-rail/store";
import { cn } from "@/lib/utils";

import { TASK_RAIL_CARDS } from "./cards";

/**
 * 侧栏里一张卡片的外壳：标题行（折叠开关 + 摘要）、拖拽手柄与正文，所有卡片共用。
 *
 * 只有手柄能拖（整张卡可拖会让点标题折叠时误拖）；手柄平时隐藏，悬停或键盘聚焦时出现，
 * 键盘排序由 dnd-kit 提供（空格拿起、方向键移动、空格放下）。折叠状态是全局偏好，见 `useTaskRailStore`。
 */
export function RailCard({ id, taskId }: { id: RailCardId; taskId: number }) {
  const { title, Summary, Body } = TASK_RAIL_CARDS[id];
  const collapsed = useTaskRailStore((state) => state.collapsed[id] ?? false);
  const setCollapsed = useTaskRailStore((state) => state.setCollapsed);
  const { attributes, listeners, setNodeRef, setActivatorNodeRef, transform, transition, isDragging } = useSortable({ id });

  return (
    <section
      ref={setNodeRef}
      style={{ transform: CSS.Transform.toString(transform), transition }}
      aria-label={title}
      data-rail-card={id}
      className={cn("group bg-card rounded-xl border p-3 text-sm shadow-xs", isDragging && "relative z-10 shadow-panel")}
    >
      <Collapsible open={!collapsed} onOpenChange={(open) => setCollapsed(id, !open)}>
        <div className="flex items-center gap-1">
          <CollapsibleTrigger className="flex min-w-0 flex-1 items-center gap-1.5 font-medium outline-none focus-visible:underline">
            {title}
            <ChevronDownIcon className={cn("size-3.5 shrink-0 transition-transform", collapsed && "-rotate-90")} aria-hidden="true" />
          </CollapsibleTrigger>
          <span className="text-muted-foreground shrink-0 text-xs tabular-nums"><Summary taskId={taskId} /></span>
          <button
            ref={setActivatorNodeRef}
            type="button"
            aria-label={`拖动排序：${title}`}
            className="text-muted-foreground hover:text-foreground focus-visible:ring-ring/50 -mr-1 flex size-6 shrink-0 cursor-grab items-center justify-center rounded-sm opacity-0 outline-none group-hover:opacity-100 focus-visible:opacity-100 focus-visible:ring-2 active:cursor-grabbing"
            {...attributes}
            {...listeners}
          >
            <GripVerticalIcon className="size-3.5" aria-hidden="true" />
          </button>
        </div>
        <CollapsibleContent className="mt-2">
          <Body taskId={taskId} />
        </CollapsibleContent>
      </Collapsible>
    </section>
  );
}

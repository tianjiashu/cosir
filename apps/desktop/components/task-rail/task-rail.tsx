import { useState, type KeyboardEvent, type RefObject } from "react";
import {
  closestCenter,
  DndContext,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
  type Announcements,
  type DragEndEvent,
} from "@dnd-kit/core";
import { restrictToParentElement, restrictToVerticalAxis } from "@dnd-kit/modifiers";
import { arrayMove, SortableContext, sortableKeyboardCoordinates, verticalListSortingStrategy } from "@dnd-kit/sortable";

import { useElementSize } from "@/hooks/use-element-size";
import { frontendLog } from "@/lib/logging/frontend-log";
import { TASK_RAIL_WIDTH, taskRailPlacement } from "@/lib/task-rail/placement";
import { useTaskRailStore, type RailCardId } from "@/lib/task-rail/store";
import { cn } from "@/lib/utils";

import { TASK_RAIL_CARDS } from "./cards";
import { RailCard } from "./rail-card";

const MODIFIERS = [restrictToVerticalAxis, restrictToParentElement];
const title = (id: unknown) => TASK_RAIL_CARDS[id as RailCardId].title;
const ANNOUNCEMENTS: Announcements = {
  onDragStart: ({ active }) => `已拿起「${title(active.id)}」`,
  onDragOver: ({ active, over }) => over ? `「${title(active.id)}」移到「${title(over.id)}」的位置` : undefined,
  onDragEnd: ({ active, over }) => over ? `已放下「${title(active.id)}」` : `已放下「${title(active.id)}」，位置不变`,
  onDragCancel: ({ active }) => `已取消拖动「${title(active.id)}」`,
};

/** 浮层最高占对话视口的这个比例，不盖住底部的输入框。 */
const FLOATING_MAX_HEIGHT_RATIO = 0.6;

/**
 * 会话侧栏：当前对话 Task 的状态摘要卡片栈（目前只有「后台任务」）。它是对话画布上的附注，不是窗格：
 * AppLayout 经 `ConversationAsideContext` 把它交给线程视口，放在与内容列同一个网格里、粘在顶部
 * （见 `conversation-aside.tsx`），所以滚动条在视口最外侧，底部终端也不必给它让宽度。
 *
 * 摆放按对话视口宽度决定（`taskRailPlacement`）：`docked` 占网格右列，`floating` 与内容列叠在同一格
 * 右上角、不占宽度。两种摆放是同一个节点只换网格位置，切换时卡片不重挂，展开状态与计时都保留。
 * 浮层是非模态的：点对话区不会关闭它（侧栏里是要盯着看的进度）；焦点在侧栏内时按 Esc 关闭，
 * 拖动中按 Esc 是 dnd-kit 的取消拖动，不关闭侧栏；关闭后焦点交还页头按钮由调用方负责（见 `useTaskRail`）。
 * 高度按视口封顶，卡片多了在侧栏内部滚动。卡片顺序与折叠是全局偏好（`useTaskRailStore`）。
 */
export function TaskRail({ taskId, viewportRef, onClose }: { taskId: number; viewportRef: RefObject<HTMLDivElement | null>; onClose: () => void }) {
  const [, viewport] = useElementSize(viewportRef);
  const placement = taskRailPlacement(viewport.width);
  const order = useTaskRailStore((state) => state.order);
  const setOrder = useTaskRailStore((state) => state.setOrder);
  const [dragging, setDragging] = useState(false);
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  const onDragEnd = ({ active, over }: DragEndEvent) => {
    setDragging(false);
    if (!over || active.id === over.id) return;
    const next = arrayMove(order, order.indexOf(active.id as RailCardId), order.indexOf(over.id as RailCardId));
    setOrder(next);
    void frontendLog("INFO", "task_rail_card_reordered", "会话侧栏卡片顺序已调整", { data: { taskId, order: next } });
  };
  const onKeyDown = (event: KeyboardEvent) => {
    if (event.key !== "Escape" || placement !== "floating" || dragging) return;
    event.stopPropagation();
    onClose();
  };

  return (
    <aside
      aria-label="会话侧栏"
      data-task-rail={placement}
      style={{
        width: TASK_RAIL_WIDTH,
        maxHeight: placement === "docked" ? viewport.height : viewport.height * FLOATING_MAX_HEIGHT_RATIO,
      }}
      onKeyDown={onKeyDown}
      className={cn(
        "row-start-1 z-20 flex min-h-0 flex-col gap-2 self-start overflow-y-auto",
        placement === "docked"
          ? "sticky top-0 col-start-2 py-2 pr-2"
          : "dock-panel bg-background sticky top-2 col-start-1 mt-2 mr-2 justify-self-end p-2",
      )}
    >
      <DndContext
        sensors={sensors}
        collisionDetection={closestCenter}
        modifiers={MODIFIERS}
        accessibility={{ announcements: ANNOUNCEMENTS, screenReaderInstructions: { draggable: "按空格拿起，方向键移动，再按空格放下，Esc 取消。" } }}
        onDragStart={() => setDragging(true)}
        onDragCancel={() => setDragging(false)}
        onDragEnd={onDragEnd}
      >
        <SortableContext items={order} strategy={verticalListSortingStrategy}>
          {order.map((id) => <RailCard key={id} id={id} taskId={taskId} />)}
        </SortableContext>
      </DndContext>
    </aside>
  );
}

import { useState, type KeyboardEvent } from "react";
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

import { frontendLog } from "@/lib/logging/frontend-log";
import { TASK_RAIL_WIDTH, type TaskRailPlacement } from "@/lib/task-rail/placement";
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

/**
 * 会话侧栏：当前对话 Task 的状态摘要卡片栈（目前只有「后台任务」），属于对话区，由 AppLayout 在对话页挂载。
 *
 * 摆放由调用方按整行宽度决定（`taskRailPlacement`）：`docked` 固定为对话右侧一列，`floating` 浮在
 * 对话区右上角、不占宽度。两种摆放是同一个节点只换样式，切换时卡片不重挂，展开状态与计时都保留。
 * 浮层是非模态的：点对话区不会关闭它（侧栏里是要盯着看的进度）；焦点在侧栏内时按 Esc 关闭，
 * 拖动中按 Esc 是 dnd-kit 的取消拖动，不关闭侧栏；关闭后焦点交还页头按钮由调用方负责（见 `useTaskRail`）。
 * 浮层最高只占这一行的六成，不盖住底部的输入框与终端面板。卡片顺序与折叠是全局偏好（`useTaskRailStore`）。
 */
export function TaskRail({ taskId, placement, onClose }: { taskId: number; placement: TaskRailPlacement; onClose: () => void }) {
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
      style={{ width: TASK_RAIL_WIDTH }}
      onKeyDown={onKeyDown}
      className={cn(
        "flex min-h-0 shrink-0 flex-col gap-2 overflow-y-auto",
        placement === "docked"
          ? "h-full py-2 pr-2"
          : "dock-panel bg-background absolute top-2 right-2 z-20 max-h-[60%] p-2",
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

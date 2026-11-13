import { useEffect, useState, type KeyboardEvent } from "react";
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

import type { ElementSize } from "@/hooks/use-element-size";
import { frontendLog } from "@/lib/logging/frontend-log";
import { CONVERSATION_ASIDE_WIDTH, conversationAsidePlacement } from "@/lib/conversation-aside/placement";
import { useConversationAsideStore, type AsideCardId } from "@/lib/conversation-aside/conversation-aside-store";
import { cn } from "@/lib/utils";

import { CONVERSATION_ASIDE_CARDS, useConversationAsideHasContent } from "./aside-card-registry";
import { AsideCard } from "./aside-card";

const MODIFIERS = [restrictToVerticalAxis, restrictToParentElement];
const title = (id: unknown) => CONVERSATION_ASIDE_CARDS[id as AsideCardId].title;
const ANNOUNCEMENTS: Announcements = {
  onDragStart: ({ active }) => `已拿起「${title(active.id)}」`,
  onDragOver: ({ active, over }) => over ? `「${title(active.id)}」移到「${title(over.id)}」的位置` : undefined,
  onDragEnd: ({ active, over }) => over ? `已放下「${title(active.id)}」` : `已放下「${title(active.id)}」，位置不变`,
  onDragCancel: ({ active }) => `已取消拖动「${title(active.id)}」`,
};

/** 浮层最高占对话视口的这个比例，不盖住底部的输入框。 */
const FLOATING_MAX_HEIGHT_RATIO = 0.6;

/**
 * 会话侧栏：当前对话 Task 的状态摘要卡片栈（目前只有「后台任务」），任何卡片都没有内容时不渲染。它是对话画布上的附注，不是窗格：
 * AppLayout 经 `ConversationAsideContext` 把它交给线程视口，放在与内容列同一个网格里、粘在顶部
 * （见 `conversation-aside.tsx`），所以滚动条在视口最外侧，底部终端也不必给它让宽度。
 *
 * 摆放按对话视口宽度决定（`conversationAsidePlacement`）：`docked` 占网格右列，`floating` 与内容列叠在同一格
 * 右上角、不占宽度。固定时默认显示；浮层会盖住对话，只在用户点开（或定位条目）时出现，从不自动弹出，
 * 所以这个组件在对话页常驻，靠它量出的摆放决定显示哪一种，也把摆放告诉页头开关。
 * 浮层是非模态的：点对话区不会关闭它（侧栏里是要盯着看的进度）；焦点在侧栏内时按 Esc 关闭，
 * 拖动中按 Esc 是 dnd-kit 的取消拖动，不关闭侧栏；关闭后焦点交还页头按钮由调用方负责（见 `useConversationAside`）。
 * 高度按视口封顶，卡片多了在侧栏内部滚动。卡片顺序与折叠是全局偏好（`useConversationAsideStore`）。
 */
export function ConversationAsidePanel({ taskId, viewport, onClose }: { taskId: number; viewport: ElementSize; onClose: () => void }) {
  const placement = conversationAsidePlacement(viewport.width);
  const floating = placement === "floating";
  const visible = useConversationAsideStore((state) => (floating ? state.floatOpen : state.open));
  const setFloating = useConversationAsideStore((state) => state.setFloating);
  const hasContent = useConversationAsideHasContent(taskId);
  const order = useConversationAsideStore((state) => state.order);
  const setOrder = useConversationAsideStore((state) => state.setOrder);
  const [dragging, setDragging] = useState(false);
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  useEffect(() => setFloating(floating), [floating, setFloating]);

  const onDragEnd = ({ active, over }: DragEndEvent) => {
    setDragging(false);
    if (!over || active.id === over.id) return;
    const next = arrayMove(order, order.indexOf(active.id as AsideCardId), order.indexOf(over.id as AsideCardId));
    setOrder(next);
    void frontendLog("INFO", "conversation_aside_card_reordered", "会话侧栏卡片顺序已调整", { data: { taskId, order: next } });
  };
  const onKeyDown = (event: KeyboardEvent) => {
    if (event.key !== "Escape" || !floating || dragging) return;
    event.stopPropagation();
    onClose();
  };

  // 没有任何内容时不占列也不浮起：宽屏不留一整列「没有东西」，内容出现时侧栏随之出现（对话列因此平移一次）。
  if (!visible || !hasContent) return null;
  return (
    <aside
      aria-label="会话侧栏"
      data-conversation-aside={placement}
      style={{
        width: CONVERSATION_ASIDE_WIDTH,
        maxHeight: floating ? viewport.height * FLOATING_MAX_HEIGHT_RATIO : viewport.height,
      }}
      onKeyDown={onKeyDown}
      className={cn(
        "row-start-1 z-20 flex min-h-0 flex-col gap-2 self-start overflow-y-auto",
        floating
          ? "dock-panel bg-background sticky top-2 col-start-1 mt-2 mr-2 justify-self-end p-2"
          : "sticky top-0 col-start-2 py-2 pr-2",
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
          {order.map((id) => <AsideCard key={id} id={id} taskId={taskId} />)}
        </SortableContext>
      </DndContext>
    </aside>
  );
}

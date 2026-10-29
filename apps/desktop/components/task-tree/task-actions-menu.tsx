import { useRef } from "react";
import { MoreHorizontalIcon, PinIcon, PinOffIcon, Trash2Icon } from "lucide-react";
import { DropdownMenu } from "radix-ui";

import { Button } from "@/components/ui/button";
import { MENU_CONTENT_CLASS, MENU_ITEM_CLASS } from "@/components/ui/menu-tokens";
import { cn } from "@/lib/utils";
import type { TaskSidebarNode } from "./workspace-tree-model";

const ITEM_CLASS = cn(MENU_ITEM_CLASS, "flex items-center gap-2 select-none [&_svg]:size-4");

/**
 * 侧栏任务行的「…」菜单：置顶 / 取消置顶与删除。
 *
 * 收进一个菜单按钮而不是并排多个图标：尾栏宽度取时间与操作的较宽者，单按钮让标题宽度不随操作数变化。
 * 只转发意图，置顶存储与删除确认由调用方负责。
 */
export function TaskActionsMenu({
  item,
  tabIndex,
  onTogglePinned,
  onDelete,
}: {
  item: TaskSidebarNode;
  tabIndex: number;
  onTogglePinned: () => void;
  onDelete: () => void;
}) {
  const triggerRef = useRef<HTMLButtonElement>(null);
  // 选中项的动作推迟到菜单关闭、焦点回到触发按钮之后再执行：删除会打开确认框，
  // 若在菜单项上直接打开，确认框记住的回焦目标是随即卸载的菜单项，关闭后焦点会丢到 body。
  const pendingActionRef = useRef<(() => void) | null>(null);
  const title = item.task.full_title;
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <Button ref={triggerRef} variant="ghost" size="icon-sm" tabIndex={tabIndex} className="text-muted-foreground" aria-label={`更多操作：${title}`} title="更多操作">
          <MoreHorizontalIcon className="size-4" />
        </Button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          className={cn(MENU_CONTENT_CLASS, "min-w-36 text-popover-foreground")}
          onCloseAutoFocus={(event) => {
            const action = pendingActionRef.current;
            if (!action) return;
            pendingActionRef.current = null;
            event.preventDefault();
            triggerRef.current?.focus();
            action();
          }}
        >
          {item.pinnable && (
            <DropdownMenu.Item className={ITEM_CLASS} onSelect={() => { pendingActionRef.current = onTogglePinned; }}>
              {item.pinned ? <PinOffIcon /> : <PinIcon />}
              {item.pinned ? "取消置顶" : "置顶"}
            </DropdownMenu.Item>
          )}
          <DropdownMenu.Item className={cn(ITEM_CLASS, "text-destructive focus:bg-destructive/10")} onSelect={() => { pendingActionRef.current = onDelete; }}>
            <Trash2Icon />
            删除
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

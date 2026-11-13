import { useRef, type ReactNode } from "react";
import { MoreHorizontalIcon } from "lucide-react";
import { DropdownMenu } from "radix-ui";

import { Button } from "@/components/ui/button";
import { MENU_CONTENT_CLASS, MENU_ITEM_CLASS } from "@/components/ui/menu-tokens";
import { cn } from "@/lib/utils";

const ITEM_CLASS = cn(MENU_ITEM_CLASS, "flex items-center gap-2 select-none [&_svg]:size-4");

export type RowAction = {
  label: string;
  icon: ReactNode;
  destructive?: boolean;
  onSelect: () => void;
};

/**
 * 侧栏树行（工作区、会话）的「…」菜单：只负责触发按钮与菜单交互，菜单项由侧栏装配。
 *
 * 收进一个菜单按钮而不是并排多个图标：行尾宽度不随操作数变化，危险操作（删除）也不直接暴露在悬停位置。
 * 选中项的动作推迟到菜单关闭、焦点回到触发按钮之后再执行：删除会打开确认框，若在菜单项上直接打开，
 * 确认框记住的回焦目标是随即卸载的菜单项，关闭后焦点会丢到 body。
 */
export function RowActionsMenu({ label, tabIndex, actions }: {
  /** 所属行的名称，用于触发按钮的可访问名称「更多操作：xxx」。 */
  label: string;
  tabIndex: number;
  actions: RowAction[];
}) {
  const triggerRef = useRef<HTMLButtonElement>(null);
  const pendingActionRef = useRef<(() => void) | null>(null);
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <Button ref={triggerRef} variant="ghost" size="icon-sm" tabIndex={tabIndex} className="text-muted-foreground" aria-label={`更多操作：${label}`} title="更多操作">
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
          {actions.map((action) => (
            <DropdownMenu.Item
              key={action.label}
              className={cn(ITEM_CLASS, action.destructive && "text-destructive focus:bg-destructive/10")}
              onSelect={() => { pendingActionRef.current = action.onSelect; }}
            >
              {action.icon}
              {action.label}
            </DropdownMenu.Item>
          ))}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

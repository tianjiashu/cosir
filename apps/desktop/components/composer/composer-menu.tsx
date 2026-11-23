import { ChevronRightIcon } from "lucide-react";
import { useRef, type ComponentProps, type ReactNode } from "react";

import { CommandItem } from "@/components/ui/command";
import { PopoverContent } from "@/components/ui/popover";
import { cn } from "@/lib/utils";

/**
 * 输入框弹出菜单（「+」添加菜单、终端权限菜单）的弹层：在输入框上方弹出，里面放 cmdk 列表。
 *
 * 键盘操作的焦点规则只在这里：cmdk 只在焦点落在它内部时响应方向键与回车，弹层默认把焦点放在自己的外壳上，
 * 所以打开时把焦点交给菜单——有搜索框（`cmdk-input`）就给搜索框，没有就给列表根（`cmdk-root`）。翻到
 * 二级页时由那一页自己的搜索框 `autoFocus` 接住。调用方不再各自接焦点。
 */
export function ComposerMenuContent({ className, align = "start", ...props }: Omit<ComponentProps<typeof PopoverContent>, "initialFocus" | "side" | "sideOffset">) {
  const popup = useRef<HTMLDivElement>(null);
  return (
    <PopoverContent
      ref={popup}
      side="top"
      sideOffset={6}
      align={align}
      initialFocus={() => popup.current?.querySelector<HTMLElement>("[cmdk-input], [cmdk-root]") ?? true}
      className={cn("w-80 gap-0 rounded-xl p-0 shadow-lg", className)}
      {...props}
    />
  );
}

/**
 * 输入框弹出菜单（「+」添加菜单、终端权限菜单）的一行：图标、名称与一句说明，菜单里才写名称与说明，
 * 触发按钮因此可以只放图标或短词。
 *
 * 行尾只有一种标记：`drill` 进入二级页时是箭头；`checked` 给出时是单选菜单，当前项打勾（用 `CommandItem`
 * 自带的对勾，按 `data-checked` 显隐）。两者都不给时行尾空着。
 */
export function ComposerMenuRow({ icon, title, description, drill = false, checked, disabled = false, onSelect }: {
  icon: ReactNode;
  title: string;
  description: string;
  drill?: boolean;
  checked?: boolean;
  disabled?: boolean;
  onSelect: () => void;
}) {
  return (
    <CommandItem
      value={title}
      disabled={disabled}
      data-checked={checked === true ? "true" : undefined}
      onSelect={onSelect}
      className={cn("gap-2.5 rounded-lg px-2.5 py-2 [&_svg]:size-4", checked === undefined && "[&>svg:last-child]:hidden")}
    >
      <span className="text-muted-foreground shrink-0">{icon}</span>
      <span className="flex min-w-0 flex-1 flex-col">
        <span className="text-sm">{title}</span>
        <span className="text-muted-foreground truncate text-xs">{description}</span>
      </span>
      {drill && <ChevronRightIcon className="text-muted-foreground shrink-0" aria-hidden="true" />}
    </CommandItem>
  );
}

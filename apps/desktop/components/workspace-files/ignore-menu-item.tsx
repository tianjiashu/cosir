import type { KeyboardEvent } from "react";
import { ContextMenu } from "radix-ui";
import { EyeOffIcon } from "lucide-react";
import { MENU_ITEM_CLASS } from "@/components/ui/menu-tokens";
import { cn } from "@/lib/utils";

export function IgnoreMenuItem({
  paths,
  directory = false,
  pending,
  onIgnore,
}: {
  paths: string[];
  directory?: boolean;
  pending: boolean;
  onIgnore: (paths: string[]) => void;
}) {
  if (!paths.length) return null;
  return (
    <>
      <ContextMenu.Separator className="my-1 h-px bg-border" />
      <ContextMenu.Item
        disabled={pending}
        className={cn(MENU_ITEM_CLASS, "flex items-center gap-2 data-[disabled]:opacity-50")}
        onSelect={() => onIgnore(paths)}
      >
        <EyeOffIcon className="size-4 text-muted-foreground" />
        {paths.length > 1
          ? `忽略所选 ${paths.length} 项`
          : directory
            ? "忽略此目录"
            : "忽略此文件"}
      </ContextMenu.Item>
    </>
  );
}

/** WebView 对 Shift+F10 的原生行为不一致，统一交给现有 Radix 菜单处理。 */
export function openKeyboardContextMenu(
  event: KeyboardEvent<HTMLElement>,
  target: HTMLElement = event.currentTarget,
) {
  if (event.key !== "ContextMenu" && !(event.shiftKey && event.key === "F10"))
    return;
  event.preventDefault();
  event.stopPropagation();
  const bounds = target.getBoundingClientRect();
  target.dispatchEvent(
    new MouseEvent("contextmenu", {
      bubbles: true,
      cancelable: true,
      clientX: bounds.left + 20,
      clientY: bounds.top + bounds.height / 2,
    }),
  );
}

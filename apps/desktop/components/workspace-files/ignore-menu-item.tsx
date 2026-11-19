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

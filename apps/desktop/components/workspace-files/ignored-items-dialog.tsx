import { useState } from "react";
import { DropdownMenu } from "radix-ui";
import { EyeOffIcon, MoreHorizontalIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  useBrowserVisibility,
  useChangeBrowserVisibility,
} from "@/lib/workspace-files/visibility";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { MENU_CONTENT_CLASS, MENU_ITEM_CLASS } from "@/components/ui/menu-tokens";
import { cn } from "@/lib/utils";

export function FileBrowserMenu({ onManage }: { onManage: () => void }) {
  return (
    // 面板菜单不锁定整个桌面，避免开关菜单时切换主对话的滚动与交互状态。
    <DropdownMenu.Root modal={false}>
      <DropdownMenu.Trigger asChild>
        <Button variant="ghost" size="icon-sm" aria-label="文件浏览器更多选项">
          <MoreHorizontalIcon />
        </Button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          className={cn(MENU_CONTENT_CLASS, "min-w-40")}
        >
          <DropdownMenu.Item
            className={cn(MENU_ITEM_CLASS, "flex items-center gap-2")}
            onSelect={onManage}
          >
            <EyeOffIcon className="size-4 text-muted-foreground" />
            管理忽略项
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

export function IgnoredItemsDialog({
  workspaceId,
  open,
  onOpenChange,
}: {
  workspaceId: number;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const settings = useBrowserVisibility(workspaceId, open);
  const { change, pending } = useChangeBrowserVisibility(workspaceId);
  const [filter, setFilter] = useState("");
  const paths = settings.data?.ignored_paths ?? [];
  const filtered = paths.filter((path) =>
    path.toLocaleLowerCase().includes(filter.toLocaleLowerCase()),
  );
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>管理忽略项</DialogTitle>
        </DialogHeader>
        {settings.isPending ? (
          <p role="status" className="text-muted-foreground">
            正在读取忽略项…
          </p>
        ) : settings.isError ? (
          <div role="alert" className="space-y-2">
            <p>{userErrorMessage(settings.error, "忽略项读取失败")}</p>
            <Button
              size="sm"
              variant="outline"
              onClick={() => void settings.refetch()}
            >
              重试
            </Button>
          </div>
        ) : paths.length === 0 ? (
          <p className="py-6 text-center text-muted-foreground">
            还没有忽略任何文件或目录
          </p>
        ) : (
          <>
            <input
              aria-label="筛选忽略项"
              placeholder="筛选路径"
              value={filter}
              onChange={(event) => setFilter(event.target.value)}
              className="focus-ring w-full rounded-md border bg-background px-3 py-2"
            />
            <ul className="max-h-[45vh] overflow-y-auto divide-y">
              {filtered.map((path) => (
                <li key={path} className="flex items-center gap-3 py-2">
                  <span
                    title={path}
                    className="min-w-0 flex-1 break-all text-sm"
                  >
                    {path}
                  </span>
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={pending}
                    aria-label={`恢复显示 ${path}`}
                    onClick={() => void change([path], false)}
                  >
                    恢复显示
                  </Button>
                </li>
              ))}
            </ul>
            {filtered.length === 0 && (
              <p className="text-muted-foreground">没有匹配的忽略项</p>
            )}
            <p className="text-xs text-muted-foreground">
              共 {paths.length}{" "}
              项。目录规则包含其子目录；恢复子项前，请先恢复被忽略的父目录。
            </p>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

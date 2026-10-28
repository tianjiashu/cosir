import { useRef, useState, type ReactNode } from "react";
import { FolderTreeIcon, MoreHorizontalIcon } from "lucide-react";
import { copyText } from "@/lib/clipboard";
import { workspacePathText } from "@/lib/workspace-files/store";
import { Button } from "@/components/ui/button";
import { TooltipIconButton } from "@/components/tooltip-icon-button";
import { Popover, PopoverContent, PopoverTitle, PopoverTrigger } from "@/components/ui/popover";
import type { FileInfo } from "@/lib/api/workspace-files";

export type ReadingNavigation = {
  path: string;
  rootPath: string;
  treeVisible: boolean;
  toggleTree: () => void;
  revealPath: () => void;
};

/** 阅读器共用一条操作栏；预览只提供自身控件，不承担目录状态或文件路径操作。 */
export function ReadingToolbar({ navigation, info, view, actions, options }: {
  navigation: ReadingNavigation;
  info?: FileInfo;
  view?: ReactNode;
  actions?: ReactNode;
  options?: ReactNode;
}) {
  const [pathOpen, setPathOpen] = useState(false);
  const pathTrigger = useRef<HTMLButtonElement>(null);
  const copy = (absolute: boolean) => copyText(workspacePathText(navigation.rootPath, navigation.path, absolute), {
    success: "路径已复制",
    logEvent: "workspace_path_copy_failed",
    logData: { absolute, source: "reading_toolbar" },
  });
  return (
    <div className="flex shrink-0 items-center gap-1 border-b px-2 py-1 text-xs" aria-label="文件阅读工具栏">
      <TooltipIconButton tooltip={navigation.treeVisible ? "收起目录" : "浏览目录与搜索"}
        aria-label="切换目录面板" aria-pressed={navigation.treeVisible} className="size-8 shrink-0" onClick={navigation.toggleTree}>
        <FolderTreeIcon />
      </TooltipIconButton>
      <Popover open={pathOpen} onOpenChange={setPathOpen}>
        <PopoverTrigger render={<button ref={pathTrigger} type="button" className="min-w-0 flex-1 truncate py-2 text-left text-muted-foreground focus-visible:outline-2 focus-visible:outline-ring" />}
          title={navigation.path} aria-label={`文件路径 ${navigation.path}`}>
          {navigation.path}
        </PopoverTrigger>
        <PopoverContent align="start" finalFocus={() => pathTrigger.current?.getClientRects().length ? pathTrigger.current : false}>
          <PopoverTitle className="break-all">{navigation.path}</PopoverTitle>
          <Button size="sm" variant="ghost" onClick={() => void copy(false)}>复制相对路径</Button>
          <Button size="sm" variant="ghost" onClick={() => void copy(true)}>复制绝对路径</Button>
          <Button size="sm" variant="ghost" onClick={() => { setPathOpen(false); navigation.revealPath(); }}>在目录中定位当前文件</Button>
        </PopoverContent>
      </Popover>
      {view}
      <div className="flex shrink-0 items-center gap-1">{actions}</div>
      {info && (
        <Popover>
          <PopoverTrigger render={<Button size="icon-sm" variant="ghost" />} aria-label="更多阅读选项" title="更多阅读选项">
            <MoreHorizontalIcon />
          </PopoverTrigger>
          <PopoverContent align="end">
            <PopoverTitle>阅读选项</PopoverTitle>
            {options}
            <div className="border-t pt-2 text-xs text-muted-foreground">
              <p>文件信息 · {info.size.toLocaleString()} 字节</p>
              <p>{new Date(info.modified_ns / 1_000_000).toLocaleString()}</p>
            </div>
          </PopoverContent>
        </Popover>
      )}
    </div>
  );
}

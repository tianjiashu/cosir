import { Tabs, DropdownMenu } from "radix-ui";
import { PinIcon, XIcon, ListIcon, TriangleAlertIcon } from "lucide-react";
import { useCallback, useLayoutEffect, useRef, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { getFileInfo } from "@/lib/api/workspace-files";
import { queryKeys } from "@/lib/query/query-keys";
import { ReadingSearchContext } from "@/lib/workspace-files/reading-search";
import { TooltipIconButton } from "@/components/tooltip-icon-button";
import { fileSession, useFilesStore } from "@/lib/workspace-files/store";
import { WorkspaceFileIcon } from "./file-icon";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { fileTabLabel } from "@/lib/workspace-files/tab-label";
import { cn } from "@/lib/utils";
import { PANEL_HEADER_CLASS, panelTabClass } from "@/components/ui/panel-tokens";

/** 仅观察阅读器已产生的元数据错误，不为非活动标签发起额外读取。 */
function FileTabError({ workspaceId, path }: { workspaceId: number; path: string; }) {
  const { isError } = useQuery({
    queryKey: queryKeys.fileInfo(workspaceId, path),
    queryFn: ({ signal }) => getFileInfo(workspaceId, path, signal),
    enabled: false,
  });
  return isError ? <span title="文件读取失败" role="status" className="shrink-0"><TriangleAlertIcon aria-hidden="true" className="size-3.5" /><span className="sr-only">文件读取失败</span></span> : null;
}

/** Tabs 管理焦点与 ARIA 关联；内容只挂载活动文件，阅读偏好由 workspace 会话持有。 */
export function FileReadingTabs({
  workspaceId,
  children,
  onEmptyClose,
  onBrowse,
}: {
  workspaceId: number;
  children: ReactNode;
  onEmptyClose: () => void;
  onBrowse: () => void;
}) {
  const session = useFilesStore((s) => fileSession(s.sessions, workspaceId));
  const open = useFilesStore((s) => s.open);
  const close = useFilesStore((s) => s.close);
  const root = useRef<HTMLDivElement>(null);
  const restoreFocus = useRef(false);
  const searchAction = useRef<(() => void) | null>(null);
  const registerSearch = useCallback((action: (() => void) | null) => {
    searchAction.current = action;
  }, []);
  useLayoutEffect(() => {
    root.current?.querySelector('[role="tab"][aria-selected="true"]')?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [session.active]);
  useLayoutEffect(() => {
    if (!restoreFocus.current) return;
    restoreFocus.current = false;
    if (session.active) {
      root.current
        ?.querySelector<HTMLButtonElement>('[role="tab"][aria-selected="true"]')
        ?.focus();
    } else onEmptyClose();
  }, [session.active, session.tabs, onEmptyClose]);
  return (
    <ReadingSearchContext.Provider value={registerSearch}>
      <Tabs.Root
        ref={root}
        tabIndex={-1}
        onKeyDownCapture={(event) => {
          if (
            (event.ctrlKey || event.metaKey) &&
            event.key.toLowerCase() === "f" &&
            searchAction.current
          ) {
            event.preventDefault();
            event.stopPropagation();
            searchAction.current();
          }
        }}
        value={session.active ?? ""}
        onValueChange={(value) => open(workspaceId, String(value))}
        className="flex min-h-0 min-w-0 flex-1 flex-col"
      >
        <div className={cn(PANEL_HEADER_CLASS, "min-w-0")}>
          <Tabs.List
            aria-label="文件预览标签"
            className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto"
          >
            {session.tabs.map((tab) => (
              <div key={tab.path} className={panelTabClass(tab.path === session.active, "max-w-64")}>
                <Tooltip disableHoverablePopup>
                  <TooltipTrigger render={<Tabs.Trigger
                    value={tab.path}
                    aria-label={fileTabLabel(tab.path, session.tabs.map(item => item.path))}
                    className="flex min-w-0 items-center gap-1.5 self-stretch focus-visible:outline-2 focus-visible:outline-ring"
                    onDoubleClick={() => open(workspaceId, tab.path, true)}
                  />} >
                    <WorkspaceFileIcon name={tab.path.split("/").at(-1)!} />
                    <span className={cn("truncate", !tab.pinned && "italic")}>
                      {fileTabLabel(tab.path, session.tabs.map(item => item.path))}
                    </span>
                    <FileTabError workspaceId={workspaceId} path={tab.path} />
                  </TooltipTrigger>
                  <TooltipContent side="top">{tab.path}{!tab.pinned && " · 临时预览，打开其他文件会替换；双击固定"}</TooltipContent>
                </Tooltip>
                {!tab.pinned && (
                  <TooltipIconButton
                    tooltip="固定预览，保留此标签"
                    aria-label={`固定 ${tab.path}`}
                    className="size-6 shrink-0"
                    onClick={() => open(workspaceId, tab.path, true)}
                  >
                    <PinIcon className="size-3.5" />
                  </TooltipIconButton>
                )}
                <TooltipIconButton
                  tooltip="关闭文件"
                  aria-label={`关闭文件 ${tab.path}`}
                  className="size-6 shrink-0"
                  onClick={() => {
                    restoreFocus.current = true;
                    close(workspaceId, tab.path);
                  }}
                >
                  <XIcon className="size-3.5" />
                </TooltipIconButton>
              </div>
            ))}
          </Tabs.List>
          {session.tabs.length > 0 && <DropdownMenu.Root>
            <DropdownMenu.Trigger asChild><Button size="icon-sm" variant="ghost" className="shrink-0 self-center" aria-label="全部文件" title="全部文件"><ListIcon /></Button></DropdownMenu.Trigger>
            <DropdownMenu.Portal>
              <DropdownMenu.Content align="end" className="z-50 max-h-80 max-w-[calc(100vw-1rem)] overflow-auto rounded-md border bg-popover p-1 text-sm shadow-md">
                <DropdownMenu.RadioGroup value={session.active ?? ""} onValueChange={path => open(workspaceId, path)}>
                  {session.tabs.map(tab => <DropdownMenu.RadioItem key={tab.path} value={tab.path} className="cursor-default rounded px-2 py-1.5 outline-none focus:bg-muted data-[state=checked]:bg-muted">
                    <span className="break-all">{tab.path}</span>{!tab.pinned && <span className="ml-2 text-xs text-muted-foreground">临时预览</span>}
                  </DropdownMenu.RadioItem>)}
                </DropdownMenu.RadioGroup>
              </DropdownMenu.Content>
            </DropdownMenu.Portal>
          </DropdownMenu.Root>}
        </div>
        {session.tabs.map((tab) => (
          <Tabs.Content
            key={tab.path}
            value={tab.path}
            forceMount
            className="flex min-h-0 flex-1 flex-col data-[state=inactive]:hidden"
          >
            {tab.path === session.active ? children : null}
          </Tabs.Content>
        ))}
        {!session.active && (
          <div className="flex flex-1 flex-col items-center justify-center gap-2 p-6 text-sm text-muted-foreground">
            选择文件以查看内容
            <Button size="sm" variant="ghost" onClick={onBrowse}>浏览文件</Button>
          </div>
        )}
      </Tabs.Root>
    </ReadingSearchContext.Provider>
  );
}

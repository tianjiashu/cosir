import { Tabs, DropdownMenu, ContextMenu } from "radix-ui";
import { XIcon, ChevronDownIcon, TriangleAlertIcon } from "lucide-react";
import { useCallback, useLayoutEffect, useRef, useState, type ReactNode, type Ref } from "react";
import { useQuery } from "@tanstack/react-query";
import { ReadingCommandsContext, type ReadingCommand, type ReadingCommandRegistry } from "@/lib/workspace-files/reading-commands";
import { TooltipIconButton } from "@/components/ui/tooltip-icon-button";
import { fileSession, useFilesStore } from "@/lib/workspace-files/store";
import { WorkspaceFileIcon } from "./file-icon";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { fileTabLabel } from "@/lib/workspace-files/tab-label";
import { cn } from "@/lib/utils";
import { panelTabClass } from "@/components/ui/panel-tokens";
import { MENU_CONTENT_CLASS, MENU_ITEM_CLASS } from "@/components/ui/menu-tokens";
import { fileInfoQueryOptions } from "@/lib/query/query-options";
import { fileReadProblem } from "@/lib/workspace-files/read-problem";

/**
 * 标签的文件名与读取状态：只观察阅读器已产生的元数据查询，不为非活动标签发起额外读取。文件不存在时名字划线
 * 变灰（与 VS Code 对已删除文件的处理一致，这是状态不是故障）；真正的读取故障才挂警告图标。
 */
function FileTabName({ workspaceId, path, label, pinned }: { workspaceId: number; path: string; label: string; pinned: boolean }) {
  const { error } = useQuery({
    ...fileInfoQueryOptions(workspaceId, path),
    enabled: false,
  });
  const problem = error ? fileReadProblem(error) : null;
  return (
    <>
      <span className={cn("truncate", !pinned && "italic", problem === "missing-file" && "text-muted-foreground line-through")}>{label}</span>
      {problem !== null && problem !== "missing-file" && (
        <span title="文件读取失败" role="status" className="shrink-0"><TriangleAlertIcon aria-hidden="true" className="size-3.5" /><span className="sr-only">文件读取失败</span></span>
      )}
    </>
  );
}

/** 阅读器快捷命令的按键（小写），与 `APP_SHORTCUTS` 的 findInFile / addToChat 对应。 */
const COMMAND_KEYS: Record<string, ReadingCommand> = { f: "find", l: "addToChat" };

/**
 * 文件标签的根：Radix Tabs 管理焦点与 ARIA 关联，并承载阅读器快捷命令的按键边界（`reading-commands`）。
 *
 * 标签条在面板头部、内容在面板正文，二者被 `DockFrame` 隔开，所以根以 `display: contents` 包住整个面板，
 * 不参与布局。快捷键只在面板内生效，不监听整个窗口。
 */
export function FileReadingTabs({
  workspaceId,
  ref,
  children,
}: {
  workspaceId: number;
  ref?: Ref<HTMLDivElement>;
  children: ReactNode;
}) {
  const active = useFilesStore((s) => fileSession(s.sessions, workspaceId).active);
  const open = useFilesStore((s) => s.open);
  const actions = useRef<Partial<Record<ReadingCommand, () => void>>>({});
  const register = useCallback<ReadingCommandRegistry>((command, action) => {
    if (action) actions.current[command] = action;
    else delete actions.current[command];
  }, []);
  return (
    <ReadingCommandsContext.Provider value={register}>
      <Tabs.Root
        ref={ref}
        data-testid="workspace-files-surface"
        onKeyDownCapture={(event) => {
          if (!(event.ctrlKey || event.metaKey) || event.altKey || event.shiftKey) return;
          const action = actions.current[COMMAND_KEYS[event.key.toLowerCase()]];
          if (!action) return;
          event.preventDefault();
          event.stopPropagation();
          action();
        }}
        value={active ?? ""}
        onValueChange={(value) => open(workspaceId, String(value))}
        className="contents"
      >
        {children}
      </Tabs.Root>
    </ReadingCommandsContext.Provider>
  );
}

/**
 * 面板头部的标签条与「全部文件」溢出菜单。临时预览标签用斜体表示，双击或右键「固定」保留它；
 * 关闭标签后焦点回到新的活动标签，没有标签时交给 `onEmptyClose`。溢出菜单只在标签条放不下时出现。
 */
export function FileTabBar({
  workspaceId,
  onEmptyClose,
}: {
  workspaceId: number;
  onEmptyClose: () => void;
}) {
  const session = useFilesStore((s) => fileSession(s.sessions, workspaceId));
  const open = useFilesStore((s) => s.open);
  const close = useFilesStore((s) => s.close);
  const list = useRef<HTMLDivElement>(null);
  const restoreFocus = useRef(false);
  const [overflow, setOverflow] = useState(false);
  // 标签增减改变内容宽度，面板拖宽拖窄改变容器宽度：两种变化都重新比较。
  useLayoutEffect(() => {
    const element = list.current;
    if (!element) return;
    const measure = () => setOverflow(element.scrollWidth > element.clientWidth + 1);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [session.tabs]);
  const closeTab = (path: string) => {
    restoreFocus.current = true;
    close(workspaceId, path);
  };
  useLayoutEffect(() => {
    list.current?.querySelector('[role="tab"][aria-selected="true"]')?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [session.active]);
  useLayoutEffect(() => {
    if (!restoreFocus.current) return;
    restoreFocus.current = false;
    if (session.active) {
      list.current
        ?.querySelector<HTMLButtonElement>('[role="tab"][aria-selected="true"]')
        ?.focus();
    } else onEmptyClose();
  }, [session.active, session.tabs, onEmptyClose]);
  const paths = session.tabs.map((item) => item.path);
  return (
    <>
      <Tabs.List
        ref={list}
        aria-label="文件预览标签"
        className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto"
      >
        {session.tabs.map((tab) => (
          <ContextMenu.Root key={tab.path}>
            <ContextMenu.Trigger asChild>
              <div className={panelTabClass(tab.path === session.active, "max-w-64")}>
                <Tooltip disableHoverablePopup>
                  <TooltipTrigger render={<Tabs.Trigger
                    value={tab.path}
                    aria-label={fileTabLabel(tab.path, paths)}
                    className="flex min-w-0 items-center gap-1.5 self-stretch focus-visible:outline-2 focus-visible:outline-ring"
                    onDoubleClick={() => open(workspaceId, tab.path, true)}
                  />} >
                    <WorkspaceFileIcon name={tab.path.split("/").at(-1)!} />
                    <FileTabName workspaceId={workspaceId} path={tab.path} label={fileTabLabel(tab.path, paths)} pinned={tab.pinned} />
                  </TooltipTrigger>
                  <TooltipContent side="bottom">{tab.path}{!tab.pinned && " · 临时预览，打开其他文件会替换；双击或右键固定"}</TooltipContent>
                </Tooltip>
                <TooltipIconButton
                  tooltip="关闭文件"
                  aria-label={`关闭文件 ${tab.path}`}
                  className="size-6 shrink-0 opacity-60 hover:opacity-100"
                  onClick={() => closeTab(tab.path)}
                >
                  <XIcon className="size-3" />
                </TooltipIconButton>
              </div>
            </ContextMenu.Trigger>
            <ContextMenu.Portal>
              <ContextMenu.Content className={cn(MENU_CONTENT_CLASS, "min-w-32")}>
                {!tab.pinned && (
                  <ContextMenu.Item className={MENU_ITEM_CLASS} onSelect={() => open(workspaceId, tab.path, true)}>
                    固定标签
                  </ContextMenu.Item>
                )}
                <ContextMenu.Item className={MENU_ITEM_CLASS} onSelect={() => closeTab(tab.path)}>
                  关闭
                </ContextMenu.Item>
              </ContextMenu.Content>
            </ContextMenu.Portal>
          </ContextMenu.Root>
        ))}
      </Tabs.List>
      {overflow && <DropdownMenu.Root>
        <DropdownMenu.Trigger asChild><Button size="icon-sm" variant="ghost" className="shrink-0 self-center" aria-label="全部文件" title="全部文件"><ChevronDownIcon /></Button></DropdownMenu.Trigger>
        <DropdownMenu.Portal>
          <DropdownMenu.Content align="end" className={cn(MENU_CONTENT_CLASS, "max-h-80 max-w-[calc(100vw-1rem)] overflow-auto")}>
            <DropdownMenu.RadioGroup value={session.active ?? ""} onValueChange={path => open(workspaceId, path)}>
              {session.tabs.map(tab => <DropdownMenu.RadioItem key={tab.path} value={tab.path} className={cn(MENU_ITEM_CLASS, "data-[state=checked]:bg-muted")}>
                <span className="break-all">{tab.path}</span>{!tab.pinned && <span className="ml-2 text-xs text-muted-foreground">临时预览</span>}
              </DropdownMenu.RadioItem>)}
            </DropdownMenu.RadioGroup>
          </DropdownMenu.Content>
        </DropdownMenu.Portal>
      </DropdownMenu.Root>}
    </>
  );
}

/** 标签内容只挂载活动文件；阅读偏好与位置由工作区会话持有，切回时恢复。 */
export function FileTabPanels({
  workspaceId,
  onBrowse,
  children,
}: {
  workspaceId: number;
  onBrowse: () => void;
  children: ReactNode;
}) {
  const session = useFilesStore((s) => fileSession(s.sessions, workspaceId));
  return (
    <>
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
    </>
  );
}

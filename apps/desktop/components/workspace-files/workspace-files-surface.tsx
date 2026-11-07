import { useOpenWorkspacePath } from "@/components/workspace-references/use-open-workspace-path";
import { useElementSize } from "@/hooks/use-element-size";
import { useBrowserVisibility } from "@/lib/workspace-files/visibility";
import { FileBrowserMenu, IgnoredItemsDialog } from "./ignored-items-dialog";
import { ReferencePreview } from "@/components/workspace-references/reference-preview";
import { useLayoutEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  FolderTreeIcon,
  Maximize2Icon,
  Minimize2Icon,
  RefreshCwIcon,
  SearchIcon,
  XIcon,
} from "lucide-react";
import { DockFrame, type DockSizing } from "@/components/layout/dock-frame";
import type { Workspace } from "@/lib/api/workspaces";
import { cn } from "@/lib/utils";
import { InputGroup, InputGroupAddon } from "@/components/ui/input-group";
import { PanelResizeHandle } from "@/components/ui/panel-resize-handle";
import { TooltipIconButton } from "@/components/ui/tooltip-icon-button";
import {
  fileSession,
  parentPath,
  parentsOf,
  useFilesStore,
} from "@/lib/workspace-files/store";
import {
  ResourceWatchContext,
  useResourceWatchTargets,
} from "@/lib/workspace-files/resource-watch";
import { useFileWatch } from "@/lib/workspace-files/use-file-watch";
import { queryKeys } from "@/lib/query/query-keys";
import { FileTree } from "./file-tree";
import { FileSearch } from "./file-search";
import { FilePreview } from "./file-preview";
import { FileReadingTabs, FileTabBar, FileTabPanels } from "./file-reading-tabs";
import { READING_ROW_CLASS, ReadingToolbar } from "./reading-toolbar";

/**
 * 文件面板：当前工作区的文件浏览器，整块渲染停靠外框。
 *
 * 头部一行是「目录开关 · 文件标签 · 全部文件 · 铺满 · 收起」；正文第一行左右对齐：目录栏放文件名搜索，
 * 阅读栏放各预览器的工具栏。面板够宽（≥650px）时目录与阅读并排，否则二者轮流占满，目录开关在两者间切换。
 * 浏览状态（打开的文件、展开的目录、目录宽度）按工作区存在 `lib/workspace-files/store`，收起面板不丢。
 */
export function WorkspaceFilesSurface({
  workspace,
  sizing,
  maximized,
  onToggleMaximized,
  onClose,
}: {
  workspace: Workspace;
  sizing: DockSizing;
  maximized: boolean;
  onToggleMaximized: () => void;
  onClose: () => void;
}) {
  const { workspace_id: workspaceId, root_path: rootPath, name: workspaceName } = workspace;
  const visibility = useBrowserVisibility(workspaceId);
  const [manageIgnored, setManageIgnored] = useState(false);
  const revision = visibility.data?.revision ?? "loading";
  const session = useFilesStore((s) => fileSession(s.sessions, workspaceId));
  const patch = useFilesStore((s) => s.patch);
  const opener = useOpenWorkspacePath();
  const client = useQueryClient();
  // 文件名搜索的文字与已提交的请求都在工作区会话里：用户可从文件名提及显式发起查找。
  const query = session.searchText;
  const searchRequest = session.searchRequest;
  const searchFilename = useFilesStore((s) => s.searchFilename);
  const [reveal, setReveal] = useState(0);
  const [shell, { width: availableWidth }] = useElementSize<HTMLDivElement>();
  const wide = availableWidth >= 650;
  const treeMaxWidth = Math.max(160, Math.min(420, availableWidth - 324));
  const treeWidth = Math.min(session.treeWidth, treeMaxWidth);
  const filenameSearch = useRef<HTMLInputElement>(null);
  const root = useRef<HTMLDivElement>(null);
  const treeToggle = useRef<HTMLButtonElement>(null);
  const runFilenameSearch = () => {
    const committedQuery = query.trim();
    if (committedQuery) searchFilename(workspaceId, committedQuery);
  };
  const clearFilenameSearch = () => patch(workspaceId, { searchText: "", searchRequest: null });

  const treeVisible = wide
    ? session.treeVisible
    : session.browsing || !session.active;
  const readingVisible = wide || !treeVisible;
  const resources = useResourceWatchTargets();
  useFileWatch(workspaceId, [
    ...session.expanded,
    ...resources.paths,
    ...session.tabs.map((tab) => parentPath(tab.path)),
  ]);
  const revealPath = () => {
    if (!session.active) return;
    patch(workspaceId, {
      expanded: [
        ...new Set([...session.expanded, ...parentsOf(session.active)]),
      ],
      treeVisible: true,
      browsing: true,
      searchText: "",
      searchRequest: null,
    });
    setReveal((v) => v + 1);
  };
  const toggleTree = () =>
    patch(
      workspaceId,
      wide ? { treeVisible: !treeVisible } : { browsing: !treeVisible },
    );
  const previousReadingVisible = useRef(readingVisible);
  // 只在切回目录时转移焦点；初次挂载目录不能抢走对话输入框的焦点。
  useLayoutEffect(() => {
    const wasReading = previousReadingVisible.current;
    previousReadingVisible.current = readingVisible;
    if (wide || wasReading === readingVisible) return;
    if (readingVisible)
      root.current
        ?.querySelector<HTMLButtonElement>('[role="tab"][aria-selected="true"]')
        ?.focus();
    else if (wasReading) filenameSearch.current?.focus();
  }, [wide, readingVisible]);
  return (
    <ResourceWatchContext.Provider value={resources.register}>
      <FileReadingTabs workspaceId={workspaceId} ref={root}>
        <DockFrame
          label="文件"
          sizing={sizing}
          fill={maximized}
          header={(
            <>
              <TooltipIconButton
                ref={treeToggle}
                tooltip={treeVisible ? "收起目录" : "浏览目录与搜索"}
                aria-label="切换目录面板"
                aria-pressed={treeVisible}
                disabled={!wide && !session.active}
                className={cn("size-7 shrink-0", treeVisible && "bg-muted text-foreground")}
                onClick={toggleTree}
              >
                <FolderTreeIcon />
              </TooltipIconButton>
              <FileTabBar
                workspaceId={workspaceId}
                onEmptyClose={() => (filenameSearch.current ?? treeToggle.current)?.focus()}
              />
              <TooltipIconButton tooltip={maximized ? "还原" : "铺满"} className="size-7 shrink-0"
                aria-label={maximized ? "还原文件面板" : "铺满文件面板"} onClick={onToggleMaximized}>
                {maximized ? <Minimize2Icon /> : <Maximize2Icon />}
              </TooltipIconButton>
              <TooltipIconButton tooltip="收起文件" className="size-7 shrink-0" aria-label="收起文件" onClick={onClose}>
                <XIcon />
              </TooltipIconButton>
            </>
          )}
        >
          <div ref={shell} className="flex min-h-0 flex-1">
            <IgnoredItemsDialog
              workspaceId={workspaceId}
              open={manageIgnored}
              onOpenChange={setManageIgnored}
            />
            {treeVisible && (
              <div
                className="flex min-h-0 min-w-0 flex-col"
                style={{
                  width: wide ? treeWidth : "100%",
                  flexShrink: 0,
                }}
              >
                {/* 工作区名不单独成行：目录根节点已显示名称，悬停可见完整路径。 */}
                <form
                  className={cn(READING_ROW_CLASS, "gap-0.5")}
                  onSubmit={(event) => {
                    event.preventDefault();
                    runFilenameSearch();
                  }}
                >
                  <InputGroup className="mr-1 h-8 flex-1">
                    <input
                      ref={filenameSearch}
                      data-slot="input-group-control"
                      className="min-w-0 flex-1 bg-transparent text-sm outline-none"
                      aria-label="搜索工作区文件名"
                      placeholder="搜索文件名或路径，Enter 查找"
                      value={query}
                      onChange={(e) => patch(workspaceId, { searchText: e.target.value, searchRequest: null })}
                      onKeyDown={(e) => {
                        if (e.key === "Escape") clearFilenameSearch();
                      }}
                    />
                    <InputGroupAddon>
                      <SearchIcon />
                    </InputGroupAddon>
                    {query && (
                      <InputGroupAddon align="inline-end">
                        <TooltipIconButton
                          type="button"
                          tooltip="清空搜索，返回目录"
                          aria-label="清空文件名搜索"
                          className="size-6"
                          onClick={clearFilenameSearch}
                        >
                          <XIcon />
                        </TooltipIconButton>
                      </InputGroupAddon>
                    )}
                  </InputGroup>
                  <TooltipIconButton
                    tooltip="刷新工作区文件"
                    aria-label="刷新工作区文件"
                    className="size-8"
                    onClick={() =>
                      void client.invalidateQueries({
                        queryKey: queryKeys.files(workspaceId),
                      })
                    }
                  >
                    <RefreshCwIcon />
                  </TooltipIconButton>
                  <FileBrowserMenu onManage={() => setManageIgnored(true)} />
                </form>
                {query.trim() && searchRequest ? (
                  <FileSearch
                    key={`${searchRequest.id}:${revision}`}
                    workspaceId={workspaceId}
                    query={searchRequest.query}
                    navigation={searchRequest.navigation !== undefined}
                    open={(path) => {
                      patch(workspaceId, { searchRequest: { ...searchRequest, navigation: undefined } });
                      void opener?.openFile(path, { lines: searchRequest.navigation?.lines, pinned: false });
                    }}
                    clear={clearFilenameSearch}
                    onManageIgnored={() => setManageIgnored(true)}
                  />
                ) : query.trim() ? (
                  <div
                    className="p-4 text-sm text-muted-foreground"
                    role="status"
                  >
                    按 Enter 开始查找。
                  </div>
                ) : (
                  <FileTree
                    workspaceId={workspaceId}
                    rootName={workspaceName}
                    rootPath={rootPath}
                    reveal={reveal}
                    revision={revision}
                    onManageIgnored={() => setManageIgnored(true)}
                  />
                )}
              </div>
            )}
            {wide && treeVisible && (
              <PanelResizeHandle
                label="调整文件目录宽度"
                value={treeWidth}
                min={160}
                max={treeMaxWidth}
                onChange={(width) => patch(workspaceId, { treeWidth: width })}
              />
            )}
            <div
              className={
                readingVisible ? "flex min-h-0 min-w-0 flex-1 flex-col" : "hidden"
              }
            >
              <FileTabPanels
                workspaceId={workspaceId}
                onBrowse={() => patch(workspaceId, { treeVisible: true, browsing: true })}
              >
                {session.active && (
                  session.reference ? (
                    <>
                      <ReadingToolbar navigation={{ path: session.active, rootPath, revealPath }} />
                      <ReferencePreview
                        reference={session.reference}
                        workspaceId={workspaceId}
                      />
                    </>
                  ) : (
                    <FilePreview
                      key={session.active}
                      workspaceId={workspaceId}
                      path={session.active}
                      navigation={{ path: session.active, rootPath, revealPath }}
                    />
                  )
                )}
              </FileTabPanels>
            </div>
          </div>
        </DockFrame>
      </FileReadingTabs>
    </ResourceWatchContext.Provider>
  );
}

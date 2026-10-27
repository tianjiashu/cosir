import { useBrowserVisibility } from "@/lib/workspace-files/visibility";
import { FileBrowserMenu, IgnoredItemsDialog } from "./ignored-items-dialog";
import { ReferencePreview } from "@/components/workspace-references/reference-preview";
import { useLayoutEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  FolderTreeIcon,
  RefreshCwIcon,
  ChevronsDownUpIcon,
  SearchIcon,
  XIcon,
} from "lucide-react";
import { InputGroup, InputGroupAddon } from "@/components/ui/input-group";
import { PanelResizeHandle } from "@/components/ui/panel-resize-handle";
import { TooltipIconButton } from "@/components/tooltip-icon-button";
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
import { FileReadingTabs } from "./file-reading-tabs";
import { ReadingToolbar } from "./reading-toolbar";

export function WorkspaceFilesSurface({
  workspaceId,
  rootPath,
  workspaceName,
}: {
  workspaceId: number;
  rootPath: string;
  workspaceName: string;
}) {
  const visibility = useBrowserVisibility(workspaceId);
  const [manageIgnored, setManageIgnored] = useState(false);
  const revision = visibility.data?.revision ?? "loading";
  const session = useFilesStore((s) => fileSession(s.sessions, workspaceId));
  const patch = useFilesStore((s) => s.patch);
  const open = useFilesStore((s) => s.open);
  const client = useQueryClient();
  const [query, setQuery] = useState("");
  const [searchRequest, setSearchRequest] = useState<{
    id: number;
    query: string;
  } | null>(null);
  const [reveal, setReveal] = useState(0);
  const [availableWidth, setAvailableWidth] = useState(0);
  const wide = availableWidth >= 650;
  const treeMaxWidth = Math.max(160, Math.min(420, availableWidth - 324));
  const treeWidth = Math.min(session.treeWidth, treeMaxWidth);
  const shell = useRef<HTMLDivElement>(null);
  const filenameSearch = useRef<HTMLInputElement>(null);
  const searchRequestId = useRef(0);
  const runFilenameSearch = () => {
    const committedQuery = query.trim();
    if (!committedQuery) return;
    setSearchRequest({
      id: ++searchRequestId.current,
      query: committedQuery,
    });
  };
  const clearFilenameSearch = () => {
    setQuery("");
    setSearchRequest(null);
  };
  useLayoutEffect(() => {
    const observer = new ResizeObserver(([entry]) =>
      setAvailableWidth(entry.contentRect.width),
    );
    observer.observe(shell.current!);
    return () => observer.disconnect();
  }, []);
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
    });
    setQuery("");
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
      shell.current
        ?.querySelector<HTMLButtonElement>('[role="tab"][aria-selected="true"]')
        ?.focus();
    else if (wasReading) filenameSearch.current?.focus();
  }, [wide, readingVisible]);
  return (
    <ResourceWatchContext.Provider value={resources.register}>
      <div
        ref={shell}
        className="flex h-full min-h-0 flex-col"
        data-testid="workspace-files-surface"
      >
        <IgnoredItemsDialog
          workspaceId={workspaceId}
          open={manageIgnored}
          onOpenChange={setManageIgnored}
        />
        <div className="flex min-h-0 flex-1">
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
                className="flex items-center gap-0.5 border-b p-2"
                onSubmit={(event) => {
                  event.preventDefault();
                  runFilenameSearch();
                }}
              >
                {!wide && (
                  <TooltipIconButton
                    tooltip={treeVisible ? "收起目录，展开阅读" : "浏览目录与搜索"}
                    aria-label="切换目录面板"
                    aria-pressed={treeVisible}
                    disabled={!wide && !session.active}
                    className="size-8"
                    onClick={toggleTree}
                  >
                    <FolderTreeIcon />
                  </TooltipIconButton>
                )}
                <InputGroup className="mr-1 h-8 flex-1">
                  <input
                    ref={filenameSearch}
                    data-slot="input-group-control"
                    className="min-w-0 flex-1 bg-transparent text-sm outline-none"
                    aria-label="搜索工作区文件名"
                    placeholder="搜索文件名或路径，Enter 查找"
                    value={query}
                    onChange={(e) => {
                      setQuery(e.target.value);
                      setSearchRequest(null);
                    }}
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
                <TooltipIconButton
                  tooltip="全部折叠"
                  aria-label="全部折叠"
                  className="size-8"
                  onClick={() => patch(workspaceId, { expanded: [] })}
                >
                  <ChevronsDownUpIcon />
                </TooltipIconButton>
                <FileBrowserMenu onManage={() => setManageIgnored(true)} />
              </form>
              {query.trim() && searchRequest ? (
                <FileSearch
                  key={`${searchRequest.id}:${revision}`}
                  workspaceId={workspaceId}
                  query={searchRequest.query}
                  open={(path) => open(workspaceId, path)}
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
            <FileReadingTabs
              workspaceId={workspaceId}
              onBrowse={() => patch(workspaceId, { treeVisible: true, browsing: true })}
              onEmptyClose={() =>
                (
                  filenameSearch.current ??
                  shell.current?.querySelector<HTMLButtonElement>(
                    'button[aria-label="切换目录面板"]',
                  )
                )?.focus()
              }
            >
              {session.active && (
                <>
                  <div className="flex min-h-0 flex-1 flex-col">
                    {session.reference ? (
                      <>
                        <ReadingToolbar navigation={{ path: session.active, rootPath, treeVisible, toggleTree, revealPath }} />
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
                        navigation={{ path: session.active, rootPath, treeVisible, toggleTree, revealPath }}
                      />
                    )}
                  </div>
                </>
              )}
            </FileReadingTabs>
          </div>
        </div>
      </div>
    </ResourceWatchContext.Provider>
  );
}

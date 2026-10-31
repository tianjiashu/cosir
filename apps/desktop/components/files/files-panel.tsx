import { FolderTreeIcon, XIcon } from "lucide-react";
import { lazy, Suspense } from "react";

import { DockFrame } from "@/components/layout/dock-frame";
import { TooltipIconButton } from "@/components/tooltip-icon-button";
import type { Workspace } from "@/lib/api/workspaces";

const WorkspaceFilesSurface = lazy(() =>
  import("@/components/workspace-files/workspace-files-surface").then((module) => ({
    default: module.WorkspaceFilesSurface,
  })),
);

/**
 * 文件面板：当前工作区的文件浏览器，独立的停靠面板（不与工作台的子 Agent、草稿标签混排）。
 * 浏览状态（打开的文件、展开的目录）按工作区存在 `lib/workspace-files/store`，收起面板不丢。
 */
export function FilesPanel({ workspace, width, minWidth, maxWidth, onWidthChange, onClose }: {
  workspace: Workspace;
  width: number;
  minWidth: number;
  maxWidth: number;
  onWidthChange: (width: number) => void;
  onClose: () => void;
}) {
  return (
    <DockFrame
      label="文件"
      width={width}
      minWidth={minWidth}
      maxWidth={maxWidth}
      onWidthChange={onWidthChange}
      header={(
        <>
          <FolderTreeIcon className="text-muted-foreground ml-1 size-3.5 shrink-0" aria-hidden="true" />
          <span className="min-w-0 flex-1 truncate text-sm font-medium" title={workspace.root_path}>{workspace.name}</span>
          <TooltipIconButton tooltip="收起文件" className="size-7" aria-label="收起文件" onClick={onClose}>
            <XIcon />
          </TooltipIconButton>
        </>
      )}
    >
      <Suspense fallback={<div className="p-4 text-sm">加载文件浏览器…</div>}>
        <WorkspaceFilesSurface workspaceId={workspace.workspace_id} rootPath={workspace.root_path} workspaceName={workspace.name} />
      </Suspense>
    </DockFrame>
  );
}

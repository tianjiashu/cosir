import { XIcon } from "lucide-react";
import { lazy, Suspense } from "react";

import { DockFrame, type DockSizing } from "@/components/layout/dock-frame";
import { TooltipIconButton } from "@/components/ui/tooltip-icon-button";
import type { Workspace } from "@/lib/api/workspaces";

const WorkspaceFilesSurface = lazy(() =>
  import("@/components/workspace-files/workspace-files-surface").then((module) => ({
    default: module.WorkspaceFilesSurface,
  })),
);

/**
 * 文件面板的停靠入口：按需加载文件浏览器（目录、阅读器、PDF.js 等较重的依赖不进首屏包）。
 * 停靠外框与头部由浏览器自己渲染，因为头部承载文件标签；这里只在加载期间先占住同样的外框。
 */
export function FilesPanel({ workspace, sizing, maximized, onToggleMaximized, onClose }: {
  workspace: Workspace;
  sizing: DockSizing;
  maximized: boolean;
  onToggleMaximized: () => void;
  onClose: () => void;
}) {
  return (
    <Suspense fallback={(
      <DockFrame
        label="文件"
        sizing={sizing}
        fill={maximized}
        header={(
          <>
            <span className="min-w-0 flex-1" />
            <TooltipIconButton tooltip="收起文件" className="size-7" aria-label="收起文件" onClick={onClose}>
              <XIcon />
            </TooltipIconButton>
          </>
        )}
      >
        <div className="p-4 text-sm">加载文件浏览器…</div>
      </DockFrame>
    )}>
      <WorkspaceFilesSurface workspace={workspace} sizing={sizing} maximized={maximized}
        onToggleMaximized={onToggleMaximized} onClose={onClose} />
    </Suspense>
  );
}

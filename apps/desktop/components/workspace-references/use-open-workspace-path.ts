import { useMemo } from "react";
import type { WorkspaceReference } from "@/lib/workspace-references/codec";
import { useWorkbenchStore } from "@/lib/workbench/store";
import { parentsOf, useFilesStore } from "@/lib/workspace-files/store";
import { toWorkspacePath } from "@/lib/workspace-files/workspace-path";
import { useReferenceBridge } from "./reference-bridge";

export type WorkspacePathOpener = {
  /** 换算成工作区相对路径；不可打开（工作区外、越界）时返回 null。 */
  resolve: (rawPath: string) => string | null;
  /** 在工作台文件面板打开文件标签；`path` 须是工作区相对路径，`excerpt` 是要先展示的引用快照。 */
  openFile: (path: string, excerpt?: WorkspaceReference) => void;
  /** 在文件树里展开并定位目录。 */
  openDirectory: (path: string) => void;
};

/**
 * 在工作台文件面板里打开当前工作区路径的唯一入口（引用胶囊、工具卡片、正文路径共用）。
 *
 * 当前工作区来自 `ReferenceBridgeProvider`；没有活动工作区时返回 null，调用方渲染为
 * 纯文本。只改前端面板状态，不读文件、不访问后端。
 */
export function useOpenWorkspacePath(): WorkspacePathOpener | null {
  const bridge = useReferenceBridge();
  const workspaceId = bridge?.workspaceId ?? null;
  const workspaceRoot = bridge?.workspaceRoot ?? "";
  const workspaceName = bridge?.workspaceName ?? "";

  return useMemo(() => {
    if (workspaceId === null || !workspaceRoot) return null;
    const showFiles = () => useWorkbenchStore.getState().openFiles(workspaceId, workspaceRoot, workspaceName);
    return {
      resolve: (rawPath) => toWorkspacePath(workspaceRoot, rawPath),
      openFile: (path, excerpt) => {
        showFiles();
        const files = useFilesStore.getState();
        files.open(workspaceId, path, true);
        if (excerpt) files.patch(workspaceId, { reference: excerpt });
      },
      openDirectory: (path) => {
        showFiles();
        useFilesStore.getState().patch(workspaceId, {
          browsing: true,
          treeVisible: true,
          expanded: [...parentsOf(path), path],
          reference: null,
        });
      },
    };
  }, [workspaceId, workspaceRoot, workspaceName]);
}

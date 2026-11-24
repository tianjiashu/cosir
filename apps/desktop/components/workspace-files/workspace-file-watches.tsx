import { useShallow } from "zustand/react/shallow";

import { useFileWatchStore } from "@/lib/workspace-files/file-watch-store";
import { useFileWatch } from "@/lib/workspace-files/use-file-watch";

function WorkspaceFileWatch({ workspaceId }: { workspaceId: number }) {
  const paths = useFileWatchStore(useShallow((state) => Object.keys(state.byWorkspace[workspaceId] ?? {})));
  useFileWatch(workspaceId, paths);
  return null;
}

/**
 * 工作区文件监听的唯一挂载点（AppLayout）：为每个有登记的工作区维持一条连接（`useFileWatch`：连上与每次变化都
 * 整体重读）。面板关着时，对话里的交付物卡片照样收得到「文件变了」；没有任何登记时没有连接。
 */
export function WorkspaceFileWatches() {
  const workspaceIds = useFileWatchStore(useShallow((state) => Object.keys(state.byWorkspace).map(Number)));
  return workspaceIds.map((workspaceId) => <WorkspaceFileWatch key={workspaceId} workspaceId={workspaceId} />);
}

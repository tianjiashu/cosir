import { useEffect, useLayoutEffect, useMemo, useRef, type ReactNode } from "react";
import { Tree, type NodeApi, type TreeApi } from "react-arborist";

import { useElementSize } from "@/hooks/use-element-size";
import type { WorkspaceTask } from "@/lib/api/workspaces";
import { buildWorkspaceTreeModel, taskTreeNodeId, workspaceTreeNodeId, type TaskSidebarNode, type WorkspaceSidebarNode, type WorkspaceTreeSource } from "./workspace-tree-model";
import { WorkspaceTreeActionsContext, WorkspaceTreeNode, WorkspaceTreeRow } from "./workspace-tree-row";
import "./workspace-tree.css";

type WorkspaceTreeProps = {
  workspaces: WorkspaceTreeSource[];
  pinnedTaskIds: readonly number[];
  activeTaskId: number | null;
  selectedWorkspaceId: number | null;
  expandedWorkspaceIds: number[] | null;
  onWorkspaceExpansionChange: (workspaceId: number, expanded: boolean) => void;
  onSelectWorkspace: (workspaceId: number) => void;
  onOpenWorkspaceSettings: (workspaceId: number) => void;
  onSelectTask: (task: WorkspaceTask, workspaceId: number) => void;
  renderWorkspaceActions: (workspace: WorkspaceTreeSource, tabIndex: number) => ReactNode;
  renderTaskActions: (item: TaskSidebarNode, tabIndex: number) => ReactNode;
};

const ROW_HEIGHT = 38;

export function WorkspaceTree({
  workspaces,
  pinnedTaskIds,
  activeTaskId,
  selectedWorkspaceId,
  expandedWorkspaceIds,
  onWorkspaceExpansionChange,
  onSelectWorkspace,
  onOpenWorkspaceSettings,
  onSelectTask,
  renderWorkspaceActions,
  renderTaskActions,
}: WorkspaceTreeProps) {
  const model = useMemo(() => buildWorkspaceTreeModel(workspaces, pinnedTaskIds), [pinnedTaskIds, workspaces]);
  const treeRef = useRef<TreeApi<WorkspaceSidebarNode> | undefined>(undefined);
  const [containerRef, containerSize] = useElementSize<HTMLDivElement>();
  // 取整：小数高度会让虚拟列表在相邻整数间来回重算。
  const treeHeight = Math.floor(containerSize.height);

  const initialOpenState = useMemo(() => {
    if (expandedWorkspaceIds === null) return undefined;
    return Object.fromEntries(workspaces.map((workspace) => [
      workspaceTreeNodeId(workspace.workspace_id),
      expandedWorkspaceIds.includes(workspace.workspace_id),
    ]));
  }, [expandedWorkspaceIds, workspaces]);

  useLayoutEffect(() => {
    if (expandedWorkspaceIds === null) return;
    for (const workspace of workspaces) {
      const id = workspaceTreeNodeId(workspace.workspace_id);
      const shouldOpen = expandedWorkspaceIds.includes(workspace.workspace_id);
      if (shouldOpen && !treeRef.current?.isOpen(id)) treeRef.current?.open(id);
      else if (!shouldOpen && treeRef.current?.isOpen(id)) treeRef.current?.close(id);
    }
  }, [expandedWorkspaceIds, model, workspaces]);

  useEffect(() => {
    if (activeTaskId === null) return;
    treeRef.current?.openParents(taskTreeNodeId(activeTaskId));
  }, [activeTaskId, model]);

  const handleToggle = (id: string) => {
    const workspace = workspaces.find((candidate) => workspaceTreeNodeId(candidate.workspace_id) === id);
    if (!workspace) return;
    onWorkspaceExpansionChange(workspace.workspace_id, treeRef.current?.isOpen(id) ?? false);
  };

  return (
    <div ref={containerRef} className="h-full min-h-0 min-w-0">
      {treeHeight > 0 && model.length > 0 && (
        <WorkspaceTreeActionsContext.Provider value={{ onOpenWorkspaceSettings, renderWorkspaceActions, renderTaskActions }}>
          <Tree<WorkspaceSidebarNode>
            ref={treeRef}
            dndRootElement={containerRef.current}
            data={model}
            idAccessor={(item) => item.id}
            childrenAccessor={(item) => item.children.length > 0 ? item.children : null}
            width="100%"
            height={treeHeight}
            indent={16}
            rowHeight={ROW_HEIGHT}
            overscanCount={8}
            openByDefault
            initialOpenState={initialOpenState}
            selection={activeTaskId !== null ? taskTreeNodeId(activeTaskId) : selectedWorkspaceId !== null ? workspaceTreeNodeId(selectedWorkspaceId) : undefined}
            disableMultiSelection
            disableDeselectOnClick
            disableDrag
            disableDrop
            disableEdit
            aria-label="工作区和任务列表"
            renderRow={WorkspaceTreeRow}
            onToggle={handleToggle}
            onActivate={(node: NodeApi<WorkspaceSidebarNode>) => {
              if (node.data.kind === "task") onSelectTask(node.data.task, node.data.task.workspace_id);
              else if (node.data.kind === "pinned") node.toggle();
              else if (node.data.kind === "workspace") {
                node.open();
                onSelectWorkspace(node.data.workspace.workspace_id);
              }
            }}
          >
            {WorkspaceTreeNode}
          </Tree>
        </WorkspaceTreeActionsContext.Provider>
      )}
    </div>
  );
}

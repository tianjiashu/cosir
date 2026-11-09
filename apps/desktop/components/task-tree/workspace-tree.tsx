import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Tree, type NodeApi, type TreeApi } from "react-arborist";

import { TooltipProvider } from "@/components/ui/tooltip";
import { useElementSize } from "@/hooks/use-element-size";
import type { WorkspaceTask } from "@/lib/api/workspaces";
import { useTaskStatusStore } from "@/lib/task-status/task-status-store";
import { buildWorkspaceTreeModel, TASK_PAGE_SIZE, taskTreeNodeId, workspaceTreeNodeId, type TaskSidebarNode, type WorkspaceSidebarNode, type WorkspaceTreeSource } from "./workspace-tree-model";
import { WorkspaceTreeActionsContext, WorkspaceTreeNode, WorkspaceTreeRow } from "./workspace-tree-row";
import "./workspace-tree.css";

type WorkspaceTreeProps = {
  workspaces: WorkspaceTreeSource[];
  pinnedTaskIds: readonly number[];
  activeTaskId: number | null;
  selectedWorkspaceId: number | null;
  expandedWorkspaceIds: number[] | null;
  onWorkspaceExpansionChange: (workspaceId: number, expanded: boolean) => void;
  onSelectTask: (task: WorkspaceTask, workspaceId: number) => void;
  renderWorkspaceActions: (workspace: WorkspaceTreeSource, tabIndex: number) => ReactNode;
  renderTaskActions: (item: TaskSidebarNode, tabIndex: number) => ReactNode;
};

const ROW_HEIGHT = 32;
/** 悬停卡片的出现延迟；同一 Provider 内移到相邻行时立即切换，不再等待。 */
const HOVER_CARD_DELAY_MS = 500;
/** 这些状态的任务必须留在侧栏可见范围内（`suspended` 可能在等用户操作）。 */
const MUST_SHOW_STATUSES = new Set(["pending", "running", "suspended"]);

function mustShowTaskIds(statuses: Readonly<Record<number, string | null>>, activeTaskId: number | null): Set<number> {
  const ids = new Set<number>();
  for (const [taskId, status] of Object.entries(statuses)) {
    if (status !== null && MUST_SHOW_STATUSES.has(status)) ids.add(Number(taskId));
  }
  if (activeTaskId !== null) ids.add(activeTaskId);
  return ids;
}

export function WorkspaceTree({
  workspaces,
  pinnedTaskIds,
  activeTaskId,
  selectedWorkspaceId,
  expandedWorkspaceIds,
  onWorkspaceExpansionChange,
  onSelectTask,
  renderWorkspaceActions,
  renderTaskActions,
}: WorkspaceTreeProps) {
  const statuses = useTaskStatusStore((state) => state.statuses);
  // 水位线是界面状态（不持久化、卸载即重置）：只升不降，收起工作区或点「收起」时回到默认。
  const [watermarks, setWatermarks] = useState<ReadonlyMap<number, number>>(() => new Map());
  const { nodes: model, watermarks: resolvedWatermarks } = useMemo(
    () => buildWorkspaceTreeModel(workspaces, pinnedTaskIds, { watermarks, mustShowTaskIds: mustShowTaskIds(statuses, activeTaskId) }),
    [activeTaskId, pinnedTaskIds, statuses, watermarks, workspaces],
  );
  // 必须可见的任务推高了水位线时记下来，它停止运行或不再打开后仍留在原位。
  useLayoutEffect(() => {
    const raised = [...resolvedWatermarks].filter(([workspaceId, value]) => value > (watermarks.get(workspaceId) ?? TASK_PAGE_SIZE));
    if (raised.length > 0) setWatermarks((current) => new Map([...current, ...raised]));
  }, [resolvedWatermarks, watermarks]);
  const setWatermark = (workspaceId: number, value: number | null) => setWatermarks((current) => {
    const next = new Map(current);
    if (value === null) next.delete(workspaceId);
    else next.set(workspaceId, value);
    return next;
  });
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
      else if (!shouldOpen && treeRef.current?.isOpen(id)) treeRef.current.close(id);
    }
  }, [expandedWorkspaceIds, model, workspaces]);

  useEffect(() => {
    if (activeTaskId === null) return;
    treeRef.current?.openParents(taskTreeNodeId(activeTaskId));
  }, [activeTaskId, model]);

  const handleToggle = (id: string) => {
    const workspace = workspaces.find((candidate) => workspaceTreeNodeId(candidate.workspace_id) === id);
    if (!workspace) return;
    const open = treeRef.current?.isOpen(id) ?? false;
    if (!open) setWatermark(workspace.workspace_id, null);
    onWorkspaceExpansionChange(workspace.workspace_id, open);
  };

  /** 「显示更多」：剩余不超过一页时全部显示，否则多显示一页；`hiddenCount` 为 0 的那一行是「收起」。 */
  const showMore = (workspaceId: number, hiddenCount: number) => {
    if (hiddenCount === 0) {
      setWatermark(workspaceId, null);
      return;
    }
    const current = resolvedWatermarks.get(workspaceId) ?? TASK_PAGE_SIZE;
    setWatermark(workspaceId, current + (hiddenCount <= TASK_PAGE_SIZE ? hiddenCount : TASK_PAGE_SIZE));
  };

  return (
    <div ref={containerRef} className="h-full min-h-0 min-w-0">
      {treeHeight > 0 && model.length > 0 && (
        <TooltipProvider delay={HOVER_CARD_DELAY_MS}>
        <WorkspaceTreeActionsContext.Provider value={{ renderWorkspaceActions, renderTaskActions }}>
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
              else if (node.data.kind === "more") showMore(node.data.workspaceId, node.data.hiddenCount);
              // 置顶分组与工作区都只是分组：激活即展开 / 收起，不改变选中的工作区。
              else if (node.data.kind !== "placeholder") node.toggle();
            }}
          >
            {WorkspaceTreeNode}
          </Tree>
        </WorkspaceTreeActionsContext.Provider>
        </TooltipProvider>
      )}
    </div>
  );
}

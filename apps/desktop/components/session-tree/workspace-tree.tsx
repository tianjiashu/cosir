import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Tree, type NodeApi, type TreeApi } from "react-arborist";

import { useElementSize } from "@/hooks/use-element-size";
import type { WorkspaceSession } from "@/lib/api/workspaces";
import { type SessionStatus, useSessionStatusStore } from "@/lib/session-status/session-status-store";
import { buildWorkspaceTreeModel, SESSION_PAGE_SIZE, sessionTreeNodeId, workspaceTreeNodeId, type SessionSidebarNode, type WorkspaceSidebarNode, type WorkspaceTreeSource } from "./workspace-tree-model";
import { WorkspaceTreeActionsContext, WorkspaceTreeNode, WorkspaceTreeRow } from "./workspace-tree-row";
import "./workspace-tree.css";

type WorkspaceTreeProps = {
  workspaces: WorkspaceTreeSource[];
  pinnedSessionIds: readonly number[];
  activeSessionId: number | null;
  selectedWorkspaceId: number | null;
  expandedWorkspaceIds: number[] | null;
  onWorkspaceExpansionChange: (workspaceId: number, expanded: boolean) => void;
  onSelectSession: (session: WorkspaceSession, workspaceId: number) => void;
  renderWorkspaceActions: (workspace: WorkspaceTreeSource, tabIndex: number) => ReactNode;
  renderSessionActions: (item: SessionSidebarNode, tabIndex: number) => ReactNode;
};

const ROW_HEIGHT = 32;
/** 这些执行状态的会话必须留在侧栏可见范围内；有事等你操作的会话无论 Run 处于什么状态都必须可见。 */
const MUST_SHOW_STATUSES = new Set(["running", "suspended"]);

/**
 * 状态表里「必须可见」的 Session id，拼成稳定的字符串供订阅：状态推送每帧都会来，侧栏只在这个集合真的变了
 * 时才重建模型，别的 Session 的工具状态变化不牵动整棵树。
 */
function mustShowKey(sessions: Readonly<Record<number, SessionStatus>>): string {
  const ids: number[] = [];
  for (const [sessionId, { executionStatus, awaitingUserCalls }] of Object.entries(sessions)) {
    if (awaitingUserCalls > 0 || (executionStatus !== null && MUST_SHOW_STATUSES.has(executionStatus))) ids.push(Number(sessionId));
  }
  return ids.sort((left, right) => left - right).join(",");
}

function mustShowSessionIds(key: string, activeSessionId: number | null): Set<number> {
  const ids = new Set(key ? key.split(",").map(Number) : []);
  if (activeSessionId !== null) ids.add(activeSessionId);
  return ids;
}

export function WorkspaceTree({
  workspaces,
  pinnedSessionIds,
  activeSessionId,
  selectedWorkspaceId,
  expandedWorkspaceIds,
  onWorkspaceExpansionChange,
  onSelectSession,
  renderWorkspaceActions,
  renderSessionActions,
}: WorkspaceTreeProps) {
  const mustShow = useSessionStatusStore((state) => mustShowKey(state.sessions));
  // 水位线是界面状态（不持久化、卸载即重置）：只升不降，收起工作区或点「收起」时回到默认。
  const [watermarks, setWatermarks] = useState<ReadonlyMap<number, number>>(() => new Map());
  const { nodes: model, watermarks: resolvedWatermarks } = useMemo(
    () => buildWorkspaceTreeModel(workspaces, pinnedSessionIds, { watermarks, mustShowSessionIds: mustShowSessionIds(mustShow, activeSessionId) }),
    [activeSessionId, pinnedSessionIds, mustShow, watermarks, workspaces],
  );
  // 必须可见的会话推高了水位线时记下来，它停止运行或不再打开后仍留在原位。
  useLayoutEffect(() => {
    const raised = [...resolvedWatermarks].filter(([workspaceId, value]) => value > (watermarks.get(workspaceId) ?? SESSION_PAGE_SIZE));
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
    if (activeSessionId === null) return;
    treeRef.current?.openParents(sessionTreeNodeId(activeSessionId));
  }, [activeSessionId, model]);

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
    const current = resolvedWatermarks.get(workspaceId) ?? SESSION_PAGE_SIZE;
    setWatermark(workspaceId, current + (hiddenCount <= SESSION_PAGE_SIZE ? hiddenCount : SESSION_PAGE_SIZE));
  };

  return (
    <div ref={containerRef} className="h-full min-h-0 min-w-0">
      {treeHeight > 0 && model.length > 0 && (
        <WorkspaceTreeActionsContext.Provider value={{ renderWorkspaceActions, renderSessionActions }}>
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
            selection={activeSessionId !== null ? sessionTreeNodeId(activeSessionId) : selectedWorkspaceId !== null ? workspaceTreeNodeId(selectedWorkspaceId) : undefined}
            disableMultiSelection
            disableDeselectOnClick
            disableDrag
            disableDrop
            disableEdit
            aria-label="工作区和会话列表"
            renderRow={WorkspaceTreeRow}
            onToggle={handleToggle}
            onActivate={(node: NodeApi<WorkspaceSidebarNode>) => {
              if (node.data.kind === "session") onSelectSession(node.data.session, node.data.session.workspace_id);
              else if (node.data.kind === "more") showMore(node.data.workspaceId, node.data.hiddenCount);
              // 置顶分组与工作区都只是分组：激活即展开 / 收起，不改变选中的工作区。
              else if (node.data.kind !== "placeholder") node.toggle();
            }}
          >
            {WorkspaceTreeNode}
          </Tree>
        </WorkspaceTreeActionsContext.Provider>
      )}
    </div>
  );
}

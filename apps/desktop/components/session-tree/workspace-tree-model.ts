import type { Workspace, WorkspaceSession } from "@/lib/api/workspaces";

import { buildSessionForest, type SessionTreeNode } from "./session-tree-model";

export type WorkspaceTreeSource = Workspace & {
  sessions: WorkspaceSession[];
  sessionLoadError?: string;
};

export type WorkspaceSidebarNode =
  | {
      /** 跨工作区的置顶分组；没有置顶会话时不出现。 */
      kind: "pinned";
      id: string;
      children: WorkspaceSidebarNode[];
    }
  | {
      kind: "workspace";
      id: string;
      workspace: WorkspaceTreeSource;
      children: WorkspaceSidebarNode[];
    }
  | {
      kind: "session";
      id: string;
      session: SessionTreeNode;
      /** 只有顶层会话可置顶；Fork 随源会话一起移动，不单独置顶。 */
      pinnable: boolean;
      pinned: boolean;
      /** 仅置顶区的会话带上所属工作区名，供 tooltip 区分来源。 */
      workspaceName?: string;
      children: WorkspaceSidebarNode[];
    }
  | {
      kind: "placeholder";
      id: string;
      label: string;
      /** 会话列表读取失败时为 true，行渲染据此使用失败语气而非空状态语气。 */
      isError: boolean;
      children: WorkspaceSidebarNode[];
    }
  | {
      /** 工作区末尾的「显示更多 / 收起」行；`hiddenCount` 为 0 时是「收起」。 */
      kind: "more";
      id: string;
      workspaceId: number;
      hiddenCount: number;
      children: WorkspaceSidebarNode[];
    };

export type SessionSidebarNode = Extract<WorkspaceSidebarNode, { kind: "session" }>;

export function workspaceTreeNodeId(workspaceId: number): string {
  return `workspace:${workspaceId}`;
}

export function sessionTreeNodeId(sessionId: number): string {
  return `session:${sessionId}`;
}

const PINNED_GROUP_NODE_ID = "pinned";

/** Fork 作为源会话的子节点进入侧栏树，由 react-arborist 承载缩进、折叠与 `aria-level`。 */
function toForkSidebarNode(session: SessionTreeNode): WorkspaceSidebarNode {
  return {
    kind: "session",
    id: sessionTreeNodeId(session.session_id),
    session,
    pinnable: false,
    pinned: false,
    children: session.children.map(toForkSidebarNode),
  };
}

function toRootSidebarNode(session: SessionTreeNode, pinned: boolean, workspaceName?: string): WorkspaceSidebarNode {
  return {
    kind: "session",
    id: sessionTreeNodeId(session.session_id),
    session,
    pinnable: true,
    pinned,
    workspaceName,
    children: session.children.map(toForkSidebarNode),
  };
}

/** 每个工作区默认显示的顶层会话数，也是「显示更多」每次多显示的条数。 */
export const SESSION_PAGE_SIZE = 5;

/**
 * 组装侧栏树时的展示状态：各工作区的水位线（显示前多少个顶层会话）与必须可见的会话。
 *
 * 水位线由 WorkspaceTree 持有、不持久化；缺省为 `SESSION_PAGE_SIZE`。
 */
export type SessionVisibility = {
  watermarks: ReadonlyMap<number, number>;
  /** 当前打开、运行中或等待中的会话：所在的顶层会话排在水位线之后时，把水位线推到它。 */
  mustShowSessionIds: ReadonlySet<number>;
};

export type WorkspaceTreeModel = {
  nodes: WorkspaceSidebarNode[];
  /** 本次实际使用的水位线（已含必须可见会话的推高）；树据此只升不降地记下，会话停止运行后不会消失。 */
  watermarks: Map<number, number>;
};

function subtreeContains(session: SessionTreeNode, sessionIds: ReadonlySet<number>): boolean {
  return sessionIds.has(session.session_id) || session.children.some((child) => subtreeContains(child, sessionIds));
}

/** 水位线：默认值与「最后一个必须可见的顶层会话」之间取较大者，按顶层会话计数（Fork 跟随源会话）。 */
function resolveWatermark(roots: SessionTreeNode[], stored: number | undefined, mustShow: ReadonlySet<number>): number {
  let watermark = stored ?? SESSION_PAGE_SIZE;
  roots.forEach((root, index) => {
    if (index >= watermark && subtreeContains(root, mustShow)) watermark = index + 1;
  });
  return watermark;
}

/**
 * 组装侧栏树：可选的置顶分组在最前，其后是各工作区。
 *
 * 置顶的顶层会话连同 Fork 子树整体移入置顶分组（按置顶顺序），保证每个会话在树中只出现一次，
 * 选中、`openParents` 与键盘导航不必区分两处。`pinnedSessionIds` 中找不到的 id 直接忽略。
 * 置顶区不受水位线限制；工作区只显示水位线以内的顶层会话，其余收进末尾的「显示更多」行，
 * 全部显示且超过默认条数时末尾是「收起」。
 */
export function buildWorkspaceTreeModel(
  workspaces: WorkspaceTreeSource[],
  pinnedSessionIds: readonly number[],
  visibility: SessionVisibility,
): WorkspaceTreeModel {
  const pinnedOrder = new Map(pinnedSessionIds.map((sessionId, index) => [sessionId, index]));
  const pinned: { order: number; node: WorkspaceSidebarNode }[] = [];
  const watermarks = new Map<number, number>();
  const workspaceNodes = workspaces.map((workspace): WorkspaceSidebarNode => {
    const roots: SessionTreeNode[] = [];
    for (const session of buildSessionForest(workspace.sessions)) {
      const order = pinnedOrder.get(session.session_id);
      if (order === undefined) roots.push(session);
      else pinned.push({ order, node: toRootSidebarNode(session, true, workspace.name) });
    }
    const watermark = resolveWatermark(roots, visibility.watermarks.get(workspace.workspace_id), visibility.mustShowSessionIds);
    watermarks.set(workspace.workspace_id, watermark);
    const children = roots.slice(0, watermark).map((session) => toRootSidebarNode(session, false));
    const hiddenCount = Math.max(0, roots.length - watermark);
    if (hiddenCount > 0 || roots.length > SESSION_PAGE_SIZE) {
      children.push({ kind: "more", id: `workspace-more:${workspace.workspace_id}`, workspaceId: workspace.workspace_id, hiddenCount, children: [] });
    }
    // 会话全部置顶时工作区下留空，不显示「暂无会话」：会话仍在，只是摆在置顶区。
    if (workspace.sessions.length === 0) {
      children.push({
        kind: "placeholder",
        id: `workspace-empty:${workspace.workspace_id}`,
        label: workspace.sessionLoadError ? "会话列表加载失败" : "暂无会话",
        isError: Boolean(workspace.sessionLoadError),
        children: [],
      });
    }
    return {
      kind: "workspace",
      id: workspaceTreeNodeId(workspace.workspace_id),
      workspace,
      children,
    };
  });
  if (pinned.length === 0) return { nodes: workspaceNodes, watermarks };
  pinned.sort((left, right) => left.order - right.order);
  return {
    nodes: [{ kind: "pinned", id: PINNED_GROUP_NODE_ID, children: pinned.map((entry) => entry.node) }, ...workspaceNodes],
    watermarks,
  };
}

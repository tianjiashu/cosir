import type { WorkspaceSession } from "@/lib/api/workspaces";
import { sessionDisplayTitle } from "@/lib/session-title/session-display-title";

export type SessionTreeNode = WorkspaceSession & {
  children: SessionTreeNode[];
  /** 侧栏标题：不截断；按侧栏宽度由 CSS 省略，完整文本在 tooltip 中可见。 */
  full_title: string;
};

/**
 * Return the direct Fork source encoded by the backend response.
 *
 * This is intentionally limited to Fork sessions. Delegation relationships are
 * not part of the workspace sidebar and must not accidentally enter this tree.
 */
function getForkSourceSessionId(session: WorkspaceSession): number | null {
  if (session.session_type !== "user_fork" && session.session_type !== "agent_fork") return null;
  const sourceSessionId = session.extra?.fork?.tree_parent_session_id;
  return typeof sourceSessionId === "number" && Number.isInteger(sourceSessionId) ? sourceSessionId : null;
}

function compareByUpdatedAt(left: WorkspaceSession, right: WorkspaceSession): number {
  const updatedDifference = right.updated_at.localeCompare(left.updated_at);
  return updatedDifference || right.session_id - left.session_id;
}

function compareByCreatedAt(left: WorkspaceSession, right: WorkspaceSession): number {
  const createdDifference = left.created_at.localeCompare(right.created_at);
  return createdDifference || left.session_id - right.session_id;
}

function hasParentCycle(sessionId: number, parentCandidates: Map<number, number>): boolean {
  const visited = new Set<number>();
  let currentId: number | undefined = sessionId;
  while (currentId !== undefined) {
    if (visited.has(currentId)) return true;
    visited.add(currentId);
    currentId = parentCandidates.get(currentId);
  }
  return false;
}

/** 节点下全部 fork 后代的 id（fork 的 fork 也算），不含自身；删除确认据此询问是否一起删。 */
export function forkDescendantIds(node: SessionTreeNode): number[] {
  return node.children.flatMap((child) => [child.session_id, ...forkDescendantIds(child)]);
}

/**
 * Convert the flat workspace catalog into a defensive Fork-only forest.
 *
 * Valid Forks are attached to their source session recursively. A missing source
 * or a cycle is treated as a root so malformed persisted metadata cannot hide
 * a session or cause recursive rendering to loop forever. Roots are ordered by
 * recent activity; siblings are ordered by creation time to keep a branch's
 * position stable while it is being used.
 *
 * 只产出森林本身：层级、父节点与展开祖先由侧栏的 react-arborist 从嵌套结构推导，
 * 这里不另存深度或父子索引。
 */
export function buildSessionForest(sessions: WorkspaceSession[]): SessionTreeNode[] {
  // 列表只含侧栏会话（后端按 Session 类型规则过滤），这里不再按类型筛。
  const sessionById = new Map(sessions.map((session) => [session.session_id, session]));
  const parentCandidates = new Map<number, number>();

  for (const session of sessions) {
    const sourceSessionId = getForkSourceSessionId(session);
    if (sourceSessionId !== null && sourceSessionId !== session.session_id && sessionById.has(sourceSessionId)) {
      parentCandidates.set(session.session_id, sourceSessionId);
    }
  }

  const parentById = new Map<number, number>();
  for (const [sessionId, sourceSessionId] of parentCandidates) {
    if (!hasParentCycle(sessionId, parentCandidates)) parentById.set(sessionId, sourceSessionId);
  }

  const childrenByParent = new Map<number, WorkspaceSession[]>();
  for (const [sessionId, parentId] of parentById) {
    const session = sessionById.get(sessionId);
    if (!session) continue;
    const siblings = childrenByParent.get(parentId) ?? [];
    siblings.push(session);
    childrenByParent.set(parentId, siblings);
  }
  for (const siblings of childrenByParent.values()) siblings.sort(compareByCreatedAt);

  const makeNode = (session: WorkspaceSession): SessionTreeNode => ({
    ...session,
    full_title: sessionDisplayTitle(session.title),
    children: (childrenByParent.get(session.session_id) ?? []).map(makeNode),
  });

  return sessions
    .filter((session) => !parentById.has(session.session_id))
    .sort(compareByUpdatedAt)
    .map(makeNode);
}

import { describe, expect, it } from "vitest";

import type { WorkspaceSession } from "@/lib/api/workspaces";
import {
  buildWorkspaceTreeModel as build,
  SESSION_PAGE_SIZE,
  type SessionVisibility,
  type WorkspaceSidebarNode,
  type WorkspaceTreeSource,
} from "@/components/session-tree/workspace-tree-model";

/** 会话 id 越大越新：顶层会话按最近更新倒序排列。 */
function session(sessionId: number, workspaceId: number, sourceSessionId?: number): WorkspaceSession {
  const at = new Date(Date.UTC(2026, 0, 1, 0, sessionId)).toISOString();
  return {
    session_id: sessionId,
    workspace_id: workspaceId,
    title: `Session ${sessionId}`,
    session_type: sourceSessionId ? "user_fork" : "user",
    extra: sourceSessionId ? { fork: { tree_parent_session_id: sourceSessionId } } : null,
    unstarted: false,
    created_at: at,
    updated_at: at,
  };
}

const noVisibility: SessionVisibility = { watermarks: new Map(), mustShowSessionIds: new Set() };

function buildWorkspaceTreeModel(workspaces: WorkspaceTreeSource[], pinnedSessionIds: readonly number[], visibility = noVisibility) {
  return build(workspaces, pinnedSessionIds, visibility).nodes;
}

/** 编号 1..count 的顶层会话；按最近更新排序后显示顺序是 count..1。 */
function sessions(count: number): WorkspaceSession[] {
  return Array.from({ length: count }, (_, index) => session(index + 1, 1));
}

function workspace(workspaceId: number, sessions: WorkspaceSession[]): WorkspaceTreeSource {
  const at = "2026-01-01T00:00:00.000Z";
  return { workspace_id: workspaceId, name: `ws-${workspaceId}`, root_path: `/ws/${workspaceId}`, created_at: at, updated_at: at, sessions };
}

const ids = (nodes: WorkspaceSidebarNode[]) => nodes.map((node) => node.id);

describe("buildWorkspaceTreeModel", () => {
  it("omits the pinned group when nothing is pinned and ignores unknown ids", () => {
    const model = buildWorkspaceTreeModel([workspace(1, [session(1, 1)])], [99]);
    expect(ids(model)).toEqual(["workspace:1"]);
  });

  it("moves pinned root sessions with their Fork subtree across workspaces in pin order", () => {
    const model = buildWorkspaceTreeModel(
      [workspace(1, [session(1, 1), session(2, 1, 1), session(3, 1)]), workspace(2, [session(4, 2)])],
      [4, 1, 2],
    );
    expect(ids(model)).toEqual(["pinned", "workspace:1", "workspace:2"]);
    const [pinned, first, second] = model;
    // Fork 2 不是顶层会话，不能单独置顶，随源会话 1 一起移动。
    expect(ids(pinned.children)).toEqual(["session:4", "session:1"]);
    expect(ids(pinned.children[1].children)).toEqual(["session:2"]);
    expect(pinned.children[0]).toMatchObject({ pinned: true, pinnable: true, workspaceName: "ws-2" });
    expect(pinned.children[1].children[0]).toMatchObject({ pinned: false, pinnable: false });
    expect(ids(first.children)).toEqual(["session:3"]);
    // 会话都在置顶区时工作区下留空，不显示「暂无会话」。
    expect(second.children).toEqual([]);
  });

  it("keeps the empty placeholder for workspaces without sessions", () => {
    const [node] = buildWorkspaceTreeModel([workspace(1, [])], []);
    expect(node.children).toMatchObject([{ kind: "placeholder", label: "暂无会话" }]);
  });

  it("shows the newest page of root sessions and summarizes the rest in a more row", () => {
    const [node] = buildWorkspaceTreeModel([workspace(1, sessions(8))], []);
    expect(ids(node.children)).toEqual(["session:8", "session:7", "session:6", "session:5", "session:4", "workspace-more:1"]);
    expect(node.children.at(-1)).toMatchObject({ kind: "more", workspaceId: 1, hiddenCount: 3 });
  });

  it("adds no more row when every root fits in the first page", () => {
    const [node] = buildWorkspaceTreeModel([workspace(1, sessions(SESSION_PAGE_SIZE))], []);
    expect(node.children.map((child) => child.kind)).toEqual(Array(SESSION_PAGE_SIZE).fill("session"));
  });

  it("counts root sessions only: a Fork rides along with its source", () => {
    const [node] = buildWorkspaceTreeModel([workspace(1, [...sessions(6), session(9, 1, 2)])], []);
    expect(ids(node.children)).toEqual(["session:6", "session:5", "session:4", "session:3", "session:2", "workspace-more:1"]);
    expect(ids(node.children[4].children)).toEqual(["session:9"]);
  });

  it("raises the watermark to the last must-show root, including one reached through a Fork", () => {
    const model = build([workspace(1, [...sessions(10), session(11, 1, 2)])], [], { watermarks: new Map(), mustShowSessionIds: new Set([11]) });
    // 会话 2 是倒数第二个顶层会话（第 9 位），它的 Fork 11 必须可见。
    expect(model.watermarks.get(1)).toBe(9);
    expect(model.nodes[0].children.at(-1)).toMatchObject({ kind: "more", hiddenCount: 1 });
  });

  it("keeps a stored watermark and turns the tail into a collapse row once everything is shown", () => {
    const expanded = build([workspace(1, sessions(8))], [], { watermarks: new Map([[1, 8]]), mustShowSessionIds: new Set() });
    expect(expanded.watermarks.get(1)).toBe(8);
    expect(expanded.nodes[0].children.at(-1)).toMatchObject({ kind: "more", hiddenCount: 0 });
  });

  it("excludes pinned roots from the page and never limits the pinned group", () => {
    const model = build([workspace(1, sessions(8))], [8, 7, 6, 5, 4, 3], noVisibility);
    const [pinned, ws] = model.nodes;
    expect(pinned.children).toHaveLength(6);
    expect(ids(ws.children)).toEqual(["session:2", "session:1"]);
  });
});

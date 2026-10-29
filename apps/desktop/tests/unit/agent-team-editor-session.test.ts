import { beforeEach, describe, expect, it, vi } from "vitest";
import { createTeamEditorSession } from "@/lib/agent-team-editor/session";
import { exportDraft } from "@/lib/agent-team-editor/model";
import { teamConfiguration } from "./team-editor-fixture";
import type { AgentTeamConfiguration } from "@/lib/api/agent-teams";
import { useConfirmStore } from "@/lib/confirm/confirm-store";
vi.mock("@/lib/logging/frontend-log", () => ({ frontendLog: vi.fn() }));
const emptySelection = () => ({ nodeIds: new Set<string>(), endNodeIds: new Set<string>(), transitionIds: new Set<string>() });
const create = () => createTeamEditorSession({ initial: teamConfiguration, saved: true, workspaceId: 11 });
const saved = (value: ReturnType<typeof exportDraft>): AgentTeamConfiguration => ({ ...value, max_runs: Number(value.max_runs) });
beforeEach(() => {
  const values = new Map<string, string>();
  vi.stubGlobal("localStorage", { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => values.set(key, value), clear: () => values.clear(), removeItem: (key: string) => values.delete(key) });
});

describe("Team editing session", () => {
  it("deletes nodes and references atomically without shifting surviving layout identities", () => {
    const session = create();
    const before = session.store.getState().snapshot;
    const [first, second] = before.configuration.nodes;
    session.remove({ ...emptySelection(), nodeIds: new Set([first.editorId]) });
    const after = session.store.getState().snapshot;
    expect(after.configuration.nodes).toEqual([second]);
    expect(after.configuration.start_node_id).toBe("fixer");
    expect(after.configuration.transitions).toEqual([]);
    expect(after.layout.positions[second.editorId]).toEqual(before.layout.positions[second.editorId]);
    expect(after.layout.positions[first.editorId]).toBeUndefined();
    session.undo();
    expect(session.store.getState().snapshot).toEqual(before);
    expect(session.store.getState().dirty).toBe(false);
    session.redo();
    expect(session.store.getState().snapshot).toEqual(after);
  });
  it("preserves identity and route mappings when renaming a node, and retains invalid status drafts", () => {
    const session = create();
    const before = session.store.getState().snapshot;
    const edge = before.configuration.transitions[0];
    session.commit({ ...before, layout: { ...before.layout, edgeRoutes: { [edge.editorId]: { axis: "x", offset: 50 } }, endTargets: { [edge.editorId]: "@@canvas-end-1" } } });
    session.updateNode(0, { node_id: "renamed", statuses: [before.configuration.nodes[0].statuses[0]] });
    const after = session.store.getState().snapshot;
    expect(after.configuration.nodes[0].editorId).toBe(before.configuration.nodes[0].editorId);
    expect(after.configuration.start_node_id).toBe("renamed");
    expect(after.configuration.transitions[2].target_node_id).toBe("renamed");
    expect(after.configuration.transitions[1].status).toBe("needs_changes");
    expect(after.layout.edgeRoutes[edge.editorId]).toEqual({ axis: "x", offset: 50 });
  });
  it("allows ambiguous drafts and only removes references once the last matching business node is deleted", () => {
    const session = createTeamEditorSession({ initial: { ...teamConfiguration, nodes: [teamConfiguration.nodes[0], { ...teamConfiguration.nodes[1], node_id: "reviewer" }] }, saved: false });
    const initial = session.store.getState().snapshot;
    session.updateNode(0, { name: "Duplicate" });
    session.remove({ ...emptySelection(), nodeIds: new Set([initial.configuration.nodes[0].editorId]) });
    expect(session.store.getState().snapshot.configuration.transitions).toEqual(initial.configuration.transitions);
  });
  it("removes edges with their routes, reroutes deleted visual ENDs and keeps one END", () => {
    const session = create();
    const before = session.store.getState().snapshot;
    const end = session.allocateEnd();
    session.commit({ ...before, layout: { ...before.layout, endNodeIds: [...before.layout.endNodeIds, end], endTargets: { "transition-0": end }, edgeRoutes: { "transition-0": { axis: "y", offset: 20 } } } });
    session.remove({ ...emptySelection(), endNodeIds: new Set([end]) });
    expect(session.store.getState().snapshot.layout.endTargets["transition-0"]).toBe("@@canvas-end-1");
    session.remove({ ...emptySelection(), endNodeIds: new Set(["@@canvas-end-1"]) });
    expect(session.store.getState().snapshot.layout.endNodeIds).toHaveLength(1);
    session.remove({ ...emptySelection(), transitionIds: new Set(["transition-0"]) });
    expect(session.store.getState().snapshot.layout.edgeRoutes).toEqual({});
    expect(session.store.getState().snapshot.layout.endTargets).toEqual({});
  });
  it("shares form/canvas history and never recycles identities after undo", () => {
    const session = create();
    session.updateConfiguration({ name: "Form" });
    session.setMode("canvas");
    const first = session.addNode("agent");
    session.undo();
    session.undo();
    expect(session.store.getState().dirty).toBe(false);
    const second = session.addNode("agent");
    expect(second).not.toBe(first);
    expect(session.store.getState().history.future).toEqual([]);
  });
  it("layout and viewport edits do not mark saved business data dirty", () => {
    const session = create();
    const before = session.store.getState().snapshot;
    session.commit({ ...before, layout: { ...before.layout, positions: { ...before.layout.positions, "@@agent-node-0": { x: 900, y: 500 } } } });
    session.setViewport({ x: 20, y: 30, zoom: 0.7 });
    expect(session.store.getState().dirty).toBe(false);
    session.undo();
    expect(session.store.getState().snapshot).toEqual(before);
  });
  it("keeps edits made during first save, advances the submitted baseline, and uses PUT identity next time", async () => {
    const session = createTeamEditorSession({ initial: teamConfiguration, saved: false });
    session.setMode("canvas");
    let finish!: (value: AgentTeamConfiguration) => void;
    const persist = vi.fn(() => new Promise<AgentTeamConfiguration>(resolve => { finish = resolve; }));
    const pending = session.save(persist);
    expect(await session.save(persist)).toBe(false);
    session.updateConfiguration({ name: "Newer" });
    finish(teamConfiguration);
    expect(await pending).toBe(true);
    expect(session.store.getState()).toMatchObject({ dirty: true, saving: false, mode: "canvas", baseline: { name: "Review" }, snapshot: { configuration: { name: "Newer" } } });
    expect(JSON.stringify(persist.mock.calls)).not.toContain("editorId");
    session.undo();
    expect(session.store.getState().dirty).toBe(false);
    const update = vi.fn(async value => saved(value));
    await session.save(update);
    expect(update).toHaveBeenCalledWith(expect.anything(), { teamId: "review", scope: "workspace" });
  });
  it("first save preserves history and view and locks persisted identity through undo", async () => {
    const session = createTeamEditorSession({ saved: false, scope: "system" });
    session.updateConfiguration({ team_id: "new_team", name: "Before" });
    session.setMode("canvas");
    session.setViewport({ x: 80, y: 20, zoom: 0.8 });
    const history = session.store.getState().history.past;
    await session.save(async value => saved(value));
    expect(session.store.getState().history.past).toBe(history);
    expect(session.store.getState()).toMatchObject({ dirty: false, mode: "canvas", viewport: { x: 80, y: 20, zoom: 0.8 } });
    session.undo();
    expect(session.store.getState().snapshot.configuration.team_id).toBe("new_team");
    expect(session.store.getState().dirty).toBe(true);
  });
  it("save failures preserve drafts, history and baseline for retry", async () => {
    const session = create();
    session.updateConfiguration({ name: "Changed" });
    const before = session.store.getState();
    expect(await session.save(async () => { throw new Error("failure"); })).toBe(false);
    expect(session.store.getState().snapshot).toBe(before.snapshot);
    expect(session.store.getState().baseline).toBe(before.baseline);
    expect(session.store.getState()).toMatchObject({ saving: false, dirty: true });
    expect(useConfirmStore.getState().queue.at(-1)).toMatchObject({ title: "保存失败", notice: true });
    expect(await session.save(async value => saved(value))).toBe(true);
    expect(session.store.getState()).toMatchObject({ dirty: false });
  });
  it("restores saved layout by business identity after deletion changes session ID allocation", async () => {
    const session = create();
    const before = session.store.getState().snapshot;
    session.remove({ ...emptySelection(), nodeIds: new Set([before.configuration.nodes[0].editorId]) });
    const savedConfig = saved(exportDraft(session.store.getState().snapshot.configuration));
    await session.save(async () => savedConfig);
    const reopened = createTeamEditorSession({ initial: savedConfig, saved: true, workspaceId: 11 });
    expect(reopened.store.getState().snapshot.layout.positions["@@agent-node-0"]).toEqual(before.layout.positions["@@agent-node-1"]);
  });
  it("compares numeric input semantically across save and undo without normalizing invalid drafts", async () => {
    const session = create();
    session.updateConfiguration({ max_runs: "9" });
    session.updateConfiguration({ max_runs: "8" });
    expect(session.store.getState().dirty).toBe(false);
    session.updateConfiguration({ max_runs: "12" });
    await session.save(async value => saved(value));
    session.updateConfiguration({ name: "Changed" });
    session.undo();
    expect(session.store.getState().dirty).toBe(false);
    session.updateConfiguration({ max_runs: "" });
    expect(session.store.getState().dirty).toBe(true);
    expect(exportDraft(session.store.getState().snapshot.configuration).max_runs).toBe("");
  });
  it("discarding unsaved deletions retains saved node and edge layout preferences", () => {
    const session = create();
    const before = session.store.getState().snapshot;
    session.commit({ ...before, layout: { ...before.layout,
      positions: { ...before.layout.positions, "@@agent-node-0": { x: 700, y: 500 } },
      edgeRoutes: { "transition-0": { axis: "x", offset: 80 } },
    } });
    session.remove({ ...emptySelection(), nodeIds: new Set(["@@agent-node-0"]) });
    // 再移动剩余节点也不应把草稿删除带入已保存实体的偏好。
    const next = session.store.getState().snapshot;
    session.commit({ ...next, layout: { ...next.layout, positions: { ...next.layout.positions, "@@agent-node-1": { x: 900, y: 600 } } } });
    const reopened = create().store.getState().snapshot;
    expect(reopened.layout.positions["@@agent-node-0"]).toEqual({ x: 700, y: 500 });
    expect(reopened.layout.edgeRoutes["transition-0"]).toEqual({ axis: "x", offset: 80 });
    expect(reopened.layout.positions["@@agent-node-1"]).toEqual({ x: 900, y: 600 });
  });
  it("isolates new drafts from storage and ignores corrupt preferences", () => {
    const setItem = vi.spyOn(localStorage, "setItem");
    const session = createTeamEditorSession({ saved: false });
    session.addNode("agent");
    expect(setItem).not.toHaveBeenCalled();
    localStorage.setItem("cosir:agent-team-layout-v2:workspace:11:review", "{broken");
    expect(create().store.getState().snapshot.configuration.nodes).toHaveLength(2);
    expect(create().store.getState().dirty).toBe(false);
  });
});

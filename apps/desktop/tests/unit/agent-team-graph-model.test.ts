import { describe, expect, it } from "vitest";
import { createGraphEdges, createGraphNodes, findAvailableNodePosition, isEndViewId, reconcileGraphNodes, statusFromHandleId, statusHandleId } from "@/components/agent-team-graph-model";
import { createIdentityAllocator, importDraft } from "@/lib/agent-team-editor/model";
import { teamConfiguration } from "./team-editor-fixture";
const draft = (changes: Partial<typeof teamConfiguration> = {}) => importDraft({ ...teamConfiguration, ...changes }, createIdentityAllocator());

describe("Agent Team graph projection", () => {
  it("projects status identities and visual ENDs without changing business targets", () => {
    const config = draft();
    const edges = createGraphEdges(config, { "transition-0": "@@canvas-end-2" }, ["@@canvas-end-1", "@@canvas-end-2"]);
    expect(edges[0]).toMatchObject({ id: "transition-0", source: config.nodes[0].editorId, target: "@@canvas-end-2", sourceHandle: config.nodes[0].statuses[0].editorId });
    expect(edges[1]).toMatchObject({ target: config.nodes[1].editorId, sourceHandle: config.nodes[0].statuses[1].editorId });
    expect(config.transitions[0].target_node_id).toBe("END");
    expect(createGraphNodes(config, {}, ["@@canvas-end-1", "@@canvas-end-2"]).filter(node => node.data.kind === "terminal")).toHaveLength(2);
  });
  it("gives duplicate draft statuses and transitions distinct identities", () => {
    const config = draft({ nodes: [{ ...teamConfiguration.nodes[0], statuses: [teamConfiguration.nodes[0].statuses[0], teamConfiguration.nodes[0].statuses[0]] }], transitions: [teamConfiguration.transitions[0], teamConfiguration.transitions[0]] });
    const edges = createGraphEdges(config, {}, ["@@canvas-end-1"]);
    expect(new Set(edges.map(edge => edge.id)).size).toBe(2);
    expect(edges.map(edge => edge.sourceHandle)).toEqual(config.nodes[0].statuses.map(status => status.editorId));
  });
  it("maps excess duplicate edges to existing exits", () => {
    const config = draft({ transitions: Array.from({ length: 4 }, () => teamConfiguration.transitions[0]) });
    expect(createGraphEdges(config, {}, ["@@canvas-end-1"]).map(edge => edge.sourceHandle)).toEqual(Array(4).fill(config.nodes[0].statuses[0].editorId));
  });
  it("keeps removed status transitions visible with orphan handles", () => {
    const config = draft({ nodes: [{ ...teamConfiguration.nodes[0], statuses: [] }, teamConfiguration.nodes[1]] });
    const edges = createGraphEdges(config, {}, ["@@canvas-end-1"]);
    expect(edges[1].sourceHandle).toBe(statusHandleId("needs_changes"));
    expect(createGraphNodes(config, {}, ["@@canvas-end-1"])[0].data).toMatchObject({ orphanStatuses: ["done", "needs_changes"] });
    expect(statusFromHandleId(statusHandleId("needs_changes"))).toBe("needs_changes");
  });
  it.each(["missing", "duplicate"])("keeps %s endpoint drafts visible and reconnectable", kind => {
    const config = draft({ nodes: kind === "missing" ? [] : [teamConfiguration.nodes[0], { ...teamConfiguration.nodes[1], node_id: "reviewer" }], transitions: [{ from_node_id: "reviewer", status: "done", target_node_id: "reviewer" }] });
    const nodes = createGraphNodes(config, {}, ["@@canvas-end-1"]);
    const edge = createGraphEdges(config, {}, ["@@canvas-end-1"])[0];
    expect(edge).toMatchObject({ source: "@@unresolved-source-transition-0", target: "@@unresolved-target-transition-0" });
    expect(nodes.filter(node => node.data.kind === "unresolved-transition-endpoint")).toHaveLength(2);
    expect(new Set(nodes.map(node => node.id)).size).toBe(nodes.length);
  });
  it("applies undo positions while retaining measured dimensions and selection", () => {
    const config = draft();
    const graph = createGraphNodes(config, {}, ["@@canvas-end-1"]);
    const current = graph.map(node => ({ ...node, position: { x: 740, y: 320 }, measured: { width: 280, height: 160 }, selected: true }));
    expect(reconcileGraphNodes(current, graph)[0]).toMatchObject({ position: graph[0].position, measured: { width: 280, height: 160 }, selected: true });
    expect(reconcileGraphNodes(current.map(node => ({ ...node, dragging: true })), graph)[0].position).toEqual({ x: 740, y: 320 });
  });
  it("finds an open slot and recognizes visual END identities", () => {
    expect(findAvailableNodePosition({ x: 100, y: 100 }, [{ x: 100, y: 100 }])).toEqual({ x: 460, y: 100 });
    expect(isEndViewId("@@canvas-end-2")).toBe(true);
    expect(isEndViewId("canvas-end-2")).toBe(false);
  });
});

import { describe, expect, it } from "vitest";

import {
  createGraphEdges,
  createGraphNodes,
  removeNodesFromConfiguration,
  removeGraphSelection,
  findAvailableNodePosition,
  isEndViewId,
  reconcileGraphNodes,
  statusFromHandleId,
  statusHandleId,
  transitionKey,
  updateNodeInConfiguration,
  upsertTransition,
} from "@/components/agent-team-graph-model";
import type { AgentTeamConfiguration } from "@/lib/api/agent-teams";

const configuration: AgentTeamConfiguration = {
  team_id: "review",
  name: "Review",
  description: "Review changes",
  max_runs: 8,
  start_node_id: "reviewer",
  scope: "workspace",
  nodes: [
    { node_id: "reviewer", name: "Review", agent_id: "reviewer_agent", statuses: ["done", "needs_changes"] },
    { node_id: "fixer", name: "Fix", agent_id: "fixer_agent", statuses: ["done"] },
  ],
  transitions: [
    { from_node_id: "reviewer", status: "done", target_node_id: "END" },
    { from_node_id: "reviewer", status: "needs_changes", target_node_id: "fixer" },
    { from_node_id: "fixer", status: "done", target_node_id: "reviewer" },
  ],
};

describe("Agent Team graph model", () => {
  it("deletes selected nodes, incoming edges and layout references in one graph operation", () => {
    const layout = {
      positions: { reviewer: { x: 10, y: 20 }, fixer: { x: 30, y: 40 }, "@@canvas-end-1": { x: 50, y: 60 } },
      endNodeIds: ["@@canvas-end-1"],
      endTargets: { "reviewer::done": "@@canvas-end-1" },
    };
    const result = removeGraphSelection(configuration, layout, {
      nodeIds: new Set(["reviewer"]),
      endNodeIds: [],
      transitionKeys: new Set(),
    });

    expect(result.configuration.nodes.map((node) => node.node_id)).toEqual(["fixer"]);
    expect(result.configuration.start_node_id).toBe("fixer");
    expect(result.configuration.transitions).toEqual([]);
    expect(result.layout.positions).not.toHaveProperty("reviewer");
    expect(result.layout.endTargets).toEqual({});
  });

  it("keeps one END and reroutes its visual mappings when deleting an END selection", () => {
    const layout = {
      positions: { "@@canvas-end-1": { x: 10, y: 20 }, "@@canvas-end-2": { x: 30, y: 40 } },
      endNodeIds: ["@@canvas-end-1", "@@canvas-end-2"],
      endTargets: { "reviewer::done": "@@canvas-end-2" },
    };
    const result = removeGraphSelection(configuration, layout, {
      nodeIds: new Set(),
      endNodeIds: ["@@canvas-end-2"],
      transitionKeys: new Set(),
    });

    expect(result.layout.endNodeIds).toEqual(["@@canvas-end-1"]);
    expect(result.layout.positions).not.toHaveProperty("@@canvas-end-2");
    expect(result.layout.endTargets).toEqual({ "reviewer::done": "@@canvas-end-1" });
    expect(result.configuration.transitions.find((item) => item.status === "done")?.target_node_id).toBe("END");
  });

  it("does not delete the last END", () => {
    const layout = { positions: { "@@canvas-end-1": { x: 10, y: 20 } }, endNodeIds: ["@@canvas-end-1"], endTargets: {} };
    const result = removeGraphSelection(configuration, layout, {
      nodeIds: new Set(),
      endNodeIds: ["@@canvas-end-1"],
      transitionKeys: new Set(),
    });

    expect(result.configuration).toBe(configuration);
    expect(result.layout).toBe(layout);
  });

  it("deletes a selected transition and its visual END mapping without affecting sibling transitions", () => {
    const layout = {
      positions: {},
      endNodeIds: ["@@canvas-end-1"],
      endTargets: { "reviewer::done": "@@canvas-end-1" },
    };
    const result = removeGraphSelection(configuration, layout, {
      nodeIds: new Set(),
      endNodeIds: [],
      transitionKeys: new Set([transitionKey("reviewer", "done")]),
    });

    expect(result.configuration.transitions.map((item) => transitionKey(item.from_node_id, item.status))).toEqual([
      transitionKey("reviewer", "needs_changes"),
      transitionKey("fixer", "done"),
    ]);
    expect(result.layout.endTargets).toEqual({});
  });

  it("preserves distinct status exits and maps END to a terminal node", () => {
    const edges = createGraphEdges(configuration, {}, ["@@canvas-end-1", "@@canvas-end-2"]);
    const endEdge = edges.find((edge) => edge.source === "reviewer" && edge.data?.status === "done");
    const reviseEdge = edges.find((edge) => edge.source === "reviewer" && edge.data?.status === "needs_changes");

    expect(endEdge).toMatchObject({ target: "@@canvas-end-1", sourceHandle: statusHandleId("done"), label: "done" });
    expect(reviseEdge).toMatchObject({ target: "fixer", sourceHandle: statusHandleId("needs_changes") });
    expect(statusFromHandleId(statusHandleId("needs_changes"))).toBe("needs_changes");
    const graphNodes = createGraphNodes(configuration, {}, ["@@canvas-end-1", "@@canvas-end-2"]);
    expect(graphNodes.map((node) => node.id)).toEqual(["reviewer", "fixer", "@@canvas-end-1", "@@canvas-end-2"]);
    expect(graphNodes.filter((node) => node.data.kind === "terminal").map((node) => node.data.kind === "terminal" ? node.data.label : "")).toEqual(["END", "END"]);
  });

  it("keeps the logical END target while restoring the local visual endpoint mapping", () => {
    const edges = createGraphEdges(configuration, { "reviewer::done": "@@canvas-end-2" }, ["@@canvas-end-1", "@@canvas-end-2"]);
    const endEdge = edges.find((edge) => edge.source === "reviewer" && edge.data?.status === "done");
    expect(endEdge?.target).toBe("@@canvas-end-2");
    expect(configuration.transitions.find((item) => item.status === "done")?.target_node_id).toBe("END");
  });

  it("refreshes graph data without losing React Flow positions and measurements", () => {
    const currentNodes = createGraphNodes(configuration, {}, ["@@canvas-end-1"]).map((node) => node.id === "reviewer"
      ? {
        ...node,
        position: { x: 740, y: 320 },
        measured: { width: 280, height: 160 },
        selected: true,
      }
      : node);
    const nextConfiguration = {
      ...configuration,
      nodes: configuration.nodes.map((node) => node.node_id === "reviewer" ? { ...node, name: "Updated review" } : node),
    };
    const graphNodes = createGraphNodes(nextConfiguration, {}, ["@@canvas-end-1"]);

    const reconciled = reconcileGraphNodes(currentNodes, graphNodes);
    const reviewer = reconciled.find((node) => node.id === "reviewer");

    expect(reviewer).toMatchObject({
      position: { x: 740, y: 320 },
      measured: { width: 280, height: 160 },
      selected: true,
      data: { kind: "agent", node: { name: "Updated review" } },
    });
    expect(reconciled.map((node) => node.id)).toEqual(["reviewer", "fixer", "@@canvas-end-1"]);
  });

  it("replaces only the transition with the same source and status", () => {
    const transitions = upsertTransition(configuration.transitions, {
      from_node_id: "reviewer",
      status: "done",
      target_node_id: "fixer",
    });

    expect(transitions).toHaveLength(3);
    expect(transitions.find((item) => transitionKey(item.from_node_id, item.status) === transitionKey("reviewer", "done"))?.target_node_id).toBe("fixer");
    expect(transitions.find((item) => item.status === "needs_changes")?.target_node_id).toBe("fixer");
  });

  it("removes incoming and outgoing transitions and selects a new entry when needed", () => {
    const next = removeNodesFromConfiguration(configuration, new Set(["reviewer"]));

    expect(next.nodes.map((node) => node.node_id)).toEqual(["fixer"]);
    expect(next.transitions).toEqual([]);
    expect(next.start_node_id).toBe("fixer");
  });

  it("keeps node references in sync and removes transitions for deleted statuses", () => {
    const renamed = updateNodeInConfiguration(configuration, "reviewer", {
      node_id: "review_agent",
      statuses: ["done"],
    });

    expect(renamed.start_node_id).toBe("review_agent");
    expect(renamed.transitions).toEqual([
      { from_node_id: "review_agent", status: "done", target_node_id: "END" },
      { from_node_id: "fixer", status: "done", target_node_id: "review_agent" },
    ]);
  });

  it("rejects a node ID that is already used without changing the graph", () => {
    const next = updateNodeInConfiguration(configuration, "reviewer", { node_id: "fixer" });
    expect(next).toBe(configuration);
  });

  it("places click-added nodes in a nearby open slot and reserves end marker keys", () => {
    const position = findAvailableNodePosition({ x: 100, y: 100 }, [{ x: 100, y: 100 }]);
    expect(position).toEqual({ x: 460, y: 100 });
    expect(isEndViewId("@@canvas-end-2")).toBe(true);
    expect(isEndViewId("canvas-end-2")).toBe(false);
  });
});

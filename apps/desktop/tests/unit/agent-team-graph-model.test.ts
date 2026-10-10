import { describe, expect, it } from "vitest";

import {
  createGraphEdges,
  createGraphNodes,
  agentNodeViewId,
  removeNodesFromConfiguration,
  removeGraphSelection,
  findAvailableNodePosition,
  isEndViewId,
  reconcileGraphNodes,
  statusFromHandleId,
  statusHandleId,
  transitionKey,
  updateNodeInConfiguration,
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
      nodeIndices: new Set([0]),
      endNodeIds: [],
      transitionIndices: new Set(),
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
      nodeIndices: new Set(),
      endNodeIds: ["@@canvas-end-2"],
      transitionIndices: new Set(),
    });

    expect(result.layout.endNodeIds).toEqual(["@@canvas-end-1"]);
    expect(result.layout.positions).not.toHaveProperty("@@canvas-end-2");
    expect(result.layout.endTargets).toEqual({ "reviewer::done": "@@canvas-end-1" });
    expect(result.configuration.transitions.find((item) => item.status === "done")?.target_node_id).toBe("END");
  });

  it("does not delete the last END", () => {
    const layout = { positions: { "@@canvas-end-1": { x: 10, y: 20 } }, endNodeIds: ["@@canvas-end-1"], endTargets: {} };
    const result = removeGraphSelection(configuration, layout, {
      nodeIndices: new Set(),
      endNodeIds: ["@@canvas-end-1"],
      transitionIndices: new Set(),
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
      nodeIndices: new Set(),
      endNodeIds: [],
      transitionIndices: new Set([0]),
    });

    expect(result.configuration.transitions.map((item) => transitionKey(item.from_node_id, item.status))).toEqual([
      transitionKey("reviewer", "needs_changes"),
      transitionKey("fixer", "done"),
    ]);
    expect(result.layout.endTargets).toEqual({});
  });

  it("preserves distinct status exits and maps END to a terminal node", () => {
    const edges = createGraphEdges(configuration, {}, ["@@canvas-end-1", "@@canvas-end-2"]);
    const endEdge = edges.find((edge) => edge.source === agentNodeViewId(0) && edge.data?.status === "done");
    const reviseEdge = edges.find((edge) => edge.source === agentNodeViewId(0) && edge.data?.status === "needs_changes");

    expect(endEdge).toMatchObject({ target: "@@canvas-end-1", sourceHandle: statusHandleId("done"), label: "done" });
    expect(reviseEdge).toMatchObject({ target: agentNodeViewId(1), sourceHandle: statusHandleId("needs_changes") });
    expect(statusFromHandleId(statusHandleId("needs_changes"))).toBe("needs_changes");
    const graphNodes = createGraphNodes(configuration, {}, ["@@canvas-end-1", "@@canvas-end-2"]);
    expect(graphNodes.map((node) => node.id)).toEqual([agentNodeViewId(0), agentNodeViewId(1), "@@canvas-end-1", "@@canvas-end-2"]);
    expect(graphNodes.filter((node) => node.data.kind === "terminal").map((node) => node.data.kind === "terminal" ? node.data.label : "")).toEqual(["END", "END"]);
  });

  it("assigns unique canvas identities to duplicate status drafts and transition drafts", () => {
    const draft: AgentTeamConfiguration = {
      ...configuration,
      nodes: [{ ...configuration.nodes[0], statuses: ["done", "done"] }, configuration.nodes[1]],
      transitions: [
        { from_node_id: "reviewer", status: "done", target_node_id: "END" },
        { from_node_id: "reviewer", status: "done", target_node_id: "fixer" },
      ],
    };
    const edges = createGraphEdges(draft, {}, ["@@canvas-end-1"]);
    const graphNodes = createGraphNodes(draft, {}, ["@@canvas-end-1"]);
    const reviewer = graphNodes.find((node) => node.id === agentNodeViewId(0));

    expect(edges.map((edge) => edge.id)).toEqual(["transition-0", "transition-1"]);
    expect(edges.map((edge) => edge.sourceHandle)).toEqual([statusHandleId("done"), statusHandleId("done", 1)]);
    expect(reviewer?.data.kind === "agent" && reviewer.data.node.statuses).toEqual(["done", "done"]);
  });

  it("renders transitions whose status was removed so the draft remains visible", () => {
    const draft: AgentTeamConfiguration = {
      ...configuration,
      nodes: [{ ...configuration.nodes[0], statuses: ["done"] }, configuration.nodes[1]],
      transitions: [{ from_node_id: "reviewer", status: "needs_changes", target_node_id: "fixer" }],
    };
    const edges = createGraphEdges(draft, {}, ["@@canvas-end-1"]);
    const graphNode = createGraphNodes(draft, {}, ["@@canvas-end-1"])[0];

    expect(edges).toHaveLength(1);
    expect(edges[0].sourceHandle).toBe(statusHandleId("needs_changes"));
    expect(graphNode.data.kind === "agent" && graphNode.data.orphanStatuses).toEqual(["needs_changes"]);
  });

  it("keeps transitions with missing endpoints visible as reconnectable draft edges", () => {
    const invalidDraft = {
      ...configuration,
      transitions: [
        { from_node_id: "missing_source", status: "done", target_node_id: "reviewer" },
        { from_node_id: "reviewer", status: "done", target_node_id: "missing_target" },
      ],
    };
    const graphNodes = createGraphNodes(invalidDraft, {}, ["@@canvas-end-1"]);
    const edges = createGraphEdges(invalidDraft, {}, ["@@canvas-end-1"]);
    expect(graphNodes.map((node) => node.id)).toContain("@@unresolved-source-0");
    expect(graphNodes.map((node) => node.id)).toContain("@@unresolved-target-1");
    expect(edges.map((edge) => [edge.source, edge.target])).toEqual([
      ["@@unresolved-source-0", agentNodeViewId(0)],
      [agentNodeViewId(0), "@@unresolved-target-1"],
    ]);
  });

  it("maps excess duplicate transitions to an existing status handle", () => {
    const invalidDraft = {
      ...configuration,
      transitions: Array.from({ length: 4 }, () => ({ from_node_id: "reviewer", status: "done", target_node_id: "fixer" })),
    };
    const edges = createGraphEdges(invalidDraft, {}, ["@@canvas-end-1"]);
    expect(edges.map((edge) => edge.sourceHandle)).toEqual([
      "status:done", "status:done", "status:done", "status:done",
    ]);
  });

  it("uses distinct canvas identities when draft node IDs are duplicated", () => {
    const draft = {
      ...configuration,
      nodes: [configuration.nodes[0], { ...configuration.nodes[1], node_id: "reviewer" }],
      transitions: [
        { from_node_id: "reviewer", status: "done", target_node_id: "reviewer" },
      ],
    };
    const graphNodes = createGraphNodes(draft, {}, ["@@canvas-end-1"]);
    const edges = createGraphEdges(draft, {}, ["@@canvas-end-1"]);

    expect(graphNodes.slice(0, 2).map((node) => node.id)).toEqual([agentNodeViewId(0), agentNodeViewId(1)]);
    expect(graphNodes.every((node) => node.data.kind !== "agent" || node.data.orphanStatuses.length === 0)).toBe(true);
    expect(graphNodes.map((node) => node.id)).toContain("@@unresolved-source-0");
    expect(graphNodes.map((node) => node.id)).toContain("@@unresolved-target-0");
    expect(edges[0]).toMatchObject({ source: "@@unresolved-source-0", target: "@@unresolved-target-0" });
    expect(graphNodes.find((node) => node.id === "@@unresolved-source-0")?.data).toMatchObject({
      label: "来源节点 ID 重复，请先修正：reviewer",
    });
    expect(graphNodes.find((node) => node.id === "@@unresolved-target-0")?.data).toMatchObject({
      label: "目标节点 ID 重复，请先修正：reviewer",
    });
  });

  it("keeps the logical END target while restoring the local visual endpoint mapping", () => {
    const edges = createGraphEdges(configuration, { "reviewer::done": "@@canvas-end-2" }, ["@@canvas-end-1", "@@canvas-end-2"]);
    const endEdge = edges.find((edge) => edge.source === agentNodeViewId(0) && edge.data?.status === "done");
    expect(endEdge?.target).toBe("@@canvas-end-2");
    expect(configuration.transitions.find((item) => item.status === "done")?.target_node_id).toBe("END");
  });

  it("refreshes graph data without losing React Flow positions and measurements", () => {
    const currentNodes = createGraphNodes(configuration, {}, ["@@canvas-end-1"]).map((node) => node.id === agentNodeViewId(0)
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
    const reviewer = reconciled.find((node) => node.id === agentNodeViewId(0));

    expect(reviewer).toMatchObject({
      position: { x: 740, y: 320 },
      measured: { width: 280, height: 160 },
      selected: true,
      data: { kind: "agent", node: { name: "Updated review" } },
    });
    expect(reconciled.map((node) => node.id)).toEqual([agentNodeViewId(0), agentNodeViewId(1), "@@canvas-end-1"]);
  });

  it("removes incoming and outgoing transitions and selects a new entry when needed", () => {
    const next = removeNodesFromConfiguration(configuration, new Set([0]));

    expect(next.nodes.map((node) => node.node_id)).toEqual(["fixer"]);
    expect(next.transitions).toEqual([]);
    expect(next.start_node_id).toBe("fixer");
  });

  it("keeps node references in sync and retains transitions for removed statuses as a draft", () => {
    const renamed = updateNodeInConfiguration(configuration, 0, {
      node_id: "review_agent",
      statuses: ["done"],
    });

    expect(renamed.start_node_id).toBe("review_agent");
    expect(renamed.transitions).toEqual([
      { from_node_id: "review_agent", status: "done", target_node_id: "END" },
      { from_node_id: "review_agent", status: "needs_changes", target_node_id: "fixer" },
      { from_node_id: "fixer", status: "done", target_node_id: "review_agent" },
    ]);
  });

  it("allows duplicate business node IDs in the draft", () => {
    const next = updateNodeInConfiguration(configuration, 0, { node_id: "fixer" });
    expect(next.nodes.map((node) => node.node_id)).toEqual(["fixer", "fixer"]);
    expect(next.transitions[0].from_node_id).toBe("fixer");
  });

  it("retains transitions while one of multiple duplicate-ID nodes is removed", () => {
    const duplicateConfiguration = {
      ...configuration,
      nodes: [configuration.nodes[0], { ...configuration.nodes[1], node_id: "reviewer" }],
    };
    const next = removeNodesFromConfiguration(duplicateConfiguration, new Set([0]));
    expect(next.nodes).toHaveLength(1);
    expect(next.transitions).toEqual(duplicateConfiguration.transitions);
  });

  it("places click-added nodes in a nearby open slot and reserves end marker keys", () => {
    const position = findAvailableNodePosition({ x: 100, y: 100 }, [{ x: 100, y: 100 }]);
    expect(position).toEqual({ x: 460, y: 100 });
    expect(isEndViewId("@@canvas-end-2")).toBe(true);
    expect(isEndViewId("canvas-end-2")).toBe(false);
  });
});

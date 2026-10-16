import { MarkerType, type Edge, type Node, type XYPosition } from "@xyflow/react";

import type { EditorDraft as AgentTeamConfigurationDraft, EditorNode as AgentTeamNodeConfiguration, EditorLayout, EdgeRoute } from "@/lib/agent-team-editor/model";
import { DEFAULT_END_VIEW_ID, END_VIEW_ID_PREFIX, END_NODE_ID } from "@/lib/agent-team-editor/model";
export { DEFAULT_END_VIEW_ID, END_VIEW_ID_PREFIX, END_NODE_ID } from "@/lib/agent-team-editor/model";
import { findAgentTeamStatus } from "@/lib/agent-team-status";

export type TeamGraphNodeData = {
  kind: "agent";
  nodeIndex: number;
  node: AgentTeamNodeConfiguration;
  isStart: boolean;
  orphanStatuses: string[];
} | {
  kind: "terminal";
  label: string;
  endNodeId: string;
} | {
  kind: "unresolved-transition-endpoint";
  side: "source" | "target";
  transitionIndex: number;
  status: string;
  label: string;
};

export type TeamGraphNode = Node<TeamGraphNodeData>;
export type AgentTeamEdgeRoute = EdgeRoute;
export type TeamGraphEdge = Edge<{
  status: string;
  transitionIndex: number;
  routeKey: string;
  route: AgentTeamEdgeRoute | null;
}>;
export type AgentTeamGraphLayout = EditorLayout;

export function isEndViewId(nodeId: string | null | undefined): nodeId is string {
  return Boolean(nodeId?.startsWith(END_VIEW_ID_PREFIX));
}

export function unresolvedEndpointViewId(side: "source" | "target", transitionId: string): string {
  return `@@unresolved-${side}-${transitionId}`;
}

export function statusHandleId(status: string, duplicateIndex = 0): string {
  return `status:${encodeURIComponent(status)}${duplicateIndex > 0 ? `#${duplicateIndex}` : ""}`;
}

export function statusFromHandleId(handleId: string | null | undefined): string | null {
  if (!handleId?.startsWith("status:")) return null;
  const encodedStatus = handleId.slice("status:".length).split("#", 1)[0];
  return decodeURIComponent(encodedStatus);
}

export function createGraphEdges(
  configuration: AgentTeamConfigurationDraft,
  endTargets: Record<string, string>,
  endNodeIds: string[],
  edgeRoutes: Record<string, AgentTeamEdgeRoute> = {},
): TeamGraphEdge[] {
  return configuration.transitions.flatMap((transition, transitionIndex) => {
    const sourceIndices = configuration.nodes
      .map((node, index) => node.node_id === transition.from_node_id ? index : -1)
      .filter((index) => index >= 0);
    const sourceIndex = sourceIndices.length === 1 ? sourceIndices[0] : -1;
    const source = configuration.nodes[sourceIndex];
    const targetIndices = configuration.nodes
      .map((node, index) => node.node_id === transition.target_node_id ? index : -1)
      .filter((index) => index >= 0);
    const targetExists = transition.target_node_id === END_NODE_ID || targetIndices.length === 1;
    const duplicateIndex = configuration.transitions
      .slice(0, transitionIndex)
      .filter((item) => item.from_node_id === transition.from_node_id && item.status === transition.status).length;
    const matchingStatusCount = source?.statuses.filter((item) => item.name === transition.status).length ?? 0;
    const statusDuplicateIndex = matchingStatusCount > 0
      ? Math.min(duplicateIndex, matchingStatusCount - 1)
      : source ? duplicateIndex : 0;
    const sourceViewId = source
      ? source.editorId
      : unresolvedEndpointViewId("source", transition.editorId);
    const targetViewId = transition.target_node_id === END_NODE_ID
      ? endTargets[transition.editorId] ?? endNodeIds[0] ?? DEFAULT_END_VIEW_ID
      : targetExists
        ? configuration.nodes[targetIndices[0]].editorId
        : unresolvedEndpointViewId("target", transition.editorId);
    const statusColor = findAgentTeamStatus(source?.statuses ?? [], transition.status)?.color
      ?? "color-mix(in oklab, var(--muted-foreground) 45%, transparent)";
    const routeKey = transition.editorId;
    return [{
      id: transition.editorId,
      source: sourceViewId,
      sourceHandle: source?.statuses.filter(status => status.name === transition.status)[statusDuplicateIndex]?.editorId ?? statusHandleId(transition.status, statusDuplicateIndex),
      target: targetViewId,
      targetHandle: "input",
      label: transition.status,
      data: {
        status: transition.status,
        transitionIndex,
        routeKey,
        route: edgeRoutes[routeKey] ?? null,
      },
      style: { stroke: statusColor },
      markerEnd: { type: MarkerType.ArrowClosed, color: statusColor },
      animated: transition.target_node_id !== END_NODE_ID,
      type: "teamRouted",
      interactionWidth: 24,
    }];
  });
}

export function createGraphNodes(
  configuration: AgentTeamConfigurationDraft,
  positions: Record<string, XYPosition>,
  endNodeIds: string[],
): TeamGraphNode[] {
  const nodes: TeamGraphNode[] = configuration.nodes.map((node, index) => ({
    id: node.editorId,
    type: "team",
    position: positions[node.editorId] ?? defaultNodePosition(index),
    data: {
      kind: "agent",
      nodeIndex: index,
      node,
      isStart: node.node_id === configuration.start_node_id,
      orphanStatuses: configuration.transitions
        .filter((transition) => transition.from_node_id === node.node_id && !node.statuses.some((status) => status.name === transition.status))
        .filter(() => configuration.nodes.filter((item) => item.node_id === node.node_id).length === 1)
        .map((transition) => transition.status),
    },
  }));
  endNodeIds.forEach((endNodeId, index) => nodes.push({
    id: endNodeId,
    type: "terminal",
    position: positions[endNodeId] ?? { x: 420, y: Math.max(80, configuration.nodes.length * 180) + index * 110 },
    data: { kind: "terminal", label: END_NODE_ID, endNodeId },
    draggable: true,
    selectable: true,
  }));
  configuration.transitions.forEach((transition, transitionIndex) => {
    const sourceMatches = configuration.nodes.filter((node) => node.node_id === transition.from_node_id).length;
    const sourceExists = sourceMatches === 1;
    const targetMatches = configuration.nodes.filter((node) => node.node_id === transition.target_node_id).length;
    const targetExists = transition.target_node_id === END_NODE_ID
      || targetMatches === 1;
    if (!sourceExists) nodes.push({
      id: unresolvedEndpointViewId("source", transition.editorId),
      type: "unresolved",
      position: { x: 520, y: 100 + transitionIndex * 90 },
      data: {
        kind: "unresolved-transition-endpoint",
        side: "source",
        transitionIndex,
        status: transition.status,
        label: sourceMatches > 1
          ? `来源节点 ID 重复，请先修正：${transition.from_node_id}`
          : `未找到来源节点：${transition.from_node_id || "（空）"}`,
      },
      draggable: true,
      selectable: false,
    });
    if (!targetExists) nodes.push({
      id: unresolvedEndpointViewId("target", transition.editorId),
      type: "unresolved",
      position: { x: 980, y: 100 + transitionIndex * 90 },
      data: {
        kind: "unresolved-transition-endpoint",
        side: "target",
        transitionIndex,
        status: transition.status,
        label: targetMatches > 1
          ? `目标节点 ID 重复，请先修正：${transition.target_node_id}`
          : `未找到目标节点：${transition.target_node_id || "（空）"}`,
      },
      draggable: true,
      selectable: false,
    });
  });
  return nodes;
}

/** 按图定义增删节点并刷新业务数据，同时保留 React Flow 维护的交互与测量状态。 */
export function reconcileGraphNodes(
  currentNodes: TeamGraphNode[],
  graphNodes: TeamGraphNode[],
): TeamGraphNode[] {
  const currentById = new Map(currentNodes.map((node) => [node.id, node]));
  return graphNodes.map((graphNode) => {
    const current = currentById.get(graphNode.id);
    if (!current) return graphNode;
    return {
      ...graphNode,
      position: current.dragging ? current.position : graphNode.position,
      dragging: current.dragging,
      measured: current.measured,
      width: current.width,
      height: current.height,
      initialWidth: current.initialWidth,
      initialHeight: current.initialHeight,
      selected: current.selected,
    };
  });
}

export function defaultNodePosition(index: number): XYPosition {
  return { x: 100, y: 80 + index * 180 };
}

export function findAvailableNodePosition(
  preferred: XYPosition,
  occupied: XYPosition[],
): XYPosition {
  const stepX = 360;
  const stepY = 240;
  const hasRoom = (candidate: XYPosition) => occupied.every((position) =>
    candidate.x + 300 < position.x || position.x + 300 < candidate.x
      || candidate.y + 200 < position.y || position.y + 200 < candidate.y,
  );

  // 画布节点数量有限；逐圈向外搜索，避免节点较多时固定候选位耗尽后直接重叠。
  for (let radius = 0; ; radius += 1) {
    if (radius === 0) {
      if (hasRoom(preferred)) return preferred;
      continue;
    }
    const cardinalOffsets = [
      { x: radius, y: 0 }, { x: -radius, y: 0 },
      { x: 0, y: radius }, { x: 0, y: -radius },
    ];
    for (const offset of cardinalOffsets) {
      const candidate = { x: preferred.x + offset.x * stepX, y: preferred.y + offset.y * stepY };
      if (hasRoom(candidate)) return candidate;
    }
    for (let x = -radius; x <= radius; x += 1) {
      for (const y of [-radius, radius]) {
        if (x === 0) continue;
        const candidate = { x: preferred.x + x * stepX, y: preferred.y + y * stepY };
        if (hasRoom(candidate)) return candidate;
      }
    }
    for (let y = -radius + 1; y < radius; y += 1) {
      for (const x of [-radius, radius]) {
        if (y === 0) continue;
        const candidate = { x: preferred.x + x * stepX, y: preferred.y + y * stepY };
        if (hasRoom(candidate)) return candidate;
      }
    }
  }
}

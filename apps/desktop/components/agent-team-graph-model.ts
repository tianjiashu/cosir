import type { Edge, Node, XYPosition } from "@xyflow/react";

import type {
  AgentTeamConfigurationDraft,
  AgentTeamNodeConfiguration,
} from "@/lib/api/agent-teams";

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
export type TeamGraphEdge = Edge<{ status: string; transitionIndex: number }>;
export type AgentTeamGraphLayout = {
  positions: Record<string, XYPosition>;
  endNodeIds: string[];
  endTargets: Record<string, string>;
};

export const END_NODE_ID = "END";
export const END_VIEW_ID_PREFIX = "@@canvas-end-";
export const DEFAULT_END_VIEW_ID = `${END_VIEW_ID_PREFIX}1`;

export function isEndViewId(nodeId: string | null | undefined): nodeId is string {
  return Boolean(nodeId?.startsWith(END_VIEW_ID_PREFIX));
}

export function transitionKey(fromNodeId: string, status: string): string {
  return `${fromNodeId}::${status}`;
}

export function agentNodeViewId(nodeIndex: number): string {
  return `@@agent-node-${nodeIndex}`;
}

export function agentNodeIndexFromViewId(viewId: string): number {
  const match = /^@@agent-node-(\d+)$/.exec(viewId);
  return match ? Number(match[1]) : -1;
}

export function unresolvedEndpointViewId(side: "source" | "target", transitionIndex: number): string {
  return `@@unresolved-${side}-${transitionIndex}`;
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
    const matchingStatusCount = source?.statuses.filter((item) => item === transition.status).length ?? 0;
    const statusDuplicateIndex = matchingStatusCount > 0
      ? Math.min(duplicateIndex, matchingStatusCount - 1)
      : source ? duplicateIndex : 0;
    const sourceViewId = source
      ? agentNodeViewId(sourceIndex)
      : unresolvedEndpointViewId("source", transitionIndex);
    const targetViewId = transition.target_node_id === END_NODE_ID
      ? endTargets[transitionKey(transition.from_node_id, transition.status)] ?? endNodeIds[0] ?? DEFAULT_END_VIEW_ID
      : targetExists
        ? agentNodeViewId(configuration.nodes.findIndex((node) => node.node_id === transition.target_node_id))
        : unresolvedEndpointViewId("target", transitionIndex);
    return [{
      id: `transition-${transitionIndex}`,
      source: sourceViewId,
      sourceHandle: statusHandleId(transition.status, statusDuplicateIndex),
      target: targetViewId,
      targetHandle: "input",
      label: transition.status,
      data: { status: transition.status, transitionIndex },
      animated: transition.target_node_id !== END_NODE_ID,
      type: "smoothstep",
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
    id: agentNodeViewId(index),
    type: "team",
    position: positions[agentNodeViewId(index)]
      ?? (configuration.nodes.filter((item) => item.node_id === node.node_id).length === 1 ? positions[node.node_id] : undefined)
      ?? defaultNodePosition(index),
    data: {
      kind: "agent",
      nodeIndex: index,
      node,
      isStart: node.node_id === configuration.start_node_id,
      orphanStatuses: configuration.transitions
        .filter((transition) => transition.from_node_id === node.node_id && !node.statuses.includes(transition.status))
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
      id: unresolvedEndpointViewId("source", transitionIndex),
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
      id: unresolvedEndpointViewId("target", transitionIndex),
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
      position: current.position,
      measured: current.measured,
      width: current.width,
      height: current.height,
      initialWidth: current.initialWidth,
      initialHeight: current.initialHeight,
      selected: current.selected,
    };
  });
}

/** 同步节点 ID 引用；状态与转移不一致时保留原草稿，交由保存接口校验。 */
export function updateNodeInConfiguration(
  configuration: AgentTeamConfigurationDraft,
  nodeIndex: number,
  changes: Partial<AgentTeamNodeConfiguration>,
): AgentTeamConfigurationDraft {
  const current = configuration.nodes[nodeIndex];
  if (!current) return configuration;

  const oldId = current.node_id;
  const nextId = changes.node_id ?? oldId;
  const canResolveIdReferences = configuration.nodes.filter((node) => node.node_id === oldId).length === 1;
  const nodes = configuration.nodes.map((node, index) => index === nodeIndex ? { ...node, ...changes } : node);
  const transitions = canResolveIdReferences && nextId !== oldId
    ? configuration.transitions.map((transition) => ({
      ...transition,
      from_node_id: transition.from_node_id === oldId ? nextId : transition.from_node_id,
      target_node_id: transition.target_node_id === oldId ? nextId : transition.target_node_id,
    }))
    : configuration.transitions;

  return {
    ...configuration,
    nodes,
    transitions,
    start_node_id: canResolveIdReferences && configuration.start_node_id === oldId ? nextId : configuration.start_node_id,
  };
}

export function removeNodesFromConfiguration(
  configuration: AgentTeamConfigurationDraft,
  nodeIndices: Set<number>,
): AgentTeamConfigurationDraft {
  const nodes = configuration.nodes.filter((_, index) => !nodeIndices.has(index));
  const removedIds = new Set([...nodeIndices]
    .map((index) => configuration.nodes[index]?.node_id)
    .filter((id): id is string => id !== undefined && !nodes.some((node) => node.node_id === id)));
  const nextStart = nodes.some((node) => node.node_id === configuration.start_node_id)
    ? configuration.start_node_id
    : nodes[0]?.node_id ?? "";
  return {
    ...configuration,
    nodes,
    start_node_id: nextStart,
    transitions: configuration.transitions.filter(
      (transition) => !removedIds.has(transition.from_node_id) && !removedIds.has(transition.target_node_id),
    ),
  };
}

/**
 * 一次性删除画布选区，并同步修复配置引用和本地 END 落点。
 * 业务节点删除会移除所有入边、出边；视觉 END 至少保留一个，移除 END 的转移改指向剩余出口。
 */
export function removeGraphSelection(
  configuration: AgentTeamConfigurationDraft,
  layout: AgentTeamGraphLayout,
  selection: { nodeIndices: Set<number>; endNodeIds: string[]; transitionIndices: Set<number> },
): { configuration: AgentTeamConfigurationDraft; layout: AgentTeamGraphLayout } {
  const remainingBusinessNodes = configuration.nodes.filter((_, index) => !selection.nodeIndices.has(index));
  const nodes = new Set([...selection.nodeIndices]
    .map((index) => configuration.nodes[index]?.node_id)
    .filter((id): id is string => id !== undefined && !remainingBusinessNodes.some((node) => node.node_id === id)));
  const remainingEndNodeIds = [...layout.endNodeIds];
  for (const endNodeId of selection.endNodeIds) {
    if (remainingEndNodeIds.length <= 1) break;
    const index = remainingEndNodeIds.indexOf(endNodeId);
    if (index >= 0) remainingEndNodeIds.splice(index, 1);
  }
  const removedEndIds = new Set(layout.endNodeIds.filter((id) => !remainingEndNodeIds.includes(id)));
  const transitions = configuration.transitions.filter((item, index) =>
    !selection.transitionIndices.has(index)
      && !nodes.has(item.from_node_id)
      && !nodes.has(item.target_node_id),
  );
  const nextConfiguration = transitions.length === configuration.transitions.length && !selection.nodeIndices.size
    ? configuration
    : { ...configuration, nodes: remainingBusinessNodes, transitions };
  const nextStartNodeId = nextConfiguration.nodes.some((node) => node.node_id === configuration.start_node_id)
    ? configuration.start_node_id
    : nextConfiguration.nodes[0]?.node_id ?? "";
  if (nextConfiguration.start_node_id !== nextStartNodeId) nextConfiguration.start_node_id = nextStartNodeId;
  const removedTransitionCount = configuration.transitions.length - transitions.length;
  if (!nodes.size && !removedEndIds.size && !removedTransitionCount) return { configuration, layout };
  const positions = { ...layout.positions };
  for (const id of [...nodes, ...removedEndIds]) delete positions[id];
  const removedIndices = [...selection.nodeIndices].filter((index) => configuration.nodes[index] !== undefined).sort((left, right) => left - right);
  for (const index of removedIndices) delete positions[agentNodeViewId(index)];
  configuration.nodes.forEach((_, previousIndex) => {
    if (selection.nodeIndices.has(previousIndex)) return;
    const nextIndex = previousIndex - removedIndices.filter((removedIndex) => removedIndex < previousIndex).length;
    if (nextIndex === previousIndex) return;
    const previousId = agentNodeViewId(previousIndex);
    const nextId = agentNodeViewId(nextIndex);
    if (positions[previousId]) positions[nextId] = positions[previousId];
    delete positions[previousId];
  });
  const fallbackEndId = remainingEndNodeIds[0];
  const endTargets = Object.fromEntries(Object.entries(layout.endTargets)
    .filter(([key]) => nextConfiguration.transitions.some((item) => transitionKey(item.from_node_id, item.status) === key && item.target_node_id === END_NODE_ID))
    .map(([key, endNodeId]) => [key, removedEndIds.has(endNodeId) ? fallbackEndId : endNodeId]));

  return {
    configuration: nextConfiguration,
    layout: { ...layout, endNodeIds: remainingEndNodeIds, positions, endTargets },
  };
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

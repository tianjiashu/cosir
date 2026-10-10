import type { Edge, Node, XYPosition } from "@xyflow/react";

import type {
  AgentTeamConfiguration,
  AgentTeamNodeConfiguration,
  AgentTeamTransitionConfiguration,
} from "@/lib/api/agent-teams";

export type TeamGraphNodeData = {
  kind: "agent";
  node: AgentTeamNodeConfiguration;
  isStart: boolean;
  hasTransitionByStatus: Record<string, boolean>;
} | {
  kind: "terminal";
  label: string;
  endNodeId: string;
};

export type TeamGraphNode = Node<TeamGraphNodeData>;
export type TeamGraphEdge = Edge<{ status: string }>;
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

export function statusHandleId(status: string): string {
  return `status:${encodeURIComponent(status)}`;
}

export function statusFromHandleId(handleId: string | null | undefined): string | null {
  if (!handleId?.startsWith("status:")) return null;
  return decodeURIComponent(handleId.slice("status:".length));
}

export function createGraphEdges(
  configuration: AgentTeamConfiguration,
  endTargets: Record<string, string>,
  endNodeIds: string[],
): TeamGraphEdge[] {
  return configuration.transitions.map((transition) => ({
    id: transitionKey(transition.from_node_id, transition.status),
    source: transition.from_node_id,
    sourceHandle: statusHandleId(transition.status),
    target: transition.target_node_id === END_NODE_ID
      ? endTargets[transitionKey(transition.from_node_id, transition.status)] ?? endNodeIds[0] ?? DEFAULT_END_VIEW_ID
      : transition.target_node_id,
    targetHandle: "input",
    label: transition.status,
    data: { status: transition.status },
    animated: transition.target_node_id !== END_NODE_ID,
    type: "smoothstep",
  }));
}

export function createGraphNodes(
  configuration: AgentTeamConfiguration,
  positions: Record<string, XYPosition>,
  endNodeIds: string[],
): TeamGraphNode[] {
  const transitionKeys = new Set(
    configuration.transitions.map((transition) => transitionKey(transition.from_node_id, transition.status)),
  );
  const nodes: TeamGraphNode[] = configuration.nodes.map((node, index) => ({
    id: node.node_id,
    type: "team",
    position: positions[node.node_id] ?? defaultNodePosition(index),
    data: {
      kind: "agent",
      node,
      isStart: node.node_id === configuration.start_node_id,
      hasTransitionByStatus: Object.fromEntries(
        node.statuses.map((status) => [status, transitionKeys.has(transitionKey(node.node_id, status))]),
      ),
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

export function upsertTransition(
  transitions: AgentTeamTransitionConfiguration[],
  next: AgentTeamTransitionConfiguration,
): AgentTeamTransitionConfiguration[] {
  const existingIndex = transitions.findIndex(
    (transition) => transition.from_node_id === next.from_node_id && transition.status === next.status,
  );
  if (existingIndex < 0) return [...transitions, next];
  return transitions.map((transition, index) => index === existingIndex ? next : transition);
}

/** 同步节点 ID 与业务状态变更，避免配置表单留下失效的转移引用。 */
export function updateNodeInConfiguration(
  configuration: AgentTeamConfiguration,
  nodeId: string,
  changes: Partial<AgentTeamNodeConfiguration>,
): AgentTeamConfiguration {
  const current = configuration.nodes.find((node) => node.node_id === nodeId);
  if (!current) return configuration;

  const nextId = changes.node_id ?? current.node_id;
  if (configuration.nodes.some((node) => node.node_id === nextId && node.node_id !== nodeId)) return configuration;
  const statuses = changes.statuses ?? current.statuses;
  const nodes = configuration.nodes.map((node) => node.node_id === nodeId ? { ...node, ...changes } : node);
  const transitions = configuration.transitions
    .map((transition) => ({
      ...transition,
      from_node_id: transition.from_node_id === nodeId ? nextId : transition.from_node_id,
      target_node_id: transition.target_node_id === nodeId ? nextId : transition.target_node_id,
    }))
    .filter((transition) => transition.from_node_id !== nextId || statuses.includes(transition.status));

  return {
    ...configuration,
    nodes,
    transitions,
    start_node_id: configuration.start_node_id === nodeId && nextId
      ? nextId
      : configuration.start_node_id,
  };
}

export function removeNodesFromConfiguration(
  configuration: AgentTeamConfiguration,
  nodeIds: Set<string>,
): AgentTeamConfiguration {
  const nodes = configuration.nodes.filter((node) => !nodeIds.has(node.node_id));
  const nextStart = nodes.some((node) => node.node_id === configuration.start_node_id)
    ? configuration.start_node_id
    : nodes[0]?.node_id ?? "";
  return {
    ...configuration,
    nodes,
    start_node_id: nextStart,
    transitions: configuration.transitions.filter(
      (transition) => !nodeIds.has(transition.from_node_id) && !nodeIds.has(transition.target_node_id),
    ),
  };
}

/**
 * 一次性删除画布选区，并同步修复配置引用和本地 END 落点。
 * 业务节点删除会移除所有入边、出边；视觉 END 至少保留一个，移除 END 的转移改指向剩余出口。
 */
export function removeGraphSelection(
  configuration: AgentTeamConfiguration,
  layout: AgentTeamGraphLayout,
  selection: { nodeIds: Set<string>; endNodeIds: string[]; transitionKeys: Set<string> },
): { configuration: AgentTeamConfiguration; layout: AgentTeamGraphLayout } {
  const nodes = new Set(configuration.nodes.filter((node) => selection.nodeIds.has(node.node_id)).map((node) => node.node_id));
  const remainingEndNodeIds = [...layout.endNodeIds];
  for (const endNodeId of selection.endNodeIds) {
    if (remainingEndNodeIds.length <= 1) break;
    const index = remainingEndNodeIds.indexOf(endNodeId);
    if (index >= 0) remainingEndNodeIds.splice(index, 1);
  }
  const removedEndIds = new Set(layout.endNodeIds.filter((id) => !remainingEndNodeIds.includes(id)));
  const existingTransitionKeys = new Set(configuration.transitions.map((item) => transitionKey(item.from_node_id, item.status)));
  const transitionKeys = new Set([...selection.transitionKeys].filter((key) => existingTransitionKeys.has(key)));
  if (!nodes.size && !removedEndIds.size && !transitionKeys.size) return { configuration, layout };
  const withoutNodes = nodes.size ? removeNodesFromConfiguration(configuration, nodes) : configuration;
  const transitions = withoutNodes.transitions.filter((item) => !transitionKeys.has(transitionKey(item.from_node_id, item.status)));
  const nextConfiguration = transitions.length === withoutNodes.transitions.length
    ? withoutNodes
    : { ...withoutNodes, transitions };
  const positions = { ...layout.positions };
  for (const id of [...nodes, ...removedEndIds]) delete positions[id];
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

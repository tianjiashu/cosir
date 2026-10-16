import type { AgentTeamConfigurationDraft, AgentTeamNodeConfiguration, AgentTeamStatusConfiguration, AgentTeamTransitionConfiguration } from "@/lib/api/agent-teams";

export type EditorStatus = AgentTeamStatusConfiguration & { editorId: string };
export type EditorNode = Omit<AgentTeamNodeConfiguration, "statuses"> & { editorId: string; statuses: EditorStatus[] };
export type EditorTransition = AgentTeamTransitionConfiguration & { editorId: string };
export type EditorDraft = Omit<AgentTeamConfigurationDraft, "nodes" | "transitions"> & { nodes: EditorNode[]; transitions: EditorTransition[] };
export type Position = { x: number; y: number };
export type EdgeRoute = { axis: "x" | "y"; offset: number };
export type EditorLayout = { positions: Record<string, Position>; endNodeIds: string[]; endTargets: Record<string, string>; edgeRoutes: Record<string, EdgeRoute> };
export type EditorSnapshot = { configuration: EditorDraft; layout: EditorLayout };
export const END_NODE_ID = "END";
export const END_VIEW_ID_PREFIX = "@@canvas-end-";
export const DEFAULT_END_VIEW_ID = `${END_VIEW_ID_PREFIX}1`;
export const emptyLayout = (): EditorLayout => ({ positions: {}, endNodeIds: [DEFAULT_END_VIEW_ID], endTargets: {}, edgeRoutes: {} });
export const emptyConfiguration = (scope: AgentTeamConfigurationDraft["scope"]): AgentTeamConfigurationDraft => ({ team_id: "", name: "", description: "", max_runs: 10, start_node_id: "", nodes: [], transitions: [], scope });

/** 编辑身份与业务字段分离；计数器属于会话，不随撤销回退，避免旧选区命中新条目。 */
export function createIdentityAllocator() {
  const counts = { node: 0, status: 0, transition: 0, end: 2 };
  return (kind: keyof typeof counts) => `${kind === "node" ? "@@agent-node" : kind === "end" ? "@@canvas-end" : kind}-${counts[kind]++}`;
}
export type AllocateIdentity = ReturnType<typeof createIdentityAllocator>;
export function importDraft(value: AgentTeamConfigurationDraft, allocate: AllocateIdentity): EditorDraft {
  return { ...value,
    nodes: value.nodes.map(node => ({ ...node, editorId: allocate("node"), statuses: node.statuses.map(status => ({ ...status, editorId: allocate("status") })) })),
    transitions: value.transitions.map(edge => ({ ...edge, editorId: allocate("transition") })),
  };
}
/** HTTP 边界只导出业务字段，编辑身份绝不写入后端配置。 */
export function exportDraft(value: EditorDraft): AgentTeamConfigurationDraft {
  return { team_id: value.team_id, name: value.name, description: value.description, max_runs: value.max_runs, scope: value.scope, start_node_id: value.start_node_id,
    nodes: value.nodes.map(node => ({ node_id: node.node_id, name: node.name, agent_id: node.agent_id, statuses: node.statuses.map(({ name, color }) => ({ name, color })) })),
    transitions: value.transitions.map(({ from_node_id, status, target_node_id }) => ({ from_node_id, status, target_node_id })),
  };
}
export function nextNodeId(nodes: EditorNode[]): string {
  let suffix = nodes.length + 1;
  while (nodes.some(node => node.node_id === `node_${suffix}`)) suffix++;
  return `node_${suffix}`;
}

/** 唯一可解析的业务 ID 才同步引用；歧义草稿保留原文，交由后端保存校验。 */
export function updateNodeInConfiguration(configuration: EditorDraft, index: number, changes: Partial<EditorNode>): EditorDraft {
  const current = configuration.nodes[index];
  const nextId = changes.node_id ?? current.node_id;
  const rename = nextId !== current.node_id && configuration.nodes.filter(node => node.node_id === current.node_id).length === 1;
  return { ...configuration,
    nodes: configuration.nodes.map((node, i) => i === index ? { ...node, ...changes, editorId: node.editorId } : node),
    transitions: rename ? configuration.transitions.map(edge => ({ ...edge,
      from_node_id: edge.from_node_id === current.node_id ? nextId : edge.from_node_id,
      target_node_id: edge.target_node_id === current.node_id ? nextId : edge.target_node_id,
    })) : configuration.transitions,
    start_node_id: rename && configuration.start_node_id === current.node_id ? nextId : configuration.start_node_id,
  };
}

/** 所有删除入口共用一次编辑事务；布局身份不随数组压缩而改变。 */
export function removeSelection(snapshot: EditorSnapshot, selection: { nodeIds: Set<string>; endNodeIds: Set<string>; transitionIds: Set<string> }): EditorSnapshot {
  const { configuration, layout } = snapshot;
  const nodes = configuration.nodes.filter(node => !selection.nodeIds.has(node.editorId));
  const removedBusinessIds = new Set(configuration.nodes.filter(node => selection.nodeIds.has(node.editorId) && !nodes.some(remaining => remaining.node_id === node.node_id)).map(node => node.node_id));
  const transitions = configuration.transitions.filter(edge => !selection.transitionIds.has(edge.editorId) && !removedBusinessIds.has(edge.from_node_id) && !removedBusinessIds.has(edge.target_node_id));
  const remainingEnds = layout.endNodeIds.filter(id => !selection.endNodeIds.has(id));
  const endNodeIds = remainingEnds.length ? remainingEnds : [layout.endNodeIds[0]];
  if (nodes.length === configuration.nodes.length && transitions.length === configuration.transitions.length && endNodeIds.length === layout.endNodeIds.length) return snapshot;
  return cleanSnapshot({ configuration: { ...configuration, nodes, transitions,
    start_node_id: nodes.some(node => node.node_id === configuration.start_node_id) ? configuration.start_node_id : nodes[0]?.node_id ?? "",
  }, layout: { ...layout, endNodeIds } });
}

export function cleanSnapshot(snapshot: EditorSnapshot): EditorSnapshot {
  const { configuration, layout } = snapshot;
  const nodeIds = new Set([...configuration.nodes.map(node => node.editorId), ...layout.endNodeIds]);
  const edgeIds = new Set(configuration.transitions.map(edge => edge.editorId));
  const ends = new Set(configuration.transitions.filter(edge => edge.target_node_id === END_NODE_ID).map(edge => edge.editorId));
  return { configuration, layout: { ...layout,
    positions: Object.fromEntries(Object.entries(layout.positions).filter(([id]) => nodeIds.has(id))),
    edgeRoutes: Object.fromEntries(Object.entries(layout.edgeRoutes).filter(([id]) => edgeIds.has(id))),
    endTargets: Object.fromEntries(Object.entries(layout.endTargets).filter(([id]) => ends.has(id)).map(([id, end]) => [id, layout.endNodeIds.includes(end) ? end : layout.endNodeIds[0]])),
  } };
}

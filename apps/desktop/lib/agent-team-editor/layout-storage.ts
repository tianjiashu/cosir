import { frontendLog } from "@/lib/logging/frontend-log";
import { emptyLayout, type EditorDraft, type EditorLayout, type Position, type EdgeRoute } from "./model";

const edgeKey = (edge: EditorDraft["transitions"][number]) => JSON.stringify([edge.from_node_id, edge.status, edge.target_node_id]);
const storageKey = (workspaceId: number | null, draft: EditorDraft) => `cosir:agent-team-layout-v2:${draft.scope}:${draft.scope === "system" ? "system" : workspaceId}:${draft.team_id}`;
function record(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) throw new Error("画布布局必须是对象");
  return value as Record<string, unknown>;
}
function position(value: unknown): Position {
  const item = record(value);
  if (typeof item.x !== "number" || !Number.isFinite(item.x) || typeof item.y !== "number" || !Number.isFinite(item.y)) throw new Error("画布坐标无效");
  return { x: item.x, y: item.y };
}
function route(value: unknown): EdgeRoute {
  const item = record(value);
  if ((item.axis !== "x" && item.axis !== "y") || typeof item.offset !== "number" || !Number.isFinite(item.offset)) throw new Error("连线路径无效");
  return { axis: item.axis, offset: item.offset };
}

/** 只保存已保存实体的展示偏好；新草稿留在会话内，不共用名为 new 的存储槽。 */
export function writeLayout(workspaceId: number | null, baseline: EditorDraft, layout: EditorLayout): void {
  try {
    localStorage.setItem(storageKey(workspaceId, baseline), JSON.stringify({
      nodes: Object.fromEntries(baseline.nodes.filter(node => layout.positions[node.editorId]).map(node => [node.node_id, layout.positions[node.editorId]])),
      ends: Object.fromEntries(layout.endNodeIds.map(id => [id, layout.positions[id] ?? { x: 420, y: 180 }])),
      edges: Object.fromEntries(baseline.transitions.map(edge => [edgeKey(edge), { end: layout.endTargets[edge.editorId], route: layout.edgeRoutes[edge.editorId] }])),
    }));
  } catch (error) {
    void frontendLog("WARNING", "agent_team_graph_layout_persist_failed", "保存画布布局偏好失败", { data: { teamId: baseline.team_id }, error });
  }
}

/** localStorage 是不可信边界；损坏的偏好整份舍弃，不影响配置或编辑。 */
export function readLayout(workspaceId: number | null, configuration: EditorDraft): EditorLayout {
  try {
    const serialized = localStorage.getItem(storageKey(workspaceId, configuration));
    if (!serialized) return emptyLayout();
    const stored = record(JSON.parse(serialized));
    const nodePositions = record(stored.nodes);
    const ends = record(stored.ends);
    const edges = record(stored.edges);
    const layout = emptyLayout();
    const endIds = Object.keys(ends);
    if (!endIds.length || endIds.some(id => !id.startsWith("@@canvas-end-"))) throw new Error("结束出口无效");
    layout.endNodeIds = endIds;
    for (const id of endIds) layout.positions[id] = position(ends[id]);
    for (const node of configuration.nodes) if (nodePositions[node.node_id] !== undefined) layout.positions[node.editorId] = position(nodePositions[node.node_id]);
    for (const edge of configuration.transitions) {
      const storedEdge = edges[edgeKey(edge)];
      if (storedEdge === undefined) continue;
      const item = record(storedEdge);
      if (item.end !== undefined) {
        if (typeof item.end !== "string" || !endIds.includes(item.end)) throw new Error("结束出口映射无效");
        layout.endTargets[edge.editorId] = item.end;
      }
      if (item.route !== undefined) layout.edgeRoutes[edge.editorId] = route(item.route);
    }
    return layout;
  } catch (error) {
    void frontendLog("WARNING", "agent_team_graph_layout_load_failed", "画布布局偏好无效，使用默认布局", { data: { teamId: configuration.team_id }, error });
    return emptyLayout();
  }
}

/**
 * 未保存的业务删除不能擦除已保存实体的布局；放弃草稿后仍能恢复原位置和路径。
 * 仍存在的实体使用当前偏好（包括显式重置路径），视觉 END 的增删立即生效。
 */
export function savedLayoutPreferences(baseline: EditorDraft, current: { configuration: EditorDraft; layout: EditorLayout }, previous: EditorLayout): EditorLayout {
  const nodeIds = new Set(current.configuration.nodes.map(node => node.editorId));
  const edges = new Map(current.configuration.transitions.map(edge => [edge.editorId, edge]));
  const positions = { ...current.layout.positions };
  const edgeRoutes = { ...current.layout.edgeRoutes };
  const endTargets = { ...current.layout.endTargets };
  for (const node of baseline.nodes) {
    if (!nodeIds.has(node.editorId) && previous.positions[node.editorId]) positions[node.editorId] = previous.positions[node.editorId];
  }
  for (const edge of baseline.transitions) {
    const liveEdge = edges.get(edge.editorId);
    if (!liveEdge && previous.edgeRoutes[edge.editorId]) edgeRoutes[edge.editorId] = previous.edgeRoutes[edge.editorId];
    if ((!liveEdge || liveEdge.target_node_id !== "END") && previous.endTargets[edge.editorId]) endTargets[edge.editorId] = previous.endTargets[edge.editorId];
  }
  for (const [id, end] of Object.entries(endTargets)) {
    if (!current.layout.endNodeIds.includes(end)) endTargets[id] = current.layout.endNodeIds[0];
  }
  return { ...current.layout, positions, edgeRoutes, endTargets };
}

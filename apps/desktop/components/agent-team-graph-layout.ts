import type { XYPosition } from "@xyflow/react";
import type { ElkExtendedEdge, ElkNode, ElkPort } from "elkjs/lib/elk-api";

import type { TeamGraphEdge, TeamGraphNode } from "@/components/agent-team-graph-model";

/** 节点尚未被测量（首帧、隐藏中）时的估计尺寸。 */
const FALLBACK_SIZE = {
  agent: { width: 256, height: 160 },
  terminal: { width: 128, height: 48 },
  "unresolved-transition-endpoint": { width: 224, height: 40 },
} as const;

const portId = (nodeId: string, handleId: string) => `${nodeId}::${handleId}`;

/**
 * 把画布当前的节点与连线转换成 ELK 分层布局图。
 *
 * 输入直接取画布已解析好的节点（含测量尺寸）与 `createGraphEdges` 的连线，复用其对
 * END 落点、重复 ID、未解析端点的处理，不重复解析业务配置。约束：
 * - 入口节点独占第一层、所有 END 独占最后一层（`*_SEPARATE`：仅「在首 / 末层」时
 *   其他节点仍可能与之同层），回路因此不会把入口挤到中间；
 * - 每个状态出口登记为节点东侧端口并固定顺序（与节点上自上而下的状态顺序一致），
 *   ELK 据此安排相邻层的上下次序，减少出口附近的交叉；
 * - 节点按「从入口出发的执行顺序」排列并以 `MODEL_ORDER` 拆环：指回更早节点的转移
 *   （如 `needs_fix` 回到实现）才被当作回头边。ELK 默认的贪心拆环只求反转的边最少，
 *   会把正向的「实现 → 测试」当回头边反转，导致执行顺序被排乱。
 * 只取 ELK 算出的节点坐标；连线路径仍由 `AgentTeamRoutedEdge` 按端口位置绘制。
 */
export function buildTeamElkGraph(nodes: TeamGraphNode[], edges: TeamGraphEdge[]): ElkNode {
  const sourcePorts = new Map<string, ElkPort[]>();
  const elkEdges: ElkExtendedEdge[] = edges.map((edge) => {
    const ports = sourcePorts.get(edge.source) ?? [];
    const handle = edge.sourceHandle ?? "";
    if (handle && !ports.some((port) => port.id === portId(edge.source, handle))) {
      ports.push({ id: portId(edge.source, handle), layoutOptions: { "elk.port.side": "EAST" } });
      sourcePorts.set(edge.source, ports);
    }
    return { id: edge.id, sources: [handle ? portId(edge.source, handle) : edge.source], targets: [edge.target] };
  });

  const ordered = executionOrder(nodes, edges);
  return {
    id: "root",
    layoutOptions: {
      "elk.algorithm": "layered",
      "elk.direction": "RIGHT",
      "elk.spacing.nodeNode": "56",
      "elk.layered.spacing.nodeNodeBetweenLayers": "120",
      // 同层节点优先保持画布里的相对次序，重复整理时结果稳定。
      "elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES",
      "elk.layered.cycleBreaking.strategy": "MODEL_ORDER",
    },
    children: ordered.map((node) => {
      const fallback = FALLBACK_SIZE[node.data.kind];
      const layoutOptions: Record<string, string> = {};
      if (node.data.kind === "agent" && node.data.isStart) layoutOptions["elk.layered.layering.layerConstraint"] = "FIRST_SEPARATE";
      if (node.data.kind === "terminal") layoutOptions["elk.layered.layering.layerConstraint"] = "LAST_SEPARATE";
      const ports = node.data.kind === "agent"
        ? orderedAgentPorts(node, sourcePorts.get(node.id) ?? [])
        : sourcePorts.get(node.id) ?? [];
      if (ports.length) layoutOptions["elk.portConstraints"] = "FIXED_ORDER";
      return {
        id: node.id,
        width: node.measured?.width ?? fallback.width,
        height: node.measured?.height ?? fallback.height,
        ports,
        layoutOptions,
      };
    }),
    edges: elkEdges,
  };
}

/**
 * 入口先行、沿连线广度优先的节点顺序；入口走不到的节点保持原顺序随后，END 一律在最后。
 */
function executionOrder(nodes: TeamGraphNode[], edges: TeamGraphEdge[]): TeamGraphNode[] {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const start = nodes.find((node) => node.data.kind === "agent" && node.data.isStart);
  const visited = new Set<string>();
  const order: TeamGraphNode[] = [];
  const queue = start ? [start] : [];
  if (start) visited.add(start.id);
  while (queue.length) {
    const current = queue.shift()!;
    order.push(current);
    for (const edge of edges) {
      const target = byId.get(edge.target);
      if (edge.source !== current.id || !target || visited.has(target.id)) continue;
      visited.add(target.id);
      queue.push(target);
    }
  }
  const rest = nodes.filter((node) => !visited.has(node.id));
  return [...order, ...rest].sort((a, b) => Number(a.data.kind === "terminal") - Number(b.data.kind === "terminal"));
}

/** 端口按节点上状态出口的显示顺序编号；ELK 东侧端口按编号自上而下排列。 */
function orderedAgentPorts(node: TeamGraphNode, ports: ElkPort[]): ElkPort[] {
  if (node.data.kind !== "agent") return ports;
  const order = node.data.node.statuses.map((status) => portId(node.id, status.editorId));
  const rank = (port: ElkPort) => {
    const index = order.indexOf(port.id);
    return index < 0 ? order.length : index;
  };
  return [...ports].sort((a, b) => rank(a) - rank(b))
    .map((port, index) => ({ ...port, layoutOptions: { ...port.layoutOptions, "elk.port.index": String(index) } }));
}

/**
 * 计算整理后的节点位置（画布坐标）。整体平移到原先包围盒的左上角，避免视口大幅跳动。
 *
 * ELK 体积较大，只在用户点「整理布局」时按需加载；使用自带 Worker 兜底的 bundled 版本，
 * 不额外配置 Web Worker。加载或计算失败时抛出，由调用方记录并提示，画布保持原样。
 */
export async function computeTeamAutoLayout(nodes: TeamGraphNode[], edges: TeamGraphEdge[]): Promise<Record<string, XYPosition>> {
  if (nodes.length === 0) return {};
  const { default: ELK } = await import("elkjs/lib/elk.bundled.js");
  const children = (await new ELK().layout(buildTeamElkGraph(nodes, edges))).children ?? [];
  // ELK 坐标带图级内边距，按结果自身的左上角对齐到原包围盒左上角。
  const shiftX = Math.min(...nodes.map((node) => node.position.x)) - Math.min(...children.map((child) => child.x ?? 0));
  const shiftY = Math.min(...nodes.map((node) => node.position.y)) - Math.min(...children.map((child) => child.y ?? 0));
  return Object.fromEntries(children.map((child) => [
    child.id,
    { x: Math.round((child.x ?? 0) + shiftX), y: Math.round((child.y ?? 0) + shiftY) },
  ]));
}

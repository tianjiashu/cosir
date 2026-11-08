import type { EditorDraft } from "./model";
import type { NodeIdIndex } from "./node-id-index";

type TeamOutline = {
  /** 从入口沿转移可达的节点下标，按广度优先的执行顺序排列。 */
  connected: number[];
  /** 入口出发走不到的节点下标，保持原有顺序。 */
  unconnected: number[];
  /** 来源节点不存在或 ID 重复、无法挂到某张节点卡片下的转移下标。 */
  dangling: number[];
};

/**
 * 把图结构排成大纲：只按转移关系排序与分组，帮助阅读执行流程。
 * 不复刻后端图校验（可达性、终止性等仍以保存时的后端结果为准）。
 */
export function buildTeamOutline(configuration: EditorDraft, nodeIds: NodeIdIndex): TeamOutline {
  const connected: number[] = [];
  const visited = new Set<number>();
  const start = nodeIds.get(configuration.start_node_id);
  const queue = start == null ? [] : [start];
  if (start != null) visited.add(start);
  while (queue.length) {
    const index = queue.shift()!;
    connected.push(index);
    for (const transition of configuration.transitions) {
      if (transition.from_node_id !== configuration.nodes[index].node_id) continue;
      const target = nodeIds.get(transition.target_node_id);
      if (target == null || visited.has(target)) continue;
      visited.add(target);
      queue.push(target);
    }
  }
  return {
    connected,
    unconnected: configuration.nodes.map((_, index) => index).filter((index) => !visited.has(index)),
    dangling: configuration.transitions.map((_, index) => index)
      .filter((index) => nodeIds.get(configuration.transitions[index].from_node_id) == null),
  };
}


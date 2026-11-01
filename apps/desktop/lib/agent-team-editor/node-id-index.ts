/** 当前草稿的业务 ID 解析结果：数字是唯一节点下标，null 是重复 ID，缺键是不存在。不跨草稿缓存。 */
export type NodeIdIndex = ReadonlyMap<string, number | null>;

export function indexNodeIds(nodes: readonly { node_id: string }[]): NodeIdIndex {
  const index = new Map<string, number | null>();
  nodes.forEach((node, position) => index.set(node.node_id, index.has(node.node_id) ? null : position));
  return index;
}

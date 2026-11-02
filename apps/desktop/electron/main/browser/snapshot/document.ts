/** 读快照得到的原始数据：每份文档（主框架或一个子框架）的无障碍树与布局结论，按 iframe 挂成一棵树。 */

/** `Accessibility.getFullAXTree` 返回的节点里本目录用到的字段。 */
export type AXValue = { value?: unknown };
export type AXNode = {
  nodeId: string;
  ignored?: boolean;
  role?: AXValue;
  name?: AXValue;
  value?: AXValue;
  properties?: { name: string; value: AXValue }[];
  childIds?: string[];
  parentId?: string;
  backendDOMNodeId?: number;
};

/** 一份文档读到的内容；`frames` 按 iframe 元素的节点 id 挂上读到的子框架文档。 */
export type SnapshotDocument = {
  /** 所在 CDP 会话（跨进程子框架的子会话；否则为 undefined）：节点 id 只在会话内唯一。 */
  sessionId?: string;
  nodes: readonly AXNode[];
  /** 最外层 pointer 元素（`pointerRoots`，按所在会话算）。 */
  pointer: ReadonlySet<number>;
  /** 与视口里看得见的区域相交的节点（`layout.ts`）：「只看视口」的视图据此筛选。 */
  inView: ReadonlySet<number>;
  frames: ReadonlyMap<number, SnapshotDocument>;
};

const FRAME_ROLES = new Set(["Iframe", "IframePresentational"]);

/** 装着子框架的节点（隐藏的不算：它们不输出，也不值得去读）。 */
export function isFrameNode(node: AXNode): boolean {
  return !node.ignored && FRAME_ROLES.has(stringValue(node.role));
}

export function isFrameRole(role: string): boolean {
  return FRAME_ROLES.has(role);
}

export function stringValue(value: AXValue | undefined): string {
  const raw = value?.value;
  return typeof raw === "string" ? raw : typeof raw === "number" ? String(raw) : "";
}

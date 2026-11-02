/**
 * `DOMSnapshot.captureSnapshot` 的结果 → 快照要的布局结论：哪些元素「看起来可点」、哪些节点在视口里。纯函数。
 *
 * 几何的单位（实测）：元素框与滚动偏移都是**设备像素**、所属文档的坐标——Retina 屏上 CSS 的 100 报成 200，与面板
 * 缩放显示、手机模拟无关；同进程子框架的文档坐标相对它自己的文档，不是主视口。所以这里统一在「设备像素 + 文档
 * 坐标」里算，只在入口把视口换成同一单位（`rootRegion`），不逐个节点来回换算。快照时成批的几何只走这里；动作与
 * 元素截图要的是此刻的位置，另由 `input/element.ts` 的 `box` 现量。
 */

/** `DOMSnapshot.captureSnapshot`（`computedStyles: ["cursor"]`）结果里本模块用到的字段。 */
export type DomSnapshot = {
  strings: string[];
  documents: {
    frameId: number;
    scrollOffsetX?: number;
    scrollOffsetY?: number;
    nodes: { parentIndex?: number[]; backendNodeId?: number[] };
    layout: { nodeIndex: number[]; styles: number[][]; bounds: number[][] };
  }[];
};

export type Rect = { x: number; y: number; width: number; height: number };

/** 一份文档的布局：滚动位置与每个有布局的节点的框（设备像素、文档坐标）。 */
export type DocumentLayout = { scroll: { x: number; y: number }; boxes: Map<number, Rect> };

/** `Page.getLayoutMetrics` 里本模块用到的字段。 */
export type LayoutMetrics = {
  layoutViewport: { clientWidth: number };
  cssLayoutViewport: { clientWidth: number };
  cssVisualViewport: { pageX: number; pageY: number; clientWidth: number; clientHeight: number };
};

/** 主框架看得见的区域（设备像素、文档坐标）：可视视口（手机模拟缩放后看到的那块）乘上设备像素比。 */
export function rootRegion(metrics: LayoutMetrics): Rect {
  const ratio = metrics.layoutViewport.clientWidth / metrics.cssLayoutViewport.clientWidth || 1;
  const view = metrics.cssVisualViewport;
  return { x: view.pageX * ratio, y: view.pageY * ratio, width: view.clientWidth * ratio, height: view.clientHeight * ratio };
}

/** 某个框架的文档布局：省略 `frameId` 即会话自己的主文档（快照的第一份文档）；快照里没有（还没加载）时为 undefined。 */
export function documentLayout(snapshot: DomSnapshot, frameId?: string): DocumentLayout | undefined {
  const document = frameId === undefined ? snapshot.documents[0] : snapshot.documents.find((item) => snapshot.strings[item.frameId] === frameId);
  if (!document) return undefined;
  const ids = document.nodes.backendNodeId ?? [];
  const boxes = new Map<number, Rect>();
  document.layout.nodeIndex.forEach((nodeIndex, i) => {
    const [x, y, width, height] = document.layout.bounds[i];
    boxes.set(ids[nodeIndex], { x, y, width, height });
  });
  return { scroll: { x: document.scrollOffsetX ?? 0, y: document.scrollOffsetY ?? 0 }, boxes };
}

/** 框与看得见的区域相交的节点。 */
export function visibleNodes(layout: DocumentLayout, region: Rect | null): Set<number> {
  const visible = new Set<number>();
  if (!region) return visible;
  for (const [id, box] of layout.boxes) if (intersection(box, region)) visible.add(id);
  return visible;
}

/**
 * 子框架看得见的区域（子框架文档坐标）：它的 iframe 元素（`owner`，在上一层）与上一层可见区域的交集，平移到子文档、
 * 再加上子文档自己的滚动。iframe 元素整个在视口外、或上一层本来就看不见时为 null。
 */
export function frameRegion(parent: DocumentLayout, parentRegion: Rect | null, owner: number, child: DocumentLayout): Rect | null {
  const box = parent.boxes.get(owner);
  const visible = box && parentRegion && intersection(box, parentRegion);
  if (!box || !visible) return null;
  return { x: visible.x - box.x + child.scroll.x, y: visible.y - box.y + child.scroll.y, width: visible.width, height: visible.height };
}

function intersection(a: Rect, b: Rect): Rect | null {
  const x = Math.max(a.x, b.x);
  const y = Math.max(a.y, b.y);
  const right = Math.min(a.x + a.width, b.x + b.width);
  const bottom = Math.min(a.y + a.height, b.y + b.height);
  return right > x && bottom > y ? { x, y, width: right - x, height: bottom - y } : null;
}

/**
 * 最外层的 `cursor: pointer` 元素（backendNodeId）：自己是 pointer、最近的有样式祖先不是。没有布局的节点
 * （`display: contents` 等）没有样式，往上找到有样式的祖先再比较。
 */
export function pointerRoots(snapshot: DomSnapshot): Set<number> {
  const roots = new Set<number>();
  for (const { nodes, layout } of snapshot.documents) {
    const parents = nodes.parentIndex ?? [];
    const ids = nodes.backendNodeId ?? [];
    const cursor = new Map<number, string>();
    layout.nodeIndex.forEach((nodeIndex, i) => cursor.set(nodeIndex, snapshot.strings[layout.styles[i]?.[0]] ?? ""));
    const styledAncestor = (index: number): number => {
      let at = parents[index] ?? -1;
      while (at >= 0 && !cursor.has(at)) at = parents[at] ?? -1;
      return at;
    };
    for (const [index, value] of cursor) {
      if (value !== "pointer") continue;
      const ancestor = styledAncestor(index);
      if (ancestor < 0 || cursor.get(ancestor) !== "pointer") roots.add(ids[index]);
    }
  }
  return roots;
}

import type { CdpSession } from "../cdp-session";
import type { FrameDocument, PageFrames } from "../page-frames";
import { isFrameNode, type AXNode, type SnapshotDocument } from "./document";
import { documentLayout, frameRegion, pointerRoots, rootRegion, visibleNodes, type DocumentLayout, type DomSnapshot, type LayoutMetrics, type Rect } from "./layout";

/** 一次快照最多展开的子框架数与嵌套层数：广告、统计的 iframe 可能有很多，读树一个个来，不设上限会拖慢快照。 */
const MAX_FRAMES = 10;
const MAX_DEPTH = 3;

/**
 * 读快照要的内容：主框架的无障碍树与布局（光标样式、元素框），再沿 iframe 逐层读子框架（`PageFrames` 给出每份文档
 * 在哪个会话）。无障碍树不带样式与位置：「看起来可点」的无语义元素靠计算样式里的 cursor 认，哪些节点在视口里靠
 * 元素框认（`layout.ts`）；一次 `DOMSnapshot` 就覆盖它会话里的全部文档（大页面上约为读树耗时的两成，实测），所以
 * 按会话读一次。子框架看得见的区域由它的 iframe 元素在上一层的可见部分推出，一层层往下传。
 *
 * 子框架读失败（读到一半被移除、导航换了进程、还没附加上）不让整个快照失败：它在快照里标为没展开，与超出上限的
 * 一样，模型照样看得到它在哪；主框架读失败照常抛出。
 */
export async function readSnapshot(session: CdpSession, frames: PageFrames): Promise<SnapshotDocument> {
  const layouts = new Map<string | undefined, Promise<{ snapshot: DomSnapshot; pointer: Set<number> }>>();
  const layoutOf = (sessionId: string | undefined) => {
    let layout = layouts.get(sessionId);
    if (!layout) {
      layout = session.send<DomSnapshot>("DOMSnapshot.captureSnapshot", { computedStyles: ["cursor"] }, { sessionId })
        .then((snapshot) => ({ snapshot, pointer: pointerRoots(snapshot) }));
      layouts.set(sessionId, layout);
    }
    return layout;
  };
  let remaining = MAX_FRAMES;

  /** `outer`：上一层的布局、可见区域与装着这份文档的 iframe 元素；主框架没有。 */
  type Outer = { layout: DocumentLayout | undefined; region: Rect | null; owner: number };
  const read = async (at: FrameDocument, depth: number, outer: Outer | Rect): Promise<SnapshotDocument> => {
    const { nodes } = await session.send<{ nodes: AXNode[] }>("Accessibility.getFullAXTree", at.frameId ? { frameId: at.frameId } : {}, { sessionId: at.sessionId });
    const { snapshot, pointer } = await layoutOf(at.sessionId);
    const layout = documentLayout(snapshot, at.frameId);
    const region = "owner" in outer
      ? outer.layout && layout ? frameRegion(outer.layout, outer.region, outer.owner, layout) : null
      : outer;
    const children = new Map<number, SnapshotDocument>();
    for (const node of nodes) {
      if (depth >= MAX_DEPTH || remaining === 0) break;
      if (!isFrameNode(node) || node.backendDOMNodeId === undefined) continue;
      remaining -= 1;
      const owner = node.backendDOMNodeId;
      const content = await frames.contentOf(at.sessionId, owner).then((found) => found && read(found, depth + 1, { layout, region, owner })).catch(() => null);
      if (content) children.set(owner, content);
    }
    return { sessionId: at.sessionId, nodes, pointer, inView: layout ? visibleNodes(layout, region) : new Set(), frames: children };
  };

  const metrics = await session.send<LayoutMetrics>("Page.getLayoutMetrics");
  return read({}, 0, rootRegion(metrics));
}

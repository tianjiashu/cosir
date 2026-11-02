/** 页面上的一个节点：所在 CDP 会话（跨进程子框架的子会话；主框架与同进程子框架为 undefined）与其中的节点 id。 */
export type PageNode = { sessionId?: string; backendNodeId: number };

/**
 * 一个标签页的 ref 登记：`e12` ↔ 节点（`PageNode`），由 `BrowserTab` 持有、快照格式化时分配。
 *
 * ref 按节点稳定：同一文档里同一个元素每次快照都是同一个 ref，新出现的元素拿新序号。这样模型拿着任何一次
 * 快照（含分页读到的后半段）里的 ref 都能用，只要元素还在页面上；不会出现「旧序号悄悄指到新快照里的另一个
 * 元素」。节点按「会话 + 节点 id」登记：跨进程子框架的节点 id 与主会话的重号（实测），序号仍是全页一套。
 *
 * 导航后 `clear` 清掉映射但序号不回头：跨进程导航后新文档的节点 id 会重新从小编号，若序号也从 `e1` 重来，
 * 导航前的 ref 可能恰好指到新页面的另一个元素；序号单调递增，旧 ref 一律查不到（`REF_STALE`）。子框架自己
 * 导航换进程时换的是会话，旧会话的 ref 查得到但命令失败，同样按 `REF_STALE` 报。
 */
export class RefRegistry {
  private readonly nodeByRef = new Map<string, PageNode>();
  private readonly refByNode = new Map<string, string>();
  private issued = 0;

  /** 给节点取 ref：已登记的沿用，否则发一个新序号。 */
  assign(node: PageNode): string {
    const key = `${node.sessionId ?? ""}:${node.backendNodeId}`;
    const known = this.refByNode.get(key);
    if (known) return known;
    this.issued += 1;
    const ref = `e${this.issued}`;
    this.refByNode.set(key, ref);
    this.nodeByRef.set(ref, node);
    return ref;
  }

  /** 没登记过（含导航前的 ref）返回 `undefined`。节点是否仍在页面上由调用方向页面确认。 */
  resolve(ref: string): PageNode | undefined {
    return this.nodeByRef.get(ref);
  }

  clear(): void {
    this.nodeByRef.clear();
    this.refByNode.clear();
  }
}

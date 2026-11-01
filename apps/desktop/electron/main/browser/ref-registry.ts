/**
 * 一个标签页的 ref 登记：`e12` ↔ CDP `backendDOMNodeId`，由 `BrowserTab` 持有、快照格式化时分配。
 *
 * ref 按节点稳定：同一文档里同一个元素每次快照都是同一个 ref，新出现的元素拿新序号。这样模型拿着任何一次
 * 快照（含分页读到的后半段）里的 ref 都能用，只要元素还在页面上；不会出现「旧序号悄悄指到新快照里的另一个
 * 元素」。
 *
 * 导航后 `clear` 清掉映射但序号不回头：跨进程导航后新文档的节点 id 会重新从小编号，若序号也从 `e1` 重来，
 * 导航前的 ref 可能恰好指到新页面的另一个元素；序号单调递增，旧 ref 一律查不到（`REF_STALE`）。
 */
export class RefRegistry {
  private readonly nodeByRef = new Map<string, number>();
  private readonly refByNode = new Map<number, string>();
  private issued = 0;

  /** 给节点取 ref：已登记的沿用，否则发一个新序号。 */
  assign(backendNodeId: number): string {
    const known = this.refByNode.get(backendNodeId);
    if (known) return known;
    this.issued += 1;
    const ref = `e${this.issued}`;
    this.refByNode.set(backendNodeId, ref);
    this.nodeByRef.set(ref, backendNodeId);
    return ref;
  }

  /** 没登记过（含导航前的 ref）返回 `undefined`。节点是否仍在页面上由调用方向页面确认。 */
  resolve(ref: string): number | undefined {
    return this.nodeByRef.get(ref);
  }

  clear(): void {
    this.nodeByRef.clear();
    this.refByNode.clear();
  }
}

import type { ThreadMessage } from "@assistant-ui/react";
import { dequal } from "dequal";

/**
 * 对话虚拟列表按消息 id 记下的实测行高，跨虚拟列表挂载保留（切到别的 Session 再回来、SessionPage 重建）。
 *
 * TanStack 的测量只活在一个 virtualizer 实例里，实例随 Thread 卸载即丢，回到长对话时每条行都要
 * 从估计值重新测一遍，向上滚动时逐行修正 scrollTop。这里只把实测值作为下一次的估计值交回
 * （`estimateSize`），不灌进 virtualizer 的测量缓存：首次测量与估计相同时 TanStack 不写 scrollTop，
 * 而 `initialMeasurementsCache` 按 index 记录起止位置，消息增删后会错位。
 *
 * 行高随列宽变化，所以每个 Session 只保留一种列宽下的测量，列宽变了整桶作废。只是渲染提示，不是
 * 对话事实；进程内存储，不持久化，Session 删除时随其他按 sessionId 分桶的 UI 状态一起丢弃。
 */
type SessionHeights = { width: number; sizes: Map<string, { size: number; message?: ThreadMessage }> };

const heightsBySession = new Map<number, SessionHeights>();

export function rememberMessageHeight(sessionId: number, width: number, messageId: string, size: number, message?: ThreadMessage): void {
  let heights = heightsBySession.get(sessionId);
  if (!heights || heights.width !== width) {
    heights = { width, sizes: new Map() };
    heightsBySession.set(sessionId, heights);
  }
  heights.sizes.set(messageId, { size, message });
}

/**
 * 这条消息在当前列宽下的实测行高；没有测过或列宽已变时为 undefined。
 *
 * `width` 为 null 表示列表还没布局（挂载后的首次估计）：此时信任桶里记下的列宽，回到对话的第一帧
 * 就能用上实测值；若列宽其实已变，挂载后的测量照常纠正。
 */
export function rememberedMessageHeight(sessionId: number, width: number | null, messageId: string, message?: ThreadMessage): number | undefined {
  const heights = heightsBySession.get(sessionId);
  const entry = heights && (width === null || heights.width === width) ? heights.sizes.get(messageId) : undefined;
  if (!entry) return undefined;
  if (entry.message !== message && !sameRenderedContent(entry.message, message)) return undefined;
  return entry.size;
}

export function forgetMessageHeights(sessionIds: readonly number[]): void {
  for (const sessionId of sessionIds) heightsBySession.delete(sessionId);
}

/** Invalidate only rows whose disclosure changed; late content invalidates through message identity at lookup. */
export function forgetDisclosureHeight(sessionId: number, key: string): void {
  const bucket = heightsBySession.get(sessionId);
  if (!bucket) return;
  for (const [id, entry] of bucket.sizes) {
    const callId = key.startsWith("call:") ? key.slice(5) : null;
    if (key === `process:${id}` || key === `agent:${id}` || key === `user:${id}` || key.startsWith(`activity:${id}:`)
      || key.startsWith(`file:${id}:`)
      || (callId && entry.message?.content.some((part) => part.type === "tool-call" && part.toolCallId === callId))) bucket.sizes.delete(id);
  }
}

/**
 * 回到 Session 时重建出的消息是新对象，内容没变就可沿用实测值；内容变了一律不沿用。逐字段深比较，不序列化：
 * `turnLayout` 由正文与 `custom` 里的结局、当前活动推导，不参与比较（它反复引用同一批 part，比较它要把
 * 同样的内容走好几遍）；只差在推导结果上的偏差由挂载后的实测纠正。
 */
function sameRenderedContent(previous: ThreadMessage | undefined, next: ThreadMessage | undefined): boolean {
  if (!previous || !next) return false;
  return dequal(previous.status, next.status) && dequal(previous.content, next.content)
    && dequal(withoutLayout(previous), withoutLayout(next));
}

function withoutLayout(message: ThreadMessage): Record<string, unknown> {
  const rest = { ...(message.metadata?.custom ?? {}) } as Record<string, unknown>;
  delete rest.turnLayout;
  return rest;
}

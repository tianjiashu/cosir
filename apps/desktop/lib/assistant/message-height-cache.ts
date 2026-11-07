import type { ThreadMessage } from "@assistant-ui/react";
/**
 * 对话虚拟列表按消息 id 记下的实测行高，跨虚拟列表挂载保留（切到别的 Task 再回来、TaskPage 重建）。
 *
 * TanStack 的测量只活在一个 virtualizer 实例里，实例随 Thread 卸载即丢，回到长对话时每条行都要
 * 从估计值重新测一遍，向上滚动时逐行修正 scrollTop。这里只把实测值作为下一次的估计值交回
 * （`estimateSize`），不灌进 virtualizer 的测量缓存：首次测量与估计相同时 TanStack 不写 scrollTop，
 * 而 `initialMeasurementsCache` 按 index 记录起止位置，消息增删后会错位。
 *
 * 行高随列宽变化，所以每个 Task 只保留一种列宽下的测量，列宽变了整桶作废。只是渲染提示，不是
 * 对话事实；进程内存储，不持久化，Task 删除时随其他按 taskId 分桶的 UI 状态一起丢弃。
 */
type TaskHeights = { width: number; sizes: Map<string, { size: number; message?: ThreadMessage }> };

const heightsByTask = new Map<number, TaskHeights>();

export function rememberMessageHeight(taskId: number, width: number, messageId: string, size: number, message?: ThreadMessage): void {
  let heights = heightsByTask.get(taskId);
  if (!heights || heights.width !== width) {
    heights = { width, sizes: new Map() };
    heightsByTask.set(taskId, heights);
  }
  heights.sizes.set(messageId, { size, message });
}

/**
 * 这条消息在当前列宽下的实测行高；没有测过或列宽已变时为 undefined。
 *
 * `width` 为 null 表示列表还没布局（挂载后的首次估计）：此时信任桶里记下的列宽，回到对话的第一帧
 * 就能用上实测值；若列宽其实已变，挂载后的测量照常纠正。
 */
export function rememberedMessageHeight(taskId: number, width: number | null, messageId: string, message?: ThreadMessage): number | undefined {
  const heights = heightsByTask.get(taskId);
  const entry = heights && (width === null || heights.width === width) ? heights.sizes.get(messageId) : undefined;
  if (entry?.message !== message && contentKey(entry?.message) !== contentKey(message)) return undefined;
  return entry?.size;
}

export function forgetMessageHeights(taskIds: readonly number[]): void {
  for (const taskId of taskIds) heightsByTask.delete(taskId);
}

/** Invalidate only rows whose disclosure changed; late content invalidates through message identity at lookup. */
export function forgetDisclosureHeight(taskId: number, key: string): void {
  const bucket = heightsByTask.get(taskId);
  if (!bucket) return;
  for (const [id, entry] of bucket.sizes) {
    const callId = key.startsWith("call:") ? key.slice(5) : null;
    if (key === `process:${id}` || key === `agent:${id}` || key.startsWith(`activity:${id}:`)
      || key.startsWith(`file:${id}:`)
      || (callId && entry.message?.content.some((part) => part.type === "tool-call" && part.toolCallId === callId))) bucket.sizes.delete(id);
  }
}

const contentKeys = new WeakMap<ThreadMessage, string>();
/** Equal snapshots recreated after navigation may reuse a measurement; changed content never may. */
function contentKey(message: ThreadMessage | undefined): string | undefined {
  if (!message) return undefined;
  let key = contentKeys.get(message);
  if (key === undefined) {
    key = JSON.stringify([message.content, message.status, message.metadata?.custom]);
    contentKeys.set(message, key);
  }
  return key;
}

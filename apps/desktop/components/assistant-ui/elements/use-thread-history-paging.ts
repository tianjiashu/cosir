import type { Virtualizer } from "@tanstack/react-virtual";
import { useEffect, useLayoutEffect, useRef, type RefObject } from "react";

import { useLoadEarlierHistory } from "@/lib/assistant/transport-history-context";

// 视口顶部离已加载的最早一条不到这么多行时预取上一页，用户滚到顶之前页已就绪。
const PREFETCH_ROWS = 2;
const OVERSCAN_ROWS = 8;

type ThreadVirtualizer = Virtualizer<HTMLDivElement, Element>;

/**
 * 对话虚拟列表的历史分页：滚到接近顶部时预取上一页；页插到前面后保持视口不跳。
 *
 * 不用 TanStack 的 `anchorTo: "end"`：它同时会在接近底部时追底，和对话「流式输出不追底」冲突。这里在
 * 每次滚动时记下第一条可见消息与它相对视口顶的偏移，插入后在 layout effect 里按新位置恢复；插入行
 * 先用估计 / 缓存行高，测量后的差值由 TanStack 对视口上方行的首次测量补偿。
 */
export function useThreadHistoryPaging({
  virtualizer,
  messageIds,
  scrollElementRef,
}: {
  virtualizer: ThreadVirtualizer;
  messageIds: readonly string[];
  scrollElementRef: RefObject<HTMLDivElement | null>;
}): void {
  const loadEarlier = useLoadEarlierHistory();
  const anchorRef = useRef<{ id: string; delta: number } | null>(null);
  const previousIdsRef = useRef(messageIds);

  // 预取只由滚动触发：挂载时列表先从顶部渲染、随后才落到底部，按渲染位置判断会在落底前白取一页。
  useEffect(() => {
    const element = scrollElementRef.current;
    if (!element) return;
    // 按滚动位置在全部行的布局里找，不用 `getVirtualItems()`：滚动事件里它还是上一次渲染的范围
    // （TanStack 在自己的滚动处理后才重算），一次跳到顶附近会被误判为离顶部很远。
    const itemAtTop = () => {
      const top = element.scrollTop;
      return virtualizer.measurementsCache.find((candidate) => candidate.end > top);
    };
    /** 记下顶部那一行作为插入前的锚点，并返回它（同一次同步回调里布局与 scrollTop 不变，只需查一次）。 */
    const record = () => {
      const item = itemAtTop();
      anchorRef.current = item ? { id: String(item.key), delta: item.start - element.scrollTop } : null;
      return item;
    };
    const onScroll = () => {
      const item = record();
      if (loadEarlier && item !== undefined && item.index <= PREFETCH_ROWS + OVERSCAN_ROWS) void loadEarlier();
    };
    record();
    element.addEventListener("scroll", onScroll, { passive: true });
    return () => element.removeEventListener("scroll", onScroll);
  }, [loadEarlier, scrollElementRef, virtualizer]);

  useLayoutEffect(() => {
    const previous = previousIdsRef.current;
    previousIdsRef.current = messageIds;
    const anchor = anchorRef.current;
    // 只处理往前插入：原来的第一条还在，但不再是第一条。
    if (!anchor || previous.length === 0 || messageIds.indexOf(previous[0]) <= 0) return;
    const start = virtualizer.measurementsCache[messageIds.indexOf(anchor.id)]?.start;
    if (start !== undefined) virtualizer.scrollToOffset(start - anchor.delta);
  }, [messageIds, virtualizer]);
}

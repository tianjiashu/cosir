import { useAui, type ThreadMessage } from "@assistant-ui/react";
import type { Virtualizer } from "@tanstack/react-virtual";
import { useEffect, useLayoutEffect, useRef, type RefObject } from "react";

import { useLoadEarlierHistory } from "@/lib/assistant/transport-history-context";
import { registerThreadNavigator } from "@/lib/assistant/thread-navigation";
import { readRuntimeTransportState } from "@/lib/assistant/transport-state-operations";

// 视口顶部离已加载的最早一条不到这么多行时预取上一页，用户滚到顶之前页已就绪。
const PREFETCH_ROWS = 2;
const OVERSCAN_ROWS = 8;
// 定位时最多往上翻这么多页，防止历史异常时无限翻。
const MAX_REVEAL_PAGES = 50;

type ThreadVirtualizer = Virtualizer<HTMLDivElement, Element>;

/**
 * 对话虚拟列表的历史分页：滚到接近顶部时预取上一页；页插到前面后保持视口不跳；为本 Task 登记
 * 「定位到工具卡片」的导航器（必要时一页页往上翻）。
 *
 * 不用 TanStack 的 `anchorTo: "end"`：它同时会在接近底部时追底，和对话「流式输出不追底」冲突。这里在
 * 每次滚动时记下第一条可见消息与它相对视口顶的偏移，插入后在 layout effect 里按新位置恢复；插入行
 * 先用估计 / 缓存行高，测量后的差值由 TanStack 对视口上方行的首次测量补偿。
 */
export function useThreadHistoryPaging({
  taskId,
  virtualizer,
  messageIds,
  scrollElementRef,
}: {
  taskId: number;
  virtualizer: ThreadVirtualizer;
  messageIds: readonly string[];
  scrollElementRef: RefObject<HTMLDivElement | null>;
}): void {
  const aui = useAui();
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
    const record = () => {
      const item = itemAtTop();
      anchorRef.current = item ? { id: String(item.key), delta: item.start - element.scrollTop } : null;
    };
    const onScroll = () => {
      record();
      const item = itemAtTop();
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

  const latest = useRef({ messageIds, virtualizer, loadEarlier });
  latest.current = { messageIds, virtualizer, loadEarlier };
  useEffect(() => registerThreadNavigator(taskId, {
    revealToolCall: async (toolCallId) => {
      for (let page = 0; page <= MAX_REVEAL_PAGES; page += 1) {
        const thread = aui.thread().getState();
        const messageId = thread.messages.find((message) => hasToolCall(message, toolCallId))?.id;
        const index = messageId === undefined ? -1 : latest.current.messageIds.indexOf(messageId);
        if (index >= 0) {
          latest.current.virtualizer.scrollToIndex(index, { align: "center" });
          await nextFrames(2);
          document.querySelector(`[data-tool-call-id="${CSS.escape(toolCallId)}"]`)
            ?.scrollIntoView({ block: "center", behavior: "smooth" });
          return true;
        }
        const offset = readRuntimeTransportState(thread.state)?.messageOffset ?? 0;
        if (offset === 0 || !latest.current.loadEarlier) return false;
        await latest.current.loadEarlier();
        // 页插入后 assistant-ui 在之后的渲染里才看到新消息；等它跟上，迟迟不变说明没翻成。
        if (!(await waitFrames(30, () => (readRuntimeTransportState(aui.thread().getState().state)?.messageOffset ?? 0) !== offset))) {
          return false;
        }
      }
      return false;
    },
  }), [aui, taskId]);
}

function hasToolCall(message: ThreadMessage, toolCallId: string): boolean {
  return message.content.some((part) => part.type === "tool-call" && part.toolCallId === toolCallId);
}

/** 逐帧检查条件，最多等 `count` 帧；返回条件是否成立。 */
async function waitFrames(count: number, done: () => boolean): Promise<boolean> {
  for (let frame = 0; frame < count; frame += 1) {
    if (done()) return true;
    await nextFrames(1);
  }
  return done();
}

function nextFrames(count: number): Promise<void> {
  return new Promise((resolve) => {
    const step = (remaining: number) => (remaining === 0 ? resolve() : requestAnimationFrame(() => step(remaining - 1)));
    step(count);
  });
}

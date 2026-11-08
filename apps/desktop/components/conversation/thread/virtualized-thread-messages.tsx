import { defaultRangeExtractor, useVirtualizer } from "@tanstack/react-virtual";
import {
  unstable_useThreadMessageIds,
  useAui,
  useAuiState,
  useThreadViewport,
  type ThreadMessage,
} from "@assistant-ui/react";
import { Fragment, memo, useCallback, useMemo, useRef, type FC } from "react";
import type { AssistantPerformanceProbe } from "@/lib/assistant/assistant-performance-probe";
import { rememberMessageHeight, rememberedMessageHeight } from "@/lib/assistant/message-height-cache";
import { estimateMessageHeight } from "@/lib/assistant/message-height-estimate";
import { queuedMessageIdsKey } from "@/lib/assistant/transport-state-operations";
import { VirtualizedThreadMessageRow, type MessageComponents } from "@/components/conversation/thread/virtualized-thread-message-row";
import { useThreadHistoryPaging } from "@/components/conversation/thread/use-thread-history-paging";
import { measureRenderedElement } from "@/lib/virtual/measure-rendered-element";

type VirtualizedThreadMessagesProps = {
  /** 行高缓存按 Task 分桶。 */
  taskId: number;
  components: MessageComponents;
  performanceProbe?: AssistantPerformanceProbe | null;
};

/**
 * 按消息 id 定位的对话虚拟列表；不负责消息内容状态维护。
 * 排队消息不在消息流里（由输入框上方的队列区渲染），这里滤掉。
 * 运行中始终保留末尾两条消息（本轮用户输入与正在输出的回复）挂载，避免流式增长时被移出可见区后丢失测量。
 *
 * 屏幕外消息只由这里跳过，消息根节点不能再叠加 `content-visibility`：视口外但在 overscan 内的行会被
 * 浏览器按占位高度布局，虚拟列表量到假高度，行接近视口时再按真实高度修正一次 scrollTop，卸载后
 * 重新挂载又回到占位高度，于是每经过一次修正一次。行高的初始值依次取本 Task 记下的实测值
 * （`message-height-cache`）与按内容的估计（`estimateMessageHeight`），实测值以 TanStack 测量为准。
 *
 * 列表只含已加载的一段历史；往上翻页、插入后保持视口与「定位到工具卡片」见 `useThreadHistoryPaging`。
 */
export const VirtualizedThreadMessages: FC<VirtualizedThreadMessagesProps> = memo(function VirtualizedThreadMessages({
  taskId,
  components,
  performanceProbe = null,
}) {
  const aui = useAui();
  const containerRef = useRef<HTMLDivElement>(null);
  const scrollElement = useThreadViewport((state) => state.element.viewport);
  const threadMessageIds = unstable_useThreadMessageIds();
  const queuedIdsKey = useAuiState((state) => queuedMessageIdsKey(state.thread.state));
  // The external-store runtime creates a trailing optimistic assistant while sending/queuing.
  // Only canonical backend replies own execution UI; sending feedback belongs to the user row.
  const optimisticId = useAuiState((state) => {
    const last = state.thread.messages.at(-1);
    return last?.metadata.isOptimistic ? last.id : null;
  });
  const messageIds = useMemo(() => {
    if (!queuedIdsKey && !optimisticId) return threadMessageIds;
    const queued = new Set(queuedIdsKey.split("|"));
    return threadMessageIds.filter((id) => id !== optimisticId && !queued.has(id));
  }, [queuedIdsKey, optimisticId, threadMessageIds]);
  const isRunning = useAuiState((state) => state.thread.isRunning);
  const activeIndexes = useMemo(() => {
    if (!isRunning) return [];
    const last = messageIds.length - 1;
    return [last - 1, last].filter((index) => index >= 0);
  }, [isRunning, messageIds]);
  // TanStack 把 key 函数身份作为布局缓存依赖；滚动重渲染不能让所有未测行重新估高。
  const getItemKey = useCallback((index: number) => messageIds[index] ?? index, [messageIds]);
  const virtualizer = useVirtualizer({
    count: messageIds.length,
    // 不能读祖先 ref：Viewport 的组合 ref 在运行状态切换时换身份，React 先置 null 再重绑，而子组件的
    // layout effect 跑在重绑之前。读到 null 虚拟列表会拆掉滚动监听，下次渲染再按旧 offset 滚回去，顶掉
    // 浏览器的滚动锚定，过程自动收起时正在读的段落因此一帧跳走。这里取渲染时的元素，不受提交过程影响。
    getScrollElement: () => scrollElement,
    // 只在算布局时读一次消息，不订阅：订阅会让流式输出的每个 token 都重算整个列表。
    estimateSize: (index) => {
      const messageId = messageIds[index];
      const width = containerRef.current?.clientWidth || null;
      const message = messagesById(aui.thread().getState().messages).get(messageId ?? "");
      return (messageId === undefined ? undefined : rememberedMessageHeight(taskId, width, messageId, message))
        ?? estimateMessageHeight(message, taskId);
    },
    // 浏览器铺满时对话区被隐藏：隐藏期间的读数既不进虚拟列表缓存，也不进按列宽分桶的行高记忆
    // （列宽读成 0 会把整桶换掉）。
    measureElement: (element, entry, instance) => measureRenderedElement(element, entry, instance, (size) => {
      const messageId = messageIds[instance.indexFromElement(element)];
      if (messageId !== undefined) rememberMessageHeight(taskId, element.clientWidth, messageId, size, messagesById(aui.thread().getState().messages).get(messageId));
    }),
    getItemKey,
    overscan: 8,
    rangeExtractor: (range) => Array.from(new Set([
      ...defaultRangeExtractor(range),
      ...activeIndexes,
    ])).sort((left, right) => left - right),
  });
  useThreadHistoryPaging({ virtualizer, messageIds, scrollElement });
  const virtualItems = virtualizer.getVirtualItems();
  const firstItem = virtualItems[0];
  const lastItem = virtualItems.at(-1);
  const measureElement = useCallback(
    (element: HTMLDivElement | null) => virtualizer.measureElement(element),
    [virtualizer],
  );

  return (
    <div
      ref={containerRef}
      className="empty:hidden"
      style={{
        paddingTop: firstItem?.start ?? 0,
        paddingBottom: Math.max(0, virtualizer.getTotalSize() - (lastItem?.end ?? 0)),
      }}
    >
      {virtualItems.map((item, position) => {
        const messageId = messageIds[item.index];
        if (!messageId) return null;
        // 活动尾行可以与可见历史不相邻；首尾 padding 之外也必须保留虚拟坐标中的间隙。
        const gap = position === 0 ? 0 : Math.max(0, item.start - virtualItems[position - 1].end);
        return (
          <Fragment key={messageId}>
            {gap > 0 && <div aria-hidden="true" data-slot="virtual-message-gap" style={{ height: gap }} />}
            <VirtualizedThreadMessageRow
              index={item.index}
              messageId={messageId}
              components={components}
              measureElement={measureElement}
              performanceProbe={performanceProbe}
            />
          </Fragment>
        );
      })}
    </div>
  );
});

// virtualizer 每次重算布局都会对全部未测量的行取估计，按 id 线性查找会随消息数平方增长。
// 消息数组随每次更新换新引用，按引用建一次索引。
const messageIndexes = new WeakMap<readonly ThreadMessage[], Map<string, ThreadMessage>>();

function messagesById(messages: readonly ThreadMessage[]): Map<string, ThreadMessage> {
  let index = messageIndexes.get(messages);
  if (!index) {
    index = new Map(messages.map((message) => [message.id, message]));
    messageIndexes.set(messages, index);
  }
  return index;
}

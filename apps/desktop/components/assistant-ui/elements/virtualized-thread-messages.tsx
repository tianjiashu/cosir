"use client";

import { defaultRangeExtractor, measureElement as measureRowElement, useVirtualizer } from "@tanstack/react-virtual";
import {
  unstable_useThreadMessageIds,
  useAui,
  useAuiState,
  type ThreadMessage,
} from "@assistant-ui/react";
import { memo, useCallback, useMemo, useRef, type FC, type RefObject } from "react";
import type { AssistantPerformanceProbe } from "@/lib/assistant/assistant-performance-probe";
import { rememberMessageHeight, rememberedMessageHeight } from "@/lib/assistant/message-height-cache";
import { estimateMessageHeight } from "@/lib/assistant/message-height-estimate";
import { queuedMessageIdsKey } from "@/lib/assistant/transport-state-operations";
import { VirtualizedThreadMessageRow, type MessageComponents } from "@/components/assistant-ui/elements/virtualized-thread-message-row";
import { useThreadHistoryPaging } from "@/components/assistant-ui/elements/use-thread-history-paging";

type VirtualizedThreadMessagesProps = {
  /** 行高缓存按 Task 分桶。 */
  taskId: number;
  scrollElementRef: RefObject<HTMLDivElement | null>;
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
  scrollElementRef,
  components,
  performanceProbe = null,
}) {
  const aui = useAui();
  const containerRef = useRef<HTMLDivElement>(null);
  const threadMessageIds = unstable_useThreadMessageIds();
  const queuedIdsKey = useAuiState((state) => queuedMessageIdsKey(state.thread.state));
  const messageIds = useMemo(() => {
    if (!queuedIdsKey) return threadMessageIds;
    const queued = new Set(queuedIdsKey.split("|"));
    return threadMessageIds.filter((id) => !queued.has(id));
  }, [queuedIdsKey, threadMessageIds]);
  const isRunning = useAuiState((state) => state.thread.isRunning);
  const activeIndexes = useMemo(() => {
    if (!isRunning) return [];
    const last = messageIds.length - 1;
    return [last - 1, last].filter((index) => index >= 0);
  }, [isRunning, messageIds]);
  const virtualizer = useVirtualizer({
    count: messageIds.length,
    getScrollElement: () => scrollElementRef.current,
    // 只在算布局时读一次消息，不订阅：订阅会让流式输出的每个 token 都重算整个列表。
    estimateSize: (index) => {
      const messageId = messageIds[index];
      const width = containerRef.current?.clientWidth || null;
      return (messageId === undefined ? undefined : rememberedMessageHeight(taskId, width, messageId))
        ?? estimateMessageHeight(messagesById(aui.thread().getState().messages).get(messageId ?? ""));
    },
    measureElement: (element, entry, instance) => {
      const size = measureRowElement(element, entry, instance);
      const messageId = messageIds[instance.indexFromElement(element)];
      if (messageId !== undefined) rememberMessageHeight(taskId, element.clientWidth, messageId, size);
      return size;
    },
    getItemKey: (index) => messageIds[index] ?? index,
    overscan: 8,
    rangeExtractor: (range) => Array.from(new Set([
      ...defaultRangeExtractor(range),
      ...activeIndexes,
    ])).sort((left, right) => left - right),
  });
  useThreadHistoryPaging({ taskId, virtualizer, messageIds, scrollElementRef });
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
      className="mb-14 empty:hidden"
      style={{
        paddingTop: firstItem?.start ?? 0,
        paddingBottom: Math.max(0, virtualizer.getTotalSize() - (lastItem?.end ?? 0)),
      }}
    >
      {virtualItems.map((item) => {
        const messageId = messageIds[item.index];
        if (!messageId) return null;
        return (
          <VirtualizedThreadMessageRow
            key={messageId}
            index={item.index}
            messageId={messageId}
            components={components}
            measureElement={measureElement}
            performanceProbe={performanceProbe}
          />
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

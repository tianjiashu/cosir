"use client";

import { defaultRangeExtractor, useVirtualizer } from "@tanstack/react-virtual";
import {
  unstable_useThreadMessageIds,
  useAuiState,
} from "@assistant-ui/react";
import { memo, useCallback, useMemo, type FC, type RefObject } from "react";
import type { AssistantPerformanceProbe } from "@/lib/assistant/assistant-performance-probe";
import { VirtualizedThreadMessageRow, type MessageComponents } from "@/components/assistant-ui/elements/virtualized-thread-message-row";

type VirtualizedThreadMessagesProps = {
  scrollElementRef: RefObject<HTMLDivElement | null>;
  components: MessageComponents;
  performanceProbe?: AssistantPerformanceProbe | null;
};

/**
 * 按消息 id 定位的对话虚拟列表；不负责消息内容状态维护。
 * 运行中始终保留末尾两条消息（本轮用户输入与正在输出的回复）挂载，避免流式增长时被移出可见区后丢失测量。
 */
export const VirtualizedThreadMessages: FC<VirtualizedThreadMessagesProps> = memo(function VirtualizedThreadMessages({
  scrollElementRef,
  components,
  performanceProbe = null,
}) {
  const messageIds = unstable_useThreadMessageIds();
  const activeMessageIds = useAuiState((state) => state.thread.isRunning
    ? state.thread.messages.slice(-2).map((message) => message.id).join("|")
    : "");
  const activeIndexes = useMemo(
    () => activeMessageIds.split("|").map((id) => messageIds.indexOf(id)).filter((index) => index >= 0),
    [activeMessageIds, messageIds],
  );
  const virtualizer = useVirtualizer({
    count: messageIds.length,
    getScrollElement: () => scrollElementRef.current,
    estimateSize: () => 120,
    getItemKey: (index) => messageIds[index] ?? index,
    overscan: 8,
    rangeExtractor: (range) => Array.from(new Set([
      ...defaultRangeExtractor(range),
      ...activeIndexes,
    ])).sort((left, right) => left - right),
  });
  const virtualItems = virtualizer.getVirtualItems();
  const firstItem = virtualItems[0];
  const lastItem = virtualItems.at(-1);
  const measureElement = useCallback(
    (element: HTMLDivElement | null) => virtualizer.measureElement(element),
    [virtualizer],
  );

  return (
    <div
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

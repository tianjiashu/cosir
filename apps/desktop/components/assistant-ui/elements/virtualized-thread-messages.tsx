"use client";

import { defaultRangeExtractor, useVirtualizer } from "@tanstack/react-virtual";
import {
  ThreadPrimitive,
  unstable_useThreadMessageIds,
  useAuiState,
} from "@assistant-ui/react";
import { useMemo, type ComponentProps, type FC, type RefObject } from "react";

type MessageComponents = ComponentProps<typeof ThreadPrimitive.Unstable_MessageById>["components"];

type VirtualizedThreadMessagesProps = {
  scrollElementRef: RefObject<HTMLDivElement | null>;
  components: MessageComponents;
  rowPaddingBottom: string;
  keepActiveTail?: boolean;
};

/** Shared assistant-ui message-id virtualizer for writable and readonly threads. */
export const VirtualizedThreadMessages: FC<VirtualizedThreadMessagesProps> = ({
  scrollElementRef,
  components,
  rowPaddingBottom,
  keepActiveTail = false,
}) => {
  const messageIds = unstable_useThreadMessageIds();
  const activeMessageIds = useAuiState((state) => keepActiveTail && state.thread.isRunning
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
    rangeExtractor: keepActiveTail
      ? (range) => Array.from(new Set([
        ...defaultRangeExtractor(range),
        ...activeIndexes,
      ])).sort((left, right) => left - right)
      : undefined,
  });
  const virtualItems = virtualizer.getVirtualItems();
  const firstItem = virtualItems[0];
  const lastItem = virtualItems.at(-1);

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
          <div
            key={messageId}
            data-index={item.index}
            ref={virtualizer.measureElement}
            className="w-full"
            style={{ paddingBottom: rowPaddingBottom }}
          >
            <ThreadPrimitive.Unstable_MessageById
              messageId={messageId}
              components={components}
            />
          </div>
        );
      })}
    </div>
  );
};

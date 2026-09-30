"use client";

import { ThreadPrimitive } from "@assistant-ui/react";
import { memo, Profiler, type ComponentProps } from "react";

import type { AssistantPerformanceProbe } from "@/lib/assistant/assistant-performance-probe";
import { RenderErrorCard } from "@/components/render-isolation/render-error-card";
import { RenderIsolationBoundary } from "@/components/render-isolation/render-isolation-boundary";

export type MessageComponents = ComponentProps<typeof ThreadPrimitive.Unstable_MessageById>["components"];

export type VirtualizedThreadMessageRowProps = {
  index: number;
  messageId: string;
  components: MessageComponents;
  rowPaddingBottom: string;
  measureElement: (element: HTMLDivElement | null) => void;
  performanceProbe?: AssistantPerformanceProbe | null;
};

/**
 * 单条虚拟消息行的渲染边界。
 *
 * 行自身只接收稳定的消息身份和布局参数；消息内容由 assistant-ui 的按消息订阅维护。
 * 因此父列表重新计算可见区时，历史行不会因 active 消息 token 变化而重新执行。
 * 本组件不维护对话事实，也不负责虚拟列表的范围计算。
 */
export const VirtualizedThreadMessageRow = memo(function VirtualizedThreadMessageRow({
  index,
  messageId,
  components,
  rowPaddingBottom,
  measureElement,
  performanceProbe = null,
}: VirtualizedThreadMessageRowProps) {
  const message = (
    <ThreadPrimitive.Unstable_MessageById
      messageId={messageId}
      components={components}
    />
  );
  const content = performanceProbe ? (
    <Profiler
      id={`assistant-message-row:${messageId}`}
      onRender={(_, phase, actualDuration) => performanceProbe.recordReactCommit({
        scope: "message-row",
        id: messageId,
        phase,
        actualDurationMs: actualDuration,
      })}
    >
      {message}
    </Profiler>
  ) : message;

  return (
    <div
      data-index={index}
      ref={measureElement}
      className="w-full"
      style={{ paddingBottom: rowPaddingBottom }}
    >
      <RenderIsolationBoundary
        scope="message"
        resetKey={messageId}
        metadata={{ messageId, renderer: "message-row" }}
        fallback={({ onRetry }) => (
          <RenderErrorCard scope="message" onRetry={onRetry} />
        )}
      >
        {content}
      </RenderIsolationBoundary>
    </div>
  );
});

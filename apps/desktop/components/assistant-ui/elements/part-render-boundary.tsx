"use client";

import { useAuiState } from "@assistant-ui/react";

import { RenderErrorCard } from "@/components/render-isolation/render-error-card";
import {
  RenderIsolationBoundary,
  type RenderFallback,
  type RenderIsolationMetadata,
} from "@/components/render-isolation/render-isolation-boundary";

type PartRenderBoundaryProps = {
  children: React.ReactNode;
  renderer: string;
  fallback?: RenderFallback;
  metadata?: RenderIsolationMetadata;
};

/**
 * 将 assistant-ui 当前 message part 隔离为独立渲染单元。
 *
 * 身份来自 assistant-ui 的 message/part 状态，不写入 runtime，也不改变
 * part 内容；因此重试只重新挂载当前 renderer，旁边的消息和 part 不受影响。
 */
export function PartRenderBoundary({
  children,
  renderer,
  fallback,
  metadata,
}: PartRenderBoundaryProps) {
  const messageId = useAuiState((state) => state.message.id);
  const partIndex = useAuiState((state) => state.message.parts.indexOf(state.part));
  const partType = useAuiState((state) => state.part.type);
  const partId = useAuiState((state) => (
    state.part.type === "tool-call" ? state.part.toolCallId : `${partIndex}`
  ));
  const defaultFallback: RenderFallback = ({ onRetry }) => (
    <RenderErrorCard
      scope="part"
      label={renderer}
      onRetry={onRetry}
    />
  );

  return (
    <RenderIsolationBoundary
      scope="part"
      resetKey={`${messageId}:${partId}:${partType}`}
      fallback={fallback ?? defaultFallback}
      metadata={{
        messageId,
        partId,
        partIndex,
        partType,
        renderer,
        ...metadata,
      }}
    >
      {children}
    </RenderIsolationBoundary>
  );
}

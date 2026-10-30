"use client";

import { MessagePrimitive, useAuiState } from "@assistant-ui/react";
import { Image } from "@/components/image";
import { PartRenderBoundary } from "@/components/assistant-ui/elements/part-render-boundary";
import { RenderErrorCard } from "@/components/render-isolation/render-error-card";

/**
 * 将用户消息中的图片 part 渲染为气泡上方的预览区。
 *
 * 普通文件不在这里：它们是正文里的行内胶囊，由 `UserMessageContent` 在原位渲染。
 * 图片复用共享 Image 组件，以保持加载失败处理、transport 定位解析和点击放大行为一致；
 * 本组件只负责布局（位于文本气泡上方并右对齐），每张图的渲染异常由独立 Boundary 隔离。
 */
export function UserMessageImages() {
  const hasImages = useAuiState((state) => state.message.parts.some((part) => part.type === "image"));

  if (!hasImages) return null;

  return (
    <div
      data-slot="user-message-images"
      className="flex max-w-full flex-wrap justify-end gap-2"
    >
      <MessagePrimitive.Parts>{({ part }) => part.type === "image" ? (
        <PartRenderBoundary
          renderer="图片附件"
          metadata={{ attachmentType: "image" }}
          fallback={({ onRetry }) => (
            <RenderErrorCard scope="attachment" label="图片附件" onRetry={onRetry} />
          )}
        >
          <Image image={part.image} />
        </PartRenderBoundary>
      ) : null}</MessagePrimitive.Parts>
    </div>
  );
}

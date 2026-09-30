"use client";

import { MessagePrimitive, useAuiState } from "@assistant-ui/react";
import { Image } from "@/components/image";
import { UserMessageFilePart } from "@/components/assistant-ui/elements/attachment.aui";
import { PartRenderBoundary } from "@/components/assistant-ui/elements/part-render-boundary";
import { RenderErrorCard } from "@/components/render-isolation/render-error-card";

/**
 * 将用户消息中的非文本 part 渲染为独立的附件区域。
 *
 * 图片复用共享 Image 组件，以保持加载失败处理、transport 定位解析和点击
 * 放大行为一致。本组件只负责消息级布局：附件位于文本气泡上方并右对齐，
 * 不参与气泡文字换行；每个附件的渲染异常由独立 Boundary 隔离。本组件不
 * 上传、修改或持久化文件事实。
 */
export function UserMessageAttachments() {
  const hasAttachments = useAuiState((state) => state.message.parts.some(
    (part) => part.type === "image" || part.type === "file",
  ));

  if (!hasAttachments) return null;

  return (
    <div
      data-slot="user-message-attachments"
      className="flex max-w-full flex-wrap justify-end gap-2"
    >
      <MessagePrimitive.Parts>{({ part }) => {
        switch (part.type) {
          case "image":
            return (
              <PartRenderBoundary
                renderer="图片附件"
                metadata={{ attachmentType: "image" }}
                fallback={({ onRetry }) => (
                  <RenderErrorCard scope="attachment" label="图片附件" onRetry={onRetry} />
                )}
              >
                <Image
                  type="image"
                  image={part.image}
                  status={{ type: "complete" }}
                  display="message-thumbnail"
                />
              </PartRenderBoundary>
            );
          case "file":
            return (
              <PartRenderBoundary
                renderer="文件附件"
                metadata={{ attachmentType: "file" }}
                fallback={({ onRetry }) => (
                  <RenderErrorCard scope="attachment" label="文件附件" onRetry={onRetry} />
                )}
              >
                <UserMessageFilePart
                  filename={part.filename ?? "附件"}
                  mimeType={part.mimeType}
                />
              </PartRenderBoundary>
            );
          default:
            return null;
        }
      }}</MessagePrimitive.Parts>
    </div>
  );
}

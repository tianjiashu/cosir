import { Fragment, useMemo } from "react";
import { useAuiState } from "@assistant-ui/react";
import { useShallow } from "zustand/react/shallow";
import { ImageTile } from "@/components/image/image-tile";
import { useImageGallery } from "@/components/image/image-gallery";
import { useAttachmentWorkspaceId } from "@/components/conversation/thread/attachment-context";
import { ImageStack } from "@/components/conversation/thread/image-stack";
import { RenderErrorCard } from "@/components/render-isolation/render-error-card";
import { RenderIsolationBoundary } from "@/components/render-isolation/render-isolation-boundary";
import { resolveTransportImageSrc } from "@/lib/assistant/attachments/image-src";
import { shouldStackImages } from "@/lib/assistant/turn/image-stack-policy";

/**
 * 将用户消息中的图片 part 渲染为气泡上方的预览区。
 *
 * 普通文件不在这里：它们是正文里的行内胶囊，由 `UserMessageContent` 在原位渲染。
 * 本组件负责布局（位于文本气泡上方并右对齐，超过阈值收成一叠）与这一组图片的放大预览：地址在这里
 * 解析一次，缩略图与灯箱共用，点任意一张都能在整组里左右翻看。每张图的渲染异常由独立 Boundary 隔离（这里不在 part 作用域里渲染，所以不用 `PartRenderBoundary`）。
 */
export function UserMessageImages() {
  const locators = useAuiState(useShallow((state) => state.message.parts.flatMap((part) => (part.type === "image" ? [part.image] : []))));
  const messageId = useAuiState((state) => state.message.id);
  const workspaceId = useAttachmentWorkspaceId();
  const images = useMemo(
    () => locators.map((locator, index) => ({ src: resolveTransportImageSrc(locator, workspaceId), alt: `图片 ${index + 1}` })),
    [locators, workspaceId],
  );
  const gallery = useImageGallery(images);

  if (images.length === 0) return null;

  const renderTile = (index: number) => (
    <RenderIsolationBoundary
      scope="part"
      resetKey={`${messageId}:image:${index}:${locators[index]}`}
      metadata={{ messageId, renderer: "图片附件", attachmentType: "image" }}
      fallback={({ onRetry }) => (
        <RenderErrorCard scope="attachment" label="图片附件" onRetry={onRetry} />
      )}
    >
      <ImageTile src={images[index].src} alt={`图片 ${index + 1}`} onOpen={() => gallery.open(index)} />
    </RenderIsolationBoundary>
  );

  return (
    <div data-slot="user-message-images" className="flex max-w-full flex-wrap justify-end gap-2">
      {shouldStackImages(images.length)
        ? <ImageStack count={images.length} renderTile={renderTile} />
        : images.map((_, index) => <Fragment key={index}>{renderTile(index)}</Fragment>)}
      {gallery.lightbox}
    </div>
  );
}

import { useLayoutEffect, useMemo, useRef } from "react";
import { useAui, useAuiState, type Attachment } from "@assistant-ui/react";

import { ComposerImageCard } from "@/components/composer/composer-image-card";
import { useAttachmentWorkspaceId } from "@/components/conversation/thread/attachment-context";
import { useImageGallery } from "@/components/image/image-gallery";
import { useObjectUrls } from "@/hooks/use-object-urls";
import { resolveTransportImageSrc } from "@/lib/assistant/attachments/image-src";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";

/** 本地 File 与恢复的资源定位共用一个地址解析入口，缩略图和灯箱共用解析结果。 */
function composerImageSrc(attachment: Attachment, urls: ReadonlyMap<File, string>, workspaceId: number | undefined) {
  if (attachment.file) return urls.get(attachment.file);
  const image = attachment.content?.find((part) => part.type === "image")?.image;
  return image ? resolveTransportImageSrc(image, workspaceId) : undefined;
}

/**
 * 待发图片组：只读 composer 的附件集合，统一管理对象 URL、预览、横向可见范围与删除后的焦点。
 * 图片高度由卡片决定；没有图片时不占位。普通文件仍是正文胶囊，不进入这条图片列表。
 */
export function ComposerImageAttachments() {
  const aui = useAui();
  const workspaceId = useAttachmentWorkspaceId();
  const attachments = useAuiState((s) => s.composer.attachments);
  const images = useMemo(() => attachments.filter((attachment) => attachment.type === "image"), [attachments]);
  const urls = useObjectUrls(useMemo(() => images.flatMap((image) => image.file ? [image.file] : []), [images]));
  const pictures = useMemo(() => images.map((image) => ({ src: composerImageSrc(image, urls, workspaceId), alt: image.name })), [images, urls, workspaceId]);
  const gallery = useImageGallery(pictures);
  const stripRef = useRef<HTMLUListElement>(null);
  const buttons = useRef(new Map<string, HTMLButtonElement>());
  const previousIds = useRef(new Set<string>());
  const pendingFocus = useRef<{ id: string; index: number; origin: Element | null } | null>(null);

  useLayoutEffect(() => {
    const strip = stripRef.current!;
    const added = images.findLast((image) => !previousIds.current.has(image.id));
    previousIds.current = new Set(images.map((image) => image.id));
    if (added) {
      // 只滚动图片条，不用 scrollIntoView 带动外层对话，也不把正文焦点抢到新图上。
      const picture = buttons.current.get(added.id)!.getBoundingClientRect();
      const bounds = strip.getBoundingClientRect();
      if (picture.right > bounds.right) strip.scrollLeft += picture.right - bounds.right;
      else if (picture.left < bounds.left) strip.scrollLeft += picture.left - bounds.left;
    }
    const pending = pendingFocus.current;
    if (!pending || previousIds.current.has(pending.id)) return;
    pendingFocus.current = null;
    // 删除完成前用户若已转向别处，就不再抢回焦点。
    if (document.activeElement !== document.body && document.activeElement !== pending.origin) return;
    const neighbor = images[Math.min(pending.index, images.length - 1)];
    if (neighbor) buttons.current.get(neighbor.id)!.focus();
    else strip.closest("form")!.querySelector<HTMLElement>('[contenteditable="true"]')?.focus();
  }, [images]);

  const remove = async (id: string, index: number) => {
    pendingFocus.current = { id, index, origin: document.activeElement };
    try {
      await aui.composer().attachment({ id }).remove();
    } catch (error) {
      pendingFocus.current = null;
      void frontendLog("ERROR", "composer_image_remove_failed", "移除待发图片失败", { data: { attachmentId: id }, error });
      notify.error("移除图片失败", { error });
    }
  };

  return (
    <>
      <ul ref={stripRef} data-slot="image-attachment-strip" aria-label="待发送图片" className="flex min-w-0 flex-nowrap gap-2 overflow-x-auto overflow-y-hidden px-2 py-1 empty:hidden">
        {images.map((image, index) => (
          <ComposerImageCard
            key={image.id}
            ref={(button) => { if (button) buttons.current.set(image.id, button); else buttons.current.delete(image.id); }}
            src={pictures[index].src}
            name={image.name}
            onOpen={() => gallery.open(index)}
            onRemove={() => void remove(image.id, index)}
          />
        ))}
      </ul>
      {gallery.lightbox}
    </>
  );
}

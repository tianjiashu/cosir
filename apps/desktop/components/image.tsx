"use client";

import {
  memo,
  useState,
  useEffect,
  useCallback,
  useRef,
  useSyncExternalStore,
  type PropsWithChildren,
} from "react";
import { createPortal } from "react-dom";
import { ImageIcon, ImageOffIcon, XIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import { useAttachmentWorkspaceId } from "@/components/assistant-ui/elements/attachment-context";
import { resolveTransportImageSrc } from "@/hooks/use-attachment-src";

/** 消息缩略图的固定尺寸外框。 */
function ImageRoot({ children }: PropsWithChildren) {
  return (
    <div
      data-slot="image-root"
      className="aui-image-root border-border/70 bg-muted/40 relative h-32 w-40 max-w-full overflow-hidden rounded-lg border"
    >
      {children}
    </div>
  );
}

function ImagePreview({ src, alt }: { src: string; alt: string }) {
  const imgRef = useRef<HTMLImageElement>(null);
  const [loadedSrc, setLoadedSrc] = useState<string | undefined>(undefined);
  const [errorSrc, setErrorSrc] = useState<string | undefined>(undefined);

  const loaded = loadedSrc === src;
  const error = errorSrc === src;

  useEffect(() => {
    if (
      imgRef.current?.complete &&
      imgRef.current.naturalWidth > 0
    ) {
      setLoadedSrc(src);
    }
  }, [src]);

  return (
    <div
      data-slot="image-preview"
      className="relative h-full min-h-0"
    >
      {!loaded && !error && (
        <div
          data-slot="image-preview-loading"
          className="bg-muted/50 absolute inset-0 flex items-center justify-center"
        >
          <ImageIcon className="text-muted-foreground size-8 animate-pulse" />
        </div>
      )}
      {error ? (
        <div
          data-slot="image-preview-error"
          className="bg-muted/50 flex min-h-32 items-center justify-center p-4"
        >
          <ImageOffIcon className="text-muted-foreground size-8" />
        </div>
      ) : (
        // The source can be a local data/blob URL or a provider URL.
        <img
          ref={imgRef}
          src={src}
          alt={alt}
          className={cn(
            "block h-full w-full object-contain",
            !loaded && "invisible",
          )}
          onLoad={() => setLoadedSrc(src)}
          onError={() => setErrorSrc(src)}
        />
      )}
    </div>
  );
}

type ImageZoomProps = PropsWithChildren<{
  src: string;
  alt?: string;
}>;

function ImageZoom({ src, alt = "图片预览", children }: ImageZoomProps) {
  const isMounted = useSyncExternalStore(
    () => () => undefined,
    () => true,
    () => false,
  );
  const [isOpen, setIsOpen] = useState(false);
  const triggerRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const overlayRef = useRef<HTMLDivElement>(null);

  const handleOpen = useCallback(() => setIsOpen(true), []);
  const handleClose = useCallback(() => {
    setIsOpen(false);
    triggerRef.current?.focus();
  }, []);

  useEffect(() => {
    if (!isOpen) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        handleClose();
        return;
      }
      if (e.key !== "Tab") return;
      const focusables = overlayRef.current?.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])',
      );
      const first = focusables?.[0];
      const last = focusables?.[focusables.length - 1];
      if (!first || !last) return;
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [isOpen, handleClose]);

  useEffect(() => {
    if (!isOpen) return;
    const originalOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = originalOverflow;
    };
  }, [isOpen]);

  useEffect(() => {
    if (isOpen) closeRef.current?.focus();
  }, [isOpen]);

  return (
    <>
      <div
        ref={triggerRef}
        onClick={handleOpen}
        onKeyDown={(event) => {
          if (event.key !== "Enter" && event.key !== " ") return;
          // 打开后焦点会转到关闭按钮；取消默认激活，避免同一次按键立即关闭弹层。
          event.preventDefault();
          handleOpen();
        }}
        role="button"
        tabIndex={0}
        className="aui-image-zoom-trigger cursor-zoom-in"
        aria-label="点击放大图片"
      >
        {children}
      </div>
      {isMounted &&
        isOpen &&
        createPortal(
          <div
            ref={overlayRef}
            data-slot="image-zoom-overlay"
            role="dialog"
            aria-modal="true"
            className="aui-image-zoom-overlay fade-in animate-in fixed inset-0 z-50 flex items-center justify-center bg-black/80 duration-200"
            onClick={handleClose}
            aria-label="放大的图片"
          >
            <img
              data-slot="image-zoom-content"
              src={src}
              alt={alt}
              className="aui-image-zoom-content fade-in zoom-in-95 animate-in max-h-[90vh] max-w-[90vw] cursor-zoom-out object-contain duration-200"
              onClick={(e) => {
                e.stopPropagation();
                handleClose();
              }}
            />
            <button
              ref={closeRef}
              type="button"
              aria-label="关闭放大图片"
              onClick={(e) => {
                e.stopPropagation();
                handleClose();
              }}
              className="text-muted-foreground hover:text-foreground bg-background/80 absolute end-4 top-4 cursor-pointer rounded-md p-2"
            >
              <XIcon className="size-5" />
            </button>
          </div>,
          document.body,
        )}
    </>
  );
}

/**
 * 用户消息里的图片附件缩略图（唯一调用方为 `UserMessageAttachments`）。
 * 附件 locator 经 `resolveTransportImageSrc` 解析为可显示地址，解析不到时显示占位图标。
 */
function ImageImpl({ image }: { image: string }) {
  const imageSrc = resolveTransportImageSrc(image, useAttachmentWorkspaceId());

  if (!imageSrc) {
    return (
      <ImageRoot>
        <div className="bg-muted/50 flex h-full items-center justify-center p-4">
          <ImageIcon className="text-muted-foreground size-8" />
        </div>
      </ImageRoot>
    );
  }

  return (
    <ImageRoot>
      <ImageZoom src={imageSrc} alt="图片内容">
        <ImagePreview src={imageSrc} alt="图片内容" />
      </ImageZoom>
    </ImageRoot>
  );
}

export const Image = memo(ImageImpl);

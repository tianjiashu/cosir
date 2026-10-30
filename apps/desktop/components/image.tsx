"use client";

import { memo, useState, useEffect, useRef, type PropsWithChildren } from "react";
import { ImageIcon, ImageOffIcon } from "lucide-react";
import { ImageLightbox } from "@/components/ui/image-lightbox";
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

/**
 * 用户消息里的图片附件缩略图（唯一调用方为 `UserMessageImages`）。
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
      <ImageLightbox
        src={imageSrc}
        alt="图片内容"
        trigger={
          <div className="aui-image-zoom-trigger h-full cursor-zoom-in" aria-label="点击放大图片">
            <ImagePreview src={imageSrc} alt="图片内容" />
          </div>
        }
      />
    </ImageRoot>
  );
}

export const Image = memo(ImageImpl);

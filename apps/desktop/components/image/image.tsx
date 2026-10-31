import { memo, useState, useEffect, useRef, type PropsWithChildren } from "react";
import { ImageIcon, ImageOffIcon } from "lucide-react";
import { cn } from "@/lib/utils";

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
          loading="lazy"
          decoding="async"
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
 * 按比例完整显示的图片缩略图：用户消息里的图片与网页截图。要方形裁切的小图用 `ImageThumbnail`。
 *
 * 地址由调用方解析好再传入（解析不到时显示占位图标）；放大预览由图片组（`useImageGallery`）持有，
 * 这里只经 `onOpen` 请求打开。
 */
function ImageImpl({ src, onOpen }: { src: string | undefined; onOpen?: () => void }) {
  if (!src) {
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
      {onOpen ? (
        <button type="button" className="aui-image-zoom-trigger block size-full cursor-zoom-in" aria-label="点击放大图片" onClick={onOpen}>
          <ImagePreview src={src} alt="图片内容" />
        </button>
      ) : (
        <ImagePreview src={src} alt="图片内容" />
      )}
    </ImageRoot>
  );
}

export const Image = memo(ImageImpl);

import { memo, useState, type ReactNode } from "react";
import { AlertCircleIcon, ImageIcon, ImageOffIcon, Loader2Icon, XIcon } from "lucide-react";

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { IMAGE_TILE_SIZE, type ImageTileSize } from "@/lib/image/tile-size";
import { cn } from "@/lib/utils";

type ImageTileProps = {
  /** 可显示的地址；尚未就绪（对象 URL 未建好、附件定位解析不到）时为空，显示占位图标。 */
  src?: string | null;
  /** 替代文本，也用于按钮的无障碍名称。 */
  alt: string;
  size?: ImageTileSize;
  /** 悬停提示（文件名、路径）；不传则不提示。 */
  title?: string;
  /** 上传状态遮罩，只有输入框的待发图片会经历上传；展示场景恒为就绪。 */
  state?: "ready" | "uploading" | "error";
  errorMessage?: string;
  onRemove?: () => void;
  /** 请求放大预览；灯箱由图片组（`useImageGallery`）持有，不传时不可点。 */
  onOpen?: () => void;
};

/**
 * 对话与输入框里唯一的图片缩略图：固定尺寸、圆角、裁切铺满并保留顶部。
 *
 * 裁切而不是按比例完整显示：整页截图这类长图完整塞进小框只剩一条细线；保留顶部是因为截图与文档最有
 * 信息的是开头。完整的图在灯箱里看（长图按宽度铺满、可滚动）。只负责呈现、加载与失败占位，以及转发
 * 打开 / 移除意图；地址从哪来、何时上传由调用方负责。尺寸只有两档，见 `IMAGE_TILE_SIZE`。
 */
function ImageTileImpl({ src, alt, size = "regular", title, state = "ready", errorMessage, onRemove, onOpen }: ImageTileProps) {
  const { width, height } = IMAGE_TILE_SIZE[size];
  const statusLabel = state === "uploading" ? "，上传中" : state === "error" ? "，上传失败" : "";
  const tile = (
    <div
      data-slot="image-tile"
      data-size={size}
      data-state={state}
      style={{ width, height }}
      className={cn(
        "bg-muted/40 border-border/70 relative max-w-full shrink-0 overflow-hidden rounded-lg border animate-in fade-in-0 zoom-in-95 duration-200 motion-reduce:animate-none",
        state === "error" && "border-destructive/60",
      )}
    >
      {src && onOpen ? (
        <button
          type="button"
          aria-label={`预览图片 ${alt}${statusLabel}`}
          onClick={onOpen}
          className="focus-visible:ring-ring absolute inset-0 cursor-zoom-in outline-none focus-visible:ring-2 focus-visible:ring-inset"
        >
          <TilePicture src={src} alt={alt} />
        </button>
      ) : src ? (
        <TilePicture src={src} alt={alt} />
      ) : (
        <ImageIcon className="text-muted-foreground absolute inset-0 m-auto size-6" aria-label={`${alt}${statusLabel}`} />
      )}
      {state === "uploading" && <StatusOverlay><Loader2Icon className="text-muted-foreground size-4 animate-spin" /></StatusOverlay>}
      {state === "error" && <StatusOverlay><AlertCircleIcon className="text-destructive size-4" /></StatusOverlay>}
      {onRemove && (
        <button
          type="button"
          className="bg-foreground/65 text-background hover:bg-foreground/80 focus-ring absolute top-1 right-1 rounded-full p-0.5"
          aria-label={`移除附件 ${alt}`}
          onClick={onRemove}
        >
          <XIcon className="size-3.5" />
        </button>
      )}
    </div>
  );
  if (!title && !errorMessage) return tile;
  return (
    <Tooltip>
      <TooltipTrigger render={tile} />
      <TooltipContent side="top" className="break-all">
        {title}
        {errorMessage && <p>{errorMessage}</p>}
      </TooltipContent>
    </Tooltip>
  );
}

export const ImageTile = memo(ImageTileImpl);

/** 加载中显示占位，失败显示破图标；按 src 记状态，地址变了重新加载。 */
function TilePicture({ src, alt }: { src: string; alt: string }) {
  const [loaded, setLoaded] = useState<string | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  if (failed === src) return <ImageOffIcon className="text-muted-foreground absolute inset-0 m-auto size-6" aria-label={`${alt}，无法显示`} />;
  return (
    <>
      {loaded !== src && <ImageIcon className="text-muted-foreground absolute inset-0 m-auto size-6 animate-pulse" aria-hidden="true" />}
      <img
        src={src}
        alt={alt}
        loading="lazy"
        decoding="async"
        className={cn("block size-full object-cover object-top", loaded !== src && "invisible")}
        onLoad={() => setLoaded(src)}
        onError={() => setFailed(src)}
      />
    </>
  );
}

function StatusOverlay({ children }: { children: ReactNode }) {
  return (
    <div aria-hidden="true" className="bg-background/60 pointer-events-none absolute inset-0 flex items-center justify-center backdrop-blur-[2px]">
      {children}
    </div>
  );
}

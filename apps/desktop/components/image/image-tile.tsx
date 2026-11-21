import { memo } from "react";
import { ImageIcon, ImageOffIcon } from "lucide-react";

import { ImagePicture } from "@/components/image/image-picture";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { IMAGE_TILE_SIZE, type ImageTileSize } from "@/lib/image/tile-size";

type ImageTileProps = {
  /** 可显示的地址；尚未就绪（对象 URL 未建好、附件定位解析不到）时为空，显示占位图标。 */
  src?: string | null;
  /** 替代文本，也用于按钮的无障碍名称。 */
  alt: string;
  size?: ImageTileSize;
  /** 悬停提示（文件名、路径）；不传则不提示。 */
  title?: string;
  /** 请求放大预览；灯箱由图片组（`useImageGallery`）持有，不传时不可点。 */
  onOpen?: () => void;
  /** 图片加载失败时通知调用方（如重读文件信息换新地址）；方块自己照常显示破图标。 */
  onError?: () => void;
  /** 图已经不在了（文件被删、无法访问）：中性图标、不可点，说明放在 `title`。 */
  unavailable?: boolean;
};

/**
 * 对话历史的图片缩略图：固定尺寸、圆角、裁切铺满并保留顶部；待发图片由 ComposerImageCard 呈现。
 *
 * 裁切而不是按比例完整显示：整页截图这类长图完整塞进小框只剩一条细线；保留顶部是因为截图与文档最有
 * 信息的是开头。完整的图在灯箱里看（长图按宽度铺满、可滚动）。只负责呈现、加载与失败占位，以及转发
 * 打开意图；地址由调用方提供，加载呈现交给 ImagePicture。尺寸见 `IMAGE_TILE_SIZE`。
 */
function ImageTileImpl({ src, alt, size = "regular", title, onOpen, onError, unavailable = false }: ImageTileProps) {
  const { width, height } = IMAGE_TILE_SIZE[size];
  const tile = (
    <div
      data-slot="image-tile"
      data-size={size}
      data-unavailable={unavailable || undefined}
      style={{ width, height }}
      className="bg-muted/40 border-border/70 relative max-w-full shrink-0 overflow-hidden rounded-lg border animate-in fade-in-0 zoom-in-95 duration-200 motion-reduce:animate-none"
    >
      {unavailable ? (
        <ImageOffIcon className="text-muted-foreground absolute inset-0 m-auto size-6" aria-label={`${alt}，${title ?? "不可用"}`} />
      ) : src && onOpen ? (
        <button
          type="button"
          aria-label={`预览图片 ${alt}`}
          onClick={onOpen}
          className="focus-visible:ring-ring absolute inset-0 cursor-zoom-in outline-none focus-visible:ring-2 focus-visible:ring-inset"
        >
          <ImagePicture src={src} alt={alt} onError={onError} className="block size-full object-cover object-top" />
        </button>
      ) : src ? (
        <ImagePicture src={src} alt={alt} onError={onError} className="block size-full object-cover object-top" />
      ) : (
        <ImageIcon className="text-muted-foreground absolute inset-0 m-auto size-6" aria-label={alt} />
      )}
    </div>
  );
  if (!title) return tile;
  return (
    <Tooltip>
      <TooltipTrigger render={tile} />
      <TooltipContent side="top">
        {title}
      </TooltipContent>
    </Tooltip>
  );
}

export const ImageTile = memo(ImageTileImpl);

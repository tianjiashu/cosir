import { useImageGallery } from "@/components/image/image-gallery";
import { ImageTile } from "@/components/image/image-tile";
import type { LightboxImage } from "@/components/ui/image-lightbox";
import type { ImageTileSize } from "@/lib/image/tile-size";
import { cn } from "@/lib/utils";

/**
 * 一张图：地址已由调用方解析；`title` 是悬停提示（如文件路径）。`previewSrc` 是方块用的小图（不传就用 `src`），
 * 灯箱仍用 `src` 原图；`onError` 让调用方在加载失败时重读文件信息（版本过期换新地址）；`unavailable` 是图已经
 * 不在了的说明（文件被删、无法访问），方块保持原尺寸、换成中性图标，不可点。
 */
export type StripImage = LightboxImage & { title?: string; previewSrc?: string; onError?: () => void; unavailable?: string };

/**
 * 工具结果里的一组图片（`read_image` 读到的图、网页截图）：缩略图横排，点任意一张在整组里放大翻看。
 *
 * 地址解析归调用方（工具展示数据只带附件 asset id，按消息附件同一规则解析）；这里只管排版与放大预览，
 * 会出图的工具都用它，外观与对话里其他图片一致。
 *
 * `limit`：只铺最近的几张（汇总行用），更早的收成一个「+N」，点它从第一张开始翻；灯箱始终是整组。
 */
export function ImageStrip({ images, label, size = "regular", limit }: { images: readonly StripImage[]; label: string; size?: ImageTileSize; limit?: number }) {
  const gallery = useImageGallery(images);
  const hidden = limit === undefined ? 0 : Math.max(0, images.length - limit);
  return (
    <>
      <ul className={cn("flex gap-2", size === "inline" ? "items-center gap-1" : "flex-wrap")} aria-label={label}>
        {hidden > 0 && <li><button type="button" data-slot="image-strip-more" aria-label={`另有 ${hidden} 张，从第一张开始查看`} onClick={() => gallery.open(0)}
          className="text-muted-foreground hover:text-foreground focus-ring rounded-sm px-1 text-xs tabular-nums">+{hidden}</button></li>}
        {images.slice(hidden).map((image, offset) => (
          // 只按位置：方块里的图片状态按地址自己记，地址变了（换版本、后端换端口）原地换图，不重新挂载、不丢焦点。
          <li key={offset}>
            <ImageTile
              size={size}
              src={image.unavailable ? null : (image.previewSrc ?? image.src)}
              alt={image.alt}
              title={image.unavailable ?? image.title}
              unavailable={image.unavailable !== undefined}
              onError={image.onError}
              onOpen={() => gallery.open(hidden + offset)}
            />
          </li>
        ))}
      </ul>
      {gallery.lightbox}
    </>
  );
}

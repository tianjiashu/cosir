import { useImageGallery } from "@/components/image/image-gallery";
import { ImageTile } from "@/components/image/image-tile";
import type { LightboxImage } from "@/components/ui/image-lightbox";

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
 */
export function ImageStrip({ images, label }: { images: readonly StripImage[]; label: string }) {
  const gallery = useImageGallery(images);
  return (
    <>
      <ul className="flex flex-wrap gap-2" aria-label={label}>
        {images.map((image, index) => (
          // 只按位置：方块里的图片状态按地址自己记，地址变了（换版本、后端换端口）原地换图，不重新挂载、不丢焦点。
          <li key={index}>
            <ImageTile
              src={image.unavailable ? null : (image.previewSrc ?? image.src)}
              alt={image.alt}
              title={image.unavailable ?? image.title}
              unavailable={image.unavailable !== undefined}
              onError={image.onError}
              onOpen={() => gallery.open(index)}
            />
          </li>
        ))}
      </ul>
      {gallery.lightbox}
    </>
  );
}

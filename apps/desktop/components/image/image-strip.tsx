import { useImageGallery } from "@/components/image/image-gallery";
import { ImageTile } from "@/components/image/image-tile";
import type { LightboxImage } from "@/components/ui/image-lightbox";

/** 一张图：地址已由调用方解析；`title` 是悬停提示（如文件路径）。 */
export type StripImage = LightboxImage & { title?: string };

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
          <li key={`${index}:${image.src}`}>
            <ImageTile src={image.src} alt={image.alt} title={image.title} onOpen={() => gallery.open(index)} />
          </li>
        ))}
      </ul>
      {gallery.lightbox}
    </>
  );
}

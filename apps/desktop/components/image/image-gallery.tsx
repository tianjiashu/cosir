import { useState, type ReactNode } from "react";
import { ImageLightbox, type LightboxImage } from "@/components/ui/image-lightbox";

/**
 * 一组图片共用的放大预览：持有「当前打开第几张」，只渲染一个灯箱，缩略图经 `open(i)` 请求打开。
 *
 * 打开状态放在组这一层而不是每张缩略图里，灯箱才知道同组还有哪些图、能左右翻看。调用方负责把
 * 图片地址解析好（消息附件、工具结果、输入框里的本地文件各有来源），并把返回的 `lightbox` 渲染出来。
 */
export function useImageGallery(images: readonly LightboxImage[]): { open: (index: number) => void; lightbox: ReactNode } {
  const [index, setIndex] = useState<number | null>(null);
  return {
    open: setIndex,
    lightbox: <ImageLightbox images={images} index={index} onIndexChange={setIndex} />,
  };
}

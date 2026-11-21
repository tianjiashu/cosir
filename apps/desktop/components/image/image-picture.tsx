import { useState, type ReactNode } from "react";
import { ImageIcon, ImageOffIcon } from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * 图片加载与解码占位：失败按 src 记录，换地址时允许重新加载；裁切、尺寸和交互由外层决定。
 * 已显示的图换地址时不先清空，浏览器解码好新图再替换，文件更新或后端换端口时不闪。
 * fallback 只替换失败呈现：待发卡片解释「无法预览」，紧凑的历史缩略图用破图图标。
 */
export function ImagePicture({ src, alt, className, onError, fallback }: {
  src?: string | null;
  alt: string;
  className: string;
  onError?: () => void;
  fallback?: ReactNode;
}) {
  const [loaded, setLoaded] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  if (src && failed === src) return fallback ?? <ImageOffIcon className="text-muted-foreground absolute inset-0 m-auto size-6" aria-label={`${alt}，无法显示`} />;
  return (
    <>
      {(!src || !loaded) && <ImageIcon className="text-muted-foreground absolute inset-0 m-auto size-6 animate-pulse motion-reduce:animate-none" aria-label={`${alt}，正在加载预览`} />}
      {src && <img
        src={src}
        alt={alt}
        loading="lazy"
        decoding="async"
        className={cn(className, !loaded && "invisible")}
        onLoad={() => setLoaded(true)}
        onError={() => { setFailed(src); onError?.(); }}
      />}
    </>
  );
}

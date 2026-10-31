import { useEffect, useRef, useState, type ReactNode } from "react";
import { Dialog as DialogPrimitive } from "@base-ui/react/dialog";
import { ChevronLeftIcon, ChevronRightIcon, ImageIcon, ImageOffIcon, XIcon } from "lucide-react";

import { cn } from "@/lib/utils";

/** 灯箱里的一张图；`src` 解析不到（附件定位失效）时显示「图片无法显示」，不影响翻页。 */
export type LightboxImage = { src: string | undefined; alt: string };

type ImageLightboxProps = {
  images: readonly LightboxImage[];
  /** 当前显示第几张；null 或越界即关闭。 */
  index: number | null;
  onIndexChange: (index: number | null) => void;
};

/**
 * 图片放大预览：一组图片共用一个全屏暗色灯箱，多张时可以左右翻看。
 *
 * 打开哪一张由图片组（`useImageGallery`）持有，缩略图只负责请求打开；灯箱本身不知道图片从哪来。
 * 焦点锁定、Esc 关闭、焦点归还、背景不可交互与滚动锁定都交给 base-ui Dialog，不在这里手写；
 * 点遮罩或点图片都关闭（看图灯箱的常见习惯），两者都是普通点击、不可聚焦。翻页到头不循环，相邻两张预先加载，翻页时不闪。
 */
export function ImageLightbox({ images, index, onIndexChange }: ImageLightboxProps) {
  const current = index === null ? undefined : images[index];
  const count = images.length;
  const go = (step: -1 | 1) => {
    if (index === null) return;
    const next = index + step;
    if (next >= 0 && next < count) onIndexChange(next);
  };
  useArrowKeys(current !== undefined, go);
  usePreloadNeighbors(images, index);

  return (
    <DialogPrimitive.Root open={current !== undefined} onOpenChange={(open) => { if (!open) onIndexChange(null); }}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Backdrop className="fixed inset-0 z-50 bg-black/80 duration-200 data-open:animate-in data-open:fade-in-0 data-closed:animate-out data-closed:fade-out-0" />
        <DialogPrimitive.Popup
          className="fixed inset-0 z-50 flex items-center justify-center p-8 outline-none duration-200 data-open:animate-in data-open:fade-in-0 data-open:zoom-in-95 data-closed:animate-out data-closed:fade-out-0 data-closed:zoom-out-95"
        >
          <DialogPrimitive.Title className="sr-only">放大的图片</DialogPrimitive.Title>
          {/* Popup 铺满视口，点图片以外的空白处等同点遮罩。普通点击层而非 Close：不可聚焦，打开时焦点落在关闭按钮上。 */}
          <div className="absolute inset-0 cursor-zoom-out" aria-hidden="true" onClick={() => onIndexChange(null)} />
          {current && <LightboxPicture key={`${index}:${current.src}`} image={current} onClose={() => onIndexChange(null)} />}
          <DialogPrimitive.Close
            aria-label="关闭放大图片"
            className="bg-background/80 text-muted-foreground hover:text-foreground absolute end-4 top-4 cursor-pointer rounded-md p-2"
          >
            <XIcon className="size-5" />
          </DialogPrimitive.Close>
          {count > 1 && index !== null && (
            <div className="bg-background/80 text-foreground absolute bottom-6 left-1/2 flex -translate-x-1/2 items-center gap-3 rounded-lg p-1 text-sm">
              <LightboxStep label="上一张" disabled={index === 0} onClick={() => go(-1)}>
                <ChevronLeftIcon className="size-5" />
              </LightboxStep>
              <span aria-live="polite" className="min-w-14 text-center tabular-nums">{index + 1} / {count}</span>
              <LightboxStep label="下一张" disabled={index === count - 1} onClick={() => go(1)}>
                <ChevronRightIcon className="size-5" />
              </LightboxStep>
            </div>
          )}
        </DialogPrimitive.Popup>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

function LightboxStep({ label, disabled, onClick, children }: {
  label: string;
  disabled: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      disabled={disabled}
      onClick={onClick}
      className="hover:bg-muted cursor-pointer rounded-md p-1.5 disabled:cursor-default disabled:opacity-40 disabled:hover:bg-transparent"
    >
      {children}
    </button>
  );
}

/**
 * 在 document 上听左右键，只在打开时翻页。焦点要等打开动画后才移进弹窗，挂在弹窗上的处理会漏掉
 * 刚打开就按下的键；监听随组件挂载一次注册（图片组一直挂着灯箱），不随开关挂卸，免得在
 * 「已经显示、effect 还没跑」的间隙里丢键。开关与回调每次渲染经 ref 取最新值。
 */
function useArrowKeys(active: boolean, go: (step: -1 | 1) => void) {
  const latest = useRef({ active, go });
  latest.current = { active, go };
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (!latest.current.active) return;
      if (event.key === "ArrowLeft") latest.current.go(-1);
      else if (event.key === "ArrowRight") latest.current.go(1);
    };
    // 捕获阶段：焦点所在的控件（如 base-ui 按钮）处理按键时可能阻止冒泡。
    document.addEventListener("keydown", onKeyDown, true);
    return () => document.removeEventListener("keydown", onKeyDown, true);
  }, []);
}

/** 预先加载当前图片前后各一张：浏览器缓存命中后，翻页时新图立即可显示。 */
function usePreloadNeighbors(images: readonly LightboxImage[], index: number | null) {
  useEffect(() => {
    if (index === null) return;
    for (const neighbor of [images[index - 1], images[index + 1]]) {
      if (neighbor?.src) new window.Image().src = neighbor.src;
    }
  }, [images, index]);
}

/** 点图片也关闭；用普通点击而不是渲染成 Close 按钮，图片在无障碍树里仍是图片。 */
function LightboxPicture({ image, onClose }: { image: LightboxImage; onClose: () => void }) {
  const [state, setState] = useState<"loading" | "loaded" | "error">(image.src ? "loading" : "error");
  if (state === "error") {
    return (
      <div className="text-background/80 relative flex flex-col items-center gap-2 text-sm" role="alert">
        <ImageOffIcon className="size-8" aria-hidden="true" />
        图片无法显示
      </div>
    );
  }
  return (
    <>
      {state === "loading" && <ImageIcon className="text-background/60 absolute size-8 animate-pulse" aria-hidden="true" />}
      <img
        src={image.src}
        alt={image.alt}
        className={cn("relative max-h-[90vh] max-w-[90vw] cursor-zoom-out object-contain", state === "loading" && "invisible")}
        onClick={onClose}
        onLoad={() => setState("loaded")}
        onError={() => setState("error")}
      />
    </>
  );
}

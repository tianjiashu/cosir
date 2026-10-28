"use client";

import { useState, type ReactElement } from "react";
import { Dialog as DialogPrimitive } from "@base-ui/react/dialog";
import { ImageIcon, ImageOffIcon, XIcon } from "lucide-react";

import { cn } from "@/lib/utils";

type ImageLightboxProps = {
  src: string;
  alt: string;
  /** 触发元素（缩略图）；不传时由调用方经 `open` / `onOpenChange` 受控打开。 */
  trigger?: ReactElement;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
};

/**
 * 图片放大预览：消息里的图片与输入框附件共用同一种全屏暗色灯箱。
 *
 * 焦点锁定、Esc 关闭、焦点归还、背景不可交互与滚动锁定都交给 base-ui Dialog，不在这里手写；
 * 点遮罩或点图片都关闭（看图灯箱的常见习惯）。大图加载中与解码失败各有占位。
 */
export function ImageLightbox({ src, alt, trigger, open, onOpenChange }: ImageLightboxProps) {
  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      {trigger && <DialogPrimitive.Trigger nativeButton={false} render={trigger} />}
      <DialogPrimitive.Portal>
        <DialogPrimitive.Backdrop className="fixed inset-0 z-50 bg-black/80 duration-200 data-open:animate-in data-open:fade-in-0 data-closed:animate-out data-closed:fade-out-0" />
        <DialogPrimitive.Popup
          aria-label={alt}
          className="fixed inset-0 z-50 flex items-center justify-center p-8 outline-none duration-200 data-open:animate-in data-open:fade-in-0 data-open:zoom-in-95 data-closed:animate-out data-closed:fade-out-0 data-closed:zoom-out-95"
        >
          <DialogPrimitive.Title className="sr-only">{alt}</DialogPrimitive.Title>
          {/* Popup 铺满视口，点图片以外的空白处等同点遮罩。 */}
          <DialogPrimitive.Close
            nativeButton={false}
            render={<div className="absolute inset-0 cursor-zoom-out" aria-hidden="true" />}
          />
          <LightboxImage key={src} src={src} alt={alt} />
          <DialogPrimitive.Close
            aria-label="关闭图片预览"
            className="bg-background/80 text-muted-foreground hover:text-foreground absolute end-4 top-4 cursor-pointer rounded-md p-2"
          >
            <XIcon className="size-5" />
          </DialogPrimitive.Close>
        </DialogPrimitive.Popup>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

function LightboxImage({ src, alt }: { src: string; alt: string }) {
  const [state, setState] = useState<"loading" | "loaded" | "error">("loading");
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
      <DialogPrimitive.Close
        nativeButton={false}
        render={
          <img
            src={src}
            alt={alt}
            className={cn("relative max-h-[90vh] max-w-[90vw] cursor-zoom-out object-contain", state === "loading" && "invisible")}
            onLoad={() => setState("loaded")}
            onError={() => setState("error")}
          />
        }
      />
    </>
  );
}

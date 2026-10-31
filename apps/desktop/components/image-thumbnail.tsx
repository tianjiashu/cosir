"use client";

import type { ReactNode } from "react";
import { AlertCircleIcon, ImageIcon, Loader2Icon, XIcon } from "lucide-react";

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

type ImageThumbnailProps = {
  /** 可显示的地址；尚未就绪（对象 URL 未建好、locator 解析不到）时为空，显示占位图标。 */
  src?: string | null;
  name: string;
  /** 上传状态遮罩，只有 Task 输入框的图片会经历上传；展示场景（新对话页、工具结果）恒为就绪。 */
  state?: "ready" | "uploading" | "error";
  errorMessage?: string;
  onRemove?: () => void;
  /** 请求放大预览；灯箱由图片组（`useImageGallery`）持有，不传时缩略图不可点。 */
  onOpen?: () => void;
};

/**
 * 方形裁切缩略图：输入框待发送图片与工具结果（`read_image`）共用外观、状态遮罩与名称提示。
 *
 * 只负责呈现与转发移除意图；图片从哪来、何时上传、移除后草稿如何变化由调用方负责。
 * 要完整显示整张图（用户消息里的图片、网页截图）用 `components/image.tsx`，不用这里。
 */
export function ImageThumbnail({ src, name, state = "ready", errorMessage, onRemove, onOpen }: ImageThumbnailProps) {
  const statusLabel = state === "uploading" ? "，上传中" : state === "error" ? "，上传失败" : "";
  const picture = src && <img src={src} alt={name} loading="lazy" decoding="async" className="block size-full object-cover" />;
  return (
    <Tooltip>
      <TooltipTrigger
        render={<div />}
        data-slot="image-thumbnail"
        data-state={state}
        delay={500}
        className={cn(
          "bg-muted relative size-16 shrink-0 overflow-hidden rounded-xl border animate-in fade-in-0 zoom-in-95 duration-200 motion-reduce:animate-none",
          state === "error" && "border-destructive/60",
        )}
      >
        {picture && onOpen ? (
          <button
            type="button"
            aria-label={`预览图片 ${name}${statusLabel}`}
            onClick={onOpen}
            className="focus-visible:ring-ring absolute inset-0 cursor-zoom-in outline-none focus-visible:ring-2 focus-visible:ring-inset"
          >
            {picture}
          </button>
        ) : picture || (
          <ImageIcon className="text-muted-foreground absolute inset-0 m-auto size-6" aria-label={`${name}${statusLabel}`} />
        )}
        {state === "uploading" && (
          <StatusOverlay><Loader2Icon className="text-muted-foreground size-4 animate-spin" /></StatusOverlay>
        )}
        {state === "error" && (
          <StatusOverlay><AlertCircleIcon className="text-destructive size-4" /></StatusOverlay>
        )}
        {onRemove && (
          <button
            type="button"
            className="bg-foreground/65 text-background hover:bg-foreground/80 focus-ring absolute top-1 right-1 rounded-full p-0.5"
            aria-label={`移除附件 ${name}`}
            onClick={onRemove}
          >
            <XIcon className="size-3.5" />
          </button>
        )}
      </TooltipTrigger>
      <TooltipContent side="top" className="break-all">
        {name}
        {errorMessage && <p>{errorMessage}</p>}
      </TooltipContent>
    </Tooltip>
  );
}

function StatusOverlay({ children }: { children: ReactNode }) {
  return (
    <div aria-hidden="true" className="bg-background/60 pointer-events-none absolute inset-0 flex items-center justify-center backdrop-blur-[2px]">
      {children}
    </div>
  );
}

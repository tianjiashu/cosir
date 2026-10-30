"use client";

import type { ReactNode } from "react";
import { AlertCircleIcon, ImageIcon, Loader2Icon, XIcon } from "lucide-react";

import { ImageLightbox } from "@/components/ui/image-lightbox";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

type ImageAttachmentCardProps = {
  /** 可显示的地址；尚未就绪（对象 URL 未建好、locator 解析不到）时为空，显示占位图标。 */
  src?: string | null;
  name: string;
  /** 只有 Task 输入框的图片会经历上传；新对话页的图片在提交时才上传，恒为就绪。 */
  state?: "ready" | "uploading" | "error";
  errorMessage?: string;
  onRemove?: () => void;
};

/**
 * 输入框里待发送图片的唯一缩略图：新对话页与 Task 输入框共用外观、状态遮罩、名称提示与放大预览。
 *
 * 只负责呈现与转发移除意图；图片从哪来、何时上传、移除后草稿如何变化由调用方（各自的草稿状态）负责。
 */
export function ImageAttachmentCard({ src, name, state = "ready", errorMessage, onRemove }: ImageAttachmentCardProps) {
  const statusLabel = state === "uploading" ? "，上传中" : state === "error" ? "，上传失败" : "";
  return (
    <Tooltip>
      <TooltipTrigger
        render={<div />}
        data-slot="image-attachment"
        data-state={state}
        delay={500}
        className={cn(
          "bg-muted relative size-16 shrink-0 overflow-hidden rounded-xl border animate-in fade-in-0 zoom-in-95 duration-200 motion-reduce:animate-none",
          state === "error" && "border-destructive/60",
        )}
      >
        {src ? (
          <ImageLightbox
            src={src}
            alt={name}
            trigger={
              <div
                role="button"
                tabIndex={0}
                aria-label={`预览图片 ${name}${statusLabel}`}
                className="focus-visible:ring-ring absolute inset-0 cursor-zoom-in outline-none focus-visible:ring-2 focus-visible:ring-inset"
              >
                <img src={src} alt={name} className="block size-full object-cover" />
              </div>
            }
          />
        ) : (
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

/** 输入框里待发送图片的横向条；没有图片时不占高度。 */
export function ImageAttachmentStrip({ children }: { children: ReactNode }) {
  return (
    <div
      data-slot="image-attachment-strip"
      aria-label="待发送图片"
      className="flex h-16 min-w-0 shrink-0 flex-nowrap items-center gap-2 overflow-x-auto overflow-y-hidden px-2 empty:hidden"
    >
      {children}
    </div>
  );
}

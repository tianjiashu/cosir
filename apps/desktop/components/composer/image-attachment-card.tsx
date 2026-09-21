"use client";

import { AlertCircleIcon, ImageIcon, Loader2Icon, XIcon } from "lucide-react";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

type ImageAttachmentCardProps = {
  src?: string | null;
  name: string;
  loading?: boolean;
  error?: boolean;
  onRemove?: () => void;
  onPreview?: () => void;
  remove?: ReactNode;
  className?: string;
};

/** Shared image attachment surface used by the new and existing composers. */
export function ImageAttachmentCard({
  src,
  name,
  loading = false,
  error = false,
  onRemove,
  onPreview,
  remove,
  className,
}: ImageAttachmentCardProps) {
  const preview = src ? (
    <img
      src={src}
      alt={name}
      className="block h-full max-h-full max-w-full w-full object-cover"
    />
  ) : (
    <ImageIcon className="text-muted-foreground absolute inset-0 m-auto size-6" />
  );

  return (
    <div
      className={cn(
        "relative h-16 max-h-16 min-h-16 w-16 max-w-16 min-w-16 shrink-0 overflow-hidden rounded-xl border bg-muted shadow-sm",
        className,
      )}
    >
      {onPreview ? (
        <button
          type="button"
          className="absolute inset-0 size-full cursor-zoom-in outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
          aria-label={`预览图片 ${name}`}
          onClick={onPreview}
        >
          {preview}
        </button>
      ) : (
        preview
      )}
      {loading && (
        <div className="bg-background/60 absolute inset-0 flex items-center justify-center backdrop-blur-[2px]">
          <Loader2Icon className="text-muted-foreground size-5 animate-spin" />
        </div>
      )}
      {error && !loading && (
        <div className="bg-background/70 absolute inset-0 flex items-center justify-center backdrop-blur-[2px]">
          <AlertCircleIcon className="text-destructive size-5" />
        </div>
      )}
      {remove ?? (onRemove && (
        <button
          type="button"
          className="absolute right-1 top-1 rounded-full bg-black/65 p-0.5 text-white hover:bg-black/80"
          aria-label={`移除附件 ${name}`}
          onClick={onRemove}
        >
          <XIcon className="size-3.5" />
        </button>
      ))}
    </div>
  );
}

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
  remove,
  className,
}: ImageAttachmentCardProps) {
  return (
    <div className={cn("relative size-20 shrink-0 overflow-hidden rounded-xl border bg-muted shadow-sm", className)}>
      {src ? (
        <img src={src} alt={name} className="size-full object-cover" />
      ) : (
        <ImageIcon className="text-muted-foreground absolute inset-0 m-auto size-6" />
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

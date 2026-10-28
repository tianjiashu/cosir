"use client";

import { ImageIcon, XIcon } from "lucide-react";

import { cn } from "@/lib/utils";

type ImageAttachmentCardProps = {
  src?: string | null;
  name: string;
  onRemove?: () => void;
  onPreview?: () => void;
  className?: string;
};

/** Shared image attachment surface used by the new and existing composers. */
export function ImageAttachmentCard({
  src,
  name,
  onRemove,
  onPreview,
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
    </div>
  );
}

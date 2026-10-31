import type { ReactNode } from "react";

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

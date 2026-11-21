import type { Ref } from "react";
import { ImageOffIcon, XIcon } from "lucide-react";

import { ImagePicture } from "@/components/image/image-picture";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

/** 待发图片用于确认整张素材：完整显示、按需移除；附件与预览组的事实均由调用方持有。 */
export function ComposerImageCard({ src, name, onOpen, onRemove, ref }: {
  src: string | undefined;
  name: string;
  onOpen: () => void;
  onRemove: () => void;
  ref: Ref<HTMLButtonElement>;
}) {
  return (
    <li data-slot="composer-image-card" className="group/composer-image relative size-28 shrink-0">
      <Tooltip>
        <TooltipTrigger render={
          <button
            ref={ref}
            type="button"
            aria-label={`预览图片 ${name}`}
            onClick={onOpen}
            className="bg-muted/30 border-border/70 focus-visible:ring-ring relative block size-full cursor-zoom-in overflow-hidden rounded-xl border outline-none focus-visible:ring-2 focus-visible:ring-inset"
          >
            <ImagePicture
              src={src}
              alt={name}
              className="block size-full object-contain"
              fallback={
                <span role="img" aria-label={`${name}，无法预览`} className="text-muted-foreground absolute inset-0 flex flex-col items-center justify-center gap-1 text-xs">
                  <ImageOffIcon className="size-5" aria-hidden="true" />
                  无法预览
                </span>
              }
            />
          </button>
        } />
        <TooltipContent>{name}</TooltipContent>
      </Tooltip>
      <button
        type="button"
        aria-label={`移除附件 ${name}`}
        onClick={onRemove}
        className="bg-background text-foreground border-border focus-ring pointer-events-none absolute top-1 right-1 flex size-6 items-center justify-center rounded-full border opacity-0 shadow-sm transition-opacity group-hover/composer-image:pointer-events-auto group-hover/composer-image:opacity-100 group-focus-within/composer-image:pointer-events-auto group-focus-within/composer-image:opacity-100 motion-reduce:transition-none [@media(hover:none)]:pointer-events-auto [@media(hover:none)]:opacity-100 [@media(pointer:coarse)]:size-11"
      >
        <XIcon className="size-3.5" />
      </button>
    </li>
  );
}

import type { ComponentProps } from "react";

import { cn } from "@/lib/utils";

/** 按键标签（shadcn Kbd）。按键文字统一经 `lib/keyboard/shortcuts.ts` 的 `formatKeys` / `shortcutLabel` 生成，不手写平台符号。 */
export function Kbd({ className, ...props }: ComponentProps<"kbd">) {
  return (
    <kbd
      data-slot="kbd"
      className={cn(
        "bg-muted text-muted-foreground pointer-events-none inline-flex h-5 w-fit min-w-5 items-center justify-center rounded-sm px-1 font-sans text-xs font-medium select-none",
        className,
      )}
      {...props}
    />
  );
}

import type { ComponentProps } from "react";

import { cn } from "@/lib/utils";

export const COMPOSER_SURFACE_CLASS_NAME =
  "border-border/60 bg-card focus-within:ring-ring/20 focus-within:ring-2 flex min-w-0 w-full flex-col gap-2 rounded-3xl border p-2 shadow-sm";

export function ComposerSurface({ className, ...props }: ComponentProps<"div">) {
  return <div className={cn(COMPOSER_SURFACE_CLASS_NAME, className)} {...props} />;
}

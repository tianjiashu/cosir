import type { ComponentProps } from "react";

import { cn } from "@/lib/utils";

const SURFACE_CLASS_NAME =
  "border-border/60 data-[dragging=true]:border-ring focus-within:border-border dark:border-muted-foreground/15 dark:focus-within:border-muted-foreground/30 flex w-full cursor-text flex-col gap-2 rounded-(--composer-radius) border bg-(--composer-bg) p-(--composer-padding) transition-[border-color] data-[dragging=true]:border-dashed data-[dragging=true]:bg-[color-mix(in_oklab,var(--color-accent)_50%,var(--color-background))]";

export function ComposerSurface({ className, ...props }: ComponentProps<"div">) {
  return <div className={cn(SURFACE_CLASS_NAME, className)} {...props} />;
}

import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"

import { cn } from "@/lib/utils"

/**
 * 状态 / 标签徽标。tone 表达语义（成功、失败、进行中…），variant 只决定
 * 浅底（soft）、描边（outline）或浅底加描边（tint），shape 区分胶囊（状态）
 * 与方角（代码类标签，如 exit code、shell）。内部文字需要省略号截断时，
 * 调用方自行包一层 `<span className="truncate">`（inline-flex 本身不截断）。
 * 业务侧的「状态 → tone」映射留在各自模块，这里不认识任何业务状态。
 */
const badgeVariants = cva(
  "inline-flex shrink-0 items-center gap-1 border font-medium whitespace-nowrap [&_svg]:pointer-events-none [&_svg]:shrink-0",
  {
    variants: {
      tone: {
        neutral: "",
        primary: "",
        info: "",
        success: "",
        warning: "",
        destructive: "",
        skill: "",
      },
      variant: {
        soft: "border-transparent",
        outline: "bg-transparent",
        tint: "",
      },
      shape: {
        pill: "rounded-full",
        tag: "rounded-sm",
      },
      size: {
        sm: "h-4.5 px-1.5 text-2xs [&_svg:not([class*='size-'])]:size-3",
        default: "h-5 px-2 text-xs [&_svg:not([class*='size-'])]:size-3.5",
      },
    },
    compoundVariants: [
      { variant: "soft", tone: "neutral", className: "bg-muted text-muted-foreground" },
      { variant: "soft", tone: "primary", className: "bg-primary/10 text-primary" },
      { variant: "soft", tone: "info", className: "bg-info/10 text-info" },
      { variant: "soft", tone: "success", className: "bg-success/10 text-success" },
      { variant: "soft", tone: "warning", className: "bg-warning/10 text-warning" },
      { variant: "soft", tone: "destructive", className: "bg-destructive/10 text-destructive" },
      { variant: "soft", tone: "skill", className: "bg-(--skill-background) text-(--skill-foreground)" },
      { variant: "outline", tone: "neutral", className: "border-border text-muted-foreground" },
      { variant: "outline", tone: "primary", className: "border-primary/30 text-primary" },
      { variant: "outline", tone: "info", className: "border-info/30 text-info" },
      { variant: "outline", tone: "success", className: "border-success/30 text-success" },
      { variant: "outline", tone: "warning", className: "border-warning/30 text-warning" },
      { variant: "outline", tone: "destructive", className: "border-destructive/30 text-destructive" },
      { variant: "outline", tone: "skill", className: "border-(--skill-border) text-(--skill-foreground)" },
      { variant: "tint", tone: "neutral", className: "border-border bg-muted text-muted-foreground" },
      { variant: "tint", tone: "primary", className: "border-primary/30 bg-primary/10 text-primary" },
      { variant: "tint", tone: "info", className: "border-info/30 bg-info/10 text-info" },
      { variant: "tint", tone: "success", className: "border-success/30 bg-success/10 text-success" },
      { variant: "tint", tone: "warning", className: "border-warning/30 bg-warning/10 text-warning" },
      { variant: "tint", tone: "destructive", className: "border-destructive/30 bg-destructive/10 text-destructive" },
      { variant: "tint", tone: "skill", className: "border-(--skill-border) bg-(--skill-background) text-(--skill-foreground)" },
    ],
    defaultVariants: { tone: "neutral", variant: "soft", shape: "pill", size: "default" },
  }
)

type BadgeProps = React.ComponentProps<"span"> & VariantProps<typeof badgeVariants>

function Badge({ className, tone, variant, shape, size, ...props }: BadgeProps) {
  return (
    <span
      data-slot="badge"
      className={cn(badgeVariants({ tone, variant, shape, size }), className)}
      {...props}
    />
  )
}

type BadgeTone = NonNullable<VariantProps<typeof badgeVariants>["tone"]>

export { Badge,  type BadgeTone }

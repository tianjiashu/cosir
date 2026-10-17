import * as React from "react"

import { cn } from "@/lib/utils"

/**
 * 配置页与面板中的分区容器。圆角固定为 xl、边框与底色走 token，
 * 不再在各页面用不同透明度的 bg-card/xx 拼卡片。
 */
function Card({ className, ...props }: React.ComponentProps<"section">) {
  return (
    <section
      data-slot="card"
      className={cn("rounded-xl border bg-card text-card-foreground shadow-xs", className)}
      {...props}
    />
  )
}

/** 卡片头部：标题区在左，操作区通过 `CardAction` 放在右侧，自带分隔线。 */
function CardHeader({ className, ...props }: React.ComponentProps<"header">) {
  return (
    <header
      data-slot="card-header"
      className={cn("flex items-start justify-between gap-3 border-b px-5 py-4", className)}
      {...props}
    />
  )
}

function CardTitle({ className, ...props }: React.ComponentProps<"h2">) {
  return <h2 data-slot="card-title" className={cn("font-medium", className)} {...props} />
}

function CardDescription({ className, ...props }: React.ComponentProps<"p">) {
  return <p data-slot="card-description" className={cn("text-muted-foreground text-sm", className)} {...props} />
}

function CardAction({ className, ...props }: React.ComponentProps<"div">) {
  return <div data-slot="card-action" className={cn("flex shrink-0 items-center gap-2", className)} {...props} />
}

function CardContent({ className, ...props }: React.ComponentProps<"div">) {
  return <div data-slot="card-content" className={cn("p-5", className)} {...props} />
}

export { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle }

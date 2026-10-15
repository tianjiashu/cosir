import * as React from "react"

import { Alert } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Spinner } from "@/components/ui/spinner"
import { cn } from "@/lib/utils"

/**
 * 区块级占位状态：加载中、空、出错。
 *
 * 页面和面板在数据未就绪时统一用这三个组件，保证间距、字号与读屏语义一致；
 * `compact` 用于侧栏、弹层等窄区域。组件只负责呈现，不发请求也不持有重试状态。
 */

type StateSize = "default" | "compact"

const PADDING: Record<StateSize, string> = {
  default: "py-10",
  compact: "py-3",
}

function LoadingState({
  label = "正在加载…",
  size = "default",
  className,
}: {
  label?: string
  size?: StateSize
  className?: string
}) {
  return (
    <div
      role="status"
      data-slot="loading-state"
      className={cn("text-muted-foreground flex items-center justify-center gap-2 text-sm", PADDING[size], size === "compact" && "justify-start text-xs", className)}
    >
      <Spinner label={null} className={size === "compact" ? "size-3.5" : undefined} />
      <span>{label}</span>
    </div>
  )
}

function EmptyState({
  icon,
  title,
  description,
  action,
  size = "default",
  className,
}: {
  icon?: React.ReactNode
  title: React.ReactNode
  description?: React.ReactNode
  action?: React.ReactNode
  size?: StateSize
  className?: string
}) {
  if (size === "compact") {
    return (
      <div data-slot="empty-state" className={cn("text-muted-foreground space-y-2 text-xs", PADDING.compact, className)}>
        <p>{title}</p>
        {description && <p>{description}</p>}
        {action}
      </div>
    )
  }
  return (
    <div
      data-slot="empty-state"
      className={cn("flex flex-col items-center justify-center gap-2 rounded-xl border border-dashed px-6 text-center", PADDING.default, className)}
    >
      {icon && <div className="text-muted-foreground [&_svg:not([class*='size-'])]:size-6">{icon}</div>}
      <p className="text-sm font-medium">{title}</p>
      {description && <p className="text-muted-foreground max-w-md text-sm">{description}</p>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  )
}

function ErrorState({
  title,
  message,
  onRetry,
  retryLabel = "重试",
  className,
}: {
  title?: React.ReactNode
  message: React.ReactNode
  onRetry?: () => void
  retryLabel?: string
  className?: string
}) {
  return (
    <Alert
      tone="destructive"
      title={title}
      className={className}
      action={onRetry ? <Button type="button" variant="outline" size="sm" onClick={onRetry}>{retryLabel}</Button> : undefined}
    >
      {message}
    </Alert>
  )
}

export { EmptyState, ErrorState, LoadingState }

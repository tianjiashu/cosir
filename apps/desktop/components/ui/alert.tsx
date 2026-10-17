import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { CircleAlertIcon, CircleCheckIcon, InfoIcon, TriangleAlertIcon } from "lucide-react"

import { cn } from "@/lib/utils"

const alertVariants = cva(
  "flex items-start gap-2 rounded-lg border px-3 py-2 text-sm [&>svg]:mt-0.5 [&>svg]:size-4 [&>svg]:shrink-0",
  {
    variants: {
      tone: {
        neutral: "border-border bg-muted/40 text-foreground [&>svg]:text-muted-foreground",
        info: "border-info/30 bg-info/10 text-info",
        success: "border-success/30 bg-success/10 text-success",
        warning: "border-warning/30 bg-warning/10 text-warning",
        destructive: "border-destructive/30 bg-destructive/10 text-destructive",
      },
    },
    defaultVariants: { tone: "neutral" },
  }
)

type AlertTone = NonNullable<VariantProps<typeof alertVariants>["tone"]>

const TONE_ICON: Record<AlertTone, React.ComponentType<{ "aria-hidden"?: boolean }> | null> = {
  neutral: null,
  info: InfoIcon,
  success: CircleCheckIcon,
  warning: TriangleAlertIcon,
  destructive: CircleAlertIcon,
}

type AlertProps = Omit<React.ComponentProps<"div">, "title"> &
  VariantProps<typeof alertVariants> & {
    title?: React.ReactNode
    /** 行尾操作（重试、撤销等）。 */
    action?: React.ReactNode
    /** 默认按 tone 选图标；传 `false` 隐藏，传节点则替换。 */
    icon?: React.ReactNode | false
  }

/**
 * 页面内的状态提示条（错误、成功、警告、说明）。
 *
 * 失败与警告默认 `role="alert"` 让读屏立即播报，其余为 `role="status"`；
 * 调用方显式传入 `role` 时以调用方为准。只负责呈现，不持有消失计时或重试状态。
 */
function Alert({ tone, title, action, icon, role, className, children, ...props }: AlertProps) {
  const resolvedTone: AlertTone = tone ?? "neutral"
  const DefaultIcon = TONE_ICON[resolvedTone]
  const iconNode = icon === false ? null : icon ?? (DefaultIcon ? <DefaultIcon aria-hidden /> : null)
  const urgent = resolvedTone === "destructive" || resolvedTone === "warning"
  return (
    <div
      data-slot="alert"
      role={role ?? (urgent ? "alert" : "status")}
      className={cn(alertVariants({ tone: resolvedTone }), className)}
      {...props}
    >
      {iconNode}
      <div className="min-w-0 flex-1 space-y-0.5 break-words">
        {title && <p className="font-medium">{title}</p>}
        {children && <div className={cn(title && "opacity-90")}>{children}</div>}
      </div>
      {action && <div className="-my-0.5 shrink-0">{action}</div>}
    </div>
  )
}

export { Alert,   }

import * as React from "react"

import { cn } from "@/lib/utils"

/**
 * 复选框：原生 input + `accent-color`。
 *
 * 不自绘是为了保留 `label` 包裹点击、表单提交与读屏语义；勾选色取 primary，
 * 深色模式下由 `color-scheme` 让系统绘制深色底。
 */
function Checkbox({ className, ...props }: Omit<React.ComponentProps<"input">, "type">) {
  return (
    <input
      type="checkbox"
      data-slot="checkbox"
      className={cn(
        "accent-primary size-4 shrink-0 cursor-pointer rounded-sm focus-ring disabled:cursor-not-allowed disabled:opacity-50",
        className
      )}
      {...props}
    />
  )
}

export { Checkbox }

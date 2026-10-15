import * as React from "react"
import { Loader2Icon } from "lucide-react"

import { cn } from "@/lib/utils"

type SpinnerProps = Omit<React.ComponentProps<typeof Loader2Icon>, "role"> & {
  /** 读屏播报文字；放在已有可见文字旁边时传 `null` 改为装饰性图标。 */
  label?: string | null
}

/** 统一的加载指示图标；减少动态效果由 globals.css 的全局规则处理。 */
function Spinner({ className, label = "加载中", ...props }: SpinnerProps) {
  return (
    <Loader2Icon
      data-slot="spinner"
      role={label ? "status" : undefined}
      aria-label={label ?? undefined}
      aria-hidden={label ? undefined : true}
      className={cn("size-4 animate-spin", className)}
      {...props}
    />
  )
}

export { Spinner }

import * as React from "react"
import { ChevronDownIcon } from "lucide-react"

import { cn } from "@/lib/utils"

const SIZE_CLASS = {
  sm: "h-7 pl-2 pr-7 text-xs",
  default: "h-8 pl-2.5 pr-8 text-sm",
} as const

type NativeSelectProps = Omit<React.ComponentProps<"select">, "size"> & {
  size?: keyof typeof SIZE_CLASS
  /** 外层容器的布局类（宽度、外边距）；`className` 作用在 select 本身。 */
  wrapperClassName?: string
}

/**
 * 原生 `<select>` 的统一外观。
 *
 * 保留原生控件是刻意的：表单语义、键盘行为、`label` 关联和 E2E 的
 * `selectOption` 都不需要额外适配；下拉列表由系统绘制，并随 `color-scheme`
 * 跟随深浅色。需要搜索、分组或富内容时再用 Popover + Command。
 */
function NativeSelect({ className, wrapperClassName, size = "default", ...props }: NativeSelectProps) {
  return (
    <div
      data-slot="native-select-wrapper"
      className={cn("relative w-full min-w-0 has-[select:disabled]:opacity-50", wrapperClassName)}
    >
      <select
        data-slot="native-select"
        className={cn(
          "w-full min-w-0 cursor-pointer appearance-none truncate rounded-lg border border-input bg-transparent text-foreground transition-colors focus-ring disabled:cursor-not-allowed aria-invalid:border-destructive dark:bg-input/30",
          SIZE_CLASS[size],
          className
        )}
        {...props}
      />
      <ChevronDownIcon
        aria-hidden="true"
        className="text-muted-foreground pointer-events-none absolute top-1/2 right-2.5 size-3.5 -translate-y-1/2"
      />
    </div>
  )
}

export { NativeSelect }

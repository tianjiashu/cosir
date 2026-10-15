import * as React from "react"

import { cn } from "@/lib/utils"

type SegmentedOption<T extends string> = {
  value: T
  label: React.ReactNode
}

type SegmentedControlProps<T extends string> = {
  value: T
  options: readonly SegmentedOption<T>[]
  onValueChange: (value: T) => void
  "aria-label": string
  size?: "sm" | "default"
  className?: string
}

const SIZE_CLASS = {
  sm: "h-7 text-xs",
  default: "h-8 text-sm",
} as const

/**
 * 互斥视图切换（表单 / 画布、表单 / JSON 等）。
 *
 * 以一组 `aria-pressed` 按钮表达当前项；滑块位置只由 `value` 计算，组件不持有状态。
 * 视觉沿用 muted 轨道 + background 滑块，与 Tabs 一致，不使用独立的品牌蓝。
 */
function SegmentedControl<T extends string>({
  value,
  options,
  onValueChange,
  size = "default",
  className,
  ...props
}: SegmentedControlProps<T>) {
  const selectedIndex = Math.max(0, options.findIndex((option) => option.value === value))
  return (
    <div
      role="group"
      aria-label={props["aria-label"]}
      data-slot="segmented-control"
      className={cn("bg-muted relative isolate inline-grid auto-cols-fr grid-flow-col rounded-lg p-0.5", SIZE_CLASS[size], className)}
    >
      <span
        aria-hidden="true"
        className="bg-background absolute inset-y-0.5 left-0.5 rounded-md shadow-sm transition-transform duration-200 motion-reduce:transition-none dark:bg-input/60"
        style={{
          width: `calc((100% - 0.25rem) / ${options.length})`,
          transform: `translateX(${selectedIndex * 100}%)`,
        }}
      />
      {options.map((option) => {
        const selected = option.value === value
        return (
          <button
            key={option.value}
            type="button"
            aria-pressed={selected}
            className={cn(
              "relative z-10 rounded-md px-3 font-medium whitespace-nowrap transition-colors focus-ring",
              selected ? "text-foreground" : "text-muted-foreground hover:text-foreground"
            )}
            onClick={() => onValueChange(option.value)}
          >
            {option.label}
          </button>
        )
      })}
    </div>
  )
}

export { SegmentedControl, type SegmentedOption }

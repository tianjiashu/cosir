import type { CSSProperties } from "react"
import { Toaster as Sonner, type ToasterProps } from "sonner"

/**
 * 全局 toast 宿主，整个应用只挂载一次（App 根部）。
 *
 * 配色走 popover / border token；`theme="system"` 与 lib/theme/system-theme.ts
 * 一样跟随系统外观。业务代码不直接调用 sonner，统一经 lib/notify.ts。
 */
function Toaster(props: ToasterProps) {
  return (
    <Sonner
      theme="system"
      position="bottom-right"
      closeButton
      className="toaster group"
      style={
        {
          "--normal-bg": "var(--popover)",
          "--normal-text": "var(--popover-foreground)",
          "--normal-border": "var(--border)",
          "--border-radius": "var(--radius)",
        } as CSSProperties
      }
      {...props}
    />
  )
}

export { Toaster }

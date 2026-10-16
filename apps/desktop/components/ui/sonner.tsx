import type { CSSProperties } from "react"
import { Toaster as Sonner, type ToasterProps } from "sonner"
import { useThemeState } from "@/lib/theme/theme-preference"

/**
 * 全局 toast 宿主，整个应用只挂载一次（App 根部）。
 *
 * 配色走 popover / border token；主题显式取应用的生效主题，避免覆盖系统设置后 toast
 * 与页面主题不一致。业务代码不直接调用 sonner，统一经 lib/notify.ts。
 */
function Toaster(props: ToasterProps) {
  const { resolvedTheme } = useThemeState()
  return (
    <Sonner
      theme={resolvedTheme}
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

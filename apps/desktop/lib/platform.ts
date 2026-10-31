import { desktopHost } from "@/lib/desktop-host";

/**
 * 运行平台与标题栏形态的唯一判断处。
 *
 * 标题栏形态由宿主的窗口配置决定（`electron/main/window.ts`）：macOS 隐藏标题栏、保留原生红绿灯；
 * Windows 隐藏标题栏、由系统窗口按钮叠在右上角；其余情况（浏览器开发、E2E、Linux）沿用原生标题栏。
 * 这里按同一规则推断，改窗口配置时必须同步这里。业务组件不直接读平台，只用 globals.css 按
 * `data-titlebar` 给出的左右安全区变量让位。
 */
export const isMac = typeof navigator !== "undefined" && /mac|iphone|ipad/i.test(navigator.platform || navigator.userAgent);

export type TitlebarMode = "mac-overlay" | "windows-overlay" | "native";

export const titlebarMode: TitlebarMode = desktopHost?.platform === "darwin"
  ? "mac-overlay"
  : desktopHost?.platform === "win32"
    ? "windows-overlay"
    : "native";

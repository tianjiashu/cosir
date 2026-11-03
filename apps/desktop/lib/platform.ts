import type { HostPlatform } from "@/electron/shared/host-contract";
import { desktopHost } from "@/lib/desktop-host";

/**
 * 运行平台与标题栏形态的唯一判断处：需要按平台分支的代码只经本模块读平台。
 *
 * 平台以宿主报告的为准；浏览器开发与 E2E 没有宿主，才按 `navigator` 推断。只有一个来源，是为了
 * 键位、粘贴、ConPTY 选项与标题栏在同一次运行里给出一致的答案。
 *
 * 标题栏形态由宿主的窗口配置决定（`electron/main/window.ts`）：macOS 隐藏标题栏、保留原生红绿灯；
 * Windows 隐藏标题栏、由系统窗口按钮叠在右上角；没有宿主（浏览器开发、E2E）或 Linux 沿用原生标题栏。
 * 这里按同一规则推断，改窗口配置时必须同步这里。页面布局不按平台分支，只用 globals.css 按
 * `data-titlebar` 给出的左右安全区变量让位。
 */
function inferPlatform(): HostPlatform {
  if (typeof navigator === "undefined") return "linux";
  const name = navigator.platform || navigator.userAgent;
  if (/mac|iphone|ipad/i.test(name)) return "darwin";
  if (/win/i.test(name)) return "win32";
  return "linux";
}

export const platform: HostPlatform = desktopHost?.platform ?? inferPlatform();

export const isMac = platform === "darwin";
export const isWindows = platform === "win32";

export type TitlebarMode = "mac-overlay" | "windows-overlay" | "native";

export const titlebarMode: TitlebarMode = desktopHost === null
  ? "native"
  : isMac
    ? "mac-overlay"
    : isWindows
      ? "windows-overlay"
      : "native";

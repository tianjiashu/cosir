import { isTauri } from "@tauri-apps/api/core";

/**
 * 运行平台与标题栏形态的唯一判断处。
 *
 * 标题栏形态由窗口配置决定：`tauri.conf.json` 在 macOS 上把原生标题栏改为叠加式
 * （保留原生红绿灯），`tauri.windows.conf.json` 在 Windows 上去掉原生边框、由
 * `WindowControls` 自绘窗口按钮；其余情况（浏览器开发、E2E、Linux）沿用原生标题栏。
 * 这里按同一规则推断，改窗口配置时必须同步这里。业务组件不直接读平台，只用
 * globals.css 按 `data-titlebar` 给出的左右安全区变量让位。
 */
export const isMac = typeof navigator !== "undefined" && /mac|iphone|ipad/i.test(navigator.platform || navigator.userAgent);

const isWindows = typeof navigator !== "undefined" && /^win|windows/i.test(navigator.platform || navigator.userAgent);

export type TitlebarMode = "mac-overlay" | "windows-custom" | "native";

export const titlebarMode: TitlebarMode = !isTauri()
  ? "native"
  : isMac
    ? "mac-overlay"
    : isWindows
      ? "windows-custom"
      : "native";

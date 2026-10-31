import type { CosirHost } from "@/electron/shared/host-contract";

declare global {
  interface Window {
    /** 由 Electron preload 注入；浏览器开发与 Playwright E2E 中不存在。 */
    cosirHost?: CosirHost;
  }
}

/**
 * 渲染进程访问桌面宿主的唯一出口。
 *
 * 为 null 表示不在桌面宿主中运行（浏览器开发、E2E）：调用方据此走浏览器路径，例如后端地址取
 * `VITE_BACKEND_URL`。业务组件不得直接读 `window.cosirHost`。
 */
export const desktopHost: CosirHost | null = typeof window === "undefined" ? null : window.cosirHost ?? null;

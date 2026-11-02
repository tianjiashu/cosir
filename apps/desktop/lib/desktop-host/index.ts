import type { CosirHost } from "@/electron/shared/host-contract";
import { UserFacingError } from "@/lib/errors/user-facing-error";

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
 *
 * 宿主调用的失败在这里转成 `UserFacingError`：宿主控制面保证失败消息是给用户看的中文（`electron/main/ipc.ts`
 * 一处定义，失败日志也在那里落盘），界面经 `userErrorMessage` 就显示得出原因，不再退化成兜底文案。错误穿过
 * contextBridge 只可靠地保留 message，所以按来源在这里认，而不是靠错误类型或名字。
 */
export const desktopHost: CosirHost | null = typeof window === "undefined" || !window.cosirHost ? null : userFacingFailures(window.cosirHost);

/**
 * 逐个包装宿主方法：返回 Promise 的，失败换成 `UserFacingError`。复制成普通对象而不用 Proxy——contextBridge
 * 暴露的属性不可配置，Proxy 返回另一个函数会违反不变式。
 */
function userFacingFailures<T>(value: T): T {
  if (typeof value === "function") {
    const method = value as (...args: unknown[]) => unknown;
    return ((...args: unknown[]) => {
      const result = method(...args);
      // 按 thenable 认：穿过 contextBridge 的 Promise 不保证是本世界的 Promise 实例。
      return isThenable(result) ? Promise.resolve(result).catch(toUserFacing) : result;
    }) as T;
  }
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, userFacingFailures(item)])) as T;
  }
  return value;
}

function toUserFacing(error: unknown): never {
  throw error instanceof Error ? new UserFacingError(error.message) : error;
}

function isThenable(value: unknown): value is PromiseLike<unknown> {
  return value !== null && typeof value === "object" && typeof (value as { then?: unknown }).then === "function";
}

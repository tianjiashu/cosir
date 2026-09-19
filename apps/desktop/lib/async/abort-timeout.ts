/**
 * 超时中止工具：统一替换散落各处的
 * `new AbortController()` + `setTimeout(() => controller.abort())` + `finally clearTimeout`
 * 样板，并统一使用全局 `setTimeout`/`clearTimeout`（避免 globalThis/window 混用）。
 */

export type TimeoutAbort = {
  /** 底层 AbortController，供外部主动 abort 或读取 controller.signal。 */
  controller: AbortController;
  /** 超时触发的 AbortSignal，直接传给 fetch 等请求。 */
  signal: AbortSignal;
  /** 请求结束后必须调用，清除定时器。 */
  clear: () => void;
};

/**
 * 创建一个在 `ms` 毫秒后自动 abort 的 AbortController。
 *
 * @param ms - 超时毫秒数。
 * @param onTimeout - 超时时额外执行的回调（如置位超时标志），可选。
 * @returns 含 controller、signal 与 clear 清理函数。
 */
export function createTimeoutAbort(ms: number, onTimeout?: () => void): TimeoutAbort {
  const controller = new AbortController();
  const timer = setTimeout(() => {
    controller.abort();
    onTimeout?.();
  }, ms);
  return {
    controller,
    signal: controller.signal,
    clear: () => clearTimeout(timer),
  };
}

/**
 * 超时中止工具：统一替换散落各处的
 * `new AbortController()` + `setTimeout(() => controller.abort())` + `finally clearTimeout`
 * 样板，并统一使用全局 `setTimeout`/`clearTimeout`（避免 globalThis/window 混用）。
 *
 * 超时以 `TimeoutError` 作为 abort reason，与调用方主动取消（无 reason 的
 * `controller.abort()`，reason 为 `AbortError`）区分：超时是一次失败，必须进入
 * 调用方的错误 / 重试分支；主动取消才是「结果已不需要」，可以静默丢弃。
 * 只看 `signal.aborted` 会把两者混为一谈，曾导致恢复请求超时后静默退出、界面卡在运行中。
 */

export type TimeoutAbort = {
  /** 底层 AbortController，供外部主动 abort 或读取 controller.signal。 */
  controller: AbortController;
  /** 超时触发的 AbortSignal，直接传给 fetch 等请求。 */
  signal: AbortSignal;
  /** 请求结束后必须调用，清除定时器。 */
  clear: () => void;
};

/** 创建超时 abort reason；fetch 会以它作为拒绝原因抛出，`error.name === "TimeoutError"`。 */
function timeoutReason(ms: number): DOMException {
  return new DOMException(`请求在 ${ms}ms 内未完成`, "TimeoutError");
}

/**
 * 创建一个在 `ms` 毫秒后以 `TimeoutError` 自动 abort 的 AbortController。
 *
 * @param ms - 超时毫秒数。
 * @returns 含 controller、signal 与 clear 清理函数。
 */
export function createTimeoutAbort(ms: number): TimeoutAbort {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(timeoutReason(ms)), ms);
  return {
    controller,
    signal: controller.signal,
    clear: () => clearTimeout(timer),
  };
}

/**
 * 在调用方的取消信号之上叠加超时：任一触发都会中止返回的 signal。
 *
 * 调用方取消时沿用其 reason（通常为 AbortError），超时则为 TimeoutError，
 * 下游可用 isTimeoutAbort / isCancelledAbort 区分。
 */
export function withTimeout(ownerSignal: AbortSignal | null | undefined, ms: number): TimeoutAbort {
  const timeout = createTimeoutAbort(ms);
  if (!ownerSignal) return timeout;
  const forward = () => timeout.controller.abort(ownerSignal.reason);
  if (ownerSignal.aborted) forward();
  else ownerSignal.addEventListener("abort", forward, { once: true });
  return {
    ...timeout,
    clear: () => {
      timeout.clear();
      ownerSignal.removeEventListener("abort", forward);
    },
  };
}

/** signal 是否因超时而中止。 */
export function isTimeoutAbort(signal: AbortSignal): boolean {
  return signal.aborted && signal.reason instanceof DOMException && signal.reason.name === "TimeoutError";
}

/**
 * signal 是否被调用方主动取消（卸载、被新请求取代等）。
 *
 * 只有这种情况允许静默丢弃结果；超时必须按失败处理。
 */
export function isCancelledAbort(signal: AbortSignal): boolean {
  return signal.aborted && !isTimeoutAbort(signal);
}

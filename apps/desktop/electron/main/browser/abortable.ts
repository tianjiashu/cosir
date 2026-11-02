/**
 * 等一个异步步骤，`signal` 中止时以它的原因提前结束。取消只结束等待、不停下底层工作：资源由所有者收尾（标签由
 * `BrowserHost` 关闭，设备模拟由 `TabEmulation` 按已生效状态对齐）。
 */
export async function abortable<T>(pending: Promise<T>, signal: AbortSignal): Promise<T> {
  let onAbort!: () => void;
  try {
    return await new Promise<T>((resolve, reject) => {
      onAbort = () => reject(signal.reason);
      pending.then(resolve, reject);
      if (signal.aborted) onAbort();
      else signal.addEventListener("abort", onAbort, { once: true });
    });
  } finally {
    signal.removeEventListener("abort", onAbort);
  }
}

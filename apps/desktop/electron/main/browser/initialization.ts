/** 等待浏览器初始化中的单个异步步骤；取消只结束等待，底层资源由 BrowserHost 统一关闭。 */
export async function waitForInitialization(pending: Promise<void>, signal: AbortSignal): Promise<void> {
  let onAbort!: () => void;
  try {
    await new Promise<void>((resolve, reject) => {
      onAbort = () => reject(signal.reason);
      pending.then(resolve, reject);
      if (signal.aborted) onAbort();
      else signal.addEventListener("abort", onAbort, { once: true });
    });
  } finally {
    signal.removeEventListener("abort", onAbort);
  }
}

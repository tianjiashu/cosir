type FitCallback = () => boolean | void;
type DeferFitCallback = () => boolean;

/**
 * 将可变宿主尺寸转换成合并后的 xterm fit 请求。
 *
 * ResizeObserver 只观察外层宿主，不观察 xterm 自己生成的 viewport；尺寸变化在
 * animation frame 中合并，并且相同尺寸不会重复 fit。这样 surface 在 flex 布局切换、
 * 面板打开/关闭或 scrollbar 更新时不会形成 ResizeObserver 反馈循环。
 */
export class TerminalResizeController {
  private readonly observer: ResizeObserver | null;
  private frame: number | null = null;
  private disposed = false;
  private pending = false;
  private lastWidth = -1;
  private lastHeight = -1;
  private retryTimer: number | null = null;
  private retryCount = 0;

  constructor(
    private readonly container: HTMLElement,
    private readonly fit: FitCallback,
    private readonly shouldDefer: DeferFitCallback = () => false,
  ) {
    this.observer = typeof ResizeObserver === "undefined"
      ? null
      : new ResizeObserver(() => this.request());
    this.observer?.observe(container);
    this.request();
  }

  request(): void {
    if (this.disposed) return;
    this.pending = true;
    if (this.frame !== null || typeof window === "undefined") return;
    this.frame = window.requestAnimationFrame(() => {
      this.frame = null;
      this.flush();
    });
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.pending = false;
    this.observer?.disconnect();
    if (this.retryTimer !== null && typeof window !== "undefined") {
      window.clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
    if (this.frame !== null && typeof window !== "undefined") {
      window.cancelAnimationFrame(this.frame);
      this.frame = null;
    }
  }

  private flush(): void {
    if (this.disposed || !this.pending) return;
    if (this.shouldDefer()) return;

    const rect = this.container.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return;
    if (rect.width === this.lastWidth && rect.height === this.lastHeight) return;
    try {
      if (this.fit() === false) {
        this.scheduleRetry();
        return;
      }
    } catch {
      this.scheduleRetry();
      return;
    }
    this.pending = false;
    this.retryCount = 0;
    this.lastWidth = rect.width;
    this.lastHeight = rect.height;
  }

  private scheduleRetry(): void {
    if (this.disposed || this.retryTimer !== null || typeof window === "undefined") return;
    if (this.retryCount >= 5) return;
    const delay = Math.min(250, 16 * 2 ** this.retryCount);
    this.retryCount += 1;
    this.retryTimer = window.setTimeout(() => {
      this.retryTimer = null;
      this.request();
    }, delay);
  }
}

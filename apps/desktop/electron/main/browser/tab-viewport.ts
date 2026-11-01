import type { Rectangle } from "electron";

/** Agent 看到的视口（CSS 像素）：固定，与视图摆在哪、多大无关（面板只按比例缩放显示它）。 */
export const VIEWPORT = { width: 1280, height: 800 } as const;
/** 视图不在浏览器面板显示时的 bounds：与视口同大，缩放为 1。 */
export const DETACHED_BOUNDS: Rectangle = { x: 0, y: 0, ...VIEWPORT };

/**
 * 一个标签的视口与显示缩放。视口固定，经 CDP 设备模拟设置；挂到面板上时视图按比例缩小或放大，设备模拟的
 * `scale` 随之改。
 *
 * 缩放有两个值：想要的（摆放时同步改）与页面上已生效的（设备模拟设置成功之后）。CDP 合成输入的坐标是视图
 * 坐标、要乘「已生效」的比例（实测：缩放 0.5 时点 1050 落到 2100），所以 `toInput` 只读已生效的那个；两者
 * 不一致时（设置失败、被弹窗闸住）记为待同步，下一次摆放或弹窗答复后补设。同步串行，总是追最新的期望值。
 */
export class TabViewport {
  private desired = 1;
  private applied = 1;
  private shown = false;
  private ready = false;
  private syncing: Promise<void> | null = null;

  constructor(
    /** 把设备模拟设成给定缩放；失败时抛出。 */
    private readonly apply: (metrics: Record<string, unknown>, signal?: AbortSignal) => Promise<void>,
    private readonly onSyncFailed: (error: unknown) => void,
  ) {}

  /** 视图当前挂在面板上（用户能看见、能点到它）。 */
  get isShown(): boolean {
    return this.shown;
  }

  /** 初始化时设置一次（随初始化一起可取消）；此前的摆放只记下期望值。 */
  async start(signal: AbortSignal): Promise<void> {
    const target = this.desired;
    await this.apply(this.metrics(target), signal);
    this.applied = target;
    this.ready = true;
    // 等待期间又摆放过（期望值变了）：追上去。
    this.resync();
  }

  /** 视图挂到面板上、宽 `width`（视图坐标）。 */
  show(width: number): void {
    this.shown = true;
    this.desired = width / VIEWPORT.width;
    void this.sync();
  }

  hide(): void {
    this.shown = false;
    this.desired = 1;
    void this.sync();
  }

  /** 期望与已生效不一致时补设（弹窗答复后调用：弹窗打开期间的设置会被闸住）。 */
  resync(): void {
    if (this.desired !== this.applied) void this.sync();
  }

  /** CSS 像素 → CDP 输入坐标。 */
  toInput({ x, y }: { x: number; y: number }): { x: number; y: number } {
    return { x: x * this.applied, y: y * this.applied };
  }

  private sync(): Promise<void> {
    if (!this.ready) return Promise.resolve();
    this.syncing ??= (async () => {
      try {
        while (this.desired !== this.applied) {
          const target = this.desired;
          await this.apply(this.metrics(target));
          this.applied = target;
        }
      } catch (error) {
        this.onSyncFailed(error);
      } finally {
        this.syncing = null;
      }
    })();
    return this.syncing;
  }

  private metrics(scale: number): Record<string, unknown> {
    return { width: VIEWPORT.width, height: VIEWPORT.height, deviceScaleFactor: 0, mobile: false, scale };
  }
}

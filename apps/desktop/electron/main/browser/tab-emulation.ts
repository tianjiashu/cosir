import type { Rectangle } from "electron";

import { abortable } from "./abortable";

/** Agent 要求的设备模拟：视口尺寸（CSS 像素）、是否按手机布局、深浅色（null 为跟随应用外观）。 */
export type Emulation = { width: number; height: number; mobile: boolean; colorScheme: "light" | "dark" | null };

export const DEFAULT_EMULATION: Emulation = { width: 1280, height: 800, mobile: false, colorScheme: null };

/**
 * 页面上已生效的设置，按负责它的 CDP 命令分段：视口与缩放（`setDeviceMetricsOverride`）、触摸支持
 * （`setTouchEmulationEnabled`）、深浅色（`setEmulatedMedia`）。每条命令成功就更新它那一段，中途失败时已设上的
 * 部分照实记下——`toInput` 读的缩放必须是页面上真实的那个。例外是初始化：第一条命令成功时以目标值打底，其余两段
 * 还没发也记成已生效；初始化任何一条失败标签都会被关闭，不会留下记错的段。
 */
export type Applied = { width: number; height: number; mobile: boolean; scale: number; touch: boolean; colorScheme: "light" | "dark" | null };

type Send = (method: string, params: Record<string, unknown>, options?: { signal?: AbortSignal }) => Promise<unknown>;

/**
 * 一个标签的设备模拟与显示缩放，经 CDP 设置。视口由 Agent 决定，与视图摆在哪、多大无关；视图挂到面板上时只按
 * 比例缩放显示（`scale`）。深浅色实测压过应用的原生外观设置、导航后保持；mobile 一并开触摸支持
 * （`ontouchstart`、`maxTouchPoints`，不少站点据此切换交互），合成的鼠标事件不受影响。
 *
 * 等比摆放只在这里算：占位矩形 × 期望视口 → 要设的缩放，占位矩形 × 已生效视口 → 视图 bounds（`viewBounds`）。
 * 两者用同一个占位矩形，改视口时第一轮就设对缩放，不会先按旧摆放算一次、摆放跟上后再补一轮。
 *
 * 状态有两份：想要的（Agent 设置与摆放同步改）与页面上已生效的（`applied`）。CDP 合成输入的坐标是视图坐标、要乘
 * 「已生效」的缩放（实测：缩放 0.5 时点 1050 落到 2100），所以 `toInput` 只读已生效的；视口与缩放每次生效都经
 * `onMetricsApplied` 通知，视图 bounds 由它跟上，不在设置之后各处手工补调。两者不一致时串行地追到最新的期望值。
 * 设置失败（被弹窗闸住等）就停下，不原地重试——闸住时命令会立刻失败，重试只会空转；等下一次摆放、设置或弹窗
 * 答复（`resync`）再追。
 */
export class TabEmulation {
  private emulation: Emulation = DEFAULT_EMULATION;
  /**
   * 挂在面板上时的占位矩形（窗口坐标），只是摆放与缩放的参数；没挂上为 null。「挂没挂上」的事实归宿主（`TabEvents.isAttached`）：
   * 关闭标签时宿主只摘视图、不清它（不再对正在断开的调试器发模拟命令）。
   */
  private box: Rectangle | null = null;
  /** 初始化完成前为 null（此前的摆放与设置只记下期望值）。 */
  private applied: Applied | null = null;
  private running: Promise<void> | null = null;
  /** 上一次设置失败，等外部触发再追；`failure` 是它的原因。 */
  private stalled = false;
  private failure: unknown = null;

  constructor(
    private readonly send: Send,
    private readonly onSyncFailed: (error: unknown) => void,
    /** 视口与缩放（`setDeviceMetricsOverride`）生效了：视图 bounds 据此跟上。 */
    private readonly onMetricsApplied: (applied: Applied) => void,
  ) {}

  /** 页面上当前生效的模拟（页面结果里报告的就是它）；初始化完成前是默认值。 */
  get current(): Emulation {
    const applied = this.applied;
    return applied ? { width: applied.width, height: applied.height, mobile: applied.mobile, colorScheme: applied.colorScheme } : DEFAULT_EMULATION;
  }

  /**
   * 视图应在的 bounds（窗口坐标），按已生效的视口：挂在面板上时在占位矩形里等比缩放、水平居中、顶部对齐（与开发者
   * 工具的设备模式一致；渲染进程的冻结帧用同一规则：`object-contain object-top`）；不在面板上时与视口同大、不缩放，
   * 合成输入落在视图之内。
   */
  get viewBounds(): Rectangle {
    const { width, height } = this.current;
    const box = this.box;
    if (!box) return { x: 0, y: 0, width, height };
    const scale = fitScale(box, this.current);
    return {
      x: Math.round(box.x + (box.width - width * scale) / 2),
      y: Math.round(box.y),
      width: Math.round(width * scale),
      height: Math.round(height * scale),
    };
  }

  /** 初始化时把三条命令都设一次（随初始化一起可取消）。 */
  async start(signal: AbortSignal): Promise<void> {
    await this.apply(this.desired(), true, signal);
    // 等待期间又摆放或设置过（期望值变了）：追上去。
    void this.sync();
  }

  /**
   * Agent 改模拟：等到页面上生效再返回（之后的加载按新视口布局）。没设上或调用被取消时，期望值对齐到页面上实际
   * 生效的那份（可能部分命令已生效）、抛出原因——期望里不留失败的要求，免得之后哪次摆放或弹窗答复把它悄悄补设、
   * 或把已生效的部分悄悄撤回。取消只结束本次等待：追赶是摆放与设置共用的一轮，不随某个调用方取消而停下（CDP
   * 命令自带时限）；取消时还在途的命令事后生效，追赶会按对齐后的期望把它撤回（被取消的要求不生效）。
   */
  async set(emulation: Emulation, signal: AbortSignal): Promise<void> {
    this.emulation = emulation;
    this.stalled = false;
    try {
      while (this.pending()) {
        await abortable(this.sync(), signal);
        if (this.stalled) throw this.failure;
      }
    } catch (error) {
      this.emulation = this.current;
      throw error;
    }
  }

  /** 视图挂到面板上的占位矩形 `box`（窗口坐标）。 */
  show(box: Rectangle): void {
    this.box = box;
    this.resync();
  }

  hide(): void {
    this.box = null;
    this.resync();
  }

  /** 期望与已生效不一致时补设（弹窗答复后调用：弹窗打开期间的设置会被闸住）。 */
  resync(): void {
    this.stalled = false;
    void this.sync();
  }

  /** CSS 像素 → CDP 输入坐标。 */
  toInput({ x, y }: { x: number; y: number }): { x: number; y: number } {
    const scale = this.applied?.scale ?? 1;
    return { x: x * scale, y: y * scale };
  }

  private desired(): Applied {
    const { width, height, mobile, colorScheme } = this.emulation;
    return { width, height, mobile, colorScheme, touch: mobile, scale: this.box ? fitScale(this.box, this.emulation) : 1 };
  }

  private pending(): boolean {
    const applied = this.applied;
    if (!applied) return false;
    const desired = this.desired();
    return (Object.keys(desired) as (keyof Applied)[]).some((key) => desired[key] !== applied[key]);
  }

  /**
   * 串行地追到最新的期望值（失败时记下 `stalled` 与原因 `failure`）。正在追的时候再调用，拿到的是同一轮——它每次
   * 比较都读最新的期望值。收尾那一拍又来了新的期望（比较已结束、标记尚未清掉），在清标记时补追一轮。
   */
  private sync(): Promise<void> {
    if (!this.applied || this.stalled) return Promise.resolve();
    if (!this.running) {
      this.running = this.drain().finally(() => {
        this.running = null;
        if (!this.stalled && this.pending()) void this.sync();
      });
    }
    return this.running;
  }

  private async drain(): Promise<void> {
    while (this.pending()) {
      try {
        await this.apply(this.desired(), false);
      } catch (error) {
        this.stalled = true;
        this.failure = error;
        this.onSyncFailed(error);
        return;
      }
    }
  }

  /** 只发变了的命令（`force` 时全发），每条成功后更新它负责的那一段已生效状态。 */
  private async apply(target: Applied, force: boolean, signal?: AbortSignal): Promise<void> {
    const changed = (...keys: (keyof Applied)[]) => force || keys.some((key) => target[key] !== this.applied![key]);
    const record = (part: Partial<Applied>) => {
      this.applied = { ...(this.applied ?? target), ...part };
    };
    if (changed("width", "height", "mobile", "scale")) {
      await this.send("Emulation.setDeviceMetricsOverride", {
        width: target.width, height: target.height, deviceScaleFactor: 0, mobile: target.mobile, scale: target.scale,
      }, { signal });
      record({ width: target.width, height: target.height, mobile: target.mobile, scale: target.scale });
      this.onMetricsApplied(this.applied!);
    }
    if (changed("touch")) {
      await this.send("Emulation.setTouchEmulationEnabled", { enabled: target.touch }, { signal });
      record({ touch: target.touch });
    }
    if (changed("colorScheme")) {
      await this.send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: target.colorScheme ?? "" }] }, { signal });
      record({ colorScheme: target.colorScheme });
    }
  }
}

/** 视口放进占位矩形的等比缩放（视图坐标 / CSS 像素）。 */
function fitScale(box: Rectangle, viewport: { width: number; height: number }): number {
  return Math.min(box.width / viewport.width, box.height / viewport.height);
}

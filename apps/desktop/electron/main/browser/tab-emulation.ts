import type { Rectangle } from "electron";

import { abortable } from "./abortable";
import type { SendOptions } from "./cdp-session";

/** 视口：跟随面板（`follow`，默认），或 Agent 固定的尺寸（CSS 像素，可按手机布局）。 */
export type ViewportRequest = "follow" | { width: number; height: number; mobile: boolean };

/** Agent 的一次设置：只改给出的部分。深浅色 null 为跟随应用外观。 */
export type EmulationRequest = { viewport?: ViewportRequest; colorScheme?: "light" | "dark" | null };

/** 页面上生效的模拟（页面结果里报告它）；`follows` 为真时视口跟随面板。 */
export type Emulation = { width: number; height: number; mobile: boolean; colorScheme: "light" | "dark" | null; follows: boolean };

/** 跟随面板但还没挂上过面板时的视口（Agent 在后台先开的标签）。 */
export const DEFAULT_VIEWPORT = { width: 1280, height: 800 };

/** 面板尺寸停止变化多久后视口才跟上：拖动面板边缘时不随每一帧重新排版。 */
export const FOLLOW_SETTLE_MS = 200;

/**
 * 页面上已生效的设置，按负责它的 CDP 命令分段：视口与缩放（`setDeviceMetricsOverride`）、触摸支持
 * （`setTouchEmulationEnabled`）、深浅色（`setEmulatedMedia`）。每条命令成功就更新它那一段，中途失败时已设上的
 * 部分照实记下——`toInput` 读的缩放必须是页面上真实的那个。例外是初始化：第一条命令成功时以目标值打底，其余两段
 * 还没发也记成已生效；初始化任何一条失败标签都会被关闭，不会留下记错的段。
 */
export type Applied = { width: number; height: number; mobile: boolean; scale: number; touch: boolean; colorScheme: "light" | "dark" | null };

type Send = (method: string, params: Record<string, unknown>, options?: SendOptions) => Promise<unknown>;

/**
 * 一个标签的设备模拟与显示缩放，经 CDP 设置。视口有两种来源（`ViewportRequest`）：
 * - **跟随面板**（默认）：挂在面板上时视口就是占位矩形的大小、缩放 1，网页按真实大小铺满面板——用户在面板里看、
 *   接手操作（交接）时看得清点得准。稳定性靠两条：面板尺寸停止变化 `FOLLOW_SETTLE_MS` 后才跟上（拖动边缘时不逐帧
 *   重新排版，期间按旧视口等比显示）；从面板摘下时保持摘下前的大小（面板关掉、切到别的标签，Agent 手上的页面不变）。
 * - **Agent 固定**：如检查手机布局。视口与视图摆在哪、多大无关，挂到面板上时只按比例缩放显示（`scale`）。
 * 深浅色实测压过应用的原生外观设置、导航后保持；mobile 一并开触摸支持（`ontouchstart`、`maxTouchPoints`，不少站点
 * 据此切换交互），合成的鼠标事件不受影响。
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
  private viewport: ViewportRequest = "follow";
  private colorScheme: "light" | "dark" | null = null;
  /** 跟随面板时的视口：面板尺寸停稳后才更新，摘下时保持（见类说明）。 */
  private followed = DEFAULT_VIEWPORT;
  private settleTimer: ReturnType<typeof setTimeout> | null = null;
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

  /** 页面上当前生效的模拟（页面结果里报告的就是它）；初始化完成前是期望值。 */
  get current(): Emulation {
    const follows = this.viewport === "follow";
    const applied = this.applied;
    if (!applied) return { ...this.size(this.viewport), colorScheme: this.colorScheme, follows };
    return { width: applied.width, height: applied.height, mobile: applied.mobile, colorScheme: applied.colorScheme, follows };
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
    await abortable(this.apply(this.desired(), true, signal), signal);
    // 等待期间又摆放或设置过（期望值变了）：追上去。
    void this.sync();
  }

  /**
   * Agent 改模拟：等到页面上生效再返回（之后的加载按新视口布局）。改回跟随面板时立即按当前占位矩形，不等停稳。
   * 没设上或调用被取消时，期望值对齐到页面上实际生效的那份（可能部分命令已生效）、抛出原因——期望里不留失败的
   * 要求，免得之后哪次摆放或弹窗答复把它悄悄补设、或把已生效的部分悄悄撤回。取消只结束本次等待：追赶是摆放与设置
   * 共用的一轮，不随某个调用方取消而停下（CDP 命令自带时限）；取消时还在途的命令事后生效，追赶会按对齐后的期望把
   * 它撤回（被取消的要求不生效）。
   */
  async set(request: EmulationRequest, signal: AbortSignal): Promise<void> {
    const previous = this.viewport;
    if (request.viewport !== undefined) {
      this.viewport = request.viewport;
      if (request.viewport === "follow" && this.box) this.settle(this.box);
    }
    if (request.colorScheme !== undefined) this.colorScheme = request.colorScheme;
    this.stalled = false;
    try {
      while (this.pending()) {
        await abortable(this.sync(), signal);
        if (this.stalled) throw this.failure;
      }
    } catch (error) {
      this.alignToApplied(previous);
      throw error;
    }
  }

  /** 等已有同步轮真正退出；取消调用者的等待不代表这轮命令已经结束。 */
  async settled(): Promise<void> {
    while (this.running) await this.running;
  }

  /** 输入开始前落定已排队的面板尺寸，避免 200ms 防抖在一次点击/拖动中途改变坐标比例。 */
  async prepareInput(signal: AbortSignal): Promise<void> {
    if (this.settleTimer && this.box) this.settle(this.box);
    await abortable(this.settled(), signal);
    if (this.stalled) throw this.failure;
  }

  /** 视图挂到面板上的占位矩形 `box`（窗口坐标）。跟随面板时视口等尺寸停稳再跟上（初始化前直接取它）。 */
  show(box: Rectangle): void {
    this.box = box;
    if (this.viewport === "follow") {
      this.clearSettle();
      if (!this.applied) this.followed = { width: box.width, height: box.height };
      else if (box.width !== this.followed.width || box.height !== this.followed.height) {
        this.settleTimer = setTimeout(() => this.settle(box), FOLLOW_SETTLE_MS);
      }
    }
    this.resync();
  }

  /** 从面板摘下：视口保持现在的大小（还没停稳的面板尺寸不再跟上）。 */
  hide(): void {
    this.box = null;
    this.clearSettle();
    this.resync();
  }

  /** 标签关闭：不再跟上面板尺寸（停稳计时器不能在会话断开后再发命令）。 */
  dispose(): void {
    this.clearSettle();
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
    const size = this.size(this.viewport);
    return { ...size, colorScheme: this.colorScheme, touch: size.mobile, scale: this.box ? fitScale(this.box, size) : 1 };
  }

  private size(viewport: ViewportRequest): { width: number; height: number; mobile: boolean } {
    return viewport === "follow" ? { ...this.followed, mobile: false } : viewport;
  }

  /** 面板尺寸停稳：跟随面板的视口跟上它。 */
  private settle(box: Rectangle): void {
    this.clearSettle();
    this.followed = { width: box.width, height: box.height };
    this.resync();
  }

  private clearSettle(): void {
    if (this.settleTimer) clearTimeout(this.settleTimer);
    this.settleTimer = null;
  }

  /**
   * 设置失败后让期望与页面上已生效的一致：视口取新要求、旧要求里与已生效尺寸相符的那个，都不符（部分生效）就固定在
   * 已生效的尺寸；深浅色取已生效的。这样不会留下待补设的要求，也不会撤回已生效的部分。
   */
  private alignToApplied(previous: ViewportRequest): void {
    const applied = this.applied;
    if (!applied) return;
    const matches = (viewport: ViewportRequest) => {
      const size = this.size(viewport);
      return size.width === applied.width && size.height === applied.height && size.mobile === applied.mobile;
    };
    if (!matches(this.viewport)) {
      this.viewport = matches(previous) ? previous : { width: applied.width, height: applied.height, mobile: applied.mobile };
    }
    this.colorScheme = applied.colorScheme;
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
      await this.sendApplied("Emulation.setDeviceMetricsOverride", {
        width: target.width, height: target.height, deviceScaleFactor: 0, mobile: target.mobile, scale: target.scale,
      }, signal, () => {
        record({ width: target.width, height: target.height, mobile: target.mobile, scale: target.scale });
        this.onMetricsApplied(this.applied!);
      });
    }
    if (changed("touch")) {
      await this.sendApplied("Emulation.setTouchEmulationEnabled", { enabled: target.touch }, signal, () => record({ touch: target.touch }));
    }
    if (changed("colorScheme")) {
      await this.sendApplied("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: target.colorScheme ?? "" }] }, signal, () => record({ colorScheme: target.colorScheme }));
    }
  }
  /** 已生效事实跟随原始命令，即使调用者已因超时/取消结束等待，迟到成功也不能留下旧缩放。 */
  private sendApplied(method: string, params: Record<string, unknown>, signal: AbortSignal | undefined, record: () => void): Promise<unknown> {
    let completion: Promise<unknown> | undefined;
    const waiting = this.send(method, params, { signal, onCommand: (raw) => { completion = raw.then(record); } });
    // 这一轮属于标签，不属于请求等待器；包装超时不能打开串行闸门。set/start 各自处理调用者的取消。
    // 未派发（如弹窗闸门拒绝）时只有 waiting；派发后只认原始完成，下一轮才不会与迟到命令交叉。
    if (!completion) return waiting;
    void waiting.catch(() => undefined);
    return completion;
  }

}

/** 视口放进占位矩形的等比缩放（视图坐标 / CSS 像素）。 */
function fitScale(box: Rectangle, viewport: { width: number; height: number }): number {
  return Math.min(box.width / viewport.width, box.height / viewport.height);
}

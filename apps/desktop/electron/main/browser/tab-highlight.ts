import { logDesktopEvent } from "../desktop-log";
import type { CdpSession } from "./cdp-session";
import type { Box } from "./input/element";
import { overlayQuad, readGeometry } from "./page-geometry";

/** 框停留多久（毫秒）：够人看清，又不在下一个动作时还挂着上一个。 */
const LINGER_MS = 600;
const FILL = { r: 59, g: 130, b: 246, a: 0.18 };
const OUTLINE = { r: 59, g: 130, b: 246, a: 0.9 };

/**
 * 给人看的「Agent 正在操作哪里」：用 CDP Overlay 在页面上画框，片刻后收起。只在标签挂在面板上时画（没人看就不画，
 * 也不必为它打开 DOM 与 Overlay 域）。
 *
 * 实测（真实应用）：框画在页面上，用户看得见；但它也会进 `Page.captureScreenshot`——所以截图前必须先 `clear`（由
 * `BrowserTab.screenshot` 一处保证），否则 Agent 截到的是带框的画面。快照读无障碍树，不受影响。框的坐标换算见
 * `page-geometry.ts` 的 `overlayQuad`。
 *
 * 画框只是旁路提示：失败（页面停着、标签正在关闭）不影响动作，记一条日志即可。
 */
export class TabHighlight {
  private enabled = false;
  private shown = false;
  private timer: NodeJS.Timeout | null = null;

  constructor(
    private readonly session: CdpSession,
    private readonly tabId: string,
    private readonly visible: () => boolean,
  ) {}

  /** 标签挂在面板上时才量位置并画框（`locate` 可能要发几条 CDP 命令，没人看就省掉）。 */
  async show(locate: () => Promise<Box>): Promise<void> {
    if (!this.visible()) return;
    try {
      const area = await locate();
      // 每次现读：窗口挪到另一块屏幕时像素比会变。
      const quad = overlayQuad(area, await readGeometry((method, params) => this.session.send(method, params)));
      if (!this.enabled) {
        await this.session.send("DOM.enable");
        await this.session.send("Overlay.enable");
        this.enabled = true;
      }
      await this.session.send("Overlay.highlightQuad", {
        quad,
        color: FILL,
        outlineColor: OUTLINE,
      });
      this.shown = true;
      if (this.timer) clearTimeout(this.timer);
      this.timer = setTimeout(() => void this.clear(), LINGER_MS);
    } catch (error) {
      logDesktopEvent("INFO", "browser", "browser_highlight_failed", { tab_id: this.tabId }, error);
    }
  }

  /** 标签关闭：不再收起（会话已断开），只停掉计时。 */
  dispose(): void {
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
  }

  /** 收起框（截图前、计时到了）。收不掉（页面被弹窗停住）就留着标记，下一次再收。 */
  async clear(): Promise<void> {
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    if (!this.shown) return;
    try {
      await this.session.send("Overlay.hideHighlight");
      this.shown = false;
    } catch (error) {
      logDesktopEvent("INFO", "browser", "browser_highlight_clear_failed", { tab_id: this.tabId }, error);
    }
  }
}

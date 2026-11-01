import { WebContentsView } from "electron";

import { logDesktopEvent } from "../desktop-log";
import { HostMethodError } from "../host-link/methods";
import { BROWSER_PARTITION } from "./browser-session";
import { runActions, type ActResult } from "./page-input";
import type { BrowserNavigation, BrowserTabCreator, BrowserTabSummary } from "../../shared/host-contract";
import type { PageAction } from "./params";
import { RefRegistry } from "./ref-registry";
import { formatSnapshot, type AXNode, type FormattedSnapshot } from "./snapshot-format";
import { isWebProtocol, urlProtocol } from "./web-url";
import { waitForInitialization } from "./initialization";

/** 视图不在浏览器面板显示时的视口；挂上面板后改为占位矩形的尺寸。 */
export const DETACHED_VIEWPORT = { x: 0, y: 0, width: 1280, height: 800 };
const NAVIGATION_TIMEOUT_MS = 30_000;
const CDP_TIMEOUT_MS = 10_000;
/** 加载完成后再等一下：多数页面在 load 之后还会渲染首屏内容。 */
const SETTLE_MS = 250;
const ERR_ABORTED = -3;
/** 动作触发导航后最多等多久加载结束；超时不算失败，快照会标「仍在加载」。 */
const ACTION_SETTLE_TIMEOUT_MS = 15_000;
/** 用户最近一次输入后多久内 Agent 的动作被拒（设计稿决定记录：2 秒）。 */
const USER_ACTIVE_WINDOW_MS = 2_000;
/** 动作结束后多久内到达的输入事件仍算 Agent 的（合成按键的事件可能稍晚才派发）。 */
const AGENT_INPUT_GRACE_MS = 500;
/** 整页截图的最大高度（CSS 像素）：再长的页面模型也看不清，按需滚动后再截。 */
const FULL_PAGE_MAX_HEIGHT = 4_000;

export type PageSnapshot = FormattedSnapshot & { url: string; title: string; loading: boolean; notes: string[] };

export type TabEvents = {
  /** 页面要开新窗口（已拒绝原生窗口），由 `BrowserHost` 决定是否在同 Task 里开新标签。 */
  onPopup: (url: string) => void;
  /** 地址、标题、加载或崩溃状态变了：`BrowserHost` 据此推送标签列表。 */
  onChange: () => void;
};

/**
 * 一个标签页：一个 `WebContentsView` 加一个 CDP 会话。本模块是 CDP 在宿主里唯一的使用者，方法集合固定，
 * 不对外透传任意 CDP 命令。
 *
 * 几条由实测确定的约束（设计稿 §8）：
 * - 视图可以不挂在任何窗口上：CDP 截图、无障碍树、点击与输入照常工作（`capturePage` 不行），视口由
 *   `setBounds` 决定，所以后台标签保持 1280×800。
 * - 未加载过任何页面的 webContents 上 CDP 命令会一直挂起：先加载 `about:blank` 再附加调试器。
 * - CDP 合成的鼠标与文字输入不触发 `before-input-event`，合成按键可能触发：动作执行期间的输入事件归
 *   Agent，其余才记为用户输入（`USER_ACTIVE`）。
 * - 视图不挂在窗口上时 CDP 的滚轮事件永远不返回：滚动用页面内的 `scrollBy`。
 * - JS 弹窗必须由 `disableDialogs` 关掉：Electron 默认的弹窗处理会在页面加载中卡死整个主进程。
 *
 * 页面上发生、模型需要知道但不是导航结果的事（弹窗被关掉、文件选择被拦、新窗口被拦）记成备注，随下一次
 * 快照交给模型。
 */
export class BrowserTab {
  readonly view: WebContentsView;
  /** 最近一次真实用户输入的时间（毫秒）；`USER_ACTIVE` 据此判断。 */
  private lastUserInputAt = 0;
  /** 在此之前到达的输入事件来自 Agent 自己的动作，不算用户输入（见构造函数里的说明）。 */
  private agentInputUntil = 0;
  private readonly refs = new RefRegistry();
  private notes: string[] = [];

  constructor(
    readonly tabId: string,
    readonly taskId: number,
    readonly createdBy: BrowserTabCreator,
    events: TabEvents,
  ) {
    this.view = new WebContentsView({
      // disableDialogs：实测不关时，页面加载中弹出的 confirm() 会卡死整个宿主主进程；关掉后 alert 直接返回、
      // confirm / prompt 视为取消，CDP 仍会发出弹窗事件，用来告诉模型。
      // defaultEncoding：没声明编码的页面按 UTF-8 解码；否则 Chromium 按系统语言猜，中文页会成乱码。
      webPreferences: {
        partition: BROWSER_PARTITION,
        sandbox: true,
        contextIsolation: true,
        nodeIntegration: false,
        disableDialogs: true,
        defaultEncoding: "UTF-8",
      },
    });
    this.view.setBounds(DETACHED_VIEWPORT);
    const contents = this.view.webContents;
    contents.setBackgroundThrottling(false);
    contents.setWindowOpenHandler(({ url }) => {
      events.onPopup(url);
      return { action: "deny" };
    });
    contents.on("will-navigate", (event, url) => this.blockNonWebNavigation(event, url));
    contents.on("will-redirect", (event, url) => this.blockNonWebNavigation(event, url));
    contents.on("did-navigate", () => this.refs.clear());
    const changed = () => events.onChange();
    contents.on("did-navigate", changed);
    contents.on("did-navigate-in-page", changed);
    contents.on("page-title-updated", changed);
    contents.on("did-start-loading", changed);
    contents.on("did-stop-loading", changed);
    contents.on("render-process-gone", changed);
    // CDP 合成的鼠标与文字输入不触发 before-input-event，但合成键盘事件可能触发（实测：提交表单时回车的
    // keyUp）。所以动作执行期间及之后一小段时间里的输入事件归 Agent，其余才是用户在操作。
    contents.on("before-input-event", () => {
      if (Date.now() < this.agentInputUntil) return;
      this.lastUserInputAt = Date.now();
    });
    contents.on("render-process-gone", (_event, details) => {
      logDesktopEvent("WARNING", "browser", "browser_tab_crashed", { tab_id: tabId, task_id: taskId, reason: details.reason });
    });
  }

  /** 视图先交给 Host 登记，再异步初始化；关闭时的 signal 使所有初始化等待立即结束。 */
  async initialize(signal: AbortSignal): Promise<void> {
    await waitForInitialization(this.view.webContents.loadURL("about:blank"), signal);
    signal.throwIfAborted();
    this.attachDebugger();
    await this.cdp("Page.enable", {}, signal);
    await this.cdp("Page.setInterceptFileChooserDialog", { enabled: true }, signal);
  }

  /** 导航并等主框架加载完成；失败带回 Chromium 的 `ERR_*` 说明。 */
  async navigate(target: BrowserNavigation, signal: AbortSignal): Promise<void> {
    if (signal.aborted) throw new HostMethodError("CANCELLED", "navigation was cancelled");
    const contents = this.view.webContents;
    const history = contents.navigationHistory;
    if ("action" in target && target.action === "back" && !history.canGoBack()) {
      throw new HostMethodError("NAVIGATION_FAILED", "there is no previous page in this tab");
    }
    if ("action" in target && target.action === "forward" && !history.canGoForward()) {
      throw new HostMethodError("NAVIGATION_FAILED", "there is no next page in this tab");
    }
    const loaded = this.waitForLoad(signal);
    if ("url" in target) {
      // 结果以加载事件为准：loadURL 自己的拒绝（含重定向造成的 ERR_ABORTED）在这里不重复处理。
      contents.loadURL(target.url).catch(() => undefined);
    } else if (target.action === "back") history.goBack();
    else if (target.action === "forward") history.goForward();
    else contents.reload();
    await loaded;
    await new Promise((resolve) => setTimeout(resolve, SETTLE_MS));
  }

  /** 读整棵无障碍树并从 `fromLine` 起输出；ref 按节点稳定分配（`RefRegistry`），不随快照替换。 */
  async snapshot(fromLine = 1): Promise<PageSnapshot> {
    this.assertAlive();
    const { nodes } = await this.cdp<{ nodes: AXNode[] }>("Accessibility.getFullAXTree");
    const formatted = formatSnapshot(nodes, this.refs, { fromLine });
    const contents = this.view.webContents;
    const notes = this.notes;
    this.notes = [];
    return { ...formatted, url: contents.getURL(), title: contents.getTitle(), loading: contents.isLoading(), notes };
  }

  /**
   * 在页面上按序执行动作（见 `page-input.ts`）。用户刚在这个页面上操作过时整批拒绝（`USER_ACTIVE`）：
   * 与终端「用户输入从不被拒、Agent 等用户停手」同一原则。
   */
  async act(actions: readonly PageAction[], signal: AbortSignal): Promise<ActResult> {
    this.assertAlive();
    if (Date.now() - this.lastUserInputAt < USER_ACTIVE_WINDOW_MS) {
      throw new HostMethodError("USER_ACTIVE", "the user is using this page right now; wait a moment and retry, or hand it over with browser_handoff");
    }
    this.agentInputUntil = Number.POSITIVE_INFINITY;
    try {
      return await runActions({
        cdp: (method, params) => this.cdp(method, params),
        resolveRef: (ref) => this.resolveRef(ref),
        settle: () => this.settle(),
      }, actions, signal);
    } finally {
      this.agentInputUntil = Date.now() + AGENT_INPUT_GRACE_MS;
    }
  }

  /** ref → CDP `backendNodeId`；从没登记过（含导航前的 ref）报 `REF_STALE`。节点是否还在页面上由 `page-input.ts` 确认。 */
  resolveRef(ref: string): number {
    const backendNodeId = this.refs.resolve(ref);
    if (backendNodeId === undefined) {
      throw new HostMethodError("REF_STALE", `ref ${ref} is not on the current page; take a new snapshot`);
    }
    return backendNodeId;
  }

  /**
   * 截图，返回 data URL。用 CDP 而不是 `capturePage`：视图摘离窗口时后者不可用（§8）。
   *
   * 按 CSS 像素出图（`clip.scale = 1 / devicePixelRatio`）：视口 1280×800 就是 1280×800 的图，不随屏幕
   * 倍率翻倍——模型按分辨率计费，多出来的像素看不出更多东西。`fullPage` 截整页，高度封顶。
   * `devicePixels` 按屏幕像素出图，只给浏览器面板的冻结帧用。
   */
  async screenshot(format: "jpeg" | "png", options: { quality?: number; fullPage?: boolean; devicePixels?: boolean } = {}): Promise<string> {
    this.assertAlive();
    const metrics = await this.cdp<{
      cssVisualViewport: { pageX: number; pageY: number; clientWidth: number; clientHeight: number };
      cssContentSize: { width: number; height: number };
    }>("Page.getLayoutMetrics");
    const { result } = await this.cdp<{ result: { value: number } }>("Runtime.evaluate", { expression: "devicePixelRatio", returnByValue: true });
    const viewport = metrics.cssVisualViewport;
    const clip = options.fullPage
      ? { x: 0, y: 0, width: viewport.clientWidth, height: Math.min(metrics.cssContentSize.height, FULL_PAGE_MAX_HEIGHT) }
      : { x: viewport.pageX, y: viewport.pageY, width: viewport.clientWidth, height: viewport.clientHeight };
    const { data } = await this.cdp<{ data: string }>("Page.captureScreenshot", {
      format,
      ...(options.quality === undefined ? {} : { quality: options.quality }),
      clip: { ...clip, scale: options.devicePixels ? 1 : 1 / (result.value || 1) },
      captureBeyondViewport: Boolean(options.fullPage),
    });
    return `data:image/${format};base64,${data}`;
  }

  summary(): BrowserTabSummary {
    const contents = this.view.webContents;
    return {
      tabId: this.tabId,
      taskId: this.taskId,
      createdBy: this.createdBy,
      url: contents.getURL(),
      title: contents.getTitle(),
      loading: contents.isLoading(),
      crashed: contents.isCrashed(),
    };
  }

  /** 直接问 webContents：崩溃事件是异步到达的，崩溃后到事件之间发出的 CDP 命令会一直挂到超时。 */
  private assertAlive(): void {
    if (this.view.webContents.isCrashed()) throw new HostMethodError("PAGE_CRASHED", "the page crashed; reload it or navigate elsewhere to recover");
  }

  /** 动作之后：页面在加载（点了链接、提交了表单）就等它结束，有时限、超时不报错，再留一点渲染时间。 */
  private async settle(): Promise<void> {
    const contents = this.view.webContents;
    // 点击触发的导航是异步开始的：先等一下再看是否在加载。
    await new Promise((resolve) => setTimeout(resolve, SETTLE_MS));
    if (contents.isLoading()) {
      await new Promise<void>((resolve) => {
        const timer = setTimeout(done, ACTION_SETTLE_TIMEOUT_MS);
        function done() {
          clearTimeout(timer);
          contents.off("did-stop-loading", done);
          resolve();
        }
        contents.on("did-stop-loading", done);
      });
    }
    await new Promise((resolve) => setTimeout(resolve, SETTLE_MS));
  }

  note(text: string): void {
    this.notes.push(text);
  }

  close(): void {
    const contents = this.view.webContents;
    if (contents.debugger.isAttached()) contents.debugger.detach();
    contents.close();
  }

  private attachDebugger(): void {
    const { debugger: cdp } = this.view.webContents;
    cdp.attach("1.3");
    cdp.on("message", (_event, method, params) => {
      if (method === "Page.javascriptDialogOpening") this.dismissDialog(params as { type: string; message: string });
      else if (method === "Page.fileChooserOpened") {
        this.note("the page tried to open a file chooser; file uploads are not supported, so it was blocked");
        logDesktopEvent("INFO", "browser", "browser_file_chooser_blocked", { tab_id: this.tabId, task_id: this.taskId });
      }
    });
    cdp.on("detach", (_event, reason) => {
      logDesktopEvent("WARNING", "browser", "browser_debugger_detached", { tab_id: this.tabId, reason });
    });
  }

  /** 弹窗已被 `disableDialogs` 自动处理（confirm / prompt 视为取消），这里只告诉模型发生过什么。 */
  private dismissDialog({ type, message }: { type: string; message: string }): void {
    this.note(`the page showed a ${type} dialog "${message.slice(0, 200)}"; dialogs are dismissed automatically (confirm and prompt count as cancel)`);
    logDesktopEvent("INFO", "browser", "browser_dialog_dismissed", { tab_id: this.tabId, task_id: this.taskId, type });
  }

  private blockNonWebNavigation(event: Electron.Event, url: string): void {
    const protocol = urlProtocol(url);
    if (isWebProtocol(protocol)) return;
    event.preventDefault();
    this.note(`the page tried to navigate to a ${protocol || "non-web"} address; it was blocked`);
  }

  /** 主框架加载完成（含同文档导航）即成功；主框架加载失败、超时或调用被撤销即失败。 */
  private waitForLoad(signal: AbortSignal): Promise<void> {
    const contents = this.view.webContents;
    return new Promise((resolve, reject) => {
      const finish = (error?: HostMethodError) => {
        clearTimeout(timer);
        contents.off("did-finish-load", onLoad);
        contents.off("did-navigate-in-page", onInPage);
        contents.off("did-fail-load", onFail);
        signal.removeEventListener("abort", onAbort);
        if (error) reject(error);
        else resolve();
      };
      const onLoad = () => finish();
      const onInPage = (_event: Electron.Event, _url: string, isMainFrame: boolean) => {
        if (isMainFrame) finish();
      };
      const onFail = (_event: Electron.Event, code: number, description: string, _url: string, isMainFrame: boolean) => {
        if (!isMainFrame || code === ERR_ABORTED) return;
        logDesktopEvent("INFO", "browser", "browser_navigation_failed", { tab_id: this.tabId, task_id: this.taskId, code, description });
        finish(new HostMethodError("NAVIGATION_FAILED", `the page failed to load (${description || code})`));
      };
      const onAbort = () => {
        contents.stop();
        finish(new HostMethodError("CANCELLED", "navigation was cancelled"));
      };
      const timer = setTimeout(() => {
        contents.stop();
        finish(new HostMethodError("NAVIGATION_FAILED", `the page did not finish loading within ${NAVIGATION_TIMEOUT_MS / 1000} seconds`));
      }, NAVIGATION_TIMEOUT_MS);
      contents.on("did-finish-load", onLoad);
      contents.on("did-navigate-in-page", onInPage);
      contents.on("did-fail-load", onFail);
      signal.addEventListener("abort", onAbort, { once: true });
    });
  }

  /** CDP 命令带时限：渲染进程卡死时命令会一直不返回。 */
  private cdp<T = unknown>(method: string, params: Record<string, unknown> = {}, signal?: AbortSignal): Promise<T> {
    signal?.throwIfAborted();
    // 只在本模块与 page-input.ts 里用固定的方法集合，不向链路透传任意 CDP。
    const command = this.view.webContents.debugger.sendCommand(method, params) as Promise<T>;
    return new Promise<T>((resolve, reject) => {
      const clear = () => {
        clearTimeout(timer);
        signal?.removeEventListener("abort", onAbort);
      };
      const fail = (error: unknown) => { clear(); reject(error); };
      const onAbort = () => fail(signal!.reason);
      const timer = setTimeout(
        () => fail(new HostMethodError("PAGE_UNRESPONSIVE", `the page did not respond to ${method}`)),
        CDP_TIMEOUT_MS,
      );
      signal?.addEventListener("abort", onAbort, { once: true });
      command.then(
        (value) => { clear(); resolve(value); },
        fail,
      );
    });
  }
}

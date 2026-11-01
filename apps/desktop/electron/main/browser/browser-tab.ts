import { WebContentsView, type Rectangle, type WebContents, type WindowOpenHandlerResponse } from "electron";

import { logDesktopEvent } from "../desktop-log";
import { HostMethodError } from "../host-link/methods";
import { browserPageWebPreferences } from "./browser-session";
import { runActions, type ActResult } from "./page-input";
import type { BrowserDialog, BrowserDialogAnswer, BrowserNavigation, BrowserTabCreator, BrowserTabSummary } from "../../shared/host-contract";
import { BROWSER_PAGE_DIALOG_CHANNEL, type BrowserPageDialogRequest } from "../../shared/browser-page-bridge";
import type { PageAction } from "./params";
import { PageDialogs } from "./page-dialogs";
import { RefRegistry } from "./ref-registry";
import { formatSnapshot, type AXNode, type FormattedSnapshot } from "./snapshot-format";
import { DETACHED_BOUNDS, TabViewport } from "./tab-viewport";
import { isWebProtocol, urlProtocol } from "./web-url";
import { waitForInitialization } from "./initialization";

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
/** 两次快照之间最多攒多少条备注：持续刷权限请求之类的页面不能让它无限增长。 */
const MAX_NOTES = 50;

export type PageSnapshot = FormattedSnapshot & {
  url: string;
  title: string;
  loading: boolean;
  notes: string[];
  /** 本标签待答的弹窗；非空时页面停着，`text` 为空（读不到无障碍树）。 */
  dialog: BrowserDialog | null;
};

export type TabEvents = {
  /** 页面要开新窗口：由 `BrowserHost` 决定拒绝，或收养成同 Task 的新标签（保留 opener）。 */
  onWindowOpen: (url: string) => WindowOpenHandlerResponse;
  /** 摘要里的任一项变了（地址、标题、加载、崩溃、弹窗）：`BrowserHost` 据此推送状态。 */
  onChange: () => void;
  /** webContents 不经 `BrowserHost` 就没了（页面自己 `window.close()`）：`BrowserHost` 据此注销标签。 */
  onGone: () => void;
  /** 本标签弹窗了：整个渲染进程停住，`BrowserHost` 让同进程标签在途的 CDP 立即失败。 */
  onDialogOpened: () => void;
  /** 页面为什么停着（本标签或同进程别的标签有待答弹窗）；没停时为 null。由 `BrowserHost` 按进程判断。 */
  pausedBy: () => HostMethodError | null;
  /** 用户在面板里点了选文件：弹系统文件选择框，返回选中的本机路径，取消为 null。 */
  chooseFiles: (multiple: boolean) => Promise<string[] | null>;
};

export type TabOptions = {
  tabId: string;
  taskId: number;
  createdBy: BrowserTabCreator;
  /** 开出这个标签的标签（页面 `window.open` / `target=_blank`）；否则为 null。 */
  openerId: string | null;
  /** Task 所在工作区的下载目录（本机绝对路径），标签创建时定下；下载本身由 `BrowserHost` 登记。 */
  downloadDir: string;
  /** 页面开出的标签：Chromium 已建好的 webContents（保留 opener）；新建的标签不传。 */
  adopted?: WebContents;
  events: TabEvents;
};

/**
 * 一个标签页：一个 `WebContentsView` 加一个 CDP 会话。本模块是 CDP 在宿主里唯一的使用者，方法集合固定，
 * 不对外透传任意 CDP 命令。
 *
 * 两种来源：新建（先加载 `about:blank` 再附加调试器——未加载过任何页面的 webContents 上 CDP 命令会一直挂起），
 * 与收养页面开出的 webContents（等它第一次加载结束、弹窗被答完再附加）；之后两者没有差别。
 *
 * 视口与显示分离（`TabViewport`）：Agent 看到的视口固定，视图挂到面板上时只按比例缩放显示。
 *
 * 弹窗（`PageDialogs`）：页面停住时整个渲染进程的 CDP 都回不来，所以 `cdp` 是唯一闸门——页面停着（由
 * `BrowserHost` 按进程判断）新发的命令直接失败，在途的在弹窗打开那一刻失败（`interruptCdp`），都以
 * `DIALOG_OPEN` 指明是哪个标签的弹窗；等加载的地方同样在那一刻结束。
 *
 * 只需告知 Agent 的事（拦下的跳转、Agent 触发或后台的选文件、站点权限被拒、强行离开 `beforeunload`、绕过网页
 * preload 的原生弹窗）记成备注，随下一次快照交给模型；备注的统一呈现见 AGENTS.md 待办。
 *
 * 几条由实测确定的约束（设计稿 §8、§17）：
 * - 视图可以不挂在任何窗口上：CDP 截图、无障碍树与输入照常工作（`capturePage` 不行）；鼠标事件要开焦点模拟。
 * - CDP 合成的鼠标与文字输入不触发 `before-input-event`，合成按键可能触发：动作执行期间的输入事件归
 *   Agent，其余才记为用户输入（`USER_ACTIVE`）。
 * - 视图不挂在窗口上时 CDP 的滚轮事件永远不返回：滚动用页面内的 `scrollBy`。
 * - webContents 销毁后再读 `view.webContents` 会抛错：构造时把它取下来自己持有（`contents`）。
 */
export class BrowserTab {
  readonly view: WebContentsView;
  readonly contents: WebContents;
  readonly tabId: string;
  readonly taskId: number;
  readonly createdBy: BrowserTabCreator;
  readonly openerId: string | null;
  readonly downloadDir: string;
  private readonly adopted: boolean;
  private readonly events: TabEvents;
  /** 最近一次真实用户输入的时间（毫秒）；`USER_ACTIVE` 据此判断。 */
  private lastUserInputAt = 0;
  /** 在此之前到达的输入事件来自 Agent 自己的动作，不算用户输入（见构造函数里的说明）。 */
  private agentInputUntil = 0;
  private readonly refs = new RefRegistry();
  private readonly dialogs = new PageDialogs();
  private readonly viewport: TabViewport;
  private notes: string[] = [];
  /** 页面停住时中止，让在途的 CDP 命令与加载等待立即结束；每次中止后换新的。 */
  private gate = new AbortController();

  constructor(options: TabOptions) {
    const { tabId, taskId, events } = options;
    this.tabId = tabId;
    this.taskId = taskId;
    this.createdBy = options.createdBy;
    this.openerId = options.openerId;
    this.downloadDir = options.downloadDir;
    this.events = events;
    this.adopted = options.adopted !== undefined;
    // 收养的 webContents 已带着创建时的 webPreferences（`overrideBrowserWindowOptions`），这里不再传。
    this.view = new WebContentsView(options.adopted ? { webContents: options.adopted } : { webPreferences: browserPageWebPreferences() });
    this.view.setBounds(DETACHED_BOUNDS);
    this.contents = this.view.webContents;
    this.viewport = new TabViewport(
      async (metrics, signal) => { await this.cdp("Emulation.setDeviceMetricsOverride", metrics, signal); },
      (error) => logDesktopEvent("INFO", "browser", "browser_viewport_sync_failed", { tab_id: tabId, error: errorText(error) }),
    );
    const contents = this.contents;
    contents.setBackgroundThrottling(false);
    contents.setWindowOpenHandler(({ url }) => events.onWindowOpen(url));
    contents.on("will-navigate", (event, url) => this.blockNonWebNavigation(event, url));
    contents.on("will-redirect", (event, url) => this.blockNonWebNavigation(event, url));
    contents.on("will-prevent-unload", (event) => {
      // 不放行时跳转会被静默取消（Agent 的导航只会超时）；没有可以作答的界面，按「离开」处理并告知。
      event.preventDefault();
      this.note("the page asked to confirm leaving it (unsaved changes?); it was left anyway");
    });
    contents.on("did-navigate", () => this.refs.clear());
    const changed = () => events.onChange();
    contents.on("did-navigate", changed);
    contents.on("did-navigate-in-page", changed);
    contents.on("page-title-updated", changed);
    contents.on("did-start-loading", changed);
    contents.on("did-stop-loading", changed);
    contents.on("render-process-gone", (_event, details) => {
      logDesktopEvent("WARNING", "browser", "browser_tab_crashed", { tab_id: tabId, task_id: taskId, reason: details.reason });
      this.dismissDialog("the page's process ended");
      changed();
    });
    contents.ipc.on(BROWSER_PAGE_DIALOG_CHANNEL, (event, request: BrowserPageDialogRequest) => this.dialogRequested(event, request));
    // CDP 合成的鼠标与文字输入不触发 before-input-event，但合成键盘事件可能触发（实测：提交表单时回车的
    // keyUp）。所以动作执行期间及之后一小段时间里的输入事件归 Agent，其余才是用户在操作。
    contents.on("before-input-event", () => {
      if (Date.now() < this.agentInputUntil) return;
      this.lastUserInputAt = Date.now();
    });
    contents.once("destroyed", () => events.onGone());
  }

  get dialog(): BrowserDialog | null {
    return this.dialogs.current;
  }

  /** 渲染进程（操作系统进程号）：同进程的页面会被同一个弹窗一起停住。已销毁时为 null。 */
  get processId(): number | null {
    return this.contents.isDestroyed() ? null : this.contents.getOSProcessId();
  }

  /** 视图先交给 Host 登记，再异步初始化；关闭时的 signal 使所有初始化等待立即结束。 */
  async initialize(signal: AbortSignal): Promise<void> {
    const contents = this.contents;
    if (this.adopted) {
      // Electron 自己开始加载收养的 webContents：等第一次加载结束；页面加载中弹窗时，等它被答完（面板提示条
      // 或 Agent 都能答）再附加调试器——停着的页面上 CDP 命令回不来。迟迟不结束也不无限等。
      await waitForInitialization(untilStopped(contents, NAVIGATION_TIMEOUT_MS, this.gate.signal), signal);
      await waitForInitialization(this.dialogs.settled(), signal);
    } else {
      await waitForInitialization(contents.loadURL("about:blank"), signal);
    }
    signal.throwIfAborted();
    this.attachDebugger();
    await this.cdp("Page.enable", {}, signal);
    await this.cdp("Page.setInterceptFileChooserDialog", { enabled: true }, signal);
    // 没挂过窗口的视图收不到 CDP 合成的鼠标事件（实测：点击不触发、每个事件空等约 1.8 秒），开焦点模拟后正常。
    // Playwright 接管页面时自己会开它，所以桌面 E2E 测不出缺了这一步（见 browser-initialization 单测）。
    await this.cdp("Emulation.setFocusEmulationEnabled", { enabled: true }, signal);
    await this.viewport.start(signal);
  }

  /** 导航并等主框架加载完成；失败带回 Chromium 的 `ERR_*` 说明。待答的弹窗按取消处理（页面停着无法跳转）。 */
  async navigate(target: BrowserNavigation, signal: AbortSignal): Promise<void> {
    if (signal.aborted) throw new HostMethodError("CANCELLED", "navigation was cancelled");
    const contents = this.contents;
    const history = contents.navigationHistory;
    if ("action" in target && target.action === "back" && !history.canGoBack()) {
      throw new HostMethodError("NAVIGATION_FAILED", "there is no previous page in this tab");
    }
    if ("action" in target && target.action === "forward" && !history.canGoForward()) {
      throw new HostMethodError("NAVIGATION_FAILED", "there is no next page in this tab");
    }
    // 校验都过了才产生副作用：不能后退时不该白白取消弹窗。
    this.dismissDialog("the tab navigated");
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

  /**
   * 读整棵无障碍树并从 `fromLine` 起输出；ref 按节点稳定分配（`RefRegistry`），不随快照替换。
   * 本标签有待答弹窗时不读树（页面停着，读不到），只报弹窗；被同进程别的标签的弹窗停住时以 `DIALOG_OPEN` 失败。
   */
  async snapshot(fromLine = 1): Promise<PageSnapshot> {
    this.assertAlive();
    const formatted = this.dialogs.current
      ? { text: "", frames: 0 }
      : formatSnapshot((await this.cdp<{ nodes: AXNode[] }>("Accessibility.getFullAXTree")).nodes, this.refs, { fromLine });
    const contents = this.contents;
    const notes = this.notes;
    this.notes = [];
    return { ...formatted, url: contents.getURL(), title: contents.getTitle(), loading: contents.isLoading(), notes, dialog: this.dialogs.current };
  }

  /**
   * 在页面上按序执行动作（见 `page-input.ts`）。用户刚在这个页面上操作过时整批拒绝（`USER_ACTIVE`）：
   * 与终端「用户输入从不被拒、Agent 等用户停手」同一原则。
   */
  async act(actions: readonly PageAction[], signal: AbortSignal): Promise<ActResult> {
    this.assertAlive();
    if (Date.now() - this.lastUserInputAt < USER_ACTIVE_WINDOW_MS) {
      throw new HostMethodError("USER_ACTIVE", "the user is using this page right now");
    }
    this.agentInputUntil = Number.POSITIVE_INFINITY;
    try {
      return await runActions({
        cdp: (method, params) => this.cdp(method, params),
        resolveRef: (ref) => this.resolveRef(ref),
        settle: () => this.settle(),
        toInput: (point) => this.viewport.toInput(point),
        paused: () => this.events.pausedBy(),
        dialog: () => this.dialogs.current,
        answerDialog: (id, answer) => this.answerDialog(id, answer, "agent"),
      }, actions, signal);
    } finally {
      this.agentInputUntil = Date.now() + AGENT_INPUT_GRACE_MS;
    }
  }

  /** 回答 `id` 这个弹窗；它已不是当前弹窗（对方答过、换成了新的）时什么也不做。 */
  answerDialog(id: number, answer: BrowserDialogAnswer, by: BrowserTabCreator): void {
    const dialog = this.dialogs.answer(id, answer);
    if (!dialog) {
      logDesktopEvent("INFO", "browser", "browser_dialog_answer_stale", { tab_id: this.tabId, dialog_id: id, by });
      return;
    }
    logDesktopEvent("INFO", "browser", "browser_dialog_answered", { tab_id: this.tabId, task_id: this.taskId, type: dialog.type, accept: answer.accept, by });
    if (by === "user") this.note(`the user answered the page's ${dialog.type} dialog #${dialog.id} (${answer.accept ? "accepted" : "dismissed"})`);
    this.events.onChange();
    // 弹窗打开期间的设备模拟设置会被闸住：答完后若期望与已生效不一致再补设。
    this.viewport.resync();
  }

  /** 页面下一次停住（弹窗）时中止的信号：等初始化的调用方据它提前返回（回答弹窗不需要调试器）。 */
  get pausedSignal(): AbortSignal {
    return this.gate.signal;
  }

  /** 页面停住了（本标签或同进程别的标签弹窗）：让在途的 CDP 命令与加载等待立即以 `reason` 结束。 */
  interruptCdp(reason: HostMethodError): void {
    this.gate.abort(reason);
    this.gate = new AbortController();
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

  /** 挂到面板上：视图放在 `bounds`（窗口坐标，已按视口等比缩放），设备模拟按同一比例缩放。 */
  show(bounds: Rectangle): void {
    this.view.setBounds(bounds);
    this.viewport.show(bounds.width);
  }

  /** 从面板摘下：恢复与视口同大、不缩放，继续在后台工作。 */
  hide(): void {
    this.view.setBounds(DETACHED_BOUNDS);
    this.viewport.hide();
  }

  summary(): BrowserTabSummary {
    const contents = this.contents;
    return {
      tabId: this.tabId,
      taskId: this.taskId,
      createdBy: this.createdBy,
      openerId: this.openerId,
      url: contents.getURL(),
      title: contents.getTitle(),
      loading: contents.isLoading(),
      crashed: contents.isCrashed(),
      dialog: this.dialogs.current,
    };
  }

  note(text: string): void {
    this.notes.push(text);
    if (this.notes.length > MAX_NOTES) this.notes.splice(0, this.notes.length - MAX_NOTES, "(earlier notes were dropped)");
  }

  /** 页面已自己关掉（`destroyed`）时只做清理；待答的弹窗先按取消答复，免得同进程的其他页面跟着停住。 */
  close(): void {
    this.dismissDialog("the tab was closed");
    const contents = this.contents;
    if (contents.isDestroyed()) return;
    if (contents.debugger.isAttached()) contents.debugger.detach();
    contents.close();
  }

  /** 直接问 webContents：崩溃事件是异步到达的，崩溃后到事件之间发出的 CDP 命令会一直挂到超时。 */
  private assertAlive(): void {
    if (this.contents.isCrashed()) throw new HostMethodError("PAGE_CRASHED", "the page crashed");
  }

  private dialogRequested(event: Electron.IpcMainEvent, request: BrowserPageDialogRequest): void {
    const opening = this.dialogs.open(event, request);
    if ("refused" in opening) {
      // 已按取消答复：同一标签另一个框架同时弹窗（不排队），或网页 preload 之外的异常请求。
      logDesktopEvent("WARNING", "browser", "browser_dialog_refused", { tab_id: this.tabId, task_id: this.taskId, reason: opening.refused });
      if (opening.refused === "busy") this.note(`the page showed another ${request.type} dialog while one was waiting; it was dismissed`);
      return;
    }
    logDesktopEvent("INFO", "browser", "browser_dialog_opened", { tab_id: this.tabId, task_id: this.taskId, type: opening.dialog.type, dialog_id: opening.dialog.id });
    this.events.onDialogOpened();
    this.events.onChange();
  }

  private dismissDialog(reason: string): void {
    const dialog = this.dialogs.dismiss();
    if (!dialog) return;
    this.note(`the page's ${dialog.type} dialog #${dialog.id} was dismissed because ${reason}`);
    this.events.onChange();
  }

  /**
   * 动作之后：页面在加载（点了链接、提交了表单）就等它结束，有时限、超时不报错，再留一点渲染时间。
   * 页面停住（弹窗）时不再等：停着的页面不会加载完，结果里报出弹窗即可。
   */
  private async settle(): Promise<void> {
    const contents = this.contents;
    // 点击触发的导航是异步开始的：先等一下再看是否在加载。
    await new Promise((resolve) => setTimeout(resolve, SETTLE_MS));
    if (this.events.pausedBy()) return;
    if (contents.isLoading()) await untilStopped(contents, ACTION_SETTLE_TIMEOUT_MS, this.gate.signal);
    await new Promise((resolve) => setTimeout(resolve, SETTLE_MS));
  }

  private attachDebugger(): void {
    const { debugger: cdp } = this.contents;
    cdp.attach("1.3");
    cdp.on("message", (_event, method, params) => {
      if (method === "Page.fileChooserOpened") this.fileChooserOpened(params as { backendNodeId?: number; mode: string });
      // 走到 Electron 原生弹窗说明页面绕过了网页 preload（例如框架还没跑 preload），已被 `disableDialogs` 按取消处理。
      else if (method === "Page.javascriptDialogOpening") this.nativeDialog(params as { type: string });
    });
    cdp.on("detach", (_event, reason) => {
      logDesktopEvent("WARNING", "browser", "browser_debugger_detached", { tab_id: this.tabId, reason });
    });
  }

  private nativeDialog({ type }: { type: string }): void {
    this.note(`the page showed a ${type} dialog that could not be held for an answer; it was dismissed automatically`);
    logDesktopEvent("WARNING", "browser", "browser_native_dialog_dismissed", { tab_id: this.tabId, task_id: this.taskId, type });
  }

  /**
   * 选文件框按触发者分流：只有用户能点到的（标签挂在面板上）且不在 Agent 动作期间触发的，才弹系统选择框把
   * 选中的文件填进去；其余（Agent 触发、后台标签上页面自己触发）拦下并告知（Agent 上传尚未支持）。只看
   * 「不在 Agent 动作期间」不够：Agent 的点击给了页面约 5 秒的用户激活，页面可以在宽限之后才触发选文件框。
   */
  private fileChooserOpened({ backendNodeId, mode }: { backendNodeId?: number; mode: string }): void {
    if (!this.viewport.isShown || Date.now() < this.agentInputUntil || backendNodeId === undefined) {
      this.note("the page tried to open a file chooser; file uploads are not supported, so it was blocked");
      logDesktopEvent("INFO", "browser", "browser_file_chooser_blocked", { tab_id: this.tabId, task_id: this.taskId });
      return;
    }
    this.events.chooseFiles(mode === "selectMultiple").then(
      (files) => (files?.length ? this.cdp("DOM.setFileInputFiles", { files, backendNodeId }) : undefined),
    ).catch((error: unknown) => {
      logDesktopEvent("WARNING", "browser", "browser_file_chooser_failed", { tab_id: this.tabId, error: errorText(error) });
    });
  }

  private blockNonWebNavigation(event: Electron.Event, url: string): void {
    const protocol = urlProtocol(url);
    if (isWebProtocol(protocol)) return;
    event.preventDefault();
    this.note(`the page tried to navigate to a ${protocol || "non-web"} address; it was blocked`);
  }

  /**
   * 主框架加载完成（含同文档导航）即成功；主框架加载失败、超时或调用被撤销即失败。页面停住（弹窗）也算结束：
   * 弹窗挡住了 load 事件（实测：等满 30 秒超时），停着的页面由结果里的弹窗说明。
   */
  private waitForLoad(signal: AbortSignal): Promise<void> {
    const contents = this.contents;
    const paused = this.gate.signal;
    return new Promise((resolve, reject) => {
      const finish = (error?: HostMethodError) => {
        clearTimeout(timer);
        contents.off("did-finish-load", onLoad);
        contents.off("did-navigate-in-page", onInPage);
        contents.off("did-fail-load", onFail);
        signal.removeEventListener("abort", onAbort);
        paused.removeEventListener("abort", onLoad);
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
      paused.addEventListener("abort", onLoad, { once: true });
    });
  }

  /**
   * CDP 命令带时限：渲染进程卡死时命令会一直不返回。页面停着（弹窗）时命令同样等不到回复：新发的直接、
   * 在途的在页面停住那一刻（`interruptCdp`）失败，错误指明是哪个标签的弹窗。
   */
  private cdp<T = unknown>(method: string, params: Record<string, unknown> = {}, signal?: AbortSignal): Promise<T> {
    signal?.throwIfAborted();
    const paused = this.events.pausedBy();
    if (paused) return Promise.reject(paused);
    const gate = this.gate.signal;
    // 只在本模块与 page-input.ts 里用固定的方法集合，不向链路透传任意 CDP。
    const command = this.contents.debugger.sendCommand(method, params) as Promise<T>;
    return new Promise<T>((resolve, reject) => {
      const clear = () => {
        clearTimeout(timer);
        signal?.removeEventListener("abort", onAbort);
        gate.removeEventListener("abort", onPaused);
      };
      const fail = (error: unknown) => { clear(); reject(error); };
      const onAbort = () => fail(signal!.reason);
      const onPaused = () => fail(gate.reason);
      const timer = setTimeout(
        () => fail(new HostMethodError("PAGE_UNRESPONSIVE", `the page did not respond to ${method}`)),
        CDP_TIMEOUT_MS,
      );
      signal?.addEventListener("abort", onAbort, { once: true });
      gate.addEventListener("abort", onPaused, { once: true });
      command.then(
        (value) => { clear(); resolve(value); },
        fail,
      );
    });
  }
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** 等到页面停止加载、时限到或页面停住（三者先到为准，都不算失败）。 */
function untilStopped(contents: WebContents, timeoutMs: number, paused: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const done = () => {
      clearTimeout(timer);
      contents.off("did-stop-loading", done);
      paused.removeEventListener("abort", done);
      resolve();
    };
    const timer = setTimeout(done, timeoutMs);
    contents.on("did-stop-loading", done);
    paused.addEventListener("abort", done, { once: true });
  });
}

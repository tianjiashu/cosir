import { WebContentsView, type Rectangle, type WebContents, type WindowOpenHandlerResponse } from "electron";

import { logDesktopEvent } from "../desktop-log";
import type { HostMethodError } from "../host-link/methods";
import { browserPageWebPreferences } from "./browser-session";
import { CdpSession } from "./cdp-session";
import { browserError } from "./errors";
import { runActions, type ActResult } from "./input/actions";
import { box, element } from "./input/element";
import { BrowserOperation, type FileChooser } from "./operation";
import type { BrowserDialog, BrowserDialogAnswer, BrowserNavigation, BrowserTabCreator, BrowserTabSummary } from "../../shared/host-contract";
import { BROWSER_PAGE_DIALOG_CHANNEL, type BrowserPageDialogRequest } from "../../shared/browser-page-bridge";
import type { PageAction } from "./params";
import { captureScreenshot, type CaptureOptions } from "./page-capture";
import { PageDialogs } from "./page-dialogs";
import { evaluateExpression, type Evaluation } from "./page-evaluate";
import { ERR_ABORTED, navigationCompletion, untilStopped, waitForLoad } from "./page-loading";
import { PageFrames } from "./page-frames";
import { TabHighlight } from "./tab-highlight";
import { RefRegistry, type PageNode } from "./ref-registry";
import { buildOutline, type Outline } from "./snapshot/outline";
import { readSnapshot } from "./snapshot/read";
import { TabEmulation, type Applied, type Emulation, type EmulationRequest } from "./tab-emulation";
import { CANCELED_FAILURE, TabLog, type ConsoleLevel } from "./tab-log";
import { isWebProtocol, urlProtocol } from "./web-url";
import { abortable } from "./abortable";

const NAVIGATION_TIMEOUT_MS = 30_000;
/** 加载完成后再等一下：多数页面在 load 之后还会渲染首屏内容。 */
const SETTLE_MS = 250;
/** 动作触发导航后最多等多久加载结束；超时不算失败，快照会标「仍在加载」。 */
const ACTION_SETTLE_TIMEOUT_MS = 15_000;
/** 用户最近一次输入后多久内 Agent 的动作被拒（设计稿决定记录：2 秒）。 */
const USER_ACTIVE_WINDOW_MS = 2_000;
/** 动作结束后多久内到达的输入事件仍算 Agent 的（合成按键的事件可能稍晚才派发）。 */
const AGENT_INPUT_GRACE_MS = 500;
/** 网络请求的响应体留在渲染进程里的上限：日志只记元数据、从不取响应体，给个最小值免得白占内存。 */
const NETWORK_BUFFER_BYTES = 1024;
/** 开发期 Electron 往每个页面的 console 注入的安全警告：与网页无关，不进日志。 */
const ELECTRON_NOISE = /^%cElectron Security Warning/;

/** 读到的页面：快照大纲（怎么呈现由拿结果的一方选视图，见 `snapshot/views.ts`），或挡住页面的弹窗。 */
export type PageSnapshot = {
  /** 本标签有待答弹窗时为 null：页面停着，读不到无障碍树。 */
  outline: Outline | null;
  document: number;
  dialog: BrowserDialog | null;
};

/** 上一次交给 Agent 的大纲，及它属于第几个文档：增量快照的比对基准（见 `BrowserTab.deliver`）。 */
type Delivered = { document: number; outline: Outline };

/** 执行 JS 的结果：值或脚本错误（见 `page-evaluate.ts`），或脚本弹出的弹窗（此时页面停住，值与错误都为空）。 */
export type EvaluateOutcome = Evaluation & { dialog: BrowserDialog | null };

export type TabEvents = {
  /** 页面要开新窗口：由 `BrowserHost` 决定拒绝，或收养成同 Task 的新标签（保留 opener）。 */
  onWindowOpen: (url: string) => WindowOpenHandlerResponse;
  /** 摘要里的任一项变了（地址、标题、加载、崩溃、弹窗）：`BrowserHost` 据此推送状态。 */
  onChange: () => void;
  /** webContents 不经 `BrowserHost` 就没了（页面自己 `window.close()`）：`BrowserHost` 据此注销标签。 */
  onGone: () => void;
  /** 本标签弹窗了：整个渲染进程停住，`BrowserHost` 让同进程标签在途的 CDP 立即失败。 */
  onDialogOpened: () => void;
  /** 本标签的弹窗答完或被取消了：`BrowserHost` 让同进程标签补设弹窗期间被闸住的设备模拟。 */
  onDialogClosed: () => void;
  /** 页面为什么停着（本标签或同进程别的标签有待答弹窗）；没停时为 null。由 `BrowserHost` 按进程判断。 */
  pausedBy: () => HostMethodError | null;
  /** 用户在面板里点了选文件：弹系统文件选择框，返回选中的本机路径，取消为 null。 */
  chooseFiles: (multiple: boolean) => Promise<string[] | null>;
  /**
   * 本标签此刻挂在浏览器面板上（用户看得见、点得到）。事实归 `BrowserHost` 的摆放（同一时刻只挂一个）；标签据此决定
   * 画不画高亮、网页自己打开的选文件框弹给用户还是拦下。不从设备模拟的占位矩形推断——那只是摆放参数。
   */
  isAttached: () => boolean;
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
 * 一个标签页：一个 `WebContentsView`，组合几个各管一件事的部件——CDP 会话（`CdpSession`，命令唯一出口与闸门、
 * 事件唯一入口）、跨进程子框架（`PageFrames`：子会话与坐标偏移）、弹窗（`PageDialogs`）、设备模拟与显示缩放
 * （`TabEmulation`）、页面日志（`TabLog`：备注、console、网络，「上次以来」的唯一事实源）、ref 登记（`RefRegistry`）、
 * 给人看的高亮框（`TabHighlight`）。本类负责生命周期、把页面事件接到这些部件上，以及需要多个部件配合的操作（导航、
 * 快照、动作、执行 JS、截图与元素截图）。
 *
 * 两种来源：新建（先加载 `about:blank` 再附加调试器——未加载过任何页面的 webContents 上 CDP 命令会一直挂起），
 * 与收养页面开出的 webContents（等它第一次加载结束、弹窗被答完再附加）；之后两者没有差别。
 *
 * 页面停住（JS 弹窗，整个渲染进程的 CDP 都回不来）：`CdpSession` 是唯一闸门，等加载的地方同样据它提前结束；
 * 「为什么停着」由 `BrowserHost` 按进程判断。
 *
 * console 用 Electron 的 `console-message`（实测覆盖主框架、iframe、跨进程 iframe、未捕获异常与未处理的拒绝），
 * 不开 CDP 的 `Runtime.enable`——它是网站识别自动化的常见指纹。网络请求用 CDP `Network` 事件，只在主会话上开
 * （跨进程子框架的子会话不开），所以跨进程 iframe（跨站嵌入）与 worker 的请求不在日志里。主文档状态码取自 `did-navigate`（不依赖
 * `Network`，404 照报、重定向报最终的码）。
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
  readonly log = new TabLog();
  private readonly adopted: boolean;
  private readonly events: TabEvents;
  readonly session: CdpSession;
  private readonly refs = new RefRegistry();
  readonly frames: PageFrames;
  readonly highlighter: TabHighlight;
  private readonly dialogs = new PageDialogs();
  readonly emulation: TabEmulation;
  /** 最近一次真实用户输入的时间（毫秒）；`USER_ACTIVE` 据此判断。 */
  private lastUserInputAt = 0;
  /** 在此之前到达的输入事件来自 Agent 自己的动作，不算用户输入（见构造函数里的说明）。 */
  private agentKeyUntil = 0;
  private agentChooserUntil = 0;
  initialized = false;
  operation: BrowserOperation | null = null;
  /** 当前主文档的 HTTP 状态码（`did-navigate`）；about:blank 等没有时为 null。 */
  private documentStatus: number | null = null;
  /** 主框架换过几次文档：增量快照只在同一文档里比对。 */
  private document = 0;
  private delivered: Delivered | null = null;
  /** Agent 的动作让页面打开、等 `upload` 回答的选文件框；换文档或页面再开一个时作废 / 换掉。 */
  fileChooser: FileChooser | null = null;

  constructor(options: TabOptions) {
    const { tabId, taskId, events } = options;
    this.tabId = tabId;
    this.taskId = taskId;
    this.createdBy = options.createdBy;
    this.openerId = options.openerId;
    this.downloadDir = options.downloadDir;
    this.events = events;
    this.adopted = options.adopted !== undefined;
    // 收养时也要传同一份 webPreferences：实测只给 `overrideBrowserWindowOptions` 时，网页 preload 在收养的页面里
    // 不生效（弹窗走原生路径被自动取消）。
    this.view = new WebContentsView({ webPreferences: browserPageWebPreferences(), ...(options.adopted ? { webContents: options.adopted } : {}) });
    this.contents = this.view.webContents;
    this.session = new CdpSession(this.contents, events.pausedBy, (reason) => {
      logDesktopEvent("WARNING", "browser", "browser_debugger_detached", { tab_id: tabId, reason });
    }, (command) => this.operation?.track(command));
    this.frames = new PageFrames(this.session, tabId);
    this.highlighter = new TabHighlight(this.session, tabId, events.isAttached);
    this.emulation = new TabEmulation(
      (method, params, sendOptions) => this.session.send(method, params, sendOptions),
      (error) => logDesktopEvent("INFO", "browser", "browser_viewport_sync_failed", { tab_id: tabId }, error),
      (applied) => this.metricsApplied(applied),
    );
    this.view.setBounds(this.emulation.viewBounds);
    const contents = this.contents;
    contents.setBackgroundThrottling(false);
    contents.setWindowOpenHandler(({ url }) => events.onWindowOpen(url));
    contents.on("will-navigate", (event, url) => this.blockNonWebNavigation(event, url));
    contents.on("will-redirect", (event, url) => this.blockNonWebNavigation(event, url));
    contents.on("will-prevent-unload", (event) => {
      // 不放行时跳转会被静默取消（Agent 的导航只会超时）；没有可以作答的界面，按「离开」处理并告知。
      event.preventDefault();
      this.log.note("the page asked to confirm leaving it (unsaved changes?); it was left anyway");
    });
    contents.on("did-start-navigation", (details) => {
      if (!details.isMainFrame || details.isSameDocument) return;
      this.log.navigationStarted();
    });
    // 主框架换了文档：提交成功（`did-navigate`），或导航失败、换成错误页（Electron 对错误页只发 `did-fail-load`）。
    // 主框架的 `did-fail-load` 也可能来自已提交文档的加载中途失败（响应体断开），那时文档没换：只认还没提交的导航。
    contents.on("did-navigate", (_event, _url, httpResponseCode) => this.documentChanged(httpResponseCode > 0 ? httpResponseCode : null));
    contents.on("did-fail-load", (_event, code, _description, _url, isMainFrame) => {
      if (isMainFrame && code !== ERR_ABORTED && this.log.navigating) this.documentChanged(null);
    });
    // 加载状态结束时导航还没换上文档：浏览器放弃了它，页面还是原来的（不论是谁发起的：Agent、用户或页面自己）。
    // 「导航进行中」只在标签日志里记一份（`TabLog.navigating`），开始、落定都经它。
    contents.on("did-stop-loading", () => this.log.navigationAbandoned());
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
    contents.on("console-message", (details) => this.consoleMessage(details));
    contents.ipc.on(BROWSER_PAGE_DIALOG_CHANNEL, (event, request: BrowserPageDialogRequest) => this.dialogRequested(event, request));
    // CDP 合成的鼠标与文字输入不触发 before-input-event，但合成键盘事件可能触发（实测：提交表单时回车的
    // keyUp）。仅在合成键盘派发后的短时间窗抑制误判；它只是近期键盘输入启发式，可靠接手依靠显式 handoff。
    contents.on("before-input-event", () => {
      if (Date.now() < this.agentKeyUntil) return;
      this.lastUserInputAt = Date.now();
    });
    contents.once("destroyed", () => events.onGone());
    this.subscribeCdp();
  }

  get dialog(): BrowserDialog | null {
    return this.dialogs.current;
  }

  /** 渲染进程（操作系统进程号）：同进程的页面会被同一个弹窗一起停住。已销毁时为 null。 */
  get processId(): number | null {
    return this.contents.isDestroyed() ? null : this.contents.getOSProcessId();
  }

  /** 页面上已生效的设备模拟（页面结果里报告的就是它）。 */
  get currentEmulation(): Emulation {
    return this.emulation.current;
  }

  get status(): number | null {
    return this.documentStatus;
  }

  /** 视图先交给 Host 登记，再异步初始化；关闭时的 signal 使所有初始化等待立即结束。 */
  async initialize(signal: AbortSignal): Promise<void> {
    const contents = this.contents;
    if (this.adopted) {
      // Electron 自己开始加载收养的 webContents：等第一次加载结束；页面加载中弹窗时，等它被答完（面板提示条
      // 或 Agent 都能答）再附加调试器——停着的页面上 CDP 命令回不来。迟迟不结束也不无限等。
      await untilStopped(contents, NAVIGATION_TIMEOUT_MS, AbortSignal.any([this.session.pausedSignal, signal]));
      signal.throwIfAborted();
      await abortable(this.dialogs.settled(), signal);
    } else {
      await abortable(contents.loadURL("about:blank"), signal);
    }
    signal.throwIfAborted();
    this.session.attach();
    await this.session.send("Page.enable", {}, { signal });
    await this.session.send("Page.setInterceptFileChooserDialog", { enabled: true }, { signal });
    await this.session.send("Network.enable", { maxTotalBufferSize: NETWORK_BUFFER_BYTES, maxResourceBufferSize: NETWORK_BUFFER_BYTES }, { signal });
    await this.frames.start(signal);
    // 没挂过窗口的视图收不到 CDP 合成的鼠标事件（实测：点击不触发、每个事件空等约 1.8 秒），开焦点模拟后正常。
    // Playwright 接管页面时自己会开它，所以桌面 E2E 测不出缺了这一步（见 browser-initialization 单测）。
    await this.session.send("Emulation.setFocusEmulationEnabled", { enabled: true }, { signal });
    await this.emulation.start(signal);
    // 收养的 webContents 在 Electron 接手后，内部网页视图不按外层视图的 bounds 布局（实测：铺到窗口右下角，
    // 盖出浏览器面板），要等一次真实的尺寸变化才纠正；接手完成于第一次加载结束，所以在这里补一次。
    if (this.adopted) this.relayout();
    this.initialized = true;
  }

  /**
   * 导航并等它落定（见 `waitForLoad`）；失败带回 Chromium 的 `ERR_*` 说明，没有打开新页面时留一条备注、按成功返回
   * 原页面（模型据此知道去哪里找下载，而不是当作失败重试、再下一次）。顺序：待答的弹窗按取消处理（页面停着无法跳转，
   * 也设不了模拟）→ 设上要求改的模拟（只改给了的项，生效后才加载，页面按新视口布局）→ 加载。
   */
  async navigate(target: BrowserNavigation, operation: BrowserOperation, emulation?: EmulationRequest): Promise<void> {
    const signal = operation.signal;
    if (signal.aborted) throw browserError("CANCELLED", "navigation was cancelled");
    const contents = this.contents;
    const history = contents.navigationHistory;
    if ("action" in target && target.action === "back" && !history.canGoBack()) {
      throw browserError("NAVIGATION_FAILED", "there is no previous page in this tab");
    }
    if ("action" in target && target.action === "forward" && !history.canGoForward()) {
      throw browserError("NAVIGATION_FAILED", "there is no next page in this tab");
    }
    // 校验都过了才产生副作用：不能后退时不该白白取消弹窗。
    this.dismissDialog("the tab navigated");
    // 被同进程别的标签的弹窗停住时跳转等不到页面配合，会等满时限：直接说明原因。
    const paused = this.events.pausedBy();
    if (paused) throw paused;
    if (emulation) {
      try { await this.emulation.set(emulation, signal); }
      finally { operation.track(this.emulation.settled()); }
    }
    signal.throwIfAborted();
    const loaded = waitForLoad(contents, {
      signal,
      paused: this.session.pausedSignal,
      timeoutMs: NAVIGATION_TIMEOUT_MS,
      log: { tab_id: this.tabId, task_id: this.taskId },
    });
    operation.track(navigationCompletion(contents));
    if ("url" in target) {
      // 结果以加载事件为准：loadURL 自己的拒绝（含重定向造成的 ERR_ABORTED）在这里不重复处理。
      operation.track(contents.loadURL(target.url));
    } else if (target.action === "back") history.goBack();
    else if (target.action === "forward") history.goForward();
    else contents.reload();
    if (!await loaded) {
      const asked = "url" in target ? `opening ${target.url.slice(0, 200)}` : `going ${target.action === "reload" ? "to reload" : target.action}`;
      this.log.note(`${asked} did not open a new page (it may have started a download, returned no content or been blocked); the tab stays on ${contents.getURL().slice(0, 200)}`);
    }
    await new Promise((resolve) => setTimeout(resolve, SETTLE_MS));
  }

  /**
   * 这次读到的大纲交给了 Agent：换成新的比对基准，返回上一次交的（换过文档时为 null，跨文档比对没有意义）。
   * 只由组装页面结果的一方调用（`BrowserHost.result`，与日志的「上次以来」同一个消费点）；被弹窗挡住、没读到大纲时
   * 基准不变。
   */
  deliver(outline: Outline): Outline | null {
    const previous = this.delivered;
    this.delivered = { document: this.document, outline };
    return previous?.document === this.document ? previous.outline : null;
  }

  /**
   * 读整棵无障碍树（含展开的子框架，见 `snapshot/read.ts`）建成大纲返回；怎么呈现（整页分页、视口内、查找、增量）由
   * 拿结果的一方选（`snapshot/views.ts`）。ref 按节点稳定分配（`RefRegistry`），不随快照替换。不碰页面日志与比对
   * 基准：「上次以来」由组装页面结果的一方显式取（`log.digest`、`deliver`）。
   * 本标签有待答弹窗时不读树（页面停着，读不到），只报弹窗；被同进程别的标签的弹窗停住时以 `DIALOG_OPEN` 失败。
   */
  async snapshot(): Promise<PageSnapshot> {
    this.assertAlive();
    if (this.dialogs.current) return { outline: null, document: this.document, dialog: this.dialogs.current };
    const generation = this.document;
    const document = await readSnapshot(this.session, this.frames, (error) => {
      logDesktopEvent("INFO", "browser", "browser_frame_read_failed", { tab_id: this.tabId }, error);
    });
    return { document: generation, outline: buildOutline(document, this.refs, { pageUrl: this.contents.getURL() }), dialog: this.dialogs.current };
  }

  /** 修改槽位由标签持有；未知输入不允许新操作覆盖。 */
  async modify<T>(signal: AbortSignal, run: (operation: BrowserOperation) => Promise<T>): Promise<T> {
    if (this.contents.isDestroyed()) throw browserError("TAB_NOT_FOUND", "the tab was closed");
    const paused = this.paused();
    if (this.operation && paused) throw paused;
    if (this.operation) throw browserError(this.operation.state === "unknown" ? "OPERATION_UNKNOWN" : "TAB_BUSY", "the previous operation has not settled");
    signal.throwIfAborted();
    const operation = new BrowserOperation(this, signal);
    this.operation = operation;
    this.events.onChange();
    try { return await run(operation); }
    finally { await operation.finish(); }
  }

  operationChanged(): void { this.events.onChange(); }
  operationSettled(operation: BrowserOperation): void {
    if (this.operation !== operation) return;
    this.operation = null;
    this.events.onChange();
  }
  get documentGeneration(): number { return this.document; }
  paused(): HostMethodError | null { return this.events.pausedBy(); }
  refLabel(ref: string): string | undefined { return this.refs.label(ref); }
  noteAgentInput(method: string): void {
    this.agentChooserUntil = Date.now() + AGENT_INPUT_GRACE_MS;
    if (method === "Input.dispatchKeyEvent") this.agentKeyUntil = Date.now() + AGENT_INPUT_GRACE_MS;
  }

  /** USER_ACTIVE 仅为近期键盘活动提示；明确交接由请求准入负责。 */
  async act(actions: readonly PageAction[], signal: AbortSignal): Promise<ActResult> {
    this.assertAlive();
    if (Date.now() - this.lastUserInputAt < USER_ACTIVE_WINDOW_MS) throw browserError("USER_ACTIVE", "the user recently used this page");
    return this.modify(signal, async (operation) => {
      const ready = this.emulation.prepareInput(operation.signal);
      operation.track(this.emulation.settled());
      await ready;
      return runActions(operation, actions, operation.signal);
    });
  }

  /** 弹窗答复不占修改槽位，否则被弹窗停住的输入无法收尾。 */
  async actDialog(action: Extract<PageAction, { type: "dialog" }>, signal: AbortSignal): Promise<ActResult> {
    const operation = new BrowserOperation(this, signal);
    try { return await runActions(operation, [action], signal); }
    finally { await operation.finish(); }
  }

  /**
   * 执行 JS（见 `page-evaluate.ts`）。页面已经停着（本标签或同进程别的标签在等弹窗作答）时整次以 `DIALOG_OPEN`
   * 失败；所以执行途中以 `DIALOG_OPEN` 结束、且本标签有弹窗时，弹窗必是这次执行期间弹出的，算脚本弹的。
   */
  async evaluate(expression: string, operation: BrowserOperation): Promise<EvaluateOutcome> {
    const signal = operation.signal;
    this.assertAlive();
    const paused = this.events.pausedBy();
    if (paused) throw paused;
    try {
      return { ...await operation.effect(() => evaluateExpression(operation.cdp, expression, signal)), dialog: null };
    } catch (error) {
      const dialog = this.dialogs.current;
      if (dialog && (error as HostMethodError).code === "DIALOG_OPEN") return { value: null, truncated: false, error: null, dialog };
      throw error;
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
    if (by === "user") this.log.note(`the user answered the page's ${dialog.type} dialog #${dialog.id} (${answer.accept ? "accepted" : "dismissed"})`);
    this.events.onChange();
    this.events.onDialogClosed();
  }

  /** 期望与已生效的设备模拟不一致时补设（同进程的弹窗答完后由 `BrowserHost` 调用：弹窗期间的设置会被闸住）。 */
  resyncEmulation(): void {
    this.emulation.resync();
    this.operation?.resumeCleanup();
  }

  /** 页面下一次停住（弹窗）时中止的信号：等初始化的调用方据它提前返回（回答弹窗不需要调试器）。 */
  get pausedSignal(): AbortSignal {
    return this.session.pausedSignal;
  }

  /** 页面停住了（本标签或同进程别的标签弹窗）：让在途的 CDP 命令与加载等待立即以 `reason` 结束。 */
  interruptCdp(reason: HostMethodError): void {
    this.session.interrupt(reason);
  }

  /** ref → 节点；从没登记过（含导航前的 ref）报 `REF_STALE`。节点是否还在页面上由 `input/element.ts` 确认。 */
  resolveRef(ref: string): PageNode {
    const node = this.refs.resolve(ref);
    if (!node) throw browserError("REF_STALE", `ref ${ref} is not on the current page; take a new snapshot`);
    return node;
  }

  /** 截图前先收起高亮框：它会进 CDP 截图（实测），Agent 与冻结帧都不该截到它。 */
  async screenshot(format: "jpeg" | "png", options: CaptureOptions = {}): Promise<string> {
    this.assertAlive();
    await this.highlighter.clear();
    return captureScreenshot(this.session.send.bind(this.session), format, options);
  }

  /** 只截一个元素（先滚入视野再量，与动作取落点同一套几何）；元素已不在、没有尺寸时报 `REF_STALE` / `NOT_INTERACTABLE`。 */
  async elementScreenshot(ref: string, operation: BrowserOperation): Promise<string> {
    this.assertAlive();
    const session = operation;
    const area = await box(session, await element(session, ref));
    await this.highlighter.clear();
    return captureScreenshot(operation.cdp, "jpeg", { quality: 85, element: area });
  }

  /** 主框架换了文档：旧 ref 作废、状态码换成新文档的（错误页为 null）、日志的「当前页面」从这里起算。 */
  private documentChanged(status: number | null): void {
    this.refs.clear();
    this.document += 1;
    this.fileChooser = null;
    this.documentStatus = status;
    this.log.pageStarted();
  }

  /** 视口与缩放生效了：视图 bounds 跟上（视口变了时长宽比随之变；后台视图要与视口同大，合成输入才落在视图之内）。 */
  private metricsApplied({ width, height, mobile, scale }: Applied): void {
    logDesktopEvent("INFO", "browser", "browser_viewport_applied", { tab_id: this.tabId, width, height, mobile, scale });
    this.view.setBounds(this.emulation.viewBounds);
  }

  /** 挂到面板上的占位矩形 `box`（窗口坐标）：跟随面板时视口随之、固定时按视口等比缩放（见 `TabEmulation`）。 */
  show(box: Rectangle): void {
    this.emulation.show(box);
    this.view.setBounds(this.emulation.viewBounds);
  }

  /** 从面板摘下：恢复与视口同大、不缩放，继续在后台工作。 */
  hide(): void {
    this.emulation.hide();
    this.view.setBounds(this.emulation.viewBounds);
  }

  /** 标签自己的事实；「Agent 正在执行」由 `BrowserHost` 按进行中的操作补上。 */
  summary(): Omit<BrowserTabSummary, "agentBusy"> {
    const contents = this.contents;
    return {
      operation: this.operation?.state ?? null,
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

  /** 页面已自己关掉（`destroyed`）时只做清理；待答的弹窗先按取消答复，免得同进程的其他页面跟着停住。 */
  close(): void {
    this.operation?.stop();
    this.highlighter.dispose();
    this.emulation.dispose();
    this.dismissDialog("the tab was closed");
    if (this.contents.isDestroyed()) return;
    this.session.detach();
    this.contents.close();
  }

  /** 让视图经历一次真实的尺寸变化再回到原 bounds：Electron 只在尺寸变化时重新布局内部网页视图。 */
  private relayout(): void {
    const bounds = this.view.getBounds();
    this.view.setBounds({ ...bounds, height: bounds.height + 1 });
    this.view.setBounds(bounds);
  }

  /** 直接问 webContents：崩溃事件是异步到达的，崩溃后到事件之间发出的 CDP 命令会一直挂到超时。 */
  private assertAlive(): void {
    if (this.contents.isCrashed()) throw browserError("PAGE_CRASHED", "the page crashed");
  }

  /** CDP 事件只在这里订阅：选文件框、绕过 preload 的原生弹窗、网络请求的生命周期、跨进程子框架的附加与分离。 */
  private subscribeCdp(): void {
    const session = this.session;
    session.on("Target.attachedToTarget", (params: Parameters<PageFrames["attached"]>[0], sessionId) => this.frames.attached(params, sessionId));
    session.on("Target.detachedFromTarget", (params: { sessionId: string }) => this.frames.detached(params));
    session.on("Page.fileChooserOpened", (params: { backendNodeId?: number; mode: string }, sessionId) => this.fileChooserOpened(params, sessionId));
    // 走到 Electron 原生弹窗说明页面绕过了网页 preload（例如框架还没跑 preload），已被 `disableDialogs` 按取消处理。
    session.on("Page.javascriptDialogOpening", ({ type }: { type: string }) => {
      this.log.note(`the page showed a ${type} dialog that could not be held for an answer; it was dismissed automatically`);
      logDesktopEvent("WARNING", "browser", "browser_native_dialog_dismissed", { tab_id: this.tabId, task_id: this.taskId, type });
    });
    type Request = { requestId: string; type?: string; timestamp: number; request: { method: string; url: string }; redirectResponse?: { status: number } };
    session.on("Network.requestWillBeSent", ({ requestId, type, timestamp, request, redirectResponse }: Request) => {
      // 重定向：同一 requestId 以新地址再发一次，上一跳带着它的状态码结束。
      if (redirectResponse) this.log.responseReceived(requestId, redirectResponse.status, timestamp, true);
      this.log.requestStarted(requestId, { method: request.method, url: request.url, resourceType: type ?? "Other", timestamp });
    });
    session.on("Network.responseReceived", ({ requestId, timestamp, response }: { requestId: string; timestamp: number; response: { status: number } }) => {
      this.log.responseReceived(requestId, response.status, timestamp);
    });
    session.on("Network.loadingFinished", ({ requestId, timestamp }: { requestId: string; timestamp: number }) => {
      this.log.requestFinished(requestId, timestamp);
    });
    session.on("Network.loadingFailed", ({ requestId, timestamp, errorText, canceled }: { requestId: string; timestamp: number; errorText: string; canceled?: boolean }) => {
      this.log.requestFailed(requestId, canceled ? CANCELED_FAILURE : errorText, timestamp);
    });
  }

  private consoleMessage(details: Electron.Event<{ level: string; message: string; lineNumber: number; sourceId: string; frame: Electron.WebFrameMain | null }>): void {
    if (ELECTRON_NOISE.test(details.message)) return;
    const frame = details.frame;
    this.log.console({
      level: CONSOLE_LEVELS[details.level] ?? "info",
      text: details.message,
      source: details.sourceId ? `${details.sourceId}:${details.lineNumber}` : null,
      frame: frame && !frame.isDestroyed() && frame !== this.contents.mainFrame ? frame.url : null,
    });
  }

  private dialogRequested(event: Electron.IpcMainEvent, request: BrowserPageDialogRequest): void {
    const opening = this.dialogs.open(event, request);
    if ("refused" in opening) {
      // 已按取消答复：同一标签另一个框架同时弹窗（不排队），或网页 preload 之外的异常请求。
      logDesktopEvent("WARNING", "browser", "browser_dialog_refused", { tab_id: this.tabId, task_id: this.taskId, reason: opening.refused });
      if (opening.refused === "busy") this.log.note(`the page showed another ${request.type} dialog while one was waiting; it was dismissed`);
      return;
    }
    logDesktopEvent("INFO", "browser", "browser_dialog_opened", { tab_id: this.tabId, task_id: this.taskId, type: opening.dialog.type, dialog_id: opening.dialog.id });
    this.events.onDialogOpened();
    this.events.onChange();
  }

  private dismissDialog(reason: string): void {
    const dialog = this.dialogs.dismiss();
    if (!dialog) return;
    this.log.note(`the page's ${dialog.type} dialog #${dialog.id} was dismissed because ${reason}`);
    this.events.onChange();
    this.events.onDialogClosed();
  }

  /**
   * 动作之后：页面在加载（点了链接、提交了表单）就等它结束，有时限、超时不报错，再留一点渲染时间。
   * 页面停住（弹窗）、调用取消或标签关闭（`signal`）时不再等：结果里报出弹窗，或由 `BrowserHost` 报关闭。
   */
  async settle(signal: AbortSignal): Promise<void> {
    const contents = this.contents;
    // 点击触发的导航是异步开始的：先等一下再看是否在加载。
    await new Promise((resolve) => setTimeout(resolve, SETTLE_MS));
    if (this.events.pausedBy() || signal.aborted) return;
    if (contents.isLoading()) await untilStopped(contents, ACTION_SETTLE_TIMEOUT_MS, AbortSignal.any([signal, this.session.pausedSignal]));
    await new Promise((resolve) => setTimeout(resolve, SETTLE_MS));
  }

  /**
   * 选文件框按触发者分流：Agent 动作期间（含其后的宽限）打开的，记下等 `upload` 回答（`input/upload.ts`），并给 Agent
   * 留备注；只有用户能点到的（标签挂在面板上）其余选文件框才弹系统选择框把选中的文件填进去；后台标签上页面自己
   * 打开的拦下并告知。只看「不在 Agent 动作期间」不够：Agent 的点击给了页面约 5 秒的用户激活，页面可以在宽限之后
   * 才打开选文件框，这时它挂在面板上就交给用户。
   */
  private fileChooserOpened({ backendNodeId, mode }: { backendNodeId?: number; mode: string }, sessionId: string | undefined): void {
    const multiple = mode === "selectMultiple";
    if (backendNodeId !== undefined && Date.now() < this.agentChooserUntil) {
      this.fileChooser = { node: { sessionId, backendNodeId }, multiple };
      this.log.note(`the page opened a file chooser${multiple ? " for several files" : ""}; answer it with an upload action without a ref`);
      logDesktopEvent("INFO", "browser", "browser_file_chooser_for_agent", { tab_id: this.tabId, task_id: this.taskId, multiple });
      return;
    }
    if (!this.events.isAttached() || backendNodeId === undefined) {
      this.log.note("the page opened a file chooser on its own; it was blocked");
      logDesktopEvent("INFO", "browser", "browser_file_chooser_blocked", { tab_id: this.tabId, task_id: this.taskId });
      return;
    }
    this.events.chooseFiles(multiple).then(
      (files) => (files?.length ? this.session.send("DOM.setFileInputFiles", { files, backendNodeId }, { sessionId }) : undefined),
    ).catch((error: unknown) => {
      logDesktopEvent("WARNING", "browser", "browser_file_chooser_failed", { tab_id: this.tabId }, error);
    });
  }

  private blockNonWebNavigation(event: Electron.Event, url: string): void {
    const protocol = urlProtocol(url);
    if (isWebProtocol(protocol)) return;
    event.preventDefault();
    this.log.note(`the page tried to navigate to a ${protocol || "non-web"} address; it was blocked`);
  }
}

/** Electron `console-message` 的级别名 → 日志级别。 */
const CONSOLE_LEVELS: Record<string, ConsoleLevel> = { debug: "debug", info: "info", warning: "warning", error: "error" };


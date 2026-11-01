import { dialog, shell, type BrowserWindow, type DownloadItem, type Rectangle, type WebContents, type WindowOpenHandlerResponse } from "electron";

import { logDesktopEvent } from "../desktop-log";
import { HostMethodError } from "../host-link/methods";
import type {
  BrowserDialog,
  BrowserDialogAnswer,
  BrowserNavigation,
  BrowserPlacement,
  BrowserState,
  BrowserTabContext,
  BrowserTabCreator,
  BrowserTabLimits,
} from "../../shared/host-contract";
import { BrowserTab, type PageSnapshot } from "./browser-tab";
import { browserPageWebPreferences, type BrowserSessionEvents } from "./browser-session";
import { BrowserDownloads } from "./downloads";
import type { ActResult } from "./page-input";
import type { ActRequest, NavigateRequest, ScreenshotRequest, SnapshotRequest, TabsRequest } from "./params";
import { VIEWPORT } from "./tab-viewport";
import { waitForInitialization } from "./initialization";
import { isWebProtocol, urlProtocol } from "./web-url";

/** 弹窗的线上形状（后端 `common.page_observation` 读取）。 */
type PageDialog = { id: number; type: BrowserDialog["type"]; message: string; default_text: string };

/** 一次导航或快照的结果；字段名与后端 `app/tools/tool_handler/browser/` 读取的一致。 */
export type PageResult = {
  tab_id: string;
  url: string;
  title: string;
  loading: boolean;
  /** 有待答的弹窗时为空串（页面停着，读不到）。 */
  snapshot: string;
  frames: number;
  notes: string[];
  /** 本 Task 的其他标签 id（模型可经 `tab` 参数切过去）。 */
  other_tabs: string[];
  dialog: PageDialog | null;
};

/** 一次动作序列的结果：执行到哪一步、停在哪（若有），以及之后的页面。 */
export type ActOutcome = PageResult & { completed: number; failure: ActResult["failure"] };

export type ScreenshotResult = { tab_id: string; url: string; title: string; image: string };

/** 本 Task 的一个标签（`browser_tabs` 的列表项）。 */
export type TaskTab = { tab_id: string; title: string; url: string; current: boolean; created_by: BrowserTabCreator };

type TabEntry = { tab: BrowserTab; ready: Promise<void>; controller: AbortController };

/**
 * 内置浏览器的唯一所有者：含初始化作业的标签登记、全部 `BrowserTab`、下载登记（`BrowserDownloads`）与
 * 「哪个视图挂在窗口上」。宿主工具链路的方法（`methods.ts`，Agent 用）与渲染进程 IPC（`ipc.ts`，用户用）
 * 都只经这里操作，不各自持有视图。
 *
 * 标签页与下载都是宿主进程事实：不持久化，后端重启不影响，宿主退出即全部消失；Task 删除后由后端通知
 * `dropTasks` 回收。入参已由 `params.ts` / `ipc.ts` 校验，这里直接信任。
 *
 * 标签的两种来源都在这里登记（`register`）：新开（Agent 导航、用户点「+」）与收养页面开出的窗口
 * （`window.open` / `target=_blank`，保留 opener，弹窗式登录才能把结果传回来源页）。注销只有一条路 `close`：
 * 宿主关闭、Task 删除，以及页面自己 `window.close()`（`gone`）。
 *
 * 跨标签的事在这里判断：分区级事件（下载、权限）按来源网页交给所属标签或 Task（实现 `BrowserSessionEvents`）；
 * JS 弹窗停住的是整个渲染进程，「页面为什么停着」按进程判断（`pausedBy`），弹窗打开时同进程标签在途的
 * CDP 一起失败。
 *
 * 摆放：同一时刻至多一个视图挂在主窗口上（浏览器面板只渲染激活标签）。挂上新视图时摘下旧的；摘下的视图
 * 恢复视口大小继续在后台工作。视图在占位矩形里按视口等比缩放、居中（渲染进程的冻结帧用同一规则：
 * `object-contain`）。摆放状态只有这里一处事实，渲染进程只上报「我想把哪个标签放在哪里」。
 */
export class BrowserHost implements BrowserSessionEvents {
  private readonly tabs = new Map<string, TabEntry>();
  private readonly current = new Map<number, string>();
  private readonly downloads = new BrowserDownloads({
    onChange: () => this.publish(),
    note: (taskId, tabId, text) => this.noteTask(taskId, tabId, text),
  });
  private sequence = 0;
  private closed = false;
  private readonly listeners = new Set<(state: BrowserState) => void>();
  private attached: BrowserTab | null = null;
  /**
   * 最近一次请求带来的上限（事实源是后端配置，每个请求都带）。页面自己开出的窗口没有请求可带上限，只能用它：
   * 弹窗多由 act 的点击触发，那次 act 请求刚刷新过它。
   */
  private latestLimits: BrowserTabLimits | null = null;

  constructor(private readonly window: () => BrowserWindow | null) {}

  /** 要求新标签、或该 Task 还没有标签时先新开一个，再导航；返回导航后的快照。 */
  async navigate(request: NavigateRequest, signal: AbortSignal): Promise<PageResult> {
    this.track(request.context);
    const tab = request.newTab || (!request.tab && !this.current.has(request.taskId))
      ? await this.open(request.taskId, request.context, "agent", true, signal)
      : await this.readyTab(this.select(request.taskId, request.tab), signal);
    await tab.navigate(request.target, signal);
    return this.result(tab, await tab.snapshot());
  }

  async snapshot(request: SnapshotRequest, signal?: AbortSignal): Promise<PageResult> {
    this.track(request.context);
    const tab = await this.usableTab(this.tabFor(request.taskId, request.tab), signal);
    return this.result(tab, await tab.snapshot(request.fromLine));
  }

  /** 按序执行动作，无论停在哪一步都返回之后的快照（新快照带新 ref，模型能接着做）。 */
  async act(request: ActRequest, signal: AbortSignal): Promise<ActOutcome> {
    this.track(request.context);
    const entry = this.tabFor(request.taskId, request.tab);
    const tab = await this.usableTab(entry, signal);
    const outcome = await tab.act(request.actions, signal);
    // 在初始化中的标签上答完弹窗后，初始化会接着做完；读快照前等它（或页面又停住）。
    await this.usableTab(entry, signal);
    return { ...this.result(tab, await tab.snapshot()), ...outcome };
  }

  async screenshot(request: ScreenshotRequest, signal?: AbortSignal): Promise<ScreenshotResult> {
    this.track(request.context);
    const tab = await this.readyTab(this.tabFor(request.taskId, request.tab), signal);
    // JPEG：同样的视口，体积比 PNG 小得多，更不容易碰到附件的单图大小上限。
    const image = await tab.screenshot("jpeg", { quality: 85, fullPage: request.fullPage });
    const { url, title } = tab.summary();
    return { tab_id: tab.tabId, url, title, image };
  }

  /** 本 Task 的标签管理：列出、切换当前标签、关闭；都返回操作后的标签列表。 */
  manageTabs(request: TabsRequest): TaskTab[] {
    this.track(request.context);
    if (request.action === "select") this.select(request.taskId, request.tab!);
    else if (request.action === "close") this.close(this.tabFor(request.taskId, request.tab).tab.tabId);
    const current = this.current.get(request.taskId);
    return this.tabsOf(request.taskId).map(({ tab }) => {
      const { url, title, createdBy } = tab.summary();
      return { tab_id: tab.tabId, title, url, current: tab.tabId === current, created_by: createdBy };
    });
  }

  /** Task 删除提交后回收已就绪和初始化中的标签，取消它未完成的下载。 */
  dropTasks(taskIds: readonly number[]): void {
    for (const taskId of taskIds) {
      for (const { tab } of this.tabsOf(taskId)) this.close(tab.tabId);
    }
    this.downloads.dropTasks(taskIds);
  }

  /** 宿主退出的终态：先拒绝新建，再关闭全部资源，后端停止宽限期内也不能重新打开。 */
  closeAll(): void {
    this.closed = true;
    for (const tabId of this.tabs.keys()) this.close(tabId);
    this.downloads.cancelAll();
  }

  // ---------------------------------------------------------------------------
  // 分区级事件（`configureBrowserSession`）：按来源网页找到所属标签。
  // ---------------------------------------------------------------------------

  download(source: WebContents, item: DownloadItem): void {
    const entry = this.byContents(source);
    if (!entry) {
      // 分区里只有标签页；找不到说明标签正在注销，没有人能接住这次下载。
      item.cancel();
      logDesktopEvent("WARNING", "browser", "browser_download_orphaned", {});
      return;
    }
    const { tab } = entry;
    this.downloads.accept(item, { taskId: tab.taskId, tabId: tab.tabId, directory: tab.downloadDir });
  }

  permissionDenied(source: WebContents, permission: string): void {
    this.byContents(source)?.tab.note(`the site asked for the "${permission}" permission; it was denied (site permissions are not available)`);
  }

  // ---------------------------------------------------------------------------
  // 用户侧（渲染进程 IPC）：用户能看到并操作全部 Task 的标签页，所以按 tabId 寻址、不校验 Task。
  // ---------------------------------------------------------------------------

  state(): BrowserState {
    return { tabs: [...this.tabs.values()].map(({ tab }) => tab.summary()), downloads: this.downloads.list() };
  }

  onChange(listener: (state: BrowserState) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** 把标签挂到主窗口的给定矩形（视图在其中按视口等比缩放、居中）；`null` 摘下。 */
  place(tabId: string, placement: BrowserPlacement | null): void {
    const { tab } = this.require(tabId);
    const window = this.window();
    if (!placement || !window || placement.width < 1 || placement.height < 1) {
      if (this.attached === tab) this.detach();
      return;
    }
    if (this.attached !== tab) {
      this.detach();
      window.contentView.addChildView(tab.view);
      this.attached = tab;
    }
    tab.show(toWindowBounds(fitViewport(placement), window.webContents.getZoomFactor()));
  }

  /** 冻结帧：遮挡弹层出现时页面先放这张图，再摘下视图（设计稿 §6.1）。 */
  async freeze(tabId: string): Promise<string> {
    // 冻结帧要和实时画面一样清晰：按屏幕像素出图，不缩到 CSS 像素。
    const tab = await this.readyTab(this.require(tabId));
    return tab.screenshot("jpeg", { quality: 70, devicePixels: true });
  }

  /**
   * 用户在浏览器面板点「+」：在该 Task 里开一个标签（与 Agent 开的同等，Agent 经 `browser_tabs` 看得到、
   * 也能操作，行为与交互终端一致），设为当前标签。上下文由渲染进程向后端读来随请求带上。
   */
  async userOpen(taskId: number, context: BrowserTabContext, url?: string): Promise<string> {
    this.track(context);
    const tab = await this.open(taskId, context, "user", true);
    if (url) await tab.navigate({ url }, new AbortController().signal);
    return tab.tabId;
  }

  async userNavigate(tabId: string, target: BrowserNavigation): Promise<void> {
    const tab = await this.readyTab(this.require(tabId));
    await tab.navigate(target, new AbortController().signal);
  }

  userClose(tabId: string): void {
    this.require(tabId);
    this.close(tabId);
  }

  /** 用户在面板里回答弹窗；它已不是当前弹窗（Agent 答过、换成了新的）时什么也不做。 */
  userAnswerDialog(tabId: string, dialogId: number, answer: BrowserDialogAnswer): void {
    this.require(tabId).tab.answerDialog(dialogId, answer, "user");
  }

  revealDownload(downloadId: string): void {
    shell.showItemInFolder(this.downloads.pathOf(downloadId));
  }

  // ---------------------------------------------------------------------------

  /** 每个请求都带上限：刷新最近一次的值，供页面自己开出的窗口使用。 */
  private track(context: BrowserTabContext): void {
    this.latestLimits = context.limits;
  }

  private async open(taskId: number, context: BrowserTabContext, createdBy: BrowserTabCreator, activate: boolean, signal?: AbortSignal): Promise<BrowserTab> {
    this.assertCapacity(taskId, context.limits);
    signal?.throwIfAborted();
    const entry = this.register({ taskId, createdBy, openerId: null, downloadDir: context.downloadDir }, activate);
    try {
      await this.readyTab(entry, signal);
      logDesktopEvent("INFO", "browser", "browser_tab_opened", { task_id: taskId, tab_id: entry.tab.tabId, created_by: createdBy });
      return entry.tab;
    } catch (error) {
      this.close(entry.tab.tabId);
      throw error;
    }
  }

  /**
   * 页面要开新窗口（`setWindowOpenHandler`，必须同步答复）：非网页地址或到上限就拒绝并告诉来源标签；否则把
   * Chromium 建好的 webContents 收养成同 Task 的新标签（保留 opener）。开出的标签不抢当前标签（Agent 的
   * 默认目标不变），面板据 `openerId` 跟过去；来源标签关闭时它不跟着关（`outlivesOpener`）。
   */
  private windowOpen(opener: BrowserTab, url: string): WindowOpenHandlerResponse {
    const refuse = (why: string): WindowOpenHandlerResponse => {
      opener.note(`the page tried to open ${url.slice(0, 200)} in a new window; ${why}`);
      logDesktopEvent("INFO", "browser", "browser_popup_dropped", { task_id: opener.taskId, reason: why });
      return { action: "deny" };
    };
    if (this.closed) return { action: "deny" };
    if (!isWebProtocol(urlProtocol(url))) return refuse("it was blocked (not an http(s) address)");
    try {
      // 有标签就一定有过带上限的请求（新开标签的两条路都先 `track`）。
      this.assertCapacity(opener.taskId, this.latestLimits!);
    } catch (error) {
      return refuse(`it was blocked: ${(error as Error).message}`);
    }
    return {
      action: "allow",
      outlivesOpener: true,
      overrideBrowserWindowOptions: { webPreferences: browserPageWebPreferences() },
      createWindow: (options) => {
        // Electron 把 Chromium 建好的 webContents 放在 options 里交过来（实测），类型声明没有写；用它才保留 opener。
        const adopted = (options as { webContents?: WebContents }).webContents;
        if (!adopted) logDesktopEvent("WARNING", "browser", "browser_popup_without_contents", { task_id: opener.taskId, tab_id: opener.tabId });
        const entry = this.register(
          { taskId: opener.taskId, createdBy: opener.createdBy, openerId: opener.tabId, downloadDir: opener.downloadDir, adopted },
          false,
        );
        const { tab } = entry;
        // 页面开出的窗口可能在初始化完成前就自己关了（弹窗式登录很快就 `window.close()`）：初始化因此失败时按「自己关掉」收尾。
        entry.ready.then(
          () => logDesktopEvent("INFO", "browser", "browser_popup_opened", { task_id: tab.taskId, tab_id: tab.tabId }),
          () => (tab.contents.isDestroyed() ? this.gone(tab) : this.close(tab.tabId)),
        );
        opener.note(`the page opened ${url.slice(0, 200)} in a new tab ${tab.tabId}`);
        return tab.contents;
      },
    };
  }

  private assertCapacity(taskId: number, limits: BrowserTabLimits): void {
    if (this.closed) throw new HostMethodError("UNAVAILABLE", "the browser is shutting down");
    if (this.tabsOf(taskId).length >= limits.perTask) {
      throw new HostMethodError("TAB_LIMIT", `this task already has ${limits.perTask} tabs open (per-task limit)`);
    }
    if (this.tabs.size >= limits.total) {
      throw new HostMethodError("TAB_LIMIT", `the browser already has ${limits.total} tabs open across all tasks (global limit)`);
    }
  }

  /** 同步登记并开始初始化；任何 await 前，资源和上限名额都已经归 Host 所有。 */
  private register(
    options: { taskId: number; createdBy: BrowserTabCreator; openerId: string | null; downloadDir: string; adopted?: WebContents },
    activate: boolean,
  ): TabEntry {
    const tabId = `t${++this.sequence}`;
    const tab: BrowserTab = new BrowserTab({
      ...options,
      tabId,
      events: {
        onWindowOpen: (url) => this.windowOpen(tab, url),
        onChange: () => this.publish(),
        onGone: () => this.gone(tab),
        onDialogOpened: () => this.pauseProcess(tab),
        pausedBy: () => this.pausedBy(tab),
        chooseFiles: (multiple) => this.chooseFiles(multiple),
      },
    });
    const controller = new AbortController();
    // 初始化延后到本轮同步登记结束（收养时 Electron 要先拿到 webContents 才会开始加载）。
    const entry: TabEntry = { tab, controller, ready: Promise.resolve().then(() => {
      controller.signal.throwIfAborted();
      return tab.initialize(controller.signal);
    }) };
    this.tabs.set(tabId, entry);
    if (activate || !this.current.has(options.taskId)) this.current.set(options.taskId, tabId);
    this.publish();
    return entry;
  }

  /** 页面自己关掉了（`window.close()`，常见于弹窗式登录完成后）：注销，并告诉来源标签。 */
  private gone(tab: BrowserTab): void {
    if (this.tabs.get(tab.tabId)?.tab !== tab) return;
    this.close(tab.tabId);
    if (tab.openerId) this.tabs.get(tab.openerId)?.tab.note(`tab ${tab.tabId} (opened by this page) closed itself`);
  }

  /**
   * `tab` 的页面为什么停着：同一渲染进程里有标签在等弹窗作答（同步 IPC 停住的是整个进程的主线程）。优先报
   * 本标签自己的。只陈述事实，下一步怎么做由后端的建议表给。
   */
  private pausedBy(tab: BrowserTab): HostMethodError | null {
    if (tab.dialog) return new HostMethodError("DIALOG_OPEN", `the page is paused: it is showing ${describeDialog(tab.dialog)}`);
    const processId = tab.processId;
    if (processId === null) return null;
    for (const { tab: other } of this.tabs.values()) {
      if (other.dialog && other.processId === processId) {
        const owner = other.taskId === tab.taskId ? "this task" : "another task";
        return new HostMethodError("DIALOG_OPEN", `the page is paused: tab ${other.tabId} (${owner}) shares its browser process and is showing ${describeDialog(other.dialog)}`);
      }
    }
    return null;
  }

  /** `opener` 弹窗了：同进程标签（含它自己）在途的 CDP 与加载等待立即结束。 */
  private pauseProcess(opener: BrowserTab): void {
    const processId = opener.processId;
    for (const { tab } of this.tabs.values()) {
      if (tab === opener || (processId !== null && tab.processId === processId)) tab.interruptCdp(this.pausedBy(tab)!);
    }
  }

  /** 下载备注落到触发它的标签；那个标签已关闭时落到 Task 的当前标签（Agent 下次看的就是它）。 */
  private noteTask(taskId: number, tabId: string, text: string): void {
    const target = this.tabs.get(tabId)?.tab ?? this.tabs.get(this.current.get(taskId) ?? "")?.tab;
    if (target) target.note(text);
    else logDesktopEvent("INFO", "browser", "browser_note_dropped", { task_id: taskId, tab_id: tabId });
  }

  private async chooseFiles(multiple: boolean): Promise<string[] | null> {
    const window = this.window();
    const options = { properties: multiple ? ["openFile" as const, "multiSelections" as const] : ["openFile" as const] };
    const result = window ? await dialog.showOpenDialog(window, options) : await dialog.showOpenDialog(options);
    return result.canceled ? null : result.filePaths;
  }

  private tabsOf(taskId: number): TabEntry[] {
    return [...this.tabs.values()].filter(({ tab }) => tab.taskId === taskId);
  }

  private byContents(contents: WebContents): TabEntry | undefined {
    return [...this.tabs.values()].find(({ tab }) => tab.contents === contents);
  }

  /** 归属检查只在 Host 入口做，跨 Task 与不存在统一报 TAB_NOT_FOUND。 */
  private tabFor(taskId: number, tabId?: string): TabEntry {
    const entry = this.tabs.get(tabId ?? this.current.get(taskId) ?? "");
    if (!entry || entry.tab.taskId !== taskId) throw new HostMethodError("TAB_NOT_FOUND", tabId ? `tab ${tabId} is not open in this task` : "this task has no open tab");
    return entry;
  }

  private select(taskId: number, tabId?: string): TabEntry {
    const entry = this.tabFor(taskId, tabId);
    this.current.set(taskId, entry.tab.tabId);
    return entry;
  }

  private require(tabId: string): TabEntry {
    const entry = this.tabs.get(tabId);
    if (!entry) throw new HostMethodError("TAB_NOT_FOUND", `tab ${tabId} is not open`);
    return entry;
  }

  /** 现有标签的请求取消只停止本次等待；新建调用失败时由 open 回收其拥有的标签。 */
  private async readyTab(entry: TabEntry, signal: AbortSignal = entry.controller.signal): Promise<BrowserTab> {
    await waitForInitialization(entry.ready, signal);
    entry.controller.signal.throwIfAborted();
    return entry.tab;
  }

  /**
   * 就绪，或页面停住（弹窗）时提前返回：收养的标签加载中弹窗时初始化在等作答，读弹窗与回答它都不需要调试器；
   * 被别的标签的弹窗停住时，随后的 CDP 会以 `DIALOG_OPEN` 指明是哪个标签。
   */
  private async usableTab(entry: TabEntry, signal: AbortSignal = entry.controller.signal): Promise<BrowserTab> {
    const { tab } = entry;
    if (!tab.dialog) {
      const paused = new Promise<void>((resolve) => tab.pausedSignal.addEventListener("abort", () => resolve(), { once: true }));
      await waitForInitialization(Promise.race([entry.ready, paused]), signal);
    }
    entry.controller.signal.throwIfAborted();
    return tab;
  }

  private detach(): void {
    const tab = this.attached;
    if (!tab) return;
    this.attached = null;
    this.window()?.contentView.removeChildView(tab.view);
    tab.hide();
  }

  private close(tabId: string): void {
    const entry = this.tabs.get(tabId);
    if (!entry) return;
    const { tab, controller } = entry;
    if (this.attached === tab) {
      // 只摘下、不恢复视口：标签马上关闭，再设设备模拟只会对正在断开的调试器发命令。
      this.attached = null;
      this.window()?.contentView.removeChildView(tab.view);
    }
    this.tabs.delete(tabId);
    if (this.current.get(tab.taskId) === tabId) {
      const remaining = this.tabsOf(tab.taskId).at(-1);
      if (remaining) this.current.set(tab.taskId, remaining.tab.tabId);
      else this.current.delete(tab.taskId);
    }
    controller.abort(new HostMethodError("TAB_NOT_FOUND", `tab ${tabId} was closed`));
    tab.close();
    logDesktopEvent("INFO", "browser", "browser_tab_closed", { task_id: tab.taskId, tab_id: tabId });
    this.publish();
  }

  private publish(): void {
    const state = this.state();
    for (const listener of this.listeners) listener(state);
  }

  private result(tab: BrowserTab, page: PageSnapshot): PageResult {
    return {
      tab_id: tab.tabId,
      url: page.url,
      title: page.title,
      loading: page.loading,
      snapshot: page.text,
      frames: page.frames,
      notes: page.notes,
      other_tabs: this.tabsOf(tab.taskId).map(({ tab: other }) => other.tabId).filter((tabId) => tabId !== tab.tabId),
      dialog: page.dialog && { id: page.dialog.id, type: page.dialog.type, message: page.dialog.message, default_text: page.dialog.defaultText },
    };
  }
}

function describeDialog(dialog: BrowserDialog): string {
  return `${dialog.type === "alert" ? "an" : "a"} ${dialog.type} dialog (#${dialog.id})`;
}

/** 占位矩形里按视口等比缩放、居中的矩形（CSS 像素）。 */
function fitViewport(placement: BrowserPlacement): BrowserPlacement {
  const scale = Math.min(placement.width / VIEWPORT.width, placement.height / VIEWPORT.height);
  const width = VIEWPORT.width * scale;
  const height = VIEWPORT.height * scale;
  return { x: placement.x + (placement.width - width) / 2, y: placement.y + (placement.height - height) / 2, width, height };
}

/** 页面上报的是 CSS 像素；页面被缩放（Cmd + / -）时乘上缩放系数才是窗口坐标。 */
function toWindowBounds(placement: BrowserPlacement, zoom: number): Rectangle {
  return {
    x: Math.round(placement.x * zoom),
    y: Math.round(placement.y * zoom),
    width: Math.round(placement.width * zoom),
    height: Math.round(placement.height * zoom),
  };
}

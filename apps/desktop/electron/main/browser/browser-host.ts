import type { BrowserWindow, Rectangle } from "electron";

import { logDesktopEvent } from "../desktop-log";
import { HostMethodError } from "../host-link/methods";
import type { BrowserNavigation, BrowserPlacement, BrowserTabCreator, BrowserTabLimits, BrowserTabSummary } from "../../shared/host-contract";
import { BrowserTab, DETACHED_VIEWPORT, type PageSnapshot } from "./browser-tab";
import type { ActResult } from "./page-input";
import type { ActRequest, NavigateRequest, ScreenshotRequest, SnapshotRequest, TabsRequest } from "./params";
import { waitForInitialization } from "./initialization";
import { isWebProtocol, urlProtocol } from "./web-url";

/** 一次导航或快照的结果；字段名与后端 `app/tools/tool_handler/browser/` 读取的一致。 */
export type PageResult = {
  tab_id: string;
  url: string;
  title: string;
  loading: boolean;
  snapshot: string;
  frames: number;
  notes: string[];
  /** 本 Task 的其他标签 id（模型可经 `tab` 参数切过去）。 */
  other_tabs: string[];
};

/** 一次动作序列的结果：执行到哪一步、停在哪（若有），以及之后的页面。 */
export type ActOutcome = PageResult & { completed: number; failure: ActResult["failure"] };

export type ScreenshotResult = { tab_id: string; url: string; title: string; image: string };

/** 本 Task 的一个标签（`browser_tabs` 的列表项）。 */
export type TaskTab = { tab_id: string; title: string; url: string; current: boolean; created_by: BrowserTabCreator };

type TabEntry = { tab: BrowserTab; ready: Promise<void>; controller: AbortController };

/**
 * 内置浏览器的唯一所有者：含初始化作业的标签登记、全部 `BrowserTab` 与「哪个视图挂在窗口上」。宿主工具链路的方法
 * （`methods.ts`，Agent 用）与渲染进程 IPC（`ipc.ts`，用户用）都只经这里操作标签页，不各自持有视图。
 *
 * 标签页是宿主进程事实：不持久化，后端重启不影响，宿主退出即全部消失；Task 删除后由后端通知
 * `dropTasks` 回收。入参已由 `params.ts` / `ipc.ts` 校验，这里直接信任。
 *
 * 摆放：同一时刻至多一个视图挂在主窗口上（浏览器面板只渲染激活标签）。挂上新视图时摘下旧的；摘下的视图
 * 恢复 1280×800 视口继续在后台工作。摆放状态只有这里一处事实，渲染进程只上报「我想把哪个标签放在哪里」。
 */
export class BrowserHost {
  private readonly tabs = new Map<string, TabEntry>();
  private readonly current = new Map<number, string>();
  private sequence = 0;
  private closed = false;
  private readonly listeners = new Set<(tabs: BrowserTabSummary[]) => void>();
  private attached: BrowserTab | null = null;
  /**
   * 最近一次请求带来的上限。页面自己弹出的新窗口没有请求可带上限，只能用它——上限的事实源仍是后端配置，
   * 宿主不另存配置，至多滞后一次请求。
   */
  private latestLimits: BrowserTabLimits | null = null;

  constructor(private readonly window: () => BrowserWindow | null) {}

  /** 要求新标签、或该 Task 还没有标签时先新开一个，再导航；返回导航后的快照。 */
  async navigate(request: NavigateRequest, signal: AbortSignal): Promise<PageResult> {
    this.latestLimits = request.limits;
    const tab = request.newTab || (!request.tab && !this.current.has(request.taskId))
      ? await this.open(request.taskId, request.limits, "agent", true, signal)
      : await this.readyTab(this.select(request.taskId, request.tab), signal);
    await tab.navigate(request.target, signal);
    return this.result(tab, await tab.snapshot());
  }

  async snapshot(request: SnapshotRequest, signal?: AbortSignal): Promise<PageResult> {
    const tab = await this.readyTab(this.tabFor(request.taskId, request.tab), signal);
    return this.result(tab, await tab.snapshot(request.fromLine));
  }

  /** 按序执行动作，无论停在哪一步都返回之后的快照（新快照带新 ref，模型能接着做）。 */
  async act(request: ActRequest, signal: AbortSignal): Promise<ActOutcome> {
    const tab = await this.readyTab(this.tabFor(request.taskId, request.tab), signal);
    const outcome = await tab.act(request.actions, signal);
    return { ...this.result(tab, await tab.snapshot()), ...outcome };
  }

  async screenshot(request: ScreenshotRequest, signal?: AbortSignal): Promise<ScreenshotResult> {
    const tab = await this.readyTab(this.tabFor(request.taskId, request.tab), signal);
    // JPEG：同样的视口，体积比 PNG 小得多，更不容易碰到附件的单图大小上限。
    const image = await tab.screenshot("jpeg", { quality: 85, fullPage: request.fullPage });
    const { url, title } = tab.summary();
    return { tab_id: tab.tabId, url, title, image };
  }

  /** 本 Task 的标签管理：列出、切换当前标签、关闭；都返回操作后的标签列表。 */
  manageTabs(request: TabsRequest): TaskTab[] {
    if (request.action === "select") this.select(request.taskId, request.tab!);
    else if (request.action === "close") this.close(this.tabFor(request.taskId, request.tab).tab.tabId);
    const current = this.current.get(request.taskId);
    return this.tabsOf(request.taskId).map(({ tab }) => {
      const { url, title, createdBy } = tab.summary();
      return { tab_id: tab.tabId, title, url, current: tab.tabId === current, created_by: createdBy };
    });
  }

  /** Task 删除提交后回收已就绪和初始化中的标签。 */
  dropTasks(taskIds: readonly number[]): void {
    for (const taskId of taskIds) {
      for (const { tab } of this.tabsOf(taskId)) this.close(tab.tabId);
    }
  }

  /** 宿主退出的终态：先拒绝新建，再关闭全部资源，后端停止宽限期内也不能重新打开。 */
  closeAll(): void {
    this.closed = true;
    for (const tabId of this.tabs.keys()) this.close(tabId);
  }

  // ---------------------------------------------------------------------------
  // 用户侧（渲染进程 IPC）：用户能看到并操作全部 Task 的标签页，所以按 tabId 寻址、不校验 Task。
  // ---------------------------------------------------------------------------

  list(): BrowserTabSummary[] {
    return [...this.tabs.values()].map(({ tab }) => tab.summary());
  }

  onChange(listener: (tabs: BrowserTabSummary[]) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** 把标签挂到主窗口的给定矩形；`null` 摘下。矩形按页面缩放换算成窗口坐标。 */
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
    tab.view.setBounds(toWindowBounds(placement, window.webContents.getZoomFactor()));
  }

  /** 冻结帧：遮挡弹层出现时页面先放这张图，再摘下视图（设计稿 §6.1）。 */
  async freeze(tabId: string): Promise<string> {
    // 冻结帧要和实时画面一样清晰：按屏幕像素出图，不缩到 CSS 像素。
    const tab = await this.readyTab(this.require(tabId));
    return tab.screenshot("jpeg", { quality: 70, devicePixels: true });
  }

  /**
   * 用户在浏览器面板点「+」：在该 Task 里开一个标签（与 Agent 开的同等，Agent 经 `browser_tabs` 看得到、
   * 也能操作，行为与交互终端一致），设为当前标签。上限由渲染进程从配置中心读来随请求带上。
   */
  async userOpen(taskId: number, limits: BrowserTabLimits, url?: string): Promise<string> {
    this.latestLimits = limits;
    const tab = await this.open(taskId, limits, "user", true);
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

  // ---------------------------------------------------------------------------

  private async open(taskId: number, limits: BrowserTabLimits, createdBy: BrowserTabCreator, activate: boolean, signal?: AbortSignal): Promise<BrowserTab> {
    if (this.closed) throw new HostMethodError("UNAVAILABLE", "the browser is shutting down");
    signal?.throwIfAborted();
    if (this.tabsOf(taskId).length >= limits.perTask) {
      throw new HostMethodError("TAB_LIMIT", `this task already has ${limits.perTask} tabs open (per-task limit)`);
    }
    if (this.tabs.size >= limits.total) {
      throw new HostMethodError("TAB_LIMIT", `the browser already has ${limits.total} tabs open across all tasks (global limit)`);
    }
    const tabId = `t${++this.sequence}`;
    const tab = new BrowserTab(tabId, taskId, createdBy, {
      onPopup: (url) => this.openPopup(tab, url),
      onChange: () => this.publish(),
    });
    const controller = new AbortController();
    // 初始化延后到本轮同步登记结束；任何 await 前，资源和上限名额都已经归 Host 所有。
    const entry: TabEntry = { tab, controller, ready: Promise.resolve().then(() => {
      controller.signal.throwIfAborted();
      return tab.initialize(controller.signal);
    }) };
    this.tabs.set(tabId, entry);
    if (activate || !this.current.has(taskId)) this.current.set(taskId, tabId);
    this.publish();
    try {
      await this.readyTab(entry, signal);
      logDesktopEvent("INFO", "browser", "browser_tab_opened", { task_id: taskId, tab_id: tabId, created_by: createdBy });
      return tab;
    } catch (error) {
      this.close(tabId);
      throw error;
    }
  }

  /** popup 在登记时就保持当前选择；异步完成后不恢复旧选择，避免覆盖用户的新操作。 */
  private openPopup(opener: BrowserTab, url: string): void {
    if (this.closed || this.tabs.get(opener.tabId)?.tab !== opener) return;
    const limits = this.latestLimits;
    if (!limits || !isWebProtocol(urlProtocol(url))) {
      opener.note(`the page tried to open ${url.slice(0, 200)} in a new window; it was blocked`);
      return;
    }
    this.open(opener.taskId, limits, opener.createdBy, false).then(
      (tab) => {
        opener.note(`the page opened ${url.slice(0, 200)} in a new tab ${tab.tabId}`);
        logDesktopEvent("INFO", "browser", "browser_popup_opened", { task_id: opener.taskId, tab_id: tab.tabId });
        tab.navigate({ url }, new AbortController().signal).catch(() => undefined);
      },
      (error: unknown) => {
        opener.note(`the page tried to open ${url.slice(0, 200)} in a new tab, but ${error instanceof Error ? error.message : "it failed"}`);
        logDesktopEvent("INFO", "browser", "browser_popup_dropped", { task_id: opener.taskId });
      },
    );
  }

  private tabsOf(taskId: number): TabEntry[] {
    return [...this.tabs.values()].filter(({ tab }) => tab.taskId === taskId);
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

  private detach(): void {
    const tab = this.attached;
    if (!tab) return;
    this.attached = null;
    this.window()?.contentView.removeChildView(tab.view);
    tab.view.setBounds(DETACHED_VIEWPORT);
  }

  private close(tabId: string): void {
    const entry = this.tabs.get(tabId);
    if (!entry) return;
    const { tab, controller } = entry;
    if (this.attached === tab) this.detach();
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
    const tabs = this.list();
    for (const listener of this.listeners) listener(tabs);
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
    };
  }
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

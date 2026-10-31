import type { BrowserWindow, Rectangle } from "electron";

import { logDesktopEvent } from "../desktop-log";
import { HostMethodError } from "../host-link/methods";
import { BrowserTab, DETACHED_VIEWPORT, type PageSnapshot, type TabCreator, type TabSummary } from "./browser-tab";
import type { ActResult } from "./page-input";
import type { ActRequest, NavigateRequest, NavigateTarget, ScreenshotRequest, SnapshotRequest, TabsRequest } from "./params";
import { TabTable, type TabLimits } from "./tab-table";

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
export type TaskTab = { tab_id: string; title: string; url: string; current: boolean; created_by: TabCreator };

/** 浏览器面板里占位区域的矩形，CSS 像素、相对窗口内容区。 */
export type Placement = { x: number; y: number; width: number; height: number };

/**
 * 内置浏览器的唯一所有者：标签表、全部 `BrowserTab` 与「哪个视图挂在窗口上」。宿主工具链路的方法
 * （`methods.ts`，Agent 用）与渲染进程 IPC（`ipc.ts`，用户用）都只经这里操作标签页，不各自持有视图。
 *
 * 标签页是宿主进程事实：不持久化，后端重启不影响，宿主退出即全部消失；Task 删除后由后端通知
 * `dropTasks` 回收。入参已由 `params.ts` / `ipc.ts` 校验，这里直接信任。
 *
 * 摆放：同一时刻至多一个视图挂在主窗口上（浏览器面板只渲染激活标签）。挂上新视图时摘下旧的；摘下的视图
 * 恢复 1280×800 视口继续在后台工作。摆放状态只有这里一处事实，渲染进程只上报「我想把哪个标签放在哪里」。
 */
export class BrowserHost {
  private readonly table = new TabTable();
  private readonly tabs = new Map<string, BrowserTab>();
  private readonly listeners = new Set<(tabs: TabSummary[]) => void>();
  private attached: BrowserTab | null = null;
  /**
   * 最近一次请求带来的上限。页面自己弹出的新窗口没有请求可带上限，只能用它——上限的事实源仍是后端配置，
   * 宿主不另存配置，至多滞后一次请求。
   */
  private latestLimits: TabLimits | null = null;

  constructor(private readonly window: () => BrowserWindow | null) {}

  /** 要求新标签、或该 Task 还没有标签时先新开一个，再导航；返回导航后的快照。 */
  async navigate(request: NavigateRequest, signal: AbortSignal): Promise<PageResult> {
    this.latestLimits = request.limits;
    const tab = request.newTab || (!request.tab && !this.table.hasCurrent(request.taskId))
      ? await this.open(request.taskId, request.limits, "agent")
      : this.tabFor(request.taskId, request.tab);
    this.table.select(request.taskId, tab.tabId);
    await tab.navigate(request.target, signal);
    return this.result(tab, await tab.snapshot());
  }

  async snapshot(request: SnapshotRequest): Promise<PageResult> {
    const tab = this.tabFor(request.taskId, request.tab);
    return this.result(tab, await tab.snapshot());
  }

  /** 按序执行动作，无论停在哪一步都返回之后的快照（新快照带新 ref，模型能接着做）。 */
  async act(request: ActRequest, signal: AbortSignal): Promise<ActOutcome> {
    const tab = this.tabFor(request.taskId, request.tab);
    const outcome = await tab.act(request.actions, signal);
    return { ...this.result(tab, await tab.snapshot()), ...outcome };
  }

  async screenshot(request: ScreenshotRequest): Promise<ScreenshotResult> {
    const tab = this.tabFor(request.taskId, request.tab);
    // JPEG：同样的视口，体积比 PNG 小得多，更不容易碰到附件的单图大小上限。
    const image = await tab.screenshot("jpeg", { quality: 85, fullPage: request.fullPage });
    const { url, title } = tab.summary();
    return { tab_id: tab.tabId, url, title, image };
  }

  /** 本 Task 的标签管理：列出、切换当前标签、关闭；都返回操作后的标签列表。 */
  manageTabs(request: TabsRequest): TaskTab[] {
    if (request.action === "select") this.table.select(request.taskId, request.tab!);
    else if (request.action === "close") this.close(this.table.require(request.taskId, request.tab));
    const current = this.table.hasCurrent(request.taskId) ? this.table.require(request.taskId) : null;
    return this.table.tabsOf(request.taskId).map((tabId) => {
      const { url, title, createdBy } = this.tabs.get(tabId)!.summary();
      return { tab_id: tabId, title, url, current: tabId === current, created_by: createdBy };
    });
  }

  /** Task 删除提交后由后端通知；也用于宿主退出前的收尾。 */
  dropTasks(taskIds: readonly number[]): void {
    for (const taskId of taskIds) {
      for (const tabId of this.table.tabsOf(taskId)) this.close(tabId);
    }
  }

  closeAll(): void {
    for (const tabId of this.table.allTabs()) this.close(tabId);
  }

  // ---------------------------------------------------------------------------
  // 用户侧（渲染进程 IPC）：用户能看到并操作全部 Task 的标签页，所以按 tabId 寻址、不校验 Task。
  // ---------------------------------------------------------------------------

  list(): TabSummary[] {
    return [...this.tabs.values()].map((tab) => tab.summary());
  }

  onChange(listener: (tabs: TabSummary[]) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** 把标签挂到主窗口的给定矩形；`null` 摘下。矩形按页面缩放换算成窗口坐标。 */
  place(tabId: string, placement: Placement | null): void {
    const tab = this.require(tabId);
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
  freeze(tabId: string): Promise<string> {
    // 冻结帧要和实时画面一样清晰：按屏幕像素出图，不缩到 CSS 像素。
    return this.require(tabId).screenshot("jpeg", { quality: 70, devicePixels: true });
  }

  /**
   * 用户在浏览器面板点「+」：在该 Task 里开一个标签（与 Agent 开的同等，Agent 经 `browser_tabs` 看得到、
   * 也能操作，行为与交互终端一致），设为当前标签。上限由渲染进程从配置中心读来随请求带上。
   */
  async userOpen(taskId: number, limits: TabLimits, url?: string): Promise<string> {
    this.latestLimits = limits;
    const tab = await this.open(taskId, limits, "user");
    this.table.select(taskId, tab.tabId);
    if (url) await tab.navigate({ url }, new AbortController().signal);
    return tab.tabId;
  }

  async userNavigate(tabId: string, target: NavigateTarget): Promise<void> {
    await this.require(tabId).navigate(target, new AbortController().signal);
  }

  userClose(tabId: string): void {
    this.require(tabId);
    this.close(tabId);
  }

  // ---------------------------------------------------------------------------

  private async open(taskId: number, limits: TabLimits, createdBy: TabCreator): Promise<BrowserTab> {
    this.table.assertCanOpen(taskId, limits);
    const tabId = this.table.add(taskId);
    try {
      const tab = await BrowserTab.create(tabId, taskId, createdBy, {
        onPopup: (url) => this.openPopup(tab, url),
        onChange: () => this.publish(),
      });
      this.tabs.set(tabId, tab);
      logDesktopEvent("INFO", "browser", "browser_tab_opened", { task_id: taskId, tab_id: tabId, created_by: createdBy });
      this.publish();
      return tab;
    } catch (error) {
      this.table.remove(tabId);
      throw error;
    }
  }

  /** 页面要开新窗口：在同一 Task 里开新标签（当前标签不变），超出上限就只告诉模型被拦了。 */
  private openPopup(opener: BrowserTab, url: string): void {
    const limits = this.latestLimits;
    const protocol = URL.canParse(url) ? new URL(url).protocol : "";
    if (!limits || (protocol !== "http:" && protocol !== "https:")) {
      opener.note(`the page tried to open ${url.slice(0, 200)} in a new window; it was blocked`);
      return;
    }
    const current = this.table.require(opener.taskId);
    // 页面弹出的标签归开它的那一方。
    this.open(opener.taskId, limits, opener.createdBy).then(
      (tab) => {
        this.table.select(opener.taskId, current);
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

  private tabFor(taskId: number, tabId?: string): BrowserTab {
    return this.tabs.get(this.table.require(taskId, tabId))!;
  }

  private require(tabId: string): BrowserTab {
    const tab = this.tabs.get(tabId);
    if (!tab) throw new HostMethodError("TAB_NOT_FOUND", `tab ${tabId} is not open`);
    return tab;
  }

  private detach(): void {
    const tab = this.attached;
    if (!tab) return;
    this.attached = null;
    this.window()?.contentView.removeChildView(tab.view);
    tab.view.setBounds(DETACHED_VIEWPORT);
  }

  private close(tabId: string): void {
    const tab = this.tabs.get(tabId);
    if (tab && this.attached === tab) this.detach();
    this.tabs.delete(tabId);
    const taskId = this.table.ownerOf(tabId);
    this.table.remove(tabId);
    tab?.close();
    logDesktopEvent("INFO", "browser", "browser_tab_closed", { task_id: taskId, tab_id: tabId });
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
      other_tabs: this.table.tabsOf(tab.taskId).filter((tabId) => tabId !== tab.tabId),
    };
  }
}

/** 页面上报的是 CSS 像素；页面被缩放（Cmd + / -）时乘上缩放系数才是窗口坐标。 */
function toWindowBounds(placement: Placement, zoom: number): Rectangle {
  return {
    x: Math.round(placement.x * zoom),
    y: Math.round(placement.y * zoom),
    width: Math.round(placement.width * zoom),
    height: Math.round(placement.height * zoom),
  };
}

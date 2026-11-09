import { OPERATION_DRAIN_MS } from "./operation";
import { dialog, shell, type BrowserWindow, type DownloadItem, type Rectangle, type WebContents, type WindowOpenHandlerResponse } from "electron";

import { logDesktopEvent } from "../desktop-log";
import type { HostMethodError } from "../host-link/method-registry";
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
import { BrowserTab, type EvaluateOutcome, type PageSnapshot } from "./browser-tab";
import { browserPageWebPreferences, type BrowserSessionEvents } from "./browser-partition";
import { BrowserDownloads } from "./downloads";
import { browserError } from "./errors";
import type { ActResult } from "./input/actions";
import type { ActRequest, EvaluateRequest, LogsRequest, NavigateRequest, ScreenshotRequest, SnapshotRequest, TabRequest, TabsRequest } from "./params";
import type { Emulation } from "./tab-emulation";
import type { ConsoleEntry, LogPage, NetworkEntry, PageDigest } from "./tab-log";
import { abortable } from "./abortable";
import { budgetSignal, deadlineMs, withinDeadline } from "./deadlines";
import type { Outline } from "./snapshot/outline";
import { changesView, snapshotView, type SnapshotView } from "./snapshot/views";
import { isWebProtocol, urlProtocol } from "./web-url";

/** 页面类结果（页面、动作、日志、执行 JS、截图）共用的页头：标签、地址、加载、主文档状态码与生效的模拟。线上形状见 `wire.ts`。 */
export type PageHead = { tabId: string; url: string; title: string; loading: boolean; status: number | null; emulation: Emulation };

/** 一次导航或快照的结果，附「Agent 上次拿到页面结果以来」的页面事件与日志计数。 */
export type PageResult = PageHead & PageDigest & {
  /** 有待答的弹窗时为空串（页面停着，读不到）。 */
  snapshot: string;
  frames: number;
  /** 本 Task 的其他标签 id（模型可经 `tab` 参数切过去）。 */
  otherTabs: string[];
  dialog: BrowserDialog | null;
};

/** 一次动作序列的结果：执行到哪一步、停在哪（若有），以及之后的页面。 */
export type ActOutcome = PageHead & { execution: ActResult; observation: PageResult | null; observationError: { code: string; message: string } | null };

export type LogsResult = PageHead & ({ kind: "console" } & LogPage<ConsoleEntry> | { kind: "network" } & LogPage<NetworkEntry>);

export type EvaluateResult = PageHead & EvaluateOutcome;

export type ScreenshotResult = PageHead & { image: string };

/** 本 Task 的一个标签（`browser_tabs` 的列表项）。 */
export type TaskTab = { tabId: string; title: string; url: string; current: boolean; createdBy: BrowserTabCreator; operation: "running" | "stopping" | "unknown" | null };

/** 冻结帧每条 CDP 命令的时限：等不到就不要帧、直接收起画面，免得原生画面长时间盖住应用的弹层。 */
const FREEZE_TIMEOUT_MS = 1_000;
/** act 给读结果（等标签可用、读快照）留的时间：与 `browser.snapshot` 的时限相同。 */
const ACT_RESULT_RESERVE_MS = deadlineMs("browser.snapshot") + OPERATION_DRAIN_MS + 1_000;

/** `agentCalls`：此刻在这个标签上执行中的 Agent 操作数（`operate` 计数），面板据此标出「正在执行」。 */
type TabEntry = { tab: BrowserTab; ready: Promise<void>; controller: AbortController; agentCalls: number };

/**
 * 内置浏览器的唯一所有者：含初始化作业的标签登记、全部 `BrowserTab`、下载登记（`BrowserDownloads`）与
 * 「哪个视图挂在窗口上」。宿主工具链路的方法（`methods.ts`，Agent 用）与渲染进程 IPC（`ipc.ts`，用户用）
 * 都只经这里操作，不各自持有视图。面向 Agent 的结果是领域类型，线上形状只在 `wire.ts` 转换。
 *
 * 「上次以来」只在这里消费：页面结果（navigate / snapshot / act）组装时取一次标签日志的 `digest`；截图、日志
 * 读取、执行 JS 与交接的首次调用（读的是标签列表）都不碰它，交还时读的是快照、照常消费。
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
 * Agent 的操作经 `operate` 带上「请求取消或标签关闭」的组合信号：标签中途关闭时一律以 `TAB_NOT_FOUND` 结束；它也是
 * 「Agent 正在这个标签上执行」的唯一计数点（标签摘要的 `agentBusy`）。
 *
 * 摆放：同一时刻至多一个视图挂在主窗口上（浏览器面板只渲染激活标签）。挂上新视图时摘下旧的；摘下的视图
 * 恢复视口大小继续在后台工作。挂哪个、挂到哪由这里决定（`place` / `detach` 是唯一入口），渲染进程只上报「我想把
 * 哪个标签放在哪里」；占位矩形交给标签的设备模拟，视图在其中怎么等比摆放归它（`TabEmulation.viewBounds`）。
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

  /**
   * 要求新标签、或该 Task 还没有标签时先新开一个；先把要求的设备模拟设上（之后的加载按新视口布局），再导航；
   * 返回导航后的快照。
   */
  async navigate(request: NavigateRequest, signal: AbortSignal): Promise<PageResult> {
    this.track(request.context);
    const entry = request.newTab || (!request.tab && !this.current.has(request.taskId))
      ? await this.open(request.taskId, request.context, "agent", true, signal)
      : this.select(request.taskId, request.tab);
    this.assertAgentMayModify(request, entry.tab);
    return this.operate(entry, signal, async (tab, scoped) => {
      await tab.modify(scoped, (operation) => tab.navigate(request.target, operation, request.emulation));
      const lastKnown = this.head(tab);
      try { return this.result(tab, await tab.snapshot(), scoped); }
      catch (error) {
        scoped.throwIfAborted();
        return {
          ...this.headOr(tab, lastKnown),
          snapshot: "",
          frames: 0,
          notes: [`navigation finished; page observation unavailable: ${describeError(error).message}; read a fresh snapshot before continuing`],
          newLogs: { consoleErrors: 0, consoleWarnings: 0, failedRequests: 0 },
          otherTabs: this.siblingTabIds(tab),
          dialog: tab.dialog,
        };
      }
    }, "ready", true);
  }

  async snapshot(request: SnapshotRequest, signal: AbortSignal): Promise<PageResult> {
    return this.operate(this.agentEntry(request), signal, async (tab, scoped) => this.result(tab, await tab.snapshot(), scoped, request.view));
  }

  /**
   * 按序执行动作并保留执行回执；事后观察独立限时，失败不改写执行事实。动作在方法时限前
   * `ACT_RESULT_RESERVE_MS` 停下（停在的那一步报 `DEADLINE_EXCEEDED`），留出读快照的时间。
   */
  async act(request: ActRequest, signal: AbortSignal): Promise<ActOutcome> {
    const entry = this.agentEntry(request);
    this.assertAgentMayModify(request, entry.tab);
    const budget = budgetSignal("browser.act", ACT_RESULT_RESERVE_MS, "the call ran out of time before this action");
    return this.operate(entry, signal, async (tab, scoped) => {
      // 回执要能在标签被销毁后仍答复；这里的最后已知页头只作为销毁后的兜底，观察失败时优先读当前页头。
      const lastKnown = this.head(tab);
      const action = request.actions[0];
      const control = request.actions.length === 1 && action.type === "dialog" && action.id !== undefined;
      const execution = control
        ? await tab.actDialog(action as Extract<typeof action, { type: "dialog" }>, scoped)
        : await tab.act(request.actions, AbortSignal.any([scoped, budget]));
      try {
        const page = await withinDeadline("browser.snapshot", scoped, async (observing) => {
          await this.usableTab(entry, observing);
          return abortable(tab.snapshot(), observing);
        });
        const observation = this.result(tab, page, scoped, { changes: true });
        return { ...this.head(tab), execution, observation, observationError: null };
      } catch (error) {
        logDesktopEvent("INFO", "browser", "browser_observation_failed", { tab_id: tab.tabId, task_id: tab.taskId }, error);
        return { ...this.headOr(tab, lastKnown), execution, observation: null, observationError: describeError(error) };
      }
    }, "usable", true);
  }

  async screenshot(request: ScreenshotRequest, signal: AbortSignal): Promise<ScreenshotResult> {
    const entry = this.agentEntry(request);
    if (request.ref) this.assertAgentMayModify(request, entry.tab);
    return this.operate(entry, signal, async (tab, scoped) => {
      // JPEG：同样的视口，体积比 PNG 小得多，更不容易碰到附件的单图大小上限。
      const image = request.ref
        ? await tab.modify(scoped, (operation) => tab.elementScreenshot(request.ref!, operation))
        : await tab.screenshot("jpeg", { quality: 85, fullPage: request.fullPage });
      return { ...this.head(tab), image };
    });
  }

  /** 读标签的 console 或网络日志：只读缓冲，不发 CDP，页面停住时照样能读。 */
  async logs(request: LogsRequest, signal: AbortSignal): Promise<LogsResult> {
    return this.operate(this.agentEntry(request), signal, async (tab) => {
      const head = this.head(tab);
      return request.query.kind === "console"
        ? { ...head, kind: "console", ...tab.log.read(request.query) }
        : { ...head, kind: "network", ...tab.log.read(request.query) };
    });
  }

  async evaluate(request: EvaluateRequest, signal: AbortSignal): Promise<EvaluateResult> {
    const entry = this.agentEntry(request);
    this.assertAgentMayModify(request, entry.tab);
    return this.operate(entry, signal, async (tab, scoped) => {
      const outcome = await tab.modify(scoped, (operation) => tab.evaluate(request.expression, operation));
      return { ...this.head(tab), ...outcome };
    });
  }

  /** 本 Task 的标签管理：列出、切换当前标签、关闭；都返回操作后的标签列表。 */
  manageTabs(request: TabsRequest): TaskTab[] {
    this.track(request.context);
    if (request.action === "select") this.select(request.taskId, request.tab!);
    else if (request.action === "close") {
      const { tab } = this.tabFor(request.taskId, request.tab);
      this.assertAgentMayModify(request, tab);
      this.close(tab.tabId);
    }
    const current = this.current.get(request.taskId);
    return this.tabsOf(request.taskId).map(({ tab }) => {
      const { url, title, createdBy } = tab.summary();
      return { tabId: tab.tabId, title, url, current: tab.tabId === current, createdBy, operation: tab.initialized ? tab.operation?.state ?? null : "running" };
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
    this.byContents(source)?.tab.log.note(`the site asked for the "${permission}" permission; it was denied (site permissions are not available)`);
  }

  // ---------------------------------------------------------------------------
  // 用户侧（渲染进程 IPC）：用户能看到并操作全部 Task 的标签页，所以按 tabId 寻址、不校验 Task。
  // ---------------------------------------------------------------------------

  state(): BrowserState {
    return {
      tabs: [...this.tabs.values()].map(({ tab, agentCalls }) => ({ ...tab.summary(), agentBusy: agentCalls > 0, operation: tab.operation?.state ?? null })),
      downloads: this.downloads.list(),
    };
  }

  onChange(listener: (state: BrowserState) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /**
   * 把标签挂到主窗口的给定矩形（视图在其中按视口等比摆放）；`null` 摘下。只由主窗口里的面板调用，窗口必然在。
   * 摘下已关闭的标签什么也不做（关闭时已摘下；面板卸载时总会再摘一次）。
   */
  place(tabId: string, placement: BrowserPlacement | null): void {
    if (!placement || placement.width < 1 || placement.height < 1) {
      const entry = this.find(tabId, "place");
      if (entry && this.attached === entry.tab) this.detach();
      return;
    }
    const { tab } = this.require(tabId);
    const window = this.window()!;
    if (this.attached !== tab) {
      this.detach();
      window.contentView.addChildView(tab.view);
      this.attached = tab;
    }
    tab.show(toWindowBounds(placement, window.webContents.getZoomFactor()));
  }

  /** 冻结帧：遮挡弹层出现时页面先放这张图，再摘下视图（设计稿 §6.1）。 */
  async freeze(tabId: string): Promise<string> {
    // 原生画面要一直盖着弹层到这里返回，所以不等初始化（收养的弹出页可能要等到第一次加载结束，至多 30 秒；调试器
    // 还没附加时截图立即失败），主线程被占着时也只等很短（`FREEZE_TIMEOUT_MS`）：失败时渲染进程直接收起画面。
    // 冻结帧要和实时画面一样清晰：按屏幕像素出图。
    return this.require(tabId).tab.screenshot("jpeg", { quality: 70, devicePixels: true, timeoutMs: FREEZE_TIMEOUT_MS });
  }

  /**
   * 用户在浏览器面板点「+」：在该 Task 里开一个标签（与 Agent 开的同等，Agent 经 `browser_tabs` 看得到、
   * 也能操作，行为与交互终端一致），设为当前标签。上下文由渲染进程向后端读来随请求带上。
   */
  async userOpen(taskId: number, context: BrowserTabContext): Promise<string> {
    this.track(context);
    return (await this.open(taskId, context, "user", true)).tab.tabId;
  }

  async userNavigate(tabId: string, target: BrowserNavigation): Promise<void> {
    const entry = this.require(tabId);
    const tab = await this.readyTab(entry);
    if (tab.operation) { tab.operation.stop(); await tab.operation.finish(); }
    await tab.modify(entry.controller.signal, (operation) => tab.navigate(target, operation));
  }

  userClose(tabId: string): void {
    if (this.find(tabId, "close")) this.close(tabId);
  }

  /** 用户在面板里回答弹窗；它已不是当前弹窗（Agent 答过、换成了新的）或标签已关闭时什么也不做。 */
  userAnswerDialog(tabId: string, dialogId: number, answer: BrowserDialogAnswer): void {
    this.find(tabId, "answer_dialog")?.tab.answerDialog(dialogId, answer, "user");
  }

  /** 在文件夹中显示下载；记录已被挤出列表（Task 删除、只留最近几次）时什么也不做。 */
  revealDownload(downloadId: string): void {
    const target = this.downloads.pathOf(downloadId);
    if (target) shell.showItemInFolder(target);
    else logDesktopEvent("INFO", "browser", "browser_user_action_skipped", { action: "reveal_download", download_id: downloadId });
  }

  // ---------------------------------------------------------------------------

  /** 每个请求都带上限：刷新最近一次的值，供页面自己开出的窗口使用。 */
  private track(context: BrowserTabContext): void {
    this.latestLimits = context.limits;
  }

  /** Agent 请求的标签条目：刷新上限、按 Task 找到标签（跨 Task 与不存在都报 TAB_NOT_FOUND）。 */
  private agentEntry(request: TabRequest): TabEntry {
    this.track(request.context);
    return this.tabFor(request.taskId, request.tab);
  }

  /** 进出一次 Agent 操作：「正在执行」只在 0 与非 0 之间切换时推送，同一标签上并发的调用不重复推送。 */
  private countAgentCall(entry: TabEntry, delta: 1 | -1): void {
    const before = entry.agentCalls;
    entry.agentCalls += delta;
    if ((before === 0) !== (entry.agentCalls === 0)) this.publish();
  }

  /**
   * Agent 对一个标签的一次操作：先等标签可用（`until`），再带着「请求取消或标签关闭」的组合信号执行。标签中途关闭
   * （页面自己 `window.close()`、用户关掉、Task 删除）时一律以 `TAB_NOT_FOUND` 结束：在途的 CDP 与加载等待被这个
   * 信号打断；关闭之后才冒出来的错误（对已销毁的目标发命令、读已销毁的 webContents）也换成关闭的原因，不当作
   * 宿主内部错误；操作碰巧在关闭后才返回（页面销毁时 Chromium 会结束在等的 Promise），结果说的是已经不在的标签，
   * 同样报关闭。
   */
  private assertAgentMayModify(request: TabRequest, tab: BrowserTab): void {
    if (request.handoffTabs?.includes(tab.tabId)) throw browserError("HANDED_OFF", "this tab is handed to the user; wait for them to hand it back");
  }

  private async operate<T>(
    entry: TabEntry,
    signal: AbortSignal,
    run: (tab: BrowserTab, signal: AbortSignal) => Promise<T>,
    until: "usable" | "ready" = "usable",
    preserveReceipt = false,
  ): Promise<T> {
    const scoped = AbortSignal.any([signal, entry.controller.signal]);
    const assertOpen = () => {
      entry.controller.signal.throwIfAborted();
      // 「destroyed」事件可能晚于命令失败到达（注销与中止都在它之后）：直接问 webContents。
      if (entry.tab.contents.isDestroyed()) throw browserError("TAB_NOT_FOUND", `tab ${entry.tab.tabId} was closed`);
    };
    let result: T;
    this.countAgentCall(entry, 1);
    try {
      const tab = until === "ready" ? await this.readyTab(entry, scoped) : await this.usableTab(entry, scoped);
      result = await run(tab, scoped);
    } catch (error) {
      assertOpen();
      throw error;
    } finally {
      this.countAgentCall(entry, -1);
    }
    if (!preserveReceipt) assertOpen();
    return result;
  }

  private head(tab: BrowserTab): PageHead {
    const { url, title, loading } = tab.summary();
    return { tabId: tab.tabId, url, title, loading, status: tab.status, emulation: tab.currentEmulation };
  }

  /** 同 Task 的其他标签 id（页面结果里给模型的可切换目标），result 与降级结果共用这一处。 */
  private siblingTabIds(tab: BrowserTab): string[] {
    return this.tabsOf(tab.taskId).map(({ tab: other }) => other.tabId).filter((tabId) => tabId !== tab.tabId);
  }

  /**
   * 观察失败时的页头：标签还活着就读当前页面；已销毁（或销毁与回执同刻竞争、读取抛错）才退回动作前
   * 最后已知值。只有后者能保证「标签关闭后仍能答复执行回执」。
   */
  private headOr(tab: BrowserTab, lastKnown: PageHead): PageHead {
    if (tab.contents.isDestroyed()) return lastKnown;
    try { return this.head(tab); } catch { return lastKnown; }
  }

  private async open(taskId: number, context: BrowserTabContext, createdBy: BrowserTabCreator, activate: boolean, signal?: AbortSignal): Promise<TabEntry> {
    this.assertCapacity(taskId, context.limits);
    signal?.throwIfAborted();
    const entry = this.register({ taskId, createdBy, openerId: null, downloadDir: context.downloadDir }, activate);
    try {
      await this.readyTab(entry, signal);
      logDesktopEvent("INFO", "browser", "browser_tab_opened", { task_id: taskId, tab_id: entry.tab.tabId, created_by: createdBy });
      return entry;
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
      opener.log.note(`the page tried to open ${url.slice(0, 200)} in a new window; ${why}`);
      logDesktopEvent("INFO", "browser", "browser_popup_dropped", { task_id: opener.taskId, reason: why });
      return { action: "deny" };
    };
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
          (error: unknown) => {
            logDesktopEvent("INFO", "browser", "browser_popup_init_failed", { task_id: tab.taskId, tab_id: tab.tabId }, error);
            if (tab.contents.isDestroyed()) this.gone(tab);
            else this.close(tab.tabId);
          },
        );
        opener.log.note(`the page opened ${url.slice(0, 200)} in a new tab ${tab.tabId}`);
        return tab.contents;
      },
    };
  }

  private assertCapacity(taskId: number, limits: BrowserTabLimits): void {
    if (this.closed) throw browserError("UNAVAILABLE", "the browser is shutting down");
    if (this.tabsOf(taskId).length >= limits.perTask) {
      throw browserError("TAB_LIMIT", `this task already has ${limits.perTask} tabs open (per-task limit)`);
    }
    if (this.tabs.size >= limits.total) {
      throw browserError("TAB_LIMIT", `the browser already has ${limits.total} tabs open across all tasks (global limit)`);
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
        onDialogClosed: () => this.resumeProcess(tab),
        pausedBy: () => this.pausedBy(tab),
        chooseFiles: (multiple) => this.chooseFiles(multiple),
        isAttached: () => this.attached === tab,
      },
    });
    const controller = new AbortController();
    // 初始化延后到本轮同步登记结束（收养时 Electron 要先拿到 webContents 才会开始加载）。
    const entry: TabEntry = { tab, controller, agentCalls: 0, ready: Promise.resolve().then(() => {
      controller.signal.throwIfAborted();
      return tab.initialize(controller.signal);
    }) };
    this.tabs.set(tabId, entry);
    if (activate || !this.current.has(options.taskId)) this.current.set(options.taskId, tabId);
    this.publish();
    return entry;
  }

  /**
   * 页面自己关掉了（`window.close()`，常见于弹窗式登录完成、弹出页下载之后）：注销，并把它还没交给 Agent 的备注
   * （如下载结果）转给来源标签——没有来源时给 Task 的当前标签——不随标签一起消失。
   */
  private gone(tab: BrowserTab): void {
    if (this.tabs.get(tab.tabId)?.tab !== tab) return;
    this.close(tab.tabId);
    const opener = tab.openerId ? this.tabs.get(tab.openerId)?.tab : undefined;
    const heir = opener ?? this.tabs.get(this.current.get(tab.taskId) ?? "")?.tab;
    const unread = tab.log.undeliveredNotes();
    if (!heir) {
      logDesktopEvent("INFO", "browser", "browser_note_dropped", { task_id: tab.taskId, tab_id: tab.tabId, count: unread.length });
      return;
    }
    for (const note of unread) heir.log.note(`in tab ${tab.tabId}: ${note}`);
    heir.log.note(`tab ${tab.tabId}${opener ? " (opened by this page)" : ""} closed itself`);
  }

  /**
   * `tab` 的页面为什么停着：同一渲染进程里有标签在等弹窗作答（同步 IPC 停住的是整个进程的主线程）。优先报
   * 本标签自己的。只陈述事实，下一步怎么做由后端的建议表给。
   */
  private pausedBy(tab: BrowserTab): HostMethodError | null {
    if (tab.dialog) return browserError("DIALOG_OPEN", `the page is paused: it is showing ${describeDialog(tab.dialog)}`);
    const processId = tab.processId;
    if (processId === null) return null;
    for (const { tab: other } of this.tabs.values()) {
      if (other.dialog && other.processId === processId) {
        const owner = other.taskId === tab.taskId ? "this task" : "another task";
        return browserError("DIALOG_OPEN", `the page is paused: tab ${other.tabId} (${owner}) shares its browser process and is showing ${describeDialog(other.dialog)}`);
      }
    }
    return null;
  }

  /** `opener` 弹窗了：同进程标签（含它自己）在途的 CDP 与加载等待立即结束。 */
  private pauseProcess(opener: BrowserTab): void {
    for (const tab of this.sameProcess(opener)) tab.interruptCdp(this.pausedBy(tab)!);
  }

  /** `owner` 的弹窗答完或被取消了：同进程标签（含它自己）补设弹窗期间被闸住的设备模拟。 */
  private resumeProcess(owner: BrowserTab): void {
    for (const tab of this.sameProcess(owner)) tab.resyncEmulation();
  }

  /** 与 `tab` 同一渲染进程的已登记标签（含它自己；正在注销、已不在登记里的标签不再操作）。 */
  private sameProcess(tab: BrowserTab): BrowserTab[] {
    const processId = tab.processId;
    return [...this.tabs.values()].map((entry) => entry.tab)
      .filter((other) => other === tab || (processId !== null && other.processId === processId));
  }

  /** 下载备注落到触发它的标签；那个标签已关闭时落到 Task 的当前标签（Agent 下次看的就是它）。 */
  private noteTask(taskId: number, tabId: string, text: string): void {
    const target = this.tabs.get(tabId)?.tab ?? this.tabs.get(this.current.get(taskId) ?? "")?.tab;
    if (target) target.log.note(text);
    else logDesktopEvent("INFO", "browser", "browser_note_dropped", { task_id: taskId, tab_id: tabId });
  }

  /** 只有挂在面板上（主窗口里）的标签会请用户选文件（`BrowserTab.fileChooserOpened`），窗口必然在。 */
  private async chooseFiles(multiple: boolean): Promise<string[] | null> {
    const properties = multiple ? ["openFile" as const, "multiSelections" as const] : ["openFile" as const];
    const result = await dialog.showOpenDialog(this.window()!, { properties });
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
    if (!entry || entry.tab.taskId !== taskId) throw browserError("TAB_NOT_FOUND", tabId ? `tab ${tabId} is not open in this task` : "this task has no open tab");
    return entry;
  }

  private select(taskId: number, tabId?: string): TabEntry {
    const entry = this.tabFor(taskId, tabId);
    this.current.set(taskId, entry.tab.tabId);
    return entry;
  }

  /** 「必须在」：要在标签上产出结果的入口（摆上画面、冻结帧、导航）。 */
  private require(tabId: string): TabEntry {
    const entry = this.tabs.get(tabId);
    if (!entry) throw browserError("TAB_NOT_FOUND", `tab ${tabId} is not open`);
    return entry;
  }

  /**
   * 「不在就算了」：用户的收尾操作（摘下画面、关闭、作答弹窗）是让标签或弹窗离开，标签已关闭时目标已经达成——这类
   * 调用常与关闭竞争（面板卸载、连点关闭），报错只是假失败。查不到时记一条日志、返回 undefined。
   */
  private find(tabId: string, action: string): TabEntry | undefined {
    const entry = this.tabs.get(tabId);
    if (!entry) logDesktopEvent("INFO", "browser", "browser_user_action_skipped", { action, tab_id: tabId });
    return entry;
  }

  /**
   * 等初始化完成；请求取消只停止本次等待，新建调用失败时由 open 回收其拥有的标签。就绪之后、续执行之前标签被关掉
   * 也报关闭：`operate` 之外的用户路径（新开、地址栏导航）只靠这一处。
   */
  private async readyTab(entry: TabEntry, signal: AbortSignal = entry.controller.signal): Promise<BrowserTab> {
    await abortable(entry.ready, signal);
    entry.controller.signal.throwIfAborted();
    return entry.tab;
  }

  /**
   * 就绪，或页面停住（弹窗）时提前返回：收养的标签加载中弹窗时初始化在等作答，读弹窗与回答它都不需要调试器；
   * 被别的标签的弹窗停住时，随后的 CDP 会以 `DIALOG_OPEN` 指明是哪个标签。
   */
  private async usableTab(entry: TabEntry, signal: AbortSignal): Promise<BrowserTab> {
    const { tab } = entry;
    if (!tab.dialog) {
      const paused = tab.pausedSignal;
      try {
        await abortable(entry.ready, AbortSignal.any([signal, paused]));
      } catch (error) {
        if (!paused.aborted || signal.aborted) throw error;
      }
    }
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
    controller.abort(browserError("TAB_NOT_FOUND", `tab ${tabId} was closed`));
    tab.close();
    logDesktopEvent("INFO", "browser", "browser_tab_closed", { task_id: tab.taskId, tab_id: tabId });
    this.publish();
  }

  private publish(): void {
    const state = this.state();
    for (const listener of this.listeners) listener(state);
  }

  /**
   * 页面结果：页头、按请求的视图呈现的快照，以及「上次以来」——这里是它唯一的消费点（日志的 `digest` 与快照的比对
   * 基准 `deliver` 都在这里推进）。`changes`（act 之后）只给与上次结果相比的增量，比不了或变化太多时给整页。
   */
  private result(tab: BrowserTab, page: PageSnapshot, signal: AbortSignal, view: SnapshotView & { changes?: boolean } = {}): PageResult {
    signal.throwIfAborted();
    if (page.document !== tab.documentGeneration) throw browserError("REF_STALE", "the page changed while reading it; read a fresh snapshot");
    return {
      ...this.head(tab),
      snapshot: page.outline ? this.snapshotText(tab, page.outline, view) : "",
      frames: page.outline?.frames ?? 0,
      ...tab.log.digest(),
      otherTabs: this.siblingTabIds(tab),
      dialog: page.dialog,
    };
  }

  private snapshotText(tab: BrowserTab, outline: Outline, { changes, ...view }: SnapshotView & { changes?: boolean }): string {
    const previous = tab.deliver(outline);
    return (changes && previous && changesView(previous, outline)) || snapshotView(outline, view);
  }
}

function describeDialog(dialog: BrowserDialog): string {
  return `${dialog.type === "alert" ? "an" : "a"} ${dialog.type} dialog (#${dialog.id})`;
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

function describeError(error: unknown): { code: string; message: string } {
  return { code: (error as HostMethodError).code ?? "ACTION_FAILED", message: error instanceof Error ? error.message : String(error) };
}

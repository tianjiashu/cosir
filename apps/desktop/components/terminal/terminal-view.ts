import "@xterm/xterm/css/xterm.css";
import { FitAddon } from "@xterm/addon-fit";
import { SearchAddon, type ISearchOptions } from "@xterm/addon-search";
import { Unicode11Addon } from "@xterm/addon-unicode11";
import { Terminal, type IDisposable, type ITheme } from "@xterm/xterm";

import { requestConfirmation } from "@/lib/confirm/confirm-store";
import { frontendLog } from "@/lib/logging/frontend-log";
import { isMac, isWindows } from "@/lib/platform";
import {
  TerminalConnection,
  type TerminalConnectionState,
  type TerminalControlFrame,
} from "@/lib/terminal/terminal-connection";
import type { LinkTarget } from "@/lib/links/resolve-link";
import { PASTE_MAX_BYTES, pasteNeedsConfirmation, resolveTerminalKeyAction } from "@/lib/terminal/terminal-keyboard";
import type { TerminalNotice, TerminalNoticeTone } from "@/lib/terminal/terminal-pane-status";

import { registerTerminalLinks } from "./terminal-links";
import { suppressTerminalQueryReplies } from "./terminal-query-replies";
import { TerminalResizeController } from "./terminal-resize-controller";
import { xtermPaneFont } from "./xterm-appearance";

/**
 * 视图向 React 报告的连接期状态。交接与退出码是会话事实，只读 snapshot（`handoffs`、
 * `terminal_sessions`），这里不持有第二份。
 */
export type TerminalViewRuntime = {
  connection: TerminalConnectionState;
  /** 短暂提示（输入被拒、重新同步）；null 表示无。 */
  notice: TerminalNotice | null;
};

export type TerminalViewOptions = {
  taskId: number;
  sessionId: string;
  theme: ITheme;
  /**
   * 读取当前工作区根目录：终端里落在工作区内的绝对路径（可带 `:行[:列]`，规则见 `terminal-links`）可点击，
   * 在文件面板打开。每次识别链接时读取，视图因此只按会话身份创建一次，根目录到达或变化不重建 xterm 与连接。
   */
  getWorkspaceRoot: () => string | undefined;
  onRuntime: (runtime: TerminalViewRuntime) => void;
  onOpenSearch: () => void;
  onSearchResults: (result: { index: number; count: number }) => void;
  /** 打开识别出的链接（`useOpenLink`）：网址与工作区路径都经这一个入口，确认与分派不在终端里做。 */
  onOpenLink: (target: LinkTarget) => void;
};

const RESIZE_SEND_DELAY_MS = 120;
/**
 * 已交给 xterm 但尚未解析完的输出字节上限。服务端按订阅者限流，但本机 WS 收包不会反压，xterm 的写缓冲
 * 实际无界、超过约 50 MB 时 `write` 直接抛错丢数据；超过这里的上限就改走 resync（清屏后从后端缓冲重放），
 * 与服务端落后时同一语义。不按水位暂停 PTY：worker 必须持续读 PTY 供 Agent 的屏幕模型使用。
 */
const WRITE_BACKLOG_LIMIT_BYTES = 4 * 1024 * 1024;
const NOTICE_MS = 2500;
const SEARCH_DECORATIONS: ISearchOptions["decorations"] = {
  matchBackground: "#facc15",
  matchOverviewRuler: "#facc15",
  activeMatchBackground: "#f97316",
  activeMatchColorOverviewRuler: "#f97316",
};

/**
 * 单个交互终端会话的 xterm 实例与 WS 通道（每会话一份）。
 *
 * 活跃会话在 TaskPage 存活期间常驻：后端预览环形缓冲有上限，卸载后重新 attach 会丢掉超出缓冲的
 * 历史，所以切换标签、收起面板只隐藏容器；只在断线重连时按 seq 续传，resync 才清屏重放。已结束
 * 会话只在被查看时存在（何时创建 / 释放由 `terminal-workbench` 决定）。
 *
 * 键盘：按键归属由 `resolveTerminalKeyAction` 判定（复制 / 粘贴 / 搜索 / 交给应用快捷键 / 送入
 * 终端 / 改发行编辑序列）；IME 组合期间一律交给 xterm。粘贴（右键菜单、Ctrl+Shift+V、原生 paste 事件）
 * 统一经 `guardedPaste`：总量上限、断线提示、会逐行执行时确认。
 * 输入在未连接或会话结束时禁用（disableStdin），提示由 React 层按 `connection` 常驻显示。
 */
export class TerminalView {
  private readonly terminal: Terminal;
  private readonly fitAddon = new FitAddon();
  private readonly searchAddon = new SearchAddon();
  private readonly connection: TerminalConnection;
  private readonly resizeController: TerminalResizeController;
  private readonly disposables: IDisposable[] = [];
  private readonly encoder = new TextEncoder();
  private runtime: TerminalViewRuntime = { connection: "connecting", notice: null };
  private noticeTimer: ReturnType<typeof setTimeout> | null = null;
  private resizeTimer: ReturnType<typeof setTimeout> | null = null;
  /** 容器量到过真实尺寸：此后终端尺寸由人的面板决定；之前采用服务端几何。 */
  private fitted = false;
  private disposed = false;
  /** 有搜索词在生效：清除时才动高亮与选区，窗格隐藏时不误清用户自己的选区。 */
  private searching = false;
  /** 已交给 xterm、尚未解析完的输出字节（见 `WRITE_BACKLOG_LIMIT_BYTES`）。 */
  private pendingWriteBytes = 0;
  /** 因写入积压请求的 resync 尚未清屏：期间不再重复请求。 */
  private backlogResync = false;

  constructor(private readonly container: HTMLElement, private readonly options: TerminalViewOptions) {
    this.terminal = new Terminal({
      ...xtermPaneFont(),
      theme: options.theme,
      allowProposedApi: true,
      cursorBlink: true,
      disableStdin: true,
      scrollback: 10_000,
      macOptionIsMeta: false,
      // ConPTY 自己重绘屏幕：面板变高时 xterm 不再从滚动区拉回旧行，免得同一段输出出现两次。
      ...(isWindows ? { windowsPty: { backend: "conpty" as const } } : {}),
    });
    this.terminal.loadAddon(this.fitAddon);
    this.terminal.loadAddon(this.searchAddon);
    // 默认按 Unicode 6 算字宽，emoji 与部分 CJK 符号会被当成单宽导致光标错位（VS Code 同样启用 11）。
    this.terminal.loadAddon(new Unicode11Addon());
    this.terminal.unicode.activeVersion = "11";
    this.terminal.open(container);
    this.terminal.attachCustomKeyEventHandler((event) => this.handleKey(event));
    this.disposables.push(
      ...suppressTerminalQueryReplies(this.terminal),
      this.terminal.onData((data) => this.sendInput(this.encoder.encode(data))),
      // 部分鼠标上报以 latin-1 字节串出现在 onBinary。
      this.terminal.onBinary((data) => this.sendInput(Uint8Array.from(data, (char) => char.charCodeAt(0) & 0xff))),
      this.searchAddon.onDidChangeResults(({ resultIndex, resultCount }) => (
        this.options.onSearchResults({ index: resultIndex, count: resultCount })
      )),
      ...registerTerminalLinks(this.terminal, {
        getWorkspaceRoot: () => this.options.getWorkspaceRoot(),
        onOpenLink: (target) => this.options.onOpenLink(target),
      }),
    );
    container.addEventListener("paste", this.handlePasteEvent, true);
    this.resizeController = new TerminalResizeController(container, () => this.fit());
    this.connection = new TerminalConnection({
      taskId: options.taskId,
      sessionId: options.sessionId,
      onOutput: (data) => this.writeOutput(data),
      onReset: (resume) => this.reset(resume),
      onFrame: (frame) => this.handleFrame(frame),
      onState: (state) => this.handleConnectionState(state),
    });
    this.connection.start();
  }

  focus(): void {
    this.terminal.focus();
  }

  /** 从隐藏回到可见时补一次 fit 与重绘（隐藏期间 ResizeObserver 量到的是 0）。 */
  refresh(): void {
    this.resizeController.request();
    this.terminal.refresh(0, this.terminal.rows - 1);
  }

  setTheme(theme: ITheme): void {
    this.terminal.options.theme = theme;
  }

  reconnect(): void {
    this.connection.reconnect();
  }

  hasSelection(): boolean {
    return this.terminal.hasSelection();
  }

  /** 前台程序开了鼠标上报（htop、tmux、`mouse=a` 的 vim）：右键属于它，不弹应用菜单。 */
  mouseReporting(): boolean {
    return this.terminal.modes.mouseTrackingMode !== "none";
  }

  /** 复制选区（快捷键与右键菜单共用）；成功后清掉选区，失败在面板提示。 */
  async copySelection(): Promise<void> {
    const text = this.terminal.getSelection();
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      this.terminal.clearSelection();
    } catch (error) {
      this.log("WARNING", "terminal_copy_failed", "终端复制失败", {}, error);
      this.flash("复制失败", "destructive");
    }
  }

  /** 读剪贴板粘贴（Ctrl+Shift+V 与右键菜单共用），经 `guardedPaste`。 */
  async pasteFromClipboard(): Promise<void> {
    let text: string;
    try {
      text = await navigator.clipboard.readText();
    } catch (error) {
      this.log("WARNING", "terminal_paste_failed", "读取剪贴板失败", {}, error);
      this.flash("无法读取剪贴板", "destructive");
      return;
    }
    await this.guardedPaste(text);
  }

  search(term: string, direction: "next" | "previous"): void {
    this.searching = term.length > 0;
    if (!term) {
      this.searchAddon.clearDecorations();
      this.options.onSearchResults({ index: -1, count: 0 });
      return;
    }
    const options: ISearchOptions = { decorations: SEARCH_DECORATIONS, incremental: direction === "next" };
    if (direction === "next") this.searchAddon.findNext(term, options);
    else this.searchAddon.findPrevious(term, options);
  }

  clearSearch(): void {
    if (!this.searching) return;
    this.searching = false;
    this.searchAddon.clearDecorations();
    this.terminal.clearSelection();
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    if (this.noticeTimer !== null) clearTimeout(this.noticeTimer);
    if (this.resizeTimer !== null) clearTimeout(this.resizeTimer);
    this.container.removeEventListener("paste", this.handlePasteEvent, true);
    this.connection.dispose();
    this.resizeController.dispose();
    for (const disposable of this.disposables) disposable.dispose();
    this.terminal.dispose();
  }

  private update(patch: Partial<TerminalViewRuntime>): void {
    this.runtime = { ...this.runtime, ...patch };
    this.options.onRuntime(this.runtime);
  }

  private flash(text: string, tone: TerminalNoticeTone): void {
    if (this.noticeTimer !== null) clearTimeout(this.noticeTimer);
    this.update({ notice: { text, tone } });
    this.noticeTimer = setTimeout(() => {
      this.noticeTimer = null;
      this.update({ notice: null });
    }, NOTICE_MS);
  }

  private handleConnectionState(state: TerminalConnectionState): void {
    this.terminal.options.disableStdin = state !== "connected";
    // 输入停用时光标不再闪烁，避免看起来还能打字。
    this.terminal.options.cursorBlink = state === "connected";
    this.update({ connection: state });
  }

  private handleFrame(frame: TerminalControlFrame): void {
    switch (frame.type) {
      case "attached":
        if (this.fitted) this.sendResizeNow();
        else this.terminal.resize(frame.columns, frame.rows);
        return;
      case "geometry":
        // 尺寸由人的面板决定；面板从未显示过时沿用会话当前几何，避免画面错位。
        if (!this.fitted) this.terminal.resize(frame.columns, frame.rows);
        else if (frame.columns !== this.terminal.cols || frame.rows !== this.terminal.rows) this.sendResizeNow();
        return;
      case "input_mark":
        this.markAgentInput();
        return;
      case "input_rejected":
        this.log("INFO", "terminal_input_rejected", "终端输入被后端拒绝", { reason: frame.reason });
        this.flash(
          frame.reason === "backpressure" ? "输入过快，部分按键未送达" : "会话已结束，输入未送达",
          "warning",
        );
        return;
      case "protocol_error":
        // message 是后端英文排查文本（连接层已记日志），界面只给受控提示。
        this.flash("终端通道协议错误", "destructive");
        return;
    }
  }

  /**
   * resync 时清屏。xterm 的 `reset()` 不清写缓冲，直接调用会让还没解析的旧字节画在清屏后的画面上；所以
   * 排进写队列，在已排队的输出之后执行，执行完才 `resume` 让连接重新 attach——重放不会排在旧积压后面，
   * 写队列因此最多是「积压上限 + 一份重放」。
   */
  private reset(resume: () => void): void {
    this.terminal.write("", () => {
      this.terminal.reset();
      this.backlogResync = false;
      resume();
    });
    this.flash("输出缓冲已接不上，已清屏重新同步（更早的输出可能缺失）", "muted");
  }

  private writeOutput(data: Uint8Array): void {
    const size = data.length;
    this.pendingWriteBytes += size;
    this.terminal.write(data, () => {
      this.pendingWriteBytes -= size;
    });
    if (this.backlogResync || this.pendingWriteBytes <= WRITE_BACKLOG_LIMIT_BYTES) return;
    this.backlogResync = true;
    this.log("WARNING", "terminal_write_backlog", "终端输出积压过多，清屏重新同步", { pendingBytes: this.pendingWriteBytes });
    this.connection.resync({ pendingBytes: this.pendingWriteBytes });
  }

  private sendInput(data: Uint8Array): void {
    // 非 connected 时 disableStdin 已让 xterm 不再发 onData，这里只剩连接刚断、状态尚未切换的窗口。
    if (!this.connection.sendInput(data)) this.flash("连接已断开，输入未送达", "warning");
  }

  /** 排进写队列再标（与清屏同一写法）：要标在它之前的输出解析完后的光标行上，否则会标到旧行。 */
  private markAgentInput(): void {
    this.terminal.write("", () => {
      const marker = this.terminal.registerMarker(0);
      const decoration = this.terminal.registerDecoration({ marker, x: 0, width: 1 });
      decoration?.onRender((element) => element.classList.add("terminal-agent-input-mark"));
    });
  }

  private fit(): boolean {
    const dimensions = this.fitAddon.proposeDimensions();
    if (!dimensions || !Number.isFinite(dimensions.cols) || !Number.isFinite(dimensions.rows)) return false;
    const cols = Math.max(10, dimensions.cols);
    const rows = Math.max(2, dimensions.rows);
    this.fitted = true;
    if (cols !== this.terminal.cols || rows !== this.terminal.rows) {
      this.terminal.resize(cols, rows);
      this.scheduleResizeSend();
    }
    return true;
  }

  /** 拖拽调高时 ResizeObserver 高频触发；本地立即重排，发往后端的 resize 合并为尾部一次。 */
  private scheduleResizeSend(): void {
    if (this.resizeTimer !== null) clearTimeout(this.resizeTimer);
    this.resizeTimer = setTimeout(() => {
      this.resizeTimer = null;
      this.sendResizeNow();
    }, RESIZE_SEND_DELAY_MS);
  }

  private sendResizeNow(): void {
    this.connection.sendResize(this.terminal.rows, this.terminal.cols);
  }

  private handleKey(event: KeyboardEvent): boolean {
    const action = resolveTerminalKeyAction(event, { isMac, hasSelection: this.terminal.hasSelection() });
    // keyup / keypress 跟随 keydown 的归属：被拦截的组合键不应再以 keypress 漏进终端。
    if (event.type !== "keydown") return action === "terminal";
    if (typeof action === "object") {
      event.preventDefault();
      // 走 xterm 的输入路径（与普通按键同经 onData）：会话结束时同样被 disableStdin 拦下，翻看回滚区时回到底部。
      this.terminal.input(action.send, true);
      return false;
    }
    switch (action) {
      case "terminal":
        return true;
      case "copy":
        event.preventDefault();
        void this.copySelection();
        return false;
      case "paste":
        event.preventDefault();
        void this.pasteFromClipboard();
        return false;
      case "search":
        event.preventDefault();
        this.options.onOpenSearch();
        return false;
      case "native-paste":
      case "app":
        // 不送入终端；浏览器默认行为（原生 paste 事件）或应用快捷键照常处理。
        return false;
    }
  }

  private readonly handlePasteEvent = (event: ClipboardEvent) => {
    const text = event.clipboardData?.getData("text/plain") ?? "";
    if (!text) return;
    if (
      this.runtime.connection === "connected"
      && this.encoder.encode(text).length <= PASTE_MAX_BYTES
      && !pasteNeedsConfirmation(text, { bracketedPasteMode: this.terminal.modes.bracketedPasteMode })
    ) {
      return;
    }
    // 超限、需要确认或连接不可用：拦下原生事件统一处理，确认后经 xterm.paste 送入（保留 bracketed paste 语义）。
    event.preventDefault();
    event.stopImmediatePropagation();
    void this.guardedPaste(text);
  };

  /**
   * 所有粘贴路径的唯一入口：总量上限、断线提示（xterm 在 disableStdin 时会静默丢掉粘贴）、会逐行执行时
   * 确认。确认框打开期间连接可能断开，确认后再查一次。
   */
  private async guardedPaste(text: string): Promise<void> {
    if (!text) return;
    if (!this.pasteDeliverable()) return;
    const bytes = this.encoder.encode(text).length;
    if (bytes > PASTE_MAX_BYTES) {
      this.log("INFO", "terminal_paste_too_large", "粘贴内容超过终端上限，未发送", { bytes });
      this.flash(`粘贴内容过大（上限 ${PASTE_MAX_BYTES / 1024} KB），未发送`, "warning");
      return;
    }
    if (pasteNeedsConfirmation(text, { bracketedPasteMode: this.terminal.modes.bracketedPasteMode })) {
      const lines = text.split(/\r\n|\r|\n/).filter((line) => line.length > 0).length;
      const confirmed = await requestConfirmation({
        title: "粘贴并执行？",
        description: `当前程序会把粘贴的内容当作命令立即执行${lines > 0 ? `（共 ${lines} 行）` : ""}。`,
        confirmLabel: "粘贴",
      });
      if (!confirmed || this.disposed || !this.pasteDeliverable()) return;
    }
    this.terminal.paste(text);
    this.terminal.focus();
  }

  private pasteDeliverable(): boolean {
    if (this.runtime.connection === "connected") return true;
    this.flash("连接已断开，粘贴未送达", "warning");
    return false;
  }

  private log(level: "INFO" | "WARNING", event: string, msg: string, data: Record<string, unknown> = {}, error?: unknown): void {
    void frontendLog(level, event, msg, {
      data: { taskId: this.options.taskId, sessionId: this.options.sessionId, ...data },
      error,
    });
  }
}

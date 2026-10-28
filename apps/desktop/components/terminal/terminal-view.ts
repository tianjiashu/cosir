import "@xterm/xterm/css/xterm.css";
import { FitAddon } from "@xterm/addon-fit";
import { SearchAddon, type ISearchOptions } from "@xterm/addon-search";
import { WebLinksAddon } from "@xterm/addon-web-links";
import { Terminal, type IDisposable, type ILink, type ITheme } from "@xterm/xterm";

import { requestConfirmation } from "@/lib/confirm/confirm-store";
import { frontendLog } from "@/lib/logging/frontend-log";
import { isMac } from "@/lib/platform";
import {
  TerminalConnection,
  type TerminalConnectionState,
  type TerminalControlFrame,
} from "@/lib/terminal/terminal-connection";
import { pasteNeedsConfirmation, resolveTerminalKeyAction } from "@/lib/terminal/terminal-keyboard";

import { TerminalResizeController } from "./terminal-resize-controller";
import { xtermPaneFont } from "./xterm-appearance";

/**
 * 视图向 React 报告的连接期状态。输入租约、交接与退出码是会话事实，只读 snapshot
 * `terminal_sessions`，这里不持有第二份。
 */
export type TerminalViewRuntime = {
  connection: TerminalConnectionState;
  /** 短暂提示（输入被拒、重新同步）；null 表示无。 */
  notice: { text: string; tone: "muted" | "warning" | "destructive" } | null;
};

export type TerminalViewOptions = {
  taskId: number;
  sessionId: string;
  theme: ITheme;
  /**
   * 读取当前工作区根目录：终端里以它开头的绝对路径可点击，在文件面板打开。每次识别链接时读取，
   * 视图因此只按会话身份创建一次，根目录到达或变化不重建 xterm 与连接。
   */
  getWorkspaceRoot: () => string | undefined;
  onRuntime: (runtime: TerminalViewRuntime) => void;
  onOpenSearch: () => void;
  onSearchResults: (result: { index: number; count: number }) => void;
  onOpenWorkspaceFile?: (relativePath: string) => void;
};

const RESIZE_SEND_DELAY_MS = 120;
const NOTICE_MS = 2500;
const SEARCH_DECORATIONS: ISearchOptions["decorations"] = {
  matchBackground: "#facc15",
  matchOverviewRuler: "#facc15",
  activeMatchBackground: "#f97316",
  activeMatchColorOverviewRuler: "#f97316",
};

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * 单个交互终端会话的 xterm 实例与 WS 通道（每会话一份）。
 *
 * 活跃会话在 TaskPage 存活期间常驻：后端预览环形缓冲有上限，卸载后重新 attach 会丢掉超出缓冲的
 * 历史，所以切换标签、收起面板只隐藏容器；只在断线重连时按 seq 续传，resync 才清屏重放。已结束
 * 会话只在被查看时存在（何时创建 / 释放由 `terminal-workbench` 决定）。
 *
 * 键盘：按键归属由 `resolveTerminalKeyAction` 判定（复制 / 粘贴 / 搜索 / 交给应用快捷键 / 送入
 * 终端）；IME 组合期间一律交给 xterm。粘贴（快捷键、菜单、原生 paste 事件）统一经大段确认。
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

  constructor(private readonly container: HTMLElement, private readonly options: TerminalViewOptions) {
    this.terminal = new Terminal({
      ...xtermPaneFont(),
      theme: options.theme,
      allowProposedApi: true,
      cursorBlink: true,
      disableStdin: true,
      scrollback: 10_000,
      macOptionIsMeta: false,
    });
    this.terminal.loadAddon(this.fitAddon);
    this.terminal.loadAddon(this.searchAddon);
    this.terminal.loadAddon(new WebLinksAddon((event, uri) => {
      event.preventDefault();
      window.open(uri, "_blank", "noopener,noreferrer");
    }));
    this.terminal.open(container);
    this.terminal.attachCustomKeyEventHandler((event) => this.handleKey(event));
    this.disposables.push(
      this.terminal.onData((data) => this.sendInput(this.encoder.encode(data))),
      // 部分鼠标上报以 latin-1 字节串出现在 onBinary。
      this.terminal.onBinary((data) => this.sendInput(Uint8Array.from(data, (char) => char.charCodeAt(0) & 0xff))),
      this.searchAddon.onDidChangeResults(({ resultIndex, resultCount }) => (
        this.options.onSearchResults({ index: resultIndex, count: resultCount })
      )),
    );
    this.registerWorkspacePathLinks();
    container.addEventListener("paste", this.handlePasteEvent, true);
    this.resizeController = new TerminalResizeController(container, () => this.fit());
    this.connection = new TerminalConnection({
      taskId: options.taskId,
      sessionId: options.sessionId,
      onOutput: (data) => this.terminal.write(data),
      onReset: () => this.reset(),
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

  releaseInput(): void {
    if (this.connection.releaseInput()) this.log("INFO", "terminal_input_released", "用户释放终端输入租约");
  }

  handoffDone(): void {
    if (this.connection.handoffDone()) this.log("INFO", "terminal_handoff_returned", "用户交还终端输入权");
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

  private flash(text: string, tone: "muted" | "warning" | "destructive"): void {
    if (this.noticeTimer !== null) clearTimeout(this.noticeTimer);
    this.update({ notice: { text, tone } });
    this.noticeTimer = setTimeout(() => {
      this.noticeTimer = null;
      this.update({ notice: null });
    }, NOTICE_MS);
  }

  private handleConnectionState(state: TerminalConnectionState): void {
    this.terminal.options.disableStdin = state !== "connected";
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
          frame.reason === "agent_input" ? "Agent 正在输入"
            : frame.reason === "backpressure" ? "输入过快，部分按键未送达"
              : "会话已结束，输入未送达",
          "warning",
        );
        return;
      case "protocol_error":
        // message 是后端英文排查文本（连接层已记日志），界面只给受控提示。
        this.flash("终端通道协议错误", "destructive");
        return;
    }
  }

  private reset(): void {
    this.terminal.reset();
    this.flash("输出缓冲已接不上，已清屏重新同步（更早的输出可能缺失）", "muted");
  }

  private sendInput(data: Uint8Array): void {
    // 非 connected 时 disableStdin 已让 xterm 不再发 onData，这里只剩连接刚断、状态尚未切换的窗口。
    if (!this.connection.sendInput(data)) this.flash("连接已断开，输入未送达", "warning");
  }

  private markAgentInput(): void {
    const marker = this.terminal.registerMarker(0);
    const decoration = this.terminal.registerDecoration({ marker, x: 0, width: 1 });
    decoration?.onRender((element) => element.classList.add("terminal-agent-input-mark"));
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
    if (event.type !== "keydown") {
      // keyup / keypress 跟随 keydown 的归属：被拦截的组合键不应再以 keypress 漏进终端。
      return resolveTerminalKeyAction(event, { isMac, hasSelection: this.terminal.hasSelection() }) === "terminal";
    }
    const action = resolveTerminalKeyAction(event, { isMac, hasSelection: this.terminal.hasSelection() });
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

  private async copySelection(): Promise<void> {
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

  private async pasteFromClipboard(): Promise<void> {
    try {
      await this.guardedPaste(await navigator.clipboard.readText());
    } catch (error) {
      this.log("WARNING", "terminal_paste_failed", "读取剪贴板失败", {}, error);
      this.flash("无法读取剪贴板", "destructive");
    }
  }

  private readonly handlePasteEvent = (event: ClipboardEvent) => {
    const text = event.clipboardData?.getData("text/plain") ?? "";
    if (!pasteNeedsConfirmation(text)) return;
    // 大段粘贴先确认：拦下原生事件，确认后再经 xterm.paste 送入（保留 bracketed paste 语义）。
    event.preventDefault();
    event.stopImmediatePropagation();
    void this.guardedPaste(text);
  };

  private async guardedPaste(text: string): Promise<void> {
    if (!text) return;
    if (pasteNeedsConfirmation(text)) {
      const lines = text.split(/\r\n|\r|\n/).length;
      const confirmed = await requestConfirmation({
        title: "粘贴大段内容？",
        description: `将向终端粘贴 ${text.length} 个字符（${lines} 行），多行内容会被逐行执行。`,
        confirmLabel: "粘贴",
      });
      if (!confirmed || this.disposed) return;
    }
    this.terminal.paste(text);
    this.terminal.focus();
  }

  private registerWorkspacePathLinks(): void {
    let cached: { root: string; pattern: RegExp } | null = null;
    this.disposables.push(this.terminal.registerLinkProvider({
      provideLinks: (lineNumber, callback) => {
        const rawRoot = this.options.getWorkspaceRoot();
        if (!rawRoot || !this.options.onOpenWorkspaceFile) {
          callback(undefined);
          return;
        }
        const root = rawRoot.replace(/[\\/]+$/, "");
        if (cached?.root !== root) {
          cached = { root, pattern: new RegExp(`${escapeRegExp(root)}[\\\\/][^\\s'"\`:;,()<>\\[\\]]+`, "g") };
        }
        const { pattern } = cached;
        const line = this.terminal.buffer.active.getLine(lineNumber - 1)?.translateToString(true) ?? "";
        const links: ILink[] = [];
        for (const match of line.matchAll(pattern)) {
          const start = match.index ?? 0;
          const relative = match[0].slice(root.length + 1).replace(/\\/g, "/");
          links.push({
            text: match[0],
            range: { start: { x: start + 1, y: lineNumber }, end: { x: start + match[0].length, y: lineNumber } },
            activate: () => this.options.onOpenWorkspaceFile?.(relative),
          });
        }
        callback(links.length > 0 ? links : undefined);
      },
    }));
  }

  private log(level: "INFO" | "WARNING", event: string, msg: string, data: Record<string, unknown> = {}, error?: unknown): void {
    void frontendLog(level, event, msg, {
      data: { taskId: this.options.taskId, sessionId: this.options.sessionId, ...data },
      error,
    });
  }
}

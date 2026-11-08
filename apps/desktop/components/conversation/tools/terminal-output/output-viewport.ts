import { FitAddon } from "@xterm/addon-fit";
import { Unicode11Addon } from "@xterm/addon-unicode11";
import { Terminal, type IDisposable, type ITheme } from "@xterm/xterm";

import { frontendLog } from "@/lib/logging/frontend-log";
import { registerTerminalLinks, type TerminalLinkOptions } from "@/components/terminal/terminal-links";
import { TerminalResizeController } from "@/components/terminal/terminal-resize-controller";
import { xtermCardTheme, xtermPaneFont } from "@/components/terminal/xterm-appearance";

import { reconcileTerminalOutput } from "./reconciler";
import { bufferText, SCROLLBACK_ROWS } from "./text";
import { TerminalWriteScheduler } from "./write-scheduler";

export type TerminalSnapshot = {
  output: string;
};

export type TerminalOutputViewOptions = {
  resolvedTheme: "light" | "dark";
  initialLine?: number;
  onPositionChange?: (line: number, atBottom: boolean) => void;
  /** 输出里的网址与工作区路径可点（规则与底部终端同一处，见 `terminal-links`）。 */
  links?: TerminalLinkOptions;
  /** 缓冲到达容量（开始丢最早的输出）时通知 true，清屏重放后行数回落时通知 false；只在变化时通知。 */
  onTruncatedChange?: (truncated: boolean) => void;
};

/**
 * 命令输出阅读器的视口：持有一个只读 xterm 实例及与之对应的输出游标。
 *
 * 游标刻意不与 React 或另一个视口共享：xterm 销毁后已渲染画面失效，替换上来的视口必须从头重放
 * 当前累计快照。本类只渲染 UI，不创建进程、不写 Transport；与交互终端的「会话」无关。
 *
 * 高度由 Dialog 分配。xterm 缓冲有界，达到容量时提示并提供原始输出下载；完整原文始终来自 backend snapshot。
 */
export class TerminalOutputView {
  private readonly terminal: Terminal;
  private readonly fitAddon: FitAddon;
  private readonly writeScheduler: TerminalWriteScheduler;
  private readonly resizeController: TerminalResizeController;
  private desiredSnapshot: TerminalSnapshot = { output: "" };
  private renderedSnapshot: TerminalSnapshot = { output: "" };
  private readonly disposables: IDisposable[] = [];
  private truncated = false;
  private disposed = false;
  private restored = false;

  constructor(container: HTMLElement, private readonly options: TerminalOutputViewOptions) {
    this.fitAddon = new FitAddon();
    this.terminal = new Terminal({
      // unicode 版本切换属于 proposed API。
      allowProposedApi: true,
      // 背景透明：视窗落在容器的 `--surface` 底面上，跟随主题换面，不再是恒深色的终端岛。
      allowTransparency: true,
      // Windows 管道工具常只输出 LF 不带 CR；真实终端会把它当作回到行首的新行，
      // xterm 需要此选项才能在一次性、非交互输出里保持同样表现。
      convertEol: true,
      cursorBlink: false,
      // 只读视口从不聚焦，空白的光标行不应画出光标块。
      cursorInactiveStyle: "none",
      disableStdin: true,
      scrollback: SCROLLBACK_ROWS,
      ...xtermPaneFont(),
      theme: xtermCardTheme(options.resolvedTheme),
    });

    this.terminal.loadAddon(this.fitAddon);
    // 与交互终端一致按 Unicode 11 算字宽，emoji / CJK 符号不错位。
    this.terminal.loadAddon(new Unicode11Addon());
    this.terminal.unicode.activeVersion = "11";
    this.terminal.open(container);
    this.disposables.push(this.terminal.onScroll(() => this.reportPosition()));
    if (options.links) this.disposables.push(...registerTerminalLinks(this.terminal, options.links));
    this.writeScheduler = new TerminalWriteScheduler((done) => {
      this.flushSnapshot(done);
    }, (error) => {
      void frontendLog("ERROR", "terminal_write_failed", "一次性终端写入失败", {
        data: { renderer: "one_shot_terminal" },
        error,
      });
    });
    // 控制器只负责宽度变化；内容增长引起的行数变化在每次写入完成后直接 layout（尺寸未变时控制器会跳过）。
    this.resizeController = new TerminalResizeController(
      container,
      () => this.layout(),
      () => this.writeScheduler.isRunning,
    );
  }

  /** 主题切换时换配色；xterm 会按新色板重绘已有内容。 */
  setTheme(theme: ITheme): void {
    if (this.disposed) return;
    this.terminal.options.theme = theme;
  }

  /** 把累计的 Transport 快照应用到本视口，同一快照只写一次。 */
  applySnapshot(next: TerminalSnapshot): void {
    if (this.disposed) return;

    if (next.output === this.desiredSnapshot.output) return;
    this.desiredSnapshot = next;
    this.writeScheduler.request();
  }

  copyText(): string {
    return bufferText(this.terminal.buffer.active);
  }

  scrollToBottom(): void {
    this.terminal.scrollToBottom();
    this.reportPosition();
  }

  private reportPosition(): void {
    if (this.disposed || !this.restored) return;
    const buffer = this.terminal.buffer.active;
    this.options.onPositionChange?.(buffer.viewportY, buffer.viewportY >= buffer.baseY);
  }

  /** 释放 xterm、插件、观察器与延迟的布局任务。 */
  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    for (const disposable of this.disposables) disposable.dispose();
    this.resizeController.dispose();
    this.writeScheduler.dispose();
    this.fitAddon.dispose();
    this.terminal.dispose();
  }

  private flushSnapshot(done: () => void): void {
    if (this.disposed) {
      done();
      return;
    }

    const action = reconcileTerminalOutput(this.renderedSnapshot.output, this.desiredSnapshot.output);

    if (action.kind === "ignore") {
      this.renderedSnapshot = this.desiredSnapshot;
      this.finishFlush(done);
      return;
    }

    const targetSnapshot = this.desiredSnapshot;
    const previousLine = this.terminal.buffer.active.viewportY;
    const wasAtBottom = previousLine >= this.terminal.buffer.active.baseY;
    if (action.kind === "reset") this.terminal.reset();

    if (action.text.length === 0) {
      this.renderedSnapshot = targetSnapshot;
      this.finishFlush(done);
      return;
    }

    this.terminal.write(action.text, () => {
      if (this.disposed) { done(); return; }
      this.renderedSnapshot = targetSnapshot;
      this.layout();
      if (!this.restored) {
        this.restored = true;
        const line = this.options.initialLine;
        if (line !== undefined && line >= 0) this.terminal.scrollToLine(line);
        else this.terminal.scrollToBottom();
      } else if (wasAtBottom) {
        this.terminal.scrollToBottom();
      } else {
        this.terminal.scrollToLine(previousLine);
      }
      this.reportPosition();
      done();
    });
  }

  private setTruncated(truncated: boolean): void {
    if (this.truncated === truncated) return;
    this.truncated = truncated;
    this.options.onTruncatedChange?.(truncated);
  }

  private finishFlush(done: () => void): void {
    done();
    if (!this.disposed) this.layout();
  }

  /** 列数与行数都取容器大小。 */
  private layout(): boolean {
    if (this.disposed) return false;
    const proposed = this.fitAddon.proposeDimensions();
    if (!proposed || !Number.isFinite(proposed.cols)) return false;
    const rows = Math.max(1, proposed.rows);
    if (proposed.cols !== this.terminal.cols || rows !== this.terminal.rows) this.terminal.resize(proposed.cols, rows);
    this.setTruncated(this.terminal.buffer.normal.length >= this.terminal.rows + SCROLLBACK_ROWS);
    return true;
  }
}

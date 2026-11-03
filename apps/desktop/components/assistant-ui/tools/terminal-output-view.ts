import { FitAddon } from "@xterm/addon-fit";
import { Unicode11Addon } from "@xterm/addon-unicode11";
import { Terminal, type IDisposable, type ITheme } from "@xterm/xterm";

import { frontendLog } from "@/lib/logging/frontend-log";
import { registerTerminalLinks, type TerminalLinkOptions } from "@/components/terminal/terminal-links";
import { TerminalResizeController } from "@/components/terminal/terminal-resize-controller";
import { xtermCardTheme, xtermPaneFont } from "@/components/terminal/xterm-appearance";

import { reconcileTerminalOutput } from "./terminal-output-reconciler";
import { TerminalWriteScheduler } from "./terminal-write-scheduler";

export type TerminalSnapshot = {
  output: string;
};

/** 卡片视口最多显示的行数；更多输出进入 xterm 回滚区，在视口内滚动查看。 */
const MAX_VISIBLE_ROWS = 16;
/** 回滚区行数；缓冲到达「视口行数 + 它」即开始丢最早的行。 */
const SCROLLBACK_ROWS = 5000;

export type TerminalOutputViewOptions = {
  resolvedTheme: "light" | "dark";
  /** 输出里的网址与工作区路径可点（规则与底部终端同一处，见 `terminal-links`）。 */
  links?: TerminalLinkOptions;
  /** 缓冲到达容量（开始丢最早的输出）时通知 true，清屏重放后行数回落时通知 false；只在变化时通知。 */
  onTruncatedChange?: (truncated: boolean) => void;
};

/**
 * `execute_terminal` 卡片的一次性输出视口：持有一个只读 xterm 实例及与之对应的输出游标。
 *
 * 游标刻意不与 React 或另一个视口共享：xterm 销毁后已渲染画面失效，替换上来的视口必须从头重放
 * 当前累计快照。本类只渲染 UI，不创建进程、不写 Transport；与交互终端的「会话」无关。
 *
 * 高度跟随内容：列数按容器宽度计算，行数取「已输出行数」并以 `MAX_VISIBLE_ROWS` 封顶，容器高度
 * 自适应 xterm。不能反过来让固定高度的容器决定行数——外层留白、cwd 行一变，xterm 自认可见的末行就会
 * 被裁到容器外且无法滚到（曾经的 bug）；两行输出也不该占一整块空白。
 *
 * 丢行判断看缓冲是否已到容量（`layout` 里每次写入后算）：到了就开始丢最早的行，只有「恰好写满还没丢」那一刻
 * 会早报一行。不用第 0 行上的 marker 被销毁当信号——清屏序列（`clear`、`cls`）也会销毁它。展示数据里的
 * 输出是完整的，丢的只是这个视口的回滚区。
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

  /**
   * 已渲染输出的纯文本（复制用）：借 xterm 自己的选区拼接，折行接回一行、Windows 上用 `\r\n` 换行，
   * 没有 ANSI 与 `\r` 进度帧；回滚区丢掉的最早部分不在其中（与丢行提示一致）。
   */
  copyText(): string {
    this.terminal.selectAll();
    const text = this.terminal.getSelection();
    this.terminal.clearSelection();
    return text.replace(/\s+$/, "");
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
    if (action.kind === "reset") this.terminal.reset();

    if (action.text.length === 0) {
      this.renderedSnapshot = targetSnapshot;
      this.finishFlush(done);
      return;
    }

    this.terminal.write(action.text, () => {
      this.renderedSnapshot = targetSnapshot;
      this.finishFlush(done);
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

  /**
   * 列数取容器宽度，行数取内容行数（封顶）。行数变大时 xterm 会把回滚区的行拉回视口，因此先以少量
   * 行数写入、随后长高不会丢画面。光标行必须计入：xterm 缩小行数时总保持光标行可见，输出以换行结尾时
   * 若不计光标所在的空行，首行会被挤进回滚区（实测两行输出只剩第二行）。代价是末尾多一行空白。
   */
  private layout(): boolean {
    const proposed = this.fitAddon.proposeDimensions();
    if (!proposed || !Number.isFinite(proposed.cols)) return false;
    const buffer = this.terminal.buffer.active;
    const contentRows = buffer.baseY + buffer.cursorY + 1;
    const rows = Math.min(MAX_VISIBLE_ROWS, Math.max(1, contentRows));
    if (proposed.cols !== this.terminal.cols || rows !== this.terminal.rows) this.terminal.resize(proposed.cols, rows);
    this.setTruncated(this.terminal.buffer.normal.length >= this.terminal.rows + SCROLLBACK_ROWS);
    return true;
  }
}

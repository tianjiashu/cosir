import { FitAddon } from "@xterm/addon-fit";
import { Unicode11Addon } from "@xterm/addon-unicode11";
import { Terminal } from "@xterm/xterm";

import { frontendLog } from "@/lib/logging/frontend-log";
import { TerminalResizeController } from "@/components/terminal/terminal-resize-controller";
import { xtermAppearance } from "@/components/terminal/xterm-appearance";

import { reconcileTerminalOutput } from "./terminal-output-reconciler";
import { TerminalWriteScheduler } from "./terminal-write-scheduler";

export type TerminalSnapshot = {
  output: string;
};

/** 卡片视口最多显示的行数；更多输出进入 xterm 回滚区，在视口内滚动查看。 */
const MAX_VISIBLE_ROWS = 16;

/**
 * `execute_terminal` 卡片的一次性输出视口：持有一个只读 xterm 实例及与之对应的输出游标。
 *
 * 游标刻意不与 React 或另一个视口共享：xterm 销毁后已渲染画面失效，替换上来的视口必须从头重放
 * 当前累计快照。本类只渲染 UI，不创建进程、不写 Transport；与交互终端的「会话」无关。
 *
 * 高度跟随内容：列数按容器宽度计算，行数取「已输出行数」并以 `MAX_VISIBLE_ROWS` 封顶，容器高度
 * 自适应 xterm。不能反过来让固定高度的容器决定行数——外层留白、cwd 行一变，xterm 自认可见的末行就会
 * 被裁到容器外且无法滚到（曾经的 bug）；两行输出也不该占一整块空白。
 */
export class TerminalOutputView {
  private readonly terminal: Terminal;
  private readonly fitAddon: FitAddon;
  private readonly writeScheduler: TerminalWriteScheduler;
  private readonly resizeController: TerminalResizeController;
  private desiredSnapshot: TerminalSnapshot = { output: "" };
  private renderedSnapshot: TerminalSnapshot = { output: "" };
  private disposed = false;

  constructor(container: HTMLElement) {
    this.fitAddon = new FitAddon();
    this.terminal = new Terminal({
      // unicode 版本切换属于 proposed API。
      allowProposedApi: true,
      allowTransparency: false,
      // Windows 管道工具常只输出 LF 不带 CR；真实终端会把它当作回到行首的新行，
      // xterm 需要此选项才能在一次性、非交互输出里保持同样表现。
      convertEol: true,
      cursorBlink: false,
      // 只读视口从不聚焦，空白的光标行不应画出光标块。
      cursorInactiveStyle: "none",
      disableStdin: true,
      scrollback: 5000,
      ...xtermAppearance(),
    });

    this.terminal.loadAddon(this.fitAddon);
    // 与交互终端一致按 Unicode 11 算字宽，emoji / CJK 符号不错位。
    this.terminal.loadAddon(new Unicode11Addon());
    this.terminal.unicode.activeVersion = "11";
    this.terminal.open(container);
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

  /** 把累计的 Transport 快照应用到本视口，同一快照只写一次。 */
  applySnapshot(next: TerminalSnapshot): void {
    if (this.disposed) return;

    if (next.output === this.desiredSnapshot.output) return;
    this.desiredSnapshot = next;
    this.writeScheduler.request();
  }

  /** 释放 xterm、插件、观察器与延迟的布局任务。 */
  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
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
    return true;
  }
}

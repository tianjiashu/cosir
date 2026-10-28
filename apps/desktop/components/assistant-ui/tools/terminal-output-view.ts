import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";

import { frontendLog } from "@/lib/logging/frontend-log";
import { TerminalResizeController } from "@/components/terminal/terminal-resize-controller";
import { xtermAppearance } from "@/components/terminal/xterm-appearance";

import { reconcileTerminalOutput } from "./terminal-output-reconciler";
import { TerminalWriteScheduler } from "./terminal-write-scheduler";

export type TerminalSnapshot = {
  output: string;
};

/**
 * `execute_terminal` 卡片的一次性输出视口：持有一个只读 xterm 实例及与之对应的输出游标。
 *
 * 游标刻意不与 React 或另一个视口共享：xterm 销毁后已渲染画面失效，替换上来的视口必须从头重放
 * 当前累计快照。本类只渲染 UI，不创建进程、不写 Transport；与交互终端的「会话」无关。
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
      allowTransparency: false,
      // Windows 管道工具常只输出 LF 不带 CR；真实终端会把它当作回到行首的新行，
      // xterm 需要此选项才能在一次性、非交互输出里保持同样表现。
      convertEol: true,
      cursorBlink: false,
      disableStdin: true,
      scrollback: 5000,
      ...xtermAppearance(),
    });

    this.terminal.loadAddon(this.fitAddon);
    this.terminal.open(container);
    this.writeScheduler = new TerminalWriteScheduler((done) => {
      this.flushSnapshot(done);
    }, (error) => {
      void frontendLog("ERROR", "terminal_write_failed", "一次性终端写入失败", {
        data: { renderer: "one_shot_terminal" },
        error,
      });
    });
    this.resizeController = new TerminalResizeController(
      container,
      () => this.fitAddon.fit(),
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

  /** 宿主面板尺寸变化后请求重新计算行列。 */
  fit(): void {
    if (this.disposed) return;
    this.resizeController.request();
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
    this.resizeController.request();
  }
}

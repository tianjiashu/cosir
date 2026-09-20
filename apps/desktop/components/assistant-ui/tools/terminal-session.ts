import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";

import { frontendLog } from "@/lib/logging/frontend-log";
import { TerminalResizeController } from "@/components/terminal/terminal-resize-controller";

import { reconcileTerminalOutput } from "./terminal-output-reconciler";
import { TerminalWriteScheduler } from "./terminal-write-scheduler";

export type TerminalSnapshot = {
  output: string;
  outputSeq?: number;
};

/**
 * Owns one xterm instance and the output cursor associated with that instance.
 *
 * The cursor is deliberately not shared with React or another TerminalSession:
 * disposing an xterm invalidates its rendered screen, so a replacement session
 * must replay the current cumulative snapshot from the beginning. This class
 * only renders UI state; it does not create processes or write to Transport.
 */
export class TerminalSession {
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
      // Piped Windows tools commonly emit LF without a preceding CR. A real
      // terminal treats that as a new line at column zero; xterm needs this
      // option to preserve that behavior for one-shot, non-interactive output.
      convertEol: true,
      cursorBlink: false,
      disableStdin: true,
      fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
      fontSize: 12,
      lineHeight: 1.35,
      scrollback: 5000,
      theme: {
        background: "#09090b",
        foreground: "#d4d4d8",
        cursor: "#a1a1aa",
        selectionBackground: "#3f3f46",
      },
    });

    this.terminal.loadAddon(this.fitAddon);
    this.terminal.open(container);
    try {
      this.fitAddon.fit();
    } catch {
      // The host can be between React layout phases; the resize controller retries.
    }
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
      () => {
        try {
          this.fitAddon.fit();
          return true;
        } catch {
          return false;
        }
      },
      () => this.writeScheduler.isRunning,
    );
  }

  /** Apply a cumulative Transport snapshot to this session exactly once. */
  applySnapshot(next: TerminalSnapshot): void {
    if (this.disposed) return;

    const action = reconcileTerminalOutput({
      previousOutput: this.desiredSnapshot.output,
      previousSeq: this.desiredSnapshot.outputSeq,
      nextOutput: next.output,
      nextSeq: next.outputSeq,
    });

    if (action.kind !== "ignore") {
      this.desiredSnapshot = next;
      this.writeScheduler.request();
    } else if (
      next.outputSeq !== undefined
      && (this.desiredSnapshot.outputSeq === undefined || next.outputSeq > this.desiredSnapshot.outputSeq)
    ) {
      this.desiredSnapshot = next;
      this.writeScheduler.request();
    }
  }

  /** Request a dimension recalculation after the host panel changes size. */
  fit(): void {
    if (this.disposed) return;
    this.resizeController.request();
  }

  /** Dispose the terminal, addons, observers, and deferred layout work. */
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

    const action = reconcileTerminalOutput({
      previousOutput: this.renderedSnapshot.output,
      previousSeq: this.renderedSnapshot.outputSeq,
      nextOutput: this.desiredSnapshot.output,
      nextSeq: this.desiredSnapshot.outputSeq,
    });

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

import "@xterm/xterm/css/xterm.css";

import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import { TerminalResizeController } from "./terminal-resize-controller";

/**
 * 只读交互终端的 xterm 容器。
 *
 * 与一次性命令的 cumulative snapshot renderer 分开：交互 session 接收后端的
 * 原始 bytes，交给 xterm 解释 CR/ANSI/光标移动，不经过 React 文本节点。
 */
export class TerminalStreamSession {
  private readonly terminal: Terminal;
  private readonly fitAddon: FitAddon;
  private readonly resizeController: TerminalResizeController;

  constructor(container: HTMLElement) {
    this.terminal = new Terminal({
      convertEol: false,
      cursorBlink: false,
      disableStdin: true,
      scrollback: 10_000,
      theme: {
        background: "#09090b",
        foreground: "#e4e4e7",
        cursor: "#a1a1aa",
      },
      fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
      fontSize: 12,
      lineHeight: 1.35,
    });
    this.fitAddon = new FitAddon();
    this.terminal.loadAddon(this.fitAddon);
    this.terminal.open(container);
    try {
      this.fitAddon.fit();
    } catch {
      // The host can be between React layout phases; the resize controller retries.
    }
    this.resizeController = new TerminalResizeController(container, () => this.fit());
  }

  write(data: Uint8Array): void {
    this.terminal.write(data);
  }

  clear(): void {
    this.terminal.clear();
    this.terminal.reset();
  }

  dispose(): void {
    this.resizeController.dispose();
    this.terminal.dispose();
  }

  private fit(): boolean {
    if (!this.terminal.element?.isConnected) return false;
    try {
      this.fitAddon.fit();
      return true;
    } catch {
      // The element can be between React layout phases; the next resize retries.
      return false;
    }
  }
}

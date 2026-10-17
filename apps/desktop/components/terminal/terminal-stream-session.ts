import "@xterm/xterm/css/xterm.css";
import { Terminal } from "@xterm/xterm";
import { xtermAppearance } from "./xterm-appearance";

/** 只读预览沿用 worker 的逻辑尺寸；容器滚动，不因面板缩放重新折行。 */
export class TerminalStreamSession {
  private readonly terminal: Terminal;

  constructor(container: HTMLElement) {
    this.terminal = new Terminal({
      convertEol: false,
      cursorBlink: false,
      disableStdin: true,
      scrollback: 10_000,
      ...xtermAppearance(),
    });
    this.terminal.open(container);
  }

  setGeometry(rows: number, columns: number): void {
    this.terminal.resize(columns, rows);
  }

  write(data: Uint8Array): void { this.terminal.write(data); }

  clear(): void {
    this.terminal.clear();
    this.terminal.reset();
  }

  dispose(): void { this.terminal.dispose(); }
}

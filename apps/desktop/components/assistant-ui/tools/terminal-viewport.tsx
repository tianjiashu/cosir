import "@xterm/xterm/css/xterm.css";

import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import { useEffect, useLayoutEffect, useRef } from "react";

import { reconcileTerminalOutput } from "./terminal-output-reconciler";

type TerminalViewportProps = {
  output: string;
  outputSeq?: number;
};

type TerminalCursor = {
  output: string;
  seq?: number;
};

/**
 * 在一次性 terminal 工具卡片中维护一个只读 xterm 实例。
 *
 * React 只负责承载 backend snapshot 的原始输出和序号；xterm 负责跨 chunk
 * 维护 ANSI/VT 控制序列、光标、擦除和回车覆盖状态。组件不创建进程、不发送
 * 输入，也不把终端屏幕状态写回 Assistant Transport。
 */
export function TerminalViewport({ output, outputSeq }: TerminalViewportProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const terminalRef = useRef<Terminal | null>(null);
  const cursorRef = useRef<TerminalCursor>({ output: "" });

  useLayoutEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const fitAddon = new FitAddon();
    const terminal = new Terminal({
      allowTransparency: false,
      convertEol: false,
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

    terminal.loadAddon(fitAddon);
    terminal.open(container);
    fitAddon.fit();
    terminalRef.current = terminal;

    const resizeObserver = typeof ResizeObserver === "undefined"
      ? null
      : new ResizeObserver(() => {
        fitAddon.fit();
      });
    resizeObserver?.observe(container);

    return () => {
      resizeObserver?.disconnect();
      terminalRef.current = null;
      terminal.dispose();
    };
  }, []);

  useEffect(() => {
    const terminal = terminalRef.current;
    if (!terminal) return;

    const previous = cursorRef.current;
    const action = reconcileTerminalOutput({
      previousOutput: previous.output,
      previousSeq: previous.seq,
      nextOutput: output,
      nextSeq: outputSeq,
    });

    if (action.kind === "append") {
      terminal.write(action.text);
    } else if (action.kind === "reset") {
      terminal.reset();
      terminal.write(action.text);
    }

    if (action.kind !== "ignore") {
      cursorRef.current = { output, seq: outputSeq };
    } else if (outputSeq !== undefined && (previous.seq === undefined || outputSeq > previous.seq)) {
      cursorRef.current = { output, seq: outputSeq };
    }
  }, [output, outputSeq]);

  return (
    <div
      className="h-72 min-h-24 overflow-hidden px-3 py-2"
      data-terminal-viewport="true"
      data-terminal-output-seq={outputSeq ?? ""}
    >
      <div ref={containerRef} className="h-full w-full" />
    </div>
  );
}

import "@xterm/xterm/css/xterm.css";

import { useEffect, useLayoutEffect, useRef } from "react";

import { TerminalSession, type TerminalSnapshot } from "./terminal-session";

type TerminalViewportProps = {
  output: string;
  outputSeq?: number;
};

/**
 * 在一次性 terminal 工具卡片中维护一个只读 xterm session。
 *
 * React 只负责承载 backend snapshot；TerminalSession 负责把一个 xterm
 * 实例与该实例的输出游标、尺寸监听和清理绑定在一起。组件不创建进程、不发送
 * 输入，也不把终端屏幕状态写回 Assistant Transport。
 */
export function TerminalViewport({ output, outputSeq }: TerminalViewportProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const sessionRef = useRef<TerminalSession | null>(null);
  const snapshotRef = useRef<TerminalSnapshot>({ output, outputSeq });
  snapshotRef.current = { output, outputSeq };

  useLayoutEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const session = new TerminalSession(container);
    sessionRef.current = session;
    // A newly created xterm has an empty screen even if React's output props
    // have not changed. Replay the current cumulative snapshot immediately;
    // this is also what makes StrictMode effect replay safe.
    session.applySnapshot(snapshotRef.current);

    return () => {
      if (sessionRef.current === session) sessionRef.current = null;
      session.dispose();
    };
  }, []);

  useEffect(() => {
    sessionRef.current?.applySnapshot({ output, outputSeq });
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

import "@xterm/xterm/css/xterm.css";

import { useEffect, useLayoutEffect, useRef } from "react";

import { TerminalOutputView, type TerminalSnapshot } from "./terminal-output-view";

type TerminalViewportProps = {
  output: string;
};

/**
 * 在一次性 terminal 工具卡片中维护一个只读 xterm session。
 *
 * React 只负责承载 backend snapshot；TerminalOutputView 负责把一个 xterm
 * 实例与该实例的输出游标、尺寸监听和清理绑定在一起。组件不创建进程、不发送
 * 输入，也不把终端屏幕状态写回 Assistant Transport。
 */
export function TerminalViewport({ output }: TerminalViewportProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const viewRef = useRef<TerminalOutputView | null>(null);
  const snapshotRef = useRef<TerminalSnapshot>({ output });
  snapshotRef.current = { output };

  useLayoutEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const view = new TerminalOutputView(container);
    viewRef.current = view;
    // A newly created xterm has an empty screen even if React's output props
    // have not changed. Replay the current cumulative snapshot immediately;
    // this is also what makes StrictMode effect replay safe.
    view.applySnapshot(snapshotRef.current);

    return () => {
      if (viewRef.current === view) viewRef.current = null;
      view.dispose();
    };
  }, []);

  useEffect(() => {
    viewRef.current?.applySnapshot({ output });
  }, [output]);

  return (
    <div
      className="h-72 min-h-24 overflow-hidden px-3 py-2"
      data-terminal-viewport="true"
    >
      <div ref={containerRef} className="h-full w-full" />
    </div>
  );
}

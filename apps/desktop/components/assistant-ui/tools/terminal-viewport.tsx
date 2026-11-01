import "@xterm/xterm/css/xterm.css";

import { useEffect, useLayoutEffect, useRef } from "react";

import { xtermCardTheme } from "@/components/terminal/xterm-appearance";
import { useThemeState } from "@/lib/theme/theme-preference";
import { TerminalOutputView, type TerminalSnapshot } from "./terminal-output-view";

type TerminalViewportProps = {
  output: string;
};

/**
 * 在 `execute_terminal` 调用行的展开体里维护一个只读 xterm session。
 *
 * React 只负责承载 backend snapshot；TerminalOutputView 负责把一个 xterm
 * 实例与该实例的输出游标、尺寸监听和清理绑定在一起。组件不创建进程、不发送
 * 输入，也不把终端屏幕状态写回 Assistant Transport。配色跟随应用主题（与终端工作台同一套），
 * 主题切换时只换色、不重建实例。
 */
export function TerminalViewport({ output }: TerminalViewportProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const viewRef = useRef<TerminalOutputView | null>(null);
  const snapshotRef = useRef<TerminalSnapshot>({ output });
  snapshotRef.current = { output };
  const { resolvedTheme } = useThemeState();
  const initialThemeRef = useRef(resolvedTheme);

  useLayoutEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const view = new TerminalOutputView(container, initialThemeRef.current);
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

  useEffect(() => {
    viewRef.current?.setTheme(xtermCardTheme(resolvedTheme));
  }, [resolvedTheme]);

  // 高度由 TerminalOutputView 按内容行数决定，这里不设高度、不加留白（留白归卡片容器）。
  return <div ref={containerRef} className="w-full" data-terminal-viewport="true" />;
}

import "@xterm/xterm/css/xterm.css";

import { useEffect, useImperativeHandle, useLayoutEffect, useRef, type Ref } from "react";

import type { TerminalLinkOptions } from "@/components/terminal/terminal-links";
import { xtermCardTheme } from "@/components/terminal/xterm-appearance";
import { useThemeState } from "@/lib/theme/theme-preference";
import { TerminalOutputView, type TerminalSnapshot } from "./terminal-output-view";

/** 视口对外只开放「取已渲染文本」：复制按钮在点击时才读，xterm 实例不出视口。 */
export type TerminalViewportHandle = {
  copyText: () => string;
  scrollToBottom: () => void;
};

type TerminalViewportProps = {
  output: string;
  initialLine?: number;
  onPositionChange?: (line: number, atBottom: boolean) => void;
  links: TerminalLinkOptions;
  onTruncatedChange: (truncated: boolean) => void;
  ref?: Ref<TerminalViewportHandle>;
};

/**
 * 在命令输出阅读器里维护一个只读 xterm session。
 *
 * React 只负责承载 backend snapshot；TerminalOutputView 负责把一个 xterm
 * 实例与该实例的输出游标、尺寸监听和清理绑定在一起。组件不创建进程、不发送
 * 输入，也不把终端屏幕状态写回 Assistant Transport。配色跟随应用主题（与终端工作台同一套），
 * 主题切换时只换色、不重建实例；链接打开回调与丢行回调经 ref 现读，同理不重建。
 */
export function TerminalViewport({ output, links, onTruncatedChange, initialLine, onPositionChange, ref }: TerminalViewportProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const viewRef = useRef<TerminalOutputView | null>(null);
  const snapshotRef = useRef<TerminalSnapshot>({ output });
  snapshotRef.current = { output };
  const latestRef = useRef({ links, onTruncatedChange, onPositionChange });
  latestRef.current = { links, onTruncatedChange, onPositionChange };
  const initialLineRef = useRef(initialLine);
  const { resolvedTheme } = useThemeState();
  const initialThemeRef = useRef(resolvedTheme);
  initialThemeRef.current = resolvedTheme;

  useImperativeHandle(ref, () => ({ copyText: () => viewRef.current?.copyText() ?? "", scrollToBottom: () => viewRef.current?.scrollToBottom() }), []);

  useLayoutEffect(() => {
    const container = containerRef.current;
    if (!container || viewRef.current) return;

    const view = new TerminalOutputView(container, {
      resolvedTheme: initialThemeRef.current,
      initialLine: initialLineRef.current,
      onPositionChange: (line, bottom) => latestRef.current.onPositionChange?.(line, bottom),
      links: {
        getWorkspaceRoot: () => latestRef.current.links.getWorkspaceRoot(),
        onOpenLink: (target) => latestRef.current.links.onOpenLink(target),
      },
      onTruncatedChange: (truncated) => latestRef.current.onTruncatedChange(truncated),
    });
    viewRef.current = view;
    // A newly created xterm has an empty screen even if React's output props
    // have not changed. Replay the current cumulative snapshot immediately;
    // this is also what makes StrictMode effect replay safe.
    view.applySnapshot(snapshotRef.current);

  }, []);

  useLayoutEffect(() => () => {
    viewRef.current?.dispose();
    viewRef.current = null;
  }, []);

  useEffect(() => {
    viewRef.current?.applySnapshot({ output });
  }, [output]);

  useEffect(() => {
    viewRef.current?.setTheme(xtermCardTheme(resolvedTheme));
  }, [resolvedTheme]);

  return <div ref={containerRef} className="h-full min-h-0 w-full" data-terminal-viewport="true" data-terminal-mode="reader" />;
}

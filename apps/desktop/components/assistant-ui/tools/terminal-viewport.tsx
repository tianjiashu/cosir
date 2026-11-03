import "@xterm/xterm/css/xterm.css";

import { useEffect, useImperativeHandle, useLayoutEffect, useRef, type Ref } from "react";

import type { TerminalLinkOptions } from "@/components/terminal/terminal-links";
import { xtermCardTheme } from "@/components/terminal/xterm-appearance";
import { useThemeState } from "@/lib/theme/theme-preference";
import { TerminalOutputView, type TerminalSnapshot } from "./terminal-output-view";

/** 视口对外只开放「取已渲染文本」：复制按钮在点击时才读，xterm 实例不出视口。 */
export type TerminalViewportHandle = {
  copyText: () => string;
};

type TerminalViewportProps = {
  output: string;
  links: TerminalLinkOptions;
  onTruncatedChange: (truncated: boolean) => void;
  ref?: Ref<TerminalViewportHandle>;
};

/**
 * 在 `execute_terminal` 调用行的展开体里维护一个只读 xterm session。
 *
 * React 只负责承载 backend snapshot；TerminalOutputView 负责把一个 xterm
 * 实例与该实例的输出游标、尺寸监听和清理绑定在一起。组件不创建进程、不发送
 * 输入，也不把终端屏幕状态写回 Assistant Transport。配色跟随应用主题（与终端工作台同一套），
 * 主题切换时只换色、不重建实例；链接打开回调与丢行回调经 ref 现读，同理不重建。
 */
export function TerminalViewport({ output, links, onTruncatedChange, ref }: TerminalViewportProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const viewRef = useRef<TerminalOutputView | null>(null);
  const snapshotRef = useRef<TerminalSnapshot>({ output });
  snapshotRef.current = { output };
  const latestRef = useRef({ links, onTruncatedChange });
  latestRef.current = { links, onTruncatedChange };
  const { resolvedTheme } = useThemeState();
  const initialThemeRef = useRef(resolvedTheme);

  useImperativeHandle(ref, () => ({ copyText: () => viewRef.current?.copyText() ?? "" }), []);

  useLayoutEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const view = new TerminalOutputView(container, {
      resolvedTheme: initialThemeRef.current,
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

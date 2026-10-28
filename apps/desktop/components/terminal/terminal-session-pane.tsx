import { useEffect, useLayoutEffect, useRef } from "react";

import { useThemeState } from "@/lib/theme/theme-preference";
import { cn } from "@/lib/utils";

import { TerminalView, type TerminalViewRuntime } from "./terminal-view";
import { xtermPaneTheme } from "./xterm-appearance";

type TerminalSessionPaneProps = {
  taskId: number;
  sessionId: string;
  /** 当前选中且面板可见：只有它可见、可交互并获取焦点。 */
  visible: boolean;
  focusToken: number;
  workspaceRoot?: string;
  onRuntime: (sessionId: string, runtime: TerminalViewRuntime) => void;
  onSearchResults: (sessionId: string, result: { index: number; count: number }) => void;
  onOpenSearch: () => void;
  onOpenWorkspaceFile?: (relativePath: string) => void;
  registerView: (sessionId: string, view: TerminalView | null) => void;
};

/**
 * 一个终端会话的窗格：挂载即创建 xterm 与 WS，卸载即销毁（何时挂载 / 卸载由父组件按
 * `mountedTerminalSessionIds` 决定）。
 *
 * 两种隐藏：面板内未选中的标签用 `invisible`，容器仍占同样的布局尺寸，切回时无需重新测量；面板
 * 收起时外层工作台是 `hidden`（display:none），容器量到 0×0——`TerminalResizeController` 遇到零尺寸
 * 直接跳过 fit，不会把 0 行列发给后端，重新可见时由 `refresh` 补一次 fit。
 */
export function TerminalSessionPane({
  taskId,
  sessionId,
  visible,
  focusToken,
  workspaceRoot,
  onRuntime,
  onSearchResults,
  onOpenSearch,
  onOpenWorkspaceFile,
  registerView,
}: TerminalSessionPaneProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const viewRef = useRef<TerminalView | null>(null);
  const { resolvedTheme } = useThemeState();
  // 回调与工作区根目录经 ref 读取最新值：xterm 与 WS 只按会话身份创建一次，不因父组件重渲染或
  // 根目录变化（如目录加载完成）重建——重建会丢掉超出后端缓冲的历史。
  const latestRef = useRef({ onRuntime, onSearchResults, onOpenSearch, onOpenWorkspaceFile, workspaceRoot });
  latestRef.current = { onRuntime, onSearchResults, onOpenSearch, onOpenWorkspaceFile, workspaceRoot };
  const initialThemeRef = useRef(resolvedTheme);

  useLayoutEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const view = new TerminalView(container, {
      taskId,
      sessionId,
      theme: xtermPaneTheme(initialThemeRef.current),
      getWorkspaceRoot: () => latestRef.current.workspaceRoot,
      onRuntime: (runtime) => latestRef.current.onRuntime(sessionId, runtime),
      onSearchResults: (result) => latestRef.current.onSearchResults(sessionId, result),
      onOpenSearch: () => latestRef.current.onOpenSearch(),
      onOpenWorkspaceFile: (path) => latestRef.current.onOpenWorkspaceFile?.(path),
    });
    viewRef.current = view;
    registerView(sessionId, view);
    return () => {
      registerView(sessionId, null);
      viewRef.current = null;
      view.dispose();
    };
  }, [registerView, sessionId, taskId]);

  useEffect(() => {
    viewRef.current?.setTheme(xtermPaneTheme(resolvedTheme));
  }, [resolvedTheme]);

  useEffect(() => {
    if (!visible) return;
    viewRef.current?.refresh();
  }, [visible]);

  useEffect(() => {
    if (visible) viewRef.current?.focus();
  }, [focusToken, visible]);

  return (
    <div
      className={cn("absolute inset-0 px-3 py-2", !visible && "invisible pointer-events-none")}
      aria-hidden={!visible || undefined}
      data-terminal-session={sessionId}
      data-active={visible || undefined}
    >
      <div ref={containerRef} className="h-full w-full" />
    </div>
  );
}

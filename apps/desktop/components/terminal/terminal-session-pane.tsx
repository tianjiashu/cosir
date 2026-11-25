import { useEffect, useLayoutEffect, useRef } from "react";

import type { LinkTarget } from "@/lib/links/resolve-link";
import { useThemeState } from "@/lib/theme/theme-preference";
import { cn } from "@/lib/utils";

import { TerminalView, type TerminalViewRuntime } from "./terminal-view";
import { xtermPaneTheme } from "./xterm-appearance";

type TerminalSessionPaneProps = {
  sessionId: number;
  terminalSessionId: string;
  /** 当前选中且面板可见：只有它可见、可交互，也只有它执行焦点请求。 */
  visible: boolean;
  /** 有一次待执行的焦点请求（见 store 的 `focusPending`）；执行后经 `onFocused` 交回。 */
  focusPending: boolean;
  onFocused: () => void;
  workspaceRoot?: string;
  onRuntime: (terminalSessionId: string, runtime: TerminalViewRuntime) => void;
  onSearchResults: (terminalSessionId: string, result: { index: number; count: number }) => void;
  onOpenSearch: () => void;
  onOpenLink: (target: LinkTarget) => void;
  registerView: (terminalSessionId: string, view: TerminalView | null) => void;
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
  sessionId,
  terminalSessionId,
  visible,
  focusPending,
  onFocused,
  workspaceRoot,
  onRuntime,
  onSearchResults,
  onOpenSearch,
  onOpenLink,
  registerView,
}: TerminalSessionPaneProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const viewRef = useRef<TerminalView | null>(null);
  const { resolvedTheme } = useThemeState();
  // 回调与工作区根目录经 ref 读取最新值：xterm 与 WS 只按会话身份创建一次，不因父组件重渲染或
  // 根目录变化（如目录加载完成）重建——重建会丢掉超出后端缓冲的历史。
  const latestRef = useRef({ onRuntime, onSearchResults, onOpenSearch, onOpenLink, onFocused, workspaceRoot });
  latestRef.current = { onRuntime, onSearchResults, onOpenSearch, onOpenLink, onFocused, workspaceRoot };
  const initialThemeRef = useRef(resolvedTheme);

  useLayoutEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const view = new TerminalView(container, {
      sessionId,
      terminalSessionId,
      theme: xtermPaneTheme(initialThemeRef.current),
      getWorkspaceRoot: () => latestRef.current.workspaceRoot,
      onRuntime: (runtime) => latestRef.current.onRuntime(terminalSessionId, runtime),
      onSearchResults: (result) => latestRef.current.onSearchResults(terminalSessionId, result),
      onOpenSearch: () => latestRef.current.onOpenSearch(),
      onOpenLink: (target) => latestRef.current.onOpenLink(target),
    });
    viewRef.current = view;
    registerView(terminalSessionId, view);
    return () => {
      registerView(terminalSessionId, null);
      viewRef.current = null;
      view.dispose();
    };
  }, [registerView, terminalSessionId, sessionId]);

  useEffect(() => {
    viewRef.current?.setTheme(xtermPaneTheme(resolvedTheme));
  }, [resolvedTheme]);

  useEffect(() => {
    // 隐藏（切到别的会话、收起面板）即清掉搜索高亮：搜索框只跟随可见会话，留下的高亮会无从关闭。
    if (!visible) {
      viewRef.current?.clearSearch();
      return;
    }
    viewRef.current?.refresh();
  }, [visible]);

  // 只执行请求，不因「变得可见」聚焦：Agent 填入命令也会让窗格出现（见 store 的 `focusPending`）。
  useEffect(() => {
    if (!visible || !focusPending) return;
    viewRef.current?.focus();
    latestRef.current.onFocused();
  }, [focusPending, visible]);

  return (
    <div
      className={cn("absolute inset-0 px-3 py-2", !visible && "invisible pointer-events-none")}
      aria-hidden={!visible || undefined}
      data-terminal-session={terminalSessionId}
      data-active={visible || undefined}
    >
      <div ref={containerRef} className="h-full w-full" />
    </div>
  );
}

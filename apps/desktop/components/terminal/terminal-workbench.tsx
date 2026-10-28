import { useQuery } from "@tanstack/react-query";
import { Maximize2Icon, Minimize2Icon, XIcon } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { TooltipIconButton } from "@/components/tooltip-icon-button";
import { Button } from "@/components/ui/button";
import { PanelDock } from "@/components/ui/panel-dock";
import { PANEL_HEADER_CLASS } from "@/components/ui/panel-tokens";
import { useReferenceBridge } from "@/components/workspace-references/reference-bridge";
import { queryKeys } from "@/lib/query/query-keys";
import { listTerminalShells } from "@/lib/terminal/terminal-api";
import type { TerminalConnectionState } from "@/lib/terminal/terminal-connection";
import {
  endedTerminalSessions,
  isActiveTerminalSession,
  mountedTerminalSessionIds,
  terminalTabs,
} from "@/lib/terminal/terminal-sessions";
import { selectTerminalTask, useTerminalWorkbenchStore } from "@/lib/terminal/terminal-workbench-store";
import { cn } from "@/lib/utils";
import { useFilesStore } from "@/lib/workspace-files/store";
import { useWorkbenchStore } from "@/lib/workbench/store";

import { TERMINAL_CONNECTION_LABEL, TerminalMoreMenu } from "./terminal-menus";
import { TerminalSearchBar } from "./terminal-search-bar";
import { TerminalSessionPane } from "./terminal-session-pane";
import { TerminalTabBar } from "./terminal-tab-bar";
import type { TerminalView, TerminalViewRuntime } from "./terminal-view";
import { MIN_TERMINAL_PANEL_HEIGHT, useTerminalPanelHeight } from "./use-terminal-panel-height";
import { type PanelNotice, useTerminalSessionActions } from "./use-terminal-session-actions";

function noticeClass(tone: PanelNotice["tone"]): string {
  return tone === "destructive" ? "text-destructive" : tone === "warning" ? "text-warning" : "text-muted-foreground";
}

/** 连接状态之上的常驻输入提示：会话结束或断线时输入已禁用，明确告诉用户而不是静默吞键。 */
function inputBlockedText(connection: TerminalConnectionState | undefined): string | null {
  if (connection === "ended") return "会话已结束，输入已停用";
  if (connection === "expired") return "该会话已不在后端（可能因后端重启），输入已停用；可点击「+」新建终端";
  if (connection === "reconnecting") return "连接中断，正在重新连接（画面保留），输入暂停";
  if (connection === "failed") return "连接失败，输入已停用；可在 ⋮ 菜单中重新连接";
  return null;
}

/**
 * Task 级底部终端工作台：会话标签栏（含「+」与选 shell 下拉）、⋮ 菜单（已结束会话 / 重连）、
 * 展开、关闭（只收起，不结束会话）。本组件只做装配：标签栏与菜单、新建 / 关闭、面板高度各在同目录
 * 的组件与 hook 里。
 *
 * 会话列表只读 snapshot 镜像（`useTerminalWorkbenchStore`），新建 / 关闭经 HTTP 后等 snapshot 回推；
 * 每个会话的 xterm + WS 由 `TerminalSessionPane` 承载，挂载 / 释放规则见 `mountedTerminalSessionIds`
 * （活跃会话常驻、面板收起只隐藏；已结束会话只在查看时存在）。窗格卸载即销毁 xterm 与 WS，并删掉
 * 该会话的运行期状态。首次打开面板之前什么都不挂载，交接等事件通过 snapshot 让 store 自动打开面板。
 */
export function TerminalWorkbench({ taskId }: { taskId: number }) {
  const state = useTerminalWorkbenchStore(selectTerminalTask(taskId));
  const { sessions, open, activeSessionId, focusToken, autoCreate } = state;
  const bridge = useReferenceBridge();
  const [everOpened, setEverOpened] = useState(open);
  const [mounted, setMounted] = useState<readonly string[]>([]);
  const [runtimes, setRuntimes] = useState<Record<string, TerminalViewRuntime>>({});
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchResult, setSearchResult] = useState<{ index: number; count: number } | null>(null);
  const viewsRef = useRef(new Map<string, TerminalView>());
  const sectionRef = useRef<HTMLElement | null>(null);
  const { panelHeight, maxPanelHeight, expanded, toggleExpanded, resizePanel } = useTerminalPanelHeight();

  useEffect(() => {
    if (open) setEverOpened(true);
  }, [open]);

  useEffect(() => {
    setMounted((current) => mountedTerminalSessionIds(current, sessions ?? [], activeSessionId, open));
  }, [activeSessionId, open, sessions]);

  // 收起面板时焦点若还在隐藏的终端里，交还给页面，避免按键继续进入看不见的终端。
  useEffect(() => {
    if (open) return;
    const active = document.activeElement;
    if (active instanceof HTMLElement && sectionRef.current?.contains(active)) active.blur();
  }, [open]);

  const tabs = useMemo(() => terminalTabs(sessions ?? [], activeSessionId), [activeSessionId, sessions]);
  const { creating, notice, createSession, closeTab } = useTerminalSessionActions(taskId, sessions, tabs);

  // 打开面板时没有活跃会话：等会话列表已知后自动新建一个默认 shell 终端（只在打开那一刻判定）。
  useEffect(() => {
    if (!open || !autoCreate || sessions === null) return;
    useTerminalWorkbenchStore.getState().consumeAutoCreate(taskId);
    if (!sessions.some(isActiveTerminalSession)) void createSession("auto");
  }, [autoCreate, createSession, open, sessions, taskId]);

  const ended = useMemo(() => endedTerminalSessions(sessions ?? []), [sessions]);
  const activeSession = sessions?.find((session) => session.sessionId === activeSessionId) ?? null;
  const activeRuntime = activeSessionId ? runtimes[activeSessionId] : undefined;
  const activeHandoff = activeSession?.handoff ?? null;

  const shellsQuery = useQuery({
    queryKey: queryKeys.terminalShells(),
    queryFn: ({ signal }) => listTerminalShells({ signal }),
    enabled: everOpened,
    staleTime: 60_000,
  });

  // 窗格卸载（会话结束后不再查看、从 snapshot 消失）时同步删掉它的运行期状态，不留孤儿条目。
  const registerView = useCallback((sessionId: string, view: TerminalView | null) => {
    if (view) {
      viewsRef.current.set(sessionId, view);
      return;
    }
    viewsRef.current.delete(sessionId);
    setRuntimes((current) => {
      if (!(sessionId in current)) return current;
      const next = { ...current };
      delete next[sessionId];
      return next;
    });
  }, []);
  const handleRuntime = useCallback((sessionId: string, runtime: TerminalViewRuntime) => {
    setRuntimes((current) => ({ ...current, [sessionId]: runtime }));
  }, []);
  const handleSearchResults = useCallback((sessionId: string, result: { index: number; count: number }) => {
    if (sessionId === useTerminalWorkbenchStore.getState().tasks[taskId]?.activeSessionId) setSearchResult(result);
  }, [taskId]);
  const openSearch = useCallback(() => setSearchOpen(true), []);
  const closeSearch = useCallback(() => {
    setSearchOpen(false);
    setSearchResult(null);
    if (activeSessionId) {
      viewsRef.current.get(activeSessionId)?.clearSearch();
      viewsRef.current.get(activeSessionId)?.focus();
    }
  }, [activeSessionId]);
  const openWorkspaceFile = useMemo(() => {
    if (!bridge?.workspaceId || !bridge.workspaceRoot) return undefined;
    const { workspaceId, workspaceRoot, workspaceName } = bridge;
    return (relativePath: string) => {
      useWorkbenchStore.getState().openFiles(workspaceId, workspaceRoot, workspaceName);
      useFilesStore.getState().open(workspaceId, relativePath, true);
    };
  }, [bridge]);

  const selectSession = (sessionId: string) => {
    setSearchOpen(false);
    useTerminalWorkbenchStore.getState().select(taskId, sessionId);
  };

  if (!everOpened) return null;

  const activeView = activeSessionId ? viewsRef.current.get(activeSessionId) : undefined;
  const blockedText = activeSession ? inputBlockedText(activeRuntime?.connection) : null;
  const transientNotice = activeRuntime?.notice ?? null;
  const showPlaceholder = tabs.length === 0;

  return (
    <div className={open ? "contents" : "hidden"} data-terminal-workbench={open ? "open" : "closed"}>
      <PanelDock side="bottom" size={panelHeight} minSize={MIN_TERMINAL_PANEL_HEIGHT} maxSize={maxPanelHeight}
        onSizeChange={resizePanel} resizeLabel="调整终端面板高度">
        <section
          ref={sectionRef}
          style={{ height: panelHeight }}
          className="bg-terminal-pane dock-panel flex w-full flex-col"
          aria-label="终端工作台"
        >
          <header className={cn(PANEL_HEADER_CLASS, "gap-1 pl-2")}>
            <TerminalTabBar
              tabs={tabs}
              activeSessionId={activeSessionId}
              shells={shellsQuery.data?.shells ?? null}
              shellsLoading={shellsQuery.isLoading}
              createDisabled={creating || sessions === null}
              onSelect={selectSession}
              onClose={(tab) => void closeTab(tab)}
              onRelease={(sessionId) => viewsRef.current.get(sessionId)?.releaseInput()}
              onCreate={(shell) => void createSession(shell)}
            />
            <div className="ml-auto flex min-w-0 items-center gap-1">
              {(notice ?? transientNotice) && (
                <span role="status" className={cn("truncate text-2xs", noticeClass((notice ?? transientNotice)!.tone))}>
                  {(notice ?? transientNotice)!.text}
                </span>
              )}
              {activeRuntime && (activeRuntime.connection === "connecting" || activeRuntime.connection === "reconnecting" || activeRuntime.connection === "failed") && (
                <span className="text-muted-foreground shrink-0 text-2xs" data-testid="terminal-connection-state">
                  {TERMINAL_CONNECTION_LABEL[activeRuntime.connection]}
                </span>
              )}
              <TerminalMoreMenu
                ended={ended}
                connection={activeRuntime?.connection ?? null}
                onOpenEnded={selectSession}
                onReconnect={activeView ? () => activeView.reconnect() : undefined}
              />
              <TooltipIconButton
                tooltip={expanded ? "还原高度" : "展开终端"}
                className="text-muted-foreground size-7"
                onClick={toggleExpanded}
                aria-label={expanded ? "还原终端面板高度" : "展开终端面板"}
              >
                {expanded ? <Minimize2Icon aria-hidden="true" /> : <Maximize2Icon aria-hidden="true" />}
              </TooltipIconButton>
              <TooltipIconButton
                tooltip="收起面板（会话保持运行）"
                className="text-muted-foreground size-7"
                onClick={() => useTerminalWorkbenchStore.getState().setOpen(taskId, false)}
                aria-label="关闭终端面板"
              >
                <XIcon aria-hidden="true" />
              </TooltipIconButton>
            </div>
          </header>
          {activeHandoff && (
            <div role="alert" className="bg-info/10 text-foreground mx-2 mb-1 flex items-center gap-2 rounded-md px-3 py-1.5 text-xs" data-testid="terminal-handoff-banner">
              <span className="min-w-0 flex-1 truncate">Agent 需要你输入：{activeHandoff.reason || "请在终端中完成输入"}</span>
              <Button
                type="button"
                size="sm"
                variant="secondary"
                className="h-6 shrink-0 px-2"
                onClick={() => activeView?.handoffDone()}
                disabled={!activeView || activeRuntime?.connection !== "connected"}
              >
                交还
              </Button>
            </div>
          )}
          {blockedText && (
            <div className="text-muted-foreground mx-2 mb-1 rounded-md bg-muted px-3 py-1 text-2xs" role="status" data-testid="terminal-input-blocked">
              {blockedText}
              {activeSession?.exitCode != null ? ` · 退出码 ${activeSession.exitCode}` : ""}
              {activeSession && !isActiveTerminalSession(activeSession) ? " · 画面受缓冲限制可能不完整" : ""}
            </div>
          )}
          <div className="relative min-h-0 flex-1">
            {mounted.map((sessionId) => (
              <TerminalSessionPane
                key={sessionId}
                taskId={taskId}
                sessionId={sessionId}
                visible={open && sessionId === activeSessionId}
                focusToken={focusToken}
                workspaceRoot={bridge?.workspaceRoot || undefined}
                onRuntime={handleRuntime}
                onSearchResults={handleSearchResults}
                onOpenSearch={openSearch}
                onOpenWorkspaceFile={openWorkspaceFile}
                registerView={registerView}
              />
            ))}
            {showPlaceholder && (
              <div className="text-muted-foreground flex h-full items-center justify-center text-xs">
                {creating || sessions === null ? "正在新建终端…" : "没有运行中的终端，点击「+」新建"}
              </div>
            )}
            {searchOpen && activeView && (
              <TerminalSearchBar
                result={searchResult}
                onSearch={(term, direction) => activeView.search(term, direction)}
                onClose={closeSearch}
              />
            )}
          </div>
        </section>
      </PanelDock>
    </div>
  );
}

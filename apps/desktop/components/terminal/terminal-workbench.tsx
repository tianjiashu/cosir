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
import { terminalNoticeClass, terminalPaneStatus } from "@/lib/terminal/terminal-pane-status";
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

import { TerminalEndedMenu } from "./terminal-menus";
import { TerminalSearchBar } from "./terminal-search-bar";
import { TerminalSessionPane } from "./terminal-session-pane";
import { TerminalTabBar } from "./terminal-tab-bar";
import type { TerminalView, TerminalViewRuntime } from "./terminal-view";
import { MIN_TERMINAL_PANEL_HEIGHT, useTerminalPanelHeight } from "./use-terminal-panel-height";
import { useTerminalSessionActions } from "./use-terminal-session-actions";

/**
 * Task 级底部终端工作台：会话标签栏（含「+」与选 shell 下拉）、已结束会话菜单、展开、关闭（只收起，
 * 不结束会话），以及页头短提示与页头下方的常驻状态栏（规则见 `terminal-pane-status`）。本组件只做装配：标签栏与菜单、新建 / 关闭、面板高度各在同目录
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
  // 搜索只属于打开它时可见的那个会话；切走、收起面板即关闭（见下方渲染期收敛）。
  const [searchSessionId, setSearchSessionId] = useState<string | null>(null);
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
  const { creating, notice, createSession, closeTab, takeOverInput, returnHandoff, returningHandoff } = useTerminalSessionActions(taskId, sessions, tabs);

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
  const openSearch = useCallback(() => {
    setSearchSessionId(useTerminalWorkbenchStore.getState().tasks[taskId]?.activeSessionId ?? null);
    setSearchResult(null);
  }, [taskId]);
  const closeSearch = useCallback(() => {
    setSearchSessionId(null);
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
    useTerminalWorkbenchStore.getState().select(taskId, sessionId);
  };

  // 切换会话的路径有多条（点标签、卡片跳转、交接、关闭后选相邻、snapshot 回落），不逐条关搜索，
  // 而在渲染期统一收敛：搜索所属会话不再可见就关闭。旧会话的高亮由窗格隐藏时清除。
  const searchOpen = open && searchSessionId !== null && searchSessionId === activeSessionId;
  if (searchSessionId !== null && !searchOpen) {
    setSearchSessionId(null);
    setSearchResult(null);
  }

  if (!everOpened) return null;

  const activeView = activeSessionId ? viewsRef.current.get(activeSessionId) : undefined;
  const paneStatus = terminalPaneStatus(activeSession, activeRuntime?.connection);
  // 面板命令（新建 / 关闭失败）优先于会话视图的短提示：前者是用户刚做的操作。
  const headerNotice = notice ?? activeRuntime?.notice ?? null;
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
              onTakeOver={(session) => void takeOverInput(session)}
              onCreate={(shell) => void createSession(shell)}
            />
            <div className="ml-auto flex min-w-0 items-center gap-1">
              {headerNotice && (
                <span role="status" className={cn("truncate text-2xs", terminalNoticeClass(headerNotice.tone))}>
                  {headerNotice.text}
                </span>
              )}
              <TerminalEndedMenu ended={ended} onOpenEnded={selectSession} />
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
                onClick={() => { if (activeSession) void returnHandoff(activeSession); }}
                disabled={returningHandoff}
              >
                交还
              </Button>
            </div>
          )}
          {paneStatus && (
            <div
              role="status"
              className={cn("mx-2 mb-1 flex items-center gap-2 rounded-md bg-muted px-3 py-1 text-2xs", terminalNoticeClass(paneStatus.tone))}
              data-testid="terminal-status"
            >
              <span className="min-w-0 flex-1 truncate">{paneStatus.text}</span>
              {paneStatus.action === "reconnect" && activeView && (
                <Button type="button" size="sm" variant="secondary" className="h-5 shrink-0 px-2 text-2xs" onClick={() => activeView.reconnect()}>
                  重新连接
                </Button>
              )}
              {paneStatus.action === "create" && (
                <Button type="button" size="sm" variant="secondary" className="h-5 shrink-0 px-2 text-2xs" disabled={creating} onClick={() => void createSession("auto")}>
                  新建终端
                </Button>
              )}
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

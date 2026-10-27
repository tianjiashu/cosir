import { Maximize2Icon, Minimize2Icon, TerminalIcon, XIcon } from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState } from "react";

import { Badge, type BadgeTone } from "@/components/ui/badge";
import { TooltipIconButton } from "@/components/tooltip-icon-button";
import { PanelDock } from "@/components/ui/panel-dock";
import { PANEL_HEADER_CLASS } from "@/components/ui/panel-tokens";
import { frontendLog } from "@/lib/logging/frontend-log";
import { cn } from "@/lib/utils";
import {
  TerminalStreamConnection,
  type TerminalStreamEvent,
} from "./terminal-stream-connection";
import { TerminalStreamSession } from "./terminal-stream-session";
import { useTerminalPanelStore } from "./terminal-panel-store";
import { terminalSessionStatusPresentation } from "./terminal-session-status";

type ConnectionState = "connecting" | "connected" | "closed" | "error";

/** WebSocket 连接状态的展示映射；会话自身生命周期见 terminal-session-status。 */
const CONNECTION_STATE_PRESENTATION: Record<ConnectionState, { label: string; tone: BadgeTone; dot: string }> = {
  connecting: { label: "连接中", tone: "warning", dot: "bg-warning" },
  connected: { label: "已连接", tone: "success", dot: "bg-success" },
  closed: { label: "已断开", tone: "neutral", dot: "bg-muted-foreground" },
  error: { label: "连接异常", tone: "destructive", dot: "bg-destructive" },
};

/** 流内的临时提示（重新同步、协议错误），与会话生命周期状态分开呈现。 */
type StreamNotice = { text: string; tone: "muted" | "destructive" };

const MIN_PANEL_HEIGHT = 192;
const DEFAULT_PANEL_HEIGHT = 320;

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function TerminalPanel({ taskId, sessionId, onClose }: { taskId: number; sessionId: string; onClose: () => void }) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const sessionRef = useRef<TerminalStreamSession | null>(null);
  const restoreHeightRef = useRef(DEFAULT_PANEL_HEIGHT);
  const [connectionState, setConnectionState] = useState<ConnectionState>("connecting");
  // attached 之前后端尚未告知会话状态，null 表示「未知」而不是某个已知状态。
  const [sessionStatus, setSessionStatus] = useState<string | null>(null);
  const [notice, setNotice] = useState<StreamNotice | null>(null);
  const [exitCode, setExitCode] = useState<number | null | undefined>();
  const [panelHeight, setPanelHeight] = useState(DEFAULT_PANEL_HEIGHT);
  const [maxPanelHeight, setMaxPanelHeight] = useState(720);
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    const updateMaxPanelHeight = () => {
      const nextMaxHeight = Math.max(MIN_PANEL_HEIGHT, Math.round(window.innerHeight * 0.8));
      setMaxPanelHeight(nextMaxHeight);
      setPanelHeight((current) => Math.min(current, nextMaxHeight));
    };
    updateMaxPanelHeight();
    window.addEventListener("resize", updateMaxPanelHeight);
    return () => window.removeEventListener("resize", updateMaxPanelHeight);
  }, []);

  useLayoutEffect(() => {
    if (!containerRef.current) return;
    const session = new TerminalStreamSession(containerRef.current);
    sessionRef.current = session;
    return () => {
      sessionRef.current = null;
      session.dispose();
    };
  }, [sessionId, taskId]);

  useEffect(() => {
    setConnectionState("connecting");
    setSessionStatus(null);
    setNotice(null);
    setExitCode(undefined);
    const connection = new TerminalStreamConnection({
      taskId,
      sessionId,
      onState: setConnectionState,
      onEvent: (event: TerminalStreamEvent) => {
        if (event.type === "output") {
          sessionRef.current?.write(TerminalStreamConnection.decodeOutput(event));
        } else if (event.type === "resync_required") {
          sessionRef.current?.clear();
          setNotice({ text: "正在重新同步输出", tone: "muted" });
        } else if (event.type === "attached" || event.type === "status") {
          if (event.type === "attached") sessionRef.current?.setGeometry(event.rows, event.columns);
          if (event.status) setSessionStatus(event.status);
          setNotice(null);
        } else if (event.type === "exit") {
          setSessionStatus(event.status ?? "exited");
          setExitCode(event.exit_code);
        } else if (event.type === "protocol_error") {
          setNotice({ text: text(event.message) || "协议错误", tone: "destructive" });
        }
      },
    });
    connection.start();
    return () => connection.dispose();
  }, [sessionId, taskId]);

  useEffect(() => {
    if (sessionStatus === null || terminalSessionStatusPresentation(sessionStatus)) return;
    void frontendLog("WARNING", "terminal_panel_unknown_status", "终端会话返回未登记的状态值", {
      data: { taskId, sessionId, status: sessionStatus },
    });
  }, [sessionId, sessionStatus, taskId]);

  const connection = CONNECTION_STATE_PRESENTATION[connectionState];
  const session = sessionStatus === null
    ? null
    : terminalSessionStatusPresentation(sessionStatus) ?? { label: "未知状态", tone: "neutral" as const };
  const clampPanelHeight = (height: number) => Math.min(
    Math.max(height, MIN_PANEL_HEIGHT),
    maxPanelHeight,
  );
  const toggleExpanded = () => {
    if (expanded) {
      setPanelHeight(clampPanelHeight(restoreHeightRef.current));
      setExpanded(false);
      return;
    }
    restoreHeightRef.current = panelHeight;
    setPanelHeight(clampPanelHeight(Math.round(window.innerHeight * 0.72)));
    setExpanded(true);
  };
  return (
    <PanelDock side="bottom" size={panelHeight} minSize={MIN_PANEL_HEIGHT} maxSize={maxPanelHeight}
      onSizeChange={(height) => { setExpanded(false); setPanelHeight(height); }} resizeLabel="调整终端面板高度">
    <section
      style={{ height: panelHeight }}
      className="terminal-surface bg-terminal-inset dock-panel flex w-full flex-col"
      aria-label="终端会话面板"
    >
      <header className={cn(PANEL_HEADER_CLASS, "gap-2 pl-3")}>
        <TerminalIcon className="text-muted-foreground size-4" aria-hidden="true" />
        <span className="text-foreground text-sm font-medium">终端会话</span>
        <div className="ml-auto flex min-w-0 items-center gap-2">
          {notice ? (
            <span className={cn("truncate text-2xs", notice.tone === "destructive" ? "text-destructive" : "text-muted-foreground")}>
              {notice.text}
            </span>
          ) : null}
          <Badge variant="outline" tone={connection.tone} size="sm" className="gap-1.5" data-testid="terminal-panel-connection">
            <span className={cn("size-1.5 rounded-full", connection.dot)} aria-hidden="true" />
            {connection.label}
          </Badge>
          {session ? (
            <Badge variant="soft" tone={session.tone} size="sm" data-testid="terminal-panel-session-status">
              {session.label}
            </Badge>
          ) : null}
          {exitCode !== undefined ? (
            <Badge variant="tint" shape="tag" size="sm" className="font-mono">
              退出码 {exitCode ?? "?"}
            </Badge>
          ) : null}
        </div>
        <TooltipIconButton
          tooltip={expanded ? "还原高度" : "展开终端"}
          className="text-muted-foreground size-7"
          onClick={toggleExpanded}
          aria-label={expanded ? "还原终端面板高度" : "展开终端面板"}
        >
          {expanded ? <Minimize2Icon aria-hidden="true" /> : <Maximize2Icon aria-hidden="true" />}
        </TooltipIconButton>
        <TooltipIconButton tooltip="关闭面板" className="text-muted-foreground size-7" onClick={onClose} aria-label="关闭终端面板">
          <XIcon aria-hidden="true" />
        </TooltipIconButton>
      </header>
      <div ref={containerRef} className="min-h-0 flex-1 overflow-auto px-3 py-2" data-terminal-stream="true" />
    </section>
    </PanelDock>
  );
}

/** Task 级嵌入式终端面板；不会创建本地终端窗口。 */
export function TerminalPanelHost({ taskId }: { taskId: number }) {
  const target = useTerminalPanelStore((state) => state.target);
  const open = useTerminalPanelStore((state) => state.open);
  const close = useTerminalPanelStore((state) => state.close);
  if (!open || !target || target.taskId !== taskId) return null;
  return <TerminalPanel key={`${target.taskId}:${target.sessionId}`} taskId={target.taskId} sessionId={target.sessionId} onClose={close} />;
}

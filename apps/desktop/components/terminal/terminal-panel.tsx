import { Maximize2Icon, Minimize2Icon, TerminalIcon, XIcon } from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState } from "react";

import { cn } from "@/lib/utils";
import {
  TerminalStreamConnection,
  type TerminalStreamEvent,
} from "./terminal-stream-connection";
import { TerminalStreamSession } from "./terminal-stream-session";
import { useTerminalPanelStore } from "./terminal-panel-store";

const MIN_PANEL_HEIGHT = 192;
const DEFAULT_PANEL_HEIGHT = 320;

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function TerminalPanel({ taskId, sessionId, onClose }: { taskId: number; sessionId: string; onClose: () => void }) {
  const panelRef = useRef<HTMLElement | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const sessionRef = useRef<TerminalStreamSession | null>(null);
  const resizeStartRef = useRef<{ pointerId: number; y: number; height: number } | null>(null);
  const restoreHeightRef = useRef(DEFAULT_PANEL_HEIGHT);
  const [connectionState, setConnectionState] = useState<"connecting" | "connected" | "closed" | "error">("connecting");
  const [status, setStatus] = useState("connecting");
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
    setStatus("connecting");
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
          setStatus("重新同步输出");
        } else if (event.type === "attached" || event.type === "status") {
          if (event.status) setStatus(event.status);
        } else if (event.type === "exit") {
          setStatus(event.status ?? "exited");
          setExitCode(event.exit_code);
        } else if (event.type === "protocol_error") {
          setStatus(text(event.message) || "协议错误");
        }
      },
    });
    connection.start();
    return () => connection.dispose();
  }, [sessionId, taskId]);

  const connectionLabel = connectionState === "connected" ? "已连接" : connectionState === "error" ? "连接异常" : connectionState === "closed" ? "已断开" : "连接中";
  const connectionTone = connectionState === "connected"
    ? "bg-emerald-400"
    : connectionState === "error"
      ? "bg-rose-400"
      : connectionState === "connecting"
        ? "bg-amber-400"
        : "bg-zinc-500";
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
  const resizeFromKeyboard = (key: string) => {
    if (key !== "ArrowUp" && key !== "ArrowDown" && key !== "Home" && key !== "End") return false;
    setExpanded(false);
    setPanelHeight((current) => clampPanelHeight(
      key === "Home" ? MIN_PANEL_HEIGHT : key === "End" ? maxPanelHeight : current + (key === "ArrowUp" ? 24 : -24),
    ));
    return true;
  };
  return (
    <section
      ref={panelRef}
      style={{ height: panelHeight }}
      className="flex max-h-[80vh] min-h-48 w-full shrink-0 flex-col border-t border-border/70 bg-zinc-950 text-zinc-100"
      aria-label="终端会话面板"
    >
      <div
        role="separator"
        aria-label="调整终端面板高度"
        aria-orientation="horizontal"
        aria-valuemin={MIN_PANEL_HEIGHT}
        aria-valuenow={panelHeight}
        aria-valuemax={maxPanelHeight}
        tabIndex={0}
        className="group flex h-2 shrink-0 cursor-row-resize touch-none items-center justify-center outline-none focus-visible:bg-white/5"
        onPointerDown={(event) => {
          event.preventDefault();
          event.currentTarget.setPointerCapture(event.pointerId);
          resizeStartRef.current = {
            pointerId: event.pointerId,
            y: event.clientY,
            height: panelRef.current?.getBoundingClientRect().height ?? panelHeight,
          };
        }}
        onPointerMove={(event) => {
          const start = resizeStartRef.current;
          if (!start || start.pointerId !== event.pointerId) return;
          setExpanded(false);
          setPanelHeight(clampPanelHeight(start.height + start.y - event.clientY));
        }}
        onPointerUp={(event) => {
          if (resizeStartRef.current?.pointerId === event.pointerId) resizeStartRef.current = null;
        }}
        onPointerCancel={() => { resizeStartRef.current = null; }}
        onKeyDown={(event) => {
          if (resizeFromKeyboard(event.key)) event.preventDefault();
        }}
      >
        <span className="h-0.5 w-10 rounded-full bg-zinc-700 transition-colors group-hover:bg-zinc-500 group-focus-visible:bg-zinc-400" />
      </div>
      <header className="flex h-11 shrink-0 items-center gap-2 border-b border-white/10 px-3">
        <TerminalIcon className="size-4 text-zinc-400" aria-hidden="true" />
        <span className="text-sm font-medium text-zinc-200">终端会话</span>
        <div className="ml-auto flex min-w-0 items-center gap-2">
          <span className="inline-flex shrink-0 items-center gap-1.5 rounded-full border border-white/10 px-2 py-1 text-[11px] text-zinc-300">
            <span className={cn("size-1.5 rounded-full", connectionTone)} aria-hidden="true" />
            {connectionLabel}
          </span>
          {(status !== "running" && status !== "starting") || exitCode !== undefined ? (
            <span className="truncate font-mono text-[11px] text-zinc-500">
              {status !== "running" && status !== "starting" ? status : ""}
              {exitCode !== undefined ? ` · 退出码 ${exitCode ?? "?"}` : ""}
            </span>
          ) : null}
        </div>
        <button
          type="button"
          onClick={toggleExpanded}
          className="rounded p-1 text-zinc-400 hover:bg-white/10 hover:text-zinc-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/30"
          aria-label={expanded ? "还原终端面板高度" : "展开终端面板"}
          title={expanded ? "还原高度" : "展开终端"}
        >
          {expanded ? <Minimize2Icon className="size-4" aria-hidden="true" /> : <Maximize2Icon className="size-4" aria-hidden="true" />}
        </button>
        <button type="button" onClick={onClose} className="rounded p-1 text-zinc-400 hover:bg-white/10 hover:text-zinc-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/30" aria-label="关闭终端面板" title="关闭面板">
          <XIcon className="size-4" aria-hidden="true" />
        </button>
      </header>
      <div ref={containerRef} className="min-h-0 flex-1 px-3 py-2" data-terminal-stream="true" />
    </section>
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

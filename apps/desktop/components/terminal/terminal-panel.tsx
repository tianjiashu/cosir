import { TerminalIcon, XIcon } from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState } from "react";

import { cn } from "@/lib/utils";
import {
  TerminalStreamConnection,
  type TerminalStreamEvent,
} from "./terminal-stream-connection";
import { TerminalStreamSession } from "./terminal-stream-session";
import { useTerminalPanelStore } from "./terminal-panel-store";

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function TerminalPanel({ taskId, sessionId, onClose }: { taskId: number; sessionId: string; onClose: () => void }) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const sessionRef = useRef<TerminalStreamSession | null>(null);
  const [connectionState, setConnectionState] = useState<"connecting" | "connected" | "closed" | "error">("connecting");
  const [status, setStatus] = useState("connecting");
  const [exitCode, setExitCode] = useState<number | null | undefined>();

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
  return (
    <section className="flex h-80 max-h-[40vh] min-h-48 w-full shrink-0 flex-col border-t border-border/70 bg-zinc-950 text-zinc-100" aria-label="只读终端面板">
      <header className="flex h-11 shrink-0 items-center gap-2 border-b border-white/10 px-3">
        <TerminalIcon className="size-4 text-zinc-400" aria-hidden="true" />
        <span className="font-mono text-xs text-zinc-300">terminal session</span>
        <span className="truncate font-mono text-[11px] text-zinc-500">{sessionId}</span>
        <span className={cn("ml-auto text-xs", connectionState === "connected" ? "text-emerald-400" : "text-zinc-500")}>
          {connectionLabel}{status !== "running" && status !== "starting" ? ` · ${status}` : ""}{exitCode !== undefined ? ` · exit ${exitCode ?? "?"}` : ""}
        </span>
        <button type="button" onClick={onClose} className="rounded p-1 text-zinc-400 hover:bg-white/10 hover:text-zinc-100" aria-label="关闭终端面板" title="关闭面板">
          <XIcon className="size-4" aria-hidden="true" />
        </button>
      </header>
      <div ref={containerRef} className="min-h-0 flex-1 px-3 py-2" data-terminal-stream="true" />
      <footer className="shrink-0 border-t border-white/5 px-3 py-1 text-[11px] text-zinc-500">只读预览 · Agent 负责输入与控制</footer>
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

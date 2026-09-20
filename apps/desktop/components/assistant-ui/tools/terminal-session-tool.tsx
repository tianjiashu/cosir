import {
  ArrowDownIcon,
  ArrowUpIcon,
  CircleAlertIcon,
  SquareTerminalIcon,
  TerminalIcon,
  XIcon,
} from "lucide-react";
import type { ReactNode } from "react";
import type { ToolCallMessagePartProps } from "@assistant-ui/react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { useTerminalPanelStore } from "@/components/terminal/terminal-panel-store";
import { asRecord, readToolArtifact } from "./types";
import { ToolStatus } from "./tool-status";
import { DisclosureRowStatic } from "../elements/disclosure-row.aui";

type TerminalSessionVariant = "start" | "read" | "write" | "signal" | "close" | "unknown";

type TerminalSessionToolProps = ToolCallMessagePartProps & { taskId?: number };

function text(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined;
}

function sessionStatusLabel(value: unknown): string | null {
  switch (value) {
    case "starting": return "启动中";
    case "running": return "运行中";
    case "exited": return "已退出";
    case "interrupted": return "已中断";
    case "failed": return "会话失败";
    case "closed": return "已关闭";
    default: return null;
  }
}

function sessionStatusClass(value: unknown): string {
  if (value === "running" || value === "starting") return "text-emerald-300";
  if (value === "failed") return "text-red-300";
  if (value === "interrupted") return "text-amber-300";
  return "text-zinc-400";
}

function sessionVariant(value: unknown): TerminalSessionVariant {
  switch (value) {
    case "terminal-session-start": return "start";
    case "terminal-session-read": return "read";
    case "terminal-session-write": return "write";
    case "terminal-session-signal": return "signal";
    case "terminal-session-close": return "close";
    default: return "unknown";
  }
}

/** Resolve the declared semantic variant; unknown values use the safe fallback row. */
function resolveVariant(presentation: Record<string, unknown>): TerminalSessionVariant {
  const declared = sessionVariant(presentation.variant);
  return declared;
}

function shellLabel(data: Record<string, unknown>): string {
  const shell = text(data.shell_kind) ?? text(data.shell);
  return shell ?? "默认 Shell";
}

function sessionIdLabel(data: Record<string, unknown>): string {
  const sessionId = text(data.session_id);
  if (!sessionId) return "终端会话";
  return sessionId.length > 20 ? `${sessionId.slice(0, 17)}…` : sessionId;
}

function actionLabel(variant: Exclude<TerminalSessionVariant, "start" | "unknown">, data: Record<string, unknown>): string {
  if (variant === "read") return "读取终端输出";
  if (variant === "write") return data.submitted === true ? "提交终端输入" : "写入终端输入";
  if (variant === "close") return "关闭终端会话";
  switch (data.signal) {
    case "interrupt": return "发送中断信号";
    case "eof": return "发送 EOF 信号";
    case "suspend": return "挂起终端进程";
    default: return "控制终端会话";
  }
}

function actionMeta(variant: Exclude<TerminalSessionVariant, "start" | "unknown">, data: Record<string, unknown>): string {
  if (variant === "read") {
    const nextSeq = typeof data.next_seq === "number" ? `next seq ${data.next_seq}` : "读取输出";
    return nextSeq;
  }
  if (variant === "write") {
    return data.submitted === true ? "已提交" : "已写入";
  }
  if (variant === "close") {
    const status = sessionStatusLabel(data.status);
    const exitCode = typeof data.exit_code === "number" ? `exit ${data.exit_code}` : null;
    return [status ?? "会话已关闭", exitCode].filter(Boolean).join(" · ");
  }
  return sessionStatusLabel(data.status) ?? "已发送";
}

function actionIcon(variant: Exclude<TerminalSessionVariant, "start" | "unknown">): ReactNode {
  if (variant === "read") return <ArrowDownIcon className="size-4 text-sky-400" aria-hidden="true" />;
  if (variant === "write") return <ArrowUpIcon className="size-4 text-violet-400" aria-hidden="true" />;
  if (variant === "signal") return <CircleAlertIcon className="size-4 text-amber-400" aria-hidden="true" />;
  return <XIcon className="size-4 text-zinc-400" aria-hidden="true" />;
}

function TerminalSessionStartCard({ artifact, taskId }: { artifact: ReturnType<typeof readToolArtifact>; taskId?: number }) {
  const data = asRecord(artifact.display_data);
  const sessionId = text(data.session_id);
  const openSession = useTerminalPanelStore((state) => state.openSession);
  const canOpen = taskId !== undefined && sessionId !== undefined;
  const status = sessionStatusLabel(data.status);

  return (
    <div className="my-1 flex min-w-0 flex-col gap-2 rounded-xl border border-sky-400/20 bg-zinc-900 px-3 py-2 text-zinc-100 shadow-sm">
      <div className="flex min-w-0 items-center gap-2">
        <TerminalIcon className="size-4 shrink-0 text-sky-300" aria-hidden="true" />
        <span className="font-medium">终端会话</span>
        <span className="rounded border border-sky-300/20 bg-sky-300/10 px-1.5 py-px font-mono text-[10px] uppercase tracking-wide text-sky-200">
          session
        </span>
        <ToolStatus status={artifact.backendStatus} className="ml-auto shrink-0 text-xs text-zinc-400" />
      </div>
      <div className="flex min-w-0 items-center gap-2 pl-6 text-xs text-zinc-400">
        <code className="shrink-0 text-zinc-300">{shellLabel(data)}</code>
        <span aria-hidden="true">·</span>
        <code className="min-w-0 truncate" title={text(data.initial_cwd) ?? undefined}>{text(data.initial_cwd) ?? "工作目录未知"}</code>
        {status && <span className={cn("shrink-0", sessionStatusClass(data.status))}>· {status}</span>}
      </div>
      <div className="flex items-center justify-between gap-2 pl-6">
        <code className="truncate text-[11px] text-zinc-500" title={sessionId}>{sessionIdLabel(data)}</code>
        <Button
          type="button"
          variant="secondary"
          size="sm"
          disabled={!canOpen}
          className="h-7 shrink-0 bg-sky-400/15 px-2.5 text-sky-100 hover:bg-sky-400/25"
          onClick={() => { if (canOpen) openSession({ taskId, sessionId }); }}
        >
          打开终端
        </Button>
      </div>
      {artifact.error && <div className="pl-6 text-xs text-red-300">{artifact.error}</div>}
      {!sessionId && <span className="pl-6 text-xs text-zinc-500">等待 session</span>}
    </div>
  );
}

function TerminalSessionActionRow({
  artifact,
  variant,
}: {
  artifact: ReturnType<typeof readToolArtifact>;
  variant: Exclude<TerminalSessionVariant, "start" | "unknown">;
}) {
  const data = asRecord(artifact.display_data);
  const title = actionLabel(variant, data);
  const meta = artifact.error ?? `${sessionIdLabel(data)} · ${actionMeta(variant, data)}`;
  return (
    <DisclosureRowStatic
      role="status"
      leading={actionIcon(variant)}
      label={<span className={cn("text-sm font-medium", artifact.backendStatus === "failed" && "text-destructive")}>{title}</span>}
      meta={<span className="text-muted-foreground max-w-[55%] truncate">{meta}</span>}
      trailing={<ToolStatus status={artifact.backendStatus} />}
      className="my-1"
    />
  );
}

/** Render one terminal-session call as either the session anchor or an action trace. */
export function TerminalSessionTool({ taskId, artifact: rawArtifact }: TerminalSessionToolProps) {
  const artifact = readToolArtifact(rawArtifact);
  const data = asRecord(artifact.display_data);
  const variant = resolveVariant(asRecord(artifact.presentation));

  if (variant === "start") return <TerminalSessionStartCard artifact={artifact} taskId={taskId} />;
  if (variant !== "unknown") return <TerminalSessionActionRow artifact={artifact} variant={variant} />;

  return (
    <DisclosureRowStatic
      role="status"
      leading={<SquareTerminalIcon className="size-4 text-zinc-400" aria-hidden="true" />}
      label={<span className="text-sm font-medium">终端会话操作</span>}
      meta={<span className="text-muted-foreground max-w-[55%] truncate">{artifact.error ?? sessionIdLabel(data)}</span>}
      trailing={<ToolStatus status={artifact.backendStatus} />}
      className={artifact.backendStatus === "failed" ? "text-destructive" : undefined}
    />
  );
}

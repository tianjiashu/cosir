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

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { useTerminalPanelStore } from "@/components/terminal/terminal-panel-store";
import { asRecord, readToolArtifact } from "./types";
import { ToolStatus } from "./tool-status";
import { DisclosureRowStatic } from "../elements/disclosure-row.aui";
import { terminalSessionStatusPresentation } from "@/components/terminal/terminal-session-status";
import type { BadgeTone } from "@/components/ui/badge";

type TerminalSessionVariant = "start" | "list" | "read" | "write" | "signal" | "close" | "unknown";

type TerminalSessionToolProps = ToolCallMessagePartProps & { taskId?: number };

function text(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined;
}

/** 会话状态的文案与色调统一取自 terminal-session-status（终端面板共用同一张表）。 */
function sessionStatusPresentation(value: unknown) {
  return typeof value === "string" ? terminalSessionStatusPresentation(value) : null;
}

function sessionStatusLabel(value: unknown): string | null {
  return sessionStatusPresentation(value)?.label ?? null;
}

const TONE_TEXT_CLASS: Record<BadgeTone, string> = {
  neutral: "text-muted-foreground",
  primary: "text-primary",
  info: "text-info",
  success: "text-success",
  warning: "text-warning",
  destructive: "text-destructive",
  skill: "text-(--skill-foreground)",
};

function sessionStatusClass(value: unknown): string {
  const presentation = sessionStatusPresentation(value);
  return presentation ? TONE_TEXT_CLASS[presentation.tone] : "text-muted-foreground";
}

function sessionVariant(value: unknown): TerminalSessionVariant {
  switch (value) {
    case "terminal-session-start": return "start";
    case "terminal-session-list": return "list";
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

function actionLabel(variant: Exclude<TerminalSessionVariant, "start" | "unknown">, data: Record<string, unknown>): string {
  if (variant === "list") return "列出终端会话";
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
  if (variant === "list") return typeof data.session_count === "number" ? `${data.session_count} 个会话` : "会话列表";
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
  if (variant === "list") return <TerminalIcon className="text-info size-4" aria-hidden="true" />;
  if (variant === "read") return <ArrowDownIcon className="text-info size-4" aria-hidden="true" />;
  if (variant === "write") return <ArrowUpIcon className="text-info size-4" aria-hidden="true" />;
  if (variant === "signal") return <CircleAlertIcon className="text-warning size-4" aria-hidden="true" />;
  return <XIcon className="text-muted-foreground size-4" aria-hidden="true" />;
}

function TerminalSessionStartCard({ artifact, taskId }: { artifact: ReturnType<typeof readToolArtifact>; taskId?: number }) {
  const data = asRecord(artifact.display_data);
  const sessionId = text(data.session_id);
  const openSession = useTerminalPanelStore((state) => state.openSession);
  const canOpen = taskId !== undefined && sessionId !== undefined;
  const status = sessionStatusLabel(data.status);

  return (
    <div className="terminal-surface border-info/30 flex min-w-0 flex-col gap-2 rounded-xl border px-3 py-2 shadow-sm">
      <div className="flex min-w-0 items-center gap-2">
        <TerminalIcon className="text-info size-4 shrink-0" aria-hidden="true" />
        <span className="font-medium">{text(data.label) ?? "终端会话"}</span>
        <Badge tone="info" variant="outline" shape="tag" size="sm" className="bg-info/10 font-mono tracking-wide uppercase">
          session
        </Badge>
        <ToolStatus status={artifact.backendStatus} className="ml-auto shrink-0" />
      </div>
      <div className="text-muted-foreground flex min-w-0 items-center gap-2 pl-6 text-xs">
        <code className="text-foreground/80 shrink-0">{shellLabel(data)}</code>
        <span aria-hidden="true">·</span>
        <code className="min-w-0 truncate" title={text(data.initial_cwd) ?? undefined}>{text(data.initial_cwd) ?? "工作目录未知"}</code>
        {status && <span className={cn("shrink-0", sessionStatusClass(data.status))}>· {status}</span>}
      </div>
      <div className="flex items-center justify-between gap-2 pl-6">
        <Button
          type="button"
          variant="secondary"
          size="sm"
          disabled={!canOpen}
          className="bg-info/10 text-info hover:bg-info/20 h-7 shrink-0 px-2.5"
          onClick={() => { if (canOpen) openSession({ taskId, sessionId }); }}
        >
          打开终端
        </Button>
      </div>
      {artifact.error && <div className="text-destructive pl-6 text-xs">{artifact.error}</div>}
      {!sessionId && <span className="text-muted-foreground pl-6 text-xs">等待 session</span>}
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
  const meta = artifact.error ?? actionMeta(variant, data);
  return (
    <DisclosureRowStatic
      role="status"
      leading={actionIcon(variant)}
      label={<span className={cn("text-sm font-medium", artifact.backendStatus === "failed" && "text-destructive")}>{title}</span>}
      meta={<span className="text-muted-foreground max-w-[55%] truncate">{meta}</span>}
      trailing={<ToolStatus status={artifact.backendStatus} />}
    />
  );
}

/** Render one terminal-session call as either the session anchor or an action trace. */
export function TerminalSessionTool({ taskId, artifact: rawArtifact }: TerminalSessionToolProps) {
  const artifact = readToolArtifact(rawArtifact);
  const variant = resolveVariant(asRecord(artifact.presentation));

  if (variant === "start") return <TerminalSessionStartCard artifact={artifact} taskId={taskId} />;
  if (variant !== "unknown") return <TerminalSessionActionRow artifact={artifact} variant={variant} />;

  return (
    <DisclosureRowStatic
      role="status"
      leading={<SquareTerminalIcon className="text-muted-foreground size-4" aria-hidden="true" />}
      label={<span className="text-sm font-medium">终端会话操作</span>}
      meta={<span className="text-muted-foreground max-w-[55%] truncate">{artifact.error ?? "终端会话操作"}</span>}
      trailing={<ToolStatus status={artifact.backendStatus} />}
      className={artifact.backendStatus === "failed" ? "text-destructive" : undefined}
    />
  );
}

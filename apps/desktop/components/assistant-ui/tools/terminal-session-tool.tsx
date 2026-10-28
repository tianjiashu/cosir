import {
  ArrowDownIcon,
  ArrowUpIcon,
  SquareTerminalIcon,
  TerminalIcon,
  XIcon,
} from "lucide-react";
import type { KeyboardEvent, ReactNode } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { TerminalSessionEntry } from "@/lib/assistant/contract";
import { terminalStatusPresentation } from "@/lib/terminal/terminal-sessions";
import { useTerminalWorkbenchStore } from "@/lib/terminal/terminal-workbench-store";
import { cn } from "@/lib/utils";
import { asRecord, readToolArtifact } from "./types";
import { ToolStatus } from "./tool-status";
import type { ToolPartRendererProps } from "./tool-renderer-registry";
import { DisclosureRowStatic } from "../elements/disclosure-row.aui";

type TerminalSessionVariant = "start" | "list" | "read" | "write" | "close" | "unknown";
type ActionVariant = Exclude<TerminalSessionVariant, "start" | "unknown">;

function text(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined;
}

function sessionVariant(value: unknown): TerminalSessionVariant {
  switch (value) {
    case "terminal-session-start": return "start";
    case "terminal-session-list": return "list";
    case "terminal-session-read": return "read";
    case "terminal-session-write": return "write";
    case "terminal-session-close": return "close";
    default: return "unknown";
  }
}

/**
 * 条件等待的结束原因（后端 `wait_result.end_reason`）→ 中文；不据此宣告命令成功。
 * `idle` 为未带等待条件，`background` 为超出工具线程时长、转为挂起等待（完成后通知 Agent）。
 */
const WAIT_END_REASON_LABELS: Record<string, string> = {
  command_returned: "命令已返回",
  matched: "匹配到输出",
  output_quiet: "输出已静默",
  elapsed: "等待结束",
  timeout: "等待超时",
  cancelled: "等待已取消",
  session_ended: "会话已结束",
  background: "转为后台等待",
};

function waitResultLabel(value: unknown): string | null {
  const reason = text(asRecord(value).end_reason);
  if (!reason || reason === "idle") return null;
  return WAIT_END_REASON_LABELS[reason] ?? reason;
}

/** `lines` 为本次返回的行号区间 `[起始行, 结束行]`；无新增行时为 null。 */
function lineRange(value: unknown): string | null {
  if (!Array.isArray(value) || value.length !== 2) return null;
  const [start, end] = value;
  if (!Number.isInteger(start) || !Number.isInteger(end)) return null;
  return start === end ? `第 ${start} 行` : `第 ${start}–${end} 行`;
}

function actionLabel(variant: ActionVariant): string {
  switch (variant) {
    case "list": return "列出终端会话";
    case "read": return "读取终端";
    case "write": return "终端输入";
    case "close": return "关闭终端会话";
  }
}

/**
 * 动作行摘要：write 显示实际输入内容（单行文本或按键序列）与等待结果，read 显示行范围与等待结果。
 * 都来自后端受控 `display_data`，不从 args 推导。
 */
function actionMeta(variant: ActionVariant, data: Record<string, unknown>): string {
  if (variant === "list") return typeof data.session_count === "number" ? `${data.session_count} 个会话` : "会话列表";
  if (variant === "close") {
    const status = data.status === "closed" || data.status === "exited" || data.status === "failed"
      ? terminalStatusPresentation(data.status).label
      : "会话已关闭";
    return [status, typeof data.exit_code === "number" ? `exit ${data.exit_code}` : null].filter(Boolean).join(" · ");
  }
  const lead = variant === "write" ? text(data.input_summary) ?? null : lineRange(data.lines);
  return [lead, waitResultLabel(data.wait_result)].filter(Boolean).join(" · ") || (variant === "write" ? "已输入" : "读取输出");
}

function actionIcon(variant: ActionVariant): ReactNode {
  if (variant === "list") return <TerminalIcon className="text-info size-4" aria-hidden="true" />;
  if (variant === "read") return <ArrowDownIcon className="text-info size-4" aria-hidden="true" />;
  if (variant === "write") return <ArrowUpIcon className="text-info size-4" aria-hidden="true" />;
  return <XIcon className="text-muted-foreground size-4" aria-hidden="true" />;
}

/** 卡片上的会话状态读 snapshot 的实时会话（Task 级终端列表），而不是工具执行那一刻的快照。 */
function useLiveSession(taskId: number | undefined, sessionId: string | undefined): TerminalSessionEntry | null {
  return useTerminalWorkbenchStore((state) => (
    taskId === undefined || sessionId === undefined
      ? null
      : state.tasks[taskId]?.sessions?.find((session) => session.sessionId === sessionId) ?? null
  ));
}

function TerminalSessionStartCard({ artifact, taskId, interactive }: {
  artifact: ReturnType<typeof readToolArtifact>;
  taskId?: number;
  interactive: boolean;
}) {
  const data = asRecord(artifact.display_data);
  const sessionId = text(data.session_id);
  const live = useLiveSession(taskId, sessionId);
  const status = live ? terminalStatusPresentation(live.status) : null;
  const canReveal = interactive && taskId !== undefined && sessionId !== undefined && live !== null;
  const reveal = canReveal ? () => useTerminalWorkbenchStore.getState().reveal(taskId, sessionId) : undefined;

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
        <code className="text-foreground/80 shrink-0">{text(data.shell_kind) ?? "默认 Shell"}</code>
        <span aria-hidden="true">·</span>
        <code className="min-w-0 truncate" title={text(data.initial_cwd) ?? undefined}>{text(data.initial_cwd) ?? "工作目录未知"}</code>
        {sessionId && (
          <span className={cn("shrink-0", status?.text ?? "text-muted-foreground")}>· {status?.label ?? "已失效"}</span>
        )}
      </div>
      {interactive && sessionId && (
        <div className="flex items-center justify-between gap-2 pl-6">
          <Button
            type="button"
            variant="secondary"
            size="sm"
            disabled={reveal === undefined}
            className="bg-info/10 text-info hover:bg-info/20 h-7 shrink-0 px-2.5"
            onClick={reveal}
          >
            在终端中查看
          </Button>
        </div>
      )}
      {artifact.error && <div className="text-destructive pl-6 text-xs">{artifact.error}</div>}
    </div>
  );
}

function TerminalSessionActionRow({ artifact, variant, taskId, interactive }: {
  artifact: ReturnType<typeof readToolArtifact>;
  variant: ActionVariant;
  taskId?: number;
  interactive: boolean;
}) {
  const data = asRecord(artifact.display_data);
  const sessionId = text(data.session_id);
  const live = useLiveSession(taskId, sessionId);
  // 只读线程（子 Agent）与已失效会话不提供跳转。
  const canReveal = interactive && taskId !== undefined && sessionId !== undefined && live !== null && variant !== "close";
  const meta = artifact.error ?? actionMeta(variant, data);
  const reveal = canReveal ? () => useTerminalWorkbenchStore.getState().reveal(taskId, sessionId) : undefined;
  return (
    <DisclosureRowStatic
      role={reveal ? "button" : "status"}
      tabIndex={reveal ? 0 : undefined}
      title={reveal ? "在终端中查看" : undefined}
      onClick={reveal}
      onKeyDown={reveal ? (event: KeyboardEvent<HTMLDivElement>) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          reveal();
        }
      } : undefined}
      className={cn(reveal && "hover:bg-muted/50 cursor-pointer rounded-md")}
      leading={actionIcon(variant)}
      label={<span className={cn("text-sm font-medium", artifact.backendStatus === "failed" && "text-destructive")}>{actionLabel(variant)}</span>}
      meta={<span className="text-muted-foreground max-w-[55%] truncate font-mono" title={meta}>{meta}</span>}
      trailing={<ToolStatus status={artifact.backendStatus} />}
    />
  );
}

/** 交互终端工具卡片：start 为会话锚点卡，其余为可跳转到对应会话的动作行。 */
export function TerminalSessionTool({ taskId, interactive = false, artifact: rawArtifact }: ToolPartRendererProps) {
  const artifact = readToolArtifact(rawArtifact);
  const variant = sessionVariant(asRecord(artifact.presentation).variant);

  if (variant === "start") return <TerminalSessionStartCard artifact={artifact} taskId={taskId} interactive={interactive} />;
  if (variant !== "unknown") return <TerminalSessionActionRow artifact={artifact} variant={variant} taskId={taskId} interactive={interactive} />;

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

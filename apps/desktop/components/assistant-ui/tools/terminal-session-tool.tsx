import {
  ArrowDownIcon,
  ArrowUpIcon,
  KeyboardIcon,
  SquareTerminalIcon,
  TerminalIcon,
  XIcon,
} from "lucide-react";
import { useState, type KeyboardEvent, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import type { TerminalSessionEntry } from "@/lib/assistant/contract";
import { submitToolDecision } from "@/lib/assistant/task-commands";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { HttpError } from "@/lib/http/errors";
import { notify } from "@/lib/notify";
import { terminalStatusPresentation } from "@/lib/terminal/terminal-sessions";
import { useTerminalWorkbenchStore } from "@/lib/terminal/terminal-workbench-store";
import { cn } from "@/lib/utils";
import { asRecord, readToolArtifact } from "./types";
import { ToolStatus } from "./tool-status";
import type { ToolPartRendererProps } from "./tool-renderer-registry";
import { DisclosureRowStatic } from "../elements/disclosure-row.aui";
import { TERMINAL_CARD_CLASS, TERMINAL_CARD_HEADER_CLASS } from "../elements/tool-layout-tokens";

type TerminalSessionVariant = "start" | "list" | "read" | "write" | "handoff" | "close" | "unknown";
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
    case "terminal-session-handoff": return "handoff";
    case "terminal-session-close": return "close";
    default: return "unknown";
  }
}

/**
 * 条件等待的结束原因（后端 `wait_result.end_reason`）→ 中文；不据此宣告命令成功。
 * `idle` 为未带等待条件。
 */
const WAIT_END_REASON_LABELS: Record<string, string> = {
  command_returned: "命令已返回",
  matched: "匹配到输出",
  output_quiet: "输出已静默",
  elapsed: "等待结束",
  timeout: "等待超时",
  cancelled: "等待已取消",
  session_ended: "会话已结束",
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
    case "handoff": return "交给你输入";
    case "close": return "关闭终端会话";
  }
}

/**
 * 动作行摘要：write 显示实际输入内容（单行文本或按键序列）与等待结果，read 显示行范围与等待结果。
 * 都来自后端受控 `display_data`，不从 args 推导。
 */
function actionMeta(variant: ActionVariant, data: Record<string, unknown>): string {
  if (variant === "list") return typeof data.session_count === "number" ? `${data.session_count} 个会话` : "会话列表";
  if (variant === "handoff") return text(data.input_summary) ?? "请在终端中完成输入";
  if (variant === "close") {
    const status = data.status === "closed" || data.status === "exited" || data.status === "failed"
      ? terminalStatusPresentation(data.status).label
      : "会话已关闭";
    return [status, typeof data.exit_code === "number" ? `exit ${data.exit_code}` : null].filter(Boolean).join(" · ");
  }
  // 全屏程序期间后端返回整屏（`screen`），没有行号区间。
  const lead = variant === "write"
    ? text(data.input_summary) ?? null
    : data.screen === true ? "全屏画面" : lineRange(data.lines);
  return [lead, waitResultLabel(data.wait_result)].filter(Boolean).join(" · ") || (variant === "write" ? "已输入" : "读取输出");
}

/** 动作行与其他低噪音工具行一样用弱化图标；只有交接需要用户动手，用强调色。 */
function actionIcon(variant: ActionVariant): ReactNode {
  if (variant === "list") return <TerminalIcon className="size-4" aria-hidden="true" />;
  if (variant === "read") return <ArrowDownIcon className="size-4" aria-hidden="true" />;
  if (variant === "write") return <ArrowUpIcon className="size-4" aria-hidden="true" />;
  if (variant === "handoff") return <KeyboardIcon className="text-info size-4" aria-hidden="true" />;
  return <XIcon className="size-4" aria-hidden="true" />;
}

/**
 * 卡片上的会话状态读 snapshot 的实时会话（Task 级终端列表），而不是工具执行那一刻的快照。
 *
 * 「镜像尚未同步（sessions 为 null）」与「会话已消失」这里都返回 null、卡片显示「已失效」，没有区分：
 * 卡片挂载时镜像已由同一份 snapshot 同步好（webkit 实测进入 / 切回 Task 均未出现闪现）。若以后卡片
 * 能早于镜像同步渲染，需把 null 镜像当作「未知」不显示状态。
 */
function useLiveSession(taskId: number, sessionId: string | undefined): TerminalSessionEntry | null {
  return useTerminalWorkbenchStore((state) => (
    sessionId === undefined
      ? null
      : state.tasks[taskId]?.sessions?.find((session) => session.sessionId === sessionId) ?? null
  ));
}

function TerminalSessionStartCard({ artifact, taskId, interactive }: {
  artifact: ReturnType<typeof readToolArtifact>;
  taskId: number;
  interactive: boolean;
}) {
  const data = asRecord(artifact.display_data);
  const sessionId = text(data.session_id);
  const live = useLiveSession(taskId, sessionId);
  const status = live ? terminalStatusPresentation(live.status) : null;
  const canReveal = interactive && sessionId !== undefined && live !== null;
  const reveal = canReveal ? () => useTerminalWorkbenchStore.getState().reveal(taskId, sessionId) : undefined;
  const cwd = text(data.initial_cwd);

  // 与 execute_terminal 共用终端卡片外壳与表头几何，两者一眼可辨为同一族；会话卡不可折叠，
  // 正文只有一行 shell 与工作目录。
  return (
    <div className={TERMINAL_CARD_CLASS}>
      <div className={TERMINAL_CARD_HEADER_CLASS}>
        <DisclosureRowStatic
          tone="terminal"
          className="w-auto min-w-0 flex-1 bg-transparent px-3 hover:bg-transparent"
          leading={<SquareTerminalIcon className="text-info size-4" aria-hidden="true" />}
          label={<span className="text-sm font-medium">{text(data.label) ?? "终端会话"}</span>}
          meta={sessionId ? (
            <span className={cn("inline-flex items-center gap-1.5", status?.text ?? "text-muted-foreground")}>
              <span aria-hidden="true" className="size-1.5 rounded-full bg-current" />
              {status?.label ?? "已失效"}
            </span>
          ) : undefined}
          trailing={<ToolStatus status={artifact.backendStatus} />}
        />
        {interactive && sessionId && (
          <Button
            type="button"
            variant="ghost"
            size="xs"
            disabled={reveal === undefined}
            className="text-info hover:bg-info/10 hover:text-info shrink-0"
            onClick={reveal}
          >
            在终端中查看
          </Button>
        )}
      </div>
      <div className="bg-terminal-inset text-muted-foreground flex min-w-0 items-center gap-2 border-t px-3 py-1.5 font-mono text-2xs">
        <span className="text-foreground/80 shrink-0">{text(data.shell_kind) ?? "默认 Shell"}</span>
        <span aria-hidden="true">·</span>
        <span className="min-w-0 truncate" title={cwd}>{cwd ?? "工作目录未知"}</span>
      </div>
      {artifact.error && <div className="text-destructive border-t px-3 py-1.5 text-xs">{artifact.error}</div>}
    </div>
  );
}

function TerminalSessionActionRow({ artifact, variant, taskId, toolCallId, interactive }: {
  artifact: ReturnType<typeof readToolArtifact>;
  variant: ActionVariant;
  taskId: number;
  toolCallId: string;
  interactive: boolean;
}) {
  const data = asRecord(artifact.display_data);
  const sessionId = text(data.session_id);
  const live = useLiveSession(taskId, sessionId);
  // 只读线程（子 Agent）与已失效会话不提供跳转。
  const canReveal = interactive && sessionId !== undefined && live !== null && variant !== "close";
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
      trailing={
        variant === "handoff" && interactive && artifact.backendStatus === "pending"
          ? <HandoffReturnButton taskId={taskId} toolCallId={toolCallId} />
          : <ToolStatus status={artifact.backendStatus} />
      }
    />
  );
}

/**
 * 交接卡片上的「交还」：交接是 HITL 调用，等待占位为 `pending` 时可对它作出决定。会话已结束或后端
 * 重启过时终端面板里没有交接提示，这里是唯一入口；决定执行后卡片随 snapshot 推到终态。
 */
function HandoffReturnButton({ taskId, toolCallId }: { taskId: number; toolCallId: string }) {
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    setBusy(true);
    try {
      await submitToolDecision(taskId, toolCallId, { decision: "approve" });
    } catch (error) {
      if (!(error instanceof HttpError && error.status === 409)) notify.error(userErrorMessage(error, "交还失败，请稍后重试"));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Button
      type="button"
      size="xs"
      variant="secondary"
      disabled={busy}
      onClick={(event) => {
        event.stopPropagation();
        void submit();
      }}
    >
      交还
    </Button>
  );
}

/** 交互终端工具卡片：start 为会话锚点卡，其余为可跳转到对应会话的动作行。 */
export function TerminalSessionTool({ taskId, toolCallId, interactive = false, artifact: rawArtifact }: ToolPartRendererProps) {
  const artifact = readToolArtifact(rawArtifact);
  const variant = sessionVariant(asRecord(artifact.presentation).variant);

  if (variant === "start") return <TerminalSessionStartCard artifact={artifact} taskId={taskId} interactive={interactive} />;
  if (variant !== "unknown") return <TerminalSessionActionRow artifact={artifact} variant={variant} taskId={taskId} toolCallId={toolCallId} interactive={interactive} />;

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

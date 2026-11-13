import { PauseIcon } from "lucide-react";

import { Badge, type BadgeTone } from "@/components/ui/badge";
import { useSessionAwaitingUserCalls, useSessionExecutionStatus } from "@/lib/session-status/session-status-store";
import { cn } from "@/lib/utils";

/**
 * Session 状态的统一呈现：执行状态（当前 Run）与「等你操作」是两项正交的事实，都来自 Session 状态推送。
 *
 * 侧栏、命令面板、页头、工作台标签与派生调用行共用，保证同一状态在各处的文案、图标与语气一致。
 * 「等你操作」优先占据状态位：它是唯一必须由你动手才能往下走的状态，Run 已完成或被停下时审批仍可能
 * 在等你。图标与色调沿用对话里的调用行与等待卡（暂停、info），用户不必再学一套。`suspended` 只表示
 * Agent 在等结果（后台命令、子会话），不是在叫你，所以是中性的。
 * 侧栏与页面头部默认隐藏已完成与无 Run；工作台标签可显式展示完成态。
 */
type Presentation = { label: string; tone: BadgeTone; active: boolean };

const EXECUTION_PRESENTATION: Record<string, Presentation> = {
  completed: { label: "已完成", tone: "success", active: false },
  running: { label: "运行中", tone: "info", active: true },
  suspended: { label: "等待结果", tone: "neutral", active: false },
  failed: { label: "运行失败", tone: "destructive", active: false },
  cancelled: { label: "已停止", tone: "neutral", active: false },
};

export const AWAITING_USER_LABEL = "等你操作";

export function executionStatusPresentation(status: string | null | undefined, showCompleted = false): Presentation | null {
  if (status === "completed" && !showCompleted) return null;
  return status ? EXECUTION_PRESENTATION[status] ?? null : null;
}

function awaitingUserLabel(calls: number): string {
  return calls > 1 ? `${AWAITING_USER_LABEL} · ${calls}` : AWAITING_USER_LABEL;
}

const DOT_CLASS: Record<BadgeTone, string> = {
  neutral: "bg-muted-foreground/60",
  primary: "bg-primary",
  info: "bg-info",
  success: "bg-success",
  warning: "bg-warning",
  destructive: "bg-destructive",
  skill: "bg-(--skill-foreground)",
};

/** 执行状态的小圆点；运行中带呼吸动画（减少动态效果时由全局规则停止）。 */
function ExecutionStatusDot({ status, className }: { status: string | null; className?: string }) {
  const presentation = executionStatusPresentation(status);
  if (!presentation) return null;
  return (
    <span
      role="img"
      aria-label={presentation.label}
      title={presentation.label}
      className={cn("inline-block size-2 shrink-0 rounded-full", DOT_CLASS[presentation.tone], presentation.active && "animate-pulse", className)}
    />
  );
}

/**
 * 侧栏与命令面板的状态位：有事等你操作时是暂停图标（用形状而不只是颜色区分：减少动态效果时运行中的
 * 蓝点不再呼吸），否则是执行状态圆点。`dotClassName` 只作用于圆点（侧栏把它压在 Fork 图标角上）。
 */
export function SessionStatusMarker({ sessionId, dotClassName }: { sessionId: number; dotClassName?: string }) {
  const status = useSessionExecutionStatus(sessionId);
  const awaiting = useSessionAwaitingUserCalls(sessionId);
  if (awaiting > 0) {
    const label = awaitingUserLabel(awaiting);
    return (
      <span role="img" aria-label={label} title={label} className="text-info inline-flex shrink-0">
        <PauseIcon aria-hidden="true" className="size-3.5" />
      </span>
    );
  }
  return <ExecutionStatusDot status={status} className={dotClassName} />;
}

/** 页面头部的状态徽标：等你操作与执行状态并列，进到会话里两件事都要知道。 */
export function SessionStatusBadges({ sessionId }: { sessionId: number }) {
  const status = useSessionExecutionStatus(sessionId);
  const awaiting = useSessionAwaitingUserCalls(sessionId);
  const presentation = executionStatusPresentation(status);
  return (
    <>
      {awaiting > 0 && (
        <Badge tone="info" variant="tint" size="sm" className="gap-1.5">
          <PauseIcon aria-hidden="true" className="size-3" />
          {awaitingUserLabel(awaiting)}
        </Badge>
      )}
      {presentation && (
        <Badge tone={presentation.tone} variant="tint" size="sm" className="gap-1.5">
          <ExecutionStatusDot status={status} className="size-1.5" />
          {presentation.label}
        </Badge>
      )}
    </>
  );
}

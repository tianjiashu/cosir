import { Badge, type BadgeTone } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

/**
 * 任务执行状态（后端 `execution_status`，由 Task 当前指向的 Run 投影）的统一呈现。
 *
 * 侧栏任务行与页面头部共用，保证同一状态在两处的文案与语气一致。
 * 侧栏与页面头部默认隐藏已完成与无轮次；工作台标签可显式展示完成态。
 */
type Presentation = { label: string; tone: BadgeTone; active: boolean };

const PRESENTATION: Record<string, Presentation> = {
  completed: { label: "已完成", tone: "success", active: false },
  pending: { label: "排队中", tone: "info", active: true },
  running: { label: "运行中", tone: "info", active: true },
  waiting_for_input: { label: "等待确认", tone: "warning", active: false },
  failed: { label: "运行失败", tone: "destructive", active: false },
  cancelled: { label: "已停止", tone: "neutral", active: false },
};

export function taskRunStatusPresentation(status: string | null | undefined, showCompleted = false): Presentation | null {
  if (status === "completed" && !showCompleted) return null;
  return status ? PRESENTATION[status] ?? null : null;
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

/** 侧栏用的小圆点；运行中带呼吸动画（减少动态效果时由全局规则停止）。 */
export function TaskRunStatusDot({ status, className }: { status: string | null | undefined; className?: string }) {
  const presentation = taskRunStatusPresentation(status);
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

/** 页面头部用的状态徽标。 */
export function TaskRunStatusBadge({ status }: { status: string | null | undefined }) {
  const presentation = taskRunStatusPresentation(status);
  if (!presentation) return null;
  return (
    <Badge tone={presentation.tone} variant="tint" size="sm" className="gap-1.5">
      <TaskRunStatusDot status={status} className="size-1.5" />
      {presentation.label}
    </Badge>
  );
}

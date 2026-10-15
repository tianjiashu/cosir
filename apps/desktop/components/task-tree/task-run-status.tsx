import { Badge, type BadgeTone } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

/**
 * 任务执行状态（后端 `execution_status`，由最新轮次派生）的统一呈现。
 *
 * 侧栏任务行与页面头部共用，保证同一状态在两处的文案与语气一致。
 * 已完成与无轮次不展示状态，只有需要用户注意的状态才出现。
 */
type Presentation = { label: string; tone: BadgeTone; active: boolean };

const PRESENTATION: Record<string, Presentation> = {
  pending: { label: "排队中", tone: "info", active: true },
  running: { label: "运行中", tone: "info", active: true },
  waiting_for_input: { label: "等待确认", tone: "warning", active: false },
  failed: { label: "运行失败", tone: "destructive", active: false },
  cancelled: { label: "已停止", tone: "neutral", active: false },
};

export function taskRunStatusPresentation(status: string | null | undefined): Presentation | null {
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

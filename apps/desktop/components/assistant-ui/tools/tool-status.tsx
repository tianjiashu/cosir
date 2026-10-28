import { CheckIcon, CircleAlertIcon, CircleDashedIcon, LoaderCircleIcon, XCircleIcon } from "lucide-react";
import type { TransportToolStatus } from "@/lib/assistant/contract";
import { cn } from "@/lib/utils";

const STATUS_LABELS: Record<TransportToolStatus, string> = {
  pending: "准备执行",
  running: "执行中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  unknown: "状态未知",
};

/** 工具调用生命周期的唯一文案表；Run / TeamRun 状态各有自己的表，不要拿这里翻译它们。 */
export function toolStatusLabel(status: TransportToolStatus): string {
  return STATUS_LABELS[status];
}

/** 失败与「状态未知」需要用户注意，其余生命周期状态保持低调；未知与工具组摘要统一用 warning。 */
const STATUS_TONE_CLASS: Partial<Record<TransportToolStatus, string>> = {
  failed: "text-destructive",
  unknown: "text-warning",
};

export function ToolStatus({ status, className, prefix }: { status: TransportToolStatus; className?: string; prefix?: string }) {
  const Icon = status === "running" ? LoaderCircleIcon
    : status === "completed" ? CheckIcon
      : status === "failed" ? CircleAlertIcon
        : status === "cancelled" ? XCircleIcon
          : status === "unknown" ? CircleAlertIcon : CircleDashedIcon;
  return (
    <span className={cn("inline-flex items-center gap-1.5 text-xs", STATUS_TONE_CLASS[status] ?? "text-muted-foreground", className)}>
      <Icon className={cn("size-3.5", status === "running" && "animate-spin")} aria-hidden="true" />
      {prefix ? `${prefix} · ` : ""}{toolStatusLabel(status)}
    </span>
  );
}

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

export function ToolStatus({ status, className }: { status: TransportToolStatus; className?: string }) {
  const Icon = status === "running" ? LoaderCircleIcon
    : status === "completed" ? CheckIcon
      : status === "failed" ? CircleAlertIcon
        : status === "cancelled" ? XCircleIcon
          : status === "unknown" ? CircleAlertIcon : CircleDashedIcon;
  return (
    <span className={cn("inline-flex items-center gap-1.5 text-xs", (status === "failed" || status === "unknown") ? "text-destructive" : "text-muted-foreground", className)}>
      <Icon className={cn("size-3.5", status === "running" && "animate-spin")} aria-hidden="true" />
      {STATUS_LABELS[status]}
    </span>
  );
}

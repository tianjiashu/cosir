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

/**
 * 工具行尾的状态标记。
 *
 * 成功是绝大多数调用的常态，展开工具组时逐行重复「已完成」只增加噪音，所以只显示图标、
 * 文案留给读屏；其余状态需要用户留意，保留可见文案。
 */
export function ToolStatus({ status, className }: { status: TransportToolStatus; className?: string }) {
  const Icon = status === "running" ? LoaderCircleIcon
    : status === "completed" ? CheckIcon
      : status === "failed" ? CircleAlertIcon
        : status === "cancelled" ? XCircleIcon
          : status === "unknown" ? CircleAlertIcon : CircleDashedIcon;
  const label = toolStatusLabel(status);
  return (
    <span className={cn("inline-flex items-center gap-1.5 text-xs", STATUS_TONE_CLASS[status] ?? "text-muted-foreground", className)}>
      <Icon className={cn("size-3.5", status === "running" && "animate-spin", status === "completed" && "text-success")} aria-hidden="true" />
      <span className={status === "completed" ? "sr-only" : undefined}>{label}</span>
    </span>
  );
}

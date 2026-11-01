import { CircleAlertIcon, LoaderCircleIcon, XCircleIcon } from "lucide-react";

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

/**
 * 调用行的状态位：只在运行中（含排队）、失败、取消出现，成功不画对勾——成功是常态，对勾只制造噪声。
 * 位置固定占一格，行内其他元素不会因状态出现而横移。文案留给读屏。
 */
export function CallStatus({ status, className }: { status: TransportToolStatus; className?: string }) {
  const unfinished = status === "pending" || status === "running";
  const Icon = unfinished ? LoaderCircleIcon
    : status === "failed" || status === "unknown" ? CircleAlertIcon
      : status === "cancelled" ? XCircleIcon
        : null;
  return (
    <span
      data-call-status={status}
      className={cn(
        "flex size-3.5 shrink-0 items-center justify-center",
        status === "failed" ? "text-destructive" : status === "unknown" ? "text-warning" : "text-muted-foreground",
        className,
      )}
    >
      {Icon && <Icon className={cn("size-3.5", unfinished && "animate-spin")} aria-hidden="true" />}
      {Icon && <span className="sr-only">{toolStatusLabel(status)}</span>}
    </span>
  );
}

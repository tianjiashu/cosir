import { CircleAlertIcon, PauseIcon, XCircleIcon } from "lucide-react";

import type { TransportToolStatus } from "@/lib/assistant/contract";
import { cn } from "@/lib/utils";

/**
 * 调用行的状态位：只有失败、取消、等你操作画图标。运行中的信号在标题扫光上，成功是常态、不说话。
 * 位置固定占一格，行内其他元素不会因状态出现而横移。文案留给读屏。
 */
export function CallStatus({ status, awaitingUser, label }: { status: TransportToolStatus; awaitingUser: boolean; label: string }) {
  const Icon = awaitingUser ? PauseIcon
    : status === "failed" ? CircleAlertIcon
      : status === "cancelled" ? XCircleIcon
        : null;
  return (
    <span
      data-call-status={status}
      className={cn(
        "flex size-3.5 shrink-0 items-center justify-center",
        status === "failed" ? "text-destructive" : "text-muted-foreground",
      )}
    >
      {Icon && <Icon className="size-3.5" aria-hidden="true" />}
      {status !== "completed" && <span className="sr-only">{label}</span>}
    </span>
  );
}

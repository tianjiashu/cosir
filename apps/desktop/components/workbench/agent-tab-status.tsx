import { BotIcon, CheckIcon, CircleAlertIcon, CircleHelpIcon, ClockIcon, LoaderCircleIcon, SquareIcon, UnplugIcon } from "lucide-react";
import { taskRunStatusPresentation } from "@/components/task-tree/task-run-status";
import { useTaskExecutionStatus, useTaskStatusConnected } from "@/lib/task-status/task-status-store";

const STATUS_ICONS = {
  pending: ClockIcon,
  running: LoaderCircleIcon,
  suspended: CircleHelpIcon,
  completed: CheckIcon,
  failed: CircleAlertIcon,
  cancelled: SquareIcon,
};
const TONE_CLASS = {
  info: "text-info", warning: "text-warning", success: "text-success", destructive: "text-destructive",
  neutral: "text-muted-foreground", primary: "text-primary", skill: "text-skill-foreground",
};

/**
 * 状态图形占用原 Agent 图标位置；状态来自应用级 Task 状态推送（与侧栏同一来源）。推送断开时
 * 保留上次的状态并标为未同步，避免与草稿未保存圆点混淆。
 */
export function WorkbenchAgentTabStatus({ taskId, expanded = false }: { taskId: number; expanded?: boolean }) {
  const status = useTaskExecutionStatus(taskId);
  const connected = useTaskStatusConnected();
  const presentation = taskRunStatusPresentation(status, true);
  const knownLabel = presentation?.label ?? "尚未运行";
  const label = connected ? knownLabel : `状态未同步${status ? `，上次为${knownLabel}` : ""}`;
  if (expanded) return <span> · {label}</span>;
  const Icon = !connected ? UnplugIcon : STATUS_ICONS[status as keyof typeof STATUS_ICONS] ?? BotIcon;
  return <span role="img" aria-label={label} className="inline-flex shrink-0">
    <Icon aria-hidden="true" className={`size-3.5 ${!connected ? "text-muted-foreground" : presentation ? TONE_CLASS[presentation.tone] : ""}`} />
  </span>;
}

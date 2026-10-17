import { BotIcon, CheckIcon, CircleAlertIcon, CircleHelpIcon, ClockIcon, LoaderCircleIcon, SquareIcon, UnplugIcon } from "lucide-react";
import { taskRunStatusPresentation } from "@/components/task-tree/task-run-status";
import { HttpError } from "@/lib/http/errors";
import { useWorkbenchAgentStatus } from "@/lib/workbench/agent-status";

const STATUS_ICONS = {
  pending: ClockIcon,
  running: LoaderCircleIcon,
  waiting_for_input: CircleHelpIcon,
  completed: CheckIcon,
  failed: CircleAlertIcon,
  cancelled: SquareIcon,
};
const TONE_CLASS = {
  info: "text-info", warning: "text-warning", success: "text-success", destructive: "text-destructive",
  neutral: "text-muted-foreground", primary: "text-primary", skill: "text-skill-foreground",
};

/** 状态图形占用原 Agent 图标位置；未同步与运行失败分别表达，避免与草稿未保存圆点混淆。 */
export function WorkbenchAgentTabStatus({ taskId, expanded = false }: { taskId: number; expanded?: boolean }) {
  const { query, available } = useWorkbenchAgentStatus(taskId);
  const status = query.data?.status;
  const missing = query.error instanceof HttpError && query.error.status === 404;
  const unsynced = !available || query.isError;
  const presentation = taskRunStatusPresentation(status, true);
  const knownLabel = presentation?.label ?? "尚未运行";
  const label = missing ? "任务已不存在"
    : unsynced ? `状态未同步${query.data ? `，上次为${knownLabel}` : ""}`
    : query.isPending ? "正在同步状态" : knownLabel;
  if (expanded) return <span> · {label}</span>;
  const Icon = missing || unsynced ? UnplugIcon
    : query.isPending ? ClockIcon
    : STATUS_ICONS[status as keyof typeof STATUS_ICONS] ?? BotIcon;
  return <span role="img" aria-label={label} className="inline-flex shrink-0">
    <Icon aria-hidden="true" className={`size-3.5 ${missing || unsynced ? "text-muted-foreground" : presentation ? TONE_CLASS[presentation.tone] : ""}`} />
  </span>;
}

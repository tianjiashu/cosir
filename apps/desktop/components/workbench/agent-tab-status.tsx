import { BotIcon, CheckIcon, CircleAlertIcon, ClockIcon, HourglassIcon, LoaderCircleIcon, SquareIcon, UnplugIcon } from "lucide-react";
import { awaitingPresentation, executionStatusPresentation, toneTextClass } from "@/components/session-status/session-status-indicator";
import { useSessionStatus, useSessionStatusConnected } from "@/lib/session-status/session-status-store";

const STATUS_ICONS = {
  pending: ClockIcon,
  running: LoaderCircleIcon,
  suspended: HourglassIcon,
  completed: CheckIcon,
  failed: CircleAlertIcon,
  cancelled: SquareIcon,
};

/**
 * 状态图形占用原 Agent 图标位置；状态来自应用级 Session 状态推送（与侧栏同一来源）。推送断开时
 * 保留上次的状态并标为未同步，避免与草稿未保存圆点混淆。在等你（待审批、待回答）优先于执行状态，同侧栏。
 */
export function WorkbenchAgentTabStatus({ sessionId, expanded = false }: { sessionId: number; expanded?: boolean }) {
  const status = useSessionStatus(sessionId);
  const connected = useSessionStatusConnected();
  const awaiting = awaitingPresentation(status);
  const presentation = awaiting ?? executionStatusPresentation(status?.executionStatus, true);
  const knownLabel = presentation?.label ?? "尚未运行";
  const label = connected ? knownLabel : `状态未同步${presentation ? `，上次为${knownLabel}` : ""}`;
  if (expanded) return <span> · {label}</span>;
  const Icon = !connected ? UnplugIcon : awaiting?.icon ?? STATUS_ICONS[status?.executionStatus as keyof typeof STATUS_ICONS] ?? BotIcon;
  const tone = !connected ? "text-muted-foreground" : presentation ? toneTextClass(presentation.tone) : "";
  return <span role="img" aria-label={label} className="inline-flex shrink-0">
    <Icon aria-hidden="true" className={`size-3.5 ${tone}`} />
  </span>;
}

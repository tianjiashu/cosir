import { LoaderCircleIcon, MessageCircleQuestionMarkIcon, ShieldAlertIcon, type LucideIcon } from "lucide-react";

import type { BadgeTone } from "@/components/ui/badge";
import { sessionAttention, type SessionAttention } from "@/lib/session-status/session-attention";
import { useSessionStatus, type SessionStatus } from "@/lib/session-status/session-status-store";
import { cn } from "@/lib/utils";

/**
 * Session 状态的统一呈现，各处的文案、图标与语气只在这里定。
 *
 * 两种读法：列表（侧栏、命令面板）按「要不要你注意」只给一个标记（`sessionAttention` 推导）；明细（工作台标签、
 * 派生调用行）说执行状态本身。在等你的两类沿用对话里卡片的图标与色调——审批卡是琥珀色盾牌、提问卡是蓝色——
 * 用户不必再学一套；用形状而不只是颜色区分，减少动态效果时进行中的圆环停转仍认得出。
 */
type Presentation = { label: string; tone: BadgeTone };

const EXECUTION_PRESENTATION: Record<string, Presentation> = {
  completed: { label: "已完成", tone: "success" },
  running: { label: "运行中", tone: "info" },
  suspended: { label: "等待结果", tone: "neutral" },
  failed: { label: "运行失败", tone: "destructive" },
  cancelled: { label: "已停止", tone: "neutral" },
};

/** 明细里的执行状态；没有 Run 或未知状态为 null。完成是常态，默认不说，工作台标签显式要。 */
export function executionStatusPresentation(status: string | null | undefined, showCompleted = false): Presentation | null {
  if (status === "completed" && !showCompleted) return null;
  return status ? EXECUTION_PRESENTATION[status] ?? null : null;
}

const TONE_TEXT: Record<BadgeTone, string> = {
  neutral: "text-muted-foreground",
  primary: "text-primary",
  info: "text-info",
  success: "text-success",
  warning: "text-warning",
  destructive: "text-destructive",
  skill: "text-skill-foreground",
};

export function toneTextClass(tone: BadgeTone): string {
  return TONE_TEXT[tone];
}

type AwaitingPresentation = Presentation & { icon: LucideIcon };

/** 在等你的调用（待审批优先于待回答，同 `sessionAttention`）；没有为 null。多于一个时带数量。 */
export function awaitingPresentation(status: SessionStatus | null | undefined): AwaitingPresentation | null {
  if (!status) return null;
  const counted = (label: string, count: number) => (count > 1 ? `${label} · ${count}` : label);
  if (status.awaitingApprovals > 0) return { label: counted("待审批", status.awaitingApprovals), tone: "warning", icon: ShieldAlertIcon };
  if (status.awaitingDecisions > 0) return { label: counted("待回答", status.awaitingDecisions), tone: "info", icon: MessageCircleQuestionMarkIcon };
  return null;
}

/** 列表上每种提醒的说法；在等你的两类另由 `awaitingPresentation` 带数量。 */
const ATTENTION_LABEL: Record<Exclude<SessionAttention, "approval" | "answer">, Presentation> = {
  working: { label: "进行中", tone: "neutral" },
  failed: { label: "运行失败", tone: "destructive" },
  unread: { label: "有新结果", tone: "info" },
};

export function attentionPresentation(attention: SessionAttention, status: SessionStatus): Presentation {
  return attention === "approval" || attention === "answer" ? awaitingPresentation(status)! : ATTENTION_LABEL[attention];
}

/** 列表上的提醒：在等你与进行中是图标，未读是圆点（标记位里放得下身份图标时压在它角上）。 */
export function attentionIsIcon(attention: SessionAttention): boolean {
  return attention !== "failed" && attention !== "unread";
}

/** 侧栏与命令面板读的同一份提醒；`viewing` 是你正开着这个会话。 */
export function useSessionAttention(sessionId: number, viewing = false): { attention: SessionAttention | null; status: SessionStatus | null } {
  const status = useSessionStatus(sessionId);
  return { attention: sessionAttention(status, viewing), status };
}

/**
 * 侧栏与命令面板的状态位。进行中的圆环慢转（减少动态效果时由全局规则停转）；`dotClassName` 只作用于圆点
 * （侧栏把它压在 Fork 图标角上）。没有要提醒的事时什么也不画。
 */
export function SessionStatusMarker({ sessionId, viewing = false, dotClassName }: { sessionId: number; viewing?: boolean; dotClassName?: string }) {
  const { attention, status } = useSessionAttention(sessionId, viewing);
  if (!attention || !status) return null;
  const { label, tone } = attentionPresentation(attention, status);
  if (attention === "working") {
    const hint = status.executionStatus === "suspended" ? "进行中，在等后台结果" : label;
    return <LoaderCircleIcon role="img" aria-label={hint} className="size-3.5 shrink-0 animate-spin text-muted-foreground [animation-duration:1.6s]" />;
  }
  if (!attentionIsIcon(attention)) {
    return (
      <span
        role="img"
        aria-label={label}
        title={label}
        className={cn("inline-block size-2 shrink-0 rounded-full", attention === "failed" ? "bg-destructive" : "bg-info", dotClassName)}
      />
    );
  }
  const Icon = awaitingPresentation(status)!.icon;
  return (
    <span role="img" aria-label={label} title={label} className={cn("inline-flex shrink-0", toneTextClass(tone))}>
      <Icon aria-hidden="true" className="size-3.5" />
    </span>
  );
}

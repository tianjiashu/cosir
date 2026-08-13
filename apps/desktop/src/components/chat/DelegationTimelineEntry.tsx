import { Children, type ReactNode, memo, useState } from "react";
import {
  Ban,
  Bot,
  CheckCircle2,
  ChevronRight,
  Clock,
  Loader2,
  XCircle,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Caption } from "@/components/ui/tokens";
import { cn } from "@/lib/utils";
import type { TimelineDelegationStatus } from "@/services/timeline/projector";
import { useDelegationStore } from "@/stores/delegationStore";

/** Props for rendering a parent-turn delegation lifecycle entry. */
interface DelegationTimelineEntryProps {
  /** Child AgentProfile id selected for this delegation. */
  childAgentId: string;
  /** Current delegation lifecycle status. */
  status: TimelineDelegationStatus;
  /** Child turn id once the backend has created the child run. */
  childTurnId?: string;
  /** Display-only delegation category. */
  delegationType: string;
  /** Successful terminal summary. */
  summary?: string;
  /** Failed or cancelled terminal reason. */
  error?: string;
  /** Render-ready expanded child timeline events. */
  childEntries?: ReactNode;
}

const STATUS_CONFIG: Record<
  TimelineDelegationStatus,
  {
    variant: "outline" | "success" | "destructive" | "warning" | "secondary";
    Icon: typeof Clock;
  }
> = {
  pending: { variant: "outline", Icon: Clock },
  running: { variant: "secondary", Icon: Loader2 },
  waiting_approval: { variant: "warning", Icon: Clock },
  completed: { variant: "success", Icon: CheckCircle2 },
  failed: { variant: "destructive", Icon: XCircle },
  cancelled: { variant: "outline", Icon: Ban },
};

/**
 * 在父 turn timeline 内渲染一行 delegation 生命周期条目。
 *
 * 行为（Task 1 A2）：
 * - 整行可点击：点击或键盘（Enter/Space）触发 `delegationStore.selectChildTurn(childTurnId)`，
 *   在右侧面板打开该 child turn 的完整 timeline（仅当 `childTurnId` 存在时）。
 * - 行支持键盘可达：`role="button"`、`tabIndex={0}`、`onKeyDown`（Enter/Space）。
 * - 选中态视觉高亮：当 `delegationStore.selectedChildTurnId` 与当前 `childTurnId` 相等时加高亮边框。
 * - 内联展开能力保留：原 `childEntries` 折叠箭头展开逻辑不受影响，与「侧边栏入口」互补共存。
 *
 * @param props - 委派元数据与可选的已展开子 timeline 内容。
 * @returns 可点击/可键盘触发的 timeline 行（含状态、子标识、终态摘要/错误、可选内联展开内容）。
 *
 * @throws 不主动抛出异常。
 *
 * @sideeffect 点击/键盘触发会调用 `delegationStore.selectChildTurn`，更新全局委派选中态（副作用跨组件）。
 *   维护本地展开/折叠状态用于内联 child entries。
 */
export const DelegationTimelineEntry = memo(function DelegationTimelineEntry({
  childAgentId,
  status,
  childTurnId,
  delegationType,
  summary,
  error,
  childEntries,
}: DelegationTimelineEntryProps) {
  const [isOpen, setIsOpen] = useState(false);
  const hasChildEntries = Children.count(childEntries) > 0;
  const { variant, Icon } = STATUS_CONFIG[status];
  const detail = status === "failed" || status === "cancelled" ? error : summary;

  // 选中态：读取委派选中 store，与当前 childTurnId 比对决定高亮（单一职责：仅 UI 选中态）。
  const selectedChildTurnId = useDelegationStore((state) => state.selectedChildTurnId);
  const selectChildTurn = useDelegationStore((state) => state.selectChildTurn);
  const isSelected = childTurnId != null && childTurnId === selectedChildTurnId;

  const openSidePanel = () => {
    if (childTurnId) {
      selectChildTurn(childTurnId);
    }
  };

  const handleKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (!childTurnId) return;
    if (event.key === "Enter" || event.key === " ") {
      // 阻止空格触发页面滚动，保持与 click 一致的行为。
      event.preventDefault();
      openSidePanel();
    }
  };

  return (
    <div className="w-full min-w-0 border-l border-border pl-3 py-1">
      <div
        className={cn(
          "flex w-full min-w-0 cursor-pointer items-start gap-2 rounded-sm outline-none transition-colors",
          "focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1",
          isSelected && "bg-accent/50 ring-1 ring-accent",
        )}
        role="button"
        tabIndex={childTurnId ? 0 : -1}
        aria-pressed={isSelected}
        aria-label={childTurnId ? `Open ${childAgentId} child timeline in side panel` : undefined}
        onClick={openSidePanel}
        onKeyDown={handleKeyDown}
      >
        <Bot className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
        <div className="min-w-0 flex-1 space-y-1">
          <div className="flex min-w-0 flex-wrap items-center gap-1.5">
            {hasChildEntries && (
              <button
                type="button"
                onClick={(event) => {
                  // 阻止冒泡：内联展开与「侧边栏入口」是互补的两条路径，互不触发。
                  event.stopPropagation();
                  setIsOpen((prev) => !prev);
                }}
                className="inline-flex shrink-0 items-center rounded p-0.5 text-muted-foreground transition-colors hover:bg-accent/40 hover:text-foreground"
                aria-label={isOpen ? "Collapse delegated child events" : "Expand delegated child events"}
              >
                <ChevronRight
                  className={cn("h-3.5 w-3.5 transition-transform", isOpen && "rotate-90")}
                />
              </button>
            )}
            <Badge variant={variant} className="shrink-0 gap-1 px-2 py-0">
              <Icon className={cn("h-3 w-3", status === "running" && "animate-spin")} />
              {status}
            </Badge>
            <span className={cn(Caption.mono, "min-w-0 break-all text-muted-foreground")}>
              {childAgentId}
            </span>
            <span className={cn(Caption.xs, "shrink-0 text-muted-foreground")}>
              {delegationType}
            </span>
          </div>
          {childTurnId && (
            <div className={cn(Caption.mono, "flex min-w-0 flex-wrap gap-1 text-muted-foreground")}>
              <span className="shrink-0">child turn:</span>
              <span className="min-w-0 break-all">{childTurnId}</span>
            </div>
          )}
          {detail && (
            <p
              className={cn(
                "min-w-0 whitespace-pre-wrap break-words text-sm",
                status === "failed" || status === "cancelled"
                  ? "text-destructive"
                  : "text-muted-foreground",
              )}
            >
              {detail}
            </p>
          )}
          {hasChildEntries && isOpen && (
            <div className="min-w-0 space-y-2 border-l border-border pl-3">
              {childEntries}
            </div>
          )}
        </div>
      </div>
    </div>
  );
});

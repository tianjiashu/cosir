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
  /**
   * 并发组规模：同 parent turn 下同时处于 running 的 delegation 数量。
   * 仅当构成并发组（数量 >= 2）时由投影器附加，非并发为 undefined。
   */
  concurrencyGroupSize?: number;
  /**
   * 并发组内序号：该 delegation 在并发组中的稳定位置（从 0 起）。
   * 仅当 `concurrencyGroupSize >= 2` 时附加；非并发时为 undefined。
   */
  concurrencyIndex?: number;
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
 * 行为（Task 1 A2，已修正 ARIA 嵌套）：
 * - 「打开侧边栏」为行内的一个显式原生 `<button type="button" aria-current={isSelected}>`
 *   （包裹 childAgentId + child turn id 主区域），点击或键盘（原生 Enter/Space）触发
 *   `delegationStore.selectChildTurn(childTurnId)`，在右侧面板打开该 child turn 的完整 timeline
 *   （仅当 `childTurnId` 存在时）。原生 button 自带键盘可达，无需手写 onKeyDown/role/tabIndex。
 * - 「内联展开」为另一个兄弟 `<button>`（折叠箭头），仅负责展开/收起 `childEntries`，
 *   与「打开侧边栏」是同级、互不嵌套的两个原生交互目标，各自独立可达，
 *   焦点在展开箭头上按 Enter 只展开、不打开侧边栏（修复原 role=button 嵌套导致的键盘 bug）。
 * - 选中态视觉高亮：当 `delegationStore.selectedChildTurnId` 与当前 `childTurnId` 相等时，
 *   给「打开侧边栏」按钮加高亮边框（aria-current 同步）。
 * - 并发指示：当 `concurrencyGroupSize` 存在且 >= 2 时，在行内（状态徽章旁）渲染
 *   `并发 ${concurrencyIndex + 1} / ${concurrencyGroupSize}` 徽章，提示该 delegation 属于
 *   一个并发组（与同组的其它 delegation 行由 TurnTimeline 的泳道左边框在视觉上归组）。
 *   非并发（size < 2 或字段缺失）不渲染并发指示。
 * - 内联展开能力保留：`childEntries` 折叠箭头展开逻辑不受影响，与「侧边栏入口」互补共存。
 *
 * @param props - 委派元数据与可选的已展开子 timeline 内容。
 * @returns timeline 行（含状态、子标识、终态摘要/错误、可选内联展开内容；两个兄弟交互按钮）。
 *
 * @throws 不主动抛出异常。
 *
 * @sideeffect 点击「打开侧边栏」按钮会调用 `delegationStore.selectChildTurn`，更新全局委派选中态
 *   （副作用跨组件）。维护本地展开/折叠状态用于内联 child entries。
 */
export const DelegationTimelineEntry = memo(function DelegationTimelineEntry({
  childAgentId,
  status,
  childTurnId,
  delegationType,
  summary,
  error,
  childEntries,
  concurrencyGroupSize,
  concurrencyIndex,
}: DelegationTimelineEntryProps) {
  const [isOpen, setIsOpen] = useState(false);
  const hasChildEntries = Children.count(childEntries) > 0;
  const { variant, Icon } = STATUS_CONFIG[status];
  const detail = status === "failed" || status === "cancelled" ? error : summary;
  // 并发指示：仅当并发组规模 >= 2 时展示「并发 index+1/size」徽章（与同组泳道左边框互补）。
  const showConcurrency = concurrencyGroupSize != null && concurrencyGroupSize >= 2;

  // 选中态：读取委派选中 store，与当前 childTurnId 比对决定高亮（单一职责：仅 UI 选中态）。
  const selectedChildTurnId = useDelegationStore((state) => state.selectedChildTurnId);
  const selectChildTurn = useDelegationStore((state) => state.selectChildTurn);
  const isSelected = childTurnId != null && childTurnId === selectedChildTurnId;

  return (
    <div className="w-full min-w-0 border-l border-border pl-3 py-1">
      <div className="flex w-full min-w-0 items-start gap-2">
        <Bot className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
        <div className="min-w-0 flex-1 space-y-1">
          <div className="flex min-w-0 flex-wrap items-center gap-1.5">
            {hasChildEntries && (
              <button
                type="button"
                onClick={(event) => {
                  // 内联展开与「打开侧边栏」是互补的两条路径，互不触发；
                  // 两者已是兄弟原生 button，无外层 onKeyDown 冒泡，无需 stopPropagation 防键盘冲突。
                  event.stopPropagation();
                  setIsOpen((prev) => !prev);
                }}
                className="inline-flex shrink-0 items-center rounded p-0.5 text-muted-foreground transition-colors hover:bg-accent/40 hover:text-foreground"
                aria-label={isOpen ? "Collapse delegated child events" : "Expand delegated child events"}
                aria-expanded={isOpen}
              >
                <ChevronRight
                  className={cn("h-3.5 w-3.5 transition-transform", isOpen && "rotate-90")}
                />
              </button>
            )}
            {/* 打开侧边栏：显式原生 button，非嵌套于任何可点击容器，自带键盘可达。 */}
            <button
              type="button"
              disabled={!childTurnId}
              onClick={() => {
                if (childTurnId) {
                  selectChildTurn(childTurnId);
                }
              }}
              aria-current={isSelected ? "true" : undefined}
              aria-label={childTurnId ? `Open ${childAgentId} child timeline in side panel` : undefined}
              className={cn(
                "inline-flex min-w-0 flex-wrap items-center gap-1.5 rounded-sm text-left outline-none transition-colors",
                "focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1",
                "disabled:cursor-default",
                isSelected && "bg-accent/50 ring-1 ring-accent",
              )}
            >
              <Badge variant={variant} className="shrink-0 gap-1 px-2 py-0">
                <Icon className={cn("h-3 w-3", status === "running" && "animate-spin")} />
                {status}
              </Badge>
              {showConcurrency && (
                <Badge variant="secondary" className="shrink-0 gap-1 px-2 py-0">
                  {`并发 ${concurrencyIndex != null ? concurrencyIndex + 1 : "?"} / ${concurrencyGroupSize}`}
                </Badge>
              )}
              <span className={cn(Caption.mono, "min-w-0 break-all text-muted-foreground")}>
                {childAgentId}
              </span>
              <span className={cn(Caption.xs, "shrink-0 text-muted-foreground")}>
                {delegationType}
              </span>
            </button>
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

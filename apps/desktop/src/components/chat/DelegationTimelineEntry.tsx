import { memo, useMemo, useState } from "react";
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
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { Caption } from "@/components/ui/tokens";
import { cn } from "@/lib/utils";
import { DELEGATION_STATUS_UI, type TimelineDelegationStatus } from "@/services/timeline/projector";
import { useDelegationStore } from "@/stores/delegationStore";
import { buildMarkdownComponents } from "./AgentMessage";
import { MarkdownStream } from "./MarkdownStream";

/** Props for rendering a parent-turn delegation lifecycle entry. */
interface DelegationTimelineEntryProps {
  /** Child AgentProfile id selected for this delegation. */
  childAgentId: string;
  /** Current delegation lifecycle status. */
  status: TimelineDelegationStatus;
  /** Child turn id once the backend has created the child run (number 维度). */
  childTurnId?: number;
  /** Display-only delegation category. */
  delegationType: string;
  /** Successful terminal summary. */
  summary?: string;
  /** Failed or cancelled terminal reason. */
  error?: string;
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

// 状态 → 徽章 variant/label 收口于 projector.DELEGATION_STATUS_UI（单一事实来源，避免双份映射漂移）。
// 图标因依赖 lucide 组件、projector 不反向依赖 UI，故保留在本地映射，仅 Icon 维度未上移。
const STATUS_ICON: Record<TimelineDelegationStatus, typeof Clock> = {
  pending: Clock,
  running: Loader2,
  waiting_approval: Clock,
  completed: CheckCircle2,
  failed: XCircle,
  cancelled: Ban,
};

/**
 * 在父 turn timeline 内渲染一行 delegation 生命周期条目。
 *
 * 行为（subagent-timeline-refactor 方案）：
 * - 整行（图标 + 子 Agent 名 + 状态徽章 + 终态文字）作为单一点击目标，由行内原生
 *   `<button type="button" aria-current={isSelected}>`（自带 Enter/Space 键盘可达）承载：
 *   点击触发 `delegationStore.selectChildTurn(childTurnId)`（仅当 `childTurnId` 存在），
 *   右侧面板切到 Subagent Tab 渲染该 child turn 的完整 timeline。
 *   不退化成裸 `div+onClick`，以保留键盘可达性与 aria-current 选中态。
 * - 选中态视觉高亮：当 `delegationStore.selectedChildTurnId` 与当前 `childTurnId` 相等时，
 *   按钮加高亮边框（aria-current 同步）。
 * - 并发指示：当 `concurrencyGroupSize` 存在且 >= 2 时，在行内（状态徽章旁）渲染
 *   `并发 ${concurrencyIndex + 1} / ${concurrencyGroupSize}` 徽章，提示该 delegation 属于
 *   一个并发组（与同组的其它 delegation 行由 TurnTimeline 的泳道左边框在视觉上归组）。
 *   非并发（size < 2 或字段缺失）不渲染并发指示。
 * - 内联展开已移除：child 完整消息流只在 Subagent Tab 渲染，主 timeline 行只显示 title + 终态文字。
 * - 终态 `detail`（summary / error，后端为 markdown 文本）以 `Collapsible` **默认折叠**呈现，
 *   避免子 Agent 大篇幅输出撑爆主对话；展开后为 markdown 渲染（复用 `MarkdownStream`），
 *   失败/取消态以 destructive 色区分。折叠态仅留一行触发条，不渲染 markdown 内容。
 *
 * @param props - 委派元数据（子 Agent id、状态、child turn id、终态 summary/error、并发组信息）。
 * @returns timeline 行（含状态、子标识、可折叠 markdown 终态、并发指示；整行为键盘可达的派发按钮）。
 *
 * @throws 不主动抛出异常。
 *
 * @sideeffect 点击按钮调用 `delegationStore.selectChildTurn`，更新全局委派选中态（副作用跨组件）；
 *   折叠展开态由组件内 `open` 受控状态管理（默认 false）。
 */
export const DelegationTimelineEntry = memo(function DelegationTimelineEntry({
  childAgentId,
  status,
  childTurnId,
  delegationType,
  summary,
  error,
  concurrencyGroupSize,
  concurrencyIndex,
}: DelegationTimelineEntryProps) {
  const { variant } = DELEGATION_STATUS_UI[status];
  const Icon = STATUS_ICON[status];
  const detail = status === "failed" || status === "cancelled" ? error : summary;
  // 并发指示：仅当并发组规模 >= 2 时展示「并发 index+1/size」徽章（与同组泳道左边框互补）。
  const showConcurrency = concurrencyGroupSize != null && concurrencyGroupSize >= 2;

  // 选中态：读取委派选中 store，与当前 childTurnId 比对决定高亮（单一职责：仅 UI 选中态）。
  const selectedChildTurnId = useDelegationStore((state) => state.selectedChildTurnId);
  const selectChildTurn = useDelegationStore((state) => state.selectChildTurn);
  const isSelected = childTurnId != null && childTurnId === selectedChildTurnId;

  // 终态 detail 默认折叠：子 Agent 输出篇幅大，避免撑爆主对话；展开后渲染 markdown。
  const [detailOpen, setDetailOpen] = useState(false);
  const isFailure = status === "failed" || status === "cancelled";
  // 复用 AgentMessage 的 markdown 组件映射（CodeBlock/FileLink 富展示），定稿态非流式。
  const markdownComponents = useMemo(() => buildMarkdownComponents(false), []);

  return (
    <div className="w-full min-w-0 border-l border-border pl-3 py-1">
      <div className="flex w-full min-w-0 items-start gap-2">
        <Bot className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
        <div className="min-w-0 flex-1 space-y-1">
          <div className="flex min-w-0 flex-wrap items-center gap-1.5">
            {/* 整行点击派发：显式原生 button，自带键盘可达，不嵌套于其它可点击容器。 */}
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
            <Collapsible open={detailOpen} onOpenChange={setDetailOpen}>
              <CollapsibleTrigger
                className={cn(
                  "group inline-flex w-full min-w-0 items-center gap-1 rounded-sm text-left",
                  "text-xs text-muted-foreground outline-none",
                  "focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1",
                )}
                aria-label={isFailure ? "展开子 Agent 错误详情" : "展开子 Agent 结果"}
              >
                <ChevronRight
                  className={cn(
                    "h-3 w-3 shrink-0 transition-transform duration-150",
                    detailOpen && "rotate-90",
                  )}
                />
                <span className="truncate">{isFailure ? "查看子 Agent 错误详情" : "查看子 Agent 结果"}</span>
              </CollapsibleTrigger>
              <CollapsibleContent
                className={cn(
                  "min-w-0 overflow-hidden text-sm",
                  isFailure ? "text-destructive" : "text-foreground",
                )}
              >
                <MarkdownStream content={detail} components={markdownComponents} />
              </CollapsibleContent>
            </Collapsible>
          )}
        </div>
      </div>
    </div>
  );
});

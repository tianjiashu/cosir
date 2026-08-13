/**
 * 侧边栏子 Agent（child turn）timeline 展示面板。
 *
 * 单一职责：渲染「当前在委派选中 store 中被选中的 child turn」的完整 timeline。
 * 复用既有数据流，不新建 SSE / 投影器 / 投影 hook：
 * - child 事件已由 useDelegationStreams 按 turn_id 分片并入 eventStore
 *   （见 stores/eventStore.ts 的 eventsByTurnId），本组件直接读取该分片；
 * - 复用既有投影器 projectTurnTimeline(turns, events)、既有渲染组件 TurnTimeline
 *   渲染该项目 timeline（零重新实现）。
 *
 * 设计约束（来自 Task 1 brief A3）：
 * - 不新建 useChildTurnTimeline hook（A4 删除项）——直接复用 eventStore 分片。
 * - 构造单元素 TurnRecord[] 喂给 projectTurnTimeline；优先复用 turnStore 中真实
 *   TurnRecord，否则兜底构造空 record，避免捏造不存在的数据。
 *
 * @module components/right-panel/SubagentPanel
 */

import { useMemo } from "react";
import type { RuntimeEvent } from "@shared/events";
import type { TurnRecord } from "@shared/turn";
import { Badge } from "@/components/ui/badge";
import { Caption } from "@/components/ui/tokens";
import { cn } from "@/lib/utils";
import { TurnTimeline } from "@/components/layout/TurnTimeline";
import { projectTurnTimeline } from "@/services/timeline/projector";
import { useDelegationStore } from "@/stores/delegationStore";
import { useEventStore } from "@/stores/eventStore";
import { useTurnStore } from "@/stores/turnStore";

/** 稳定的空事件数组常量：避免 useEventStore 每次返回新 [] 引用触发无限重渲染。 */
const EMPTY_EVENTS: RuntimeEvent[] = [];

/** 描述委派生命周期的事件类型集合（用于从事件流派生 child 状态徽章）。 */
const DELEGATION_LIFECYCLE_EVENTS = new Set<RuntimeEvent["event_type"]>([
  "delegation_child_started",
  "delegation_finished",
  "delegation_failed",
  "delegation_cancelled",
]);

/** 委派状态到中文徽章文案与变体的映射。 */
const DELEGATION_STATUS_BADGE: Record<
  string,
  { label: string; variant: "outline" | "success" | "destructive" | "warning" | "secondary" }
> = {
  running: { label: "运行中", variant: "secondary" },
  completed: { label: "已完成", variant: "success" },
  failed: { label: "失败", variant: "destructive" },
  cancelled: { label: "已取消", variant: "outline" },
};

/**
 * 从扁平事件流中派生指定 child turn 的委派终态状态。
 *
 * delegation 生命周期事件的 `turn_id` 属于父 turn，但其 `payload.child_turn_id`
 * 指向真实 child turn，故按 payload 反查。取 sequence 最大的有效事件作为当前状态。
 *
 * @param childTurnId - 待查询的 child turn 标识。
 * @param events - 扁平事件流（来自 eventStore.events）。
 * @returns 派生状态 key（running/completed/failed/cancelled）或 undefined（尚无委派事件）。
 *
 * @throws 不抛出异常；payload 字段缺失或类型异常时安全跳过。
 *
 * @sideeffect 无。
 */
function deriveDelegationStatus(
  childTurnId: string,
  events: RuntimeEvent[],
): string | undefined {
  let best: { sequence: number; status: string } | undefined;
  for (const event of events) {
    if (!DELEGATION_LIFECYCLE_EVENTS.has(event.event_type)) continue;
    const payload = event.payload as { child_turn_id?: string; status?: unknown };
    if (payload.child_turn_id !== childTurnId) continue;
    const status = normalizeDelegationStatus(payload.status, event.event_type);
    if (!status) continue;
    const sequence = Number(event.sequence || 0);
    if (!best || sequence >= best.sequence) {
      best = { sequence, status };
    }
  }
  return best?.status;
}

/**
 * 将委派状态（事件 payload 或事件类型推导）归一化为统一 key。
 *
 * 与投影器 projectDelegation 的归一逻辑保持口径一致：终态事件直接给终态，
 * delegation_child_started 视为运行中。
 *
 * @param status - 事件 payload 中的 status（可能为任意类型）。
 * @param eventType - 事件类型，用于无显式 status 时兜底推导。
 * @returns 归一化状态 key，或 undefined（无法识别）。
 *
 * @throws 不抛出异常。
 *
 * @sideeffect 无。
 */
function normalizeDelegationStatus(status: unknown, eventType: RuntimeEvent["event_type"]): string | undefined {
  if (typeof status === "string" && status.length > 0) {
    if (["running", "completed", "failed", "cancelled", "pending", "waiting_approval"].includes(status)) {
      return status;
    }
    return undefined;
  }
  if (eventType === "delegation_child_started") return "running";
  if (eventType === "delegation_finished") return "completed";
  if (eventType === "delegation_failed") return "failed";
  if (eventType === "delegation_cancelled") return "cancelled";
  return undefined;
}

/**
 * 侧边栏子 Agent 面板。
 *
 * 订阅 delegationStore.selectedChildTurnId：
 * - 未选中：显示提示空态。
 * - 选中但对应 child 事件尚未到达：显示 loading 占位（框架已就绪，等待数据流填充）。
 * - 选中且事件已到：顶部展示 child turn 元信息（id + 委派状态徽章），
 *   下方复用 TurnTimeline 渲染该 child turn 的完整 timeline。
 *
 * @returns 右侧面板中展示选中 child turn 的区块；属于 RightPanel 的 SourcesTab 子树。
 *
 * @throws 不主动抛出异常。
 *
 * @sideeffect 仅读取 store（eventStore/turnStore/delegationStore），不写入、不发起网络。
 */
export function SubagentPanel() {
  const selectedChildTurnId = useDelegationStore((state) => state.selectedChildTurnId);
  // 订阅分片与扁平事件：child 事件到达即触发重渲染，无需额外 revision 信号。
  const childEvents = useEventStore((state) =>
    selectedChildTurnId ? (state.eventsByTurnId[selectedChildTurnId] ?? EMPTY_EVENTS) : EMPTY_EVENTS,
  );
  const allEvents = useEventStore((state) => state.events);
  const turnsByTaskId = useTurnStore((state) => state.turnsByTaskId);

  const turnRecord = useMemo<TurnRecord>(() => {
    if (!selectedChildTurnId) return createFallbackTurn("");
    // 优先在 turnStore 中查找真实 TurnRecord，避免捏造不存在的数据。
    for (const turns of Object.values(turnsByTaskId)) {
      const match = turns.find((turn) => turn.turn_id === selectedChildTurnId);
      if (match) return match;
    }
    return createFallbackTurn(selectedChildTurnId);
  }, [selectedChildTurnId, turnsByTaskId]);

  const delegationStatus = useMemo(
    () => (selectedChildTurnId ? deriveDelegationStatus(selectedChildTurnId, allEvents) : undefined),
    [selectedChildTurnId, allEvents],
  );

  // 选中态下的派生渲染数据（仅在已选中时计算，减少无谓投影）。
  const childTimelineItem = useMemo(() => {
    if (!selectedChildTurnId) return null;
    const items = projectTurnTimeline([turnRecord], childEvents);
    return items[0] ?? null;
  }, [selectedChildTurnId, turnRecord, childEvents]);

  if (!selectedChildTurnId) {
    return (
      <div className="flex items-center gap-2 rounded-md border border-dashed border-border px-3 py-2 opacity-60">
        <div>
          <p className="text-xs font-medium">Subagent</p>
          <p className="text-xs text-muted-foreground">点击对话中的委派行以查看子 Agent 时间线</p>
        </div>
      </div>
    );
  }

  const statusBadge = delegationStatus ? DELEGATION_STATUS_BADGE[delegationStatus] : undefined;

  return (
    <div className="space-y-2 rounded-md border border-border p-2">
      {/* 元信息头部：child turn id + 委派状态徽章 */}
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <span className="text-xs font-medium">Subagent</span>
        {statusBadge ? (
          <Badge variant={statusBadge.variant} className="shrink-0 gap-1 px-2 py-0">
            {statusBadge.label}
          </Badge>
        ) : (
          <Badge variant="outline" className="shrink-0 gap-1 px-2 py-0">
            状态未知
          </Badge>
        )}
        <span className={cn(Caption.mono, "min-w-0 break-all text-xs text-muted-foreground")}>
          {selectedChildTurnId}
        </span>
      </div>

      {/* timeline 区域：事件未到时显示 loading，已到则复用 TurnTimeline 渲染 */}
      {childEvents.length === 0 ? (
        <p className="text-xs text-muted-foreground">正在等待子 Agent 事件流…</p>
      ) : childTimelineItem ? (
        <TurnTimeline turn={turnRecord} events={childEvents} />
      ) : (
        <p className="text-xs text-muted-foreground">该子 Agent 暂无可渲染的时间线条目。</p>
      )}
    </div>
  );
}

/**
 * 构造兜底的空 TurnRecord（仅在 turnStore 无真实记录时使用）。
 *
 * 仅填充业务必需的 turn_id；input/response 留空，由 projectTurnTimeline 在
 * 无条目且有 response_text 时兜底渲染，保证不捏造不存在的内容。
 *
 * @param turnId - child turn 标识。
 * @returns 最小可用的 TurnRecord。
 */
function createFallbackTurn(turnId: string): TurnRecord {
  return {
    turn_id: turnId,
    task_id: "",
    input_text: "",
    status: "completed",
    end_reason: null,
    response_text: null,
    created_at: "",
    updated_at: "",
  };
}

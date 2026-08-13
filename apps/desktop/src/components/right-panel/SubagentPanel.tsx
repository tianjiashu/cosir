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
import type { TurnRecord } from "@shared/turn";
import { Badge } from "@/components/ui/badge";
import { Caption } from "@/components/ui/tokens";
import { cn } from "@/lib/utils";
import { TurnTimeline } from "@/components/layout/TurnTimeline";
import {
  deriveChildDelegationStatus,
  projectTurnTimeline,
  type TimelineDelegationStatus,
} from "@/services/timeline/projector";
import { useDelegationStore } from "@/stores/delegationStore";
import { EMPTY_EVENTS, useEventStore } from "@/stores/eventStore";
import { useTurnStore } from "@/stores/turnStore";

/** 委派状态到中文徽章文案与变体的映射（穷尽 TimelineDelegationStatus 全部取值）。 */
const DELEGATION_STATUS_BADGE: Record<
  TimelineDelegationStatus,
  { label: string; variant: "outline" | "success" | "destructive" | "warning" | "secondary" }
> = {
  pending: { label: "等待中", variant: "outline" },
  waiting_approval: { label: "待审批", variant: "warning" },
  running: { label: "运行中", variant: "secondary" },
  completed: { label: "已完成", variant: "success" },
  failed: { label: "失败", variant: "destructive" },
  cancelled: { label: "已取消", variant: "outline" },
};

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

  // 选中态下从事件流派生 child 委派状态，供徽章与兜底 record 的 status 映射共用，
  // 保证「左侧状态」与「右侧 timeline 口径」同源（复用投影器 deriveChildDelegationStatus）。
  const delegationStatus = useMemo(
    () => (selectedChildTurnId ? deriveChildDelegationStatus(selectedChildTurnId, allEvents) : undefined),
    [selectedChildTurnId, allEvents],
  );

  // 未选中时返回 null（类型 TurnRecord | null），避免无谓构造兜底 record；
  // 下方消费点加 null 守卫。
  const turnRecord = useMemo<TurnRecord | null>(() => {
    if (!selectedChildTurnId) return null;
    // 优先在 turnStore 中查找真实 TurnRecord，避免捏造不存在的数据。
    for (const turns of Object.values(turnsByTaskId)) {
      const match = turns.find((turn) => turn.turn_id === selectedChildTurnId);
      if (match) return match;
    }
    // 无真实记录时构造兜底 record，status 据派生状态映射（详见 createFallbackTurn docstring）。
    return createFallbackTurn(selectedChildTurnId, delegationStatus);
  }, [selectedChildTurnId, turnsByTaskId, delegationStatus]);

  // 选中态下的派生渲染数据（仅在已选中且已取到 turnRecord 时计算，减少无谓投影）。
  const childTimelineItem = useMemo(() => {
    if (!selectedChildTurnId || !turnRecord) return null;
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
      ) : turnRecord && childTimelineItem ? (
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
 * `status` 取值逻辑（正确性优先，不再写死 "completed"）：
 * - 派生状态为 "running" → "running"；
 * - 派生状态为 "completed"/"failed"/"cancelled" → 同名（与派生口径一致）；
 * - 派生状态为 "pending"/"waiting_approval"/undefined → "pending"（尚无明确运行信号）。
 * 该字段被下游 `TurnTimeline` 用于判定 `isTurnActive`（取 "pending"||"running" 为活跃态）：
 * 写死 "completed" 会让「正在运行但 turnStore 尚未落库」的 child 被误判为终态，导致
 * pending/思考块被提前折叠、看不到实时进行态；改用派生映射后，进行态 child 以 streaming
 * 口径展开，贴合 brief A3「选中但事件未到时显示 loading/进行态」意图。
 *
 * @param turnId - child turn 标识。
 * @param derived - 从该 child 事件流派生的委派状态（可为 undefined）。
 * @returns 最小可用的 TurnRecord，其 status 按派生状态映射（见上方取值逻辑）。
 */
function createFallbackTurn(turnId: string, derived: TimelineDelegationStatus | undefined): TurnRecord {
  const status: TurnRecord["status"] =
    derived === "running" ||
    derived === "completed" ||
    derived === "failed" ||
    derived === "cancelled"
      ? derived
      : "pending";
  return {
    turn_id: turnId,
    task_id: "",
    input_text: "",
    status,
    end_reason: null,
    response_text: null,
    created_at: "",
    updated_at: "",
  };
}

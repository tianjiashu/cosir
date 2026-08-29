/**
 * 侧边栏子 Agent（child turn）timeline 展示面板。
 *
 * 单一职责：渲染「当前在委派选中 store 中被选中的 child turn」的完整 timeline。
 * 复用既有数据流，不新建 SSE / 投影器 / 投影 hook：
 * - child 事件已由 useDelegationStreams 按 turn_id 分片并入 eventStore
 *   （见 stores/eventStore.ts 的 eventsByTurnId），本组件直接读取该分片；
 * - child timeline 直接复用既有渲染组件 TurnTimeline（其自身已是增量投影实现，
 *   projectTimelineIncrementally + stateRef + delta 切片），不再经 projectTurnTimeline
 *   全量投影（零重新实现，且避免投影结果被丢弃的死代码）。
 *
 * 设计约束（来自 Task 1 brief A3）：
 * - 不新建 useChildTurnTimeline hook（A4 删除项）——直接复用 eventStore 分片。
 * - 优先复用 turnStore 中真实 TurnRecord，否则兜底构造空 record，避免捏造不存在的数据；
 *   child timeline 直接以该 record 喂给 TurnTimeline 渲染（不再经 projectTurnTimeline 全量投影）。
 *
 * @module components/right-panel/SubagentPanel
 */

import { useDeferredValue, useMemo } from "react";
import type { TurnRecord } from "@shared/turn";
import { Badge } from "@/components/ui/badge";
import { Caption } from "@/components/ui/tokens";
import { cn } from "@/lib/utils";
import { TurnTimeline } from "@/components/layout/TurnTimeline";
import {
  DELEGATION_STATUS_UI,
  deriveChildDelegationStatus,
  deriveSiblingDelegations,
  type TimelineDelegationStatus,
} from "@/services/timeline/projector";
import { useDelegationStore } from "@/stores/delegationStore";
import { EMPTY_EVENTS, useEventStore } from "@/stores/eventStore";
import { useTurnStore } from "@/stores/turnStore";
import { logWarn } from "@/lib/logger";

// 委派状态 → 徽章 UI 的单一映射收口于 projector.DELEGATION_STATUS_UI，
// 本组件直接引用 projector 的 DELEGATION_STATUS_UI 单一映射，不再各自维护一份
// （避免双份穷尽映射漂移，见问题 7 审查结论）。

/**
 * 侧边栏子 Agent 面板。
 *
 * 订阅 delegationStore.selectedChildTurnId：
 * - 未选中：显示提示空态。
 * - 选中但对应 child 事件尚未到达：显示 loading 占位（框架已就绪，等待数据流填充）。
 * - 选中且事件已到：顶部展示 child turn 元信息（id + 委派状态徽章），
 *   下方复用 TurnTimeline 渲染该 child turn 的完整 timeline。
 * - 并发切换：当前选中的 child 属于某并发组（同 parent turn 下 >= 2 个 running 的 delegation）
 *   时，面板顶部以 tab 列出全部 sibling 子 Agent（各自状态徽章），点击 tab 调
 *   `delegationStore.selectChildTurn` 切换选中（复用 Task 1 的选中态通道），下方随之渲染对应 child。
 *
 * 性能降频（修复 2）：sibling/status 这两个对全量事件的 O(N) 派生使用
 * `useDeferredValue` 包裹的 deferred 全量事件，使它们在高频事件流（每帧一次 set）
 * 下延后到低优先级渲染空闲时才重算，避免每次 store 变更都立即重跑派生扫描。
 * 注意：child timeline 维度（childEvents）刻意不使用 deferred，以保证选中 child
 * 事件流的实时响应，不延迟。
 *
 * 双层数据契约（重要，避免后续误改）：
 * - timeline 渲染维度：严格按 child 分片（`eventStore.eventsByTurnId[selectedChildTurnId]`），
 *   只取该 child 的事件，与主 timeline 及 sibling 完全隔离，杜绝跨 child 数据泄漏。
 * - sibling/status 派生维度：必须跨 child 聚合（同 parent 下全部 delegation），因此
 *   显式读取 `eventStore.events`（扁平全量事件）喂给 `deriveSiblingDelegations` /
 *   `deriveChildDelegationStatus`，而非 child 分片——分片拿不到 sibling 数据，故此处
 *   用全局事件是设计使然，非「混用数据源」。两层口径不同但各自闭环，请勿强行统一。
 *
 * @returns 右侧面板中展示选中 child turn 的区块；作为 RightPanel 的独立 Subagent Tab 渲染（不再嵌套于 SourcesTab 子树）。
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
  // 全量事件订阅：先取原始引用，再用 useDeferredValue 派生低优先级副本。
  // 高频事件流（每帧一次 set）下，deferred 值会在渲染空闲时才更新，
  // 使下方 sibling/status 两个 O(N) 派生降频重跑（详见组件 docstring 性能降频段）。
  // 注意：childEvents 分片刻意不用 deferred，需实时响应选中 child 事件流。
  const rawAllEvents = useEventStore((state) => state.events);
  const allEvents = useDeferredValue(rawAllEvents);
  const turnsByTaskId = useTurnStore((state) => state.turnsByTaskId);
  const selectChildTurn = useDelegationStore((state) => state.selectChildTurn);

  // 并发 sibling 派生：消费 deferred 全量事件（allEvents），依赖其为低优先级值，
  // store 高频变更时此 O(N) 扫描延后到渲染空闲才重跑（性能降频，不手写节流）。
  // 从扁平事件流派生当前选中 child 所属并发组的全部 sibling；
  // 仅长度 >= 2 时面板渲染 tab（纯前端推导，复用投影器 deriveSiblingDelegations，不重复造轮子）。
  // 并发 sibling 列表（同 parent 下全部 delegation，含已终态项，便于历史切换查看）。
  // 注意口径差异：此处列出「同 parent 全量 delegation」，而 timeline 行左侧泳道
  // （TurnTimeline 的 concurrencyGroupSize）标注的是「当前仍 running 的并发组」，
  // 两者刻意不同——已结束的并发仍可在 tab 间切换查看，但不再以泳道归组。
  const siblings = useMemo(
    () => (selectedChildTurnId ? deriveSiblingDelegations(allEvents, selectedChildTurnId) : []),
    [selectedChildTurnId, allEvents],
  );

  // 选中态下从事件流派生 child 委派状态：同样消费 deferred 全量事件，随其低优先级更新，
  // 避免每帧对全量事件重跑 O(N) 状态扫描（性能降频，依赖数组仍为 [selectedChildTurnId, allEvents]）。
  // 供徽章与兜底 record 的 status 映射共用，
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
    // 降级日志：turnStore 未落库即进入兜底，属数据流分片异常分支，需可排查线索
    // （child 事件已到但 turn 记录缺失，可能是 useDelegationStreams 落库延迟或丢事件）。
    logWarn("subagent panel falls back to synthetic turn record", {
      module: "SubagentPanel",
      child_turn_id: selectedChildTurnId,
      derived_status: delegationStatus ?? "unknown",
      has_child_events: childEvents.length > 0,
    });
    return createFallbackTurn(selectedChildTurnId, delegationStatus);
  }, [selectedChildTurnId, turnsByTaskId, delegationStatus]);

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

  const statusBadge = delegationStatus ? DELEGATION_STATUS_UI[delegationStatus] : undefined;

  return (
    <div className="space-y-2 rounded-md border border-border p-2">
      {/* 并发 tab：仅当当前选中 child 属于并发组（siblings.length >= 2）时渲染，
          每个 tab 显示 childAgentId + 状态徽章，点击切换选中（aria-current 标记当前项）。 */}
      {siblings.length >= 2 && (
        <div className="flex min-w-0 flex-wrap gap-1.5" role="tablist" aria-label="并发子 Agent 切换">
          {siblings.map((sibling) => {
            const badge = DELEGATION_STATUS_UI[sibling.status];
            const isCurrent = sibling.childTurnId === selectedChildTurnId;
            return (
              <button
                key={sibling.childTurnId}
                type="button"
                role="tab"
                aria-current={isCurrent ? "true" : undefined}
                onClick={() => selectChildTurn(sibling.childTurnId)}
                className={cn(
                  "inline-flex min-w-0 shrink-0 items-center gap-1.5 rounded-sm px-2 py-0.5 text-xs outline-none transition-colors",
                  "focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1",
                  isCurrent ? "bg-accent ring-1 ring-accent" : "hover:bg-accent/40",
                )}
              >
                <span className={cn(Caption.mono, "min-w-0 break-all")}>{sibling.childAgentId}</span>
                <Badge variant={badge.variant} className="shrink-0 gap-1 px-2 py-0">
                  {badge.label}
                </Badge>
              </button>
            );
          })}
        </div>
      )}

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
      ) : turnRecord && childEvents.length > 0 ? (
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
 * 仅填充业务必需的 turn_id；input/response 留空，由 TurnTimeline 在
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
 * @param turnId - child turn 标识（真实后端 turn 主键，number 维度）。
 * @param derived - 从该 child 事件流派生的委派状态（可为 undefined）。
 * @returns 最小可用的 TurnRecord，其 status 按派生状态映射（见上方取值逻辑）。
 */
function createFallbackTurn(turnId: number, derived: TimelineDelegationStatus | undefined): TurnRecord {
  const status: TurnRecord["status"] =
    derived === "running" ||
    derived === "completed" ||
    derived === "failed" ||
    derived === "cancelled"
      ? derived
      : "pending";
  return {
    turn_id: turnId,
    // task_id 为 number 占位：兜底 record 仅用于分片渲染，不回写 store/后端，
    // 无真实 task 归属时落 0（与 TurnRecord.task_id:number 对齐；原 string 占位编译不过）。
    task_id: 0,
    input_text: "",
    status,
    end_reason: null,
    response_text: null,
    created_at: "",
    updated_at: "",
  };
}

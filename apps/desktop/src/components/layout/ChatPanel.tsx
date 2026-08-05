/**
 * 中央主会话区（ChatPanel）。
 *
 * 展示当前任务下的 turn timeline：用户输入来自 turnStore，
 * assistant/tool/status 项由 timeline projector 按 runtime event 顺序投影。
 *
 * @module components/layout/ChatPanel
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useShallow } from "zustand/react/shallow";
import { FolderOpen } from "lucide-react";
import { Button } from "@/components/ui/button";
import { VirtualList } from "@/lib/virtual/VirtualList";
import { useEventStore, selectLatestEvent, selectEventsForTask, EMPTY_EVENTS } from "@/stores/eventStore";
import { useTaskStore, selectActiveTask } from "@/stores/taskStore";
import { useTurnStore } from "@/stores/turnStore";
import { useWorkspaceStore } from "@/stores/workspaceStore";
import { TurnTimeline } from "@/components/layout/TurnTimeline";
import type { TurnRecord } from "@shared/turn";

/**
 * 判断当前是否无活跃任务（即空会话）。
 *
 * 真实语义仅取决于是否存在活跃任务；空状态是否展示由 timelineTurns 是否为空决定，
 * 不由事件计数驱动。函数名如实反映行为，避免误导后续维护者。
 *
 * @param activeTask - 当前活跃任务（可能为 null）。
 * @returns 无活跃任务时返回 true。
 */
function isNoActiveTask(activeTask: ReturnType<typeof selectActiveTask>): boolean {
  return !activeTask;
}

/**
 * 首屏渲染的 turn 数量上限（分片窗口）。
 *
 * 历史对话可能包含大量 turn，一次性投影全部会阻塞首屏；仅渲染最近若干个 turn，
 * 更早的由「加载更早对话」入口按需扩展，使首屏渲染代价与总 turn 数解耦。
 */
const INITIAL_TURN_COUNT = 20;

/**
 * 单次「加载更早对话」扩展的 turn 数量。
 */
const LOAD_MORE_TURN_COUNT = 20;

/** ChatPanel 组件属性。 */
export interface ChatPanelProps {
  /** 点击「选择工作区」引导按钮后的跳转回调。 */
  onPickWorkspace: () => void;
}

/**
 * ChatPanel 中央主会话区组件。
 *
 * 消息列表区域由 VirtualList 虚拟化渲染（仅挂载视口内 turn），
 * 新消息通过滚动容器 scrollTo 到底部（节流 200ms）。
 *
 * 当不存在活跃工作区时（例如删除了唯一工作区）展示引导空状态，
 * 提示用户先选择或创建项目目录，避免中间区域完全空白。
 *
 * 数据来源：
 * - 用户消息：turnStore 中的 turn.input_text
 * - Agent 输出、工具、状态：timeline projector 按事件顺序投影后的显示项
 */
export function ChatPanel({ onPickWorkspace }: ChatPanelProps) {
  const activeTaskId = useTaskStore((s) => s.activeTaskId);
  // 按 turn 分片的事件映射：每个 turn 的数组引用在其它 turn 收事件时保持不变，
  // 使 <TurnTimeline> 的 memo 能精确跳过未变化的 turn，只重渲染活跃 turn。
  const eventsByTurnId = useEventStore(useShallow((s) => s.eventsByTurnId));
  const events = useEventStore(useShallow((s) => selectEventsForTask(s, activeTaskId)));
  const latestEvent = useEventStore((s) => selectLatestEvent(s, activeTaskId));
  const activeTask = useTaskStore(selectActiveTask);
  const activeWorkspaceId = useWorkspaceStore((s) => s.activeWorkspaceId);
  const turns = useTurnStore(useShallow((s) => (activeTask ? s.turnsByTaskId[activeTask.task_id] ?? [] : [])));
  const scrollContainerRef = useRef<HTMLDivElement>(null);

  // 无任何工作区时的引导空状态（先于「无活跃任务」判断，覆盖删完所有工作区的场景）。
  const noWorkspace = !activeWorkspaceId;

  const timelineTurns = useMemo(() => {
    if (turns.length > 0) {
      return turns;
    }
    if (activeTask) {
      const fallbackTurn: TurnRecord = {
        turn_id: activeTask.latest_turn_id ?? activeTask.task_id,
        task_id: activeTask.task_id,
        input_text: activeTask.input_text,
        status: "pending",
        end_reason: null,
        response_text: null,
        created_at: activeTask.created_at,
        updated_at: activeTask.updated_at,
      };
      return [fallbackTurn];
    }
    return [];
  }, [activeTask, turns]);

  // 分片窗口：首屏仅渲染最近若干 turn，更早的由「加载更早对话」按需扩展。
  // 仅当切换任务（activeTaskId 变化）时重置窗口；流式期间 turns 引用变化（新事件到达）
  // 不应把已展开的窗口打回首屏大小，否则用户展开的更早对话会突然消失。
  const [visibleTurnCount, setVisibleTurnCount] = useState(INITIAL_TURN_COUNT);
  useEffect(() => {
    setVisibleTurnCount(INITIAL_TURN_COUNT);
  }, [activeTaskId]);

  // 首屏只取最近 visibleTurnCount 个 turn；更早的以折叠入口呈现。
  const visibleTurns = useMemo(
    () => timelineTurns.slice(-visibleTurnCount),
    [timelineTurns, visibleTurnCount],
  );
  const hasEarlierTurns = timelineTurns.length > visibleTurnCount;

  // 单个 turn 渲染：外层包 px-4 py-2 承托内边距与条目垂直节奏（VirtualList 绝对定位条目，
  // 不受 space-y 影响）。
  // 关键性能点：renderItem 必须保持引用稳定——VirtualList 每次重渲染都会对视口内条目调用
  // renderItem，若其引用随 store 更新而频繁变化，会导致整个视口重新协调、击穿 TurnTimeline
  // 的 memo。这里用 ref 持有最新 eventsByTurnId（每次 ChatPanel 渲染即刷新），
  // renderItem 本身用 useCallback([]) 锁定引用；TurnTimeline 仍按各自 events 引用的变化
  // 决定是否重渲染（由 eventStore.mergeByEventId 的引用稳定性保证），无需在此感知全局映射。
  const eventsByTurnIdRef = useRef(eventsByTurnId);
  eventsByTurnIdRef.current = eventsByTurnId;
  const renderTurnItem = useCallback((turn: TurnRecord) => {
    const turnEvents = eventsByTurnIdRef.current[turn.turn_id] ?? EMPTY_EVENTS;
    return (
      <div className="px-4 py-2">
        <TurnTimeline turn={turn} events={turnEvents} />
      </div>
    );
  }, []);

  // 是否显示空状态（仅取决于是否有活跃任务；具体空态由 timelineTurns 决定）
  const emptySession = isNoActiveTask(activeTask);

  // 当有新事件或新消息时自动滚动到底部。
  // 节流：流式期间事件高频到达，若每次都触发 smooth 滚动动画会导致大量重排重绘而卡顿。
  // 用 rAF + 节流（每 200ms 至多滚动一次），保证跟随最新内容的同时不阻塞渲染。
  // 虚拟列表下改用滚动容器 scrollTo 到底（锚点 div 在虚拟列表中不保证挂载）。
  const lastScrollAt = useRef(0);
  useEffect(() => {
    const now = Date.now();
    if (now - lastScrollAt.current < 200) {
      return;
    }
    lastScrollAt.current = now;
    const el = scrollContainerRef.current;
    if (el) {
      el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
    }
  }, [events.length, latestEvent]);

  return (
    <main className="flex flex-1 flex-col overflow-hidden bg-background">
      {/* 无工作区引导：删除所有工作区后回到会话页时的防御性空状态 */}
      {noWorkspace && (
        <div
          data-testid="workspace-guide"
          className="flex flex-col items-center justify-center gap-4 py-24 text-center"
        >
          <p className="text-sm text-muted-foreground">
            当前没有可用的工作区。工作区是 Agent 读取和修改代码的项目根目录，请先选择或创建一个。
          </p>
          <Button variant="outline" size="sm" onClick={onPickWorkspace} className="gap-2">
            <FolderOpen className="h-4 w-4" />
            选择工作区
          </Button>
        </div>
      )}

      {/* 空会话提示（已有工作区但无活跃任务） */}
      {!noWorkspace && emptySession && (
        <div className="flex items-center justify-center py-20 text-sm text-muted-foreground">
          在下方输入框发送指令开始对话
        </div>
      )}

      {/* 更早的历史对话折叠入口：点击后向窗口扩展，避免一次性投影全部 turn */}
      {hasEarlierTurns && (
        <div className="flex justify-center py-2">
          <Button
            variant="ghost"
            size="sm"
            className="text-xs text-muted-foreground"
            onClick={() => setVisibleTurnCount((count) => count + LOAD_MORE_TURN_COUNT)}
            data-testid="load-earlier-turns"
          >
            加载更早的 {Math.min(LOAD_MORE_TURN_COUNT, timelineTurns.length - visibleTurnCount)} 条对话
          </Button>
        </div>
      )}

      {/* 每个 turn 独立投影 + memo：只有收事件的活跃 turn 重渲染，历史 turn 整块跳过。
          长会话通过 VirtualList 仅渲染视口内 turn，与既有时序窗口（maxTurns）协同控制渲染代价。
          renderItem 外包 px-4 py-2 承担条目内边距与垂直节奏，补偿原滚动容器内 mx-auto/space-y
          在虚拟列表下丢失的布局（虚拟条目绝对定位，不受父容器 space-y 影响）。 */}
      {!noWorkspace && !emptySession && (
        <VirtualList
          items={visibleTurns}
          getKey={(turn) => turn.turn_id}
          renderItem={renderTurnItem}
          scrollContainerRef={scrollContainerRef}
          className="min-w-0 flex-1 scrollbar-thin py-6"
          estimateSize={240}
          overscan={4}
        />
      )}
    </main>
  );
}

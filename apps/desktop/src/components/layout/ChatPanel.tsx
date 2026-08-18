/**
 * 中央主会话区（ChatPanel）。
 *
 * 展示当前任务下的 turn timeline：用户输入来自 turnStore，
 * assistant/tool/status 项由 timeline projector 按 runtime event 顺序投影。
 *
 * 顶部内嵌 TaskHeaderBar：与 NewTaskPage 共享同一选择条，强关联「正在聊的
 * task 用什么配置」。ProviderSettingsDialog 的开关状态默认由该 bar 自治持有；
 * 也可由宿主（App）受控注入 settingsOpen/onSettingsOpenChange，用于
 * InputBar 发送拦截（guardSend openSettings=true）联动打开配置中心。
 *
 * @module components/layout/ChatPanel
 */

import { useCallback, useEffect, useMemo, useRef, useState, type UIEvent } from "react";
import { useShallow } from "zustand/react/shallow";
import { FolderOpen } from "lucide-react";
import { Button } from "@/components/ui/button";
import { VirtualList } from "@/lib/virtual/VirtualList";
import { useEventStore, selectLatestEvent, selectEventsForTask, EMPTY_EVENTS } from "@/stores/eventStore";
import { useTaskStore, selectActiveTask } from "@/stores/taskStore";
import { useTurnStore } from "@/stores/turnStore";
import { useWorkspaceStore } from "@/stores/workspaceStore";
import { PerfTrace } from "@/lib/perf";
import { TurnTimeline } from "@/components/layout/TurnTimeline";
import { TaskHeaderBar } from "@/components/chat/TaskHeaderBar";
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

/**
 * 「用户正在底部」判定阈值（px）：滚动位置距底不超过该值即视为正在跟随最新内容，
 * 新事件到达时才允许自动滚底；超过该值说明用户已上滚阅读历史，不得打断。
 */
const NEAR_BOTTOM_THRESHOLD_PX = 80;

/** ChatPanel 组件属性。 */
export interface ChatPanelProps {
  /** 点击「选择工作区」引导按钮后的跳转回调。 */
  onPickWorkspace: () => void;
  /** 可选：厂商配置中心对话框开关（受控模式，未提供时 TaskHeaderBar 自治）。 */
  settingsOpen?: boolean;
  /** 可选：厂商配置中心开关变化回调（受控模式）。 */
  onSettingsOpenChange?: (open: boolean) => void;
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
export function ChatPanel({ onPickWorkspace, settingsOpen, onSettingsOpenChange }: ChatPanelProps) {
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
  // 用户是否正处于（或接近）底部：仅此时新事件到达才自动滚底；
  // 用户上滚阅读历史时为 false，流式 delta 不得把视口拽回底部。
  const isNearBottomRef = useRef(true);
  const renderCountRef = useRef(0);
  const prevEventsRef = useRef<unknown>(events);
  renderCountRef.current += 1;
  const eventsRefChanged = prevEventsRef.current !== events;
  prevEventsRef.current = events;

  // childTurnIds 增量缓存：delegation 子 turn 集合只在「新增 delegation_child_started
  // 事件」时变化，普通 delta 帧不影响它。但 SSE 每帧都让 events 引用变化，若每次都
  // 全量扫描 events 重建 Set，长会话（数千事件）下是真实的每帧 O(n) 开销。
  // 这里用 ref 持有上一次的 events 引用、已扫描长度与已算出的 Set：每帧只扫描
  // 「本次新增的事件尾部切片」（通常仅 1 条 delta），无新增 delegation 事件时直接复用
  // 旧 Set 引用（引用稳定 → 下游 timelineTurns memo 可精确跳过）。仅当 events 长度
  // 回退（切换任务 / invalidateTask 清空）时才全量重算，保证与 store 的 append-only
  // 语义一致。这样把「全量循环」降为「增量尾部扫描」，且不引入任何新依赖。
  const childTurnIdsCacheRef = useRef<{
    eventsRef: unknown;
    scannedLen: number;
    set: Set<string>;
  }>({ eventsRef: null, scannedLen: 0, set: new Set<string>() });
  // 渲染打点（采样）：用于排查「进入新 turn 后 ChatPanel 是否每帧重渲染爆炸」。
  // 仅当 events 引用真正变化时才打，避免纯内部 state 触发的冗余渲染刷屏淹没关键日志；
  // events 每帧变化正是要诊断的「渲染风暴」信号，采样后既保留信号又不淹没日志。
  if (eventsRefChanged) {
    PerfTrace.markCurrent("chatPanel:render", {
      render_seq: renderCountRef.current,
      events_changed: eventsRefChanged,
      task_id: activeTaskId,
    });
  }

  // 无任何工作区时的引导空状态（先于「无活跃任务」判断，覆盖删完所有工作区的场景）。
  const noWorkspace = !activeWorkspaceId;

  /**
   * 主 timeline 应渲染的 turn 列表。
   *
   * 数据来源优先级：turnStore 中的真实 turn 记录 > activeTask 兜底。
   *
   * 关键过滤：child turn（由 delegate_task 创建的子 Agent 轮次）必须从主 timeline
   * 中排除——它们的 timeline 步骤（工具调用/思考/Changes）应在右侧 SubagentPanel
   * 中独立渲染，不应混入主聊天区。识别依据：事件流中存在
   * `delegation_child_started` 事件且其 `payload.child_turn_id` 匹配该 turn 的 turn_id。
   *
   * 顺序收口：过滤后按 `created_at` 稳定排序。turnStore 的 upsertTurn/replaceTurnId
   * 用 push 假定「后到即后置」，但乐观临时 turn（本地时钟）与真实 turn（后端时钟）存在
   * 同秒/漂移窗口，push 顺序不等于时间序。在此收口使 VirtualList 渲染顺序只由时间决定，
   * 不依赖数组原序的巧合，修复「新一轮 turn 与上一轮消息重叠」的时序根因之一。
   *
   * 性能（M7 修复）：本 memo 依赖 `[activeTask, turns, childTurnIds]` 而非 `events`。
   * `childTurnIds` 本身由上方独立 memo 经增量缓存计算（仅扫描 events 新增尾部，
   * 普通 delta 帧复用旧 Set 引用），因此 SSE 每帧变化的是 `events` 引用而非
   * `childTurnIds`——普通流式帧不再触发本 memo 的「全量循环 + 排序」重算，长会话下
   * 彻底消除每帧 O(n) 开销。输出与改造前完全一致。
   */
  // 提取当前 task 的 delegation 子 turn id 集合（delegation_child_started 事件
  // 的 payload.child_turn_id）。该集合只在「新增 delegation 事件」时变化，普通
  // delta 帧不影响它。因此用增量缓存（childTurnIdsCacheRef）避免每帧全量扫描 events：
  // 每帧仅扫描本次新增的事件尾部；无新增 delegation 事件时复用旧 Set 引用，使下游
  // timelineTurns 的 memo 能精确跳过（不依赖 events 引用，从而切断每帧重算链路）。
  // 这是 M7 性能缺陷的修复点：原实现把「全量循环 events 建 childTurnIds」放在
  // 依赖 [events] 的 memo 内，导致长会话下每帧 O(n) 开销。
  const childTurnIds = useMemo<Set<string>>(() => {
    const cache = childTurnIdsCacheRef.current;
    const currentLen = events.length;

    // 1) 同一引用（非 SSE 帧触发）：直接复用已缓存集合，零扫描。
    if (cache.eventsRef === events) {
      return cache.set;
    }

    // 2) 长度回退（切换任务 / invalidateTask 清空）：全量重算后建立新引用。
    if (currentLen < cache.scannedLen) {
      const next = new Set<string>();
      for (const event of events) {
        if (event.event_type === "delegation_child_started") {
          const payload = event.payload as { child_turn_id?: string };
          if (payload.child_turn_id) {
            next.add(payload.child_turn_id);
          }
        }
      }
      childTurnIdsCacheRef.current = { eventsRef: events, scannedLen: currentLen, set: next };
      return next;
    }

    // 3) 增量帧：仅扫描本次新增的事件尾部 [scannedLen, currentLen)，复用旧集合内容。
    //    只在确实新增了 child_turn_id 时才新建 Set 引用，否则原样复用旧引用。
    let changed = false;
    const base = cache.set;
    for (let i = cache.scannedLen; i < currentLen; i++) {
      const event = events[i];
      if (event.event_type === "delegation_child_started") {
        const payload = event.payload as { child_turn_id?: string };
        if (payload.child_turn_id && !base.has(payload.child_turn_id)) {
          if (!changed) {
            changed = true;
          }
        }
      }
    }
    if (!changed) {
      // 无新增 delegation 子 turn：复用旧引用（下游 memo 可跳过）。
      childTurnIdsCacheRef.current = { eventsRef: events, scannedLen: currentLen, set: base };
      return base;
    }
    const next = new Set(base);
    for (let i = cache.scannedLen; i < currentLen; i++) {
      const event = events[i];
      if (event.event_type === "delegation_child_started") {
        const payload = event.payload as { child_turn_id?: string };
        if (payload.child_turn_id) {
          next.add(payload.child_turn_id);
        }
      }
    }
    childTurnIdsCacheRef.current = { eventsRef: events, scannedLen: currentLen, set: next };
    return next;
  }, [events]);

  const timelineTurns = useMemo(() => {
    // 此处不再依赖 events 引用——childTurnIds 已是「仅随 delegation 事件变化」的
    // 稳定集合（见上方 memo + 增量缓存）。这样普通 delta 帧（events 引用变化但无新
    // delegation）不会触发本 memo 重算，彻底切断每帧 O(n) 重建链路（M7 修复）。
    let candidateTurns: TurnRecord[];
    if (turns.length > 0) {
      candidateTurns = turns;
    } else if (activeTask) {
      const fallbackTurn: TurnRecord = {
        turn_id: activeTask.task_id,
        task_id: activeTask.task_id,
        input_text: "",
        status: "pending",
        end_reason: null,
        response_text: null,
        created_at: activeTask.created_at,
        updated_at: activeTask.updated_at,
      };
      candidateTurns = [fallbackTurn];
    } else {
      return [];
    }

    // 过滤掉 child turn：主 timeline 只展示父 Agent 的对话轮次，
    // child turn 由右侧 SubagentPanel 独立渲染（通过 delegationStore 选中态驱动）。
    const parentTurns = candidateTurns.filter((t) => !childTurnIds.has(t.turn_id));
    // 按 created_at 稳定排序：turnStore 的 upsertTurn/replaceTurnId 用 push 假定「后到即后置」，
    // 但乐观临时 turn（本地时钟）与真实 turn（后端时钟）存在同秒/漂移窗口，push 顺序不等于时间序。
    // 排序在此收口，使 VirtualList 渲染顺序只由时间决定，不依赖数组原序的巧合。
    // 用 Date.getTime() 比较而非 localeCompare：对「ISO 字符串 / 非规范格式 / undefined」均稳健，
    // 解析失败返回 NaN 经 `|| 0` 兜底排到尾部（而非 localeCompare 把 "" 排到头部），语义更明确。
    return parentTurns.slice().sort((a, b) => {
      const ta = new Date(a.created_at ?? 0).getTime() || 0;
      const tb = new Date(b.created_at ?? 0).getTime() || 0;
      return ta - tb;
    });
  }, [activeTask, turns, childTurnIds]);

  // 分片窗口：首屏仅渲染最近若干 turn，更早的由「加载更早对话」按需扩展。
  // 仅当切换任务（activeTaskId 变化）时重置窗口；流式期间 turns 引用变化（新事件到达）
  // 不应把已展开的窗口打回首屏大小，否则用户展开的更早对话会突然消失。
  const [visibleTurnCount, setVisibleTurnCount] = useState(INITIAL_TURN_COUNT);
  useEffect(() => {
    setVisibleTurnCount(INITIAL_TURN_COUNT);
    // 切换任务后重新允许自动滚底：打开任务应定位到最新内容，而非沿用上一任务的阅读位置。
    isNearBottomRef.current = true;
  }, [activeTaskId]);

  // 首屏只取最近 visibleTurnCount 个 turn；更早的以折叠入口呈现。
  const visibleTurns = useMemo(
    () => timelineTurns.slice(-visibleTurnCount),
    [timelineTurns, visibleTurnCount],
  );
  // 视口切片打点：诊断「分片窗口」是否生效——可见 turn 数应远小于总 turn 数，
  // 若 visibleTurns 接近 timelineTurns 全长，说明分片未生效、首屏渲染压力大。
  // 采样：仅当 total/visible/window 任一变化时才打，避免每帧无条件打点刷屏淹没关键日志。
  const lastVisibleMarkRef = useRef<{ total: number; visible: number; window: number } | null>(null);
  const lv = lastVisibleMarkRef.current;
  if (!lv || lv.total !== timelineTurns.length || lv.visible !== visibleTurns.length || lv.window !== visibleTurnCount) {
    lastVisibleMarkRef.current = { total: timelineTurns.length, visible: visibleTurns.length, window: visibleTurnCount };
    PerfTrace.markCurrent("chatPanel:visible-turns", {
      task_id: activeTaskId,
      total_turns: timelineTurns.length,
      visible_turns: visibleTurns.length,
      window: visibleTurnCount,
    });
  }
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
        <TurnTimeline
          turn={turn}
          events={turnEvents}
        />
      </div>
    );
  }, []);

  // 是否显示空状态（仅取决于是否有活跃任务；具体空态由 timelineTurns 决定）
  const emptySession = isNoActiveTask(activeTask);

  // 当有新事件或新消息时自动滚动到底部。
  // 节流：流式期间事件高频到达，若每次都触发 smooth 滚动动画会导致大量重排重绘而卡顿。
  // 用 rAF + 节流（每 200ms 至多滚动一次），保证跟随最新内容的同时不阻塞渲染。
  // 关键修正：虚拟列表（VirtualList）内条目为绝对定位 + translateY，滚动容器的
  // `scrollHeight` 由虚拟器 totalSize 撑起，而 totalSize 随单条动态测量（折叠/展开/流式）
  // 异步更新——若用 `scrollHeight` 滚底，会在测量滞后窗口内滚不到真底，且 smooth 动画
  // 在 totalSize 变化的瞬间与 translateY 重排相互打架，视觉上出现条目短暂重叠/跳动。
  // 因此滚动目标改为 VirtualList 上报的真实 totalSize（onTotalSizeChange 持久化到 ref），
  // 并去掉 smooth 动画（瞬时跳到最新，避免动画期间虚拟列表重排造成的抖动）。
  const lastScrollAt = useRef(0);
  const lastScrollTaskIdRef = useRef<string | null>(null);
  const virtualTotalSizeRef = useRef(0);
  const handleTotalSizeChange = useCallback((totalSize: number) => {
    virtualTotalSizeRef.current = totalSize;
  }, []);
  // 滚动跟随判定：用户在容器上的每一次滚动都刷新「是否近底」状态。
  // 程序化 scrollTo 同样触发 scroll 事件，故自动滚底后该状态保持为 true。
  const handleListScroll = useCallback((event: UIEvent<HTMLDivElement>) => {
    const el = event.currentTarget;
    isNearBottomRef.current =
      el.scrollHeight - el.scrollTop - el.clientHeight <= NEAR_BOTTOM_THRESHOLD_PX;
  }, []);
  useEffect(() => {
    const now = Date.now();
    const taskChanged = lastScrollTaskIdRef.current !== activeTaskId;
    if (!taskChanged && now - lastScrollAt.current < 200) {
      return;
    }
    const el = scrollContainerRef.current;
    // 近底守卫：用户已上滚阅读历史时不自动滚底（不打断阅读位置），也不刷新
    // 节流时间戳——用户回到底部后，下一条新事件可立即恢复跟随。
    if (!taskChanged && !isNearBottomRef.current) {
      return;
    }
    lastScrollAt.current = now;
    lastScrollTaskIdRef.current = activeTaskId;
    if (el && virtualTotalSizeRef.current > 0) {
      el.scrollTo({ top: virtualTotalSizeRef.current });
    }
    // 渲染完成锚点：本次因 events/最新事件触发的滚动提交完成，即「首帧内容可见」终点。
    PerfTrace.markCurrent("chatPanel:scroll-committed", {
      task_id: activeTaskId,
      events: events.length,
    });
  }, [activeTaskId, events.length, latestEvent]);

  return (
    <main className="flex flex-1 flex-col overflow-hidden bg-background">
      {/* 顶部：task 维度元数据选择条（与 NewTaskPage 共享同一选择条，强关联「正在聊的 task 用什么配置」） */}
      <div className="flex justify-end px-4 pt-4">
        <TaskHeaderBar
          settingsOpen={settingsOpen}
          onSettingsOpenChange={onSettingsOpenChange}
        />
      </div>

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

      {/* 空会话提示（已有工作区但无活跃任务）：给出明确动作引导，避免"死胡同"空态 */}
      {!noWorkspace && emptySession && (
        <div className="flex flex-col items-center justify-center gap-2 py-20 text-sm text-muted-foreground">
          <FolderOpen className="h-8 w-8 opacity-40" />
          <div>选择左侧任务开始对话，或直接在下方输入框开始新会话</div>
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
          onScroll={handleListScroll}
          onTotalSizeChange={handleTotalSizeChange}
          containerTestId="chat-turn-scroll-container"
        />
      )}
    </main>
  );
}



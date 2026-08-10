/**
 * 变更集数据 Hook。
 *
 * 封装「task 级文件变更集」的拉取与操作：
 * - 初始全量拉取 + 检查点过滤
 * - 保留 / 撤销单/多文件
 * - 订阅 ``file_change_stable`` SSE 事件，turn 结束时增量触发全量校准
 * - 订阅 ``file_change_updated`` SSE 事件，工具执行中实时（去抖）刷新运行中变更
 *
 * 设计取舍：两类 SSE 事件收到后都走全量 ``refresh()`` 而非本地增量，
 * 避免在前端重复实现「检查点过滤 + 按 path 去重」逻辑（该逻辑已在后端
 * ``change_set_service`` 收敛，前端再写一遍必然漂移）。
 * ``file_change_updated`` 在工具批量执行时高频到达，故做 ``FILE_CHANGE_UPDATED_DEBOUNCE_MS``
 * 去抖聚合；``file_change_stable`` 是 turn 终态校准，不去抖以保证最终一致。
 *
 * 两类事件 effect 采用「增量游标」遍历 ``taskEvents``：维护 ``stableCursorRef`` /
 * ``updatedCursorRef`` 记录上次处理到的索引，每帧只遍历新增尾部，避免长会话下事件累积导致的
 * O(n²) 全量重扫。整体替换的识别同时比对「首事件身份」（``stableFirstEventIdRef`` /
 * ``updatedFirstEventIdRef``）：``setEvents`` 经排序/合并后可能在数组头部插入更早的历史事件，
 * 此时长度可能不减反增，仅靠长度无法识别，故以首事件 ``event_id`` 是否变化判定——身份变化即回退
 * 全量重扫（例如同 task 内 ``openTask(forceRefresh=true)`` 补灌历史）。切 task（``taskId`` 变化）
 * 时清空已消费集合与游标、重新消费既有事件，避免跨 task 无限累积与遗漏。
 *
 * @module hooks/useChanges
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { ChangeSet, ChangeFile } from "@shared/api";
import type { FileChangeUpdatedPayload } from "@shared/events";
import { useEventStore, selectEventsForTask } from "../stores/eventStore";
import * as api from "../services/api";
import { logError } from "../lib/logger";

/** 运行中文件变更事件去抖刷新的静默窗口（毫秒）。 */
const FILE_CHANGE_UPDATED_DEBOUNCE_MS = 600;

/**
 * 变更集 Hook 返回值接口。
 */
interface UseChangesReturn {
  /** 当前变更集；未加载或 taskId 为空时为 null。 */
  changeSet: ChangeSet | null;
  /** 当前检查点 turn 标识；null 表示展示全部。 */
  checkpoint: string | null;
  /** 切换检查点（null 表示全部）。 */
  setCheckpoint: (turnId: string | null) => void;
  /** 撤销一批文件。 */
  revert: (paths: string[]) => Promise<void>;
  /** 保留一批文件。 */
  keep: (paths: string[]) => Promise<void>;
  /** 全量拉取变更集。 */
  refresh: () => Promise<void>;
  /** 是否正在加载。 */
  loading: boolean;
  /** 最近一次错误信息（如有）。 */
  error: string | null;
}

/**
 * 变更集数据 Hook。
 *
 * @param taskId - 任务标识；为 null 时清空状态且不发起请求。
 * @returns 变更集、检查点控制、操作与加载/错误状态。
 */
export function useChanges(taskId: string | null): UseChangesReturn {
  const [changeSet, setChangeSet] = useState<ChangeSet | null>(null);
  const [checkpoint, setCheckpointState] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 当前 task 的事件列表（SSE 增量经 eventStore 落地），用于感知 file_change_stable。
  const taskEvents = useEventStore((s) => selectEventsForTask(s, taskId));
  const taskIdRef = useRef<string | null>(taskId);
  taskIdRef.current = taskId;
  // checkpoint 的镜像 ref：与 taskIdRef 同口径，供 refresh 在 await 之后检测
  // 「请求在途期间检查点已切换」的过期响应（同任务内 C1→C2 竞态）。
  const checkpointRef = useRef<string | null>(checkpoint);
  checkpointRef.current = checkpoint;
  // 已触发 refresh 的 stable 事件 id 集合，避免历史已消费的 stable 重复触发全量刷新。
  // 切 task（taskId 变化）时清空，避免跨 task 无限累积导致内存随会话增长。
  const consumedStableEventIdsRef = useRef<Set<string>>(new Set());
  // 标记当前 taskId 是否已完成「历史 stable 消费」：初始 refresh 由 taskId-effect 承担，
  // 因此首次遇到非空事件列表时先把既有 stable 全部消费进 set，此后仅对新增 stable 触发刷新。
  const initializedTaskIdRef = useRef<string | null>(null);
  // 已消费的 file_change_updated 事件 id 集合；该事件不持久化、仅 SSE 实时推送，
  // 每个未消费的 updated 触发一次去抖全量刷新（运行中实时展示增量变更）。
  // 切 task 时清空，避免跨 task 无限累积（event_id 全局唯一，不清空也无害，但为内存考虑清空）。
  const consumedUpdatedEventIdsRef = useRef<Set<string>>(new Set());
  // 标记当前 taskId 是否已完成「历史 updated 消费」：与 stable 对称。updated 不持久化、历史
  // 回放不含，故正常场景不会命中；但在同 task 内 forceRefresh 补灌且实时已累积较多事件时，
  // 该分支可避免把「游标之前位置的既有 updated」跳过，保证预览/去抖不遗漏。
  const updatedInitializedTaskIdRef = useRef<string | null>(null);
  // file_change_updated 去抖定时器；避免工具批量执行时高频事件导致连续全量刷新。
  const updatedDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // file_change_stable 增量遍历游标：记录上次处理到的 taskEvents 索引，仅遍历新增尾部。
  // taskEvents 被整体替换（长度小于游标，或首事件身份变化）时回退为 0 做全量重扫。
  const stableCursorRef = useRef(0);
  // file_change_updated 增量遍历游标：语义同 stableCursorRef，用于 updated 事件增量处理。
  const updatedCursorRef = useRef(0);
  // file_change_stable 已处理区间的首个事件 event_id：用于识别「头部插入」式整体替换
  // （setEvents 经排序/合并后可能在数组头部插入更早的历史事件，此时长度可能不减反增，
  // 仅比长度无法识别，必须靠首事件身份）。身份变化则回退全量重扫，避免漏处理历史插入事件。
  const stableFirstEventIdRef = useRef<string | null>(null);
  // file_change_updated 首事件身份游标：语义同 stableFirstEventIdRef，用于 updated 事件。
  const updatedFirstEventIdRef = useRef<string | null>(null);

  /**
   * 全量拉取当前 task 的变更集，整体替换本地状态。
   *
   * 过期响应防护：发起时快照 requestTaskId 与 requestCheckpoint，await 之后若
   * 当前 taskId 已切换（跨任务竞态）或 checkpoint 已切换（同任务内 C1→C2 竞态），
   * 说明该响应属于旧视图，直接丢弃，不得覆盖新视图的面板状态、错误态与 loading 态。
   */
  const refresh = useCallback(async () => {
    const requestTaskId = taskIdRef.current;
    const requestCheckpoint = checkpointRef.current;
    if (!requestTaskId) {
      setChangeSet(null);
      return;
    }
    // 请求在途期间 taskId/checkpoint 是否已偏离发起时的快照。
    const isStale = () =>
      taskIdRef.current !== requestTaskId || checkpointRef.current !== requestCheckpoint;
    setLoading(true);
    setError(null);
    try {
      const data = await api.fetchChangeSet(requestTaskId, requestCheckpoint ?? undefined);
      if (isStale()) {
        return;
      }
      setChangeSet(data);
    } catch (err) {
      if (isStale()) {
        return;
      }
      const message = err instanceof Error ? err.message : "拉取变更集失败";
      logError("fetchChangeSet 失败", err, { module: "useChanges", taskId: requestTaskId });
      setError(message);
    } finally {
      // 仅当本次请求仍是「当前视图」时复位 loading；过期请求的 finally 不得
      // 清除新任务/新检查点正在进行的加载态。
      if (!isStale()) {
        setLoading(false);
      }
    }
    // 全 ref 驱动（taskIdRef/checkpointRef 调用时即时读取），闭包无外部状态依赖，
    // 故 deps 为空、refresh 身份稳定；checkpoint 变化的触发职责由下方 effect 显式承担。
  }, []);

  // taskId 或 checkpoint 变化时刷新（refresh 身份稳定，checkpoint 是显式触发源）；
  // taskId 为空则清空状态。
  useEffect(() => {
    if (!taskId) {
      setChangeSet(null);
      setError(null);
      // 旧任务请求在途时其 finally 会因过期守卫跳过复位，这里兜底清除，
      // 避免 loading 残留为 true 直至下次有效 refresh。
      setLoading(false);
      return;
    }
    void refresh();
  }, [taskId, checkpoint, refresh]);

  // 订阅 file_change_stable：turn 结束导致新变更稳定，全量校准。
  // 用 event_id 去重：
  // - 打开任务采用「先渲染后回填」策略：taskId-effect 先做初始全量 refresh，历史事件
  //   稍后经 setEvents 灌入。为避免把回放的历史 stable 当作新增而 K 次冗余刷新，
  //   首次遇到非空事件列表时先把既有 stable 全部消费进 set（不触发 refresh，数据已由
  //   初始 refresh 覆盖）；
  // - 此后仅对会话中新到达的 stable 触发一次 refresh。
  useEffect(() => {
    if (!taskId) {
      consumedStableEventIdsRef.current = new Set();
      initializedTaskIdRef.current = null;
      stableCursorRef.current = 0;
      stableFirstEventIdRef.current = null;
      return;
    }
    const consumed = consumedStableEventIdsRef.current;
    // 增量遍历起点：默认沿用上次游标；切 task（taskId 变化）或整体替换时回退全量。
    const firstEventId = taskEvents[0]?.event_id ?? null;
    let start = stableCursorRef.current;
    const replaced =
      start < 0 ||
      taskEvents.length < start ||
      (firstEventId !== null && firstEventId !== stableFirstEventIdRef.current);
    // 切 task（taskId 变化）：清空当前 task 的已消费集合与游标，重新消费既有 stable（不触发刷新，
    // 数据由初始全量 refresh 覆盖），并回退全量重扫后续新增。
    // 注意：用 clear() 保留同一 Set 引用（consumed 与 ref 指向同一对象），不可用 new Set() 替换。
    if (initializedTaskIdRef.current !== taskId) {
      consumedStableEventIdsRef.current.clear();
      if (taskEvents.length > 0) {
        for (const event of taskEvents) {
          if (event.event_type === "file_change_stable" && event.payload.task_id === taskId) {
            consumedStableEventIdsRef.current.add(event.event_id);
          }
        }
      }
      initializedTaskIdRef.current = taskId;
      stableCursorRef.current = 0;
      stableFirstEventIdRef.current = firstEventId;
      start = 0;
    } else if (replaced) {
      // 同 task 内整体替换（重连补灌 / 头部插入历史）：回退全量重扫；历史已在 consumed 中，
      // 仅对未消费的（新增或补灌）触发刷新，不会重复处理既有。
      start = 0;
    }
    // 仅对 set 中未消费的 stable 触发刷新，循环内只触发一次。
    let shouldRefresh = false;
    for (let i = start; i < taskEvents.length; i++) {
      const event = taskEvents[i];
      if (
        event.event_type === "file_change_stable" &&
        event.payload.task_id === taskId &&
        !consumed.has(event.event_id)
      ) {
        consumed.add(event.event_id);
        shouldRefresh = true;
      }
    }
    stableCursorRef.current = taskEvents.length;
    stableFirstEventIdRef.current = firstEventId;
    if (shouldRefresh) {
      void refresh();
    }
  }, [taskEvents, taskId, refresh]);

  /**
   * 把单条 file_change_updated 的 diff 即时合并进本地变更集（零延迟预览）。
   *
   * 以 path 为键 upsert：已存在则原地更新 diff，不存在则追加一行 pending 文件。
   * 该预览在去抖全量刷新到达后被权威数据整体替换，不会长期漂移。
   *
   * 参数:
   *   payload - 文件变更实时更新负载，含 path / action / additions / deletions / before / after。
   */
  const applyUpdatedDiff = useCallback((payload: FileChangeUpdatedPayload) => {
    const currentTaskId = taskIdRef.current;
    if (!currentTaskId) {
      return;
    }
    setChangeSet((prev) => {
      const base: ChangeSet =
        prev ?? { task_id: currentTaskId, checkpoints: [], files: [] };
      const files = base.files.slice();
      const idx = files.findIndex((f) => f.path === payload.path);
      const merged: ChangeFile = {
        path: payload.path,
        action: payload.action,
        status: "pending",
        last_tool_call_id: "",
        last_turn_id: payload.turn_id,
        additions: payload.additions,
        deletions: payload.deletions,
      };
      if (idx >= 0) {
        files[idx] = { ...files[idx], ...merged };
      } else {
        files.push(merged);
      }
      return { ...base, files };
    });
  }, []);

  // 订阅 file_change_updated：工具执行中每次产生文件变更即实时推送（不持久化，仅 SSE 实时）。
  // 每个未消费事件先本地即时合并 diff（零延迟渲染），再触发一次去抖全量刷新做最终校准，
  // 聚合高频批量变更，避免连续刷屏。数据源以后端 changes 接口为准，本地合并仅是即时预览。
  useEffect(() => {
    if (!taskId) {
      // 切到无 task（卸载/task 置空）：清理 pending 去抖定时器，避免对空 task 发起多余刷新。
      if (updatedDebounceRef.current) {
        clearTimeout(updatedDebounceRef.current);
        updatedDebounceRef.current = null;
      }
      consumedUpdatedEventIdsRef.current = new Set();
      updatedInitializedTaskIdRef.current = null;
      updatedCursorRef.current = 0;
      updatedFirstEventIdRef.current = null;
      return;
    }
    const consumed = consumedUpdatedEventIdsRef.current;
    // 增量遍历起点：默认沿用上次游标；切 task（taskId 变化）或整体替换时回退全量。
    const firstEventId = taskEvents[0]?.event_id ?? null;
    let start = updatedCursorRef.current;
    const replaced =
      start < 0 ||
      taskEvents.length < start ||
      (firstEventId !== null && firstEventId !== updatedFirstEventIdRef.current);
    // 切 task（taskId 变化）：清空当前 task 的已消费集合与游标，重新消费既有 updated（不触发刷新，
    // 数据将由初始全量 refresh 覆盖），并回退全量重扫后续新增。
    // 注意：用 clear() 保留同一 Set 引用（consumed 与 ref 指向同一对象），不可用 new Set() 替换。
    if (updatedInitializedTaskIdRef.current !== taskId) {
      // 切 task 前清理旧 task 的 pending 去抖定时器，避免其用新 taskId 触发多余 fetchChangeSet。
      if (updatedDebounceRef.current) {
        clearTimeout(updatedDebounceRef.current);
        updatedDebounceRef.current = null;
      }
      consumedUpdatedEventIdsRef.current.clear();
      if (taskEvents.length > 0) {
        for (const event of taskEvents) {
          if (event.event_type === "file_change_updated" && event.payload.task_id === taskId) {
            consumedUpdatedEventIdsRef.current.add(event.event_id);
          }
        }
      }
      updatedInitializedTaskIdRef.current = taskId;
      updatedCursorRef.current = 0;
      updatedFirstEventIdRef.current = firstEventId;
      start = 0;
    } else if (replaced) {
      // 同 task 内整体替换（重连补灌 / 头部插入历史）：回退全量重扫；历史已在 consumed 中，
      // 仅对未消费的（新增或补灌）即时合并，不会重复处理既有。
      start = 0;
    }
    let shouldSchedule = false;
    for (let i = start; i < taskEvents.length; i++) {
      const event = taskEvents[i];
      if (
        event.event_type === "file_change_updated" &&
        event.payload.task_id === taskId &&
        !consumed.has(event.event_id)
      ) {
        consumed.add(event.event_id);
        shouldSchedule = true;
        // 即时合并：把该事件的 diff 直接写入本地变更集，工具返回即显示，不等去抖刷新。
        applyUpdatedDiff(event.payload as FileChangeUpdatedPayload);
      }
    }
    updatedCursorRef.current = taskEvents.length;
    updatedFirstEventIdRef.current = firstEventId;
    if (!shouldSchedule) {
      return;
    }
    if (updatedDebounceRef.current) {
      clearTimeout(updatedDebounceRef.current);
    }
    updatedDebounceRef.current = setTimeout(() => {
      updatedDebounceRef.current = null;
      void refresh();
    }, FILE_CHANGE_UPDATED_DEBOUNCE_MS);
  }, [taskEvents, taskId, refresh, applyUpdatedDiff]);

  // 卸载时清理未触发的去抖定时器，避免组件销毁后仍触发 refresh 导致的状态更新与无效请求。
  useEffect(() => {
    return () => {
      if (updatedDebounceRef.current) {
        clearTimeout(updatedDebounceRef.current);
        updatedDebounceRef.current = null;
      }
    };
  }, []);

  /**
   * 切换检查点并触发刷新。
   *
   * @param turnId - 检查点 turn 标识；null 表示展示全部。
   */
  const setCheckpoint = useCallback((turnId: string | null) => {
    setCheckpointState(turnId);
  }, []);

  /**
   * 撤销一批文件的最新变更，用服务端返回的全量变更集替换本地状态。
   *
   * @param paths - 待撤销的文件路径列表。
   */
  const revert = useCallback(async (paths: string[]) => {
    const requestTaskId = taskIdRef.current;
    const requestCheckpoint = checkpointRef.current;
    if (!requestTaskId) {
      return;
    }
    // 与 refresh 同口径：taskId 或 checkpoint 任一失配即为过期响应（过期后由
    // 新视图的 refresh 兜底回填，丢弃安全）。
    const isStale = () =>
      taskIdRef.current !== requestTaskId || checkpointRef.current !== requestCheckpoint;
    setLoading(true);
    setError(null);
    try {
      const data = await api.revertChanges(requestTaskId, paths);
      if (isStale()) {
        return;
      }
      setChangeSet(data);
    } catch (err) {
      if (isStale()) {
        return;
      }
      const message = err instanceof Error ? err.message : "撤销变更失败";
      logError("revertChanges 失败", err, { module: "useChanges", taskId: requestTaskId });
      setError(message);
    } finally {
      if (!isStale()) {
        setLoading(false);
      }
    }
  }, []);

  /**
   * 保留一批文件的最新变更，用服务端返回的全量变更集替换本地状态。
   *
   * @param paths - 待保留的文件路径列表。
   */
  const keep = useCallback(async (paths: string[]) => {
    const requestTaskId = taskIdRef.current;
    const requestCheckpoint = checkpointRef.current;
    if (!requestTaskId) {
      return;
    }
    // 与 refresh 同口径：taskId 或 checkpoint 任一失配即为过期响应（过期后由
    // 新视图的 refresh 兜底回填，丢弃安全）。
    const isStale = () =>
      taskIdRef.current !== requestTaskId || checkpointRef.current !== requestCheckpoint;
    setLoading(true);
    setError(null);
    try {
      const data = await api.keepChanges(requestTaskId, paths);
      if (isStale()) {
        return;
      }
      setChangeSet(data);
    } catch (err) {
      if (isStale()) {
        return;
      }
      const message = err instanceof Error ? err.message : "保留变更失败";
      logError("keepChanges 失败", err, { module: "useChanges", taskId: requestTaskId });
      setError(message);
    } finally {
      if (!isStale()) {
        setLoading(false);
      }
    }
  }, []);

  return {
    changeSet,
    checkpoint,
    setCheckpoint,
    revert,
    keep,
    refresh,
    loading,
    error,
  };
}

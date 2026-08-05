/**
 * 变更集数据 Hook。
 *
 * 封装「task 级文件变更集」的拉取与操作：
 * - 初始全量拉取 + 检查点过滤
 * - 保留 / 撤销单/多文件
 * - 订阅 ``file_change_stable`` SSE 事件，turn 结束时增量触发全量校准
 *
 * 设计取舍：SSE 收到 ``file_change_stable`` 后走全量 ``refresh()`` 而非本地增量，
 * 避免在前端重复实现「检查点过滤 + 按 path 去重」逻辑（该逻辑已在后端
 * ``change_set_service`` 收敛，前端再写一遍必然漂移）。
 *
 * @module hooks/useChanges
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { ChangeSet } from "@shared/api";
import { useEventStore, selectEventsForTask } from "../stores/eventStore";
import * as api from "../services/api";
import { logError } from "../lib/logger";

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
  // 已触发 refresh 的 stable 事件 id 集合，避免历史已消费的 stable 重复触发全量刷新。
  const consumedStableEventIdsRef = useRef<Set<string>>(new Set());
  // 标记当前 taskId 是否已完成「历史 stable 消费」：初始 refresh 由 taskId-effect 承担，
  // 因此首次遇到非空事件列表时先把既有 stable 全部消费进 set，此后仅对新增 stable 触发刷新。
  const initializedTaskIdRef = useRef<string | null>(null);

  /**
   * 全量拉取当前 task 的变更集，整体替换本地状态。
   */
  const refresh = useCallback(async () => {
    if (!taskIdRef.current) {
      setChangeSet(null);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const data = await api.fetchChangeSet(taskIdRef.current, checkpoint ?? undefined);
      setChangeSet(data);
    } catch (err) {
      const message = err instanceof Error ? err.message : "拉取变更集失败";
      logError("fetchChangeSet 失败", err, { module: "useChanges", taskId: taskIdRef.current });
      setError(message);
    } finally {
      setLoading(false);
    }
  }, [checkpoint]);

  // taskId 或 checkpoint 变化时刷新；taskId 为空则清空状态。
  useEffect(() => {
    if (!taskId) {
      setChangeSet(null);
      setError(null);
      return;
    }
    void refresh();
  }, [taskId, refresh]);

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
      return;
    }
    const consumed = consumedStableEventIdsRef.current;
    // 该 taskId 首次见到非空事件列表：消费全部既有 stable，不触发刷新。
    if (initializedTaskIdRef.current !== taskId && taskEvents.length > 0) {
      for (const event of taskEvents) {
        if (event.event_type === "file_change_stable" && event.payload.task_id === taskId) {
          consumed.add(event.event_id);
        }
      }
      initializedTaskIdRef.current = taskId;
    }
    // 仅对 set 中未消费的 stable 触发刷新，循环内只触发一次。
    let shouldRefresh = false;
    for (const event of taskEvents) {
      if (
        event.event_type === "file_change_stable" &&
        event.payload.task_id === taskId &&
        !consumed.has(event.event_id)
      ) {
        consumed.add(event.event_id);
        shouldRefresh = true;
      }
    }
    if (shouldRefresh) {
      void refresh();
    }
  }, [taskEvents, taskId, refresh]);

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
    if (!taskIdRef.current) {
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const data = await api.revertChanges(taskIdRef.current, paths);
      setChangeSet(data);
    } catch (err) {
      const message = err instanceof Error ? err.message : "撤销变更失败";
      logError("revertChanges 失败", err, { module: "useChanges", taskId: taskIdRef.current });
      setError(message);
    } finally {
      setLoading(false);
    }
  }, []);

  /**
   * 保留一批文件的最新变更，用服务端返回的全量变更集替换本地状态。
   *
   * @param paths - 待保留的文件路径列表。
   */
  const keep = useCallback(async (paths: string[]) => {
    if (!taskIdRef.current) {
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const data = await api.keepChanges(taskIdRef.current, paths);
      setChangeSet(data);
    } catch (err) {
      const message = err instanceof Error ? err.message : "保留变更失败";
      logError("keepChanges 失败", err, { module: "useChanges", taskId: taskIdRef.current });
      setError(message);
    } finally {
      setLoading(false);
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

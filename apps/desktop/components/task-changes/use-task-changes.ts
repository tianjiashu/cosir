import { useCallback, useEffect, useRef, useState } from "react";

import {
  getTaskChanges,
  keepTaskChanges,
  revertTaskChanges,
  type TaskChangeActionResponse,
  type TaskChangeResult,
  type TaskChangeSet,
} from "@/lib/api/changes";
import { HttpError } from "@/lib/http/errors";
import { safeFrontendErrorMessage } from "@/lib/logging/frontend-log";

export type TaskChangesViewState = {
  changeSet: TaskChangeSet | null;
  loading: boolean;
  loadError: string | null;
  actionError: string | null;
  refreshing: boolean;
  actionBusy: boolean;
  resultsByChangeId: ReadonlyMap<string, TaskChangeResult>;
  refresh: () => Promise<void>;
  keep: (changeIds: string[]) => Promise<void>;
  revert: (changeIds: string[]) => Promise<void>;
};

export function isTaskChangesResponseCurrent(input: {
  signal: AbortSignal;
  requestedTaskId: number;
  currentTaskId: number;
  requestedTaskGeneration: number;
  currentTaskGeneration: number;
  requestedLoadGeneration?: number;
  currentLoadGeneration?: number;
}): boolean {
  return !input.signal.aborted
    && input.requestedTaskId === input.currentTaskId
    && input.requestedTaskGeneration === input.currentTaskGeneration
    && (input.requestedLoadGeneration === undefined
      || input.requestedLoadGeneration === input.currentLoadGeneration);
}

/** Owns task-scoped ChangeSet reads and actions, fencing every response by task and request generation. */
export function useTaskChanges(taskId: number, isRunActive: boolean): TaskChangesViewState {
  const [storedChangeSet, setStoredChangeSet] = useState<TaskChangeSet | null>(null);
  const [storedTaskId, setStoredTaskId] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionBusy, setActionBusy] = useState(false);
  const [resultsByChangeId, setResultsByChangeId] = useState<ReadonlyMap<string, TaskChangeResult>>(new Map());

  const taskIdRef = useRef(taskId);
  const taskGenerationRef = useRef(0);
  const loadGenerationRef = useRef(0);
  const readControllerRef = useRef<AbortController | null>(null);
  const actionControllerRef = useRef<AbortController | null>(null);
  const runActiveRef = useRef(isRunActive);
  taskIdRef.current = taskId;
  runActiveRef.current = isRunActive;

  const load = useCallback(async (taskGeneration: number, showInitialLoading = false) => {
    readControllerRef.current?.abort();
    const controller = new AbortController();
    readControllerRef.current = controller;
    const loadGeneration = ++loadGenerationRef.current;
    const requestedTaskId = taskId;
    const isCurrent = () => isTaskChangesResponseCurrent({
      signal: controller.signal,
      requestedTaskId,
      currentTaskId: taskIdRef.current,
      requestedTaskGeneration: taskGeneration,
      currentTaskGeneration: taskGenerationRef.current,
      requestedLoadGeneration: loadGeneration,
      currentLoadGeneration: loadGenerationRef.current,
    });

    if (showInitialLoading) setLoading(true);
    else setRefreshing(true);
    setLoadError(null);

    try {
      const changeSet = await getTaskChanges(requestedTaskId, { signal: controller.signal });
      if (!isCurrent()) return;
      if (changeSet.task_id !== requestedTaskId) {
        setLoadError("任务变更响应与当前任务不匹配，请重试");
        return;
      }
      setStoredChangeSet(changeSet);
      setStoredTaskId(requestedTaskId);
    } catch (cause) {
      if (!isCurrent()) return;
      setLoadError(safeFrontendErrorMessage(cause, "无法加载任务文件变更，请重试"));
    } finally {
      if (isCurrent()) {
        setLoading(false);
        setRefreshing(false);
      }
    }
  }, [taskId]);

  useEffect(() => {
    const taskGeneration = ++taskGenerationRef.current;
    loadGenerationRef.current += 1;
    readControllerRef.current?.abort();
    actionControllerRef.current?.abort();
    actionControllerRef.current = null;
    setStoredChangeSet(null);
    setStoredTaskId(null);
    setLoading(true);
    setRefreshing(false);
    setLoadError(null);
    setActionError(null);
    setActionBusy(false);
    setResultsByChangeId(new Map());
    void load(taskGeneration, true);

    return () => {
      readControllerRef.current?.abort();
      actionControllerRef.current?.abort();
      if (taskGenerationRef.current === taskGeneration) taskGenerationRef.current += 1;
    };
  }, [load]);

  const refresh = useCallback(async () => {
    await load(taskGenerationRef.current);
  }, [load]);

  const runTransitionRef = useRef({ taskId, active: isRunActive });
  useEffect(() => {
    if (runTransitionRef.current.taskId !== taskId) {
      runTransitionRef.current = { taskId, active: isRunActive };
      return;
    }
    const wasActive = runTransitionRef.current.active;
    runTransitionRef.current.active = isRunActive;
    if (wasActive && !isRunActive) void refresh();
  }, [isRunActive, refresh, taskId]);

  const mutate = useCallback(async (
    action: "keep" | "revert",
    changeIds: string[],
  ) => {
    const uniqueChangeIds = [...new Set(changeIds)].filter(Boolean);
    if (uniqueChangeIds.length === 0 || actionControllerRef.current || actionBusy) return;
    if (runActiveRef.current) {
      setActionError("任务仍在运行，请完成后再操作");
      return;
    }

    const requestedTaskId = taskId;
    const taskGeneration = taskGenerationRef.current;
    const controller = new AbortController();
    actionControllerRef.current = controller;
    readControllerRef.current?.abort();
    loadGenerationRef.current += 1;
    setActionBusy(true);
    setActionError(null);
    setLoadError(null);

    const isCurrent = () => isTaskChangesResponseCurrent({
      signal: controller.signal,
      requestedTaskId,
      currentTaskId: taskIdRef.current,
      requestedTaskGeneration: taskGeneration,
      currentTaskGeneration: taskGenerationRef.current,
    });

    try {
      const response: TaskChangeActionResponse = action === "keep"
        ? await keepTaskChanges(requestedTaskId, uniqueChangeIds, controller.signal)
        : await revertTaskChanges(requestedTaskId, uniqueChangeIds, controller.signal);
      if (!isCurrent()) return;
      if (response.task_id !== requestedTaskId || response.change_set.task_id !== requestedTaskId) {
        setActionError("任务变更响应与当前任务不匹配，请刷新后重试");
        return;
      }
      setStoredChangeSet(response.change_set);
      setStoredTaskId(requestedTaskId);
      setResultsByChangeId(new Map(response.results.map((result) => [result.change_id, result])));
    } catch (cause) {
      if (!isCurrent()) return;
      setActionError(cause instanceof HttpError && cause.status === 409
        ? "任务仍在运行，请完成后再操作"
        : safeFrontendErrorMessage(cause, "文件变更操作失败，请刷新后重试"));
    } finally {
      if (isCurrent()) {
        setActionBusy(false);
        actionControllerRef.current = null;
      }
    }
  }, [actionBusy, taskId]);

  return {
    changeSet: storedTaskId === taskId ? storedChangeSet : null,
    loading,
    loadError,
    actionError,
    refreshing,
    actionBusy,
    resultsByChangeId,
    refresh,
    keep: (changeIds) => mutate("keep", changeIds),
    revert: (changeIds) => mutate("revert", changeIds),
  };
}

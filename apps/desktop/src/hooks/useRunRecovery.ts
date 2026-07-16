/**
 * Durable Run State 恢复编排 Hook。
 *
 * @module hooks/useRunRecovery
 */

import { useCallback } from "react";
import { fetchRecoverableRuns, cancelRunTask } from "@/services/runs";
import { useRunStore } from "@/stores/runStore";
import { logError, logInfo } from "@/lib/logger";
import { beginClientTrace, endClientTrace, hasClientTrace } from "@/services/tracePropagation";

/**
 * 提供可恢复运行查询与取消编排。
 *
 * @returns 恢复状态和可供组件调用的动作。
 *
 * @sideeffect 调用 runs service、写入 runStore，并通过统一 logger 记录恢复路径。
 */
export function useRunRecovery() {
  const recoverableRuns = useRunStore((state) => state.recoverableRuns);
  const isLoadingRecoverableRuns = useRunStore((state) => state.isLoadingRecoverableRuns);
  const recoveryError = useRunStore((state) => state.recoveryError);
  const setRecoverableRuns = useRunStore((state) => state.setRecoverableRuns);
  const setLoadingRecoverableRuns = useRunStore((state) => state.setLoadingRecoverableRuns);
  const setRecoveryError = useRunStore((state) => state.setRecoveryError);

  const refreshRecoverableRuns = useCallback(async () => {
    setLoadingRecoverableRuns(true);
    setRecoveryError(null);
    const ownsOperation = !hasClientTrace();
    if (ownsOperation) {
      beginClientTrace();
    }
    try {
      const runs = await fetchRecoverableRuns();
      setRecoverableRuns(runs);
      logInfo("恢复运行状态刷新完成", { module: "useRunRecovery", count: runs.length });
    } catch (err) {
      setRecoveryError(err instanceof Error ? err.message : String(err));
      logError("恢复运行状态刷新失败", err, { module: "useRunRecovery" });
    } finally {
      setLoadingRecoverableRuns(false);
      if (ownsOperation) {
        endClientTrace();
      }
    }
  }, [setLoadingRecoverableRuns, setRecoveryError, setRecoverableRuns]);

  const cancelRecoverableRun = useCallback(
    async (taskId: string) => {
      const ownsOperation = !hasClientTrace();
      if (ownsOperation) {
        beginClientTrace({ taskId });
      }
      try {
        await cancelRunTask(taskId);
        await refreshRecoverableRuns();
      } catch (err) {
        setRecoveryError(err instanceof Error ? err.message : String(err));
        logError("取消可恢复运行失败", err, { module: "useRunRecovery", task_id: taskId });
      } finally {
        if (ownsOperation) {
          endClientTrace();
        }
      }
    },
    [refreshRecoverableRuns, setRecoveryError],
  );

  return {
    recoverableRuns,
    isLoadingRecoverableRuns,
    recoveryError,
    refreshRecoverableRuns,
    cancelRecoverableRun,
  };
}

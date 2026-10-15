import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import type { TransportState } from "@/lib/assistant/contract";
import { fetchCanonicalSnapshot } from "@/lib/assistant/canonical-snapshot";
import { frontendLog } from "@/lib/logging/frontend-log";
import { getBackendRuntimeSnapshot, subscribeBackendRuntime } from "@/src/runtime-config";
import { userErrorMessage } from "@/lib/errors/user-error-message";

const INITIAL_STATE_TIMEOUT_MS = 30_000;

/** hook 返回的首屏历史加载结果。 */
export type AssistantInitialStateResult = {
  /** 服务端历史已就绪时的 state；未就绪（加载中/出错）时为 null。 */
  initialState: TransportState | null;
  /** 加载失败时的错误信息；无错误时为 null。 */
  error: string | null;
  /** 手动重试：清空错误后重新拉取，路径与初次加载一致。 */
  retry: () => void;
};

/**
 * 拉取指定任务的首屏对话历史。
 *
 * `useAssistantTransportRuntime` 只在 runtime 首次创建时捕获一次 `initialState`，
 * 因此首屏历史必须「先取后挂」：本 hook 取到快照后入口才挂载会话。
 *
 * 每个 task 只成功加载一次。会话挂载后，后端不可用、重启或换实例都**不会**清空
 * 这里的结果——那会卸载会话、丢掉草稿与附件；后端恢复后由会话内的
 * useBackendTransitions → recovery.resync 原地对账。只有尚未加载成功时，后端恢复
 * 或换代才会自动重新拉取。
 */
export function useAssistantInitialState(
  taskId: number,
): AssistantInitialStateResult {
  const backendRuntime = useSyncExternalStore(
    subscribeBackendRuntime,
    getBackendRuntimeSnapshot,
    getBackendRuntimeSnapshot,
  );
  const [loadedState, setLoadedState] = useState<{ taskId: number; state: TransportState } | null>(null);
  const [loadError, setLoadError] = useState<{ taskId: number; message: string } | null>(null);
  const [retryToken, setRetryToken] = useState(0);
  const loaded = loadedState?.taskId === taskId;
  const loadedRef = useRef(loaded);
  loadedRef.current = loaded;

  const load = useCallback((signal: AbortSignal) => {
    void fetchCanonicalSnapshot(taskId, {
      reason: "initial_state",
      timeoutMs: INITIAL_STATE_TIMEOUT_MS,
      signal,
    })
      .then((outcome) => {
        // superseded：task 已切换、组件卸载或后端在请求期间变化；后两者由 effect 重新发起。
        if (outcome.kind === "superseded") return;
        const state = outcome.snapshot;
        void frontendLog("INFO", "assistant_initial_state_loaded", "加载对话历史成功", {
          data: {
            taskId,
            runCount: state.runs.length,
            messageCount: state.runs.reduce((count, run) => count + run.messages.length, 0),
            runId: state.current_run_id,
            runStatus: state.current_run_id === null ? null : state.runs.find((run) => run.runId === state.current_run_id)?.status ?? null,
          },
        });
        setLoadedState({ taskId, state });
        setLoadError(null);
      })
      .catch(async (cause: unknown) => {
        if (signal.aborted) return;
        await frontendLog("ERROR", "assistant_initial_state_failed", "加载对话历史失败", {
          data: { taskId },
          error: cause,
        });
        setLoadError({ taskId, message: userErrorMessage(cause, "对话历史加载失败，请重试") });
      });
  }, [taskId]);

  useEffect(() => {
    if (loadedRef.current || !backendRuntime.available) return;
    const controller = new AbortController();
    setLoadError(null);
    load(controller.signal);
    return () => controller.abort();
  }, [backendRuntime.available, backendRuntime.generation, load, retryToken]);

  const retry = useCallback(() => setRetryToken((token) => token + 1), []);

  return {
    initialState: loaded ? loadedState.state : null,
    error: loadError?.taskId === taskId ? loadError.message : null,
    retry,
  };
}

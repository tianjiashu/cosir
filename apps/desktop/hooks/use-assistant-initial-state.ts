import { apiRequest } from "@/lib/api/client";
import { useEffect, useRef, useState } from "react";
import type { TransportState } from "@/lib/assistant/contract";
import { parseTransportState } from "@/lib/assistant/snapshot-validation";
import { frontendLog, safeFrontendErrorMessage } from "@/lib/logging/frontend-log";

/** hook 返回的首屏历史加载结果。 */
export type AssistantInitialStateResult = {
  /** 服务端历史已就绪时的 state；未就绪（加载中/出错）时为 null。 */
  initialState: TransportState | null;
  /** 加载失败时的错误信息；无错误时为 null。 */
  error: string | null;
  /**
   * 手动重试：清空当前错误与历史，重新向服务端拉取首屏 state。
   * 入口的错误态「重试」按钮调用它，行为与初次加载完全一致。
   */
  retry: () => void;
};

/**
 * 拉取指定任务的首屏对话历史，并管理加载/错误状态。
 *
 * 关键约束（任务书 §3.5）：`useAssistantTransportRuntime` 只在 runtime 首次
 * 创建时捕获一次 `initialState`，之后变化不再生效。因此首屏历史必须「先取后挂」，
 * 在挂载后就绪后才由入口渲染内层 runtime。本 hook 负责这一段拉取与状态管理，
 * 渲染分支（骨架屏/错误提示）留在入口组件。
 *
 * 行为说明：
 * - 挂载及 `taskId` 变化时立即拉取；同时重置上一次的错误与历史，避免旧 task 残留。
 * - 拉取成功：严格校验并写入服务端返回的完整 `initialState`，不补造字段。
 * - 拉取失败：写 `error`（优先取 Error.message，否则固定文案），并保留错误态供「重试」。
 * - 卸载或 `taskId` 变化导致的新一轮拉取发起后，旧请求的结果不再写入（cancelledRef 守卫）。
 * - 初次加载与 `retry` 共用同一个 `load()`，请求逻辑与错误文案只写一处，消除重复。
 *
 * @param taskId - 当前任务 id；变化即触发重新拉取。
 * @returns 含 `initialState` / `error` / `retry` 的对象，供入口渲染与重试使用。
 */
export function useAssistantInitialState(
  taskId: number,
): AssistantInitialStateResult {
  const [loadedState, setLoadedState] = useState<{
    taskId: number;
    state: TransportState;
  } | null>(null);
  const [loadError, setLoadError] = useState<{
    taskId: number;
    message: string;
  } | null>(null);
  // cancelledRef 守卫：task 切换或卸载后，旧请求的结果不再写入 state/error，
  // 避免旧 task 的加载结果污染新 task 的界面。
  const cancelledRef = useRef(false);
  const requestGenerationRef = useRef(0);

  useEffect(() => {
    // 切换 task 时重置状态，并复位守卫，避免旧 task 的历史/错误态残留到新 task。
    cancelledRef.current = false;
    load();
    return () => {
      // 卸载或 task 变化：标记本轮拉取作废，后续回调不再写状态。
      cancelledRef.current = true;
    };
    // 仅在 taskId 变化时重新拉取；load 以最新 taskId 闭包捕获，见下方定义。
  }, [taskId]);

  /**
   * 向服务端拉取首屏 state 并写入状态。
   *
   * 初次加载与 `retry` 共用本函数：成功写 `initialState`，失败写 `error`
   * （优先取 Error.message，否则固定文案）。`cancelledRef` 守卫确保作废的
   * 旧请求不会覆盖新状态。
   */
  function load() {
    const requestGeneration = ++requestGenerationRef.current;
    void apiRequest<unknown>(`/tasks/${taskId}/assistant/state`)
      .then((data) => {
        if (
          cancelledRef.current ||
          requestGeneration !== requestGenerationRef.current
        ) {
          return;
        }
        const state = parseTransportState(data);
        void frontendLog("INFO", "assistant_initial_state_loaded", "加载对话历史成功", {
          data: {
            taskId,
            messageCount: state.messages.length,
            runId: state.run.runId,
            runStatus: state.run.status,
          },
        });
        setLoadedState({
          taskId,
          state,
        });
        setLoadError(null);
      })
      .catch(async (cause: unknown) => {
        if (
          cancelledRef.current ||
          requestGeneration !== requestGenerationRef.current
        ) {
          return;
        }
        await frontendLog("ERROR", "assistant_initial_state_failed", "加载对话历史失败", {
          data: { taskId },
          error: cause,
        });
        setLoadError({
          taskId,
          message: safeFrontendErrorMessage(cause, "对话历史加载失败，请重试"),
        });
      });
  }

  const retry = () => {
    // 清空错误与历史后重新拉取，路径与初次加载的 load() 完全一致。
    setLoadError(null);
    setLoadedState(null);
    load();
  };

  return {
    initialState:
      loadedState?.taskId === taskId ? loadedState.state : null,
    error: loadError?.taskId === taskId ? loadError.message : null,
    retry,
  };
}

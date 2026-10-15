import { isCancelledAbort, isTimeoutAbort, withTimeout } from "@/lib/async/abort-timeout";
import { requestAssistantSnapshot } from "@/lib/assistant/assistant-snapshot-client";
import type { TransportState } from "@/lib/assistant/contract";
import { frontendLog } from "@/lib/logging/frontend-log";
import { getBackendRuntimeSnapshot } from "@/src/runtime-config";

/**
 * 读取 canonical snapshot 的结果。
 *
 * - `fresh`：快照来自请求开始时的同一个、仍然可用的后端实例，可以投影到 runtime。
 * - `superseded`：请求期间后端实例变化 / 不可用，或调用方主动取消；结果不得使用，
 *   也不是失败——后端恢复后由会话的后端变化入口统一对账。
 * 网络错误、HTTP 错误、快照校验失败与超时（`TimeoutError`）直接抛出，由调用方按失败处理。
 */
export type CanonicalSnapshotOutcome =
  | { kind: "fresh"; snapshot: TransportState }
  | { kind: "superseded" };

export type CanonicalSnapshotOptions = {
  /** 稳定的 snake_case 原因，写入日志用于区分是哪条恢复链路发起的读取。 */
  reason: string;
  timeoutMs: number;
  traceId?: string;
  /** 调用方的取消信号（卸载、被新请求取代）；触发后结果视为 superseded。 */
  signal?: AbortSignal;
};

/**
 * 会话内读取 canonical Assistant Transport snapshot 的唯一入口。
 *
 * 统一承担此前散落在各恢复用例中的样板：超时（以 TimeoutError 区别于取消）、
 * 调用方取消、后端实例 generation 守卫与失败日志。各用例只保留自己的业务判断
 * （运行是否仍在进行、是否需要 attach、取消是否已生效），不再各自比较后端代。
 */
export async function fetchCanonicalSnapshot(
  taskId: number,
  options: CanonicalSnapshotOptions,
): Promise<CanonicalSnapshotOutcome> {
  const backend = getBackendRuntimeSnapshot();
  if (!backend.available) return { kind: "superseded" };
  const isSameBackend = () => {
    const current = getBackendRuntimeSnapshot();
    return current.available && current.generation === backend.generation;
  };

  const { signal, clear } = withTimeout(options.signal, options.timeoutMs);

  try {
    const snapshot = await requestAssistantSnapshot(taskId, {
      signal,
      traceId: options.traceId,
      timeoutMs: options.timeoutMs,
    });
    if (isCancelledAbort(signal) || !isSameBackend()) return { kind: "superseded" };
    return { kind: "fresh", snapshot };
  } catch (error) {
    if (isCancelledAbort(signal) || !isSameBackend()) return { kind: "superseded" };
    void frontendLog("WARNING", "assistant_snapshot_fetch_failed", "读取对话权威快照失败", {
      traceId: options.traceId,
      data: { taskId, reason: options.reason, timedOut: isTimeoutAbort(signal), backendGeneration: backend.generation },
      error,
    });
    throw error;
  } finally {
    clear();
  }
}

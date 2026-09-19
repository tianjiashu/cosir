/** 向本机后端提交一次工具级取消信号。 */

import { requestRaw } from "@/lib/http/client";
import { createTimeoutAbort } from "@/lib/async/abort-timeout";
import { frontendLog } from "@/lib/logging/frontend-log";
import { getActiveTraceId, newTraceId } from "@/lib/trace";

const CANCEL_TOOL_CALL_TIMEOUT_MS = 15_000;

export type CancelToolCallResult =
  | { kind: "signalled" }
  | { kind: "already_signalled" }
  | { kind: "failed"; reason: "invalid_target" | "run_not_found" | "rejected" | "network"; message: string };

/**
 * 标记某个工具调用待取消；调用成功不代表工具已经结束，终态仍以 Transport snapshot 为准。
 */
export async function cancelToolCall(runId: number | null, toolCallId: string): Promise<CancelToolCallResult> {
  if (typeof runId !== "number" || !Number.isInteger(runId) || runId < 1 || !toolCallId.trim()) {
    return { kind: "failed", reason: "invalid_target", message: "当前命令标识无效，无法停止。" };
  }

  const traceId = getActiveTraceId() ?? newTraceId();
  void frontendLog("INFO", "assistant_tool_call_cancel_requested", "用户请求停止终端工具调用", {
    traceId,
    data: { runId, toolCallId },
  });
  const { signal, clear } = createTimeoutAbort(CANCEL_TOOL_CALL_TIMEOUT_MS);

  try {
    const response = await requestRaw(
      `/runs/${runId}/tool-calls/${encodeURIComponent(toolCallId)}/cancel`,
      {
        method: "POST",
        signal,
        traceId,
      },
    );

    if (response.ok) return logResult({ kind: "signalled" }, runId, toolCallId, traceId);
    if (response.status === 409) return logResult({ kind: "already_signalled" }, runId, toolCallId, traceId);
    if (response.status === 404) {
      return logResult({ kind: "failed", reason: "run_not_found", message: "运行已结束或不可用，请等待状态同步。" }, runId, toolCallId, traceId);
    }
    return logResult({
      kind: "failed",
      reason: "rejected",
      message: `停止命令请求被服务端拒绝（HTTP ${response.status}）。`,
    }, runId, toolCallId, traceId);
  } catch {
    return logResult(
      { kind: "failed", reason: "network", message: "停止请求未能确认，请检查本机后端连接。" },
      runId,
      toolCallId,
      traceId,
    );
  } finally {
    clear();
  }
}

function logResult(
  result: CancelToolCallResult,
  runId: number,
  toolCallId: string,
  traceId: string,
): CancelToolCallResult {
  const failed = result.kind === "failed";
  void frontendLog(failed ? "WARNING" : "INFO", "assistant_tool_call_cancel_result", "终端工具取消请求已返回", {
    traceId,
    data: {
      runId,
      toolCallId,
      result: result.kind,
      reason: failed ? result.reason : null,
    },
  });
  return result;
}

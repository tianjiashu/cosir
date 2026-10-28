/**
 * 按 `tool_call_id` 寻址的单次工具调用命令：取消与转到后台。
 *
 * 两者都只表示命令被接受，调用的终态与看板条目以 Transport snapshot 为准（卡片状态来自占位行，
 * 看板来自 `deferred_results`），前端不做乐观更新。
 */

import { requestRaw } from "@/lib/http/client";
import { createTimeoutAbort } from "@/lib/async/abort-timeout";
import { frontendLog } from "@/lib/logging/frontend-log";
import { newTraceId } from "@/lib/trace";

const TOOL_CALL_COMMAND_TIMEOUT_MS = 15_000;

export type CancelToolCallResult =
  | { kind: "signalled" }
  | { kind: "already_signalled" }
  | { kind: "failed"; reason: "invalid_target" | "rejected" | "network"; message: string };

/**
 * 取消一次工具调用：登记了延迟结果的调用（前台等待或后台工作）不要求 Run 在执行，其余只命中 Task
 * 当前执行中的调用。调用成功不代表工具已经结束。后端 409 表示已没有可取消的对象（已标记过、调用已
 * 结束或 Task 没有执行），对界面都意味着无需再次请求，统一视为 already_signalled。
 */
export async function cancelToolCall(taskId: number | undefined, toolCallId: string): Promise<CancelToolCallResult> {
  if (taskId === undefined || !Number.isInteger(taskId) || !toolCallId.trim()) {
    return { kind: "failed", reason: "invalid_target", message: "当前命令标识无效，无法停止。" };
  }

  const traceId = newTraceId();
  void frontendLog("INFO", "assistant_tool_call_cancel_requested", "用户请求取消工具调用", {
    traceId,
    data: { taskId, toolCallId },
  });
  const { signal, clear } = createTimeoutAbort(TOOL_CALL_COMMAND_TIMEOUT_MS);

  try {
    const response = await requestRaw(
      `/tasks/${taskId}/tool-calls/${encodeURIComponent(toolCallId)}/cancel`,
      {
        method: "POST",
        signal,
        traceId,
      },
    );

    if (response.ok) return logResult({ kind: "signalled" }, taskId, toolCallId, traceId);
    if (response.status === 409) return logResult({ kind: "already_signalled" }, taskId, toolCallId, traceId);
    return logResult({
      kind: "failed",
      reason: "rejected",
      message: `停止命令请求被服务端拒绝（HTTP ${response.status}）。`,
    }, taskId, toolCallId, traceId);
  } catch {
    return logResult(
      { kind: "failed", reason: "network", message: "停止请求未能确认，请检查本机后端连接。" },
      taskId,
      toolCallId,
      traceId,
    );
  } finally {
    clear();
  }
}

function logResult(
  result: CancelToolCallResult,
  taskId: number,
  toolCallId: string,
  traceId: string,
): CancelToolCallResult {
  const failed = result.kind === "failed";
  void frontendLog(failed ? "WARNING" : "INFO", "assistant_tool_call_cancel_result", "工具调用取消请求已返回", {
    traceId,
    data: {
      taskId,
      toolCallId,
      result: result.kind,
      reason: failed ? result.reason : null,
    },
  });
  return result;
}

export type BackgroundToolCallResult =
  | { kind: "detached" }
  | { kind: "not_detachable" }
  | { kind: "failed"; message: string };

/**
 * 把一次前台等待中的工具调用转到后台：工具立即以「已转后台」占位闭合，结论稍后回投。409
 * `TOOL_CALL_NOT_DETACHABLE` 表示调用已结束或本来就在后台（结论与转后台同时到达时结论先到）。
 */
export async function backgroundToolCall(taskId: number, toolCallId: string): Promise<BackgroundToolCallResult> {
  const traceId = newTraceId();
  void frontendLog("INFO", "assistant_tool_call_background_requested", "用户请求把工具调用转到后台", {
    traceId,
    data: { taskId, toolCallId },
  });
  const { signal, clear } = createTimeoutAbort(TOOL_CALL_COMMAND_TIMEOUT_MS);
  let result: BackgroundToolCallResult;
  try {
    const response = await requestRaw(
      `/tasks/${taskId}/tool-calls/${encodeURIComponent(toolCallId)}/background`,
      { method: "POST", signal, traceId },
    );
    if (response.ok) result = { kind: "detached" };
    else if (response.status === 409) result = { kind: "not_detachable" };
    else result = { kind: "failed", message: `转到后台被服务端拒绝（HTTP ${response.status}）。` };
  } catch {
    result = { kind: "failed", message: "转到后台请求未能确认，请检查本机后端连接。" };
  } finally {
    clear();
  }
  void frontendLog(result.kind === "failed" ? "WARNING" : "INFO", "assistant_tool_call_background_result", "转到后台请求已返回", {
    traceId,
    data: { taskId, toolCallId, result: result.kind },
  });
  return result;
}

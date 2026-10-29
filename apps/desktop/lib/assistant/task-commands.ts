/**
 * Task 级业务命令客户端：发送、排队消息的立即发送 / 删除（编辑排队消息即删除后放回输入框，不是独立命令）、
 * 编辑重跑、恢复、用户决定、停止 Task，以及按 `tool_call_id` 寻址的取消工具调用与转到后台。
 *
 * 命令只对 Task（或消息）寻址，不携带 runId：Run 身份与调度规则（立即并入、排队、新开 Run、
 * 唤醒挂起的 Run）全部由后端 Task 调度 actor 决定。后端处理完这一轮才返回：200 表示已受理
 * （发送类为消息已持久化入队，开新 Run、并入或唤醒由 actor 随后推进），响应体为空对象；状态冲突（不可恢复、重跑目标已过期等）为
 * 409。Run 输出与状态经 Task 长订阅的 snapshot 到达，本模块不读取 SSE。
 *
 * 异常：发送类命令非 2xx 抛 HttpError（经 requestOk 解析结构化原因并记日志）；网络层失败原样上抛，
 * 调用方需自行区分「后端明确拒绝」与「结果未知」。发送类命令不设超时：它们不幂等，超时后结果未知，
 * 重发会被重复受理。停止与工具调用命令幂等，带超时并把失败收敛成结果对象，界面可放心提供「重试」。
 */

import { jsonRequestInit, requestOk, type HttpRequestInit } from "@/lib/http/client";
import { HttpError } from "@/lib/http/errors";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { frontendLog } from "@/lib/logging/frontend-log";
import { newTraceId } from "@/lib/trace";

/** 停止、取消与转后台都幂等（重复投递后端回 409 或照常受理），超时后可以放心重试。 */
const IDEMPOTENT_COMMAND_TIMEOUT_MS = 15_000;

/** 用户消息正文（文本、图片与附件）。 */
export type TaskUserMessage = {
  role: "user";
  parts: Array<{ type: "text"; text: string } | { type: "image"; image: string }>;
  attachments?: Array<{ id: string; name: string; contentType: string; path: string }>;
};

/** 发送 / 编辑重跑共用的消息请求体（带发起配置），与后端 `SendMessageRequest` / `RerunMessageRequest` 对齐。 */
export type TaskMessageRequest = {
  message: TaskUserMessage;
  modelConfigId: number;
  reasoningEffort: string | null;
  /** 用户关掉的工具组名；后端按注册表展开为工具名。 */
  disabledToolGroups: readonly string[];
};

/** 用户对一次等待决定的工具调用（HITL）的决定；`data` 是批准时附带的工具自定义载荷，驳回只带 `feedback`。 */
export type ToolDecision = {
  decision: "approve" | "reject";
  feedback?: string;
  data?: Record<string, unknown>;
};

type CommandInit = Pick<HttpRequestInit, "signal" | "traceId">;

async function postTaskCommand(
  path: string,
  body: unknown,
  init: CommandInit = {},
  method: "POST" | "PATCH" | "DELETE" = "POST",
): Promise<void> {
  const requestInit: HttpRequestInit = { ...init, method };
  await requestOk(path, body === undefined ? requestInit : jsonRequestInit(body, requestInit));
}

/**
 * 发送消息。并入时机由后端决定：Task 有活动 Run（含挂起）时排队，Run 结束后一轮一条依次处理；
 * 空闲时立即处理。排队状态经 snapshot 的 `queued_messages` 投影。
 */
export function sendTaskMessage(
  taskId: number,
  request: TaskMessageRequest,
  init?: CommandInit,
): Promise<void> {
  return postTaskCommand(`/tasks/${taskId}/messages`, request, init);
}

function queuedMessagePath(taskId: number, messageId: string): string {
  return `/tasks/${taskId}/queued-messages/${encodeURIComponent(messageId)}`;
}

/**
 * 排队消息改为立即处理：运行中并入当前 Run，挂起时唤醒，空闲时开新 Run。后端以新身份重新入队，
 * 原条目从队列投影里消失。已被并入时 409 `MESSAGE_CONSUMED`。
 */
export function sendQueuedTaskMessageNow(taskId: number, messageId: string, init?: CommandInit): Promise<void> {
  return postTaskCommand(`${queuedMessagePath(taskId, messageId)}/send-now`, undefined, init);
}

/**
 * 删除排队消息（编辑排队消息也走它：先撤回，再把内容放回输入框）。已不在队列中（已被并入或已删除）
 * 时 409 `MESSAGE_CONSUMED`。
 */
export function deleteQueuedTaskMessage(taskId: number, messageId: string, init?: CommandInit): Promise<void> {
  return postTaskCommand(queuedMessagePath(taskId, messageId), undefined, init, "DELETE");
}

/** 编辑最新一条已并入的用户消息并重跑：只在 Task 静止时受理（运行中 409 `RUN_NOT_STOPPED`），按本次配置开新 Run。 */
export function rerunTaskMessage(
  taskId: number,
  messageId: string,
  request: TaskMessageRequest,
  init?: CommandInit,
): Promise<void> {
  return postTaskCommand(`/tasks/${taskId}/messages/${encodeURIComponent(messageId)}/rerun`, request, init);
}

/** 最新 Run 已取消或失败时续跑同一个 Run。 */
export function resumeTask(taskId: number, init?: CommandInit): Promise<void> {
  return postTaskCommand(`/tasks/${taskId}/resume`, undefined, init);
}

/** 对一次等待决定的工具调用作出决定（按工具调用寻址，与具体工具无关）；后续输出经 Task 长订阅到达。 */
export function submitToolDecision(
  taskId: number,
  toolCallId: string,
  decision: ToolDecision,
  init?: CommandInit,
): Promise<void> {
  return postTaskCommand(`/tasks/${taskId}/tool-calls/${encodeURIComponent(toolCallId)}/decision`, decision, init);
}

/**
 * 幂等命令的失败文案：优先用后端给出的可读原因（如「任务不存在」），没有时按「被拒绝 / 未确认」兜底；
 * 不向用户展示裸 HTTP 状态码。
 */
function commandFailureMessage(error: unknown, action: string): string {
  return userErrorMessage(
    error,
    error instanceof HttpError ? `${action}被后端拒绝。` : `${action}未能确认，请检查本机后端连接。`,
  );
}

function isConflict(error: unknown): boolean {
  return error instanceof HttpError && error.status === 409;
}

export type CancelTaskResult =
  | { accepted: true }
  | { accepted: false; reason: "rejected" | "network"; message: string };

/**
 * 停止 Task 当前执行。按 taskId 取消：同一 Task 内 Run 串行，后端命中的永远是当前执行。命令只投递
 * （202）：运行中的执行在后端入口同步打取消信号，挂起的 Run 由调度 actor 直接迁移；是否真的停下、
 * 停成什么状态一律以 Task 长订阅里的 `activity` 为准，这里不判断「有没有可停止的对象」。
 */
export async function cancelTask(taskId: number): Promise<CancelTaskResult> {
  try {
    await requestOk(`/tasks/${taskId}/cancel`, { method: "POST", timeoutMs: IDEMPOTENT_COMMAND_TIMEOUT_MS });
    return { accepted: true };
  } catch (error) {
    return {
      accepted: false,
      reason: error instanceof HttpError ? "rejected" : "network",
      message: commandFailureMessage(error, "停止请求"),
    };
  }
}

export type CancelToolCallResult =
  | { kind: "signalled" }
  | { kind: "already_signalled" }
  | { kind: "failed"; reason: "rejected" | "network"; message: string };

/**
 * 取消一次工具调用：登记了延迟结果的调用（前台等待或后台工作）不要求 Run 在执行，其余只命中 Task
 * 当前执行中的调用。调用成功不代表工具已经结束。后端 409 表示已没有可取消的对象（已标记过、调用已
 * 结束或 Task 没有执行），对界面都意味着无需再次请求，统一视为 already_signalled。
 */
export async function cancelToolCall(taskId: number, toolCallId: string): Promise<CancelToolCallResult> {
  const traceId = newTraceId();
  void frontendLog("INFO", "assistant_tool_call_cancel_requested", "用户请求取消工具调用", {
    traceId,
    data: { taskId, toolCallId },
  });
  let result: CancelToolCallResult;
  try {
    await requestOk(`/tasks/${taskId}/tool-calls/${encodeURIComponent(toolCallId)}/cancel`, {
      method: "POST",
      traceId,
      timeoutMs: IDEMPOTENT_COMMAND_TIMEOUT_MS,
    });
    result = { kind: "signalled" };
  } catch (error) {
    result = isConflict(error)
      ? { kind: "already_signalled" }
      : {
        kind: "failed",
        reason: error instanceof HttpError ? "rejected" : "network",
        message: commandFailureMessage(error, "停止请求"),
      };
  }
  void frontendLog(result.kind === "failed" ? "WARNING" : "INFO", "assistant_tool_call_cancel_result", "工具调用取消请求已返回", {
    traceId,
    data: { taskId, toolCallId, result: result.kind, reason: result.kind === "failed" ? result.reason : null },
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
  let result: BackgroundToolCallResult;
  try {
    await requestOk(`/tasks/${taskId}/tool-calls/${encodeURIComponent(toolCallId)}/background`, {
      method: "POST",
      traceId,
      timeoutMs: IDEMPOTENT_COMMAND_TIMEOUT_MS,
    });
    result = { kind: "detached" };
  } catch (error) {
    result = isConflict(error)
      ? { kind: "not_detachable" }
      : { kind: "failed", message: commandFailureMessage(error, "转到后台") };
  }
  void frontendLog(result.kind === "failed" ? "WARNING" : "INFO", "assistant_tool_call_background_result", "转到后台请求已返回", {
    traceId,
    data: { taskId, toolCallId, result: result.kind },
  });
  return result;
}

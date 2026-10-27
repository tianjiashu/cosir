/**
 * Task 级业务命令客户端：发送、编辑排队消息、编辑重跑、恢复与用户决定。
 *
 * 命令只对 Task（或消息）寻址，不携带 runId：Run 身份与调度规则（立即并入、排队、新开 Run、
 * 唤醒挂起的 Run）全部由后端 Task 调度 actor 决定。后端处理完这一轮才返回：200 表示已受理
 * （新 Run 已启动、已并入或已排队），响应体为空对象；状态冲突（不可恢复、重跑目标已过期等）为
 * 409。Run 输出与状态经 Task 长订阅的 snapshot 到达，本模块不读取 SSE。
 *
 * 异常：非 2xx 抛 HttpError（经 parseHttpError 解析结构化原因）；网络层失败原样上抛，调用方
 * 需自行区分「后端明确拒绝」与「结果未知」。
 */

import { jsonRequestInit, requestRaw, type HttpRequestInit } from "@/lib/http/client";
import { parseHttpError } from "@/lib/http/errors";
import type { InputDelivery } from "@/lib/assistant/contract";

/** 用户消息正文；编辑排队消息只带它，与后端 `EditQueuedMessageRequest` 对齐。 */
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
  banTools: readonly string[];
};

/** 用户对一个待确认 Agent Team 方案的决定；`data` 批准时为最终运行输入，驳回时带 `feedback`。 */
export type TeamDecision = {
  teamRunId: number;
  decision: "approve" | "reject";
  data?: Record<string, unknown>;
};

type CommandInit = Pick<HttpRequestInit, "signal" | "traceId">;

async function postTaskCommand(
  path: string,
  body: unknown,
  init: CommandInit = {},
  method: "POST" | "PATCH" = "POST",
): Promise<void> {
  const requestInit: HttpRequestInit = { ...init, method };
  const response = await requestRaw(path, body === undefined ? requestInit : jsonRequestInit(body, requestInit));
  if (!response.ok) throw await parseHttpError(response);
}

export function sendTaskMessage(
  taskId: number,
  request: TaskMessageRequest & { delivery: InputDelivery },
  init?: CommandInit,
): Promise<void> {
  return postTaskCommand(`/tasks/${taskId}/messages`, request, init);
}

/**
 * 原地编辑排队中（未并入）的用户消息：只改正文与附件，发起配置与投递方式沿用发送时的值，不影响 Run。
 * 消息已被并入时后端 409 `MESSAGE_CONSUMED`，不会转成重跑。
 */
export function editQueuedTaskMessage(
  taskId: number,
  messageId: string,
  message: TaskUserMessage,
  init?: CommandInit,
): Promise<void> {
  return postTaskCommand(
    `/tasks/${taskId}/queued-messages/${encodeURIComponent(messageId)}`,
    { message },
    init,
    "PATCH",
  );
}

/** 编辑最新一条已并入的用户消息并重跑：运行中后端先停止当前 Run，按本次配置开新 Run。 */
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

/** 对待确认的 Agent Team 方案作出决定；后端执行决定后以消息唤醒主 Agent，后续输出经 Task 长订阅到达。 */
export function submitTeamDecision(taskId: number, decision: TeamDecision, init?: CommandInit): Promise<void> {
  return postTaskCommand(`/tasks/${taskId}/decisions`, decision, init);
}

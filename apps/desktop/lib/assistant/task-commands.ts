/**
 * Task 级业务命令客户端：发送、编辑、恢复与用户决定。
 *
 * 命令只对 Task（或消息）寻址，不携带 runId：Run 身份与调度规则（立即并入、排队、新开 Run、
 * 从 wait_user 继续）全部由后端 WorkflowSession 决定。2xx 回执即 accepted barrier，携带提交后
 * 的 canonical snapshot；后续输出由调用方另行 attach 订阅，本模块不读取 SSE。
 *
 * 异常：非 2xx 抛 HttpError（经 parseHttpError 解析结构化原因）；网络层失败原样上抛，调用方
 * 需自行区分「后端明确拒绝」与「结果未知」。
 */

import { jsonRequestInit, requestRaw, type HttpRequestInit } from "@/lib/http/client";
import { parseHttpError } from "@/lib/http/errors";
import type { InputDelivery, TransportState } from "@/lib/assistant/contract";
import { parseTransportState } from "@/lib/assistant/snapshot-validation";

/** 发送 / 编辑共用的消息请求体，与后端 `SendMessageRequest` / `EditMessageRequest` 对齐。 */
export type TaskMessageRequest = {
  message: {
    role: "user";
    parts: Array<{ type: "text"; text: string } | { type: "image"; image: string }>;
    attachments?: Array<{ id: string; name: string; contentType: string; path: string }>;
  };
  modelConfigId: number;
  reasoningEffort: string | null;
  banTools: readonly string[];
};

/** 用户对一次等待请求作出的决定（与后端 `UserDecisionKind` 对齐，不含系统合成的 force_reject）。 */
export type TaskDecision = {
  requestId: string;
  decision: "approve" | "reject" | "abort";
  data?: Record<string, unknown>;
};

/** `queued` 为真表示消息只进了 inbox，本次没有启动或恢复执行。 */
export type TaskCommandAccepted = {
  state: TransportState;
  queued: boolean;
};

type CommandInit = Pick<HttpRequestInit, "signal" | "traceId">;

async function postTaskCommand(path: string, body: unknown, init: CommandInit = {}): Promise<TaskCommandAccepted> {
  const requestInit: HttpRequestInit = { ...init, method: "POST" };
  const response = await requestRaw(path, body === undefined ? requestInit : jsonRequestInit(body, requestInit));
  if (!response.ok) throw await parseHttpError(response);
  const accepted = await response.json() as { state: unknown; queued: boolean };
  return { state: parseTransportState(accepted.state), queued: accepted.queued };
}

export function sendTaskMessage(
  taskId: number,
  request: TaskMessageRequest & { delivery: InputDelivery },
  init?: CommandInit,
): Promise<TaskCommandAccepted> {
  return postTaskCommand(`/tasks/${taskId}/messages`, request, init);
}

/** 编辑最新一条已消费用户消息；后端只在 Task 已到终态时接受。 */
export function editTaskMessage(
  taskId: number,
  messageId: string,
  request: TaskMessageRequest,
  init?: CommandInit,
): Promise<TaskCommandAccepted> {
  return postTaskCommand(`/tasks/${taskId}/messages/${encodeURIComponent(messageId)}/edit`, request, init);
}

/** 最新 Run 已取消或失败时从模型节点续跑。 */
export function resumeTask(taskId: number, init?: CommandInit): Promise<TaskCommandAccepted> {
  return postTaskCommand(`/tasks/${taskId}/resume`, undefined, init);
}

/** 回答等待中的审批请求，Run 从 wait_user 节点继续；调用方随后需重新 attach 才能收到输出。 */
export function submitTaskDecision(taskId: number, decision: TaskDecision, init?: CommandInit): Promise<TaskCommandAccepted> {
  return postTaskCommand(`/tasks/${taskId}/decisions`, decision, init);
}

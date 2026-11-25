import type { TerminalPermissionMode } from "@/lib/assistant/contract";
/**
 * Session 级业务命令客户端：发送、排队消息的立即发送 / 删除（编辑排队消息即删除后放回输入框，不是独立命令）、
 * 撤回（编辑即撤回后放回输入框，不是独立命令）、恢复、用户决定、停止 Session，以及按 `tool_call_id` 寻址的
 * 取消工具调用与转到后台。
 *
 * 命令只对 Session（或消息）寻址，不携带 Run 身份：调度规则（立即并入、排队、新开 Run、
 * 唤醒挂起的 Run）全部由后端 Session 调度 actor 决定。后端处理完这次命令才返回：200 表示已受理
 * （发送类为消息已持久化入队，开新 Run、并入或唤醒由 actor 随后推进），响应体为空对象；状态冲突（不可恢复、撤回目标已过期等）为
 * 409。Run 输出与状态经 Session 长订阅的 snapshot 到达，本模块不读取 SSE。
 *
 * 异常：发送类命令非 2xx 抛 HttpError（经 requestOk 解析结构化原因并记日志）；网络层失败原样上抛，
 * 调用方需自行区分「后端明确拒绝」与「结果未知」。发送类命令不设超时：它们不幂等，超时后结果未知，
 * 重发会被重复受理。停止与工具调用命令幂等，带超时并把失败收敛成结果对象，界面可放心提供「重试」。
 */

import { jsonRequestInit, requestOk, type HttpRequestInit } from "@/lib/http/client";
import { HttpError } from "@/lib/http/http-error";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { frontendLog } from "@/lib/logging/frontend-log";
import { approvalTarget, backgroundTarget, beginCommand, decisionTarget, finishCommand, stopTarget, type CommandTarget } from "@/lib/assistant/command-request-store";
import { newTraceId } from "@/lib/logging/trace-id";

/**
 * 占用命令暂态槽位的命令（停止、转后台、决定、批准）都幂等：重复投递后端回 409 或照常受理，超时后可以放心重试。
 * 时限只在 {@link runSlotCommand} 一处施加。
 */
const IDEMPOTENT_COMMAND_TIMEOUT_MS = 15_000;

/**
 * 用户消息正文：文本（带 `<file>` / `<ref>` / `<skill>` 胶囊标签）与已上传图片的 locator。
 * 本机文件、工作区引用与 Skill 都只是正文里的指针，不再有旁挂附件列表。
 */
type SessionUserMessage = {
  role: "user";
  parts: Array<{ type: "text"; text: string } | { type: "image"; image: string }>;
};

/** 发送消息的请求体（带发起配置），与后端 `SendMessageRequest` 对齐。 */
export type SessionMessageRequest = {
  message: SessionUserMessage;
  modelConfigId: string;
  reasoningEffort: string | null;
  /** 用户要求加入队列（⌘回车）；只是意图，后端在空闲、等回答或有终端审批时仍立即处理。 */
  queue: boolean;
  /** 乐观消息的命令 id：后端存下并在快照里原样带回，前端据此对账（`reconcilePending`）。 */
  clientMessageId: string;
};

/**
 * 用户对一次等待决定的工具调用（HITL）的决定；驳回可带 `feedback`。`data` 是工具声明的结构化回答
 * （如提问的各题答案），形状由后端按工具校验（400 `DECISION_DATA_INVALID`），这里原样带上。
 */
export type ToolDecision = {
  decision: "approve" | "reject";
  feedback?: string;
  data?: Record<string, unknown>;
};

type CommandInit = Pick<HttpRequestInit, "signal" | "traceId">;

async function postSessionCommand(
  path: string,
  body: unknown,
  init: CommandInit = {},
  method: "POST" | "PATCH" | "DELETE" = "POST",
): Promise<void> {
  const requestInit: HttpRequestInit = { ...init, method };
  await requestOk(path, body === undefined ? requestInit : jsonRequestInit(body, requestInit));
}

/**
 * 发送消息。默认立即处理：空闲开新 Run，运行中插话（下一步并入当前 Run）；`queue` 时有活动 Run 就排到它结束后，
 * 每个 Run 一条依次处理。还没并入的消息经 snapshot 的 `queued_messages` 投影，`delivery` 区分插话与排队。
 */
export function sendSessionMessage(
  sessionId: number,
  request: SessionMessageRequest,
  init?: CommandInit,
): Promise<void> {
  return postSessionCommand(`/sessions/${sessionId}/messages`, request, init);
}

/** 切换会话的终端权限：随时可切，下一条命令生效；新值经 snapshot 推回（409 `TERMINAL_SANDBOX_UNAVAILABLE`）。 */
export function setTerminalPermission(sessionId: number, mode: TerminalPermissionMode): Promise<void> {
  return postSessionCommand(`/sessions/${sessionId}/terminal-permission`, { mode });
}

/**
 * 对页面上一条终端批准作出决定（`sessionId` 是显示批准的会话，不一定是发起命令的会话）。进行中状态在
 * command-request-store 的 `approval:` 槽位，卡片切走再回来不丢；已决定或已作废时 409，卡片随 snapshot 消失。
 */
export async function decideTerminalApproval(sessionId: number, requestId: string, decision: "approve" | "reject"): Promise<void> {
  const path = `/sessions/${sessionId}/terminal-approvals/${encodeURIComponent(requestId)}/decision`;
  await runSlotCommand(sessionId, approvalTarget(requestId), "提交批准", (init) => postSessionCommand(path, { decision }, init));
}

function queuedMessagePath(sessionId: number, messageId: string): string {
  return `/sessions/${sessionId}/queued-messages/${encodeURIComponent(messageId)}`;
}

/**
 * 立即发送一条还没并入的消息：排队的原地转为插话（身份不变，从排队条移到对话流），并停下当前
 * 执行中的工具调用，让模型下一步就看到它；挂起时唤醒，空闲时开新 Run。已被并入时 409 `MESSAGE_CONSUMED`。
 */
export function sendQueuedSessionMessageNow(sessionId: number, messageId: string, init?: CommandInit): Promise<void> {
  return postSessionCommand(`${queuedMessagePath(sessionId, messageId)}/send-now`, undefined, init);
}

/**
 * 删除排队消息（编辑排队消息也走它：先删除，再把内容放回输入框）。已不在队列中（已被并入或已删除）
 * 时 409 `MESSAGE_CONSUMED`。
 */
export function deleteQueuedSessionMessage(sessionId: number, messageId: string, init?: CommandInit): Promise<void> {
  return postSessionCommand(queuedMessagePath(sessionId, messageId), undefined, init, "DELETE");
}

/**
 * 撤回最新一条已并入的用户消息开启的回合：只在 Session 静止时受理（运行中 409 `RUN_NOT_STOPPED`，已不是
 * 最新一条 409 `WITHDRAW_TARGET_STALE`），不开新 Run。编辑 = 撤回后把内容放回输入框再照常发送。
 */
export function withdrawSessionMessage(sessionId: number, messageId: string, init?: CommandInit): Promise<void> {
  return postSessionCommand(`/sessions/${sessionId}/messages/${encodeURIComponent(messageId)}/withdraw`, undefined, init);
}

/** 最新 Run 已取消或失败时续跑同一个 Run。 */
export function resumeSession(sessionId: number, init?: CommandInit): Promise<void> {
  return postSessionCommand(`/sessions/${sessionId}/resume`, undefined, init);
}

/** 对一次等待决定的工具调用作出决定（按工具调用寻址，与具体工具无关）；后续输出经 Session 长订阅到达。 */
export async function submitToolDecision(
  sessionId: number,
  toolCallId: string,
  decision: ToolDecision,
  init?: CommandInit,
): Promise<void> {
  const path = `/sessions/${sessionId}/tool-calls/${encodeURIComponent(toolCallId)}/decision`;
  await runSlotCommand(sessionId, decisionTarget(toolCallId), "提交决定", (bounded) => postSessionCommand(path, decision, bounded), init);
}

/**
 * 占用命令暂态槽位的请求骨架：开始 → 发送（统一施加 {@link IDEMPOTENT_COMMAND_TIMEOUT_MS}）→ 结算。
 *
 * 槽位的不变量是「进行中不重复发送、结果未知时可以重试」，请求必须有界：一直不返回的请求会让槽位永远停在
 * 进行中、按钮永远锁住，所以时限在这里施加，不交给各命令自己记得。同一槽位已有请求在途时不发送，返回
 * `false`；后端拒绝（HttpError）释放槽位，其余失败（网络、超时）标为待确认、可重试。失败结算后原样抛出，
 * 由调用方决定怎么呈现。
 */
async function runSlotCommand(
  sessionId: number,
  target: CommandTarget,
  action: string,
  send: (init: HttpRequestInit) => Promise<void>,
  init: CommandInit = {},
): Promise<boolean> {
  const request = beginCommand(sessionId, target);
  if (!request) return false;
  try {
    await send({ ...init, timeoutMs: IDEMPOTENT_COMMAND_TIMEOUT_MS });
  } catch (error) {
    finishCommand(sessionId, target, request, error instanceof HttpError ? "rejected" : "unknown", commandFailureMessage(error, action));
    throw error;
  }
  finishCommand(sessionId, target, request, "accepted");
  return true;
}

/**
 * 幂等命令的失败文案：优先用后端给出的可读原因（如「会话不存在」），没有时按「被拒绝 / 未确认」兜底；
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

export type CancelSessionResult =
  | { accepted: true }
  | { accepted: false; reason: "rejected" | "network"; message: string };

/**
 * 停止 Session 当前执行。按 sessionId 取消：同一 Session 内 Run 串行，后端命中的永远是当前执行。命令只投递
 * （202）：运行中的执行在后端入口同步打取消信号，挂起的 Run 由调度 actor 直接迁移；是否真的停下、
 * 停成什么状态一律以 Session 长订阅里的 `activity` 为准，这里不判断「有没有可停止的对象」。
 */
export async function cancelSession(sessionId: number): Promise<CancelSessionResult> {
  try {
    await runSlotCommand(sessionId, "session", "停止请求", (init) => postSessionCommand(`/sessions/${sessionId}/cancel`, undefined, init));
    return { accepted: true };
  } catch (error) {
    return {
      accepted: false,
      reason: error instanceof HttpError ? "rejected" : "network",
      message: commandFailureMessage(error, "停止请求"),
    };
  }
}

/**
 * 工具调用级命令（停止、转后台）的请求骨架：成功返回 null，失败返回可展示的原因。
 *
 * 409 表示已没有可操作的对象（调用已结束、已转走），对界面都意味着无需再做，算不上失败。调用成功
 * 不代表工具已经结束，结局经 snapshot 到达。
 */
async function postCallCommand(
  sessionId: number,
  toolCallId: string,
  target: CommandTarget,
  path: "cancel" | "background",
  action: string,
): Promise<string | null> {
  const url = `/sessions/${sessionId}/tool-calls/${encodeURIComponent(toolCallId)}/${path}`;
  const traceId = newTraceId();
  let failure: string | null = null;
  try {
    const sent = await runSlotCommand(sessionId, target, action, (init) => {
      void frontendLog("INFO", "assistant_tool_call_command_requested", "用户对工具调用发起命令", { traceId, data: { sessionId, toolCallId, action: path } });
      return postSessionCommand(url, undefined, init);
    }, { traceId });
    if (!sent) return null;
  } catch (error) {
    if (!isConflict(error)) failure = commandFailureMessage(error, action);
  }
  void frontendLog(failure ? "WARNING" : "INFO", "assistant_tool_call_command_result", "工具调用命令已返回", { traceId, data: { sessionId, toolCallId, action: path, failed: failure !== null } });
  return failure;
}

/** 取消一次工具调用：登记了后台结果的调用不要求 Run 在执行，其余只命中 Session 当前执行中的调用。 */
export function cancelToolCall(sessionId: number, toolCallId: string): Promise<string | null> {
  return postCallCommand(sessionId, toolCallId, stopTarget(toolCallId), "cancel", "停止请求");
}

/** 把一次前台等待转到后台：工具立即以「已转后台」占位闭合，结论稍后回投。 */
export function backgroundToolCall(sessionId: number, toolCallId: string): Promise<string | null> {
  return postCallCommand(sessionId, toolCallId, backgroundTarget(toolCallId), "background", "转到后台");
}

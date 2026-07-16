/**
 * 工具审批 HTTP 服务封装。
 *
 * @module services/approvals
 */

import {
  API_PATHS,
  type ApprovalDecisionRequest,
  type ApprovalDecisionResponse,
  type ApprovalsResponse,
} from "@shared/api";
import { logError, logInfo } from "../lib/logger";
import { ServiceError } from "./types";
import {
  buildTraceHeaders,
  readBackendTraceHeaders,
  recordBackendTrace,
} from "./tracePropagation";
import { useConversationTraceStore } from "@/stores/conversationTraceStore";

/** 后端基础 URL，开发环境走 Vite 代理。 */
const BASE_URL = "";

/**
 * 查询任务待处理审批请求。
 *
 * @param taskId - 任务标识符。
 * @returns 待处理审批请求列表。
 * @throws {ServiceError} 当后端不可达或返回非 2xx 状态码时抛出。
 *
 * @sideeffect 发起 GET /tasks/{id}/approvals 请求并记录日志。
 */
export async function fetchPendingApprovals(taskId: string): Promise<ApprovalsResponse> {
  const path = API_PATHS.TASK_APPROVALS(taskId);
  const requestTrace = buildTraceHeaders({ taskId });
  useConversationTraceStore.getState().recordTrace({
    traceId: requestTrace.trace.traceId,
    taskId,
    approvalId: "",
    operation: "task_approvals",
    method: "GET",
    path,
  });
  const requestContext = {
    module: "approvals",
    task_id: taskId,
    method: "GET",
    path,
    trace_id: requestTrace.trace.traceId,
  };
  try {
    const response = await fetch(`${BASE_URL}${path}`, {
      headers: { ...requestTrace.headers },
    });
    recordBackendTrace(readBackendTraceHeaders(response, requestTrace.trace.traceId));
    if (!response.ok) {
      throw new ServiceError(`查询审批请求失败: HTTP ${response.status}`, {
        statusCode: response.status,
        taskId,
      });
    }
    const approvals = (await response.json()) as ApprovalsResponse;
    logInfo("查询审批请求完成", { ...requestContext, count: approvals.length });
    return approvals;
  } catch (err) {
    logError("查询审批请求失败", err, requestContext);
    throw err;
  }
}

/**
 * 提交审批决策。
 *
 * @param approvalId - 审批请求标识符。
 * @param request - 审批决策请求体。
 * @returns 审批决策响应。
 * @throws {ServiceError} 当后端不可达或返回非 2xx 状态码时抛出。
 *
 * @sideeffect 发起 POST /approvals/{id}/decision 请求并记录日志。
 */
export async function submitApprovalDecision(
  approvalId: string,
  request: ApprovalDecisionRequest,
): Promise<ApprovalDecisionResponse> {
  const path = API_PATHS.APPROVAL_DECISION(approvalId);
  const requestTrace = buildTraceHeaders();
  useConversationTraceStore.getState().recordTrace({
    traceId: requestTrace.trace.traceId,
    taskId: requestTrace.trace.taskId ?? "",
    approvalId,
    operation: "approval_decision",
    method: "POST",
    path,
  });
  const requestContext = {
    module: "approvals",
    approval_id: approvalId,
    method: "POST",
    path,
    trace_id: requestTrace.trace.traceId,
  };
  try {
    const response = await fetch(`${BASE_URL}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...requestTrace.headers },
      body: JSON.stringify(request),
    });
    recordBackendTrace(readBackendTraceHeaders(response, requestTrace.trace.traceId));
    if (!response.ok) {
      throw new ServiceError(`提交审批决策失败: HTTP ${response.status}`, {
        statusCode: response.status,
      });
    }
    const decision = (await response.json()) as ApprovalDecisionResponse;
    logInfo("提交审批决策完成", {
      ...requestContext,
      decision: request.decision,
    });
    return decision;
  } catch (err) {
    logError("提交审批决策失败", err, requestContext);
    throw err;
  }
}

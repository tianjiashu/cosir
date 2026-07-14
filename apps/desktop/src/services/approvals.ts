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
  try {
    const response = await fetch(`${BASE_URL}${API_PATHS.TASK_APPROVALS(taskId)}`);
    if (!response.ok) {
      throw new ServiceError(`查询审批请求失败: HTTP ${response.status}`, {
        statusCode: response.status,
        taskId,
      });
    }
    const approvals = (await response.json()) as ApprovalsResponse;
    logInfo("查询审批请求完成", { module: "approvals", taskId, count: approvals.length });
    return approvals;
  } catch (err) {
    logError("查询审批请求失败", err, { module: "approvals", taskId });
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
  try {
    const response = await fetch(`${BASE_URL}${API_PATHS.APPROVAL_DECISION(approvalId)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request),
    });
    if (!response.ok) {
      throw new ServiceError(`提交审批决策失败: HTTP ${response.status}`, {
        statusCode: response.status,
      });
    }
    const decision = (await response.json()) as ApprovalDecisionResponse;
    logInfo("提交审批决策完成", {
      module: "approvals",
      approvalId,
      decision: request.decision,
    });
    return decision;
  } catch (err) {
    logError("提交审批决策失败", err, { module: "approvals", approvalId });
    throw err;
  }
}

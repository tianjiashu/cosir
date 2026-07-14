/**
 * Durable Run State HTTP 服务封装。
 *
 * @module services/runs
 */

import { API_PATHS, type RecoverableRunsResponse } from "@shared/api";
import { logError, logInfo } from "../lib/logger";
import { ServiceError } from "./types";

/** 后端基础 URL，开发环境走 Vite 代理。 */
const BASE_URL = "";

/**
 * 查询可恢复运行列表。
 *
 * @returns 可恢复运行记录列表。
 * @throws {ServiceError} 当后端不可达或返回非 2xx 状态码时抛出。
 *
 * @sideeffect 发起 GET /runs/recoverable 请求并记录成功/失败日志。
 */
export async function fetchRecoverableRuns(): Promise<RecoverableRunsResponse> {
  const startedAt = performance.now();
  try {
    const response = await fetch(`${BASE_URL}${API_PATHS.RUNS_RECOVERABLE}`);
    if (!response.ok) {
      throw new ServiceError(`查询可恢复运行失败: HTTP ${response.status}`, {
        statusCode: response.status,
      });
    }
    const runs = (await response.json()) as RecoverableRunsResponse;
    logInfo("查询可恢复运行完成", {
      module: "runs",
      count: runs.length,
      durationMs: Math.round(performance.now() - startedAt),
    });
    return runs;
  } catch (err) {
    logError("查询可恢复运行失败", err, { module: "runs" });
    throw err;
  }
}

/**
 * 请求后端取消运行关联任务。
 *
 * @param taskId - 需要取消的任务标识符。
 * @returns 后端返回的任务状态。
 * @throws {ServiceError} 当后端不可达或返回非 2xx 状态码时抛出。
 *
 * @sideeffect 发起 POST /tasks/{id}/cancel 请求并记录日志。
 */
export async function cancelRunTask(taskId: string): Promise<unknown> {
  try {
    const response = await fetch(`${BASE_URL}${API_PATHS.TASK_CANCEL(taskId)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    if (!response.ok) {
      throw new ServiceError(`取消运行失败: HTTP ${response.status}`, {
        statusCode: response.status,
        taskId,
      });
    }
    logInfo("取消运行完成", { module: "runs", taskId });
    return response.json();
  } catch (err) {
    logError("取消运行失败", err, { module: "runs", taskId });
    throw err;
  }
}

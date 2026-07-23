/**
 * 后端 API 共享类型定义。
 *
 * 本文件由 `scripts/generate_api_ts.py` 从后端 Pydantic API schema 生成。
 * 不要手动修改；请先更新 `apps/backend/app/api/schemas/` 后重新生成。
 *
 * @module shared/logs
 */

export type LogLevel = string;

export interface QueryLogsRequest {
  trace_id: string;
  level?: string;
  start_time?: string;
  end_time?: string;
  limit?: number;
}

export interface RecentLogsRequest {
  level?: string;
  start_time?: string;
  end_time?: string;
  limit?: number;
}

export interface LogError extends Record<string, unknown> {
  type?: string;
  message?: string;
  stack?: string;
}

export interface LogEntryResponse {
  ts: string;
  level: string;
  logger: string;
  trace_id: string;
  caller: string;
  event: string;
  msg: string;
  data?: Record<string, unknown>;
  error?: LogError | null;
  truncated?: boolean;
}

export interface LogQueryResponse {
  entries: LogEntryResponse[];
  text: string;
}

export interface LogQueryRequest {
  trace_id?: string;
  level?: string;
  start_time?: string;
  end_time?: string;
  limit?: number;
}

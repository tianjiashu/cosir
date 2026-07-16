/**
 * 后端日志查询共享类型。
 *
 * @module shared/logs
 */

/** Python 标准日志级别。 */
export type LogLevel = "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL";

/** 单条结构化日志。 */
export interface LogEntry {
  /** UTC RFC3339 日志时间。 */
  ts: string;
  /** 日志级别。 */
  level: LogLevel;
  /** logger 名称。 */
  logger_name: string;
  /** 稳定事件名。 */
  event_name: string;
  /** 人类可读展示文本。 */
  message: string;
  /** 前端操作 trace 标识。 */
  trace_id: string;
  /** 任务标识。 */
  task_id: string;
  /** Durable Run 标识。 */
  run_id: string;
  /** Trace span 标识。 */
  span_id: string;
  /** 事件标识。 */
  event_id: string;
  /** 步骤标识。 */
  step_id: string;
  /** 工具调用标识。 */
  tool_call_id: string;
  /** 审批标识。 */
  approval_id: string;
  /** 可变业务字段。 */
  attributes: Record<string, unknown>;
  /** 异常类型。 */
  error_type: string;
  /** 异常消息。 */
  error_message: string;
  /** 异常栈。 */
  stack: string;
  /** 是否发生截断。 */
  truncated: boolean;
}

/** 日志查询请求参数。 */
export interface LogQueryRequest {
  /** trace 查询时必填。 */
  trace_id?: string;
  /** 可选级别过滤。 */
  level?: LogLevel;
  /** 可选任务过滤。 */
  task_id?: string;
  /** 可选 run 过滤。 */
  run_id?: string;
  /** 可选起始 UTC RFC3339 时间。 */
  start_time?: string;
  /** 可选结束 UTC RFC3339 时间。 */
  end_time?: string;
  /** 可选返回数量。 */
  limit?: number;
}

/** 日志查询响应。 */
export interface LogQueryResponse {
  /** 结构化日志列表。 */
  entries: LogEntry[];
  /** 后端渲染的纯文本日志。 */
  text: string;
}

/**
 * 后端日志查询共享类型。
 *
 * @module shared/logs
 */

/** Python 标准日志级别。 */
export type LogLevel = "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL";

/** 嵌套错误现场块。 */
export interface LogError {
  /** 异常类型名。 */
  type: string;
  /** 异常消息。 */
  message: string;
  /** 异常堆栈文本。 */
  stack: string;
}

/** 单条结构化日志（统一 9 字段）。 */
export interface LogEntry {
  /** UTC RFC3339 日志时间。 */
  ts: string;
  /** 日志级别。 */
  level: LogLevel;
  /** 统一 logger 名称。 */
  logger: string;
  /** 唯一链路关联键。 */
  trace_id: string;
  /** 调用位置 模块:类.方法:行号。 */
  caller: string;
  /** 稳定英文事件名。 */
  event: string;
  /** 中文可读消息。 */
  msg: string;
  /** 结构化业务字段。 */
  data: Record<string, unknown>;
  /** 嵌套错误块，正常为 null。 */
  error: LogError | null;
  /** 是否发生截断。 */
  truncated: boolean;
}

/** 日志查询请求参数。 */
export interface LogQueryRequest {
  /** trace 查询时必填（日志层唯一链路键）。 */
  trace_id?: string;
  /** 可选级别过滤。 */
  level?: LogLevel;
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

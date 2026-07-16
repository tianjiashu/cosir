/**
 * 客户端到后端 trace 传导共享类型。
 *
 * 第一版只保留一个核心语义：`trace_id` 表示一次前端用户操作触发的完整链路。
 * HTTP、SSE、前端日志和后端日志都围绕同一个 `x-trace-id` 传递与查询。
 *
 * @module shared/tracePropagation
 */

/** 一次前端用户操作的 trace 上下文。 */
export interface ClientTraceContext {
  /** 32 位小写十六进制 trace ID。 */
  traceId: string;
  /** 当前 trace 首次创建时间。 */
  startedAt: string;
  /** 可选任务 ID，用于日志辅助定位。 */
  taskId?: string;
  /** 可选 run ID，用于日志辅助定位。 */
  runId?: string;
}

/** 发送给后端的 trace 请求头集合。 */
export interface TraceHeaders {
  /** 一次前端用户操作触发的完整链路 ID。 */
  "x-trace-id": string;
}

/** 后端响应中确认的 trace 信息。 */
export interface BackendTraceHeaders {
  /** 后端确认或生成的 trace ID。 */
  traceId: string;
}

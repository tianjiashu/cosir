/**
 * 服务层内部类型定义。
 *
 * 定义 HTTP 请求/响应的 TypeScript 类型，
 * 以及服务层内部的错误类型。对外契约类型使用 `@shared` 包。
 *
 * @module services/types
 */

/** 服务层统一错误类型，封装网络错误、HTTP 错误和解析错误。 */
export class ServiceError extends Error {
  /** HTTP 状态码（0 表示网络层错误或非 HTTP 异常）。 */
  public readonly statusCode: number;
  /** 关联的 task_id（如有）。 */
  public readonly taskId?: string;
  /** 原始错误对象（用于日志记录，不暴露给 UI）。 */
  public readonly cause?: unknown;

  constructor(
    message: string,
    options: { statusCode?: number; taskId?: string; cause?: unknown } = {},
  ) {
    super(message);
    this.name = "ServiceError";
    this.statusCode = options.statusCode ?? 0;
    this.taskId = options.taskId;
    this.cause = options.cause;
  }
}

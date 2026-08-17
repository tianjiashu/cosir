/**
 * 统一 HTTP 客户端（基于 ky）。
 *
 * 职责边界：
 * - 仅服务「请求-响应」型的普通 API（POST/GET/DELETE 等一次性拿到 JSON 结果的调用）。
 * - **不**服务 SSE 长连接：ky 不消费 ReadableStream，SSE 必须保留原生 fetch 由 api.ts
 *   的 connectWorkspaceEventStream 处理。
 * - 本文件只做「ky 实例配置 + 错误归一」，不写任何业务逻辑；超时、重试均使用 ky 标准 API。
 *
 * 错误归一策略：
 * - 经 beforeError 钩子，把 ky 抛出的 HTTPError / NetworkError / TimeoutError 统一转成
 *   项目内部的 ServiceError，调用方（api.ts）无需再手写状态码提取与 body.detail 解析。
 *
 * @module services/httpClient
 */

import ky, { HTTPError, isHTTPError } from "ky";
import { ServiceError } from "./types";

/**
 * 普通请求-响应型 API 的单一 ky 实例。
 *
 * 配置说明：
 * - prefixUrl: "" —— 开发环境走 Vite 代理，生产同源，路径直接相对当前 origin。
 * - timeout: 30000 —— 每轮尝试 30s 超时（重试不计入，totalTimeout 另控）。
 * - retry: 仍保留 methods（含 GET/HEAD/OPTIONS/PUT/DELETE）与 limit:2，
 *   但通过 statusCodes:[] 关闭对 HTTP 状态码的重试，并配合 shouldRetry
 *   仅在「请求根本没送达（isHTTPError 为假的网络/连接错误）」时重试。
 *   送达后的 4xx/5xx（含超时导致的 5xx）一律不重试，以避免级联删除
 *   （DELETE/PUT，如 deleteWorkspace/deleteTask）被重放造成二次破坏性副作用。
 * - throwHttpErrors: true —— 非 2xx 抛 HTTPError，由 beforeError 归一为 ServiceError。
 * - 个别超长请求（如 prepareWorkspace）可在调用处覆盖 timeout 为 false 或更大值。
 *
 * 注意：本实例只用于普通请求-响应型 API；SSE 必须使用原生 fetch。
 */
/**
 * 把 ky 抛出的任意错误归一为项目内部的 ServiceError。
 *
 * 归一规则（纯函数，便于单测，不依赖 ky 运行时）：
 * - HTTPError（isHTTPError 为真）：取 `error.response.status` 作为 statusCode；
 *   优先用响应体 `error.data.detail` 作为 message，缺失或非法时回退 `error.message`。
 * - 非 HTTP 错误（网络不可达 / 超时 / 其他）：statusCode 置 0，message 取 `error.message`。
 *
 * @param error - ky 抛出的错误对象（HTTPError / TimeoutError / 普通 Error 等）。
 * @returns 归一后的 ServiceError。
 */
export function normalizeToServiceError(error: unknown): ServiceError {
  if (isHTTPError(error)) {
    const statusCode = error.response.status;
    let detail: string = error.message;
    const body = (error as HTTPError).data;
    if (body && typeof body === "object" && "detail" in body) {
      const maybeDetail = (body as { detail?: unknown }).detail;
      if (typeof maybeDetail === "string" && maybeDetail.length > 0) {
        detail = maybeDetail;
      }
    }
    return new ServiceError(detail, { statusCode, cause: error });
  }

  const message = error instanceof Error ? error.message : String(error);
  return new ServiceError(message, { statusCode: 0, cause: error });
}

export const apiClient = ky.create({
  prefixUrl: "",
  timeout: 30000,
  retry: {
    limit: 2,
    methods: ["get", "head", "options", "put", "delete"],
    // 关闭对 HTTP 状态码的重试，仅对「无响应」的网络/连接错误重试，
    // 避免级联删除（DELETE/PUT，如 deleteWorkspace/deleteTask）被 5xx 超时重放
    // 造成二次破坏性副作用。送达后的 4xx/5xx 一律不重试。
    statusCodes: [],
    shouldRetry: (error) => !isHTTPError(error),
  },
  throwHttpErrors: true,
  hooks: {
    beforeError: [
      // ky v2 对 4xx/5xx 抛 HTTPError（含 .response.status），且会预解析响应体到 error.data。
      // 注意：HTTPError 构造函数已消费响应流，error.response.json() 不可用，必须读 error.data。
      async (error): Promise<ServiceError> => normalizeToServiceError(error),
    ],
  },
});

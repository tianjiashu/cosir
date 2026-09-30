export type StructuredHttpError = {
  code: string;
  message: string;
  retryable: boolean;
};

export class HttpError extends Error {
  readonly status: number;
  readonly code?: string;
  readonly retryable?: boolean;

  constructor(
    message: string,
    options: {
      status: number;
      code?: string;
      retryable?: boolean;
    },
  ) {
    super(message);
    this.name = "HttpError";
    this.status = options.status;
    this.code = options.code;
    this.retryable = options.retryable;
  }
}

/**
 * 解析本机后端的 HTTP 错误响应。
 *
 * 后端普通 HTTPException 通常使用 `detail`，Assistant Transport 错误则在
 * `detail.error` 或 `error` 中携带结构化错误。这里统一转换为 HttpError，
 * 调用方无需重复理解后端响应包装层。
 */
export async function parseHttpError(response: Response): Promise<HttpError> {
  const body = await response.text();

  try {
    const parsed = JSON.parse(body) as {
      detail?: unknown;
      error?: unknown;
    };
    const structured = parseStructuredHttpError(parsed);

    if (structured?.message) {
      return new HttpError(structured.message, {
        status: response.status,
        code: structured.code,
        retryable: structured.retryable,
      });
    }

    if (typeof parsed.detail === "string") {
      return new HttpError(parsed.detail, { status: response.status });
    }
  } catch {
    // 非 JSON 响应使用状态码作为安全的默认错误信息。
  }

  return new HttpError(`HTTP request failed with status ${response.status}`, {
    status: response.status,
  });
}

/**
 * 把 HTTP 层错误转换为用户可见文案。
 *
 * 与 `safeFrontendErrorMessage`（`@/lib/logging/frontend-log`）的区别：本函数透出后端给出的
 * 可行动原因（例如 409 的「模型配置名称已存在」），仅当缺少可读原因时才回退兜底文案；后者按
 * 安全默认值收敛，适合无法给出可行动信息的场景。两者职责不同，调用方按「用户能否据此纠正」
 * 选择，避免出现「后端已说明原因、界面只说请重试」。
 */
export function httpErrorMessage(error: unknown, fallback: string): string {
  return error instanceof HttpError && error.message ? error.message : fallback;
}

/** 从后端错误响应的不同包装形式中提取统一结构化错误。 */
export function parseStructuredHttpError(value: unknown): StructuredHttpError | null {
  if (!isRecord(value)) return null;

  if (isStructuredHttpError(value.error)) return value.error;

  if (isRecord(value.detail)) {
    if (isStructuredHttpError(value.detail.error)) return value.detail.error;
    if (isStructuredHttpError(value.detail)) return value.detail;
  }

  return null;
}

function isStructuredHttpError(value: unknown): value is StructuredHttpError {
  if (!isRecord(value)) return false;
  return typeof value.code === "string"
    && typeof value.message === "string"
    && typeof value.retryable === "boolean";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

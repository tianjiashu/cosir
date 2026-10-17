import { UserFacingError } from "@/lib/errors/user-facing-error";
import { getApiBaseUrl } from "@/lib/http/client";
import { HttpError, parseStructuredHttpError } from "@/lib/http/errors";
import { frontendLog } from "@/lib/logging/frontend-log";
import { newTraceId, type TraceId } from "@/lib/trace";

export type UploadedAttachment = {
  id: string;
  name: string;
  contentType: string;
  byteSize: number;
  width: number;
  height: number;
  locator: string;
  status: "ready";
};

const SUPPORTED_IMAGE_EXTENSIONS = new Set(["jpg", "jpeg", "png"]);
const UNSUPPORTED_IMAGE_EXTENSIONS = new Set(["gif", "webp", "bmp", "tif", "tiff"]);

/** 判断浏览器 MIME 是否属于后端附件协议支持的图片格式。 */
function isSupportedImageContentType(contentType: string): boolean {
  const normalized = contentType.toLowerCase();
  return normalized === "image/jpeg" || normalized === "image/png";
}

/** 判断文件是否明确属于当前不支持的图片格式。 */
export function isUnsupportedImageFile(file: Pick<File, "name" | "type">): boolean {
  const normalizedType = file.type.toLowerCase();
  if (normalizedType.startsWith("image/")) return !isSupportedImageContentType(normalizedType);
  const extension = file.name.split(".").pop()?.toLowerCase() ?? "";
  return UNSUPPORTED_IMAGE_EXTENSIONS.has(extension);
}

/** 判断文件是否可以作为视觉图片上传，MIME 优先，扩展名只作本地选择补充。 */
export function isSupportedImageFile(file: Pick<File, "name" | "type">): boolean {
  if (isSupportedImageContentType(file.type)) return true;
  const extension = file.name.split(".").pop()?.toLowerCase() ?? "";
  return SUPPORTED_IMAGE_EXTENSIONS.has(extension) && !file.type;
}

/** 本机上传的总时限；超过视为后端无响应，必须释放 composer 的发送状态。 */
const UPLOAD_TIMEOUT_MS = 120_000;

/**
 * 通过 workspace 级 HTTP 接口上传图片；不创建或绑定 Task/Run。
 * 上传成功后返回 workspace 内稳定的 asset locator，调用方再把 locator
 * 放入 Assistant Transport 的 image part。
 *
 * 用 XHR 而不是 lib/http 的 fetch 封装，只是为了拿到上传进度；其余约定与 lib/http 一致：
 * 动态基地址、X-Trace-Id、超时以 TimeoutError 拒绝、调用方取消以 AbortError 拒绝、
 * 非 2xx 转为 HttpError，界面统一经 userErrorMessage 呈现。没有超时时，卡住的上传会让
 * assistant-ui 的发送状态永远不结束，composer 再也无法发送。
 */
export function uploadAttachment(
  workspaceId: number,
  file: File,
  onProgress?: (progress: number) => void,
  options: { signal?: AbortSignal; traceId?: TraceId } = {},
): Promise<UploadedAttachment> {
  const traceId = options.traceId ?? newTraceId();
  const path = `/workspaces/${workspaceId}/attachments`;
  const fail = (reject: (error: unknown) => void, error: Error, status: number | null) => {
    void frontendLog("ERROR", "attachment_upload_failed", "附件上传失败", {
      traceId,
      data: { workspaceId, status, byteSize: file.size },
      error,
    });
    reject(error);
  };

  return new Promise((resolve, reject) => {
    if (options.signal?.aborted) {
      reject(new DOMException("附件上传已取消", "AbortError"));
      return;
    }
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${getApiBaseUrl()}${path}`);
    xhr.setRequestHeader("X-Trace-Id", traceId);
    xhr.responseType = "json";
    xhr.timeout = UPLOAD_TIMEOUT_MS;
    const abort = () => xhr.abort();
    options.signal?.addEventListener("abort", abort, { once: true });
    const cleanup = () => options.signal?.removeEventListener("abort", abort);

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress?.(Math.round((event.loaded / event.total) * 100));
    };
    xhr.onerror = () => {
      cleanup();
      fail(reject, new UserFacingError("附件上传失败，请检查本机后端是否正在运行"), null);
    };
    xhr.ontimeout = () => {
      cleanup();
      fail(reject, new DOMException(`附件上传在 ${UPLOAD_TIMEOUT_MS}ms 内未完成`, "TimeoutError"), null);
    };
    xhr.onabort = () => {
      cleanup();
      reject(new DOMException("附件上传已取消", "AbortError"));
    };
    xhr.onload = () => {
      cleanup();
      const body: unknown = xhr.response;
      const record = typeof body === "object" && body !== null
        ? body as Record<string, unknown>
        : null;
      if (xhr.status >= 200 && xhr.status < 300 && record && typeof record.id === "string") {
        resolve(record as unknown as UploadedAttachment);
        return;
      }
      const structured = parseStructuredHttpError(record);
      const detail = record?.detail;
      const message = structured?.message
        ?? (typeof detail === "object" && detail !== null && "message" in detail && typeof detail.message === "string"
          ? detail.message
          : "附件上传失败");
      fail(reject, new HttpError(message, { status: xhr.status, code: structured?.code }), xhr.status);
    };
    const form = new FormData();
    form.append("file", file, file.name);
    xhr.send(form);
  });
}

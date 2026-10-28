import { requestJson } from "@/lib/http/client";
import type { TraceId } from "@/lib/trace";

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
 * 走 lib/http 的统一请求边界（动态基地址、X-Trace-Id、非 2xx 转 HttpError、失败日志），
 * 只把超时放宽到上传时限：没有超时时，卡住的上传会让 assistant-ui 的发送状态永远不结束，
 * composer 再也无法发送。
 */
export function uploadAttachment(
  workspaceId: number,
  file: File,
  traceId?: TraceId,
): Promise<UploadedAttachment> {
  const form = new FormData();
  form.append("file", file, file.name);
  return requestJson<UploadedAttachment>(`/workspaces/${workspaceId}/attachments`, {
    method: "POST",
    body: form,
    traceId,
    timeoutMs: UPLOAD_TIMEOUT_MS,
  });
}

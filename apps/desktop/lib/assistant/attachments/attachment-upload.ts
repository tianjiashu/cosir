import { getApiBaseUrl } from "@/lib/http/client";

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
export function isSupportedImageContentType(contentType: string): boolean {
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

/**
 * 通过 workspace 级 HTTP 接口上传图片；不创建或绑定 Task/Run。
 * 上传成功后返回 workspace 内稳定的 asset locator，调用方再把 locator
 * 放入 Assistant Transport 的 image part。
 */
export function uploadAttachment(
  workspaceId: number,
  file: File,
  onProgress?: (progress: number) => void,
): Promise<UploadedAttachment> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${getApiBaseUrl()}/workspaces/${workspaceId}/attachments`);
    xhr.setRequestHeader("X-Trace-Id", crypto.randomUUID());
    xhr.responseType = "json";
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress?.(Math.round((event.loaded / event.total) * 100));
    };
    xhr.onerror = () => reject(new Error("附件上传失败，请检查本机后端状态"));
    xhr.onabort = () => reject(new Error("附件上传已取消"));
    xhr.onload = () => {
      const body: unknown = xhr.response;
      const record = typeof body === "object" && body !== null
        ? body as Record<string, unknown>
        : null;
      if (xhr.status < 200 || xhr.status >= 300 || !record || typeof record.id !== "string") {
        const detail = record?.detail;
        const message = typeof detail === "object" && detail !== null && "message" in detail && typeof detail.message === "string"
          ? detail.message
          : "附件上传失败";
        reject(new Error(message));
        return;
      }
      resolve(record as unknown as UploadedAttachment);
    };
    const form = new FormData();
    form.append("file", file, file.name);
    xhr.send(form);
  });
}

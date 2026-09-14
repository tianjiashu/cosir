import { getApiBaseUrl } from "@/lib/http/client";

export type UploadedAttachment = {
  id: string;
  name: string;
  contentType: string;
  byteSize: number;
  width: number;
  height: number;
  locator: string;
  status: "staged" | "ready";
};

export function uploadAttachment(
  taskId: number,
  file: File,
  onProgress?: (progress: number) => void,
): Promise<UploadedAttachment> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${getApiBaseUrl()}/tasks/${taskId}/attachments`);
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
      resolve(record as unknown as UploadedImage);
    };
    const form = new FormData();
    form.append("file", file, file.name);
    xhr.send(form);
  });
}

/** Backwards-compatible image-specific name for callers outside the adapter. */
export const uploadImage = uploadAttachment;
export type UploadedImage = UploadedAttachment;

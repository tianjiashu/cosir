export const IMAGE_EXTENSIONS = new Set([
  "jpg",
  "jpeg",
  "png",
]);

const UNSUPPORTED_IMAGE_EXTENSIONS = new Set(["gif", "webp", "bmp", "tif", "tiff"]);

const MIME_TYPES: Record<string, string> = {
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  png: "image/png",
};

/** MIME-like marker used for a directory represented as a regular attachment. */
export const DIRECTORY_CONTENT_TYPE = "application/x-directory";

/** Return the final path segment without exposing platform-specific separators to the UI. */
export function fileName(path: string): string {
  return path.replaceAll("\\", "/").split("/").pop() || path;
}

/** Infer a stable MIME type for files selected through the native path picker. */
export function contentTypeFor(path: string): string {
  const extension = fileName(path).split(".").pop()?.toLowerCase() ?? "";
  return MIME_TYPES[extension] ?? "application/octet-stream";
}

export function isImagePath(path: string, contentType = contentTypeFor(path)): boolean {
  return contentType.startsWith("image/")
    || IMAGE_EXTENSIONS.has(fileName(path).split(".").pop()?.toLowerCase() ?? "");
}

/** 判断原生选择路径是否是已识别但当前不支持的图片格式。 */
export function isUnsupportedImagePath(path: string, contentType = contentTypeFor(path)): boolean {
  const extension = fileName(path).split(".").pop()?.toLowerCase() ?? "";
  const supported = contentType === "image/jpeg" || contentType === "image/png";
  return !supported && (contentType.startsWith("image/") || UNSUPPORTED_IMAGE_EXTENSIONS.has(extension));
}

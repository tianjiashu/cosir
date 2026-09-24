export const IMAGE_EXTENSIONS = new Set([
  "jpg",
  "jpeg",
  "png",
  "gif",
  "webp",
  "bmp",
  "tif",
  "tiff",
]);

const MIME_TYPES: Record<string, string> = {
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  png: "image/png",
  gif: "image/gif",
  webp: "image/webp",
  bmp: "image/bmp",
  tif: "image/tiff",
  tiff: "image/tiff",
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

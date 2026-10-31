import { ATTACHMENT_ASSET_ID } from "@/lib/assistant/attachments/attachment-locator";

/** `display_data.kind = "image-read"` 的载荷（后端 `app/tools/display/image_display.py`）。 */
export type ImageReadDisplay = {
  images: { path: string; assetId: string; width: number; height: number }[];
  /** 没读成的路径与受控短提示。 */
  failures: { path: string; hint: string }[];
};


/** 校验展示载荷；形状不对返回 null，由注册表回落通用卡片。单个条目不合法时只丢这一条。 */
export function readImageReadDisplay(value: unknown): ImageReadDisplay | null {
  if (typeof value !== "object" || value === null) return null;
  const data = value as Record<string, unknown>;
  if (data.kind !== "image-read" || !Array.isArray(data.images) || !Array.isArray(data.failures)) return null;
  const images = data.images.flatMap((item: unknown) => {
    const image = item as Record<string, unknown> | null;
    if (typeof image?.path !== "string" || typeof image.asset_id !== "string" || !ATTACHMENT_ASSET_ID.test(image.asset_id)) return [];
    const width = typeof image.width === "number" ? image.width : 0;
    const height = typeof image.height === "number" ? image.height : 0;
    return [{ path: image.path, assetId: image.asset_id, width, height }];
  });
  const failures = data.failures.flatMap((item: unknown) => {
    const failure = item as Record<string, unknown> | null;
    return typeof failure?.path === "string" && typeof failure.hint === "string" ? [{ path: failure.path, hint: failure.hint }] : [];
  });
  return { images, failures };
}

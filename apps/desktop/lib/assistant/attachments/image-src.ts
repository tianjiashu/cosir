import { IMAGE_LOCATOR } from "@/lib/assistant/attachments/attachment-locator";
import { getApiBaseUrl } from "@/lib/http/client";

/** 工作区附件的内容地址；没有工作区时无法定位，为 undefined。 */
export const attachmentContentUrl = (assetId: string, workspaceId?: number): string | undefined =>
  workspaceId === undefined ? undefined : `${getApiBaseUrl()}/workspaces/${workspaceId}/attachments/${assetId}/content`;

/** 把 Transport 图片定位（`cosir-attachment://<asset id>`）解析为可显示地址；data/blob/http(s) 原样放行，其余为 undefined。 */
export const resolveTransportImageSrc = (
  src: string,
  workspaceId?: number,
): string | undefined => {
  const assetId = src.match(IMAGE_LOCATOR)?.[1];
  if (assetId && workspaceId !== undefined) return attachmentContentUrl(assetId, workspaceId);
  return /^(?:data:|blob:|https?:\/\/)/i.test(src) ? src : undefined;
};

import { useQuery } from "@tanstack/react-query";

import { desktopHost } from "@/lib/desktop-host";
import { fileCapabilities } from "@/electron/shared/file-capabilities";
import { platform } from "@/lib/platform";

/** 当前环境能否取系统缩略图：桌面宿主里，且平台支持（`fileCapabilities`；浏览器开发模式没有宿主）。 */
export const SYSTEM_THUMBNAILS = desktopHost !== null && fileCapabilities(platform).thumbnail;

/**
 * 系统缩略图（宿主 `files.thumbnail`，PNG 的 data URL）。按路径、版本与边长缓存且不过期：同一版本的文件缩略图不变，
 * 文件改了版本就换，自然重取。系统对不认识的格式会给一张通用图标，所以调用方只对 `system_preview` 为真的文件启用；
 * 取不到（平台不支持、系统拒绝）时返回 null，界面退回文件图标，不提示错误。
 */
export function useSystemThumbnail(absolutePath: string | null, version: string, edge: number): string | null {
  const query = useQuery({
    queryKey: ["cosir", "system-thumbnail", absolutePath, version, edge] as const,
    queryFn: () => desktopHost!.files.thumbnail(absolutePath!, edge),
    enabled: SYSTEM_THUMBNAILS && absolutePath !== null,
    staleTime: Infinity,
    retry: false,
  });
  return query.data ?? null;
}

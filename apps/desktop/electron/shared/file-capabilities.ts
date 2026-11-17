/**
 * 各平台能把本机文件交给系统的哪些能力：宿主（`electron/main/files/file-actions.ts` 拒绝平台不支持的调用）与渲染进程
 * （`lib/desktop-host/file-exits.ts` 决定显示哪些出口、`use-system-thumbnail.ts` 决定要不要取缩略图）共用这一份，
 * 不在两边各判一次平台。「用系统应用打开」与「在文件夹中显示」各平台都有，不在表里。
 *
 * - 快速查看：Electron 的 `previewFile` 只有 macOS 实现。
 * - 系统缩略图：`nativeImage.createThumbnailFromPath` 只支持 macOS 与 Windows。
 */
export type FileCapabilities = { quickLook: boolean; thumbnail: boolean };

export function fileCapabilities(platform: string): FileCapabilities {
  return { quickLook: platform === "darwin", thumbnail: platform === "darwin" || platform === "win32" };
}

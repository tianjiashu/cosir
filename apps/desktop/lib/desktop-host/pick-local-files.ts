import { UserFacingError } from "@/lib/errors/user-facing-error";
import { desktopHost } from "@/lib/desktop-host";

export type PickedLocalFile = { file: File; path: string };

/**
 * 用系统文件选择框选本机文件，返回文件及其本机路径；用户取消时为空数组。
 *
 * 用 `<input type="file">` 而不是宿主对话框：Electron 中它打开的就是系统原生选择框，拿到的 `File`
 * 可以直接读取，路径再经宿主的 `files.pathOf` 取得，不需要把文件字节经 IPC 搬一遍。
 * 代价是不能指定初始目录。只能在桌面宿主中使用：路径是后端定位文件的依据，浏览器拿不到。
 */
export function pickLocalFiles(options: { accept?: string; multiple?: boolean }): Promise<PickedLocalFile[]> {
  const host = desktopHost;
  if (!host) return Promise.reject(new UserFacingError("只能在桌面应用中选择本机文件"));
  return new Promise((resolve) => {
    const input = document.createElement("input");
    input.type = "file";
    input.multiple = options.multiple ?? false;
    if (options.accept) input.accept = options.accept;
    input.addEventListener("change", () => {
      resolve([...(input.files ?? [])].map((file) => ({ file, path: host.files.pathOf(file) })));
    }, { once: true });
    input.addEventListener("cancel", () => resolve([]), { once: true });
    input.click();
  });
}

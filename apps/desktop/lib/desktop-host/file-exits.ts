import { desktopHost } from "@/lib/desktop-host";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";
import { fileCapabilities } from "@/electron/shared/file-capabilities";
import { platform } from "@/lib/platform";

export type FileExitId = "quick-look" | "open-externally" | "reveal";

/** 把一个本机文件交给系统的一种方式；`run` 的失败已在这里提示与记日志，调用方不必再处理。 */
export type FileExit = { id: FileExitId; label: string; run: (absolutePath: string) => void };

function exit(id: FileExitId, label: string, call: (absolutePath: string) => Promise<void>): FileExit {
  return {
    id,
    label,
    run: (absolutePath) => {
      call(absolutePath).catch((error: unknown) => {
        void frontendLog("WARNING", "file_exit_failed", "把文件交给系统失败", { data: { exit: id }, error });
        notify.error(`无法${label}`, { error });
      });
    },
  };
}

/**
 * 当前平台可用的文件出口（快速查看、用系统应用打开、在文件夹中显示），文件面板的空态与「更多」、交付物胶囊的菜单共用
 * 这一份。各平台有哪些出口只看 `fileCapabilities`（快速查看只有 macOS 有），对任何文件都给：系统不认识的格式也会
 * 显示文件名、大小与图标，比不给入口好。没有桌面宿主（浏览器开发、E2E 不装假宿主时）一项都没有，界面据此不显示入口，
 * 不出现点了没反应的按钮。路径由调用方给出存在的绝对路径，宿主再确认一次。
 */
export const FILE_EXITS: readonly FileExit[] = desktopHost === null ? [] : [
  ...(fileCapabilities(platform).quickLook ? [exit("quick-look", "快速查看", desktopHost.files.quickLook)] : []),
  exit("open-externally", "用系统应用打开", desktopHost.files.openExternally),
  exit("reveal", "在文件夹中显示", desktopHost.files.reveal),
];

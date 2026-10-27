import { getCurrentWindow } from "@tauri-apps/api/window";
import { CopyIcon, MinusIcon, SquareIcon, XIcon } from "lucide-react";
import { useEffect, useState } from "react";

import { frontendLog } from "@/lib/logging/frontend-log";
import { cn } from "@/lib/utils";

/** 窗口命令失败只影响这次点击；记录后保持界面可用，不向用户弹错。 */
function runWindowCommand(action: string, command: () => Promise<void>) {
  void command().catch((error: unknown) => {
    void frontendLog("WARNING", "window_control_failed", "窗口控制命令执行失败", { data: { action }, error });
  });
}

/**
 * Windows 自绘窗口按钮（`tauri.windows.conf.json` 去掉了原生边框）：最小化、最大化 / 还原、关闭。
 *
 * 固定在窗口右上角、位于所有页面层（含 z-100 的启动页）之上，启动页与主界面共用一份；宽度与
 * globals.css 的 `--titlebar-inset-right` 一致，右上角的页面头部或工作台标签行据此让位。
 * 关闭走 Tauri 原生关闭请求，与原生按钮一样由宿主的 CloseRequested 处理（隐藏到托盘）。
 * 双击拖动区域最大化由 Tauri 拖拽脚本负责，这里不重复实现；
 * 原生按钮悬停时的贴靠布局菜单无法自绘提供，Win+Z 与拖到屏幕边缘贴靠仍可用。
 */
export function WindowControls() {
  const [maximized, setMaximized] = useState(false);

  useEffect(() => {
    const appWindow = getCurrentWindow();
    let active = true;
    const sync = () => void appWindow.isMaximized().then((value) => { if (active) setMaximized(value); });
    sync();
    const unlisten = appWindow.onResized(sync);
    return () => {
      active = false;
      void unlisten.then((stop) => stop());
    };
  }, []);

  const button = "flex h-10 w-[46px] items-center justify-center text-foreground/80 transition-colors focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring";
  return (
    <div className="fixed top-0 right-0 z-[110] flex" role="group" aria-label="窗口控制">
      <button type="button" className={cn(button, "hover:bg-muted")} aria-label="最小化" title="最小化"
        onClick={() => runWindowCommand("minimize", () => getCurrentWindow().minimize())}>
        <MinusIcon className="size-4" aria-hidden="true" />
      </button>
      <button type="button" className={cn(button, "hover:bg-muted")} aria-label={maximized ? "还原" : "最大化"} title={maximized ? "还原" : "最大化"}
        onClick={() => runWindowCommand("toggle_maximize", () => getCurrentWindow().toggleMaximize())}>
        {maximized ? <CopyIcon className="size-3.5 -scale-x-100" aria-hidden="true" /> : <SquareIcon className="size-3.5" aria-hidden="true" />}
      </button>
      <button type="button" className={cn(button, "hover:bg-[#c42b1c] hover:text-white")} aria-label="关闭" title="关闭"
        onClick={() => runWindowCommand("close", () => getCurrentWindow().close())}>
        <XIcon className="size-4" aria-hidden="true" />
      </button>
    </div>
  );
}

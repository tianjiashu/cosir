import { contextBridge, ipcRenderer } from "electron";

import { BROWSER_PAGE_DIALOG_CHANNEL, type BrowserPageDialogReply, type BrowserPageDialogRequest } from "../shared/browser-page-bridge";

/**
 * 内置浏览器网页的 preload（与应用自身的 `index.ts` 无关）：只做一件事，把页面的 `alert` / `confirm` /
 * `prompt` 换成「同步问宿主」（原因见 `shared/browser-page-bridge.ts`）。
 *
 * 不向页面暴露任何对象：问宿主的函数只作为参数交进主世界的闭包。经 `nodeIntegrationInSubFrames` 在每个
 * 框架里都运行，iframe 里的弹窗同样接得住。页面能察觉函数已被替换（`toString` 不再是 native code），接受。
 */
contextBridge.executeInMainWorld({
  func: (ask: (request: BrowserPageDialogRequest) => BrowserPageDialogReply) => {
    const text = (value: unknown) => (value === undefined ? "" : String(value));
    window.alert = (message?: unknown) => {
      ask({ type: "alert", message: text(message), defaultText: "" });
    };
    window.confirm = (message?: unknown) => ask({ type: "confirm", message: text(message), defaultText: "" }) === true;
    window.prompt = (message?: unknown, defaultText?: unknown) => {
      const reply = ask({ type: "prompt", message: text(message), defaultText: text(defaultText) });
      return typeof reply === "string" ? reply : null;
    };
  },
  args: [(request: BrowserPageDialogRequest) => ipcRenderer.sendSync(BROWSER_PAGE_DIALOG_CHANNEL, request) as BrowserPageDialogReply],
});

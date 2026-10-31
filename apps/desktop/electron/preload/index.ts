import { contextBridge, ipcRenderer, webUtils } from "electron";

import {
  HOST_CHANNEL,
  type BackendSnapshot,
  type BrowserTabSummary,
  type CosirHost,
  type HostPlatform,
  type HostReply,
} from "../shared/host-contract";

/**
 * 把宿主控制面以 `window.cosirHost` 暴露给页面；只暴露契约里的方法，`ipcRenderer` 本身不出 preload。
 *
 * 运行在沙箱 preload 中，只能用 Electron 允许的少数模块；这里不做业务校验，入参校验在 main 的 `ipc.ts`。
 */
async function call<T>(channel: string, ...args: unknown[]): Promise<T> {
  const reply = (await ipcRenderer.invoke(channel, ...args)) as HostReply<T>;
  if (!reply.ok) throw new Error(reply.message);
  return reply.value;
}

const host: CosirHost = {
  platform: process.platform as HostPlatform,
  backend: {
    snapshot: () => call(HOST_CHANNEL.backendSnapshot),
    restart: () => call(HOST_CHANNEL.backendRestart),
    onChange: (listener) => {
      const forward = (_event: Electron.IpcRendererEvent, snapshot: BackendSnapshot) => listener(snapshot);
      ipcRenderer.on(HOST_CHANNEL.backendChanged, forward);
      return () => ipcRenderer.removeListener(HOST_CHANNEL.backendChanged, forward);
    },
  },
  log: {
    write: (entry) => call(HOST_CHANNEL.logWrite, entry),
  },
  dialog: {
    openDirectory: (options) => call(HOST_CHANNEL.dialogOpenDirectory, options),
  },
  window: {
    setThemeSource: (source) => call(HOST_CHANNEL.windowThemeSource, source),
  },
  browser: {
    listTabs: () => call(HOST_CHANNEL.browserTabs),
    open: (taskId, limits, url) => call(HOST_CHANNEL.browserOpen, taskId, limits, url),
    onTabsChanged: (listener) => {
      const forward = (_event: Electron.IpcRendererEvent, tabs: BrowserTabSummary[]) => listener(tabs);
      ipcRenderer.on(HOST_CHANNEL.browserTabsChanged, forward);
      return () => ipcRenderer.removeListener(HOST_CHANNEL.browserTabsChanged, forward);
    },
    place: (tabId, placement) => call(HOST_CHANNEL.browserPlace, tabId, placement),
    freeze: (tabId) => call(HOST_CHANNEL.browserFreeze, tabId),
    navigate: (tabId, navigation) => call(HOST_CHANNEL.browserNavigate, tabId, navigation),
    close: (tabId) => call(HOST_CHANNEL.browserClose, tabId),
  },
  files: {
    pathOf: (file) => webUtils.getPathForFile(file),
  },
};

contextBridge.exposeInMainWorld("cosirHost", host);

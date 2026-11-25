import type { Page } from "@playwright/test";
import type { BrowserState, HostPlatform } from "@/electron/shared/host-contract";
import { API_ORIGIN } from "./e2e-env";

/** 测试宿主记下的内置浏览器调用：方法名与参数，按调用顺序。 */
export type FakeBrowserCall = { method: string; args: unknown[] };

/**
 * 在页面脚本加载前注入一个最小的桌面宿主（`window.cosirHost`），只替代操作系统边界：
 * 文件的本机路径与目录选择框。后端状态直接报告 ready（指向测试服务），其余宿主能力为空操作。
 *
 * 与真实 Electron 一致：只有经文件选择框交给页面的 File 才有本机路径；页面里构造的 File
 * （粘贴、拖放测试数据）没有路径。`directory` 是目录选择框要返回的路径，null 表示用户取消。
 *
 * 内置浏览器只是一份状态（`browser.state`，缺省为空）：不开真实网页，作答、定位下载、在文件夹中显示等调用只记进
 * `fakeBrowserCalls`（`browser.failAnswer` 时作答失败），状态不随调用变化；用例要模拟宿主推送新状态（如 Agent 新开了
 * 标签）时用 `pushBrowserState`。真实标签行为由桌面 E2E 覆盖。
 *
 * `platform` 缺省是 linux（没有快速查看、没有缩略图）；按平台分支的界面用 darwin / win32 测。文件出口（显示、打开、
 * 快速查看、缩略图）同样只记调用；`thumbnail` 给出时缩略图返回它（data URL），否则按「系统没给出」失败。
 */
export async function installFakeDesktopHost(
  page: Page,
  options: { directory?: string | null; browser?: { state?: BrowserState; failAnswer?: boolean }; platform?: HostPlatform; thumbnail?: string } = {},
) {
  await page.addInitScript(({ directory, backendBaseUrl, browserState, failAnswer, platform, thumbnail }) => {
    // 选择框的 <input> 不一定挂在文档上，所以在读取 files 时记录，而不是监听 change 冒泡。
    const pickedFiles = new WeakSet<File>();
    const filesDescriptor = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "files")!;
    Object.defineProperty(HTMLInputElement.prototype, "files", {
      ...filesDescriptor,
      get(this: HTMLInputElement) {
        const files = filesDescriptor.get!.call(this) as FileList | null;
        if (this.type === "file") for (const file of files ?? []) pickedFiles.add(file);
        return files;
      },
    });
    let snapshot = { generation: 1, status: { state: "ready" }, backendBaseUrl };
    const backendListeners = new Set<(next: typeof snapshot) => void>();
    let currentBrowserState = browserState;
    const browserListeners = new Set<(next: typeof browserState) => void>();
    const browserCalls: { method: string; args: unknown[] }[] = [];
    const record = (method: string, args: unknown[]) => {
      browserCalls.push({ method, args });
    };
    Object.assign(window, {
      fakeBrowserCalls: browserCalls,
      // 模拟后端重启：宿主推送新一代快照、地址换新（真实宿主每次重启都重新分配端口）。
      fakeBackendRestart: (baseUrl: string) => {
        snapshot = { ...snapshot, generation: snapshot.generation + 1, backendBaseUrl: baseUrl };
        for (const listener of backendListeners) listener(snapshot);
      },
      // 模拟宿主推送浏览器状态（标签出现、关闭）：与真实宿主一样整份推送。
      fakeBrowserPush: (next: typeof browserState) => {
        currentBrowserState = next;
        for (const listener of browserListeners) listener(next);
      },
      cosirHost: {
        platform,
        homeDir: "/home/e2e",
        backend: {
          snapshot: async () => snapshot,
          restart: async () => snapshot,
          onChange: (listener: (next: typeof snapshot) => void) => {
            backendListeners.add(listener);
            return () => backendListeners.delete(listener);
          },
        },
        log: { write: async () => undefined },
        dialog: { openDirectory: async () => directory },
        window: { setThemeSource: async () => undefined },
        files: {
          pathOf: (file: File) => (pickedFiles.has(file) ? `/tmp/${file.name}` : ""),
          reveal: async (...args: unknown[]) => record("files.reveal", args),
          openExternally: async (...args: unknown[]) => record("files.openExternally", args),
          quickLook: async (...args: unknown[]) => record("files.quickLook", args),
          thumbnail: async (...args: unknown[]) => {
            record("files.thumbnail", args);
            if (!thumbnail) throw new Error("系统没有给出这个文件的缩略图");
            return thumbnail;
          },
        },
        browser: {
          state: async () => currentBrowserState,
          onStateChanged: (listener: (next: typeof browserState) => void) => {
            browserListeners.add(listener);
            return () => browserListeners.delete(listener);
          },
          // 只记下请求、不模拟标签：用例据此断言「要在内置浏览器打开什么」（地址随 navigate 给出）。
          open: async (...args: unknown[]) => { record("browser.open", args); return "fake-tab"; },
          place: async () => undefined,
          freeze: async () => "",
          navigate: async (...args: unknown[]) => record("browser.navigate", args),
          close: async () => undefined,
          answerDialog: async (...args: unknown[]) => {
            record("answerDialog", args);
            if (failAnswer) throw new Error("弹窗已关闭");
          },
          revealDownload: async (...args: unknown[]) => record("revealDownload", args),
        },
      },
    });
  }, {
    directory: options.directory ?? null,
    backendBaseUrl: API_ORIGIN,
    browserState: options.browser?.state ?? { tabs: [], downloads: [] },
    failAnswer: options.browser?.failAnswer ?? false,
    platform: options.platform ?? "linux",
    thumbnail: options.thumbnail ?? null,
  });
}

/** 页面至今对测试宿主内置浏览器的调用。 */
export async function fakeBrowserCalls(page: Page): Promise<FakeBrowserCall[]> {
  return page.evaluate(() => (window as unknown as { fakeBrowserCalls: FakeBrowserCall[] }).fakeBrowserCalls);
}

/** 模拟宿主推送一份新的浏览器状态（整份替换），渲染进程经 `browser.onStateChanged` 收到。 */
export async function pushBrowserState(page: Page, state: BrowserState): Promise<void> {
  await page.evaluate((next) => (window as unknown as { fakeBrowserPush: (state: unknown) => void }).fakeBrowserPush(next), state);
}

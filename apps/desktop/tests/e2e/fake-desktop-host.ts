import type { Page } from "@playwright/test";
import type { BrowserState } from "@/electron/shared/host-contract";
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
 * 内置浏览器只是一份静态状态（`browser.state`，缺省为空）：不开真实网页，作答、定位下载等调用只记进
 * `fakeBrowserCalls`（`browser.failAnswer` 时作答失败），状态不随调用变化；真实标签行为由桌面 E2E 覆盖。
 */
export async function installFakeDesktopHost(
  page: Page,
  options: { directory?: string | null; browser?: { state?: BrowserState; failAnswer?: boolean } } = {},
) {
  await page.addInitScript(({ directory, backendBaseUrl, browserState, failAnswer }) => {
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
    const snapshot = { generation: 1, status: { state: "ready" }, backendBaseUrl };
    const browserCalls: { method: string; args: unknown[] }[] = [];
    const record = (method: string, args: unknown[]) => {
      browserCalls.push({ method, args });
    };
    Object.assign(window, {
      fakeBrowserCalls: browserCalls,
      cosirHost: {
        platform: "linux",
        backend: {
          snapshot: async () => snapshot,
          restart: async () => snapshot,
          onChange: () => () => undefined,
        },
        log: { write: async () => undefined },
        dialog: { openDirectory: async () => directory },
        window: { setThemeSource: async () => undefined },
        files: { pathOf: (file: File) => (pickedFiles.has(file) ? `/tmp/${file.name}` : "") },
        browser: {
          state: async () => browserState,
          onStateChanged: () => () => undefined,
          open: async () => { throw new Error("测试宿主不提供内置浏览器"); },
          place: async () => undefined,
          freeze: async () => "",
          navigate: async () => undefined,
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
  });
}

/** 页面至今对测试宿主内置浏览器的调用。 */
export async function fakeBrowserCalls(page: Page): Promise<FakeBrowserCall[]> {
  return page.evaluate(() => (window as unknown as { fakeBrowserCalls: FakeBrowserCall[] }).fakeBrowserCalls);
}

import type { Page } from "@playwright/test";

/**
 * 在页面脚本加载前注入一个最小的桌面宿主（`window.cosirHost`），只替代操作系统边界：
 * 文件的本机路径与目录选择框。后端状态直接报告 ready（指向测试服务），其余宿主能力为空操作
 * （内置浏览器始终没有标签页）。
 *
 * 与真实 Electron 一致：只有经文件选择框交给页面的 File 才有本机路径；页面里构造的 File
 * （粘贴、拖放测试数据）没有路径。`directory` 是目录选择框要返回的路径，null 表示用户取消。
 */
export async function installFakeDesktopHost(page: Page, options: { directory?: string | null } = {}) {
  await page.addInitScript(({ directory }) => {
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
    const snapshot = { generation: 1, status: { state: "ready" }, backendBaseUrl: "http://127.0.0.1:8000" };
    Object.assign(window, {
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
        // 内置浏览器：没有标签页；需要真实标签行为的场景由桌面 E2E 覆盖。
        browser: {
          listTabs: async () => [],
          onTabsChanged: () => () => undefined,
          open: async () => { throw new Error("测试宿主不提供内置浏览器"); },
          place: async () => undefined,
          freeze: async () => "",
          navigate: async () => undefined,
          close: async () => undefined,
        },
      },
    });
  }, { directory: options.directory ?? null });
}

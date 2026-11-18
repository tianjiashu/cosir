import { beforeEach, describe, expect, it, vi } from "vitest";

const electron = vi.hoisted(() => ({
  app: { isPackaged: true },
  Menu: { buildFromTemplate: vi.fn((template: unknown) => template), setApplicationMenu: vi.fn() },
  shell: { openPath: vi.fn() },
}));
vi.mock("electron", () => electron);
vi.mock("@/electron/main/paths", () => ({ appLogDir: () => "/tmp/logs" }));
import { installAppMenu } from "@/electron/main/app-menu";
import type { BrowserNetLogCapture } from "@/electron/main/browser/diagnostics/netlog-capture";

type Item = { role?: string; label?: string; enabled?: boolean; click?: () => void; submenu?: Item[] };
const menu = () => electron.Menu.setApplicationMenu.mock.calls.at(-1)![0] as Item[];
const netLog = (active: boolean) => ({ active, start: vi.fn(), stop: vi.fn().mockResolvedValue(undefined) });

const roles = () => menu().map((item) => item.role);

beforeEach(() => vi.clearAllMocks());

describe("installAppMenu", () => {
  // 目的：焦点在内置浏览器里时按 Cmd+R / Cmd + 不能刷新整个应用、缩放网页；潜在缺陷：沿用 Electron 默认菜单，
  // 「视图」菜单的快捷键窗口级生效，刷新掉应用界面、改掉 Agent 看到的布局。
  it("leaves out the view menu in packaged builds but keeps editing shortcuts", () => {
    electron.app.isPackaged = true;
    installAppMenu(netLog(false) as unknown as BrowserNetLogCapture);
    expect(roles()).not.toContain("viewMenu");
    expect(roles()).toContain("editMenu");

    electron.app.isPackaged = false;
    installAppMenu(netLog(false) as unknown as BrowserNetLogCapture);
    expect(roles()).toContain("viewMenu");
  });

  // 目的：网络问题要在复现那一刻开记录，打包后也要能开；潜在缺陷：采集中仍能再点开始，或停止项一直灰着、无法提前收尾。
  it("offers the browser network capture with entries matching its state", () => {
    const idle = netLog(false);
    installAppMenu(idle as unknown as BrowserNetLogCapture);
    const [start, stop, , folder] = menu().find((item) => item.label === "诊断")!.submenu!;
    expect([start.enabled, stop.enabled]).toEqual([true, false]);
    start.click!();
    expect(idle.start).toHaveBeenCalledOnce();
    folder.click!();
    expect(electron.shell.openPath).toHaveBeenCalledWith("/tmp/logs");

    const running = netLog(true);
    installAppMenu(running as unknown as BrowserNetLogCapture);
    const [startAgain, stopNow] = menu().find((item) => item.label === "诊断")!.submenu!;
    expect([startAgain.enabled, stopNow.enabled]).toEqual([false, true]);
    stopNow.click!();
    expect(running.stop).toHaveBeenCalledOnce();
  });
});

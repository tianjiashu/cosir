import { beforeEach, describe, expect, it, vi } from "vitest";

const electron = vi.hoisted(() => ({
  app: { isPackaged: true },
  Menu: { buildFromTemplate: vi.fn((template: unknown) => template), setApplicationMenu: vi.fn() },
}));
vi.mock("electron", () => electron);
import { installAppMenu } from "@/electron/main/app-menu";

const roles = () => (electron.Menu.setApplicationMenu.mock.calls.at(-1)![0] as { role: string }[]).map((item) => item.role);

beforeEach(() => vi.clearAllMocks());

describe("installAppMenu", () => {
  // 目的：焦点在内置浏览器里时按 Cmd+R / Cmd + 不能刷新整个应用、缩放网页；潜在缺陷：沿用 Electron 默认菜单，
  // 「视图」菜单的快捷键窗口级生效，刷新掉应用界面、改掉 Agent 看到的布局。
  it("leaves out the view menu in packaged builds but keeps editing shortcuts", () => {
    electron.app.isPackaged = true;
    installAppMenu();
    expect(roles()).not.toContain("viewMenu");
    expect(roles()).toContain("editMenu");

    electron.app.isPackaged = false;
    installAppMenu();
    expect(roles()).toContain("viewMenu");
  });
});

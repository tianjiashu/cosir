import { describe, expect, it } from "vitest";

import {
  normalizeLayoutPreferences,
  SIDEBAR_WIDTH,
  TERMINAL_PANEL_HEIGHT,
  DOCK_PANEL_WIDTH,
  useLayoutPreferences,
} from "@/lib/layout-preferences";

const DEFAULTS = {
  sidebarCollapsed: false,
  sidebarWidth: SIDEBAR_WIDTH.default,
  terminalPanelHeight: TERMINAL_PANEL_HEIGHT.default,
  dockWidths: { workbench: DOCK_PANEL_WIDTH.workbench.default, files: DOCK_PANEL_WIDTH.files.default, browser: DOCK_PANEL_WIDTH.browser.default },
};

describe("界面尺寸偏好", () => {
  it("缺失或非法时用默认值", () => {
    expect(normalizeLayoutPreferences(undefined)).toEqual(DEFAULTS);
    expect(normalizeLayoutPreferences("broken")).toEqual(DEFAULTS);
    expect(normalizeLayoutPreferences({
      sidebarCollapsed: "true",
      sidebarWidth: "300",
      terminalPanelHeight: Number.NaN,
      dockWidths: { workbench: Number.POSITIVE_INFINITY, files: "wide" },
    })).toEqual(DEFAULTS);
  });

  it("数值限幅到各自范围并取整，终端高度只保底下限", () => {
    expect(normalizeLayoutPreferences({
      sidebarCollapsed: true,
      sidebarWidth: 10,
      terminalPanelHeight: 50,
      dockWidths: { workbench: 5000, files: 100, browser: 5000 },
    })).toEqual({ sidebarCollapsed: true, sidebarWidth: 240, terminalPanelHeight: 192, dockWidths: { workbench: 900, files: 320, browser: 1600 } });
    expect(normalizeLayoutPreferences({ sidebarWidth: 999, terminalPanelHeight: 2000.4 }))
      .toMatchObject({ sidebarWidth: 420, terminalPanelHeight: 2000 });
  });

  it("setter 同样限幅", () => {
    const store = useLayoutPreferences.getState();
    store.setSidebarWidth(1000);
    store.setTerminalPanelHeight(10);
    store.setDockWidth("browser", 333.6);
    store.setDockWidth("files", 600.2);
    expect(useLayoutPreferences.getState()).toMatchObject({ sidebarWidth: 420, terminalPanelHeight: 192, dockWidths: { browser: 360, files: 600 } });
  });
});

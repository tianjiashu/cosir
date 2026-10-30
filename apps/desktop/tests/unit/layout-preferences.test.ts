import { describe, expect, it } from "vitest";

import {
  normalizeLayoutPreferences,
  SIDEBAR_WIDTH,
  TERMINAL_PANEL_HEIGHT,
  useLayoutPreferences,
  WORKBENCH_WIDTH,
} from "@/lib/layout-preferences";

const DEFAULTS = {
  sidebarCollapsed: false,
  sidebarWidth: SIDEBAR_WIDTH.default,
  terminalPanelHeight: TERMINAL_PANEL_HEIGHT.default,
  workbenchWidth: WORKBENCH_WIDTH.default,
};

describe("界面尺寸偏好", () => {
  it("缺失或非法时用默认值", () => {
    expect(normalizeLayoutPreferences(undefined)).toEqual(DEFAULTS);
    expect(normalizeLayoutPreferences("broken")).toEqual(DEFAULTS);
    expect(normalizeLayoutPreferences({
      sidebarCollapsed: "true",
      sidebarWidth: "300",
      terminalPanelHeight: Number.NaN,
      workbenchWidth: Number.POSITIVE_INFINITY,
    })).toEqual(DEFAULTS);
  });

  it("数值限幅到各自范围并取整，终端高度只保底下限", () => {
    expect(normalizeLayoutPreferences({
      sidebarCollapsed: true,
      sidebarWidth: 10,
      terminalPanelHeight: 50,
      workbenchWidth: 5000,
    })).toEqual({ sidebarCollapsed: true, sidebarWidth: 240, terminalPanelHeight: 192, workbenchWidth: 900 });
    expect(normalizeLayoutPreferences({ sidebarWidth: 999, terminalPanelHeight: 2000.4, workbenchWidth: 100 }))
      .toMatchObject({ sidebarWidth: 420, terminalPanelHeight: 2000, workbenchWidth: 320 });
  });

  it("setter 同样限幅", () => {
    const store = useLayoutPreferences.getState();
    store.setSidebarWidth(1000);
    store.setTerminalPanelHeight(10);
    store.setWorkbenchWidth(333.6);
    expect(useLayoutPreferences.getState()).toMatchObject({ sidebarWidth: 420, terminalPanelHeight: 192, workbenchWidth: 334 });
  });
});

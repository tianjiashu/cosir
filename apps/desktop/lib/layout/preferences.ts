import { create } from "zustand";
import { persist } from "zustand/middleware";

/**
 * 展开侧栏的宽度范围（px）。默认值放得下约 10 个汉字的任务标题；上限放得下后端标题上限
 * （`TASK_TITLE_LIMIT` = 20 个汉字）加省略号。
 */
export const SIDEBAR_WIDTH = { min: 240, default: 288, max: 420 } as const;

/** 底部终端面板的高度（px）；上限随窗口高度变化，由面板在渲染时收敛，偏好只保底下限。 */
export const TERMINAL_PANEL_HEIGHT = { min: 192, default: 320 } as const;

/** 右侧停靠区的面板：同一时刻只显示一个（见 `lib/layout/dock-store.ts`）。 */
export type DockPanel = "workbench" | "files" | "browser";

/** 各停靠面板的宽度偏好范围（px）；窗口变窄时由布局在渲染时再收窄，不改写偏好。浏览器要放下网页，默认更宽。 */
export const DOCK_PANEL_WIDTH: Record<DockPanel, { min: number; default: number; max: number }> = {
  workbench: { min: 320, default: 480, max: 900 },
  files: { min: 320, default: 480, max: 900 },
  browser: { min: 360, default: 720, max: 1600 },
};

type LayoutPreferences = {
  /** 宽屏下侧栏是否收起；窄屏始终以图标栏 + 抽屉呈现，不读取它。 */
  sidebarCollapsed: boolean;
  sidebarWidth: number;
  terminalPanelHeight: number;
  /** 每个停靠面板各记各的宽度。 */
  dockWidths: Record<DockPanel, number>;
};

type LayoutPreferencesState = LayoutPreferences & {
  setSidebarCollapsed: (collapsed: boolean) => void;
  setSidebarWidth: (width: number) => void;
  setTerminalPanelHeight: (height: number) => void;
  setDockWidth: (panel: DockPanel, width: number) => void;
};

function clamp(value: number, min: number, max: number): number {
  return Math.round(Math.max(min, Math.min(max, value)));
}

const clampSidebarWidth = (width: number) => clamp(width, SIDEBAR_WIDTH.min, SIDEBAR_WIDTH.max);
const clampTerminalPanelHeight = (height: number) => clamp(height, TERMINAL_PANEL_HEIGHT.min, Infinity);
const clampDockWidth = (panel: DockPanel) => (width: number) => clamp(width, DOCK_PANEL_WIDTH[panel].min, DOCK_PANEL_WIDTH[panel].max);

function finiteOr(value: unknown, fallback: number, clampValue: (value: number) => number): number {
  return typeof value === "number" && Number.isFinite(value) ? clampValue(value) : fallback;
}

/**
 * 把读到的偏好收敛成合法值：非法项回退默认值，数值限幅到各自范围。持久化数据来自 localStorage
 * （可能被手改或来自旧版本），这是它唯一的入口校验；setter 只限幅调用方传入的值。
 */
export function normalizeLayoutPreferences(raw: unknown): LayoutPreferences {
  const value = typeof raw === "object" && raw !== null ? raw as Record<string, unknown> : {};
  return {
    sidebarCollapsed: value.sidebarCollapsed === true,
    sidebarWidth: finiteOr(value.sidebarWidth, SIDEBAR_WIDTH.default, clampSidebarWidth),
    terminalPanelHeight: finiteOr(value.terminalPanelHeight, TERMINAL_PANEL_HEIGHT.default, clampTerminalPanelHeight),
    dockWidths: normalizeDockWidths(value.dockWidths),
  };
}

function normalizeDockWidths(raw: unknown): Record<DockPanel, number> {
  const value = typeof raw === "object" && raw !== null ? raw as Record<string, unknown> : {};
  const panels = Object.keys(DOCK_PANEL_WIDTH) as DockPanel[];
  return Object.fromEntries(panels.map((panel) => [
    panel,
    finiteOr(value[panel], DOCK_PANEL_WIDTH[panel].default, clampDockWidth(panel)),
  ])) as Record<DockPanel, number>;
}

/**
 * 界面尺寸偏好（侧栏收起与宽度、终端面板高度、各停靠面板宽度）：全局一份，跨工作区、Task 与重启保留，
 * 不是任务事实。经 zustand `persist`（默认存 localStorage）在每次 set 时落盘，调用方不再自行写回；
 * 存储不可用（隐私模式等）时只在本次会话内生效。
 *
 * 偏好只记用户意图：窗口变窄、变矮时的显示收敛由各组件在渲染时派生，不改写这里的值。
 */
export const useLayoutPreferences = create<LayoutPreferencesState>()(persist(
  (set) => ({
    ...normalizeLayoutPreferences(undefined),
    setSidebarCollapsed: (sidebarCollapsed) => set({ sidebarCollapsed }),
    setSidebarWidth: (width) => set({ sidebarWidth: clampSidebarWidth(width) }),
    setTerminalPanelHeight: (height) => set({ terminalPanelHeight: clampTerminalPanelHeight(height) }),
    setDockWidth: (panel, width) => set((state) => ({ dockWidths: { ...state.dockWidths, [panel]: clampDockWidth(panel)(width) } })),
  }),
  {
    name: "cosir:layout",
    partialize: ({ sidebarCollapsed, sidebarWidth, terminalPanelHeight, dockWidths }) => ({
      sidebarCollapsed, sidebarWidth, terminalPanelHeight, dockWidths,
    }),
    merge: (persisted, current) => ({ ...current, ...normalizeLayoutPreferences(persisted) }),
  },
));

const LAST_WORKSPACE_KEY = "cosir:last-workspace-id";

export function readLastWorkspaceId(): number | null {
  if (typeof window === "undefined") return null;
  const raw = window.localStorage.getItem(LAST_WORKSPACE_KEY);
  const value = raw === null ? NaN : Number(raw);
  return Number.isInteger(value) && value > 0 ? value : null;
}

export function writeLastWorkspaceId(workspaceId: number): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(LAST_WORKSPACE_KEY, String(workspaceId));
}

const SIDEBAR_COLLAPSED_KEY = "cosir:sidebar-collapsed";

/** 宽屏下侧栏是否收起的用户偏好；窄屏始终以图标栏 + 抽屉呈现，不读取它。 */
export function readSidebarCollapsed(): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === "true";
  } catch {
    return false;
  }
}

export function writeSidebarCollapsed(collapsed: boolean): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(SIDEBAR_COLLAPSED_KEY, String(collapsed));
  } catch {
    // 偏好写入失败（隐私模式等）只影响下次启动的默认状态，不影响当前会话。
  }
}

const SIDEBAR_WIDTH_KEY = "cosir:sidebar-width";

/**
 * 展开侧栏的宽度范围（px）。默认值放得下约 10 个汉字的任务标题；上限放得下后端标题上限
 * （`TASK_TITLE_LIMIT` = 20 个汉字）加省略号。
 */
export const SIDEBAR_WIDTH = { min: 240, default: 288, max: 420 } as const;

export function clampSidebarWidth(width: number): number {
  return Math.round(Math.max(SIDEBAR_WIDTH.min, Math.min(SIDEBAR_WIDTH.max, width)));
}

/** 展开侧栏的宽度偏好；缺失或非法时用默认值。 */
export function readSidebarWidth(): number {
  if (typeof window === "undefined") return SIDEBAR_WIDTH.default;
  try {
    const value = Number(window.localStorage.getItem(SIDEBAR_WIDTH_KEY));
    return Number.isFinite(value) && value > 0 ? clampSidebarWidth(value) : SIDEBAR_WIDTH.default;
  } catch {
    return SIDEBAR_WIDTH.default;
  }
}

export function writeSidebarWidth(width: number): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(SIDEBAR_WIDTH_KEY, String(width));
  } catch {
    // 同收起偏好：写入失败只影响下次启动的默认宽度。
  }
}

const TERMINAL_PANEL_HEIGHT_KEY = "cosir:terminal-panel-height";

/** 底部终端面板的高度（px）；上限随窗口高度变化，由面板在渲染时收敛，这里只保底下限。 */
export const TERMINAL_PANEL_HEIGHT = { min: 192, default: 320 } as const;

/** 拖拽得到的终端面板高度偏好（所有 Task 共用）；缺失或非法时用默认值。 */
export function readTerminalPanelHeight(): number {
  if (typeof window === "undefined") return TERMINAL_PANEL_HEIGHT.default;
  try {
    const value = Number(window.localStorage.getItem(TERMINAL_PANEL_HEIGHT_KEY));
    return Number.isFinite(value) && value >= TERMINAL_PANEL_HEIGHT.min ? Math.round(value) : TERMINAL_PANEL_HEIGHT.default;
  } catch {
    return TERMINAL_PANEL_HEIGHT.default;
  }
}

export function writeTerminalPanelHeight(height: number): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(TERMINAL_PANEL_HEIGHT_KEY, String(Math.round(height)));
  } catch {
    // 同侧栏偏好：写入失败只影响下次打开的默认高度。
  }
}

const WORKBENCH_WIDTH_KEY = "cosir:workbench-width";

/** 右侧工作台的宽度偏好范围（px）；窗口变窄时由布局在渲染时再收窄，不改写偏好。 */
export const WORKBENCH_WIDTH = { min: 320, default: 480, max: 900 } as const;

export function clampWorkbenchWidth(width: number): number {
  return Math.round(Math.max(WORKBENCH_WIDTH.min, Math.min(WORKBENCH_WIDTH.max, width)));
}

/** 工作台宽度偏好（所有工作区共用）；缺失或非法时用默认值。 */
export function readWorkbenchWidth(): number {
  if (typeof window === "undefined") return WORKBENCH_WIDTH.default;
  try {
    const value = Number(window.localStorage.getItem(WORKBENCH_WIDTH_KEY));
    return Number.isFinite(value) && value > 0 ? clampWorkbenchWidth(value) : WORKBENCH_WIDTH.default;
  } catch {
    return WORKBENCH_WIDTH.default;
  }
}

export function writeWorkbenchWidth(width: number): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(WORKBENCH_WIDTH_KEY, String(width));
  } catch {
    // 同侧栏偏好：写入失败只影响下次启动的默认宽度。
  }
}

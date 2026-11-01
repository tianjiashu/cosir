import { create } from "zustand";

import type { DockPanel } from "@/lib/layout/preferences";

/**
 * 右侧停靠区显示哪个面板：文件、内置浏览器、工作台（子 Agent 与配置草稿）三选一，同一时刻只显示一个，
 * 各自独立、不共用标签条。页头按钮各管一个，打开一个即替换另一个，点亮的再点一次收起。
 *
 * 只是界面状态，不持久化：面板里的内容各有事实源（文件状态在 `lib/workspace-files/store`，标签页是宿主
 * 事实，工作台标签在 `lib/workbench/store`），收起面板不丢任何内容。当前面板可以铺满对话区（`maximized`，
 * 是否提供铺满按钮由面板自己决定），收起或换到别的面板时还原；再次显示同一面板时保持。
 */
type DockState = {
  panel: DockPanel | null;
  maximized: boolean;
  show: (panel: DockPanel) => void;
  toggle: (panel: DockPanel) => void;
  close: () => void;
  setMaximized: (maximized: boolean) => void;
};

export const useDockStore = create<DockState>()((set) => ({
  panel: null,
  maximized: false,
  show: (panel) => set((state) => ({ panel, maximized: state.panel === panel && state.maximized })),
  toggle: (panel) => set((state) => (state.panel === panel ? { panel: null, maximized: false } : { panel, maximized: false })),
  close: () => set({ panel: null, maximized: false }),
  setMaximized: (maximized) => set({ maximized }),
}));

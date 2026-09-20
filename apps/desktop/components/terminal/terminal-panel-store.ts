import { create } from "zustand";

export type TerminalPanelTarget = {
  taskId: number;
  sessionId: string;
};

type TerminalPanelState = {
  target: TerminalPanelTarget | null;
  open: boolean;
  openSession: (target: TerminalPanelTarget) => void;
  close: () => void;
};

/**
 * Task UI scope 内的 terminal preview 面板状态。
 *
 * 这里只保存当前可视目标，不保存终端输出；输出由 xterm 实例消费，避免
 * 高频原始 bytes 进入 React/Zustand 状态树。面板永远没有输入动作。
 */
export const useTerminalPanelStore = create<TerminalPanelState>((set) => ({
  target: null,
  open: false,
  openSession: (target) => set({ target, open: true }),
  close: () => set({ open: false }),
}));

import { create } from "zustand";

/**
 * 浏览器面板正在看哪个 Task 的标签、各 Task 选中的是哪个标签。
 *
 * 默认看当前对话 Task；工具卡片上的「打开」可能来自子 Agent 的对话，所以打开时连同标签所属的 Task 一起
 * 指定。选中的标签已关闭时由面板回落到该 Task 最新的标签，这里不跟踪关闭。只是界面状态，不持久化。
 */
type BrowserPanelState = {
  /** 面板显示的 Task；null 表示跟随当前对话。 */
  taskId: number | null;
  selected: Readonly<Record<number, string>>;
  /** 打开某个 Task 的某个标签（工具卡片、`+` 新开后）。 */
  focus: (taskId: number, tabId: string) => void;
  select: (taskId: number, tabId: string) => void;
  /** 回到跟随当前对话（页头按钮打开面板时）。 */
  followConversation: () => void;
};

export const useBrowserPanelStore = create<BrowserPanelState>()((set) => ({
  taskId: null,
  selected: {},
  focus: (taskId, tabId) => set((state) => ({ taskId, selected: { ...state.selected, [taskId]: tabId } })),
  select: (taskId, tabId) => set((state) => ({ selected: { ...state.selected, [taskId]: tabId } })),
  followConversation: () => set({ taskId: null }),
}));

/**
 * 委派 UI 选中态管理（Zustand）。
 *
 * 单一职责：仅承载「当前在右侧面板中查看的子 Agent（child turn）」这一 UI 选中态。
 * 不持有事件流、不持有 projection、不发起网络/SSE——数据来自 eventStore 与 turnStore，
 * 渲染由 SubagentPanel 负责，本 store 只负责「用户点了哪一行 delegation」。
 *
 * 设计约束（来自 Task 1 brief）：
 * - 不引入 Utils/Helper 等含糊命名；状态与动作语义明确。
 * - 与 eventStore / turnStore 同层，属于 UI 选中态的轻量单一事实来源。
 *
 * @module stores/delegationStore
 */

import { create } from "zustand";

/** 委派 UI 选中态。 */
interface DelegationSelectionState {
  /** 当前在侧边栏查看的 child turn id；未选中时为 null。 */
  selectedChildTurnId: string | null;
}

/** 委派 UI 选中态动作。 */
interface DelegationSelectionActions {
  /**
   * 选中某个 child turn，使其在右侧面板展示完整 timeline。
   *
   * @param childTurnId - 待查看的 child turn 标识；应为非空字符串。
   *
   * @sideeffect 更新 selectedChildTurnId，触发订阅组件（如 SubagentPanel）重渲染。
   */
  selectChildTurn: (childTurnId: string) => void;

  /**
   * 清空当前选中态，侧边栏回到未选中空态。
   *
   * @sideeffect 将 selectedChildTurnId 置为 null，触发订阅组件回到空态渲染。
   */
  clearSelection: () => void;
}

/**
 * 委派 UI 选中态 Zustand Store 实例。
 *
 * @returns Zustand hook；组件调用后可读取 selectedChildTurnId、调用 selectChildTurn/clearSelection。
 *
 * @throws 不主动抛出异常。
 *
 * @sideeffect action 仅更新前端内存状态；不发起 HTTP、IPC 或写日志。
 */
export const useDelegationStore = create<DelegationSelectionState & DelegationSelectionActions>(
  (set) => ({
    // --- 初始状态 ---
    selectedChildTurnId: null,

    // --- 动作 ---
    selectChildTurn: (childTurnId: string) => {
      set({ selectedChildTurnId: childTurnId });
    },

    clearSelection: () => {
      set({ selectedChildTurnId: null });
    },
  }),
);

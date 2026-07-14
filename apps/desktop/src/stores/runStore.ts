/**
 * Durable Run State 前端状态 Store。
 *
 * @module stores/runStore
 */

import { create } from "zustand";
import type { RunRecord } from "@shared/runs";

/** Run Store 状态。 */
interface RunState {
  /** 可恢复运行列表。 */
  recoverableRuns: RunRecord[];
  /** 当前是否正在加载恢复状态。 */
  isLoadingRecoverableRuns: boolean;
  /** 最近一次恢复相关错误。 */
  recoveryError: string | null;
}

/** Run Store 动作。 */
interface RunActions {
  /** 覆盖可恢复运行列表。 */
  setRecoverableRuns: (runs: RunRecord[]) => void;
  /** 设置恢复加载状态。 */
  setLoadingRecoverableRuns: (loading: boolean) => void;
  /** 设置恢复错误。 */
  setRecoveryError: (message: string | null) => void;
  /** 清空恢复状态。 */
  resetRunState: () => void;
}

/**
 * Durable Run State Zustand Store。
 *
 * 只保存状态和纯动作，不发起 HTTP、SSE、IPC 或日志副作用。
 */
export const useRunStore = create<RunState & RunActions>((set) => ({
  recoverableRuns: [],
  isLoadingRecoverableRuns: false,
  recoveryError: null,

  setRecoverableRuns: (runs) => set({ recoverableRuns: runs }),
  setLoadingRecoverableRuns: (loading) => set({ isLoadingRecoverableRuns: loading }),
  setRecoveryError: (message) => set({ recoveryError: message }),
  resetRunState: () =>
    set({
      recoverableRuns: [],
      isLoadingRecoverableRuns: false,
      recoveryError: null,
    }),
}));

/**
 * 本地后端托管状态 Store。
 *
 * 管理桌面端感知到的本地 Python 后端状态，
 * 作为 TopBar、Hook 与后续诊断面板的单一事实源。
 *
 * @module stores/backendStore
 */

import { create } from "zustand";
import type { BackendStatusResponse, DesktopBackendStatus } from "@shared/backend";

/** 后端 Store 的状态接口。 */
interface BackendState {
  /** 当前后端生命周期状态。 */
  status: DesktopBackendStatus;
  /** 最近一次完整状态快照。 */
  snapshot: BackendStatusResponse | null;
  /** 当前是否有未完成的命令。 */
  isBusy: boolean;
  /** 最近一次调用层错误消息。 */
  transportError: string | null;
}

/** 后端 Store 的动作接口。 */
interface BackendActions {
  /** 用新快照覆盖当前状态。 */
  applySnapshot: (snapshot: BackendStatusResponse) => void;
  /** 标记当前有后台命令进行中。 */
  setBusy: (busy: boolean) => void;
  /** 单独设置当前状态文本。 */
  setStatus: (status: DesktopBackendStatus) => void;
  /** 记录 transport 层错误。 */
  setTransportError: (message: string | null) => void;
  /** 清空状态。 */
  reset: () => void;
}

/**
 * 本地后端托管 Zustand Store。
 *
 * 只存储状态和纯动作，不直接发起 IPC 调用。
 */
export const useBackendStore = create<BackendState & BackendActions>((set) => ({
  status: "stopped",
  snapshot: null,
  isBusy: false,
  transportError: null,

  applySnapshot: (snapshot) => {
    set({
      snapshot,
      status: snapshot.status,
      transportError: null,
    });
  },

  setBusy: (busy) => {
    set({ isBusy: busy });
  },

  setStatus: (status) => {
    set({ status });
  },

  setTransportError: (message) => {
    set({ transportError: message });
  },

  reset: () => {
    set({
      status: "stopped",
      snapshot: null,
      isBusy: false,
      transportError: null,
    });
  },
}));

/**
 * 选择当前后端状态。
 *
 * @param state - 后端 Store 状态。
 * @returns 当前生命周期状态。
 */
export function selectBackendStatus(state: BackendState): DesktopBackendStatus {
  return state.status;
}

/**
 * 选择最近一次完整快照。
 *
 * @param state - 后端 Store 状态。
 * @returns 最近一次完整快照。
 */
export function selectBackendSnapshot(state: BackendState): BackendStatusResponse | null {
  return state.snapshot;
}

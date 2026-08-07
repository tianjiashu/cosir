/**
 * 轮次状态管理（Zustand）。
 *
 * 管理 task 下 turn 列表和当前 streaming turn。
 *
 * @module stores/turnStore
 */

import { create } from "zustand";
import type { TurnRecord } from "@shared/turn";

/** 轮次 Store 状态接口。 */
interface TurnState {
  /** 按 task_id 聚合的轮次列表。 */
  turnsByTaskId: Record<string, TurnRecord[]>;
  /** 当前正在接收 SSE 的轮次标识。 */
  streamingTurnId: string | null;
}

/** 轮次 Store 动作接口。 */
interface TurnActions {
  /** 替换指定 task 下的轮次列表。 */
  setTurnsForTask: (taskId: string, turns: TurnRecord[]) => void;
  /** 添加或替换一个轮次。 */
  upsertTurn: (turn: TurnRecord) => void;
  /** 按 task_id/turn_id 局部更新一个轮次。 */
  updateTurn: (taskId: string, turnId: string, updates: Partial<TurnRecord>) => void;
  /**
   * 用真实轮次整体替换临时轮次（乐观更新回写）。
   *
   * 前端在用户输入后先用临时 turn_id 乐观插入以便立即渲染，待后端返回真实
   * turn 后调用本方法把临时记录整条替换为真实记录，保证 turn_id 与后续 SSE
   * 事件流对齐。临时与真实记录的 input_text 保持一致，渲染层无感知。
   */
  replaceTurnId: (taskId: string, oldTurnId: string, realTurn: TurnRecord) => void;
  /** 按 task_id/turn_id 移除一个轮次（乐观更新失败回滚）。 */
  removeTurnId: (taskId: string, turnId: string) => void;
  /** 设置当前 streaming turn。 */
  setStreamingTurn: (turnId: string | null) => void;
}

/**
 * 轮次 Zustand Store 实例。
 *
 * @returns Zustand hook；组件调用后可读取 task 下 turn 列表和当前 streaming turn。
 *
 * @throws 不主动抛出异常。
 *
 * @sideeffect action 会更新前端内存状态；不会直接发起 HTTP、IPC 或写日志。
 */
export const useTurnStore = create<TurnState & TurnActions>((set) => ({
  turnsByTaskId: {},
  streamingTurnId: null,

  setTurnsForTask: (taskId, turns) => {
    set((state) => ({
      turnsByTaskId: { ...state.turnsByTaskId, [taskId]: turns },
    }));
  },

  upsertTurn: (turn) => {
    set((state) => {
      const current = state.turnsByTaskId[turn.task_id] ?? [];
      return {
        turnsByTaskId: {
          ...state.turnsByTaskId,
          [turn.task_id]: [...current.filter((item) => item.turn_id !== turn.turn_id), turn],
        },
      };
    });
  },

  updateTurn: (taskId, turnId, updates) => {
    set((state) => ({
      turnsByTaskId: {
        ...state.turnsByTaskId,
        [taskId]: (state.turnsByTaskId[taskId] ?? []).map((turn) =>
          turn.turn_id === turnId ? { ...turn, ...updates } : turn,
        ),
      },
    }));
  },

  setStreamingTurn: (turnId) => {
    set({ streamingTurnId: turnId });
  },

  replaceTurnId: (taskId, oldTurnId, realTurn) => {
    set((state) => {
      const current = state.turnsByTaskId[taskId] ?? [];
      const filtered = current.filter((item) => item.turn_id !== oldTurnId);
      return {
        turnsByTaskId: {
          ...state.turnsByTaskId,
          [taskId]: [...filtered, realTurn],
        },
      };
    });
  },

  removeTurnId: (taskId, turnId) => {
    set((state) => {
      const current = state.turnsByTaskId[taskId] ?? [];
      return {
        turnsByTaskId: {
          ...state.turnsByTaskId,
          [taskId]: current.filter((item) => item.turn_id !== turnId),
        },
      };
    });
  },
}));

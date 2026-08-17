/**
 * 轮次状态管理（Zustand）。
 *
 * 管理 task 下 turn 列表，以及按 task 维度隔离的当前 streaming turn。
 *
 * 设计要点：桌面端支持多 task 并发流式，因此「当前正在接收 SSE 的 turn」不能
 * 用跨所有 task 共享的单一字段表达，否则并发下一个 task 的写入会覆盖另一个，
 * 某 task 终态清空还会误清别的仍在跑的 task。故以 Record<taskId, turnId> 按
 * task 隔离，每个 task 独立记录自己的 streaming turn。
 *
 * @module stores/turnStore
 */

import { create } from "zustand";
import type { TurnRecord } from "@shared/turn";

/** 轮次 Store 状态接口。 */
interface TurnState {
  /** 按 task_id 聚合的轮次列表。 */
  turnsByTaskId: Record<string, TurnRecord[]>;
  /**
   * 按 task_id 维度隔离的「当前正在接收 SSE 的轮次标识」映射。
   *
   * 桌面端支持多 task 并发流式，故不能用单一 cross-task 字段，否则并发下会互相
   * 覆盖、误清。每个 task 独立记录自己的 streaming turn；未运行的 task 不在此映射
   * 中（读取时用 `streamingTurnIds[taskId] ?? null`）。
   */
  streamingTurnIds: Record<string, string>;
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
  /**
   * 按 task_id 设置该 task 的当前 streaming turn。
   *
   * @param taskId - 目标 task 标识。
   * @param turnId - 该 task 正在接收 SSE 的 turn 标识；传 null 表示清除该 task 的
   *   streaming 标记（终态/取消时调用）。其它 task 的 streaming 标记不受影响，
   *   不会被误清空，从而支持多 task 并发流式时各自独立停止。
   */
  setStreamingTurn: (taskId: string, turnId: string | null) => void;
}

/**
 * 轮次 Zustand Store 实例。
 *
 * @returns Zustand hook；组件调用后可读取 task 下 turn 列表，以及按 task 维度隔离的
 *   当前 streaming turn。
 *
 * @throws 不主动抛出异常。
 *
 * @sideeffect action 会更新前端内存状态；不会直接发起 HTTP、IPC 或写日志。
 */
export const useTurnStore = create<TurnState & TurnActions>((set) => ({
  turnsByTaskId: {},
  streamingTurnIds: {},

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

  setStreamingTurn: (taskId, turnId) => {
    set((state) => {
      if (turnId === null) {
        if (!(taskId in state.streamingTurnIds)) return state;
        const next = { ...state.streamingTurnIds };
        delete next[taskId];
        return { streamingTurnIds: next };
      }
      return { streamingTurnIds: { ...state.streamingTurnIds, [taskId]: turnId } };
    });
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

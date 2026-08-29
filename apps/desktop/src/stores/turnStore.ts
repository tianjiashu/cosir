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
  /**
   * 按 task_id（后端 int 主键）聚合的轮次列表。
   *
   * 键类型为 number（与 TaskRecord.task_id / TurnRecord.task_id 一致），不再经由
   * Object.entries 的 string 中间态，避免 number↔string 边界失配。
   */
  turnsByTaskId: Record<number, TurnRecord[]>;
  /**
   * 按 task_id（后端 int 主键）维度隔离的「当前正在接收 SSE 的轮次标识」映射。
   *
   * 桌面端支持多 task 并发流式，故不能用单一 cross-task 字段，否则并发下会互相
   * 覆盖、误清。每个 task 独立记录自己的 streaming turn；未运行的 task 不在此映射
   * 中（读取时用 `streamingTurnIds[taskId] ?? null`）。
   *
   * 键类型为 number（task_id）；值类型为 number（真实 turn_id，后端 int 主键）。
   * 乐观更新的临时 turn（"temp-" 前缀字符串）不写入本映射——它仅在真实 turn 返回后
   * 经 replaceTurnId 落库时以 number 键写入，避免污染 number 键空间。
   */
  streamingTurnIds: Record<number, number>;
}

/** 轮次 Store 动作接口。 */
interface TurnActions {
  /**
   * 替换指定 task 下的轮次列表。
   *
   * @param taskId - 目标 task 标识（后端 int 主键）。
   * @param turns - 该 task 的完整轮次列表。
   */
  setTurnsForTask: (taskId: number, turns: TurnRecord[]) => void;
  /** 添加或替换一个轮次。 */
  upsertTurn: (turn: TurnRecord) => void;
  /**
   * 按 task_id/turn_id 局部更新一个轮次。
   *
   * @param taskId - 目标 task 标识（后端 int 主键）。
   * @param turnId - 目标 turn 标识（后端 int 主键）。
   * @param updates - 待合并到该 turn 的局部字段。
   */
  updateTurn: (taskId: number, turnId: number, updates: Partial<TurnRecord>) => void;
  /**
   * 用真实轮次整体替换临时轮次（乐观更新回写）。
   *
   * 前端在用户输入后先用负 number 占位 turn_id（`-1 - seq`）乐观插入以便立即渲染，
   * 该占位与真实 turn 同为 number 类型，与 `TurnRecord.turn_id` 一致；待后端返回真实
   * turn（number 主键）后调用本方法把临时记录整条替换为真实记录，保证 turn_id 与后续
   * SSE 事件流对齐。临时与真实记录的 input_text 保持一致，渲染层无感知。
   *
   * 临时 id 的 string 维度（"temp-<uuid>"）仅用于 SSE 连接键（useSSE.connectionsRef 为
   * `Map<string>`），不写入 store 的 number 字段，故本方法定位临时记录用 number 占位
   * `oldTurnId` 直接比对 `item.turn_id`（number），语义正确。
   *
   * @param taskId - 目标 task 标识（后端 int 主键）。
   * @param oldTurnId - 被替换的临时 turn 占位（number，乐观更新期间由前端生成的负 number，
   *   如 `-1 - seq`）；用于在 `turnsByTaskId[taskId]` 中定位并移除临时记录。
   * @param realTurn - 后端返回的真实轮次（turn_id 为 number 主键）。
   */
  replaceTurnId: (taskId: number, oldTurnId: number, realTurn: TurnRecord) => void;
  /**
   * 按 task_id/turn_id 移除一个轮次（乐观更新失败回滚）。
   *
   * @param taskId - 目标 task 标识（后端 int 主键）。
   * @param turnId - 目标 turn 标识（后端 int 主键）。
   */
  removeTurnId: (taskId: number, turnId: number) => void;
  /**
   * 按 task_id 设置该 task 的当前 streaming turn。
   *
   * @param taskId - 目标 task 标识（后端 int 主键）。
   * @param turnId - 该 task 正在接收 SSE 的 turn 标识（后端 int 主键）；传 null 表示清除
   *   该 task 的 streaming 标记（终态/取消时调用）。其它 task 的 streaming 标记不受影响，
   *   不会被误清空，从而支持多 task 并发流式时各自独立停止。
   */
  setStreamingTurn: (taskId: number, turnId: number | null) => void;
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

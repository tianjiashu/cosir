/**
 * 上下文窗口占用状态管理（Zustand）。
 *
 * 管理：各任务上下文窗口的输入侧 token 占用（来自后端 CONTEXT_USAGE 事件）。
 * 该值基于 RuntimeContext.messages 本地估算，不依赖模型 usage_metadata，因此 turn
 * 中途取消也会因消息已在上下文中而保留最近一次占用，前端圆环不会因取消而失真。
 *
 * 与 eventStore 解耦：上下文占用是「最新值覆盖」语义（非事件历史），单独成 store
 * 避免 InputBar 订阅整个事件流造成不必要重渲染。
 *
 * 按 task 维度存储（而非全局单值）：桌面端支持多 task 并发流式，后台 task 的
 * CONTEXT_USAGE 事件同样会被 SSE 收到。全局单值会让后台事件覆盖当前任务圆环，
 * 也会让「任意连接建立时清零」这类副作用误伤正在查看的任务。按 taskId 分键后，
 * 写入方无需感知「当前是哪个任务」，读取方按 activeTaskId 选择，二者解耦。
 * 键类型为 number，与 TaskRecord.task_id / tasksById / drafts 的 idUnify 约定一致。
 *
 * 本 store 是占用数值的**唯一写入点**（SSE 事件与 openTask 回填都经 ``setUsage``），
 * 因此「两个计数都必须是有限非负数」这一不变量在此处收口，下游（圆环等）可直接信任。
 * 注意**不保证** used <= total：占用超出窗口是真实状态（圆环按 100% 封顶显示实际
 * 数值更有诊断价值），故此处不做 clamp。
 *
 * @module stores/contextUsageStore
 */

import { create } from "zustand";
import type { ContextUsagePayload } from "@shared/events";
import { logWarn } from "@/lib/logger";

/** 单个任务的上下文占用快照。 */
export interface TaskUsageEntry {
  /** 已用 token（输入侧估算）。 */
  usedTokens: number;
  /** 实际上下文窗口上限（token）= min(模型最大窗口, 全局软上限)。 */
  totalTokens: number;
  /** 最近一次更新的事件时间，用于排查与排序。 */
  updatedAt: string | null;
}

/**
 * 无占用数据时的共享空快照。
 *
 * 选择器在「无 active task」或「该 task 尚无条目」时必须返回本常量，不得内联新建
 * 对象：zustand 默认以引用相等判定是否触发重渲染，每次调用返回新对象会让组件在
 * 任何 store 变更下都重新渲染（直至无限循环）。
 */
export const EMPTY_USAGE: TaskUsageEntry = Object.freeze({
  usedTokens: 0,
  totalTokens: 0,
  updatedAt: null,
});

/** 上下文占用 Store 的状态接口。 */
interface ContextUsageState {
  /**
   * 按 task_id（后端 int 主键）索引的上下文占用快照。
   *
   * 条目由 taskStore 的三条删除路径（removeTask / clearWorkspaceTasks / 清空全部）
   * 同步清理，规模受 tasksById 约束，不存在无界增长。
   */
  usageByTaskId: Record<number, TaskUsageEntry>;
}

/** 上下文占用 Store 的动作接口。 */
interface ContextUsageActions {
  /** 用后端 CONTEXT_USAGE 事件的最新占用覆盖指定任务的状态。 */
  setUsage: (taskId: number, payload: ContextUsagePayload, updatedAt: string) => void;
  /** 清除单个任务的占用（如该任务被删除、或后端无占用数据时复位）。 */
  resetTask: (taskId: number) => void;
  /** 清空全部任务的占用（清空工作区 / 会话时使用）。 */
  clearAll: () => void;
}

/**
 * 把来源不可控的 token 计数归一为有限非负数。
 *
 * 占用数据来自 SSE 事件，写入链路是 ``event.payload as ContextUsagePayload``——一个
 * 编译期断言，不做任何运行时校验。后端契约违约（字段缺失、类型不符、NaN、负数）时，
 * 未归一的值会让圆环算出 ``undefined / undefined`` 并渲染出 ``NaN%``。归一到 0 后
 * 圆环回落 0.0%，而不是显示无意义的字面量。
 *
 * @param value - 待归一的 token 计数。
 * @returns 有限且非负的计数；契约违约时返回 0。
 */
function toTokenCount(value: number | undefined | null): number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : 0;
}

/**
 * 单任务内两条非法 payload 日志的最小间隔（毫秒）。
 *
 * 占用事件随每个模型步下发，后端若持续下发残缺 payload，不加节流会每条事件落一条
 * warn，把日志文件冲垮并淹没其它问题（规范六：禁止无边界循环日志）。按任务节流后
 * 仍保留「哪个任务的数据有问题」这一关键线索。
 */
const MALFORMED_WARN_INTERVAL_MS = 60_000;

/**
 * task_id → 上次告警时间戳（毫秒），用于节流。
 *
 * 随 ``resetTask`` / ``clearAll`` 一并清理：任务号可能被后续新建的任务复用（SQLite
 * rowid 复用），残留的节流状态会让新任务首个非法 payload 被静默跳过。
 */
const malformedWarnedAt = new Map<number, number>();

/**
 * 校验占用数值是否满足协议，不满足时按任务节流落 warn 日志。
 *
 * 归一本身是静默的，若不同步记录，后端偶发下发残缺 payload 会表现为「圆环一直是 0%」
 * 而无任何可排查线索。
 *
 * @param taskId - 事件所属任务，用于定位是哪条流的数据有问题。
 * @param payload - 原始事件 payload。
 */
function warnIfMalformed(taskId: number, payload: ContextUsagePayload): void {
  const used = (payload as { used_tokens?: unknown }).used_tokens;
  const total = (payload as { total_tokens?: unknown }).total_tokens;
  const valid = (v: unknown): boolean => typeof v === "number" && Number.isFinite(v) && v >= 0;
  if (valid(used) && valid(total)) {
    return;
  }
  const now = Date.now();
  const last = malformedWarnedAt.get(taskId);
  if (last != null && now - last < MALFORMED_WARN_INTERVAL_MS) {
    return;
  }
  malformedWarnedAt.set(taskId, now);
  logWarn("上下文占用事件 payload 不合法，已按 0 处理（同任务内已节流）", {
    task_id: taskId,
    used_tokens: used,
    total_tokens: total,
  });
}

export const useContextUsageStore = create<ContextUsageState & ContextUsageActions>((set) => ({
  usageByTaskId: {},
  setUsage: (taskId, payload, updatedAt) => {
    warnIfMalformed(taskId, payload);
    set((state) => ({
      usageByTaskId: {
        ...state.usageByTaskId,
        [taskId]: {
          usedTokens: toTokenCount(payload.used_tokens),
          totalTokens: toTokenCount(payload.total_tokens),
          updatedAt,
        },
      },
    }));
  },
  resetTask: (taskId) =>
    set((state) => {
      if (!(taskId in state.usageByTaskId)) {
        // 无条目时返回原对象，避免无意义的引用变更触发订阅者重渲染。
        return state;
      }
      const usageByTaskId = { ...state.usageByTaskId };
      delete usageByTaskId[taskId];
      malformedWarnedAt.delete(taskId);
      return { usageByTaskId };
    }),
  clearAll: () => {
    malformedWarnedAt.clear();
    set({ usageByTaskId: {} });
  },
}));

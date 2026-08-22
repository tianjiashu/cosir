/**
 * 输入框可用性与提交互斥状态（纯逻辑层，零 React 渲染）。
 *
 * 借鉴 deepseek-harness「machine（状态）→ decorations（派生）→ UI 只渲染」的分层
 * 思路（``docs/输入组件优化.md`` §4.5），把发送/停止可用性从 InputBar 组件内联
 * 布尔推导中抽出为可单测纯函数，使 InputBar 变薄。
 *
 * 职责边界：
 * - 本文件只负责「可发送/可停止/提交中」的派生与同步互斥锁。
 * - 发送事务（guard/创建任务或轮次/trace/perf/日志/失败回滚）由 ``useSendInput.ts`` 承担。
 * - 草稿持久化由 ``taskStore.setInputDraft`` 承担。
 *
 * @module components/layout/useInputState
 */

import { useCallback, useRef, useState } from "react";

/**
 * 输入框提交阶段（借鉴外部 phase 状态机，语义简化）。
 *
 * - ``idle``：空闲，可编辑可发送。
 * - ``submitting``：提交事务在飞（guard 校验/创建任务或轮次中），结构性锁防连点。
 */
export type InputPhase = "idle" | "submitting";

/**
 * 输入可用性派生入参（纯函数参数，可单测）。
 */
export interface InputAvailabilityInput {
  /** 草稿 trim 后是否非空。 */
  hasDraftText: boolean;
  /** 是否有创建/取消等操作在飞（useTask.operation.loading）。 */
  isLoading: boolean;
  /** 当前任务是否有运行中的流式轮次。 */
  isStreaming: boolean;
  /** 是否处于提交事务中（同步互斥锁位）。 */
  isSubmitting: boolean;
  /** 是否有发送上下文（活跃任务或活跃工作区）。 */
  hasContext: boolean;
  /** 是否已显式选择模型（无 Auto 语义，未选择即禁用发送）。 */
  hasModelSelected: boolean;
}

/**
 * 输入可用性派生结果。
 */
export interface InputAvailability {
  /** 是否可发送。 */
  canSend: boolean;
  /** 是否可停止当前流式轮次。 */
  canStop: boolean;
}

/**
 * 从状态派生输入框可用性（纯函数，零副作用）。
 *
 * 与原 InputBar 内联 ``canSend`` 推导语义完全一致，仅外置为纯函数便于单测：
 * ``canSend = 非空 && 非加载 && 非流式 && 非提交中 && 有上下文 && 已选模型``；
 * ``canStop = 流式中 && 非加载``。
 *
 * @param input - 派生所需快照输入。
 * @returns 可发送/可停止两个布尔位。
 */
export function deriveInputAvailability(input: InputAvailabilityInput): InputAvailability {
  const canSend =
    input.hasDraftText &&
    !input.isLoading &&
    !input.isStreaming &&
    !input.isSubmitting &&
    input.hasContext &&
    input.hasModelSelected;
  const canStop = input.isStreaming && !input.isLoading;
  return { canSend, canStop };
}

/**
 * 输入状态 hook 返回值。
 */
export interface UseInputStateReturn {
  /** 当前提交阶段。 */
  phase: InputPhase;
  /** 是否可发送（已含提交互斥）。 */
  canSend: boolean;
  /** 是否可停止当前流式轮次。 */
  canStop: boolean;
  /** 是否处于提交事务中（同步锁位，供 UI 冻结样式）。 */
  isSubmitting: boolean;
  /**
   * 尝试进入提交阶段（同步锁，无异步缝隙）。
   *
   * 对应外部「enter 即切 phase」的防连点双提交互斥。若已在提交中返回 false，
   * 调用方应立即中止本次发送（防连点双提交竞态窗口）。
   *
   * @returns 成功进入提交阶段返回 true；已在提交中返回 false。
   */
  beginSubmit: () => boolean;
  /** 结束提交阶段（无论成败，必须与 beginSubmit 配对，在 finally 中调用）。 */
  endSubmit: () => void;
}

/**
 * 输入框状态 hook：提交互斥 + 可用性派生。
 *
 * @param input - 派生所需快照（草稿非空/加载/流式/上下文/模型选择）。
 * @returns 阶段、可用性派生与提交互斥锁。
 */
export function useInputState(input: Omit<InputAvailabilityInput, "isSubmitting">): UseInputStateReturn {
  const [phase, setPhase] = useState<InputPhase>("idle");
  // 同步提交锁：enter 即锁，避免依赖异步 operation.loading 造成的连点双提交竞态窗口。
  const submittingRef = useRef(false);

  const beginSubmit = useCallback((): boolean => {
    if (submittingRef.current) {
      return false;
    }
    submittingRef.current = true;
    setPhase("submitting");
    return true;
  }, []);

  const endSubmit = useCallback((): void => {
    submittingRef.current = false;
    setPhase("idle");
  }, []);

  const { canSend, canStop } = deriveInputAvailability({ ...input, isSubmitting: submittingRef.current });

  return {
    phase,
    canSend,
    canStop,
    isSubmitting: submittingRef.current,
    beginSubmit,
    endSubmit,
  };
}

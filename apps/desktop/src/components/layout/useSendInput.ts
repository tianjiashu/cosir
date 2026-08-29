/**
 * 输入框发送事务编排（副作用外置层）。
 *
 * 借鉴 deepseek-harness「facade/hub 承担事务与副作用，UI 只喂事件读快照」的分层
 * 思路（``docs/输入组件优化.md`` §5），把 InputBar 组件内联的发送副作用
 * （guard 校验 / 创建任务或轮次 / trace 传播 / 性能埋点 / 日志 / 失败回滚草稿）
 * 收进本 hook，让组件只负责渲染与键盘事件分发。
 *
 * 职责边界：
 * - 本文件只负责「一次发送/停止的完整事务」。
 * - 可用性派生与同步互斥锁由 ``useInputState.ts`` 承担。
 * - 草稿持久化由 ``taskStore.setInputDraft`` 承担；store 侧的写入/清理时机下沉到
 *   ``useTask``（业务层掌握真实 task_id 的诞生时机），本层只清「已有 activeTaskId」
 *   场景下的草稿，并负责本地输入框的乐观清空与失败回填。
 *
 * @module components/layout/useSendInput
 */

import { useCallback } from "react";
import { logError, logInfo, logWarn } from "@/lib/logger";
import { PerfTrace } from "@/lib/perf";
import { beginClientTrace, endClientTrace } from "@/services/tracePropagation";
import { useClientTraceStore } from "@/stores/clientTraceStore";
import type { ModelSendGuardResult } from "@/hooks/useModelSendGuard";
import type { UseInputStateReturn } from "./useInputState";

/**
 * useSendInput 依赖参数。
 */
export interface UseSendInputParams {
  /** 当前活跃任务 ID（追加轮次场景；后端 int 主键，number 主通道）。 */
  activeTaskId: number | null;
  /** 当前活跃工作区 ID（新建任务场景；后端 int 主键，方案 §3.7 收敛为 number）。 */
  activeWorkspaceId: number | null;
  /** 发送前模型校验（异步，含缓存补拉）。 */
  guardSend: () => Promise<ModelSendGuardResult>;
  /** 给当前任务追加轮次。 */
  createTurn: (text: string) => Promise<boolean>;
  /** 在工作区新建任务（workspaceId 为后端 int 主键，方案 §3.7 收敛为 number）。 */
  createTask: (text: string, workspaceId: number) => Promise<boolean>;
  /** 取消当前流式轮次。 */
  cancelTurn: () => Promise<void>;
  /** 当前流式轮次 ID（停止日志用；后端 int 主键，number 主通道）。 */
  streamingTurnId: number | null;
  /** 写入指定任务的输入草稿（成功清空/失败回滚用；taskId 为后端 int 主键，number）。 */
  setInputDraft: (taskId: number, draft: string) => void;
  /** 写入模型校验拦截提示（内联展示）。 */
  setGuardMessage: (message: string | null) => void;
  /** 拦截规则要求打开配置中心时的联动回调。 */
  onOpenSettings?: () => void;
}

/**
 * useSendInput 返回值。
 */
export interface UseSendInputReturn {
  /**
   * 执行一次发送事务（含同步互斥锁与失败回滚草稿）。
   *
   * @param text - 已 trim 的输入文本。
   * @param local - 组件本地输入框生命周期回调（乐观清空/失败回滚）。
   */
  send: (text: string, local: { commit: () => void; restore: (restored: string) => void }) => Promise<void>;
  /** 停止当前流式轮次。 */
  stop: () => Promise<void>;
}

/**
 * 输入框发送事务 hook。
 *
 * @param params - 依赖（任务/工作区/守卫/动作/草稿写入/提示/回调）。
 * @param inputState - useInputState 返回的可用性与提交互斥锁。
 * @returns 发送与停止两个动作。
 */
export function useSendInput(params: UseSendInputParams, inputState: UseInputStateReturn): UseSendInputReturn {
  const {
    activeTaskId,
    activeWorkspaceId,
    guardSend,
    createTurn,
    createTask,
    cancelTurn,
    streamingTurnId,
    setInputDraft,
    setGuardMessage,
    onOpenSettings,
  } = params;
  const { canSend, canStop, beginSubmit, endSubmit } = inputState;

  const send = useCallback(
    async (text: string, local: { commit: () => void; restore: (restored: string) => void }) => {
      // 防御性检查：不可发送（空草稿/加载中/流式中/提交中/无上下文/未选模型）直接拒绝。
      if (!canSend) {
        return;
      }
      // 同步锁：enter 即锁，无异步缝隙（防连点双提交）。
      if (!beginSubmit()) {
        return;
      }
      try {
        const guard = await guardSend();
        if (!guard.ok) {
          setGuardMessage(guard.block.message);
          if (guard.block.openSettings) {
            onOpenSettings?.();
          }
          logWarn("发送被模型校验拦截", {
            module: "useSendInput",
            reason: guard.block.reason,
            message: guard.block.message,
            openSettings: guard.block.openSettings,
            hasActiveTask: Boolean(activeTaskId),
            workspace: activeWorkspaceId ?? null,
          });
          return;
        }
        setGuardMessage(null);

        beginClientTrace();
        const perf = PerfTrace.startCurrent(
          "user-input-to-render",
          useClientTraceStore.getState().currentTrace?.traceId,
        );
        logInfo("用户提交任务输入", { module: "useSendInput", input_preview: text.slice(0, 100), trace_id: perf.traceId });

        try {
          // 发送前的草稿快照：失败时据此回滚（失败不丢草稿，借鉴外部 restore 原则）。
          const draftSnapshot = text;
          // 乐观清空：先清 store 草稿与本地输入框再发送，成功即完成；失败时回滚。
          // 新建任务场景 activeTaskId 为 null，草稿由 useTask.createTask 挂在乐观占位
          // ID 上（转正时迁移到真实 ID），故此处不写 store，避免误清。
          if (activeTaskId) {
            setInputDraft(activeTaskId, "");
          }
          local.commit();

          let succeeded = false;
          if (activeTaskId) {
            perf.mark("handleSend:before-createTurn", { task_id: activeTaskId });
            succeeded = await createTurn(text);
            perf.mark("handleSend:after-createTurn", { task_id: activeTaskId });
          } else if (activeWorkspaceId) {
            perf.mark("handleSend:before-createTask", { workspace_id: activeWorkspaceId });
            succeeded = await createTask(text, activeWorkspaceId);
            perf.mark("handleSend:after-createTask", { workspace_id: activeWorkspaceId });
          }
          perf.mark("handleSend:done", { succeeded });

          // 失败/异常一律把草稿回填本地输入框（失败不丢草稿）。
          // store 侧草稿的写入与清理下沉到 useTask（业务层最清楚真实 task_id 的诞生时机），
          // 本层只负责本地输入框这一表现层状态，不再跨层猜测该往哪个 task_id 回滚。
          if (!succeeded) {
            local.restore(draftSnapshot);
          }
        } catch (err) {
          logError("handleSend: 发送任务输入抛出未捕获异常", err instanceof Error ? err : new Error(String(err)), {
            module: "useSendInput",
            input_preview: text.slice(0, 100),
          });
          // 异常同样回滚草稿（失败不丢草稿）。
          local.restore(text);
        } finally {
          endClientTrace();
        }
      } finally {
        endSubmit();
      }
    },
    [activeTaskId, activeWorkspaceId, beginSubmit, canSend, createTask, createTurn, endSubmit, guardSend, onOpenSettings, setGuardMessage, setInputDraft],
  );

  const stop = useCallback(async () => {
    if (!canStop) {
      return;
    }
    beginClientTrace();
    logInfo("用户请求停止当前轮次", { module: "useSendInput", turn_id: streamingTurnId });
    try {
      await cancelTurn();
    } catch (err) {
      logError("handleStop: 停止当前轮次抛出未捕获异常", err instanceof Error ? err : new Error(String(err)), {
        module: "useSendInput",
        turn_id: streamingTurnId,
      });
    } finally {
      endClientTrace();
    }
  }, [cancelTurn, canStop, streamingTurnId]);

  return { send, stop };
}

import { useEffect, useMemo } from "react";

import type { TransportIssue } from "@/components/assistant/transport-status";
import type { RuntimeSessionContext } from "@/components/assistant/runtime/runtime-types";
import {
  SubscriptionRecovery,
  type ReconnectReason,
} from "@/components/assistant/runtime/subscription-recovery";

export type RuntimeRecovery = {
  /** 长订阅意外结束后的有限重连；背压重同步立即重开、不消耗预算。 */
  reconnect: (reason: ReconnectReason) => void;
  /** 订阅已建立：撤下断流提示与待执行的重连；连接稳定后再断开才清零预算。 */
  markConnected: () => void;
  /** 显式重新同步（「重新同步」按钮）：清零预算后立即重开订阅。 */
  resync: (issue?: TransportIssue | null) => void;
  /** 后端恢复或换实例：清零预算并提示；订阅由 runtime 按新的后端实例自行重开。 */
  restored: (issue: TransportIssue | null) => void;
  /** 后端不可用：停止计时器，等待 restored。 */
  suspend: () => void;
};

/** 会话内的订阅恢复：把 {@link SubscriptionRecovery} 绑定到会话上下文与组件生命周期。 */
export function useRuntimeRecovery(context: RuntimeSessionContext): RuntimeRecovery {
  const recovery = useMemo(() => new SubscriptionRecovery({
    taskId: context.taskId,
    traceId: context.traceId,
    attach: () => context.attachTransportRef.current,
    backend: () => context.backendRef.current,
    setIssue: context.setIssue,
  }), [context]);

  useEffect(() => {
    // React StrictMode 会在已挂载实例上重放 effect；每次 setup 都恢复存活标记。
    recovery.setActive(true);
    return () => recovery.setActive(false);
  }, [recovery]);

  return useMemo(() => ({
    reconnect: recovery.reconnect,
    markConnected: recovery.markConnected,
    resync: recovery.resync,
    restored: recovery.restored,
    suspend: recovery.resetBudget,
  }), [recovery]);
}

import { useCallback, useState } from "react";

import type { TransportState } from "@/lib/assistant/contract";
import type { RuntimeSessionContext } from "@/components/assistant/runtime/runtime-types";
import { isActivityActive } from "@/lib/assistant/transport-state-operations";

export type RuntimeCancellation = {
  /** 仅前端：停止信号已发出，当前活动仍在收敛。 */
  cancelling: boolean;
  onRequested: () => void;
  onResult: (accepted: boolean) => void;
  /** 一轮结束（活动进入终态）：停止已收敛。 */
  onSettled: () => void;
  onStateCommitted: (state: TransportState) => void;
};

/**
 * 停止的 Task 级标记。
 *
 * 取消端点只确认信号已投递，不代表 Run 已停；标记保持到长订阅带来活动终态（`settled`），或
 * 提交时发现 Task 已没有活动（信号到达时活动恰已结束）。不比对 runId：同一 Task 同时至多一个
 * 活动，停止针对的就是它。
 */
export function useRuntimeCancellation(context: RuntimeSessionContext): RuntimeCancellation {
  const [cancelling, setCancelling] = useState(false);

  const clear = useCallback(() => {
    context.cancelRequestedRef.current = false;
    setCancelling(false);
  }, [context.cancelRequestedRef]);

  const onRequested = useCallback(() => {
    context.cancelRequestedRef.current = true;
    setCancelling(true);
  }, [context.cancelRequestedRef]);

  const onResult = useCallback((accepted: boolean) => {
    if (!accepted || !isActivityActive(context.latestStateRef.current)) {
      clear();
      return;
    }
    context.setIssue(null);
  }, [clear, context]);

  const onStateCommitted = useCallback((state: TransportState) => {
    if (context.cancelRequestedRef.current && !isActivityActive(state)) clear();
  }, [clear, context.cancelRequestedRef]);

  return { cancelling, onRequested, onResult, onSettled: clear, onStateCommitted };
}

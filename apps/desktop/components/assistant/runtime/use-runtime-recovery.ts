import { useEffect, useMemo } from "react";

import {
  SubscriptionRecovery,
  type SubscriptionRecoveryDeps,
} from "@/components/assistant/runtime/subscription-recovery";

/**
 * 交给调用方的恢复操作。`setActive` 不在其中：存活标记跟随组件生命周期，只由
 * {@link useSubscriptionRecovery} 管理，调用方不应自行开关。
 */
export type RuntimeRecovery = Pick<
  SubscriptionRecovery,
  "reconnect" | "markConnected" | "resync" | "restored" | "resetBudget"
>;

/**
 * 把 {@link SubscriptionRecovery} 绑定到组件生命周期；主对话与工作台只读子 Task 共用。
 *
 * `deps` 由调用方 memo：它变化（换 Task、换会话上下文）时重建实例，旧实例随 effect 清理停用。
 */
export function useSubscriptionRecovery(deps: SubscriptionRecoveryDeps): RuntimeRecovery {
  const recovery = useMemo(() => new SubscriptionRecovery(deps), [deps]);
  useEffect(() => {
    // React StrictMode 会在已挂载实例上重放 effect；每次 setup 都恢复存活标记。
    recovery.setActive(true);
    return () => recovery.setActive(false);
  }, [recovery]);
  return recovery;
}

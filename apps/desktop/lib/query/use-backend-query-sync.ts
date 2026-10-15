import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useSyncExternalStore } from "react";

import { classifyBackendTransition } from "@/lib/backend/backend-transition";
import { queryKeys } from "@/lib/query/query-keys";
import { getBackendRuntimeSnapshot, subscribeBackendRuntime } from "@/src/runtime-config";

/**
 * 后端恢复或换实例后让全部页面级查询重新拉取——查询缓存与后端实例的唯一同步点。
 *
 * 新实例可能换了端口、重放了迁移，旧缓存不再可信；与 Assistant 会话的
 * useBackendTransitions 使用同一个边沿判定。后端不可用期间不做处理：查询失败
 * 会按 retry 策略收敛为错误态，恢复后由这里统一刷新。在应用根部调用一次。
 */
export function useBackendQuerySync(): void {
  const queryClient = useQueryClient();
  const backend = useSyncExternalStore(subscribeBackendRuntime, getBackendRuntimeSnapshot, getBackendRuntimeSnapshot);
  const previousRef = useRef(backend);

  useEffect(() => {
    const previous = previousRef.current;
    previousRef.current = backend;
    if (classifyBackendTransition(previous, backend)?.kind !== "restored") return;
    void queryClient.invalidateQueries({ queryKey: queryKeys.all });
  }, [backend, queryClient]);
}

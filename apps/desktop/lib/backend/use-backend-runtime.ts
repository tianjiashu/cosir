import { useSyncExternalStore } from "react";

import { getBackendRuntimeSnapshot, subscribeBackendRuntime, type BackendRuntimeSnapshot } from "@/lib/backend/runtime-config";

/** 订阅当前后端实例（地址与 generation）；地址或 generation 变化时重渲染。 */
export function useBackendRuntime(): BackendRuntimeSnapshot {
  return useSyncExternalStore(subscribeBackendRuntime, getBackendRuntimeSnapshot, getBackendRuntimeSnapshot);
}

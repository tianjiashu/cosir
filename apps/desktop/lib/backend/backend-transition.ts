import type { BackendRuntimeSnapshot } from "@/src/runtime-config";

/**
 * 后端实例变化的边沿判定，供 Assistant 会话（useBackendTransitions）与页面查询缓存
 * （useBackendQuerySync）共用，保证两处对「不可用 / 恢复」的理解一致。
 */
export type BackendTransition =
  | { kind: "unavailable" }
  | { kind: "restored"; previousGeneration: number; generation: number; wasUnavailable: boolean };

/**
 * 判定两次后端快照之间的边沿。
 *
 * 新 generation 只会随可用的配置一起发布，因此「换代」总是伴随 available；
 * 不可用期间的快照抖动不产生事件，恢复时只触发一次 restored。
 */
export function classifyBackendTransition(
  previous: BackendRuntimeSnapshot,
  next: BackendRuntimeSnapshot,
): BackendTransition | null {
  if (previous.available && !next.available) return { kind: "unavailable" };
  const wasUnavailable = !previous.available && next.available;
  const generationChanged = next.available && previous.generation !== next.generation;
  if (!wasUnavailable && !generationChanged) return null;
  return { kind: "restored", previousGeneration: previous.generation, generation: next.generation, wasUnavailable };
}

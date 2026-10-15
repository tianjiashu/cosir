import { useEffect, useRef } from "react";

import { frontendLog } from "@/lib/logging/frontend-log";
import type { BackendRuntimeSnapshot } from "@/src/runtime-config";

export type BackendTransitionHandlers = {
  /** 后端变为不可用：停止一切依赖当前实例的请求与计时器，但保留会话与界面。 */
  onUnavailable: () => void;
  /**
   * 后端重新可用或换了新实例：此前的进程内订阅已失效，后端已把遗留 active Run
   * 收敛为 cancelled，必须以 canonical snapshot 原地重新对齐。
   */
  onRestored: (change: Extract<BackendTransition, { kind: "restored" }>) => void;
};

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

/**
 * 会话内检测后端实例变化的唯一位置。
 *
 * 此前 recovery、取消确认、业务续跑各自用「上一次的值」比较 generation / available，
 * 谁先执行、谁取消谁全靠 hook 声明顺序。这里只检测一次边沿，再由会话按显式顺序
 * 调用各用例的处理函数。handlers 通过 ref 读取最新值，不因引用变化重复触发。
 */
export function useBackendTransitions(
  backend: BackendRuntimeSnapshot,
  handlers: BackendTransitionHandlers,
  log: { taskId: number; traceId: string },
): void {
  const previousRef = useRef(backend);
  const handlersRef = useRef(handlers);
  handlersRef.current = handlers;

  useEffect(() => {
    const previous = previousRef.current;
    previousRef.current = backend;
    const transition = classifyBackendTransition(previous, backend);
    if (!transition) return;
    if (transition.kind === "unavailable") {
      void frontendLog("INFO", "assistant_backend_unavailable", "Assistant 会话检测到本机后端不可用", {
        traceId: log.traceId,
        data: { taskId: log.taskId, generation: previous.generation },
      });
      handlersRef.current.onUnavailable();
      return;
    }
    void frontendLog("INFO", "assistant_backend_restored", "Assistant 会话检测到本机后端恢复或更换实例", {
      traceId: log.traceId,
      data: {
        taskId: log.taskId,
        previousGeneration: transition.previousGeneration,
        generation: transition.generation,
        wasUnavailable: transition.wasUnavailable,
        backendBaseUrl: backend.backendBaseUrl,
      },
    });
    handlersRef.current.onRestored(transition);
  }, [backend, log.taskId, log.traceId]);
}

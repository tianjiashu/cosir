import { useCallback, useEffect, useRef } from "react";

import { HttpError } from "@/lib/http/errors";
import { resumeTask } from "@/lib/assistant/task-commands";
import type { RuntimeSessionContext } from "@/components/assistant/runtime/runtime-types";
import { UserFacingError } from "@/lib/errors/user-facing-error";
import { userErrorMessage } from "@/lib/errors/user-error-message";

export type BusinessResumeController = {
  resumeBusinessRun: () => Promise<void>;
};

/**
 * 显式业务恢复（`POST /tasks/{id}/resume`），与 transport attach 语义分开。
 *
 * 命令只投递：202 后结果经 Task 长订阅到达（新 Run 进入活动态，或 actor 拒绝时顶层 `error`），
 * 这里不读响应内容、不按 runId 对账，也不管订阅：长订阅断开后的重连只由订阅恢复逻辑负责。会话内同时
 * 只允许一个恢复请求在途。
 */
export function useBusinessResume(context: RuntimeSessionContext): BusinessResumeController {
  const inFlightRef = useRef<Promise<void> | null>(null);
  const disposedRef = useRef(false);

  useEffect(() => {
    disposedRef.current = false;
    return () => { disposedRef.current = true; };
  }, []);

  const resumeBusinessRun = useCallback(() => {
    if (inFlightRef.current) return inFlightRef.current;
    if (disposedRef.current) return Promise.resolve();
    if (!context.backendRef.current.available) {
      return Promise.reject(new UserFacingError("本机后端当前不可用，无法继续运行"));
    }
    const request = (async () => {
      try {
        await resumeTask(context.taskId, { traceId: context.traceId });
      } catch (error) {
        if (error instanceof HttpError) {
          throw new UserFacingError(userErrorMessage(error, `继续运行失败（HTTP ${error.status}）`));
        }
        throw error;
      }
    })();
    const tracked = request.finally(() => {
      if (inFlightRef.current === tracked) inFlightRef.current = null;
    });
    inFlightRef.current = tracked;
    return tracked;
  }, [context]);

  return { resumeBusinessRun };
}

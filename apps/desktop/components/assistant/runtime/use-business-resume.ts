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
 * 后端处理完才返回：不可恢复时 409 `RUN_NOT_RESUMABLE` 经 HttpError 抛给调用方提示；成功后续跑
 * 输出经 Task 长订阅到达。这里不读响应内容、不按 runId 对账，也不管订阅：长订阅断开后的重连只由
 * 订阅恢复逻辑负责。会话内同时只允许一个恢复请求在途。
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

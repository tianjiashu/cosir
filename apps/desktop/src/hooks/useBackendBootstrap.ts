/**
 * 本地后端自启动 Hook。
 *
 * 负责在桌面应用根组件挂载后确保本地 Python 后端启动，
 * 不向 UI 暴露额外状态。
 *
 * @module hooks/useBackendBootstrap
 */

import { useEffect } from "react";
import { useBackend } from "./useBackend";
import { logError } from "../lib/logger";

/**
 * 在根组件挂载后尝试确保本地后端运行。
 *
 * @returns 无。
 */
export function useBackendBootstrap(): void {
  const { ensureRunning } = useBackend();

  useEffect(() => {
    void ensureRunning().catch((error) => {
      logError("桌面应用启动时拉起本地后端失败", error, {
        module: "useBackendBootstrap",
      });
    });
  }, [ensureRunning]);
}

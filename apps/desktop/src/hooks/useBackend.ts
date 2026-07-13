/**
 * 后端进程状态 Hook（第一版占位）。
 *
 * 第一版 Python 后端需手动启动，此 Hook 预留接口，
 * 后续对接 Tauri sidecar / commands.rs 的进程管理命令。
 *
 * @module hooks/useBackend
 */

import { useState, useCallback } from "react";
import { logInfo } from "../lib/logger";

/** 后端进程状态枚举。 */
export type BackendStatus = "unknown" | "running" | "stopped" | "error";

/**
 * 后端进程状态 Hook 返回值。
 */
interface UseBackendReturn {
  /** 当前后端进程状态。 */
  status: BackendStatus;
  /** 探活后端（预留）。 */
  checkHealth: () => Promise<void>;
}

/**
 * 后端进程状态 Hook。
 *
 * 第一版返回 "unknown" 状态，不执行实际探活。
 * 后续接入 Tauri IPC 命令实现真实的启动/停止/探活。
 *
 * @returns 后端状态和操作方法。
 */
export function useBackend(): UseBackendReturn {
  const [status, setStatus] = useState<BackendStatus>("unknown");

  /**
   * 探活后端进程（占位实现）。
   *
   * TODO: 对接 commands.rs::check_backend_health
   * TODO: 经 Tauri invoke 调用 Rust 侧探活逻辑
   */
  const checkHealth = useCallback(async (): Promise<void> => {
    // 第一版占位：手动确认后端已启动
    logInfo("checkHealth: 第一版为占位实现", { module: "useBackend" });
    setStatus("unknown");
  }, []);

  return { status, checkHealth };
}

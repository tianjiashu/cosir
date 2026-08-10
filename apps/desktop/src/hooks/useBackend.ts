/**
 * 本地后端托管 Hook。
 *
 * 负责编排桌面端对 Tauri 后端托管命令的调用，
 * 并把结构化状态回写到 Zustand Store。
 *
 * @module hooks/useBackend
 */

import { useCallback } from "react";
import type { BackendLogsTailResponse, BackendStatusResponse } from "@shared/backend";
import {
  getBackendStatus,
  restartBackend,
  startBackend,
  stopBackend,
  tailBackendLogs,
} from "../services/backend";
import { logError, logInfo } from "../lib/logger";
import { useBackendStore } from "../stores/backendStore";

/** 本地后端托管 Hook 返回值。 */
interface UseBackendReturn {
  /** 当前后端生命周期状态。 */
  status: ReturnType<typeof useBackendStore.getState>["status"];
  /** 最近一次完整状态快照。 */
  snapshot: BackendStatusResponse | null;
  /** 当前是否有命令执行中。 */
  isBusy: boolean;
  /** 最近一次 IPC transport 错误。 */
  transportError: string | null;
  /** 刷新当前后端状态。 */
  refreshStatus: () => Promise<BackendStatusResponse>;
  /** 确保本地后端处于运行状态。 */
  ensureRunning: () => Promise<BackendStatusResponse>;
  /** 主动启动本地后端。 */
  start: () => Promise<BackendStatusResponse>;
  /** 主动停止本地后端。 */
  stop: () => Promise<BackendStatusResponse>;
  /** 主动重启本地后端。 */
  restart: () => Promise<BackendStatusResponse>;
  /** 读取本地后端日志尾部。 */
  tailLogs: (maxLines?: number) => Promise<BackendLogsTailResponse>;
}

/**
 * 本地后端托管 Hook。
 *
 * @returns 当前后端状态和操作方法。
 */
export function useBackend(): UseBackendReturn {
  const status = useBackendStore((state) => state.status);
  const snapshot = useBackendStore((state) => state.snapshot);
  const isBusy = useBackendStore((state) => state.isBusy);
  const transportError = useBackendStore((state) => state.transportError);
  const applySnapshot = useBackendStore((state) => state.applySnapshot);
  const setBusy = useBackendStore((state) => state.setBusy);
  const setStatus = useBackendStore((state) => state.setStatus);
  const setTransportError = useBackendStore((state) => state.setTransportError);

  /**
   * 执行一次标准化命令调用并同步 Store。
   *
   * @param nextStatus - 命令开始前预设的过渡状态。
   * @param commandName - 仅用于日志的命令名。
   * @param action - 实际命令调用。
   * @returns 命令返回的最新快照。
   * @throws 命令调用失败时继续向上抛出。
   */
  const runCommand = useCallback(
    async (
      nextStatus: BackendStatusResponse["status"],
      commandName: string,
      action: () => Promise<BackendStatusResponse>,
    ): Promise<BackendStatusResponse> => {
      setBusy(true);
      setTransportError(null);
      setStatus(nextStatus);

      try {
        const result = await action();
        applySnapshot(result);
        logInfo(`本地后端命令执行完成: ${commandName}`, {
          module: "useBackend",
          commandName,
          status: result.status,
          managed: result.managed,
          pid: result.pid,
        });
        return result;
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setTransportError(message);
        setStatus("failed");
        logError(`本地后端命令执行失败: ${commandName}`, error, {
          module: "useBackend",
          commandName,
        });
        throw error;
      } finally {
        setBusy(false);
      }
    },
    [applySnapshot, setBusy, setStatus, setTransportError],
  );

  /**
   * 刷新当前后端状态。
   *
   * 状态查询不是生命周期命令，不预设过渡状态（保持当前 status 原样写入，避免一次纯查询
   * 让 TopBar 闪烁「后端启动中」）；当前状态经 getState() 即时读取而非闭包捕获，
   * 使本回调引用稳定——否则 status 每次变化都会重建 refreshStatus → ensureRunning →
   * 级联重触发 useBackendBootstrap 的挂载 effect，导致重复 backend_start。
   *
   * @returns 最新状态快照。
   */
  const refreshStatus = useCallback(async (): Promise<BackendStatusResponse> => {
    return runCommand(useBackendStore.getState().status, "backend_status", getBackendStatus);
  }, [runCommand]);

  /**
   * 主动启动本地后端。
   *
   * @returns 启动后的状态快照。
   */
  const start = useCallback(async (): Promise<BackendStatusResponse> => {
    return runCommand("starting", "backend_start", startBackend);
  }, [runCommand]);

  /**
   * 主动停止本地后端。
   *
   * @returns 停止后的状态快照。
   */
  const stop = useCallback(async (): Promise<BackendStatusResponse> => {
    return runCommand("stopping", "backend_stop", stopBackend);
  }, [runCommand]);

  /**
   * 主动重启本地后端。
   *
   * @returns 重启后的状态快照。
   */
  const restart = useCallback(async (): Promise<BackendStatusResponse> => {
    return runCommand("restarting", "backend_restart", restartBackend);
  }, [runCommand]);

  /**
   * 确保后端处于运行状态。
   *
   * @returns 运行中或启动后的状态快照。
   */
  const ensureRunning = useCallback(async (): Promise<BackendStatusResponse> => {
    const current = await refreshStatus();
    if (current.status === "running") {
      return current;
    }
    return start();
  }, [refreshStatus, start]);

  /**
   * 读取本地后端日志尾部。
   *
   * @param maxLines - 每个日志文件最多返回的尾部行数。
   * @returns 日志尾部片段。
   */
  const tailLogs = useCallback(async (maxLines = 80): Promise<BackendLogsTailResponse> => {
    try {
      return await tailBackendLogs(maxLines);
    } catch (error) {
      logError("读取本地后端日志尾部失败", error, {
        module: "useBackend",
        maxLines,
      });
      throw error;
    }
  }, []);

  return {
    status,
    snapshot,
    isBusy,
    transportError,
    refreshStatus,
    ensureRunning,
    start,
    stop,
    restart,
    tailLogs,
  };
}

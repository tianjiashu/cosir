/**
 * Tauri 后端托管 IPC 服务。
 *
 * 统一封装桌面端对 Rust 本地后端托管命令的调用，
 * 组件和 Hook 不直接触碰 `invoke()`。
 *
 * @module services/backend
 */

import type { BackendLogsTailResponse, BackendStatusResponse } from "@shared/backend";
import { ServiceError } from "./types";
import { logError } from "../lib/logger";

/**
 * 调用 Tauri IPC 命令并统一映射错误。
 *
 * @param command - Rust 侧注册的命令名。
 * @param args - 可选命令参数。
 * @returns 命令的结构化返回值。
 * @throws {ServiceError} 当 IPC 调用失败时抛出。
 */
async function invokeCommand<T>(command: string, args?: Record<string, unknown>): Promise<T> {
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    return await invoke<T>(command, args);
  } catch (error) {
    logError(`Tauri IPC 调用失败: ${command}`, error, {
      module: "backendService",
      command,
    });
    throw new ServiceError(`Tauri IPC 调用失败: ${command}`, {
      cause: error,
    });
  }
}

/**
 * 启动桌面端托管的本地 Python 后端。
 *
 * @returns 启动后的结构化后端状态。
 * @throws {ServiceError} 当 IPC 通道调用失败时抛出。
 */
export async function startBackend(): Promise<BackendStatusResponse> {
  return invokeCommand<BackendStatusResponse>("backend_start");
}

/**
 * 停止桌面端托管的本地 Python 后端。
 *
 * @returns 停止后的结构化后端状态。
 * @throws {ServiceError} 当 IPC 通道调用失败时抛出。
 */
export async function stopBackend(): Promise<BackendStatusResponse> {
  return invokeCommand<BackendStatusResponse>("backend_stop");
}

/**
 * 重启桌面端托管的本地 Python 后端。
 *
 * @returns 重启后的结构化后端状态。
 * @throws {ServiceError} 当 IPC 通道调用失败时抛出。
 */
export async function restartBackend(): Promise<BackendStatusResponse> {
  return invokeCommand<BackendStatusResponse>("backend_restart");
}

/**
 * 查询桌面端托管的本地 Python 后端状态。
 *
 * @returns 当前结构化后端状态。
 * @throws {ServiceError} 当 IPC 通道调用失败时抛出。
 */
export async function getBackendStatus(): Promise<BackendStatusResponse> {
  return invokeCommand<BackendStatusResponse>("backend_status");
}

/**
 * 读取本地后端相关日志尾部内容。
 *
 * @param maxLines - 每个日志文件最多返回的行数。
 * @returns 多个日志文件的尾部内容。
 * @throws {ServiceError} 当 IPC 通道调用失败时抛出。
 */
export async function tailBackendLogs(maxLines = 80): Promise<BackendLogsTailResponse> {
  return invokeCommand<BackendLogsTailResponse>("backend_logs_tail", { maxLines });
}

import path from "node:path";
import { BrowserWindow, dialog, ipcMain, nativeTheme, type IpcMainInvokeEvent } from "electron";

import type { BackendSupervisor } from "./backend/supervisor";
import { appendJsonLine } from "./desktop-log";
import { appLogDir } from "./paths";
import {
  FRONTEND_LOG_LEVELS,
  HOST_CHANNEL,
  THEME_SOURCES,
  type FrontendLogEntry,
  type HostReply,
  type OpenDirectoryOptions,
} from "../shared/host-contract";

/**
 * 注册宿主控制面的 IPC 处理器。
 *
 * 这里是渲染进程到宿主的信任边界：每个通道的入参只在这一层校验，main 的其他模块直接信任本层传入的值。
 * 处理器的失败（含入参不合法）统一转成 `HostReply`，消息原样回给渲染进程，由 preload 还原为带中文消息的 `Error`。
 */
export function registerHostIpc(supervisor: BackendSupervisor): void {
  handle(HOST_CHANNEL.backendSnapshot, () => supervisor.snapshot());
  handle(HOST_CHANNEL.backendRestart, () => supervisor.restart());
  handle(HOST_CHANNEL.logWrite, (_event, entry) => {
    appendJsonLine(path.join(appLogDir(), "frontend.log"), parseFrontendLogEntry(entry));
  });
  handle(HOST_CHANNEL.dialogOpenDirectory, async (event, options) => {
    const { title, defaultPath } = parseOpenDirectoryOptions(options);
    const owner = BrowserWindow.fromWebContents(event.sender);
    const dialogOptions = { title, defaultPath, properties: ["openDirectory" as const, "createDirectory" as const] };
    const result = owner ? await dialog.showOpenDialog(owner, dialogOptions) : await dialog.showOpenDialog(dialogOptions);
    return result.canceled ? null : result.filePaths[0] ?? null;
  });
  handle(HOST_CHANNEL.windowThemeSource, (_event, source) => {
    if (!isOneOf(THEME_SOURCES, source)) throw new Error("外观偏好不合法");
    nativeTheme.themeSource = source;
  });
}

function handle<T>(channel: string, handler: (event: IpcMainInvokeEvent, ...args: unknown[]) => T | Promise<T>): void {
  ipcMain.handle(channel, async (event, ...args): Promise<HostReply<T>> => {
    try {
      return { ok: true, value: await handler(event, ...args) };
    } catch (error) {
      return { ok: false, message: error instanceof Error && error.message ? error.message : "宿主操作失败" };
    }
  });
}

function isOneOf<T extends string>(values: readonly T[], value: unknown): value is T {
  return (values as readonly unknown[]).includes(value);
}

const SNAKE_CASE = /^[a-z][a-z0-9_]*$/;
const TRACE_ID = /^[0-9a-f]{32}$/;

function isSnakeCase(value: unknown): value is string {
  return typeof value === "string" && SNAKE_CASE.test(value) && !value.endsWith("_");
}

function isLogName(value: unknown): value is string {
  return typeof value === "string" && value.split(".").every(isSnakeCase);
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** 只放行统一日志契约里的字段，多余字段视为契约违背，防止渲染进程借日志写入任意内容结构。 */
function parseFrontendLogEntry(value: unknown): FrontendLogEntry {
  if (!isPlainObject(value)) throw new Error("日志必须是对象");
  const allowed = ["ts", "level", "logger", "trace_id", "caller", "event", "msg", "data", "error", "truncated"];
  if (Object.keys(value).some((key) => !allowed.includes(key))) throw new Error("日志包含契约外字段");
  const { ts, level, logger, trace_id, caller, event, msg, data, error, truncated } = value;
  if (!isOneOf(FRONTEND_LOG_LEVELS, level)) throw new Error("日志 level 不符合统一契约");
  if (typeof trace_id !== "string" || !TRACE_ID.test(trace_id)) throw new Error("日志 trace_id 不符合统一契约");
  if (
    typeof ts !== "string" || Number.isNaN(Date.parse(ts))
    || !isSnakeCase(event) || !isLogName(logger) || !isLogName(caller)
    || typeof msg !== "string" || !msg
    || !isPlainObject(data)
    || !(error === null || isPlainObject(error))
    || typeof truncated !== "boolean"
  ) {
    throw new Error("日志字段不符合统一契约");
  }
  return value as FrontendLogEntry;
}

function parseOpenDirectoryOptions(value: unknown): OpenDirectoryOptions {
  if (!isPlainObject(value) || typeof value.title !== "string") throw new Error("目录选择参数不合法");
  if (value.defaultPath !== undefined && typeof value.defaultPath !== "string") throw new Error("目录选择参数不合法");
  return { title: value.title, defaultPath: value.defaultPath };
}

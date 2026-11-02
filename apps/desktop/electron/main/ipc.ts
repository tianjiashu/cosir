import path from "node:path";
import { BrowserWindow, dialog, ipcMain, nativeTheme, type IpcMainInvokeEvent } from "electron";

import type { BackendSupervisor } from "./backend/supervisor";
import type { BrowserHost } from "./browser/browser-host";
import { httpUrl } from "./browser/params";
import { BROWSER_TAB_ID } from "../shared/host-contract";
import { appendJsonLine } from "./desktop-log";
import { appLogDir } from "./paths";
import {
  BROWSER_NAVIGATION_ACTIONS,
  FRONTEND_LOG_LEVELS,
  HOST_CHANNEL,
  THEME_SOURCES,
  type BrowserDialogAnswer,
  type BrowserNavigation,
  type BrowserPlacement,
  type BrowserTabContext,
  type BrowserTabLimits,
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
export function registerHostIpc(supervisor: BackendSupervisor, browser: BrowserHost): void {
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
  handle(HOST_CHANNEL.browserState, () => browser.state());
  handle(HOST_CHANNEL.browserOpen, (_event, taskId, context, url) => browser.userOpen(
    parseTaskId(taskId),
    parseTabContext(context),
    url === undefined ? undefined : parseNavigationUrl(url),
  ));
  handle(HOST_CHANNEL.browserPlace, (_event, tabId, placement) => {
    browser.place(parseTabId(tabId), placement === null ? null : parsePlacement(placement));
  });
  handle(HOST_CHANNEL.browserFreeze, (_event, tabId) => browser.freeze(parseTabId(tabId)));
  handle(HOST_CHANNEL.browserNavigate, (_event, tabId, navigation) => browser.userNavigate(parseTabId(tabId), parseNavigation(navigation)));
  handle(HOST_CHANNEL.browserClose, (_event, tabId) => browser.userClose(parseTabId(tabId)));
  handle(HOST_CHANNEL.browserAnswerDialog, (_event, tabId, dialogId, answer) => browser.userAnswerDialog(
    parseTabId(tabId),
    parseDialogId(dialogId),
    parseDialogAnswer(answer),
  ));
  handle(HOST_CHANNEL.browserRevealDownload, (_event, downloadId) => browser.revealDownload(parseDownloadId(downloadId)));
}

/** 地址栏输入：没写协议的按 https 补全；协议只允许 http(s)（与 Agent 走同一条校验）。 */
function parseNavigationUrl(value: unknown): string {
  if (typeof value !== "string" || !value.trim()) throw new Error("网址不能为空");
  if (value.length > 4096) throw new Error("网址太长");
  const text = value.trim();
  const withScheme = /^[a-z][a-z0-9+.-]*:/i.test(text) ? text : `https://${text}`;
  try {
    return httpUrl(withScheme);
  } catch {
    throw new Error("只能打开 http 或 https 网址");
  }
}

function parseTabId(value: unknown): string {
  if (typeof value !== "string" || !BROWSER_TAB_ID.test(value)) throw new Error("标签页标识不合法");
  return value;
}

function parseTaskId(value: unknown): number {
  if (!Number.isSafeInteger(value) || (value as number) <= 0) throw new Error("任务标识不合法");
  return value as number;
}

/** 下载目录来自后端（`GET /tasks/{id}/browser-context`），渲染进程只转交；这里只确认是绝对路径。 */
function parseTabContext(value: unknown): BrowserTabContext {
  if (!isPlainObject(value) || typeof value.downloadDir !== "string" || !path.isAbsolute(value.downloadDir)) {
    throw new Error("标签上下文不合法");
  }
  return { limits: parseLimits(value.limits), downloadDir: value.downloadDir };
}

function parseDialogAnswer(value: unknown): BrowserDialogAnswer {
  if (!isPlainObject(value) || typeof value.accept !== "boolean") throw new Error("弹窗答复不合法");
  if (value.text === undefined) return { accept: value.accept };
  if (typeof value.text !== "string" || value.text.length > 10_000) throw new Error("弹窗答复不合法");
  return { accept: value.accept, text: value.text };
}

function parseDialogId(value: unknown): number {
  if (!Number.isSafeInteger(value) || (value as number) <= 0) throw new Error("弹窗标识不合法");
  return value as number;
}

/** 只确认形状；认不认识这个下载由宿主的下载登记判断（`BrowserDownloads.pathOf`）。 */
function parseDownloadId(value: unknown): string {
  if (typeof value !== "string" || !value || value.length > 64) throw new Error("下载标识不合法");
  return value;
}

function parseLimits(value: unknown): BrowserTabLimits {
  if (!isPlainObject(value)) throw new Error("标签上限不合法");
  const { perTask, total } = value;
  if (!Number.isSafeInteger(perTask) || !Number.isSafeInteger(total) || (perTask as number) <= 0 || (total as number) <= 0) {
    throw new Error("标签上限不合法");
  }
  return { perTask: perTask as number, total: total as number };
}

function parsePlacement(value: unknown): BrowserPlacement {
  if (!isPlainObject(value)) throw new Error("显示区域不合法");
  const { x, y, width, height } = value;
  const numbers = [x, y, width, height];
  if (!numbers.every((item) => typeof item === "number" && Number.isFinite(item)) || (width as number) < 0 || (height as number) < 0) {
    throw new Error("显示区域不合法");
  }
  return { x: x as number, y: y as number, width: width as number, height: height as number };
}

/** 地址栏的导航：打开地址，或后退 / 前进 / 刷新。 */
function parseNavigation(value: unknown): BrowserNavigation {
  if (!isPlainObject(value)) throw new Error("导航参数不合法");
  if (typeof value.url === "string") return { url: parseNavigationUrl(value.url) };
  if (!isOneOf(BROWSER_NAVIGATION_ACTIONS, value.action)) throw new Error("导航参数不合法");
  return { action: value.action };
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

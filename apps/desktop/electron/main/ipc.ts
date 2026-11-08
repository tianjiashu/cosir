import path from "node:path";
import { BrowserWindow, dialog, ipcMain, nativeTheme, type IpcMainInvokeEvent } from "electron";

import type { BackendSupervisor } from "./backend/supervisor";
import type { BrowserHost } from "./browser/browser-host";
import { httpUrl, tabContextOf } from "./browser/params";
import { addressToUrl } from "./browser/web-url";
import { BROWSER_USER_WORDING, type BrowserErrorCode } from "./browser/errors";
import { appendJsonLine, logDesktopEvent } from "./desktop-log";
import { HostMethodError } from "./host-link/method-registry";
import { HostUserError } from "./host-user-error";
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
  type FrontendLogEntry,
  type HostReply,
  type OpenDirectoryOptions,
} from "../shared/host-contract";

/**
 * 注册宿主控制面的 IPC 处理器。
 *
 * 这里是渲染进程到宿主的信任边界：每个通道的入参只在这一层校验，main 的其他模块直接信任本层传入的值。
 *
 * 失败也只在这一层定语义（`handle`）：回给渲染进程的消息一律是给用户看的中文——有意给用户的失败（`HostUserError`，
 * 含入参不合法）原样，浏览器错误码按 `BROWSER_USER_WORDING` 换说法，其余异常回「宿主操作失败」；每次失败都在这里
 * 落一条日志（意外异常为 ERROR）。渲染侧据此把宿主的失败当作用户可读错误（`lib/desktop-host`），调用点不必各自记日志。
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
    if (!isOneOf(THEME_SOURCES, source)) throw new HostUserError("外观偏好不合法");
    nativeTheme.themeSource = source;
  });
  handle(HOST_CHANNEL.browserState, () => browser.state());
  handle(HOST_CHANNEL.browserOpen, (_event, taskId, context) => browser.userOpen(parseTaskId(taskId), parseTabContext(context)));
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

/** 地址栏输入：没写协议的补全（`addressToUrl`）；协议只允许 http(s)（与 Agent 走同一条校验）。 */
function parseNavigationUrl(value: unknown): string {
  if (typeof value !== "string" || !value.trim()) throw new HostUserError("网址不能为空");
  if (value.length > 4096) throw new HostUserError("网址太长");
  try {
    return httpUrl(addressToUrl(value.trim()));
  } catch {
    throw new HostUserError("只能打开 http 或 https 网址");
  }
}

/** 标签、下载、弹窗的 id 只用来查宿主的登记，认不认识由宿主判断（查不到就是不在）；这里只确认类型。 */
function parseTabId(value: unknown): string {
  if (typeof value !== "string") throw new HostUserError("标签页标识不合法");
  return value;
}

function parseTaskId(value: unknown): number {
  if (!Number.isSafeInteger(value) || (value as number) <= 0) throw new HostUserError("任务标识不合法");
  return value as number;
}

/** 标签上下文来自后端（`GET /tasks/{id}/browser-context`），渲染进程只转交；规则与工具请求同一份（`tabContextOf`）。 */
function parseTabContext(value: unknown): BrowserTabContext {
  const limits = isPlainObject(value) && isPlainObject(value.limits) ? value.limits : {};
  const context = isPlainObject(value) ? tabContextOf(limits.perTask, limits.total, value.downloadDir) : null;
  if (!context) throw new HostUserError("标签上下文不合法");
  return context;
}

function parseDialogAnswer(value: unknown): BrowserDialogAnswer {
  if (!isPlainObject(value) || typeof value.accept !== "boolean") throw new HostUserError("弹窗答复不合法");
  if (value.text === undefined) return { accept: value.accept };
  if (typeof value.text !== "string" || value.text.length > 10_000) throw new HostUserError("弹窗答复不合法");
  return { accept: value.accept, text: value.text };
}

function parseDialogId(value: unknown): number {
  if (typeof value !== "number") throw new HostUserError("弹窗标识不合法");
  return value;
}

function parseDownloadId(value: unknown): string {
  if (typeof value !== "string") throw new HostUserError("下载标识不合法");
  return value;
}

function parsePlacement(value: unknown): BrowserPlacement {
  if (!isPlainObject(value)) throw new HostUserError("显示区域不合法");
  const { x, y, width, height } = value;
  const numbers = [x, y, width, height];
  if (!numbers.every((item) => typeof item === "number" && Number.isFinite(item)) || (width as number) < 0 || (height as number) < 0) {
    throw new HostUserError("显示区域不合法");
  }
  return { x: x as number, y: y as number, width: width as number, height: height as number };
}

/** 地址栏的导航：打开地址，或后退 / 前进 / 刷新。 */
function parseNavigation(value: unknown): BrowserNavigation {
  if (!isPlainObject(value)) throw new HostUserError("导航参数不合法");
  if (typeof value.url === "string") return { url: parseNavigationUrl(value.url) };
  if (!isOneOf(BROWSER_NAVIGATION_ACTIONS, value.action)) throw new HostUserError("导航参数不合法");
  return { action: value.action };
}

function handle<T>(channel: string, handler: (event: IpcMainInvokeEvent, ...args: unknown[]) => T | Promise<T>): void {
  ipcMain.handle(channel, async (event, ...args): Promise<HostReply<T>> => {
    try {
      return { ok: true, value: await handler(event, ...args) };
    } catch (error) {
      return { ok: false, message: failureMessage(channel, error) };
    }
  });
}

function failureMessage(channel: string, error: unknown): string {
  if (error instanceof HostUserError) {
    logDesktopEvent("INFO", "host_ipc", "host_ipc_rejected", { channel }, error);
    return error.message;
  }
  if (error instanceof HostMethodError) {
    logDesktopEvent("INFO", "host_ipc", "host_ipc_rejected", { channel, code: error.code }, error);
    return BROWSER_USER_WORDING[error.code as BrowserErrorCode] ?? FALLBACK_MESSAGE;
  }
  logDesktopEvent("ERROR", "host_ipc", "host_ipc_failed", { channel }, error);
  return FALLBACK_MESSAGE;
}

const FALLBACK_MESSAGE = "宿主操作失败";

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
  if (!isPlainObject(value)) throw new HostUserError("日志必须是对象");
  const allowed = ["ts", "level", "logger", "trace_id", "caller", "event", "msg", "data", "error", "truncated"];
  if (Object.keys(value).some((key) => !allowed.includes(key))) throw new HostUserError("日志包含契约外字段");
  const { ts, level, logger, trace_id, caller, event, msg, data, error, truncated } = value;
  if (!isOneOf(FRONTEND_LOG_LEVELS, level)) throw new HostUserError("日志 level 不符合统一契约");
  if (typeof trace_id !== "string" || !TRACE_ID.test(trace_id)) throw new HostUserError("日志 trace_id 不符合统一契约");
  if (
    typeof ts !== "string" || Number.isNaN(Date.parse(ts))
    || !isSnakeCase(event) || !isLogName(logger) || !isLogName(caller)
    || typeof msg !== "string" || !msg
    || !isPlainObject(data)
    || !(error === null || isPlainObject(error))
    || typeof truncated !== "boolean"
  ) {
    throw new HostUserError("日志字段不符合统一契约");
  }
  return value as FrontendLogEntry;
}

function parseOpenDirectoryOptions(value: unknown): OpenDirectoryOptions {
  if (!isPlainObject(value) || typeof value.title !== "string") throw new HostUserError("目录选择参数不合法");
  if (value.defaultPath !== undefined && typeof value.defaultPath !== "string") throw new HostUserError("目录选择参数不合法");
  return { title: value.title, defaultPath: value.defaultPath };
}

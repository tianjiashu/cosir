/**
 * 前端统一日志出口。
 *
 * 开发期（`tauri dev`）：输出到 DevTools console 便于调试，同时通过 Tauri invoke
 * 写入仓库根 `logs/desktop.log`（与后端日志目录约定一致），关闭窗口后仍可回看排查。
 * 纯浏览器 `vite dev`：无 Tauri 运行时，仅输出到 DevTools console，不落盘。
 * 生产期：通过 Tauri invoke 命令写入仓库根 `logs/desktop.log`。
 *
 * 所有前端代码禁止直接使用 `console.log/warn/error` 作为系统日志，
 * 必须经此统一出口，确保异常路径可排查。
 *
 * @module lib/logger
 */

import { selectClientLogContext } from "@/stores/clientTraceStore";

/** 日志级别枚举。 */
export enum LogLevel {
  DEBUG = "debug",
  INFO = "info",
  WARN = "warn",
  ERROR = "error",
}

/** 日志条目结构（传递给 Tauri 侧落盘）。 */
interface LogEntry {
  /** 日志级别。 */
  level: LogLevel;
  /** 日志消息。 */
  message: string;
  /** ISO-8601 时间戳。 */
  timestamp: string;
  /** 可选的附加上下文（如 task_id、模块名）。 */
  context?: Record<string, unknown>;
  /** 可选的错误堆栈（仅 error 级别）。 */
  stack?: string;
}

/**
 * 判断当前是否运行在 Tauri 环境中。
 *
 * 通过检测 `__TAURI_INTERNALS__` 全局变量判断，
 * 避免在浏览器/Node.js 测试环境中调用 Tauri API 报错。
 *
 * @returns 是否在 Tauri WebView 中运行。
 */
function isTauriEnv(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

/**
 * 通过 Tauri invoke 将日志写入本地文件系统。
 *
 * 调用 Rust 侧的 `log_write` 命令将日志追加到 `logs/desktop.log`。
 * 如果 Tauri 命令不可用或执行失败，静默回退到 console 输出并标记 error。
 *
 * @param entry - 待写入的日志条目。
 *
 * @sideeffect
 * - 经 Tauri IPC 调用 Rust 侧命令
 * - 写入 logs/desktop.log 文件
 */
async function writeToDisk(entry: LogEntry): Promise<void> {
  if (!isTauriEnv()) return;

  try {
    const { invoke } = await import("@tauri-apps/api/core");
    await invoke("log_write", { entry });
  } catch (err) {
    // 日志写入失败不能阻塞主流程，至少回退到 console
    console.error(`[logger] 落盘失败: ${err instanceof Error ? err.message : String(err)}`, entry);
  }
}

/** 需要脱敏的敏感字段名（键名匹配，大小写不敏感）。 */
const SENSITIVE_KEYS = [
  "apikey",
  "api_key",
  "password",
  "token",
  "secret",
  "authorization",
  "accesskey",
  "access_key",
  "privatekey",
  "private_key",
  "credential",
  "cookie",
];

/**
 * 对上下文对象做敏感字段脱敏，避免 secret 经日志泄漏到 console 或落盘文件。
 *
 * 返回脱敏后的新对象（不修改原对象）。匹配敏感键名的值会被替换为 "[REDACTED]"；
 * 嵌套对象会递归脱敏；数组元素（含数组中的对象）会逐元素递归脱敏且保留数组结构；
 * 非对象/数组值原样保留。键名匹配大小写不敏感。
 *
 * @param context - 原始上下文字典。
 * @returns 脱敏后的上下文副本；原值为空时直接返回。
 */
function redactContext(context?: Record<string, unknown>): Record<string, unknown> | undefined {
  const merged = mergeTraceContext(context);
  if (!merged) return merged;
  const normalized = normalizeContextKeys(merged);
  const result: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(normalized)) {
    result[key] = redactValue(key, value);
  }
  return result;
}

/**
 * 将当前客户端 trace 上下文合并进日志上下文。
 *
 * 调用方显式传入的字段优先，避免 logger 覆盖更精确的业务上下文。
 *
 * @param context - 调用方传入的日志上下文。
 * @returns 合并后的日志上下文；没有任何字段时返回 undefined。
 */
function mergeTraceContext(context?: Record<string, unknown>): Record<string, unknown> | undefined {
  const traceContext = selectClientLogContext();
  const merged = { ...traceContext, ...(context ?? {}) };
  return Object.keys(merged).length > 0 ? merged : undefined;
}

/**
 * 将日志上下文字段统一转换为 snake_case。
 *
 * 前端调用方可能传入 camelCase 字段；统一出口在落盘和 console 前转换，
 * 保持客户端诊断字段与后端 JSONL 查询契约一致。
 *
 * @param context - 原始上下文字典。
 * @returns 字段名已规范化的新对象。
 */
function normalizeContextKeys(context: Record<string, unknown>): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(context)) {
    result[toSnakeCase(key)] = normalizeContextValue(value);
  }
  return result;
}

/**
 * 递归规范化日志上下文值。
 *
 * @param value - 原始字段值。
 * @returns 对象字段名规范化后的值，标量值原样返回。
 */
function normalizeContextValue(value: unknown): unknown {
  if (value === null || typeof value !== "object") {
    return value;
  }
  if (Array.isArray(value)) {
    return value.map((item) => normalizeContextValue(item));
  }
  return normalizeContextKeys(value as Record<string, unknown>);
}

/**
 * 将 camelCase / PascalCase / acronym 字段转换为 snake_case。
 *
 * @param key - 原始字段名。
 * @returns snake_case 字段名。
 */
function toSnakeCase(key: string): string {
  return key
    .replace(/([A-Z]+)([A-Z][a-z])/g, "$1_$2")
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .replace(/[-\s]+/g, "_")
    .toLowerCase();
}

/**
 * 对单个键值对做脱敏处理。
 *
 * 若键名为敏感键（大小写不敏感）则值替换为 "[REDACTED]"；
 * 否则对对象/数组递归脱敏，标量原样返回。
 *
 * @param key - 字段名。
 * @param value - 字段值。
 * @returns 脱敏后的值。
 */
function redactValue(key: string, value: unknown): unknown {
  if (SENSITIVE_KEYS.includes(key.toLowerCase())) {
    return "[REDACTED]";
  }
  if (value === null || typeof value !== "object") {
    return value;
  }
  if (Array.isArray(value)) {
    return value.map((item) => redactValue("", item));
  }
  const obj = value as Record<string, unknown>;
  const result: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(obj)) {
    result[k] = redactValue(k, v);
  }
  return result;
}

// ---------- 公开日志函数 ----------

/**
 * 输出 DEBUG 级别日志。
 *
 * 开发环境（`tauri dev`）下输出到 DevTools console 并经 Tauri 落盘到
 * 仓库根 `logs/desktop.log`，便于关闭窗口后回看排查；纯浏览器 `vite dev`
 * 下没有 Tauri 运行时，仅输出到 DevTools console 不落盘。
 * 生产环境不输出也不落盘（避免噪音）。
 *
 * @param message - 日志消息。
 * @param context - 可选附加上下文字典。
 */
export function logDebug(message: string, context?: Record<string, unknown>): void {
  if (!import.meta.env?.DEV) return;
  const safeContext = redactContext(context);
  console.debug(`[DEBUG] ${message}`, safeContext);
  // 仅 Tauri 环境下（含 tauri dev）落盘；纯浏览器静默跳过
  if (isTauriEnv()) {
    const entry: LogEntry = {
      level: LogLevel.DEBUG,
      message,
      timestamp: new Date().toISOString(),
      context: safeContext,
    };
    writeToDisk(entry).catch(() => {});
  }
}

/**
 * 输出 INFO 级别日志。
 *
 * 记录应用启动、窗口创建、用户操作入口、关键状态变更等正常流程信息。
 *
 * @param message - 日志消息。
 * @param context - 可选附加上下文字典（如 task_id、模块名）。
 *
 * @sideeffect 生产期经 Tauri 写入 logs/desktop.log。
 */
export function logInfo(message: string, context?: Record<string, unknown>): void {
  const safeContext = redactContext(context);
  const entry: LogEntry = {
    level: LogLevel.INFO,
    message,
    timestamp: new Date().toISOString(),
    context: safeContext,
  };

  console.info(`[INFO] ${message}`, safeContext);
  // 异步落盘，不阻塞主流程
  writeToDisk(entry).catch(() => {});
}

/**
 * 输出 WARN 级别日志。
 *
 * 记录潜在问题、降级处理、重试等警告信息。
 *
 * @param message - 警告消息。
 * @param context - 可选附加上下文字典。
 *
 * @sideeffect 生产期经 Tauri 写入 logs/desktop.log。
 */
export function logWarn(message: string, context?: Record<string, unknown>): void {
  const safeContext = redactContext(context);
  const entry: LogEntry = {
    level: LogLevel.WARN,
    message,
    timestamp: new Date().toISOString(),
    context: safeContext,
  };

  console.warn(`[WARN] ${message}`, safeContext);
  writeToDisk(entry).catch(() => {});
}

/**
 * 输出 ERROR 级别日志。
 *
 * 记录异常路径、操作失败、IPC/SSE 错误等必须可排查的问题。
 * 自动提取错误对象的堆栈信息。
 *
 * @param message - 错误描述。
 * @param error - 可选的原始错误对象（用于提取堆栈）。
 * @param context - 可选附加上下文字典（如 task_id、模块名）。
 *
 * @sideeffect 生产期经 Tauri 写入 logs/desktop.log（含堆栈）。
 */
export function logError(
  message: string,
  error?: unknown,
  context?: Record<string, unknown>,
): void {
  const stack = error instanceof Error ? error.stack : undefined;
  const errorMessage = error instanceof Error ? error.message : String(error);

  const safeContext = redactContext({ ...context, originalMessage: message });

  const entry: LogEntry = {
    level: LogLevel.ERROR,
    message: `${message}: ${errorMessage}`,
    timestamp: new Date().toISOString(),
    context: safeContext,
    stack,
  };

  console.error(`[ERROR] ${entry.message}`, safeContext, stack);
  writeToDisk(entry).catch(() => {});
}

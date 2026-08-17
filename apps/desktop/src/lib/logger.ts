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

import { snakeCase } from "change-case";
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

import fastRedact from "fast-redact";

/**
 * 需要脱敏的敏感字段路径（fast-redact 语法，精确键名匹配）。
 *
 * 关键约束：这些键在 `redactContext` 中**先经 `normalizeContextKeys`（toSnakeCase）**
 * 归一化为 snake_case 后才交给 redactor 匹配。因此这里只列 snake_case 形态，
 * 不列 `apikey` / `privatekey` 等无下划线写法（归一化后实为 `api_key` / `private_key`，
 * 裸键永不命中，属死配置）。
 *
 * 由于 `redactRecursive` 会逐层递归对象 / 数组，每个层级单独用本表做精确键名匹配，
 * 故嵌套路径（如 `headers.authorization`）无需 `*.` 通配，子层 `{authorization: ...}`
 * 会被精确键 `authorization` 命中。
 *
 * 新增敏感字段只需在此扩展（保持 snake_case），无需改动脱敏逻辑。
 */
const SENSITIVE_PATHS = [
  "api_key",
  "password",
  "token",
  "secret",
  "authorization",
  "access_key",
  "private_key",
  "credential",
  "cookie",
];

/**
 * 脱敏器单例（进程级复用，避免每次调用重建编译后的 redactor）。
 *
 * `serialize: false`：fast-redact 默认将结果 JSON 序列化为字符串；
 * 本项目日志上下文需保持对象结构（调用方直接读取字段写入 LogEntry），
 * 故关闭序列化，使其返回**变异后的对象**（引用不变）。
 *
 * 不使用 `deep: true`：fast-redact 的 deep 模式依赖在对象上挂载隐藏代理标记，
 * 在同一 redactor 实例被多次调用时（嵌套 + 数组场景）会出现数组元素脱敏失效的
 * 复用 bug。因此改为**非 deep 模式 + 自研递归遍历**（见 `redactRecursive`），
 * 每个层级单独调用 redactor 做精确键名匹配，既能覆盖任意嵌套深度，又避开单例复用陷阱。
 */
const redactor = fastRedact({
  paths: SENSITIVE_PATHS,
  censor: "[REDACTED]",
  serialize: false,
});

/**
 * 递归脱敏任意深度的上下文值。
 *
 * 目的：
 *   自研递归遍历对象 / 数组（替代 fast-redact 的 deep 模式），
 *   对每个对象层调用 redactor 做精确键名匹配后，继续下钻其字段值。
 *   标量原样返回。避免 fast-redact deep 模式在同实例多次调用时的数组脱敏失效。
 *
 * 参数：
 *   value - 待脱敏的值（对象 / 数组 / 标量）。
 *
 * 返回：
 *   脱敏后的同构值（不修改入参）。
 *
 * 副作用：
 *   无（内部对每层对象做 JSON 克隆后交给 redactor，避免污染原对象）。
 */
function redactRecursive(value: unknown): unknown {
  if (value === null || typeof value !== "object") return value;
  if (Array.isArray(value)) return value.map((item) => redactRecursive(item));
  const obj = value as Record<string, unknown>;
  // redactor 非 deep 模式会变异传入对象再还原；克隆一份保证原对象不被改动。
  const layer = redactor(JSON.parse(JSON.stringify(obj))) as Record<string, unknown>;
  const result: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(layer)) {
    result[k] = redactRecursive(v);
  }
  return result;
}

/**
 * 对上下文对象做敏感字段脱敏，避免 secret 经日志泄漏到 console 或落盘文件。
 *
 * 委托 `fast-redact` 编译后的 redactor（精确键名匹配）做单层脱敏，
 * 配合 `redactRecursive` 的递归遍历覆盖嵌套对象 / 数组；
 * 替代原自研纯递归遍历。返回脱敏后的对象（不修改原对象）。
 * 匹配敏感键名的值会被替换为 "[REDACTED]"；非对象 / 数组值原样保留。
 *
 * @param context - 原始上下文字典。
 * @returns 脱敏后的上下文副本；原值为空时直接返回。
 */
function redactContext(context?: Record<string, unknown>): Record<string, unknown> | undefined {
  const merged = mergeTraceContext(context);
  if (!merged) return merged;
  const normalized = normalizeContextKeys(merged);
  return redactRecursive(normalized) as Record<string, unknown>;
}

/**
 * 导出脱敏函数供单元测试断言（安全关键逻辑，需可独立验证）。
 *
 * @param context - 原始上下文字典。
 * @returns 脱敏后的上下文副本。
 */
export { redactContext };

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
 * 委托 `change-case` 的 `snakeCase`（经长期验证的边界覆盖，如
 * `XMLParser` → `xml_parser`、`version2Update` → `version_2_update`），
 * 不再自研正则，避免边界覆盖不全。
 *
 * @param key - 原始字段名。
 * @returns snake_case 字段名。
 */
function toSnakeCase(key: string): string {
  return snakeCase(key);
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
/**
 * 沿错误链递归收集每一层的类型、消息与堆栈。
 *
 * 用于错误日志落盘时保留完整的因果链（如 ky 的 HTTPError 被包装进
 * ServiceError.cause 后，底层堆栈不应丢失）。只收集 `Error` 实例层，
 * 对循环引用或非 Error 的 cause 自动截断，避免无限递归或崩溃。
 *
 * @param error - 起始错误对象。
 * @param maxDepth - 最大递归深度，防御性防止异常深的 cause 链。
 * @returns 每一层错误现场的数组，顶层在前；无 cause 时返回空数组。
 */
function collectCauseChain(
  error: unknown,
  maxDepth = 8,
): Array<{ type: string; message: string; stack?: string }> {
  const chain: Array<{ type: string; message: string; stack?: string }> = [];
  if (!(error instanceof Error)) return chain;
  // 入口 error 已由顶层 stack 表示，加入 seen 防止被误当作 cause 重复展开
  // （也用于切断 a.cause=b, b.cause=a 这类回到入口的循环引用）。
  const seen = new Set<unknown>([error]);
  let current: unknown = error.cause;
  let depth = 0;
  while (current instanceof Error && depth < maxDepth) {
    if (seen.has(current)) break;
    seen.add(current);
    chain.push({
      type: current.name || current.constructor?.name || "Error",
      message: current.message,
      stack: current.stack,
    });
    const next = current.cause;
    // 下一层若已出现过（循环引用）则停止，避免无限递归；
    // 注意：seen 已含当前层，故循环回到已处理层时 next 必命中 seen。
    if (next instanceof Error && seen.has(next)) break;
    current = next;
    depth += 1;
  }
  return chain;
}

/**
 * 将错误因果链格式化为多段文本，拼到顶层堆栈之后。
 *
 * 格式：`Caused by <n>. <Type>: <message>\n<stack>`，每层之间空行分隔，
 * 保证落盘日志中底层原始错误（如 ky HTTPError/TimeoutError）的堆栈可见。
 *
 * @param chain - collectCauseChain 收集到的因果链。
 * @returns 拼接后的多段文本；空链返回空字符串。
 */
function formatCauseChain(
  chain: Array<{ type: string; message: string; stack?: string }>,
): string {
  if (chain.length === 0) return "";
  const blocks = chain.map((layer, index) => {
    const header = `Caused by ${index + 1}. ${layer.type}: ${layer.message}`;
    return layer.stack ? `${header}\n${layer.stack}` : header;
  });
  return `\n\n${blocks.join("\n\n")}`;
}

export function logError(
  message: string,
  error?: unknown,
  context?: Record<string, unknown>,
): void {
  const topStack = error instanceof Error ? error.stack : undefined;
  const errorMessage = error instanceof Error ? error.message : String(error);
  const causeChain = collectCauseChain(error);
  const causeText = formatCauseChain(causeChain);
  const stack = topStack ? `${topStack}${causeText}` : causeText || undefined;

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

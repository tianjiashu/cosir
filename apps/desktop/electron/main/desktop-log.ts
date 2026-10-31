import fs from "node:fs";
import path from "node:path";

import { appLogDir } from "./paths";

/** 单个日志文件的大小上限；超过时轮转为 `.1`、`.2`…，与后端日志的分片规则一致。 */
export const MAX_LOG_BYTES = 5 * 1024 * 1024;
const BACKUP_COUNT = 7;

/**
 * 以 JSONL 追加一条结构化日志到按本地日期命名的文件（`frontend.log` → `frontend-2026-10-30.log`）。
 *
 * 用同步 IO：main 进程单线程，整条写入不会与另一条交错，不需要额外加锁；日志行很短，
 * 同步写不会明显阻塞事件循环。`entry` 缺 `ts` 时补上 UTC 时间。失败抛出，由调用方决定是否忽略
 * ——日志失败不得影响宿主主流程。
 */
export function appendJsonLine(basePath: string, entry: Record<string, unknown>): void {
  const line = JSON.stringify({ ts: new Date().toISOString(), ...entry });
  const bytes = Buffer.byteLength(line) + 1;
  if (bytes > MAX_LOG_BYTES) throw new Error("日志单行超过大小上限");
  fs.mkdirSync(path.dirname(basePath), { recursive: true });
  const activePath = datedLogPath(basePath, localDate(new Date()));
  rotateIfNeeded(activePath, bytes);
  fs.appendFileSync(activePath, `${line}\n`);
}

export function datedLogPath(basePath: string, logDate: string): string {
  const { dir, name, ext } = path.parse(basePath);
  return path.join(dir, `${name}-${logDate}${ext}`);
}

export function shardPath(activePath: string, index: number): string {
  const { dir, name, ext } = path.parse(activePath);
  return path.join(dir, `${name}.${index}${ext}`);
}

/** 写入前判断：现有文件非空且加上这一行会超限时，先把它轮转出去，保证每个文件都不超过上限。 */
export function rotateIfNeeded(activePath: string, incomingBytes: number): void {
  let size = 0;
  try {
    size = fs.statSync(activePath).size;
  } catch {
    return;
  }
  if (size === 0 || size + incomingBytes <= MAX_LOG_BYTES) return;
  fs.rmSync(shardPath(activePath, BACKUP_COUNT), { force: true });
  for (let index = BACKUP_COUNT - 1; index >= 1; index -= 1) {
    const source = shardPath(activePath, index);
    if (fs.existsSync(source)) fs.renameSync(source, shardPath(activePath, index + 1));
  }
  fs.renameSync(activePath, shardPath(activePath, 1));
}

function localDate(date: Date): string {
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

/**
 * 宿主自己写的一行日志（桌面事件、后端控制台转写）：补齐统一 JSONL 契约里宿主没有的字段（宿主没有
 * trace 上下文，`trace_id` 恒为空串）。日志是诊断旁路：写入失败只吞掉，不影响调用方流程。
 */
export function appendHostLogLine(
  basePath: string,
  line: { level: string; logger: string; caller: string; event: string; msg: string; data: Record<string, unknown>; error?: unknown },
): void {
  const { error, ...fields } = line;
  try {
    appendJsonLine(basePath, {
      ...fields,
      trace_id: "",
      error: error === undefined ? null : { message: error instanceof Error ? error.message : String(error) },
      truncated: false,
    });
  } catch {
    // 诊断旁路失败不影响宿主。
  }
}

/** 宿主自身的生命周期事件（后端启停、窗口、退出确认等），写入 `.cosir/logs/desktop-*.log`。 */
export function logDesktopEvent(
  level: "INFO" | "WARNING" | "ERROR",
  caller: string,
  event: string,
  data: Record<string, unknown> = {},
  error?: unknown,
): void {
  appendHostLogLine(path.join(appLogDir(), "desktop.log"), { level, logger: "coding_agent.desktop", caller, event, msg: event, data, error });
}

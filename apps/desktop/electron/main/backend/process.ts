import { spawn, type ChildProcess } from "node:child_process";
import readline from "node:readline";

import { appendHostLogLine, logDesktopEvent } from "../desktop-log";
import type { BackendRuntime } from "./runtime";

export type BackendLaunch = {
  runtime: BackendRuntime;
  port: number;
  bootstateFile: string;
  dataDir: string;
  consoleLogFile: string;
  /** 宿主工具链路的认证 token（见 `host-link/client.ts`），后端据此只接受本宿主的连接。 */
  hostToken: string;
};

/**
 * 拉起后端进程。后端始终是宿主的直接子进程。
 *
 * 宿主存活契约：stdin 是一条宿主从不写入的管道，并注入 `CODING_AGENT_PARENT_PIPE=stdin`；后端读到 EOF
 * 即优雅退出。宿主主动停止时关闭它（见 `stopProcessTree`），宿主崩溃或被强杀时由操作系统关闭它，两种情况
 * 走同一条退出路径，同时覆盖 macOS 与 Windows。
 *
 * POSIX 上 `detached` 让后端成为进程组长，强杀兜底时可以对整组发信号（含后端拉起的工具子进程）。
 * stdout / stderr 逐行写入 `backend-console` 日志，用于排查后端在结构化日志可用之前的启动与崩溃问题。
 */
export function spawnBackend({ runtime, port, bootstateFile, dataDir, consoleLogFile, hostToken }: BackendLaunch): ChildProcess {
  const args = runtime.mode === "python-module" ? ["-m", "app"] : [];
  const child = spawn(runtime.launcher, args, {
    cwd: runtime.backendDir,
    detached: process.platform !== "win32",
    windowsHide: true,
    stdio: ["pipe", "pipe", "pipe"],
    env: {
      ...process.env,
      CODING_AGENT_PORT: String(port),
      CODING_AGENT_RELOAD: "false",
      CODING_AGENT_BOOT_STATE_FILE: bootstateFile,
      CODING_AGENT_DATA_DIR: dataDir,
      CODING_AGENT_TERMINAL_WORKER: runtime.terminalWorker,
      CODING_AGENT_PARENT_PIPE: "stdin",
      CODING_AGENT_HOST_TOKEN: hostToken,
    },
  });
  forwardConsole(child.stdout, consoleLogFile, "stdout");
  forwardConsole(child.stderr, consoleLogFile, "stderr");
  return child;
}

function forwardConsole(stream: NodeJS.ReadableStream | null, logFile: string, name: "stdout" | "stderr"): void {
  if (!stream) return;
  readline.createInterface({ input: stream }).on("line", (line) => {
    appendHostLogLine(logFile, {
      level: "INFO",
      logger: "coding_agent.backend_console",
      caller: "backend_process",
      event: "backend_console_output",
      msg: line,
      data: { stream: name },
    });
  });
}

/** 优雅退出的宽限期：须覆盖 uvicorn 等长连接的 2 秒（`timeout_graceful_shutdown`）加 lifespan 收尾。 */
const STOP_GRACE_MS = 8_000;
/** 强杀后再等多久；超时只记日志放行，宿主退出不能被一个杀不掉的进程卡住。 */
const FORCE_EXIT_WAIT_MS = 3_000;

/**
 * 停止后端进程树并等它退出：先请求优雅退出，超过宽限期再强杀进程树。
 *
 * 优雅退出的请求就是关闭 stdin：与宿主消失时同一条路径（后端 `app/parent_watch.py` 读到 EOF →
 * uvicorn 优雅关闭 → lifespan 收尾，工具子进程与 Terminal Worker 由后端自己清理），两个平台一致。
 * 不用 SIGTERM / 不带 `/F` 的 `taskkill`：前者会同时打到整个进程组、与后端自己的清理抢跑，后者结束
 * 不了没有窗口的控制台进程。强杀只兜底：POSIX 对整个进程组发 SIGKILL，Windows 用 `taskkill /T /F`。
 */
export async function stopProcessTree(child: ChildProcess): Promise<void> {
  if (child.exitCode !== null || child.signalCode !== null || child.pid === undefined) return;
  const exited = new Promise<boolean>((resolve) => child.once("exit", () => resolve(true)));
  const pid = child.pid;
  child.stdin?.end();
  if (await Promise.race([exited, delay(STOP_GRACE_MS)])) return;
  killTree(pid);
  if (await Promise.race([exited, delay(FORCE_EXIT_WAIT_MS)])) return;
  logDesktopEvent("ERROR", "backend_process", "backend_kill_timeout", { pid, waitMs: FORCE_EXIT_WAIT_MS });
}

function killTree(pid: number): void {
  try {
    if (process.platform === "win32") {
      spawn("taskkill", ["/PID", String(pid), "/T", "/F"], { windowsHide: true, stdio: "ignore" });
    } else {
      process.kill(-pid, "SIGKILL");
    }
  } catch {
    // 进程组已不存在：等同已经退出。
  }
}

function delay(ms: number): Promise<false> {
  return new Promise((resolve) => setTimeout(() => resolve(false), ms));
}

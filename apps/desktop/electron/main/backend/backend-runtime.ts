import { execFile } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { promisify } from "node:util";
import { app } from "electron";

const execFileAsync = promisify(execFile);

/**
 * 后端的启动方式。运行时解析与进程创建分离，`process.ts` 不了解环境变量与目录约定。
 *
 * - `python-module`：解释器 + `-m app`，开发期（解释器由 uv 解析或 `COSIR_BACKEND_PYTHON` 显式指定）。
 * - `frozen-executable`：随应用交付的 PyInstaller 可执行文件。
 */
export type BackendRuntime = {
  mode: "python-module" | "frozen-executable";
  launcher: string;
  backendDir: string;
  terminalWorker: string;
};

/**
 * 解析当前应使用的后端运行时；任何前置条件缺失都抛出带中文说明的错误，由 supervisor 转成启动失败。
 *
 * 未打包时：后端目录取 `COSIR_BACKEND_DIR`（默认仓库 `apps/backend`），Terminal Worker 必须由开发启动脚本
 * 经 `CODING_AGENT_TERMINAL_WORKER` 注入（脚本负责先构建它，宿主不猜路径）。打包后：两者都在资源目录里。
 */
export async function resolveBackendRuntime(runtimeDir: string): Promise<BackendRuntime> {
  if (app.isPackaged) {
    const backendDir = path.join(process.resourcesPath, "backend");
    const launcher = path.join(backendDir, process.platform === "win32" ? "cosir-backend.exe" : "cosir-backend");
    const terminalWorker = path.join(process.resourcesPath, "terminal-worker", terminalWorkerName());
    requireFile(launcher, "随应用交付的本地后端可执行文件不存在");
    requireFile(terminalWorker, "随应用交付的 Terminal Worker 不存在");
    return { mode: "frozen-executable", launcher, backendDir, terminalWorker };
  }

  const backendDir = process.env.COSIR_BACKEND_DIR ?? path.resolve(__dirname, "..", "..", "apps", "backend");
  const configuredWorker = process.env.CODING_AGENT_TERMINAL_WORKER;
  if (!configuredWorker) throw new Error("开发模式未注入 CODING_AGENT_TERMINAL_WORKER");
  requireFile(configuredWorker, "开发模式 Terminal Worker 不存在");
  const launcher = process.env.COSIR_BACKEND_PYTHON ?? await resolveUvInterpreter(backendDir, path.join(runtimeDir, "uv-cache"));
  return { mode: "python-module", launcher, backendDir, terminalWorker: configuredWorker };
}

/**
 * 用 uv 同步项目环境并解析后端解释器路径。
 *
 * 刻意不把 `uv run` 当作被监督的进程：那样真正的后端会是 uv 的孙进程，停止时信号落不到它身上，
 * uv 一退后端就被系统收养并长期驻留（Tauri 宿主时期实测每次退出泄漏一个后端，各自占端口并持有 SQLite）。
 * 所以 uv 只负责「解析 + 同步」，之后宿主直接拉起解释器，后端是宿主的直接子进程。
 */
async function resolveUvInterpreter(backendDir: string, cacheDir: string): Promise<string> {
  fs.mkdirSync(cacheDir, { recursive: true });
  let stdout: string;
  try {
    ({ stdout } = await execFileAsync(
      "uv",
      ["run", "--directory", backendDir, "python", "-c", "import sys; print(sys.executable)"],
      { env: { ...process.env, UV_CACHE_DIR: cacheDir }, windowsHide: true },
    ));
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    throw new Error(`uv 解析后端解释器失败，请安装 uv 或设置 COSIR_BACKEND_PYTHON 指向 Python 解释器：${detail}`);
  }
  const launcher = stdout.trim();
  requireFile(launcher, "uv 返回的后端解释器路径不可用");
  return launcher;
}

function terminalWorkerName(): string {
  return process.platform === "win32" ? "terminal-worker.exe" : "terminal-worker";
}

function requireFile(file: string, message: string): void {
  if (!fs.existsSync(file) || !fs.statSync(file).isFile()) throw new Error(`${message}：${file}`);
}

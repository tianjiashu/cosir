import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

import { repositoryRoot, targetDirectory } from "./project-paths.mjs";

/**
 * 构造项目 Cargo 子进程使用的环境变量。
 *
 * 项目明确以仓库根 `target/` 为唯一构建事实源，因此会覆盖调用环境中的
 * `CARGO_TARGET_DIR`。调用方可以通过 extraEnvironment 传递其他环境变量，
 * 但不能改变项目的 Cargo target 目录。
 */
export function projectCargoEnvironment(extraEnvironment = {}) {
  return {
    ...process.env,
    ...extraEnvironment,
    CARGO_TARGET_DIR: targetDirectory,
  };
}

/**
 * Terminal Worker 可执行文件名。
 *
 * 交叉构建时必须以目标平台而不是当前 Node 进程所在平台决定文件名；未
 * 提供目标 triple 时才回退到当前宿主平台。
 */
export function terminalWorkerExecutableName(targetTriple = "") {
  const windows = targetTriple ? targetTriple.toLowerCase().includes("windows") : process.platform === "win32";
  return windows ? "terminal-worker.exe" : "terminal-worker";
}

/**
 * 构建开发用（debug）Terminal Worker 并返回可执行文件路径。
 *
 * 开发启动脚本与桌面 E2E 共用：宿主与后端都不猜测 Worker 位置，由调用方把这里返回的路径经
 * `CODING_AGENT_TERMINAL_WORKER` 注入。构建失败或产物缺失时抛出，避免宿主进入半可用状态。
 */
export function buildDevelopmentTerminalWorker() {
  const build = spawnSync(process.platform === "win32" ? "cargo.exe" : "cargo", [
    "build", "--package", "cosir-terminal-worker", "--locked",
  ], { cwd: repositoryRoot, env: projectCargoEnvironment(), stdio: "inherit", windowsHide: true });
  if (build.error) throw new Error(`无法启动 Terminal Worker 构建：${build.error.message}`);
  if (build.status !== 0) throw new Error(`Terminal Worker 构建失败（退出码 ${build.status}）`);
  const executable = path.join(targetDirectory, "debug", terminalWorkerExecutableName());
  if (!fs.existsSync(executable)) throw new Error(`构建完成但未找到 Terminal Worker：${executable}`);
  return executable;
}

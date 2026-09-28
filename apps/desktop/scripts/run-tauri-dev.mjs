import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import {
  projectCargoEnvironment,
  projectTargetDirectory,
} from "./cargo-target.mjs";

const desktopRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repositoryRoot = path.resolve(desktopRoot, "..", "..");
const targetDirectory = projectTargetDirectory(repositoryRoot);
const workerExecutable = path.join(
  targetDirectory,
  "debug",
  process.platform === "win32" ? "terminal-worker.exe" : "terminal-worker",
);
const developmentResourceDirectories = [
  path.join(targetDirectory, "resources", "backend"),
  path.join(targetDirectory, "resources", "terminal-worker"),
];

/**
 * 执行开发桌面启动流程，并把已经确认的 Worker 路径注入 Tauri 环境。
 *
 * 该脚本是开发模式唯一的构建编排入口：先通过根 Cargo workspace 构建
 * Terminal Worker，再启动 Tauri。脚本会把 Cargo target 目录规范化为绝对路径，
 * 并同时注入 Tauri 和后端；后端不再自行猜测或回退 Worker 路径。
 *
 * 副作用：更新 Cargo target 目录中的构建产物，并启动 Tauri/Vite/后端进程树。
 * 异常：任一步骤失败都返回非零退出码，阻止桌面应用进入半可用状态。
 */
function run() {
  for (const directory of developmentResourceDirectories) {
    fs.mkdirSync(directory, { recursive: true });
  }

  const cargo = process.platform === "win32" ? "cargo.exe" : "cargo";
  const build = spawnSync(
    cargo,
    [
      "build",
      "--package",
      "cosir-terminal-worker",
      "--locked",
      "--target-dir",
      targetDirectory,
    ],
    {
      cwd: repositoryRoot,
      env: projectCargoEnvironment(repositoryRoot),
      stdio: "inherit",
      windowsHide: true,
    },
  );
  if (build.error) {
    throw new Error(`无法启动 Terminal Worker 构建：${build.error.message}`);
  }
  if (build.status !== 0) {
    process.exitCode = build.status ?? 1;
    return;
  }
  if (!fs.existsSync(workerExecutable)) {
    throw new Error(`Workspace 构建完成但未找到 Terminal Worker：${workerExecutable}`);
  }
  if (process.platform !== "win32") {
    fs.chmodSync(workerExecutable, 0o755);
  }

  const tauri = spawnSync(
    cargo,
    [
      "tauri",
      "dev",
      "--config",
      "src-tauri/tauri.conf.json",
      ...process.argv.slice(2),
    ],
    {
      cwd: desktopRoot,
      env: projectCargoEnvironment(repositoryRoot, {
        CODING_AGENT_TERMINAL_WORKER: workerExecutable,
      }),
      stdio: "inherit",
      windowsHide: true,
    },
  );
  if (tauri.error) {
    throw new Error(`无法启动 Tauri 开发模式：${tauri.error.message}`);
  }
  process.exitCode = tauri.status ?? 1;
}

try {
  run();
} catch (error) {
  console.error(`[cosir] ${error instanceof Error ? error.message : String(error)}`);
  process.exitCode = 1;
}

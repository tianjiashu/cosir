import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { projectCargoEnvironment } from "./cargo-target.mjs";

const desktopRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repositoryRoot = path.resolve(desktopRoot, "..", "..");

/**
 * 执行 Tauri 发布构建，并强制桌面宿主与资源 staging 使用同一 Cargo 目录。
 *
 * 该脚本只负责构建编排，不负责复制或清理发布资源；资源准备由
 * `build:bundle` 和 `stage-portable` 等专用脚本完成。构建失败时以非零状态
 * 退出，阻止生成缺少 Worker 或后端资源的半成品发布目录。
 */
function run() {
  const cargo = process.platform === "win32" ? "cargo.exe" : "cargo";
  const result = spawnSync(
    cargo,
    ["tauri", "build", ...process.argv.slice(2)],
    {
      cwd: desktopRoot,
      env: projectCargoEnvironment(repositoryRoot),
      stdio: "inherit",
      windowsHide: true,
    },
  );
  if (result.error) {
    throw new Error(`无法启动 Tauri 发布构建：${result.error.message}`);
  }
  process.exitCode = result.status ?? 1;
}

try {
  run();
} catch (error) {
  console.error(`[cosir] ${error instanceof Error ? error.message : String(error)}`);
  process.exitCode = 1;
}

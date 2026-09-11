import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const desktopRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repositoryRoot = path.resolve(desktopRoot, "..", "..");
const workerRoot = path.join(repositoryRoot, "apps", "terminal-worker");
const manifestPath = path.join(workerRoot, "Cargo.toml");
const targetTriple = (
  process.env.COSIR_TERMINAL_WORKER_TARGET || process.env.TAURI_ENV_TARGET_TRIPLE || ""
).trim();
const executableName = process.platform === "win32" ? "terminal-worker.exe" : "terminal-worker";
// 直接复用 cargo 解析后的真实 target 目录（尊重 CARGO_TARGET_DIR 与 .cargo/config.toml 的
// target-dir 配置），避免与统一构建路径后的实际输出位置脱节。
const cargoTargetRoot = resolveCargoTargetDir(manifestPath);

/**
 * 通过 `cargo metadata` 取得当前 crate 解析后的 target 目录。
 *
 * 该目录由 CARGO_TARGET_DIR 环境变量或仓库根 .cargo/config.toml 的
 * target-dir 决定，是 cargo 实际落盘编译产物的位置。直接读取其
 * `target_directory` 字段，避免硬编码回退路径导致的产物找不到问题。
 *
 * @param {string} manifestPath Cargo.toml 的绝对路径
 * @returns {string} 已解析的 target 目录绝对路径
 */
function resolveCargoTargetDir(manifestPath) {
  const result = spawnSync(process.platform === "win32" ? "cargo.exe" : "cargo", [
    "metadata",
    "--manifest-path",
    manifestPath,
    "--no-deps",
    "--format-version",
    "1",
  ], { cwd: repositoryRoot, stdio: ["ignore", "pipe", "inherit"], windowsHide: true });
  if (result.error) {
    throw new Error(`无法启动 cargo metadata：${result.error.message}`);
  }
  if (result.status !== 0) {
    throw new Error(`cargo metadata 执行失败，退出码：${result.status}`);
  }
  const metadata = JSON.parse(result.stdout.toString("utf-8"));
  return metadata.target_directory;
}
const releaseRoot = targetTriple
  ? path.join(cargoTargetRoot, targetTriple, "release")
  : path.join(cargoTargetRoot, "release");
const sourcePath = path.join(releaseRoot, executableName);
const stagingRoot = path.join(desktopRoot, "src-tauri", "resources", "terminal-worker");
const destinationPath = path.join(stagingRoot, executableName);

function runCargoBuild() {
  const args = ["build", "--release", "--locked", "--manifest-path", manifestPath];
  if (targetTriple) {
    args.push("--target", targetTriple);
  }
  const result = spawnSync(process.platform === "win32" ? "cargo.exe" : "cargo", args, {
    cwd: repositoryRoot,
    stdio: "inherit",
    windowsHide: true,
  });
  if (result.error) {
    throw new Error(`无法启动 cargo 构建 Terminal Worker：${result.error.message}`);
  }
  if (result.status !== 0) {
    throw new Error(`Terminal Worker release 构建失败，退出码：${result.status}`);
  }
}

function stageBinary() {
  if (!fs.existsSync(sourcePath)) {
    throw new Error(`未找到构建产物：${sourcePath}`);
  }
  fs.mkdirSync(stagingRoot, { recursive: true });
  for (const staleName of ["terminal-worker", "terminal-worker.exe"]) {
    const stalePath = path.join(stagingRoot, staleName);
    if (stalePath !== destinationPath && fs.existsSync(stalePath)) {
      fs.unlinkSync(stalePath);
    }
  }
  fs.copyFileSync(sourcePath, destinationPath);
  if (process.platform !== "win32") {
    fs.chmodSync(destinationPath, 0o755);
  }
  console.log(`已 staging Terminal Worker：${destinationPath}`);
}

runCargoBuild();
stageBinary();

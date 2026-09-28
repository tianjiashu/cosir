import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import {
  projectCargoEnvironment,
  projectTargetDirectory,
  targetUsesWindowsExecutable,
} from "./cargo-target.mjs";

const desktopRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repositoryRoot = path.resolve(desktopRoot, "..", "..");
const buildArtifactsRoot = projectTargetDirectory(repositoryRoot);
const workspaceManifestPath = path.join(repositoryRoot, "Cargo.toml");
const targetTriple = (
  process.env.COSIR_TERMINAL_WORKER_TARGET || process.env.TAURI_ENV_TARGET_TRIPLE || ""
).trim();
const executableName = targetUsesWindowsExecutable(targetTriple)
  ? "terminal-worker.exe"
  : "terminal-worker";
const releaseRoot = targetTriple
  ? path.join(buildArtifactsRoot, targetTriple, "release")
  : path.join(buildArtifactsRoot, "release");
const sourcePath = path.join(releaseRoot, executableName);
const stagingRoot = path.join(buildArtifactsRoot, "resources", "terminal-worker");
const destinationPath = path.join(stagingRoot, executableName);

function runCargoBuild() {
  const args = [
    "build",
    "--package",
    "cosir-terminal-worker",
    "--release",
    "--locked",
    "--manifest-path",
    workspaceManifestPath,
    "--target-dir",
    buildArtifactsRoot,
  ];
  if (targetTriple) {
    args.push("--target", targetTriple);
  }
  const result = spawnSync(process.platform === "win32" ? "cargo.exe" : "cargo", args, {
    cwd: repositoryRoot,
    env: projectCargoEnvironment(repositoryRoot),
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

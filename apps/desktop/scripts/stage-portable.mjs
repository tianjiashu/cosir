import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const desktopRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repositoryRoot = path.resolve(desktopRoot, "..", "..");
const manifestPath = path.join(desktopRoot, "src-tauri", "Cargo.toml");
const resourcesRoot = path.join(desktopRoot, "src-tauri", "resources");
const artifactsRoot = path.join(desktopRoot, "artifacts");
const portableRoot = path.join(artifactsRoot, "Cosir-portable");
const appExecutable = process.platform === "win32" ? "Cosir.exe" : "Cosir";
const sourceExecutableName = process.platform === "win32" ? "cosir-desktop.exe" : "cosir-desktop";

function assertWithin(parent, candidate, label) {
  const relative = path.relative(parent, candidate);
  if (!relative || relative.startsWith("..") || path.isAbsolute(relative)) {
    throw new Error(`拒绝操作仓库目录之外的${label}：${candidate}`);
  }
}

function resolveCargoTargetDir() {
  const result = spawnSync(process.platform === "win32" ? "cargo.exe" : "cargo", [
    "metadata",
    "--manifest-path",
    manifestPath,
    "--no-deps",
    "--format-version",
    "1",
  ], { cwd: repositoryRoot, stdio: ["ignore", "pipe", "inherit"], windowsHide: true });
  if (result.error) throw new Error(`无法启动 cargo metadata：${result.error.message}`);
  if (result.status !== 0) throw new Error(`cargo metadata 失败，退出码：${result.status}`);
  return JSON.parse(result.stdout.toString("utf-8")).target_directory;
}

function copyResourceDirectory(source, destination, label) {
  if (!fs.existsSync(source) || !fs.statSync(source).isDirectory()) {
    throw new Error(`便携包缺少${label}目录：${source}`);
  }
  fs.mkdirSync(destination, { recursive: true });
  for (const entry of fs.readdirSync(source)) {
    if (entry === ".gitkeep") continue;
    fs.cpSync(path.join(source, entry), path.join(destination, entry), { recursive: true });
  }
}

const releaseRoot = path.join(resolveCargoTargetDir(), "release");
const sourceExecutable = path.join(releaseRoot, sourceExecutableName);
if (!fs.existsSync(sourceExecutable)) {
  throw new Error(`未找到 Tauri 发布版程序：${sourceExecutable}`);
}

assertWithin(desktopRoot, artifactsRoot, "便携包输出目录");
assertWithin(artifactsRoot, portableRoot, "便携包目录");
fs.rmSync(portableRoot, { recursive: true, force: true });
fs.mkdirSync(portableRoot, { recursive: true });
fs.copyFileSync(sourceExecutable, path.join(portableRoot, appExecutable));
copyResourceDirectory(path.join(resourcesRoot, "backend"), path.join(portableRoot, "backend"), "Python 后端");
copyResourceDirectory(
  path.join(resourcesRoot, "terminal-worker"),
  path.join(portableRoot, "terminal-worker"),
  "Terminal Worker",
);

const readme = [
  "Cosir Windows portable build",
  "",
  "Double-click Cosir.exe to start the desktop app.",
  "Keep the backend and terminal-worker folders beside Cosir.exe.",
  "App data and SQLite databases are stored in the current Windows user data directory.",
  "",
].join("\r\n");
fs.writeFileSync(path.join(portableRoot, "README.txt"), readme, "utf8");

const totalBytes = fs.readdirSync(portableRoot, { recursive: true }).reduce((total, entry) => {
  const candidate = path.join(portableRoot, entry.toString());
  return fs.statSync(candidate).isFile() ? total + fs.statSync(candidate).size : total;
}, 0);
console.log(
  `便携版已生成：${path.join(portableRoot, appExecutable)} (${(totalBytes / 1024 / 1024).toFixed(1)} MB)`,
);

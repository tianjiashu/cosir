import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { projectTargetDirectory, targetUsesWindowsExecutable } from "./cargo-target.mjs";

const desktopRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repositoryRoot = path.resolve(desktopRoot, "..", "..");
const artifactsRoot = projectTargetDirectory(repositoryRoot);
const targetTriple = (
  process.env.COSIR_TERMINAL_WORKER_TARGET || process.env.TAURI_ENV_TARGET_TRIPLE || ""
).trim();
const resourcesRoot = path.join(artifactsRoot, "resources");
const portableRoot = path.join(artifactsRoot, "Cosir-portable");
const targetIsWindows = targetUsesWindowsExecutable(targetTriple);
const appExecutable = targetIsWindows ? "Cosir.exe" : "Cosir";
const sourceExecutableName = targetIsWindows ? "cosir-desktop.exe" : "cosir-desktop";

function assertWithin(parent, candidate, label) {
  const relative = path.relative(parent, candidate);
  if (!relative || relative.startsWith("..") || path.isAbsolute(relative)) {
    throw new Error(`拒绝操作仓库目录之外的${label}：${candidate}`);
  }
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

const releaseRoot = targetTriple
  ? path.join(artifactsRoot, targetTriple, "release")
  : path.join(artifactsRoot, "release");
const sourceExecutable = path.join(releaseRoot, sourceExecutableName);
if (!fs.existsSync(sourceExecutable)) {
  throw new Error(`未找到 Tauri 发布版程序：${sourceExecutable}`);
}

assertWithin(repositoryRoot, artifactsRoot, "便携包输出目录");
assertWithin(artifactsRoot, portableRoot, "便携包目录");
fs.rmSync(portableRoot, { recursive: true, force: true });
fs.mkdirSync(portableRoot, { recursive: true });
fs.copyFileSync(sourceExecutable, path.join(portableRoot, appExecutable));
copyResourceDirectory(
  path.join(resourcesRoot, "terminal-worker"),
  path.join(portableRoot, "app-resources", "terminal-worker"),
  "Terminal Worker",
);
copyResourceDirectory(
  path.join(resourcesRoot, "backend"),
  path.join(portableRoot, "app-resources", "backend"),
  "Python 后端运行目录",
);

const readme = [
  "Cosir Windows portable build",
  "",
  "Double-click Cosir.exe to start the desktop app.",
  "Keep the app-resources folder beside Cosir.exe.",
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

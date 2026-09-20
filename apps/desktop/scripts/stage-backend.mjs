import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const desktopRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repositoryRoot = path.resolve(desktopRoot, "..", "..");
const backendRoot = path.join(repositoryRoot, "apps", "backend");
const packagingEnvironment = path.join(backendRoot, ".venv-packaging");
const buildArtifactsRoot = path.join(repositoryRoot, "target");
const distributionRoot = path.join(buildArtifactsRoot, "backend");
const buildRoot = path.join(buildArtifactsRoot, "pyinstaller");
const appName = "cosir-backend";
const sourceRoot = path.join(distributionRoot, appName);
const stagingRoot = path.join(buildArtifactsRoot, "resources", "backend");
const executableName = process.platform === "win32" ? `${appName}.exe` : appName;
const pyinstallerExecutable = process.platform === "win32" ? "pyinstaller.exe" : "pyinstaller";
const uvExecutable = process.platform === "win32" ? "uv.exe" : "uv";
const providerCapabilityRoot = path.join(
  backendRoot,
  "app",
  "core",
  "llm_provider",
  "capability",
);
const providerCapabilityDestination = "app/core/llm_provider/capability";
const providerCapabilityDataFiles = ["llm_provider.json", "model_capabilities.json"].map(
  (fileName) => path.join(providerCapabilityRoot, fileName),
);
const pyinstallerDataSeparator = process.platform === "win32" ? ";" : ":";

function assertWithin(parent, candidate, label) {
  const relative = path.relative(parent, candidate);
  if (!relative || relative.startsWith("..") || path.isAbsolute(relative)) {
    throw new Error(`拒绝操作仓库目录之外的${label}：${candidate}`);
  }
}

function run(executable, args, options = {}) {
  const result = spawnSync(executable, args, {
    cwd: backendRoot,
    stdio: "inherit",
    windowsHide: true,
    ...options,
  });
  if (result.error) {
    throw new Error(`无法启动 ${executable}：${result.error.message}`);
  }
  if (result.status !== 0) {
    throw new Error(`${executable} 失败，退出码：${result.status}`);
  }
}

function resolveUv() {
  const configured = process.env.COSIR_UV || process.env.UV_EXECUTABLE;
  if (configured) return configured;
  const command = spawnSync(uvExecutable, ["--version"], {
    cwd: repositoryRoot,
    stdio: "ignore",
    windowsHide: true,
  });
  if (command.error || command.status !== 0) {
    throw new Error("构建桌面发布包需要 uv。请安装 uv，或通过 COSIR_UV 指定 uv 可执行文件。");
  }
  return uvExecutable;
}

function validateTargetHost() {
  const target = (process.env.TAURI_ENV_TARGET_TRIPLE || "").trim().toLowerCase();
  if (!target) return;
  const targetOs = target.includes("windows")
    ? "win32"
    : target.includes("apple-darwin")
      ? "darwin"
      : target.includes("linux")
        ? "linux"
        : undefined;
  const targetArch = target.startsWith("aarch64") || target.startsWith("arm64")
    ? "arm64"
    : target.startsWith("i686") || target.startsWith("i586") || target.startsWith("i386")
      ? "ia32"
      : target.startsWith("x86_64") || target.startsWith("x64")
        ? "x64"
        : undefined;
  if (targetOs && targetOs !== process.platform) {
    throw new Error(`PyInstaller 不能交叉构建目标 ${target}；请在对应操作系统上构建。`);
  }
  if (targetArch && targetArch !== process.arch) {
    throw new Error(`当前 Python 架构 ${process.arch} 与 Tauri 目标 ${target} 不一致。`);
  }
}

validateTargetHost();

const uv = resolveUv();
const pyinstallerPath = path.join(packagingEnvironment, "Scripts", pyinstallerExecutable);
const unixPyinstallerPath = path.join(packagingEnvironment, "bin", pyinstallerExecutable);
const actualPyinstallerPath = process.platform === "win32" ? pyinstallerPath : unixPyinstallerPath;

const syncEnvironment = {
  ...process.env,
  UV_PROJECT_ENVIRONMENT: packagingEnvironment,
  UV_CACHE_DIR: process.env.UV_CACHE_DIR || path.join(repositoryRoot, ".uv-cache"),
};
run(uv, ["sync", "--frozen", "--no-dev", "--group", "build", "--project", backendRoot], {
  cwd: repositoryRoot,
  env: syncEnvironment,
});

const entrypoint = path.join(backendRoot, "scripts", "frozen_backend_entrypoint.py");
const pyinstallerArgs = [
  "--noconfirm",
  "--clean",
  "--onedir",
  "--windowed",
  "--name",
  appName,
  "--contents-directory",
  "_internal",
  "--distpath",
  distributionRoot,
  "--workpath",
  buildRoot,
  "--specpath",
  buildRoot,
  "--collect-submodules",
  "app",
  ...providerCapabilityDataFiles.flatMap((filePath) => [
    "--add-data",
    `${filePath}${pyinstallerDataSeparator}${providerCapabilityDestination}`,
  ]),
  entrypoint,
];
run(actualPyinstallerPath, pyinstallerArgs, {
  cwd: backendRoot,
  env: {
    ...syncEnvironment,
    PYTHONPATH: backendRoot,
  },
});

const packagedExecutable = path.join(sourceRoot, executableName);
if (!fs.existsSync(packagedExecutable)) {
  throw new Error(`PyInstaller 未生成后端可执行文件：${packagedExecutable}`);
}

function assertPackagedCapabilityData(root, label) {
  const capabilityRoot = path.join(root, "_internal", providerCapabilityDestination);
  for (const filePath of providerCapabilityDataFiles) {
    const fileName = path.basename(filePath);
    const packagedPath = path.join(capabilityRoot, fileName);
    if (!fs.existsSync(packagedPath)) {
      throw new Error(`PyInstaller ${label}缺少 Provider 能力数据文件：${packagedPath}`);
    }
  }
}

assertPackagedCapabilityData(sourceRoot, "产物");

assertWithin(buildArtifactsRoot, sourceRoot, "后端构建产物");
assertWithin(buildArtifactsRoot, stagingRoot, "Tauri 后端资源目录");
fs.mkdirSync(path.dirname(stagingRoot), { recursive: true });
fs.mkdirSync(stagingRoot, { recursive: true });
for (const entry of fs.readdirSync(stagingRoot)) {
  if (entry === ".gitkeep") continue;
  const stalePath = path.join(stagingRoot, entry);
  assertWithin(stagingRoot, stalePath, "过期后端资源");
  fs.rmSync(stalePath, { recursive: true, force: true });
}
for (const entry of fs.readdirSync(sourceRoot)) {
  fs.cpSync(path.join(sourceRoot, entry), path.join(stagingRoot, entry), { recursive: true });
}
assertPackagedCapabilityData(stagingRoot, "staging 资源");
if (process.platform !== "win32") {
  fs.chmodSync(path.join(stagingRoot, executableName), 0o755);
}

const executableBytes = fs.statSync(path.join(stagingRoot, executableName)).size;
const bundleBytes = fs.readdirSync(stagingRoot, { recursive: true }).reduce((total, entry) => {
  const candidate = path.join(stagingRoot, entry.toString());
  return fs.statSync(candidate).isFile() ? total + fs.statSync(candidate).size : total;
}, 0);
console.log(
  `已 staging PyInstaller onedir 后端：${path.join(stagingRoot, executableName)} (${(executableBytes / 1024 / 1024).toFixed(1)} MB executable，${(bundleBytes / 1024 / 1024).toFixed(1)} MB bundle)`,
);

import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { targetDirectory } from "./project-paths.mjs";

const require = createRequire(import.meta.url);

/**
 * 返回开发宿主可执行文件。macOS 的 Dock 读取应用包名称，app.setName 无法改变它，
 * 因此在 target 下复制专用 Cosir.app；其他平台直接使用依赖中的 Electron。
 *
 * 原始应用包保持不动，保留 Electron 可执行文件、Helper 与 framework 的相对关系。
 * 缓存按源 plist（含 Electron 版本）、架构和本脚本内容区分；完整生成后才发布，
 * 失败时清除临时副本并抛错。这里只准备开发运行时，不打包业务代码，仍由启动参数加载工作区。
 */
export function developmentElectronExecutable() {
  const executable = require("electron");
  if (process.platform !== "darwin") return executable;

  const sourceBundle = path.resolve(executable, "..", "..", "..");
  const sourcePlist = path.join(sourceBundle, "Contents", "Info.plist");
  const fingerprint = createHash("sha256")
    .update(fs.readFileSync(sourcePlist))
    .update(process.arch)
    .update(fs.readFileSync(fileURLToPath(import.meta.url)))
    .digest("hex").slice(0, 16);
  const cacheRoot = path.join(targetDirectory, "electron-dev");
  const cacheDirectory = path.join(cacheRoot, fingerprint);
  const bundle = path.join(cacheDirectory, "Cosir.app");
  const brandedExecutable = path.join(bundle, "Contents", "MacOS", "Electron");
  if (fs.existsSync(brandedExecutable)) return brandedExecutable;

  fs.mkdirSync(cacheRoot, { recursive: true });
  const staging = fs.mkdtempSync(path.join(cacheRoot, ".prepare-"));
  try {
    const stagedBundle = path.join(staging, "Cosir.app");
    fs.cpSync(sourceBundle, stagedBundle, {
      recursive: true,
      verbatimSymlinks: true,
      mode: fs.constants.COPYFILE_FICLONE,
    });
    const plist = path.join(stagedBundle, "Contents", "Info.plist");
    for (const [key, value] of Object.entries({
      CFBundleName: "Cosir",
      CFBundleDisplayName: "Cosir",
      CFBundleIdentifier: "dev.cosir.desktop",
    })) {
      execFileSync("/usr/bin/plutil", ["-replace", key, "-string", value, plist]);
    }
    fs.renameSync(staging, cacheDirectory);
  } finally {
    fs.rmSync(staging, { recursive: true, force: true });
  }
  return brandedExecutable;
}

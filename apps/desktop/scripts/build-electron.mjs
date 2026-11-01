import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import * as esbuild from "esbuild";

import { desktopRoot, targetDirectory } from "./project-paths.mjs";

/** main 与 preload 的输出目录；`package.json` 的 `main` 指向这里的 `main.cjs`。 */
export const electronOutDir = path.join(targetDirectory, "electron");

/**
 * Electron main 与 preload 的 esbuild 配置：两者都打成单个 CJS 文件（沙箱 preload 不能加载 ESM 或多个模块），
 * `electron` 由运行时提供，不打包。渲染进程仍由 Vite 构建，与这里无关。
 */
export const electronBuildOptions = {
  entryPoints: {
    main: path.join(desktopRoot, "electron", "main", "app.ts"),
    preload: path.join(desktopRoot, "electron", "preload", "index.ts"),
    // 内置浏览器网页的 preload（`browser-session.ts` 以 `__dirname/browser-page-preload.cjs` 定位）。
    "browser-page-preload": path.join(desktopRoot, "electron", "preload", "browser-page.ts"),
  },
  outdir: electronOutDir,
  outExtension: { ".js": ".cjs" },
  bundle: true,
  platform: "node",
  format: "cjs",
  target: "node22",
  external: ["electron"],
  sourcemap: true,
  logLevel: "warning",
};

/** 宿主运行期读取的静态资源（托盘图标等）随 main 一起放到输出目录，main 以 `__dirname/resources` 定位。 */
export function copyElectronResources() {
  fs.cpSync(path.join(desktopRoot, "electron", "resources"), path.join(electronOutDir, "resources"), { recursive: true });
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  await esbuild.build(electronBuildOptions);
  copyElectronResources();
}

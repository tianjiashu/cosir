import path from "node:path";
import * as esbuild from "esbuild";

import { electronBuildOptions } from "../../scripts/build-electron.mjs";
import { buildDevelopmentTerminalWorker } from "../../scripts/cargo-target.mjs";
import { desktopRoot, targetDirectory } from "../../scripts/project-paths.mjs";

/** 浏览器引擎用例的 Electron 入口打包位置（见 `browser-harness/main.ts`）。 */
export const browserHarnessBundle = path.join(targetDirectory, "electron-browser-harness", "main.cjs");

/**
 * 桌面 E2E 驱动真实宿主，宿主要拉起真实后端，后端需要 Terminal Worker：先构建它并交给用例。
 * 浏览器引擎用例不经后端，单独打包一个只含 `BrowserHost` 的 Electron 入口。
 */
export default async function globalSetup() {
  process.env.CODING_AGENT_TERMINAL_WORKER = buildDevelopmentTerminalWorker();
  await esbuild.build({
    ...electronBuildOptions,
    entryPoints: [path.join(desktopRoot, "tests", "desktop-e2e", "browser-harness", "main.ts")],
    outdir: undefined,
    outfile: browserHarnessBundle,
    outExtension: undefined,
  });
}

import { defineConfig } from "@playwright/test";

import base from "./playwright.config";
import { OUTPUT_DIR } from "./tests/e2e/e2e-env";

/**
 * 视觉回归：只跑 `*.visual.spec.ts`，与日常 `test:e2e` 分开（`npm run test:e2e:visual`）。
 *
 * 基线入库在 `tests/e2e/__visual__/`，文件名带平台：应用用系统字体栈（`styles/globals.css`），字形随系统变，
 * 基线只在 macOS 录与比；其他平台第一次运行会生成自己的基线而不是失败，不入库。
 * 改了样式要重录：`npm run test:e2e:visual -- --update-snapshots`，并逐张审图。
 */
export default defineConfig({
  ...base,
  testMatch: "**/*.visual.spec.ts",
  testIgnore: undefined,
  outputDir: `${OUTPUT_DIR}-visual`,
  snapshotPathTemplate: "{testDir}/__visual__/{testFileName}/{arg}-{platform}{ext}",
  expect: { toHaveScreenshot: { animations: "disabled", caret: "hide", maxDiffPixelRatio: 0.002 } },
});

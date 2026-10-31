// 检测器：类型系统已能排除的守卫、空捕获与原样重抛（包装项目已有的 ESLint 与 typescript-eslint）。
//
// 对应条款与档位（都是二档，需按标准 §4.3 四步判定确认）：
//   @typescript-eslint/no-unnecessary-condition → F1 不可能情形守卫（类型显示条件恒真 / 恒假、可选链多余）；
//   no-useless-catch                            → F3 捕获后原样抛出；
//   no-empty（含空 catch）                       → F4 吞异常。
// 只在检测时启用这些规则，不改项目的 eslint.config.mjs。只查生产代码（不含 tests、scripts 与配置文件）。
// 类型信息来自两个临时 tsconfig：分别继承渲染进程（apps/desktop/tsconfig.json）与宿主（electron/tsconfig.json），
// 只额外打开 noUncheckedIndexedAccess。项目没开这一项，`record[key]`、`array[i]` 在类型上「必然有值」，
// 对它们的 `?.` / `??` 会被误报为多余；打开后下标访问带上 undefined，这类误报消失（2026-10-31 首轮发现）。
// 已知局限：类型声明本身写错（该允许 null 却没写）时结论不可靠，例如标准库把 JSON.stringify 的返回值声明为 string，
//           实际可能是 undefined；有意的 `while (true)` 已通过 allowConstantLoopConditions 排除；
//           在异步回调里被改写的局部标志（如 effect 清理里的 `active = false`）会被误判为恒真；
//          跨进程输入（后端响应、IPC 入参）的类型只是约定，对它们的守卫不能只凭类型判定为多余。
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, relative } from "node:path";
import { pathToFileURL } from "node:url";
import { desktopRoot, emit, inScope, loadScope, resolveFromDesktop, toRepoRelative } from "./lib.mjs";

const DETECTOR = "eslint-defense";
const RULES = {
  "@typescript-eslint/no-unnecessary-condition": "F1",
  "no-useless-catch": "F3",
  "no-empty": "F4",
};
const scope = loadScope();

const targets =
  scope === null
    ? ["."]
    : [...scope].filter((file) => /^apps\/desktop\/.+\.tsx?$/.test(file)).map((file) => relative("apps/desktop", file));

if (targets.length === 0) process.exit(0);

// 配置写到临时目录；typescript-eslint 按 apps/desktop 解析，与项目用的是同一份。
const workDir = mkdtempSync(join(tmpdir(), "code-simplification-eslint-defense-"));
const configPath = join(workDir, "eslint.config.mjs");
const tsconfigs = [join(desktopRoot, "tsconfig.json"), join(desktopRoot, "electron", "tsconfig.json")].map((base, index) => {
  const path = join(workDir, `tsconfig.${index}.json`);
  writeFileSync(path, JSON.stringify({ extends: base, compilerOptions: { noUncheckedIndexedAccess: true, incremental: false } }));
  return path;
});
writeFileSync(
  configPath,
  `import tseslint from ${JSON.stringify(pathToFileURL(resolveFromDesktop("typescript-eslint")).href)};
export default [
  { ignores: ["node_modules/**", "dist/**", "tests/**", "scripts/**", "**/*.mjs", "*.config.ts"] },
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      parser: tseslint.parser,
      parserOptions: { project: ${JSON.stringify(tsconfigs)}, tsconfigRootDir: ${JSON.stringify(desktopRoot)} },
    },
    plugins: { "@typescript-eslint": tseslint.plugin },
    rules: {
      "@typescript-eslint/no-unnecessary-condition": ["error", { allowConstantLoopConditions: "always" }],
      "no-useless-catch": "error",
      "no-empty": ["error", { allowEmptyCatch: false }],
    },
  },
];
`,
);

try {
  const result = spawnSync("npx", ["eslint", "--no-config-lookup", "-c", configPath, "-f", "json", "--no-warn-ignored", ...targets], {
    cwd: desktopRoot,
    encoding: "utf8",
    maxBuffer: 256 * 1024 * 1024,
  });
  let report;
  try {
    report = JSON.parse(result.stdout);
  } catch {
    process.stderr.write(result.stderr || result.stdout);
    process.exitCode = 2;
    report = [];
  }
  for (const fileResult of report) {
    const file = toRepoRelative(fileResult.filePath);
    if (!inScope(scope, file)) continue;
    for (const message of fileResult.messages) {
      if (message.fatal) {
        // 解析失败（如文件不属于任何 tsconfig）要暴露出来，否则会被误当成「没有发现」。
        process.stderr.write(`${file}: ${message.message}\n`);
        process.exitCode = 2;
        continue;
      }
      const transform = RULES[message.ruleId];
      if (transform) {
        emit(DETECTOR, { transform, tier: 2, file, line: message.line, evidence: `${message.ruleId}：${message.message}` });
      }
    }
  }
} finally {
  rmSync(workDir, { recursive: true, force: true });
}

// 检测器共用的工具：仓库根、本轮范围、前端模块解析与统一输出。
//
// 统一输出：每个候选一行 JSON，写到 stdout，字段为
//   { detector, transform, tier, path, line, symbol, evidence }
// path 为相对仓库根的路径（code-simplification skill 的 seeds.mjs、metrics.mjs 按它归属候选）；
// line、symbol 没有时为 null。run-all.mjs 负责汇总。
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { join, relative } from "node:path";

export const repositoryRoot = execFileSync("git", ["rev-parse", "--show-toplevel"], { encoding: "utf8" }).trim();
export const desktopRoot = join(repositoryRoot, "apps/desktop");

// 检测器依赖的 typescript、eslint 等只装在 apps/desktop 下，按它的 node_modules 解析。
const desktopRequire = createRequire(join(desktopRoot, "package.json"));
export const requireFromDesktop = (id) => desktopRequire(id);
export const resolveFromDesktop = (id) => desktopRequire.resolve(id);

// --scope 指向 scope.json（scope.mjs --json 的输出，files 相对仓库根）；不传时返回 null，表示全量。
export function loadScope(argv = process.argv.slice(2)) {
  const index = argv.indexOf("--scope");
  if (index === -1) return null;
  return new Set(JSON.parse(readFileSync(argv[index + 1], "utf8")).files);
}

// 范围内的文件；全量时取 apps/desktop 下全部受版本控制或未被忽略的文件。
export function scopeFiles(scope) {
  if (scope !== null) return [...scope];
  return execFileSync("git", ["ls-files", "--cached", "--others", "--exclude-standard", "apps/desktop"], {
    cwd: repositoryRoot,
    encoding: "utf8",
  })
    .split("\n")
    .filter(Boolean);
}

export function inScope(scope, repoRelativePath) {
  return scope === null || scope.has(repoRelativePath);
}

export const fromDesktop = (desktopRelativePath) => join("apps/desktop", desktopRelativePath);
export const toRepoRelative = (absolutePath) => relative(repositoryRoot, absolutePath);

export function emit(detector, { transform, tier, path, line = null, symbol = null, evidence }) {
  process.stdout.write(`${JSON.stringify({ detector, transform, tier, path, line, symbol, evidence })}\n`);
}

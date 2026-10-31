// 检测器：前端死文件、死导出、死依赖（包装项目已有的 knip，配置见 apps/desktop/knip.jsonc）。
//
// 对应条款：D2 死符号（未使用的文件、导出、类型），D1 死依赖（未使用的 dependencies）。
// 档位：二档。knip 看不到注册表、按字符串分发与跨进程引用，结果必须按标准 §5.2 排除假死。
// 不报告：unlisted（直接引用了未声明的间接依赖）与 duplicates（同一值的多个导出名），
//         它们是依赖声明与命名问题，不是死代码。
// 已知局限：测试替身、只被测试引用的导出会被当作「有引用」；入口以 knip.jsonc 为准。
import { spawnSync } from "node:child_process";
import { desktopRoot, emit, fromDesktop, inScope, loadScope } from "./lib.mjs";

const DETECTOR = "knip";
const scope = loadScope();

const result = spawnSync("npx", ["knip", "--reporter", "json", "--no-progress"], {
  cwd: desktopRoot,
  encoding: "utf8",
  maxBuffer: 64 * 1024 * 1024,
});
// knip 有发现时以 1 退出；只有输出不是 JSON 才算检测器失败。
let report;
try {
  report = JSON.parse(result.stdout);
} catch {
  process.stderr.write(result.stderr || result.stdout);
  process.exit(2);
}

const KINDS = [
  ["files", "D2", "未使用的文件"],
  ["exports", "D2", "未使用的导出"],
  ["types", "D2", "未使用的导出类型"],
  ["enumMembers", "D2", "未使用的枚举成员"],
  ["dependencies", "D1", "未使用的依赖"],
  ["devDependencies", "D1", "未使用的开发依赖"],
];

for (const issue of report.issues) {
  const file = fromDesktop(issue.file);
  if (!inScope(scope, file) && !issue.file.endsWith("package.json")) continue;
  for (const [kind, transform, label] of KINDS) {
    for (const item of issue[kind] ?? []) {
      emit(DETECTOR, {
        transform,
        tier: 2,
        file,
        line: item.line ?? null,
        symbol: kind === "files" ? null : item.name,
        evidence: `knip：${label}`,
      });
    }
  }
}

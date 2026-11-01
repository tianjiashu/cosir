// 检测器：未使用的局部变量、导入、类型与参数（包装 TypeScript 编译器的 noUnused* 检查）。
//
// 对应条款与档位：
//   未使用的局部变量、导入、私有成员、局部类型 → D1，一档（编译器能看到全部引用）；
//   未使用的参数 → D3，二档（回调、接口实现、事件处理器需要保持签名，不能直接删）。
// 项目的 tsconfig 没有开启 noUnused*，这里只在检测时临时打开，不改项目配置。
// 两个 TypeScript 工程分开检查：渲染进程（apps/desktop/tsconfig.json）与宿主（apps/desktop/electron/tsconfig.json）。
// 已知局限：以 `_` 开头的参数编译器不报；只为副作用存在的导入不会被报（编译器把它当作已使用）。
import { spawnSync } from "node:child_process";
import { desktopRoot, emit, fromDesktop, inScope, loadScope } from "./lib.mjs";

const DETECTOR = "tsc-unused";
const scope = loadScope();
const PROJECTS = [".", "electron"];
// file(line,col): error TS6133: 'name' is declared but its value is never read.
const LINE = /^(.+?)\((\d+),\d+\): error (TS\d+): (.+)$/;

function diagnostics(project, flags) {
  const result = spawnSync("npx", ["tsc", "--noEmit", "--incremental", "false", "--pretty", "false", "-p", project, ...flags], {
    cwd: desktopRoot,
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
  });
  const found = [];
  for (const line of result.stdout.split("\n")) {
    const match = line.match(LINE);
    if (match) found.push({ file: match[1], line: Number(match[2]), code: match[3], message: match[4] });
  }
  return found;
}

for (const project of PROJECTS) {
  // 两个开关分开跑：编译器报告的文案对参数与局部变量相同，只能靠开关区分来源。
  for (const [flag, transform, tier, label] of [
    ["--noUnusedLocals", "D1", 1, "未使用的局部变量、导入或类型"],
    ["--noUnusedParameters", "D3", 2, "未使用的参数"],
  ]) {
    for (const d of diagnostics(project, [flag])) {
      const file = fromDesktop(d.file);
      if (!inScope(scope, file)) continue;
      const symbol = d.message.match(/'([^']+)'/)?.[1] ?? null;
      emit(DETECTOR, { transform, tier, path: file, line: d.line, symbol, evidence: `${d.code} ${label}：${d.message}` });
    }
  }
}
